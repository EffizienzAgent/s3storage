import os
from urllib.parse import quote

import frappe
from frappe.website.page_renderers.base_renderer import BaseRenderer
from werkzeug.wrappers import Response

from s3storage import storage

PUBLIC_CACHE = "public, max-age=3600, stale-while-revalidate=86400"
PRIVATE_CACHE = "private, max-age=3600, stale-while-revalidate=86400"
CHUNK_SIZE = 64 * 1024


INLINE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf", "text/plain"}
INLINE_PREFIXES = ("audio/", "video/")
HTML_EXTENSIONS = (".html", ".htm")
STRICT_CSP = "sandbox; default-src 'none'"
HTML_CSP = "sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads"


def html_inline():
	return bool(frappe.conf.get("s3storage_html_inline", True))


def presentation(file_url):
	"""Return (content type, disposition, CSP) for a file, decided by its stored URL only."""
	if str(file_url).lower().endswith(HTML_EXTENSIONS) and html_inline():
		return "text/html", "inline", HTML_CSP
	content_type = storage.content_type_for(file_url)
	if content_type in INLINE_TYPES or content_type.startswith(INLINE_PREFIXES):
		return content_type, "inline", STRICT_CSP
	return "application/octet-stream", "attachment", STRICT_CSP


def file_headers(file_url, filename, cache_control):
	content_type, disposition, csp = presentation(file_url)
	return {
		"Content-Type": content_type,
		"Cache-Control": cache_control,
		"X-Content-Type-Options": "nosniff",
		"Content-Security-Policy": csp,
		"Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(filename)}",
	}


def apply_headers(response, file_url, filename, cache_control):
	for name, value in file_headers(file_url, filename, cache_control).items():
		response.headers[name] = value
	return response


def stream(file_url, filename, cache_control):
	key = storage.key_for(file_url)
	obj = storage.open_object(key) if key else None
	if obj is None:
		return None

	body = obj["Body"]
	headers = file_headers(file_url, filename, cache_control)

	etag = (obj.get("ETag") or "").strip('"')
	request = getattr(frappe.local, "request", None)
	if etag and request is not None and request.if_none_match.contains(etag):
		body.close()
		response = Response(status=304, headers=headers)
		response.set_etag(etag)
		return response

	response = Response(body.iter_chunks(CHUNK_SIZE), headers=headers, direct_passthrough=True)
	response.call_on_close(body.close)
	if obj.get("ContentLength") is not None:
		response.content_length = obj["ContentLength"]
	if etag:
		response.set_etag(etag)
	if obj.get("LastModified"):
		response.last_modified = obj["LastModified"]
	return response


class PublicFilePage(BaseRenderer):
	def can_render(self):
		return self.path.startswith("files/") and storage.is_active() and bool(storage.safe_key_for(self.url))

	@property
	def url(self):
		return "/" + self.path

	def render(self):
		filename = self.url.rsplit("/", 1)[-1]
		response = stream(self.url, filename, PUBLIC_CACHE)
		if response is not None:
			return response
		path = storage.local_path(self.url)
		if path and os.path.isfile(path):
			from werkzeug.utils import send_file

			response = send_file(path, environ=frappe.local.request.environ, conditional=True)
			return apply_headers(response, self.url, filename, PUBLIC_CACHE)
		raise frappe.PageDoesNotExistError


def readable_file(file_url, name=None):
	filters = {"file_url": file_url}
	if name:
		filters["name"] = str(name)
	for row in frappe.get_all("File", filters=filters, fields=["name", "file_url"]):
		if row.file_url != file_url:
			continue
		file = frappe.get_doc("File", row.name)
		if file.is_downloadable():
			return file
	return None


def download_private_file(path, original):
	from frappe.core.doctype.access_log.access_log import make_access_log
	from frappe.utils.response import send_private_file
	from werkzeug.exceptions import Forbidden

	denied = Forbidden(frappe._("You don't have permission to access this file"))
	if not storage.is_active():
		return original(path)
	try:
		key = storage.key_for(path)
	except storage.UnsafeFileURL:
		raise denied
	if not key:
		return original(path)

	if frappe.session.user == "Guest":
		raise denied
	file = readable_file(path, frappe.form_dict.fid)
	if not file:
		raise denied

	make_access_log(doctype="File", document=file.name, file_type=os.path.splitext(file.file_url)[-1][1:])
	response = stream(file.file_url, file.file_name, PRIVATE_CACHE)
	if response is None:
		response = send_private_file(file.file_url.split("/private", 1)[1], filename=file.file_name)
		apply_headers(response, file.file_url, file.file_name, PRIVATE_CACHE)
	return response
