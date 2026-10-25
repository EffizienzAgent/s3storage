import os

import frappe
from frappe import _
from frappe.core.doctype.file.utils import delete_file, get_file_name, get_safe_file_name
from frappe.utils import cint
from frappe.utils.file_manager import save_file_on_filesystem

from s3storage import guards, storage

MAX_NAME_ATTEMPTS = 10


def write_file(file, content=None, content_type=None, is_private=0):
	if isinstance(file, str):
		return _write_named(file, content, content_type, is_private)
	if not storage.is_active():
		local_url = file_url_for(get_safe_file_name(file.file_name), file.is_private)
		if not file.is_new() and local_url == file.file_url:
			guards.before_object_write(local_url)
		return file.save_file_on_filesystem()
	if file.is_remote_file:
		return None

	if isinstance(file._content, str):
		file._content = file._content.encode()
	file.check_content()

	name = get_safe_file_name(file.file_name)
	url = file_url_for(name, file.is_private)
	if file.is_new() or url != file.file_url:
		name = free_name(name, file.content_hash, file.is_private)
		url = file_url_for(name, file.is_private)
	else:
		guards.before_object_write(url)

	store(url, file._content)
	file.file_name = name
	file.file_url = url
	frappe.db.after_rollback.add(file.on_rollback)
	return {"file_name": name, "file_url": url}


def _write_named(fname, content, content_type, is_private):
	if not storage.is_active():
		return save_file_on_filesystem(fname, content, content_type=content_type, is_private=is_private)
	if isinstance(content, str):
		content = content.encode()
	name = free_name(get_safe_file_name(fname), storage.md5(content), is_private)
	url = file_url_for(name, is_private)
	store(url, content)
	return {"file_name": name, "file_url": url}


def insert_file(file_name, source, is_private=1, ignore_permissions=False, **fields):
	"""Store a path or binary file object as a new File, in parts and without reading it into memory."""
	url = file_url_for(free_name(get_safe_file_name(file_name), None, is_private), is_private)
	if storage.is_active():
		size, content_hash = storage.put_file(storage.key_for(url), source)
	else:
		guards.before_object_write(url)
		size, content_hash = write_local(url, source)
	frappe.db.after_rollback.add(lambda: discard(url))
	try:
		return register_file(url, size, content_hash, ignore_permissions, **fields)
	except Exception:
		discard(url)
		raise


def register_file(file_url, file_size, content_hash, ignore_permissions=False, **fields):
	"""Insert a File row for an object that is already stored, without reading its content."""
	file = frappe.get_doc(
		{
			"doctype": "File",
			"file_url": file_url,
			"file_size": file_size,
			"content_hash": content_hash,
			"is_private": cint(file_url.startswith("/private/")),
			**fields,
		}
	)
	file.flags.copy_from_existing_file = True
	file.flags.s3storage_new_object = True
	return file.insert(ignore_permissions=ignore_permissions)


def write_local(url, source):
	path = storage.local_path(url)
	os.makedirs(os.path.dirname(path), exist_ok=True)
	with open(path, "xb") as out:
		if not isinstance(source, str | os.PathLike):
			return storage.digest(storage.read_chunks(source), out)
		with open(source, "rb") as f:
			return storage.digest(storage.read_chunks(f), out)


def discard(url):
	if storage.is_active():
		storage.delete(storage.key_for(url))
	delete_file(url)


def delete_file_data_content(file, only_thumbnail=False):
	urls = [file.thumbnail_url] if only_thumbnail else [file.file_url, file.thumbnail_url]
	if not storage.is_active():
		for url in urls:
			if storage.safe_key_for(url):
				guards.before_object_delete(url)
		return file.delete_file_from_filesystem(only_thumbnail=only_thumbnail)

	for url in urls:
		key = storage.safe_key_for(url)
		if not key or is_shared(url, file.name):
			continue
		guards.before_object_delete(url)
		storage.delete(key)
		delete_file(url)


def store(url, content):
	storage.put(storage.key_for(url), content)
	delete_file(url)


def file_url_for(name, is_private):
	return f"/private/files/{name}" if cint(is_private) else f"/files/{name}"


def is_taken(url):
	return bool(guards.files_using(url)) or storage.file_exists(url)


def free_name(name, content_hash=None, is_private=0):
	if not is_taken(file_url_for(name, is_private)):
		return name
	if content_hash:
		candidate = get_file_name(name, content_hash[-6:])
		if not is_taken(file_url_for(candidate, is_private)):
			return candidate
	for _attempt in range(MAX_NAME_ATTEMPTS):
		candidate = get_file_name(name)
		if not is_taken(file_url_for(candidate, is_private)):
			return candidate
	frappe.throw(_("No free file name for {0}").format(name))


def is_shared(url, name):
	return bool(guards.files_using(url, exclude=name))
