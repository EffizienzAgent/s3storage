import hashlib
import mimetypes
import os
import re

import frappe

from s3storage.client import FILES, client_for, target_config

URL_PREFIXES = {"/private/files/": "private/files/", "/files/": "public/files/"}
MISSING_CODES = {"404", "NoSuchKey", "NotFound"}
UNSAFE_CHARS = re.compile(r"[%?#\\\x00-\x1f\x7f]")
CHUNK_SIZE = 1024 * 1024


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


def local_digest(path):
	if not path or not os.path.isfile(path):
		return None
	digest = hashlib.md5(usedforsecurity=False)
	size = 0
	with open(path, "rb") as f:
		while chunk := f.read(CHUNK_SIZE):
			digest.update(chunk)
			size += len(chunk)
	return size, digest.hexdigest()


def content_type_for(name):
	return mimetypes.guess_type(name)[0] or "application/octet-stream"


def put(key, content):
	if isinstance(content, str):
		content = content.encode()
	client().put_object(
		Bucket=bucket(),
		Key=key,
		Body=content,
		ContentType=content_type_for(key),
		Metadata={"md5": md5(content)},
	)


def _missing(error):
	code = str(getattr(error, "response", {}).get("Error", {}).get("Code", ""))
	return code in MISSING_CODES


def head(key):
	try:
		return client().head_object(Bucket=bucket(), Key=key)
	except Exception as e:
		if _missing(e):
			return None
		raise


def exists(key):
	return head(key) is not None


def open_object(key):
	try:
		return client().get_object(Bucket=bucket(), Key=key)
	except Exception as e:
		if _missing(e):
			return None
		raise


def stream(key, out=None):
	obj = open_object(key)
	if obj is None:
		return None
	digest = hashlib.md5(usedforsecurity=False)
	size = 0
	with obj["Body"] as body:
		for chunk in body.iter_chunks(CHUNK_SIZE):
			digest.update(chunk)
			size += len(chunk)
			if out is not None:
				out.write(chunk)
	return size, digest.hexdigest()


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
	client().copy_object(
		Bucket=bucket(),
		Key=target_key,
		CopySource={"Bucket": bucket(), "Key": source_key},
	)


def matches(meta, size, digest):
	if not meta or meta.get("ContentLength") != size:
		return False
	stored = (meta.get("Metadata") or {}).get("md5") or meta.get("ETag", "").strip('"')
	return stored == digest


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
	return bool(path and os.path.isfile(path)) or exists(key)
