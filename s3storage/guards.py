import frappe


def files_using(file_url, exclude=None):
	if not file_url:
		return []
	names = set()
	for field in ("file_url", "thumbnail_url"):
		filters = {field: file_url}
		if exclude:
			filters["name"] = ("!=", exclude)
		names.update(frappe.get_all("File", filters=filters, pluck="name"))
	return sorted(names)


def affected_files(file_url, content_hash=None, exclude=None):
	names = set(files_using(file_url, exclude))
	if content_hash:
		filters = {"content_hash": content_hash}
		if exclude:
			filters["name"] = ("!=", exclude)
		names.update(frappe.get_all("File", filters=filters, pluck="name"))
	return [frappe.get_doc("File", name) for name in sorted(names)]


def readable_by_user(file_url):
	for row in frappe.get_all("File", filters={"file_url": file_url}, fields=["name", "file_url"]):
		if row.file_url == file_url and frappe.get_doc("File", row.name).is_downloadable():
			return True
	return False


def before_file_delete(file):
	for method in frappe.get_hooks("s3storage_before_file_delete"):
		frappe.get_attr(method)(file)


def before_object_delete(file_url):
	run_object_hooks("s3storage_before_object_delete", file_url)


def before_object_write(file_url):
	run_object_hooks("s3storage_before_object_write", file_url)


def run_object_hooks(hook, file_url):
	from s3storage import storage

	for file in affected_files(file_url):
		before_file_delete(file)
	key = storage.safe_key_for(file_url)
	for method in frappe.get_hooks(hook):
		frappe.get_attr(method)(file_url=file_url, key=key)
