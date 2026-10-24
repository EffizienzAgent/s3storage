import base64
import hashlib
import os
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import frappe
import frappe.utils.response as response
from frappe.tests import IntegrationTestCase

from s3storage import client as s3client
from s3storage import migrate, overrides, storage, verify
from s3storage.tests.test_s3storage import DeleteRefused, body, make_file

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
		from werkzeug.test import EnvironBuilder
		from werkzeug.wrappers import Request

		frappe.local.request = Request(EnvironBuilder(path=file.file_url).get_environ())
		self.addCleanup(setattr, frappe.local, "request", None)
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
