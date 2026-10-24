from collections import Counter

import frappe

from s3storage import storage

PROBLEMS = ("missing", "hash_mismatch", "verify_failed", "unsafe_url", "failed")
MAX_LISTED = 100


def run(dry_run=False):
	if not storage.is_active():
		return {
			"active": False,
			"message": "S3 storage inactive: the s3_files_* site config keys are not set",
		}

	counts = Counter()
	problems = []
	for file_url, content_hash in file_urls():
		try:
			outcomes = migrate_one(file_url, content_hash, dry_run)
		except storage.UnsafeFileURL:
			outcomes = ["unsafe_url"]
		except Exception:
			frappe.log_error(f"S3 files migration failed for {file_url}")
			outcomes = ["failed"]
		for outcome in outcomes:
			counts[outcome] += 1
			if outcome in PROBLEMS and len(problems) < MAX_LISTED:
				problems.append(f"{outcome}: {file_url}")
	return {"active": True, "dry_run": bool(dry_run), "counts": dict(counts), "problems": problems}


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
