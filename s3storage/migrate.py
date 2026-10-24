import os
import tempfile
from collections import Counter

import frappe

from s3storage import storage

PROBLEMS = ("missing", "hash_mismatch", "verify_failed", "local_differs", "unsafe_url", "failed")
MAX_LISTED = 100
PART_SUFFIX = ".s3storage-part"


def run(dry_run=False, to_local=False):
	if not storage.is_active():
		return {
			"active": False,
			"message": "S3 storage inactive: the s3_files_* site config keys are not set",
		}

	step = download_one if to_local else migrate_one
	counts = Counter()
	problems = []
	for file_url, content_hash in file_urls():
		try:
			outcomes = step(file_url, content_hash, dry_run)
		except storage.UnsafeFileURL:
			outcomes = ["unsafe_url"]
		except Exception:
			frappe.log_error(f"S3 files migration failed for {file_url}")
			outcomes = ["failed"]
		for outcome in outcomes:
			counts[outcome] += 1
			if outcome in PROBLEMS and len(problems) < MAX_LISTED:
				problems.append(f"{outcome}: {file_url}")
	return {
		"active": True,
		"direction": "to_local" if to_local else "to_s3",
		"dry_run": bool(dry_run),
		"counts": dict(counts),
		"problems": problems,
	}


def file_urls():
	urls = {}
	for row in frappe.get_all(
		"File",
		filters={"is_folder": 0},
		fields=["file_url", "thumbnail_url", "content_hash"],
		order_by="creation asc",
	):
		for url, content_hash in ((row.file_url, row.content_hash), (row.thumbnail_url, None)):
			if url and url.startswith(tuple(storage.URL_PREFIXES)) and not urls.get(url):
				urls[url] = content_hash
	return urls.items()


def migrate_one(file_url, content_hash, dry_run):
	key = storage.key_for(file_url)
	content = storage.read_local(file_url)
	meta = storage.head(key)
	if content is None:
		return ["in_s3_only" if meta else "missing"]

	digest = storage.md5(content)
	outcomes = ["hash_mismatch"] if content_hash and content_hash != digest else []
	if storage.matches(meta, len(content), digest):
		return [*outcomes, "already_uploaded"]
	if dry_run:
		return [*outcomes, "would_upload"]
	storage.put(key, content)
	if not storage.matches(storage.head(key), len(content), digest):
		return [*outcomes, "verify_failed"]
	return [*outcomes, "uploaded"]


def download_one(file_url, content_hash, dry_run):
	key = storage.key_for(file_url)
	path = storage.local_path(file_url)
	if not key or not path:
		return ["unsafe_url"]
	meta = storage.head(key)
	local = storage.local_digest(path)
	if meta is None:
		return ["local_only" if local else "missing"]
	if local:
		return ["already_local" if storage.matches(meta, *local) else "local_differs"]
	if dry_run:
		return ["would_download"]

	os.makedirs(os.path.dirname(path), exist_ok=True)
	fd, part = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".", suffix=PART_SUFFIX)
	try:
		os.chmod(part, 0o644)
		with os.fdopen(fd, "wb") as out:
			streamed = storage.stream(key, out)
		if streamed is None:
			return ["missing"]
		if not storage.matches(meta, *streamed):
			return ["verify_failed"]
		os.replace(part, path)
	finally:
		if os.path.exists(part):
			os.remove(part)
	outcomes = ["hash_mismatch"] if content_hash and content_hash != streamed[1] else []
	return [*outcomes, "downloaded"]
