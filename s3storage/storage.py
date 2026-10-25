import hashlib
import mimetypes
import os
import re
import tempfile
from functools import partial

import frappe
from frappe.utils import cint

from s3storage import guards
from s3storage.client import FILES, client_for, target_config

URL_PREFIXES = {"/private/files/": "private/files/", "/files/": "public/files/"}
MISSING_CODES = {"404", "NoSuchKey", "NotFound"}
TAKEN_CODES = {"412", "409", "PreconditionFailed", "ConditionalRequestConflict"}
UNSUPPORTED_CODES = {"501", "NotImplemented"}
UNSAFE_CHARS = re.compile(r"[%?#\\\x00-\x1f\x7f]")
MIB = 1024 * 1024
DEFAULT_CHUNK_SIZE = MIB
DEFAULT_PART_SIZE = 8 * MIB
DEFAULT_MAX_CONCURRENCY = 4
CHUNK_SIZE_RANGE = (4 * 1024, 64 * MIB)
PART_SIZE_RANGE = (5 * MIB, 512 * MIB)
MAX_CONCURRENCY_RANGE = (1, 16)


class UnsafeFileURL(frappe.ValidationError):
	pass


def settings():
	return target_config(FILES)


def is_active():
	return settings() is not None


def client():
	return client_for(settings())


def bucket():
	return settings().bucket


def bounded_setting(key, default, bounds):
	value = cint(frappe.conf.get(key))
	if value <= 0:
		return default
	low, high = bounds
	return min(max(value, low), high)


def chunk_size():
	return bounded_setting("s3storage_chunk_size", DEFAULT_CHUNK_SIZE, CHUNK_SIZE_RANGE)


def part_size():
	return bounded_setting("s3storage_part_size", DEFAULT_PART_SIZE, PART_SIZE_RANGE)


def max_concurrency():
	return bounded_setting("s3storage_max_concurrency", DEFAULT_MAX_CONCURRENCY, MAX_CONCURRENCY_RANGE)


def transfer_config():
	from boto3.s3.transfer import TransferConfig

	size, threads = part_size(), max_concurrency()
	config = TransferConfig(
		multipart_threshold=size,
		multipart_chunksize=size,
		max_concurrency=threads,
		use_threads=threads > 1,
		preferred_transfer_client="classic",
	)
	config.max_in_memory_upload_chunks = threads
	return config


def key_for(file_url):
	if not file_url:
		return None
	path = str(file_url)
	for prefix, key_prefix in URL_PREFIXES.items():
		if path.startswith(prefix):
			rel = path[len(prefix) :]
			if UNSAFE_CHARS.search(rel):
				raise UnsafeFileURL(f"Unsafe file URL: {path!r}")
			if not rel or any(part in ("", ".", "..") for part in rel.split("/")):
				return None
			return key_prefix + rel
	return None


def url_for(key):
	for prefix, key_prefix in URL_PREFIXES.items():
		if key.startswith(key_prefix):
			return prefix + key[len(key_prefix) :]
	return None


def safe_key_for(file_url):
	try:
		return key_for(file_url)
	except UnsafeFileURL:
		return None


def local_path(file_url):
	key = key_for(file_url)
	if not key:
		return None
	visibility, rel = key.split("/files/", 1)
	return frappe.get_site_path(visibility, "files", *rel.split("/"))


def read_local(file_url):
	path = local_path(file_url)
	if path and os.path.isfile(path):
		with open(path, "rb") as f:
			return f.read()
	return None


def md5(content):
	return hashlib.md5(content, usedforsecurity=False).hexdigest()


def read_chunks(fileobj):
	return iter(partial(fileobj.read, chunk_size()), b"")


def digest(chunks, out=None):
	md5_hash = hashlib.md5(usedforsecurity=False)
	size = 0
	for chunk in chunks:
		md5_hash.update(chunk)
		size += len(chunk)
		if out is not None:
			out.write(chunk)
	return size, md5_hash.hexdigest()


def local_digest(path):
	if not path or not os.path.isfile(path):
		return None
	with open(path, "rb") as f:
		return digest(read_chunks(f))


def content_type_for(name):
	return mimetypes.guess_type(name)[0] or "application/octet-stream"


def put(key, content, if_absent=False):
	"""Store `content` at `key`. With `if_absent`, return False instead of overwriting an existing object."""
	if isinstance(content, str):
		content = content.encode()
	args = {
		"Bucket": bucket(),
		"Key": key,
		"Body": content,
		"ContentType": content_type_for(key),
		"Metadata": {"md5": md5(content)},
	}
	if if_absent:
		try:
			client().put_object(**args, IfNoneMatch="*")
			return True
		except Exception as e:
			if error_code(e) in TAKEN_CODES:
				return False
			if error_code(e) not in UNSUPPORTED_CODES:
				raise
	client().put_object(**args)
	return True


def put_file(key, source, content_type=None):
	"""Upload a path or binary file object in parts without reading it into memory.

	Runs the `s3storage_before_object_write` hooks for keys of files. Returns `(size, md5)`.
	"""
	url = url_for(key)
	if url and key_for(url):
		guards.before_object_write(url)
	return upload(key, source, content_type)


def upload(key, source, content_type=None):
	is_path = isinstance(source, str | os.PathLike)
	if not is_path and not source.seekable():
		with tempfile.TemporaryFile() as spool:
			digest(read_chunks(source), spool)
			spool.seek(0)
			return upload(key, spool, content_type)

	if is_path:
		if not os.path.isfile(source):
			raise FileNotFoundError(source)
		size, md5_hex = local_digest(source)
		send = partial(client().upload_file, os.fspath(source))
	else:
		start = source.tell()
		size, md5_hex = digest(read_chunks(source))
		source.seek(start)
		send = partial(client().upload_fileobj, source)
	extra = {"ContentType": content_type or content_type_for(key), "Metadata": {"md5": md5_hex}}
	send(bucket(), key, ExtraArgs=extra, Config=transfer_config())
	return size, md5_hex


def error_code(error):
	return str(getattr(error, "response", {}).get("Error", {}).get("Code", ""))


def _missing(error):
	return error_code(error) in MISSING_CODES


def head(key):
	try:
		return client().head_object(Bucket=bucket(), Key=key)
	except Exception as e:
		if _missing(e):
			return None
		raise


def exists(key):
	return head(key) is not None


def open_object(key, byte_range=None):
	kwargs = {"Range": byte_range} if byte_range else {}
	try:
		return client().get_object(Bucket=bucket(), Key=key, **kwargs)
	except Exception as e:
		if _missing(e):
			return None
		raise


def stream(key, out=None):
	obj = open_object(key)
	if obj is None:
		return None
	body = obj["Body"]
	with body:
		return digest(body.iter_chunks(chunk_size()), out)


def list_objects(prefix):
	kwargs = {"Bucket": bucket(), "Prefix": prefix}
	while True:
		page = client().list_objects_v2(**kwargs)
		yield from page.get("Contents", [])
		if not page.get("IsTruncated"):
			return
		kwargs["ContinuationToken"] = page["NextContinuationToken"]


def get(key):
	obj = open_object(key)
	if obj is None:
		return None
	with obj["Body"] as body:
		return body.read()


def delete(key):
	client().delete_object(Bucket=bucket(), Key=key)


def copy(source_key, target_key):
	meta = head(source_key) or {}
	extra = {
		"ContentType": meta.get("ContentType") or content_type_for(target_key),
		"Metadata": meta.get("Metadata") or {},
		"MetadataDirective": "REPLACE",
	}
	client().copy(
		{"Bucket": bucket(), "Key": source_key},
		bucket(),
		target_key,
		ExtraArgs=extra,
		Config=transfer_config(),
	)


def matches(meta, size, digest):
	if not meta or meta.get("ContentLength") != size:
		return False
	stored = (meta.get("Metadata") or {}).get("md5") or meta.get("ETag", "").strip('"')
	return stored == digest


def holds(key, size, digest):
	"""Check an object against a known size and MD5 without trusting its `md5` metadata."""
	meta = head(key)
	if not meta or meta.get("ContentLength") != size:
		return False
	return meta.get("ETag", "").strip('"') == digest or stream(key) == (size, digest)


def read_file_url(file_url):
	key = key_for(file_url)
	if not key:
		return None
	content = get(key)
	return content if content is not None else read_local(file_url)


def file_exists(file_url):
	key = key_for(file_url)
	if not key:
		return False
	path = local_path(file_url)
	return bool(path and os.path.isfile(path)) or (is_active() and exists(key))
