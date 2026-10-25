import base64
import hashlib
import io
import os
import tempfile
import tracemalloc
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import frappe
import frappe.utils.response as response
from frappe.tests import IntegrationTestCase
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from s3storage import backend, migrate, overrides, serve, storage, verify
from s3storage import client as s3client
from s3storage.tests.test_s3storage import DeleteRefused, body, make_file, make_user

ENDPOINT = os.environ.get("S3STORAGE_TEST_ENDPOINT")


def test_config(bucket):
	return {
		"s3_files_endpoint": ENDPOINT,
		"s3_files_region": os.environ.get("S3STORAGE_TEST_REGION", "us-east-1"),
		"s3_files_bucket": bucket,
		"s3_files_access_key": os.environ.get("S3STORAGE_TEST_ACCESS_KEY"),
		"s3_files_secret_key": os.environ.get("S3STORAGE_TEST_SECRET_KEY"),
	}


class MinioTestCase(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		if not ENDPOINT:
			raise unittest.SkipTest("S3STORAGE_TEST_ENDPOINT is not set")
		super().setUpClass()
		cls.bucket = f"s3storage-test-{uuid.uuid4().hex[:8]}"
		cls.config = test_config(cls.bucket)
		with patch.dict(frappe.local.conf, cls.config):
			s3client.get_client()[0].create_bucket(Bucket=cls.bucket, ObjectLockEnabledForBucket=True)

	def setUp(self):
		self.tag = f"minio-{uuid.uuid4().hex[:10]}"
		self.enterContext(patch.dict(frappe.local.conf, self.config))
		self.s3 = s3client.get_client()[0]
		overrides.apply()
		self.addCleanup(frappe.set_user, "Administrator")

	def request(self, path, **headers):
		frappe.local.request = Request(EnvironBuilder(path=path, headers=headers).get_environ())
		self.addCleanup(setattr, frappe.local, "request", None)

	def keys(self):
		listing = self.s3.list_objects_v2(Bucket=self.bucket)
		return {item["Key"] for item in listing.get("Contents", [])}


class TestMinio(MinioTestCase):
	def test_file_roundtrip(self):
		file = make_file(f"{self.tag}.txt", b"real s3")
		key = f"public/files/{self.tag}.txt"
		self.assertIn(key, self.keys())
		self.assertEqual(frappe.get_doc("File", file.name).get_content(), "real s3")
		file.delete()
		self.assertNotIn(key, self.keys())

	def test_private_file_streams_with_permission(self):
		file = make_file(f"{self.tag}.csv", b"a,1", is_private=1)
		self.request(file.file_url)
		self.assertEqual(body(response.download_private_file(file.file_url)), b"a,1")

	def test_refusing_hook_keeps_object(self):
		file = make_file(f"{self.tag}.txt", self.tag.encode())
		key = storage.key_for(file.file_url)
		hook = "s3storage.tests.test_s3storage.refuse_delete"
		with self.patch_hooks({"s3storage_before_file_delete": [hook]}):
			self.assertRaises(DeleteRefused, file.delete)
		self.assertEqual(storage.get(key), self.tag.encode())

	def test_migration_uploads_local_file(self):
		with patch.dict(frappe.local.conf, dict.fromkeys(self.config)):
			file = make_file(f"{self.tag}.txt", self.tag.encode())
		self.addCleanup(os.remove, file.get_full_path())
		self.assertGreaterEqual(migrate.run()["counts"]["uploaded"], 1)
		self.assertEqual(storage.get(storage.key_for(file.file_url)), self.tag.encode())
		self.assertNotIn("uploaded", migrate.run()["counts"])

	def test_verify_and_migrate_to_local(self):
		file = make_file(f"{self.tag}.txt", self.tag.encode())
		orphan = f"public/files/{self.tag}-orphan.txt"
		self.s3.put_object(Bucket=self.bucket, Key=orphan, Body=b"orphan")

		result = verify.run()
		self.assertIn(orphan, result["orphans"]["keys"])
		self.assertFalse([p for p in result["problems"] if p.endswith(file.file_url)])

		path = storage.local_path(file.file_url)
		self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
		with patch.object(migrate, "file_urls", return_value=[(file.file_url, file.content_hash)]):
			self.assertEqual(migrate.run(to_local=True)["counts"], {"downloaded": 1})
			self.assertEqual(migrate.run(to_local=True)["counts"], {"already_local": 1})
		with open(path, "rb") as f:
			self.assertEqual(f.read(), self.tag.encode())

	def test_client_factory_supports_object_lock_uploads(self):
		client, settings = s3client.get_client()
		content = b"archived"
		until = datetime.now(UTC) + timedelta(days=1)
		client.put_object(
			Bucket=settings.bucket,
			Key=f"archive/{self.tag}.txt",
			Body=content,
			ContentMD5=base64.b64encode(hashlib.md5(content).digest()).decode(),
			ObjectLockMode="GOVERNANCE",
			ObjectLockRetainUntilDate=until,
		)
		retention = client.get_object_retention(Bucket=settings.bucket, Key=f"archive/{self.tag}.txt")
		self.assertEqual(retention["Retention"]["Mode"], "GOVERNANCE")


PART = 5 * 1024 * 1024
CHUNK = 64 * 1024


class ChunkedReader(io.RawIOBase):
	"""A non-seekable stream that fails on any read larger than one chunk."""

	def __init__(self, content):
		self.source = io.BytesIO(content)

	def readable(self):
		return True

	def read(self, size=-1):
		if size is None or size < 0 or size > CHUNK:
			raise AssertionError(f"read({size}) reads more than one chunk")
		return self.source.read(size)


class TestLargeFiles(MinioTestCase):
	def setUp(self):
		super().setUp()
		self.enterContext(
			patch.dict(
				frappe.local.conf,
				{"s3storage_part_size": PART, "s3storage_chunk_size": CHUNK, "s3storage_max_concurrency": 2},
			)
		)
		self.content = os.urandom(2 * PART + 1024)
		self.md5 = hashlib.md5(self.content).hexdigest()
		self.key = f"private/files/{self.tag}.bin"

	def temp_path(self):
		f = tempfile.NamedTemporaryFile(delete=False)
		self.addCleanup(os.remove, f.name)
		with f:
			f.write(self.content)
		return f.name

	def assert_multipart(self, key):
		meta = storage.head(key)
		self.assertTrue(meta["ETag"].strip('"').endswith("-3"), meta["ETag"])
		self.assertEqual(meta["Metadata"]["md5"], self.md5)
		self.assertTrue(storage.matches(meta, len(self.content), self.md5))
		self.assertEqual(storage.stream(key), (len(self.content), self.md5))
		return meta

	def test_put_file_from_path_uploads_parts_with_bounded_memory(self):
		path = self.temp_path()
		tracemalloc.start()
		try:
			result = storage.put_file(self.key, path)
			peak = tracemalloc.get_traced_memory()[1]
		finally:
			tracemalloc.stop()
		self.assertEqual(result, (len(self.content), self.md5))
		self.assertLess(peak, PART)
		self.assertEqual(self.assert_multipart(self.key)["ContentType"], "application/octet-stream")

	def test_put_file_from_stream_reads_in_chunks(self):
		storage.put_file(self.key, ChunkedReader(self.content), content_type="application/zip")
		self.assertEqual(self.assert_multipart(self.key)["ContentType"], "application/zip")

	def test_put_file_from_seekable_file_object(self):
		storage.put_file(self.key, io.BytesIO(self.content))
		self.assert_multipart(self.key)

	def test_failed_upload_is_aborted(self):
		with patch.object(self.s3, "upload_part", side_effect=RuntimeError("network down")):
			self.assertRaises(RuntimeError, storage.put_file, self.key, self.temp_path())
		self.assertFalse(self.s3.list_multipart_uploads(Bucket=self.bucket).get("Uploads"))
		self.assertNotIn(self.key, self.keys())

	def test_put_file_runs_write_hook(self):
		with self.patch_hooks(
			{"s3storage_before_object_write": ["s3storage.tests.test_s3storage.refuse_object"]}
		):
			self.assertRaises(DeleteRefused, storage.put_file, self.key, self.temp_path())
		self.assertNotIn(self.key, self.keys())

	def test_copy_above_threshold_keeps_metadata(self):
		storage.put_file(self.key, self.temp_path(), content_type="application/zip")
		target = f"private/files/{self.tag}-copy.bin"
		storage.copy(self.key, target)
		self.assertEqual(self.assert_multipart(target)["ContentType"], "application/zip")

	def test_insert_file_registers_without_reading_content(self):
		user = make_user("s3storage-large@example.com")
		frappe.set_user(user)
		with patch.object(storage, "get", side_effect=AssertionError("full read")):
			file = backend.insert_file(f"{self.tag}.zip", self.temp_path(), ignore_permissions=True)
		self.assertEqual(file.file_url, f"/private/files/{self.tag}.zip")
		self.assertEqual(
			(file.file_size, file.content_hash, file.is_private), (len(self.content), self.md5, 1)
		)
		self.assert_multipart(storage.key_for(file.file_url))

		self.request(file.file_url, Range="bytes=-10")
		resp = frappe.utils.response.download_private_file(file.file_url)
		self.assertEqual((resp.status_code, body(resp)), (206, self.content[-10:]))

	def test_insert_file_removes_object_when_the_row_fails(self):
		with self.assertRaises(frappe.LinkValidationError):
			backend.insert_file(
				f"{self.tag}.zip", io.BytesIO(self.content), attached_to_doctype="NoSuchDocType"
			)
		self.assertNotIn(f"private/files/{self.tag}.zip", self.keys())


class TestRangeRequests(MinioTestCase):
	def setUp(self):
		super().setUp()
		self.content = bytes(range(256)) * 4
		self.file = make_file(f"{self.tag}.bin", self.content)
		self.etag = storage.head(storage.key_for(self.file.file_url))["ETag"].strip('"')

	def get(self, **headers):
		self.request(self.file.file_url, **headers)
		return serve.stream(self.file.file_url, self.file.file_name, serve.PUBLIC_CACHE)

	def assert_partial(self, header, start, end):
		resp = self.get(Range=header)
		self.assertEqual(resp.status_code, 206)
		self.assertEqual(resp.headers["Content-Range"], f"bytes {start}-{end}/{len(self.content)}")
		self.assertEqual(resp.content_length, end - start + 1)
		self.assertEqual(body(resp), self.content[start : end + 1])

	def test_full_response_advertises_ranges(self):
		resp = self.get()
		self.assertEqual((resp.status_code, resp.headers["Accept-Ranges"]), (200, "bytes"))
		self.assertEqual(body(resp), self.content)

	def test_first_middle_and_suffix_ranges(self):
		self.assert_partial("bytes=0-9", 0, 9)
		self.assert_partial("bytes=100-299", 100, 299)
		self.assert_partial("bytes=1000-", 1000, 1023)
		self.assert_partial("bytes=-24", 1000, 1023)

	def test_unsatisfiable_range(self):
		resp = self.get(Range="bytes=5000-")
		self.assertEqual(resp.status_code, 416)
		self.assertEqual(resp.headers["Content-Range"], f"bytes */{len(self.content)}")

	def test_if_none_match_wins_over_range(self):
		self.assertEqual(self.get(Range="bytes=0-9", If_None_Match=f'"{self.etag}"').status_code, 304)
		self.assertEqual(self.get(Range="bytes=5000-", If_None_Match=f'"{self.etag}"').status_code, 304)

	def test_if_range_with_other_etag_sends_full_file(self):
		resp = self.get(Range="bytes=0-9", If_Range='"outdated"')
		self.assertEqual((resp.status_code, body(resp)), (200, self.content))
		self.assertEqual(self.get(Range="bytes=0-9", If_Range=f'"{self.etag}"').status_code, 206)

	def test_multiple_ranges_send_full_file(self):
		self.assertEqual(self.get(Range="bytes=0-9,20-29").status_code, 200)
