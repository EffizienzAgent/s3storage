import io
import os
import uuid
import zipfile
from unittest.mock import patch

import frappe
import frappe.email.email_body as email_body
import frappe.utils.response as response
from frappe.tests import IntegrationTestCase
from werkzeug.exceptions import Forbidden
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from s3storage import client as s3client
from s3storage import migrate, overrides, storage
from s3storage.serve import PublicFilePage
from s3storage.tests.fake_s3 import FakeS3

CONFIG = {
	"s3_files_endpoint": "https://s3.example.com",
	"s3_files_region": "eu-central-1",
	"s3_files_bucket": "files",
	"s3_files_access_key": "key",
	"s3_files_secret_key": "secret",
}
UNSET = dict.fromkeys(CONFIG)


def make_file(file_name, content=b"hello", is_private=0, **fields):
	return frappe.get_doc(
		{"doctype": "File", "file_name": file_name, "content": content, "is_private": is_private, **fields}
	).insert(ignore_permissions=True)


def make_user(email):
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{"doctype": "User", "email": email, "first_name": email.split("@")[0], "send_welcome_email": 0}
		).insert(ignore_permissions=True)
	return email


def png():
	from PIL import Image

	buffer = io.BytesIO()
	Image.new("RGB", (640, 480), "red").save(buffer, format="PNG")
	return buffer.getvalue()


def body(resp):
	return b"".join(resp.response)


class S3FilesTestCase(IntegrationTestCase):
	def setUp(self):
		self.tag = f"s3-{uuid.uuid4().hex[:10]}"
		self.s3 = FakeS3()
		self.enterContext(patch.dict(frappe.local.conf, CONFIG))
		self.enterContext(patch.object(storage, "client", return_value=self.s3))
		self.saved = (getattr(frappe.local, "request", None), frappe.local.form_dict)
		frappe.local.form_dict = frappe._dict()
		self.addCleanup(self.restore)
		overrides.apply()

	def restore(self):
		frappe.local.request, frappe.local.form_dict = self.saved
		frappe.set_user("Administrator")

	def request(self, path, headers=None):
		frappe.local.request = Request(EnvironBuilder(path=path, headers=headers).get_environ())


class TestInactive(IntegrationTestCase):
	def test_unconfigured_module_keeps_local_disk(self):
		with (
			patch.dict(frappe.local.conf, UNSET),
			patch.object(storage, "client", side_effect=AssertionError("S3 used")),
		):
			self.assertFalse(storage.is_active())
			file = make_file(f"inactive-{uuid.uuid4().hex[:8]}.txt")
			self.assertTrue(os.path.exists(file.get_full_path()))
			self.assertEqual(frappe.get_doc("File", file.name).get_content(), "hello")
			file.delete()
			self.assertFalse(os.path.exists(file.get_full_path()))

	def test_partial_config_is_inactive(self):
		with patch.dict(frappe.local.conf, {**CONFIG, "s3_files_secret_key": None}):
			self.assertFalse(storage.is_active())

	def test_real_client_uses_path_style_endpoint(self):
		try:
			import boto3
		except ImportError:
			self.skipTest("boto3 not installed")
		client = s3client.make_client(
			frappe._dict({key.removeprefix("s3_files_"): v for key, v in CONFIG.items()})
		)
		self.assertEqual(client.meta.endpoint_url, CONFIG["s3_files_endpoint"])
		self.assertEqual(client.meta.config.s3["addressing_style"], "path")


class TestStorage(S3FilesTestCase):
	def test_keys_mirror_file_urls(self):
		self.assertEqual(storage.key_for("/files/a.pdf"), "public/files/a.pdf")
		self.assertEqual(storage.key_for("/private/files/a b.pdf"), "private/files/a b.pdf")
		for url in ("/files/../site_config.json", "/files/", "/assets/x.js", "https://x/files/a", None):
			self.assertIsNone(storage.key_for(url))

	def test_encoded_or_odd_urls_are_rejected_not_mapped(self):
		for url in (
			"/private/files/secre%74.pdf",
			"/files/a%2fb.pdf",
			"/files/a\\b.pdf",
			"/files/a\nb.pdf",
			"/files/a\x7fb.pdf",
			"/private/files/a.pdf?fid=1",
		):
			with self.assertRaises(storage.UnsafeFileURL, msg=url):
				storage.key_for(url)

	def test_public_upload_goes_to_s3_only(self):
		file = make_file(f"{self.tag}.txt")
		self.assertEqual(file.file_url, f"/files/{self.tag}.txt")
		self.assertEqual(self.s3.content(f"public/files/{self.tag}.txt"), b"hello")
		self.assertFalse(os.path.exists(file.get_full_path()))
		self.assertEqual(frappe.get_doc("File", file.name).get_content(), "hello")

	def test_private_upload_uses_private_prefix(self):
		file = make_file(f"{self.tag}.csv", b"a,b", is_private=1)
		self.assertEqual(file.file_url, f"/private/files/{self.tag}.csv")
		self.assertIn(f"private/files/{self.tag}.csv", self.s3.keys())

	def test_name_taken_in_s3_gets_suffix(self):
		self.s3.put_object(Bucket="files", Key=f"public/files/{self.tag}.txt", Body=b"other")
		file = make_file(f"{self.tag}.txt", b"mine")
		self.assertNotEqual(file.file_url, f"/files/{self.tag}.txt")
		self.assertEqual(self.s3.content(f"public/files/{self.tag}.txt"), b"other")
		self.assertEqual(self.s3.content(storage.key_for(file.file_url)), b"mine")

	def test_duplicate_content_shares_one_object_until_last_delete(self):
		content = self.tag.encode()
		first = make_file(f"{self.tag}-a.txt", content)
		second = make_file(f"{self.tag}-b.txt", content)
		self.assertEqual(first.file_url, second.file_url)
		key = storage.key_for(first.file_url)

		first.delete()
		self.assertIn(key, self.s3.keys())
		second.delete()
		self.assertNotIn(key, self.s3.keys())

	def test_shared_file_url_is_kept_even_with_differing_hash(self):
		first = make_file(f"{self.tag}.txt")
		copy = first.create_attachment_copy("User", "Administrator", ignore_permissions=True)
		frappe.db.set_value("File", copy.name, "content_hash", "different")
		first.delete()
		self.assertIn(storage.key_for(first.file_url), self.s3.keys())

	def test_rollback_of_new_file_removes_object(self):
		file = make_file(f"{self.tag}.txt", self.tag.encode())
		frappe.db.delete("File", file.name)
		file.on_rollback()
		self.assertNotIn(storage.key_for(file.file_url), self.s3.keys())

	def test_making_private_moves_object(self):
		file = make_file(f"{self.tag}.txt", self.tag.encode())
		file.is_private = 1
		file.save()
		self.assertEqual(file.file_url, f"/private/files/{self.tag}.txt")
		self.assertIn(f"private/files/{self.tag}.txt", self.s3.keys())
		self.assertNotIn(f"public/files/{self.tag}.txt", self.s3.keys())

	def test_thumbnail_is_stored_in_s3(self):
		file = make_file(f"{self.tag}.png", png())
		thumbnail_url = file.make_thumbnail()
		self.assertEqual(thumbnail_url, f"/files/{self.tag}_small.png")
		self.assertIn(f"public/files/{self.tag}_small.png", self.s3.keys())
		file.delete()
		self.assertNotIn(f"public/files/{self.tag}_small.png", self.s3.keys())

	def test_optimize_overwrites_own_object(self):
		file = make_file(f"{self.tag}.png", png())
		file.optimize_file()
		self.assertEqual(file.file_url, f"/files/{self.tag}.png")
		self.assertEqual(self.s3.content(f"public/files/{self.tag}.png"), file._content)

	def test_unzip_reads_archive_from_s3(self):
		archive = io.BytesIO()
		with zipfile.ZipFile(archive, "w") as z:
			z.writestr(f"{self.tag}-inner.txt", "inner")
		file = make_file(f"{self.tag}.zip", archive.getvalue())
		children = file.unzip()
		self.assertEqual([frappe.get_doc("File", child.name).get_content() for child in children], ["inner"])
		self.assertIn(storage.key_for(children[0].file_url), self.s3.keys())

	def test_email_inline_images_read_from_s3(self):
		file = make_file(f"{self.tag}.png", png())
		self.assertEqual(email_body.get_filecontent_from_path(file.file_url), png())
		self.assertIsNone(email_body.get_filecontent_from_path("/files/../site_config.json"))


class TestDoubleEncoding(S3FilesTestCase):
	def setUp(self):
		super().setUp()
		self.victim = make_user(f"{self.tag}-victim@example.com")
		self.attacker = make_user(f"{self.tag}-attacker@example.com")
		frappe.set_user(self.victim)
		self.secret = make_file(f"{self.tag}-secret.png", png(), is_private=1)
		self.encoded = f"/private/files/{self.tag}-secre%2574.png"
		self.s3.calls.clear()
		frappe.set_user(self.attacker)

	def assert_secret_not_read(self):
		self.assertNotIn("get_object", self.s3.calls)

	def test_unsafe_duplicate_row_does_not_block_upload(self):
		own = make_file(f"{self.tag}-own.png", self.tag.encode(), is_private=1)
		frappe.db.set_value("File", own.name, "file_url", self.encoded.replace("%25", "%"))
		copy = make_file(f"{self.tag}-copy.png", self.tag.encode(), is_private=1)
		self.assertEqual(copy.file_url, f"/private/files/{self.tag}-copy.png")

	def test_upload_of_double_encoded_url_is_rejected(self):
		file = frappe.get_doc({"doctype": "File", "file_url": self.encoded, "is_private": 1})
		with self.assertRaises(storage.UnsafeFileURL):
			file.insert(ignore_permissions=True)
		self.assert_secret_not_read()

	def test_row_with_encoded_url_cannot_stream_other_object(self):
		own = make_file(f"{self.tag}-own.png", self.tag.encode(), is_private=1)
		stored = self.encoded.replace("%25", "%")
		frappe.db.set_value("File", own.name, "file_url", stored)
		self.s3.calls.clear()

		self.request(stored)
		self.assertRaises(Forbidden, response.download_private_file, stored)
		with self.assertRaises(storage.UnsafeFileURL):
			frappe.get_doc("File", own.name).get_content()
		self.assertIsNone(email_body.get_filecontent_from_path(stored))
		self.assert_secret_not_read()


class TestServing(S3FilesTestCase):
	def test_private_file_requires_permission(self):
		owner = make_user(f"{self.tag}-owner@example.com")
		other = make_user(f"{self.tag}-other@example.com")
		frappe.set_user(owner)
		file = make_file(f"{self.tag}.csv", b"secret,1", is_private=1)

		self.request(file.file_url)
		resp = response.download_private_file(file.file_url)
		self.assertEqual(body(resp), b"secret,1")
		self.assertIn("private", resp.headers["Cache-Control"])
		self.assertEqual(resp.mimetype, "text/csv")

		frappe.set_user(other)
		self.assertRaises(Forbidden, response.download_private_file, file.file_url)
		frappe.set_user("Guest")
		self.assertRaises(Forbidden, response.download_private_file, file.file_url)

	def test_public_file_route_streams_from_s3(self):
		from frappe.website.serve import get_response

		file = make_file(f"{self.tag}.txt")
		self.request(file.file_url)
		resp = get_response(file.file_url)
		self.assertEqual(resp.status_code, 200)
		self.assertEqual(body(resp), b"hello")
		self.assertIn("public", resp.headers["Cache-Control"])

		self.request(file.file_url, headers={"If-None-Match": resp.headers["ETag"]})
		self.assertEqual(PublicFilePage(file.file_url).render().status_code, 304)

	def test_missing_public_file_is_not_found(self):
		self.request(f"/files/{self.tag}.txt")
		with self.assertRaises(frappe.PageDoesNotExistError):
			PublicFilePage(f"/files/{self.tag}.txt").render()

	def test_svg_is_forced_to_download(self):
		file = make_file(f"{self.tag}.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>")
		self.request(file.file_url)
		resp = PublicFilePage(file.file_url).render()
		self.assertTrue(resp.headers["Content-Disposition"].startswith("attachment"))

	def test_renderer_ignores_other_paths_and_inactive_config(self):
		self.assertFalse(PublicFilePage("desk/user").can_render())
		self.assertTrue(PublicFilePage("files/x.txt").can_render())
		with patch.dict(frappe.local.conf, UNSET):
			self.assertFalse(PublicFilePage("files/x.txt").can_render())


class TestMigration(S3FilesTestCase):
	def test_dry_run_then_upload_is_idempotent_and_keeps_local(self):
		with patch.dict(frappe.local.conf, UNSET):
			file = make_file(f"{self.tag}.txt", self.tag.encode())
		key = storage.key_for(file.file_url)
		local = file.get_full_path()
		self.addCleanup(lambda: os.path.exists(local) and os.remove(local))

		result = migrate.run(dry_run=True)
		self.assertTrue(result["dry_run"])
		self.assertGreaterEqual(result["counts"]["would_upload"], 1)
		self.assertNotIn(key, self.s3.keys())

		result = migrate.run()
		self.assertGreaterEqual(result["counts"]["uploaded"], 1)
		self.assertEqual(self.s3.content(key), self.tag.encode())
		self.assertTrue(os.path.exists(local))

		result = migrate.run()
		self.assertNotIn("uploaded", result["counts"])
		self.assertGreaterEqual(result["counts"]["already_uploaded"], 1)

	def test_local_file_still_readable_before_migration(self):
		with patch.dict(frappe.local.conf, UNSET):
			file = make_file(f"{self.tag}.txt", self.tag.encode())
		self.addCleanup(lambda: os.path.exists(file.get_full_path()) and os.remove(file.get_full_path()))
		self.assertEqual(frappe.get_doc("File", file.name).get_content(), self.tag)
		self.assertTrue(frappe.get_doc("File", file.name).exists_on_disk())

	def test_downloads_fall_back_to_local_file_before_migration(self):
		with patch.dict(frappe.local.conf, UNSET):
			private = make_file(f"{self.tag}.csv", b"local,1", is_private=1)
			public = make_file(f"{self.tag}.txt", b"public local")
		for file in (private, public):
			self.addCleanup(os.remove, file.get_full_path())

		self.request(private.file_url)
		resp = response.download_private_file(private.file_url)
		resp.direct_passthrough = False
		self.assertEqual(resp.get_data(), b"local,1")

		self.request(public.file_url)
		resp = PublicFilePage(public.file_url).render()
		resp.direct_passthrough = False
		self.assertEqual(resp.get_data(), b"public local")
		self.assertEqual(email_body.get_filecontent_from_path(public.file_url), b"public local")
		self.assertEqual(self.s3.keys(), [])

	def test_public_local_fallback_forces_svg_download(self):
		with patch.dict(frappe.local.conf, UNSET):
			file = make_file(f"{self.tag}.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>")
		self.addCleanup(os.remove, file.get_full_path())
		self.request(file.file_url)
		resp = PublicFilePage(file.file_url).render()
		self.assertTrue(resp.headers["Content-Disposition"].startswith("attachment"))
		self.assertEqual(resp.headers["X-Content-Type-Options"], "nosniff")

	def test_inactive_migration_does_nothing(self):
		with patch.dict(frappe.local.conf, UNSET):
			self.assertFalse(migrate.run(dry_run=True)["active"])


class DeleteRefused(frappe.ValidationError):
	pass


def refuse_delete(file):
	raise DeleteRefused(f"{file.file_name} is protected")


def allow_delete(file):
	frappe.flags.s3storage_checked_delete = file.name


class TestBeforeDeleteHook(S3FilesTestCase):
	def test_refusing_hook_keeps_bytes_and_row(self):
		file = make_file(f"{self.tag}.txt", self.tag.encode())
		key = storage.key_for(file.file_url)
		with self.patch_hooks({"s3storage_before_file_delete": [f"{__name__}.refuse_delete"]}):
			self.assertRaises(DeleteRefused, file.delete)
		self.assertIn(key, self.s3.keys())
		self.assertTrue(frappe.db.exists("File", file.name))
		self.assertNotIn("delete_object", self.s3.calls)

	def test_refusing_hook_keeps_local_bytes_when_inactive(self):
		with patch.dict(frappe.local.conf, UNSET):
			file = make_file(f"{self.tag}.txt", self.tag.encode())
			with self.patch_hooks({"s3storage_before_file_delete": [f"{__name__}.refuse_delete"]}):
				self.assertRaises(DeleteRefused, file.delete)
			self.assertTrue(os.path.exists(file.get_full_path()))
			file.delete()

	def test_allowing_hook_sees_file_then_bytes_are_deleted(self):
		file = make_file(f"{self.tag}.txt", self.tag.encode())
		key = storage.key_for(file.file_url)
		with self.patch_hooks({"s3storage_before_file_delete": [f"{__name__}.allow_delete"]}):
			file.delete()
		self.assertEqual(frappe.flags.pop("s3storage_checked_delete"), file.name)
		self.assertNotIn(key, self.s3.keys())


ARCHIVE = {
	"s3_archive_endpoint": "https://archive.example.com",
	"s3_archive_region": "eu-west-1",
	"s3_archive_bucket": "archive",
}


class TestClientFactory(IntegrationTestCase):
	def test_files_target_reads_files_keys(self):
		with patch.dict(frappe.local.conf, CONFIG):
			settings = s3client.target_config()
		self.assertEqual(settings.bucket, "files")
		self.assertEqual(settings.endpoint, CONFIG["s3_files_endpoint"])

	def test_named_target_borrows_files_credentials(self):
		with patch.dict(frappe.local.conf, {**CONFIG, **ARCHIVE}):
			settings = s3client.target_config("archive")
		self.assertEqual(settings.bucket, "archive")
		self.assertEqual((settings.access_key, settings.secret_key), ("key", "secret"))

	def test_named_target_with_own_credentials(self):
		own = {"s3_archive_access_key": "archive-key", "s3_archive_secret_key": "archive-secret"}
		with patch.dict(frappe.local.conf, {**CONFIG, **ARCHIVE, **own}):
			settings = s3client.target_config("archive")
		self.assertEqual(settings.access_key, "archive-key")

	def test_incomplete_target_is_not_configured(self):
		with patch.dict(frappe.local.conf, {**UNSET, **ARCHIVE}):
			self.assertIsNone(s3client.target_config("archive"))
			with self.assertRaises(s3client.NotConfigured):
				s3client.get_client("archive")

	def test_invalid_target_name(self):
		with self.assertRaises(ValueError):
			s3client.target_config("../x")

	def test_get_client_by_target_and_by_explicit_config(self):
		try:
			import boto3
		except ImportError:
			self.skipTest("boto3 not installed")
		with patch.dict(frappe.local.conf, {**CONFIG, **ARCHIVE}):
			client, settings = s3client.get_client("archive")
		self.assertEqual(client.meta.endpoint_url, "https://archive.example.com")
		self.assertEqual(settings.bucket, "archive")

		explicit = {**dict(settings), "endpoint": "https://other.example.com"}
		other = s3client.get_client(config=explicit)[0]
		self.assertEqual(other.meta.endpoint_url, "https://other.example.com")
		self.assertIsNot(other, client)
		self.assertIs(s3client.get_client(config=explicit)[0], other)
		with self.assertRaises(s3client.NotConfigured):
			s3client.get_client(config={"endpoint": "https://other.example.com"})
