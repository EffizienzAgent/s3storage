app_name = "s3storage"
app_title = "S3 Storage"
app_publisher = "EffizienzAgent"
app_description = "S3-compatible file storage for the Frappe Framework"
app_email = "info@effizienzagent.de"
app_license = "mit"

before_request = ["s3storage.overrides.apply"]
before_job = ["s3storage.overrides.apply"]
page_renderer = ["s3storage.serve.PublicFilePage"]
write_file = "s3storage.backend.write_file"
delete_file_data_content = "s3storage.backend.delete_file_data_content"
override_doctype_class = {"File": "s3storage.file.S3File"}
