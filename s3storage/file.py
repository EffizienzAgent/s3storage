import io
import os
import tempfile

import frappe
from frappe import _
from frappe.core.doctype.file.file import FILE_ENCODING_OPTIONS, OLE_FILE_SIGNATURE, File
from frappe.core.doctype.file.utils import (
	delete_file,
	get_content_hash,
	get_extension,
	update_existing_file_docs,
)
from frappe.utils import cint

from s3storage import storage


def decode(content, encodings=None):
	if content.startswith(OLE_FILE_SIGNATURE):
		return content
	for encoding in encodings or FILE_ENCODING_OPTIONS:
		try:
			return content.decode(encoding)
		except UnicodeDecodeError:
			continue
	return content


class S3File(File):
	def on_trash(self):
		for method in frappe.get_hooks("s3storage_before_file_delete"):
			frappe.get_attr(method)(self)
		super().on_trash()

	def in_s3(self):
		return (
			storage.is_active()
			and not self.is_folder
			and not self.is_remote_file
			and bool(storage.key_for(self.file_url))
		)

	def read_stored(self):
		content = storage.read_file_url(self.file_url)
		if content is None:
			frappe.throw(_("File {0} does not exist").format(self.file_url), IOError)
		return content

	def get_content(self, encodings=None):
		if self.get("content") or not self.in_s3():
			return super().get_content(encodings)
		self.validate_file_path()
		self.validate_file_url()
		content = storage.get(storage.key_for(self.file_url))
		if content is None:
			return super().get_content(encodings)
		self._content = decode(content, encodings)
		return self._content

	def exists_on_disk(self):
		try:
			stored = self.in_s3()
		except storage.UnsafeFileURL:
			return False
		if not stored:
			return super().exists_on_disk()
		return storage.file_exists(self.file_url)

	def validate_file_on_disk(self):
		if not self.in_s3():
			return super().validate_file_on_disk()
		if not self.exists_on_disk():
			frappe.throw(_("File {0} does not exist").format(self.file_url), IOError)

	def generate_content_hash(self):
		if self.content_hash or not self.in_s3():
			return super().generate_content_hash()
		self.content_hash = get_content_hash(self.read_stored())

	def make_thumbnail(self, set_as_thumbnail=True, width=300, height=300, suffix="small", crop=False):
		if not self.in_s3():
			return super().make_thumbnail(set_as_thumbnail, width, height, suffix, crop)
		from PIL import Image, ImageOps

		try:
			content = self.read_stored()
			image = Image.open(io.BytesIO(content))
		except OSError:
			return None

		filename, _dot, extn = self.file_url.rpartition(".")
		if not filename:
			filename, extn = self.file_url, None
		extn = get_extension(filename, extn, content)
		image_format = extn and Image.registered_extensions().get(f".{extn.lower()}")
		if not image_format:
			return None

		size = width, height
		if crop:
			image = ImageOps.fit(image, size, Image.Resampling.LANCZOS)
		else:
			image.thumbnail(size, Image.Resampling.LANCZOS)

		thumbnail_url = f"{filename}_{suffix}.{extn}"
		buffer = io.BytesIO()
		try:
			image.save(buffer, format=image_format)
		except OSError:
			frappe.msgprint(_("Unable to write file format for {0}").format(thumbnail_url))
			return None

		storage.put(storage.key_for(thumbnail_url), buffer.getvalue())
		if set_as_thumbnail:
			self.db_set("thumbnail_url", thumbnail_url)
		return thumbnail_url

	def handle_is_private_changed(self):
		if not storage.is_active() or self.is_remote_file:
			return super().handle_is_private_changed()

		old_file_url = self.file_url
		file_name = old_file_url.split("/")[-1]
		new_file_url = f"/private/files/{file_name}" if cint(self.is_private) else f"/files/{file_name}"
		if new_file_url == old_file_url:
			return

		content = storage.read_file_url(old_file_url)
		if content is None:
			frappe.throw(_("Cannot find file {} on disk").format(old_file_url), exc=FileNotFoundError)
		if storage.file_exists(new_file_url):
			frappe.throw(
				_("A file with same name {} already exists").format(new_file_url), exc=FileExistsError
			)

		storage.put(storage.key_for(new_file_url), content)
		storage.delete(storage.key_for(old_file_url))
		delete_file(old_file_url)
		self.flags.s3_moved = {"old": old_file_url, "new": new_file_url}
		frappe.db.after_rollback.add(self.on_rollback)

		self.file_url = new_file_url
		update_existing_file_docs(self)

		if (
			not self.attached_to_doctype
			or not self.attached_to_name
			or not self.fetch_attached_to_field(old_file_url)
		):
			return

		if frappe.get_meta(self.attached_to_doctype).issingle:
			frappe.db.set_single_value(self.attached_to_doctype, self.attached_to_field, self.file_url)
		else:
			frappe.db.set_value(
				self.attached_to_doctype, self.attached_to_name, self.attached_to_field, self.file_url
			)

	def on_rollback(self):
		if not storage.is_active():
			return super().on_rollback()

		if self.flags.pop("new_file", None):
			self.flags.pop("original_content", None)
			self._delete_file_on_disk()
			return

		target = self.file_url
		moved = self.flags.pop("s3_moved", None)
		if moved:
			storage.copy(storage.key_for(moved["new"]), storage.key_for(moved["old"]))
			storage.delete(storage.key_for(moved["new"]))
			target = moved["old"]

		original = self.flags.pop("original_content", None)
		if original and storage.key_for(target):
			storage.put(storage.key_for(target), original)

	def unzip(self):
		if not self.in_s3() or os.path.exists(self.get_full_path()):
			return super().unzip()
		self.check_permission("read")
		with tempfile.NamedTemporaryFile(suffix=".zip") as archive:
			archive.write(self.read_stored())
			archive.flush()
			self.get_full_path = lambda: archive.name
			try:
				return super().unzip()
			finally:
				del self.get_full_path
