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
	def __init__(self):
		self.objects = {}
		self.calls = []

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
