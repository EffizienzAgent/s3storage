import threading
from functools import wraps

import frappe

from s3storage import serve, storage

_lock = threading.Lock()
_applied = False


def apply():
	global _applied
	if _applied:
		return
	with _lock:
		if _applied:
			return
		import frappe.email.email_body as email_body
		import frappe.utils.response as response

		_wrap(response, "download_private_file", serve.download_private_file)
		_wrap(email_body, "get_filecontent_from_path", get_filecontent_from_path)
		try:
			from frappe.utils.typst_emitter import TypstEmitter
		except ImportError:
			pass
		else:
			_wrap(TypstEmitter, "_read_site_file", read_site_file)
		_applied = True


def _wrap(owner, name, replacement):
	original = getattr(owner, name)

	@wraps(original)
	def wrapper(*args, **kwargs):
		return replacement(*args, original=original, **kwargs)

	setattr(owner, name, wrapper)


def get_filecontent_from_path(path, original):
	if path and storage.is_active():
		try:
			key = storage.key_for(path if path.startswith("/") else "/" + path)
		except storage.UnsafeFileURL:
			return None
		content = storage.get(key) if key else None
		if content is not None:
			return content
	return original(path)


def read_site_file(emitter, src, original):
	content = original(emitter, src)
	if content is not None or not src or not storage.is_active():
		return content
	url = src.split("?", 1)[0]
	key = storage.safe_key_for(url)
	if not key or (url.startswith("/private/files/") and not can_read(url)):
		return None
	return storage.get(key)


def can_read(file_url):
	names = frappe.get_all("File", filters={"file_url": file_url}, pluck="name")
	return any(frappe.has_permission("File", doc=name, ptype="read") for name in names)
