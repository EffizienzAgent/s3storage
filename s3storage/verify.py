import os
import random
from collections import Counter

import frappe

from s3storage import storage

PROBLEMS = ("missing", "size_mismatch", "hash_mismatch", "unsafe_url", "failed")
MAX_LISTED = 100


def run(sample=None):
	if not storage.is_active():
		return {
			"active": False,
			"message": "S3 storage inactive: the s3_files_* site config keys are not set",
		}

	urls = file_urls()
	to_hash = {url for url, info in urls.items() if info.hashes}
	if sample is not None and sample < len(to_hash):
		to_hash = set(random.sample(sorted(to_hash), max(sample, 0)))

	counts = Counter()
	problems = []
	for url, info in urls.items():
		try:
			outcomes = check(url, info, url in to_hash)
		except storage.UnsafeFileURL:
			outcomes = ["unsafe_url"]
		except Exception:
			frappe.log_error(f"S3 files verify failed for {url}")
			outcomes = ["failed"]
		for outcome in outcomes:
			counts[outcome] += 1
			if outcome in PROBLEMS and len(problems) < MAX_LISTED:
				problems.append(f"{outcome}: {url}")

	orphans = find_orphans({storage.safe_key_for(url) for url in urls})
	counts["orphan"] = orphans["count"]
	return {
		"active": True,
		"sample": sample,
		"counts": dict(counts),
		"problems": problems,
		"orphans": orphans,
	}


def file_urls():
	urls = {}
	for row in frappe.get_all(
		"File",
		filters={"is_folder": 0},
		fields=["file_url", "thumbnail_url", "file_size", "content_hash"],
		order_by="creation asc",
	):
		for url, size, content_hash in (
			(row.file_url, row.file_size, row.content_hash),
			(row.thumbnail_url, None, None),
		):
			if not url or not url.startswith(tuple(storage.URL_PREFIXES)):
				continue
			info = urls.setdefault(url, frappe._dict(sizes=set(), hashes=set()))
			if size:
				info.sizes.add(size)
			if content_hash:
				info.hashes.add(content_hash)
	return urls


def check(url, info, hash_content):
	key = storage.key_for(url)
	if not key:
		return ["unsafe_url"]
	meta = storage.head(key)
	if meta is None:
		return ["local_only" if os.path.isfile(storage.local_path(url)) else "missing"]

	outcomes = []
	if any(size != meta["ContentLength"] for size in info.sizes):
		outcomes.append("size_mismatch")
	if hash_content:
		streamed = storage.stream(key)
		if streamed is None:
			return ["missing"]
		outcomes.append("hashed")
		if any(content_hash != streamed[1] for content_hash in info.hashes):
			outcomes.append("hash_mismatch")
	return outcomes if set(outcomes) - {"hashed"} else [*outcomes, "ok"]


def find_orphans(referenced):
	count = size = 0
	keys = []
	for prefix in storage.URL_PREFIXES.values():
		for obj in storage.list_objects(prefix):
			if obj["Key"] in referenced:
				continue
			count += 1
			size += obj.get("Size", 0)
			if len(keys) < MAX_LISTED:
				keys.append(obj["Key"])
	return {"count": count, "bytes": size, "keys": keys}
