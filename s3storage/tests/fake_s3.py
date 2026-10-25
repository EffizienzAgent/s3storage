import hashlib
import io
from datetime import UTC, datetime


class FakeClientError(Exception):
	def __init__(self, code):
		super().__init__(code)
		self.response = {"Error": {"Code": code}}


class FakeBody(io.BytesIO):
	def __enter__(self):
		# Like botocore's StreamingBody: the context manager yields the raw stream.
		return io.BytesIO(self.getvalue())

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

	def get_object(self, Bucket, Key, Range=None):
		self.calls.append("get_object")
		obj = self._object(Bucket, Key)
		if not Range:
			return {**self._meta(obj), "Body": FakeBody(obj["Body"])}
		size = len(obj["Body"])
		start, end = Range.removeprefix("bytes=").split("-")
		start, end = (
			(size - int(end), size - 1) if not start else (int(start), min(int(end or size - 1), size - 1))
		)
		if start >= size:
			raise FakeClientError("InvalidRange")
		part = obj["Body"][max(start, 0) : end + 1]
		return {
			**self._meta(obj),
			"ContentLength": len(part),
			"ContentRange": f"bytes {max(start, 0)}-{end}/{size}",
			"Body": FakeBody(part),
		}

	def delete_object(self, Bucket, Key):
		self.calls.append("delete_object")
		self.objects.pop((Bucket, Key), None)

	def copy(self, CopySource, Bucket, Key, ExtraArgs=None, Config=None):
		self.calls.append("copy")
		obj = dict(self._object(CopySource["Bucket"], CopySource["Key"], missing="404"))
		extra = ExtraArgs or {}
		if extra.get("MetadataDirective") == "REPLACE":
			obj |= {"ContentType": extra.get("ContentType"), "Metadata": dict(extra.get("Metadata") or {})}
		self.objects[(Bucket, Key)] = obj

	def upload_fileobj(self, Fileobj, Bucket, Key, ExtraArgs=None, Config=None):
		self.calls.append("upload_fileobj")
		extra = ExtraArgs or {}
		self.put_object(Bucket, Key, Fileobj.read(), extra.get("ContentType"), extra.get("Metadata"))

	def upload_file(self, Filename, Bucket, Key, ExtraArgs=None, Config=None):
		with open(Filename, "rb") as f:
			self.upload_fileobj(f, Bucket, Key, ExtraArgs, Config)

	def list_objects_v2(self, Bucket, Prefix="", ContinuationToken=None):
		self.calls.append("list_objects_v2")
		keys = [key for key in self.keys(Bucket) if key.startswith(Prefix)]
		start = int(ContinuationToken or 0)
		page = keys[start : start + self.page_size]
		result = {"Contents": [{"Key": key, "Size": len(self.content(key, Bucket))} for key in page]}
		if start + self.page_size < len(keys):
			result |= {"IsTruncated": True, "NextContinuationToken": str(start + self.page_size)}
		return result
