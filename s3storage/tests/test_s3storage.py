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

from s3storage import backend, migrate, overrides, storage, verify
from s3storage import client as s3client
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


def png(color="red"):
	from PIL import Image

	buffer = io.BytesIO()
	Image.new("RGB", (640, 480), color).save(buffer, format="PNG")
	return buffer.getvalue()


def pdf():
	from pypdf import PdfWriter

	writer = PdfWriter()
	writer.add_blank_page(width=200, height=200)
	buffer = io.BytesIO()
	writer.write(buffer)
	return buffer.getvalue()


def unique_png():
	return png(f"#{uuid.uuid4().hex[:6]}")


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

	def test_insert_file_stores_stream_and_registers_row(self):
		file = backend.insert_file(f"{self.tag}.zip", io.BytesIO(b"zip"), attached_to_doctype="User")
		self.assertEqual(self.s3.content(storage.key_for(file.file_url)), b"zip")
		self.assertEqual((file.file_size, file.content_hash), (3, storage.md5(b"zip")))
		self.assertEqual(frappe.get_doc("File", file.name).get_content(), "zip")

	def test_insert_file_on_local_disk(self):
		with patch.dict(frappe.local.conf, UNSET):
			file = backend.insert_file(f"{self.tag}.zip", io.BytesIO(b"zip"), is_private=0)
			with open(file.get_full_path(), "rb") as f:
				self.assertEqual(f.read(), b"zip")
			self.assertEqual(
				(file.file_url, file.content_hash), (f"/files/{self.tag}.zip", storage.md5(b"zip"))
			)
			file.delete()
		self.assertEqual(self.s3.keys(), [])

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
		self.assertEqual(resp.mimetype, "application/octet-stream")

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


class TestVerify(S3FilesTestCase):
	def test_reports_missing_objects_and_mismatches(self):
		ok = make_file(f"{self.tag}-ok.txt", b"ok")
		gone = make_file(f"{self.tag}-gone.txt", b"gone")
		self.s3.delete_object(Bucket="files", Key=storage.key_for(gone.file_url))
		resized = make_file(f"{self.tag}-size.txt", b"size")
		frappe.db.set_value("File", resized.name, "file_size", 999)
		changed = make_file(f"{self.tag}-hash.txt", b"hash")
		self.s3.objects[("files", storage.key_for(changed.file_url))]["Body"] = b"HASH"
		with patch.dict(frappe.local.conf, UNSET):
			local = make_file(f"{self.tag}-local.txt", b"local")
		self.addCleanup(os.remove, local.get_full_path())

		result = verify.run()
		self.assertGreaterEqual(result["counts"]["ok"], 1)
		self.assertGreaterEqual(result["counts"]["local_only"], 1)
		self.assertIn(f"missing: {gone.file_url}", result["problems"])
		self.assertIn(f"size_mismatch: {resized.file_url}", result["problems"])
		self.assertIn(f"hash_mismatch: {changed.file_url}", result["problems"])
		self.assertFalse([p for p in result["problems"] if p.endswith((ok.file_url, local.file_url))])

	def test_is_read_only(self):
		make_file(f"{self.tag}.txt", self.tag.encode())
		self.s3.put_object(Bucket="files", Key=f"public/files/{self.tag}-orphan.txt", Body=b"x")
		self.s3.calls.clear()
		verify.run()
		self.assertLessEqual(set(self.s3.calls), {"head_object", "get_object", "list_objects_v2"})
		self.assertFalse(os.path.exists(storage.local_path(f"/files/{self.tag}-orphan.txt")))

	def test_lists_orphans_across_pages(self):
		file = make_file(f"{self.tag}.png", png())
		file.make_thumbnail()
		orphans = [f"public/files/{self.tag}-orphan.txt", f"private/files/{self.tag}-orphan.txt"]
		for key in [*orphans, f"archive/{self.tag}.txt"]:
			self.s3.put_object(Bucket="files", Key=key, Body=b"orphan")
		self.s3.page_size = 1

		result = verify.run()
		self.assertEqual(sorted(result["orphans"]["keys"]), sorted(orphans))
		self.assertEqual(result["orphans"]["count"], 2)
		self.assertEqual(result["orphans"]["bytes"], 12)
		self.assertEqual(result["counts"]["orphan"], 2)

	def test_sample_limits_hashing(self):
		for suffix in "abc":
			file = make_file(f"{self.tag}-{suffix}.txt", suffix.encode())
		self.s3.objects[("files", storage.key_for(file.file_url))]["Body"] = b"C"
		self.s3.calls.clear()

		result = verify.run(sample=0)
		self.assertNotIn("hashed", result["counts"])
		self.assertNotIn("get_object", self.s3.calls)
		self.assertNotIn(f"hash_mismatch: {file.file_url}", result["problems"])

		result = verify.run()
		self.assertGreaterEqual(result["counts"]["hashed"], 3)
		self.assertIn(f"hash_mismatch: {file.file_url}", result["problems"])

	def test_inactive_verify_does_nothing(self):
		with patch.dict(frappe.local.conf, UNSET):
			self.assertFalse(verify.run()["active"])


class TestMigrationToLocal(S3FilesTestCase):
	def local(self, file_url):
		path = storage.local_path(file_url)
		self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
		return path

	def test_dry_run_then_download_is_idempotent_and_keeps_rows_and_objects(self):
		file = make_file(f"{self.tag}.txt", self.tag.encode())
		row = frappe.db.get_value("File", file.name, ["file_url", "is_private", "content_hash"], as_dict=True)
		path = self.local(file.file_url)
		self.assertFalse(os.path.exists(path))

		result = migrate.run(dry_run=True, to_local=True)
		self.assertEqual(result["direction"], "to_local")
		self.assertGreaterEqual(result["counts"]["would_download"], 1)
		self.assertFalse(os.path.exists(path))

		result = migrate.run(to_local=True)
		self.assertGreaterEqual(result["counts"]["downloaded"], 1)
		with open(path, "rb") as f:
			self.assertEqual(f.read(), self.tag.encode())
		self.assertFalse([n for n in os.listdir(os.path.dirname(path)) if n.endswith(migrate.PART_SUFFIX)])
		self.assertIn(storage.key_for(file.file_url), self.s3.keys())

		result = migrate.run(to_local=True)
		self.assertNotIn("downloaded", result["counts"])
		self.assertGreaterEqual(result["counts"]["already_local"], 1)

		with patch.dict(frappe.local.conf, UNSET):
			doc = frappe.get_doc("File", file.name)
			self.assertTrue(os.path.samefile(doc.get_full_path(), path))
			self.assertEqual(doc.get_content(), self.tag)
		self.assertEqual(
			frappe.db.get_value("File", file.name, ["file_url", "is_private", "content_hash"], as_dict=True),
			row,
		)

	def test_private_files_and_thumbnails_land_where_frappe_reads_them(self):
		private = make_file(f"{self.tag}.csv", b"a,1", is_private=1)
		image = make_file(f"{self.tag}.png", png())
		thumbnail_url = image.make_thumbnail()
		paths = [self.local(url) for url in (private.file_url, image.file_url, thumbnail_url)]

		migrate.run(to_local=True)
		self.assertTrue(all(os.path.isfile(path) for path in paths))
		self.assertIn(os.path.join("private", "files"), paths[0])
		with patch.dict(frappe.local.conf, UNSET):
			self.assertTrue(frappe.get_doc("File", private.name).exists_on_disk())
			self.assertTrue(frappe.get_doc("File", image.name).exists_on_disk())

	def test_differing_local_file_is_reported_not_overwritten(self):
		file = make_file(f"{self.tag}.txt", b"remote")
		path = self.local(file.file_url)
		with open(path, "wb") as f:
			f.write(b"stale")

		result = migrate.run(to_local=True)
		self.assertIn(f"local_differs: {file.file_url}", result["problems"])
		with open(path, "rb") as f:
			self.assertEqual(f.read(), b"stale")

	def test_failed_download_leaves_no_partial_file_and_resumes(self):
		file = make_file(f"{self.tag}.txt", b"remote")
		path = self.local(file.file_url)

		def broken(key, out=None):
			out.write(b"rem")
			raise ConnectionError("dropped")

		with patch.object(storage, "stream", side_effect=broken), patch.object(frappe, "log_error"):
			result = migrate.run(to_local=True)
		self.assertIn(f"failed: {file.file_url}", result["problems"])
		self.assertFalse(os.path.exists(path))
		self.assertFalse([n for n in os.listdir(os.path.dirname(path)) if n.endswith(migrate.PART_SUFFIX)])

		self.assertGreaterEqual(migrate.run(to_local=True)["counts"]["downloaded"], 1)
		with open(path, "rb") as f:
			self.assertEqual(f.read(), b"remote")

	def test_download_not_matching_object_metadata_is_discarded(self):
		file = make_file(f"{self.tag}.txt", b"remote")
		path = self.local(file.file_url)
		self.s3.objects[("files", storage.key_for(file.file_url))]["Metadata"]["md5"] = "0" * 32

		result = migrate.run(to_local=True)
		self.assertIn(f"verify_failed: {file.file_url}", result["problems"])
		self.assertFalse(os.path.exists(path))


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


def refuse_protected(file):
	if file.name in (frappe.flags.s3storage_protected or ()):
		raise DeleteRefused(f"{file.name} is protected")


def record_delete(file_url=None, *, key=None):
	frappe.flags.s3storage_changes.append(("delete", file_url, key))


def record_write(file_url=None, *, key=None):
	frappe.flags.s3storage_changes.append(("write", file_url, key))


def refuse_object(file_url=None, *, key=None):
	raise DeleteRefused(f"{key} is protected")


class TestInlineServing(S3FilesTestCase):
	def render(self, file):
		self.request(file.file_url)
		if file.is_private:
			return response.download_private_file(file.file_url)
		return PublicFilePage(file.file_url).render()

	def assert_download_only(self, resp):
		self.assertTrue(resp.headers["Content-Disposition"].startswith("attachment"))
		self.assertEqual(resp.mimetype, "application/octet-stream")
		self.assertEqual(resp.headers["Content-Security-Policy"], "sandbox; default-src 'none'")
		self.assertEqual(resp.headers["X-Content-Type-Options"], "nosniff")

	def test_script_capable_types_are_download_only(self):
		payload = b"<x:script xmlns:x='http://www.w3.org/1999/xhtml'>alert(1)</x:script>"
		for ext in ("xpdl", "rdf", "wsdl", "xml", "svg", "xhtml", "js"):
			for is_private in (0, 1):
				file = make_file(f"{self.tag}-{is_private}.{ext}", payload, is_private=is_private)
				self.assert_download_only(self.render(file))

	def test_type_comes_from_file_url_not_file_name(self):
		file = make_file(f"{self.tag}.rdf", b"<rdf/>", is_private=1)
		frappe.db.set_value("File", file.name, "file_name", f"{self.tag}.png")
		resp = self.render(frappe.get_doc("File", file.name))
		self.assert_download_only(resp)

	def test_allowlisted_types_are_inline_and_sandboxed(self):
		file = make_file(f"{self.tag}.png", png())
		resp = self.render(file)
		self.assertTrue(resp.headers["Content-Disposition"].startswith("inline"))
		self.assertEqual(resp.mimetype, "image/png")
		self.assertEqual(resp.headers["Content-Security-Policy"], "sandbox; default-src 'none'")

	def test_pdf_is_inline_without_csp_so_browsers_render_it(self):
		for is_private in (0, 1):
			file = make_file(f"{self.tag}-{is_private}.pdf", pdf(), is_private=is_private)
			resp = self.render(file)
			self.assertEqual(resp.mimetype, "application/pdf")
			self.assertTrue(resp.headers["Content-Disposition"].startswith("inline"))
			self.assertEqual(resp.headers["X-Content-Type-Options"], "nosniff")
			self.assertNotIn("Content-Security-Policy", resp.headers)

	def test_html_is_inline_in_a_script_sandbox_without_same_origin(self):
		page = b"<html><body><script>document.write(6*7)</script></body></html>"
		file = make_file(f"{self.tag}.html", page, is_private=1)
		resp = self.render(file)
		self.assertEqual(resp.status_code, 200)
		self.assertEqual(body(resp), page)
		self.assertEqual(resp.mimetype, "text/html")
		self.assertTrue(resp.headers["Content-Disposition"].startswith("inline"))
		csp = resp.headers["Content-Security-Policy"]
		self.assertTrue(csp.startswith("sandbox allow-scripts"))
		self.assertNotIn("allow-same-origin", csp)

	def test_html_inline_can_be_turned_off(self):
		file = make_file(f"{self.tag}.htm", b"<p>x</p>", is_private=1)
		with patch.dict(frappe.local.conf, {"s3storage_html_inline": 0}):
			resp = self.render(file)
		self.assert_download_only(resp)
		self.assertEqual(body(resp), b"<p>x</p>")

	def test_local_fallback_gets_the_same_headers(self):
		with patch.dict(frappe.local.conf, UNSET):
			private = make_file(f"{self.tag}.rdf", b"<rdf/>", is_private=1)
			public = make_file(f"{self.tag}.wsdl", b"<wsdl/>")
		for file in (private, public):
			self.addCleanup(os.remove, file.get_full_path())
			resp = self.render(file)
			resp.direct_passthrough = False
			self.assert_download_only(resp)


class TestPrivateFileAccess(S3FilesTestCase):
	def setUp(self):
		super().setUp()
		self.victim = make_user(f"{self.tag}-victim@example.com")
		self.attacker = make_user(f"{self.tag}-attacker@example.com")
		frappe.set_user(self.victim)
		self.secret = make_file(f"{self.tag}-secret.txt", b"victim secret", is_private=1)
		frappe.set_user(self.attacker)

	def case_variant_row(self, content):
		own = make_file(f"{self.tag}-own.txt", content, is_private=1)
		variant = self.secret.file_url.replace("secret", "SECRET")
		frappe.db.set_value("File", own.name, "file_url", variant)
		self.s3.put_object(Bucket="files", Key=storage.key_for(variant), Body=content)
		return variant

	def test_case_variant_row_does_not_open_other_users_file(self):
		variant = self.case_variant_row(b"mine")
		self.request(self.secret.file_url)
		self.assertRaises(Forbidden, response.download_private_file, self.secret.file_url)
		self.request(variant)
		self.assertEqual(body(response.download_private_file(variant)), b"mine")

	def test_owner_cannot_repoint_protected_fields(self):
		own = make_file(f"{self.tag}-own.txt", b"mine", is_private=1)
		for field, value in (
			("file_url", self.secret.file_url),
			("thumbnail_url", self.secret.file_url),
			("content_hash", self.secret.content_hash),
		):
			doc = frappe.get_doc("File", own.name)
			doc.set(field, value)
			with self.assertRaises(frappe.PermissionError, msg=field):
				doc.save()
		self.assertEqual(frappe.db.get_value("File", own.name, "file_url"), own.file_url)

	def test_new_row_cannot_point_at_unreadable_object(self):
		self.case_variant_row(b"mine")
		doc = frappe.get_doc({"doctype": "File", "file_url": self.secret.file_url, "is_private": 1})
		with self.assertRaises(frappe.PermissionError):
			doc.insert()

	def test_making_public_cannot_move_other_users_object(self):
		own = make_file(f"{self.tag}-own.txt", b"mine", is_private=1)
		doc = frappe.get_doc("File", own.name)
		doc.file_url = self.secret.file_url
		doc.is_private = 0
		with self.assertRaises(frappe.PermissionError):
			doc.save()
		self.assertEqual(self.s3.content(storage.key_for(self.secret.file_url)), b"victim secret")
		self.assertNotIn(f"public/files/{self.tag}-secret.txt", self.s3.keys())

	def test_making_public_refuses_an_exact_existing_target(self):
		frappe.set_user("Administrator")
		public = make_file(f"{self.tag}-taken.txt", b"public one")
		private = make_file(f"{self.tag}-taken.txt", b"private one", is_private=1)
		self.assertEqual(private.file_url, f"/private/files/{self.tag}-taken.txt")
		private.is_private = 0
		with self.assertRaises(FileExistsError):
			private.save()
		self.assertEqual(self.s3.content(storage.key_for(public.file_url)), b"public one")


class TestSharedObjects(S3FilesTestCase):
	def setUp(self):
		super().setUp()
		self.content = unique_png()
		self.protected = make_file(f"{self.tag}.png", self.content)
		frappe.flags.s3storage_protected = {self.protected.name}
		frappe.flags.s3storage_changes = []
		self.addCleanup(frappe.flags.pop, "s3storage_protected", None)
		self.addCleanup(frappe.flags.pop, "s3storage_changes", None)
		self.key = storage.key_for(self.protected.file_url)
		self.enterContext(
			self.patch_hooks(
				{
					"s3storage_before_file_delete": [f"{__name__}.refuse_protected"],
					"s3storage_before_object_delete": [f"{__name__}.record_delete"],
					"s3storage_before_object_write": [f"{__name__}.record_write"],
				}
			)
		)

	def other_row(self, **fields):
		other = make_file(f"{self.tag}-other.txt", self.tag.encode())
		frappe.db.set_value("File", other.name, fields)
		return frappe.get_doc("File", other.name)

	def test_row_whose_thumbnail_is_the_object_cannot_delete_it(self):
		other = self.other_row(thumbnail_url=self.protected.file_url)
		other.delete()
		self.assertIn(self.key, self.s3.keys())

	def test_row_whose_file_url_is_the_object_cannot_delete_it(self):
		other = self.other_row(file_url=self.protected.file_url, content_hash="unique")
		other.delete()
		self.assertIn(self.key, self.s3.keys())

	def test_making_a_sharing_row_private_is_refused_by_the_hook(self):
		copy = make_file(f"{self.tag}-copy.png", self.content)
		self.assertEqual(copy.file_url, self.protected.file_url)
		copy.is_private = 1
		with self.assertRaises(DeleteRefused):
			copy.save()
		self.assertIn(self.key, self.s3.keys())

	def test_making_a_sharing_row_private_copies_without_deleting(self):
		frappe.flags.s3storage_protected = set()
		copy = make_file(f"{self.tag}-copy.png", self.content)
		copy.is_private = 1
		copy.save()
		self.assertIn(self.key, self.s3.keys())
		self.assertIn(storage.key_for(copy.file_url), self.s3.keys())
		self.assertEqual(
			frappe.db.get_value("File", self.protected.name, "file_url"), self.protected.file_url
		)

	def test_overwrite_of_protected_object_is_refused(self):
		before = self.s3.content(self.key)
		with self.assertRaises(DeleteRefused):
			frappe.get_doc("File", self.protected.name).save_file(content=unique_png(), overwrite=True)
		self.assertEqual(self.s3.content(self.key), before)

	def changes(self):
		return frappe.flags.s3storage_changes

	def test_object_hooks_see_deletes(self):
		frappe.flags.s3storage_protected = set()
		self.protected.delete()
		self.assertIn(("delete", self.protected.file_url, self.key), self.changes())

	def test_object_hooks_see_overwrites(self):
		frappe.flags.s3storage_protected = set()
		frappe.get_doc("File", self.protected.name).save_file(content=unique_png(), overwrite=True)
		self.assertIn(("write", self.protected.file_url, self.key), self.changes())

	def test_object_hooks_see_privacy_moves_and_thumbnails(self):
		frappe.flags.s3storage_protected = set()
		file = frappe.get_doc("File", self.protected.name)
		file.is_private = 1
		file.save()
		new_key = storage.key_for(file.file_url)
		self.assertIn(("write", file.file_url, new_key), self.changes())
		self.assertIn(("delete", self.protected.file_url, self.key), self.changes())
		thumbnail_url = file.make_thumbnail()
		self.assertIn(("write", thumbnail_url, storage.key_for(thumbnail_url)), self.changes())

	def test_refusing_object_hook_keeps_object(self):
		frappe.flags.s3storage_protected = set()
		with self.patch_hooks({"s3storage_before_object_delete": [f"{__name__}.refuse_object"]}):
			self.assertRaises(DeleteRefused, frappe.get_doc("File", self.protected.name).delete)
		self.assertIn(self.key, self.s3.keys())

	def test_object_hooks_run_on_local_disk(self):
		frappe.flags.s3storage_protected = set()
		with patch.dict(frappe.local.conf, UNSET):
			file = make_file(f"{self.tag}-local.txt", self.tag.encode())
			path = file.get_full_path()
			with self.patch_hooks({"s3storage_before_object_delete": [f"{__name__}.refuse_object"]}):
				self.assertRaises(DeleteRefused, file.delete)
			self.assertTrue(os.path.exists(path))
			file.delete()
		self.assertIn(("delete", file.file_url, storage.key_for(file.file_url)), self.changes())
		self.assertFalse(os.path.exists(path))


class TestThumbnails(S3FilesTestCase):
	def test_making_private_moves_the_thumbnail(self):
		file = make_file(f"{self.tag}.png", png())
		file.make_thumbnail()
		file = frappe.get_doc("File", file.name)
		file.is_private = 1
		file.save()
		self.assertEqual(file.thumbnail_url, f"/private/files/{self.tag}_small.png")
		self.assertIn(f"private/files/{self.tag}_small.png", self.s3.keys())
		self.assertNotIn(f"public/files/{self.tag}_small.png", self.s3.keys())

	def test_thumbnail_does_not_overwrite_another_object(self):
		taken = f"public/files/{self.tag}_small.png"
		self.s3.put_object(Bucket="files", Key=taken, Body=b"someone else")
		file = make_file(f"{self.tag}.png", png())
		thumbnail_url = file.make_thumbnail()
		self.assertNotEqual(thumbnail_url, f"/files/{self.tag}_small.png")
		self.assertEqual(self.s3.content(taken), b"someone else")
		self.assertIn(storage.key_for(thumbnail_url), self.s3.keys())
