import os
from urllib.parse import quote

import frappe
from frappe.website.page_renderers.base_renderer import BaseRenderer
from werkzeug.wrappers import Response

from s3storage import storage

PUBLIC_CACHE = "public, max-age=3600, stale-while-revalidate=86400"
PRIVATE_CACHE = "private, max-age=3600, stale-while-revalidate=86400"
CHUNK_SIZE = 64 * 1024


def force_download(filename):
	from frappe.utils.response import FORCE_DOWNLOAD_EXTENSIONS

	return os.path.splitext(filename)[1].lower() in FORCE_DOWNLOAD_EXTENSIONS


def file_headers(filename, cache_control):
	disposition = "attachment" if force_download(filename) else "inline"
	return {
		"Cache-Control": cache_control,
		"X-Content-Type-Options": "nosniff",
		"Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(filename)}",
	}


def stream(file_url, filename, cache_control):
	key = storage.key_for(file_url)
	obj = storage.open_object(key) if key else None
	if obj is None:
		return None

	body = obj["Body"]
	headers = file_headers(filename, cache_control)

	etag = (obj.get("ETag") or "").strip('"')
	request = getattr(frappe.local, "request", None)
	if etag and request is not None and request.if_none_match.contains(etag):
		body.close()
		response = Response(status=304, headers=headers)
		response.set_etag(etag)
		return response

	response = Response(
		body.iter_chunks(CHUNK_SIZE),
		mimetype=storage.content_type_for(filename),
		headers=headers,
		direct_passthrough=True,
	)
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

			response = send_file(
				path,
				environ=frappe.local.request.environ,
				conditional=True,
				as_attachment=force_download(filename),
				download_name=filename,
			)
			response.headers.update(file_headers(filename, PUBLIC_CACHE))
			return response
		raise frappe.PageDoesNotExistError


def download_private_file(path, original):
	from frappe.core.doctype.access_log.access_log import make_access_log
	from frappe.core.doctype.file.utils import find_file_by_url
	from frappe.utils.response import send_private_file
	from werkzeug.exceptions import Forbidden

	if not storage.is_active():
		return original(path)
	try:
		key = storage.key_for(path)
	except storage.UnsafeFileURL:
		raise Forbidden(frappe._("You don't have permission to access this file"))
	if not key:
		return original(path)

	if frappe.session.user == "Guest":
		raise Forbidden(frappe._("You don't have permission to access this file"))
	file = find_file_by_url(path, name=frappe.form_dict.fid)
	if not file:
		raise Forbidden(frappe._("You don't have permission to access this file"))

	make_access_log(doctype="File", document=file.name, file_type=os.path.splitext(path)[-1][1:])
	response = stream(path, file.file_name, PRIVATE_CACHE)
	if response is None:
		return send_private_file(path.split("/private", 1)[1], filename=file.file_name)
	return response
