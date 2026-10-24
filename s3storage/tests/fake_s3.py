import hashlib
import io
from datetime import UTC, datetime


class FakeClientError(Exception):
	def __init__(self, code):
		super().__init__(code)
		self.response = {"Error": {"Code": code}}


class FakeBody(io.BytesIO):
	def iter_chunks(self, chunk_size=1024):
		while chunk := self.read(chunk_size):
			yield chunk


class FakeS3:
	def __init__(self, page_size=1000):
		self.objects = {}
		self.calls = []
		self.page_size = page_size

	def _object(self, Bucket, Key, missing="NoSuchKey"):
		try:
			return self.objects[(Bucket, Key)]
		except KeyError:
			raise FakeClientError(missing)

	def keys(self, bucket="files"):
		return sorted(key for b, key in self.objects if b == bucket)

	def content(self, key, bucket="files"):
		return self.objects[(bucket, key)]["Body"]

	def put_object(self, Bucket, Key, Body, ContentType=None, Metadata=None):
		self.calls.append("put_object")
		self.objects[(Bucket, Key)] = {
			"Body": bytes(Body),
			"ContentType": ContentType,
			"Metadata": dict(Metadata or {}),
			"LastModified": datetime.now(UTC),
		}

	def _meta(self, obj):
		return {
			"ContentLength": len(obj["Body"]),
			"ContentType": obj["ContentType"],
			"Metadata": obj["Metadata"],
			"ETag": f'"{hashlib.md5(obj["Body"]).hexdigest()}"',
			"LastModified": obj["LastModified"],
		}

	def head_object(self, Bucket, Key):
		self.calls.append("head_object")
		return self._meta(self._object(Bucket, Key, missing="404"))

	def get_object(self, Bucket, Key):
		self.calls.append("get_object")
		obj = self._object(Bucket, Key)
		return {**self._meta(obj), "Body": FakeBody(obj["Body"])}

	def delete_object(self, Bucket, Key):
		self.calls.append("delete_object")
		self.objects.pop((Bucket, Key), None)

	def copy_object(self, Bucket, Key, CopySource):
		self.calls.append("copy_object")
		self.objects[(Bucket, Key)] = dict(self._object(CopySource["Bucket"], CopySource["Key"]))

	def list_objects_v2(self, Bucket, Prefix="", ContinuationToken=None):
		self.calls.append("list_objects_v2")
		keys = [key for key in self.keys(Bucket) if key.startswith(Prefix)]
		start = int(ContinuationToken or 0)
		page = keys[start : start + self.page_size]
		result = {"Contents": [{"Key": key, "Size": len(self.content(key, Bucket))} for key in page]}
		if start + self.page_size < len(keys):
			result |= {"IsTruncated": True, "NextContinuationToken": str(start + self.page_size)}
		return result
