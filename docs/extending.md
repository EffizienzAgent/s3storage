# Extending s3storage

## S3 client factory

Other apps can reuse the connection settings and client cache:

```python
from s3storage.client import NotConfigured, get_client, target_config

# The file storage bucket
client, config = get_client()

# A named target: reads s3_archive_endpoint, s3_archive_region, s3_archive_bucket
# and s3_archive_access_key / s3_archive_secret_key (or the files credentials)
client, config = get_client("archive")
client.put_object(Bucket=config.bucket, Key="a.pdf", Body=b"...")

# An explicit config, for example from a DocType
client, config = get_client(
	config={
		"endpoint": "https://s3.example.com",
		"region": "eu-central-1",
		"bucket": "other-bucket",
		"access_key": "...",
		"secret_key": "...",
	}
)
```

| Function | Returns |
|---|---|
| `get_client(target="files", config=None)` | `(boto3 S3 client, config)`. Raises `NotConfigured` if a field is missing. |
| `target_config(target="files")` | The config as a `frappe._dict` with `endpoint`, `region`, `bucket`, `access_key`, `secret_key`, or `None` if incomplete. |
| `config_keys(target="files")` | The site config key for each field. |

Clients are cached per config, so calling `get_client()` per request is cheap. See
[configuration.md](configuration.md#named-targets) for the key names.

The client only sends checksums when an operation requires them. For uploads with Object Lock retention,
send `ContentMD5` yourself:

```python
import base64, hashlib

client.put_object(
	Bucket=config.bucket,
	Key=key,
	Body=content,
	ContentMD5=base64.b64encode(hashlib.md5(content).digest()).decode(),
	ObjectLockMode="COMPLIANCE",
	ObjectLockRetainUntilDate=retain_until,
)
```

## Refusing file deletes

Frappe's `doc_events` `on_trash` runs after the `File` controller has already deleted the bytes. To protect a
file, register a function under `s3storage_before_file_delete` in your app's `hooks.py`:

```python
s3storage_before_file_delete = ["myapp.guards.protect_file"]
```

```python
import frappe
from frappe import _


def protect_file(file):
	if is_archived(file):
		frappe.throw(_("Archived files cannot be deleted"))
```

Each function gets the `File` document. They run at the start of `File.on_trash`, before any bytes are
deleted from the bucket or the local disk, also while the S3 storage is not configured. Raising an exception
cancels the delete: the `File` row and its bytes stay.

The hook also runs before an object is deleted or overwritten through any other path, for every `File` whose
`file_url` or `thumbnail_url` is that object, so a host app can protect an object no matter which row
triggers the change. When a file is made private or public, it runs for the file itself and every `File` that
shares its URL or `content_hash`. An object that other rows still use is never deleted: a private/public
change then copies it.

## Object hooks

Two hooks work on objects instead of rows:

```python
s3storage_before_object_delete = ["myapp.guards.before_object_delete"]
s3storage_before_object_write = ["myapp.guards.before_object_write"]
```

```python
def before_object_delete(file_url=None, *, key=None):
	if is_archived_url(file_url):
		frappe.throw(_("Archived files cannot be deleted"))


def before_object_write(file_url=None, *, key=None):
	if is_archived_url(file_url):
		frappe.throw(_("Archived files cannot be changed"))
```

Each function is called as `fn(file_url=..., key=...)`, with the `file_url` (for example
`/private/files/x.pdf`) and the object key (`private/files/x.pdf`). Raising an exception refuses the action.
They run with S3 active and also while files are on local disk:

| Hook | Runs before |
|---|---|
| `s3storage_before_object_delete` | `delete_file_data_content` (deleting a `File` or its thumbnail), removing the old object when a file is made private or public |
| `s3storage_before_object_write` | overwriting a file's own object (`save_file(overwrite=True)`, `optimize_file`), writing a thumbnail, writing the new object when a file is made private or public |
