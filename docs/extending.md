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

## Large files

Generated files of any size, such as export archives, can be stored without reading them into memory:

```python
from s3storage.backend import insert_file

file = insert_file(
	"export.zip",
	"/tmp/export.zip",  # a path or a binary file object
	is_private=1,
	attached_to_doctype="Data Export",
	attached_to_name=name,
	ignore_permissions=True,
)
```

| Function | Does |
|---|---|
| `insert_file(file_name, source, is_private=1, ignore_permissions=False, **fields)` | Uploads `source` in parts (or writes it to the local files folder while S3 is inactive) under a new name with a random suffix, and inserts a `File` row with `file_size` and `content_hash`. The row's `file_name` stays `file_name`. Returns the `File`. The object is removed again if the insert fails or the transaction is rolled back, unless another `File` uses it or an `s3storage_before_object_delete` hook refuses. |
| `storage.put_file(key, source, content_type=None)` | Uploads a path or binary file object to an object key in parts. Runs `s3storage_before_object_write` for keys of files. Returns `(size, md5)`. |
| `register_file(file_url, file_size, content_hash, ignore_permissions=False, **fields)` | Inserts a `File` row for an object that is already stored, without reading it. Returns the `File`. |

`insert_file` and `register_file` are meant for server-side code: they skip Frappe's `max_file_size` and the
PDF content check, which only apply to uploaded content. Pass a path where you can: a path is read twice from
disk (once for the MD5, once for the upload), a seekable file object is read twice from its position, and a
non-seekable stream is first spooled to a temporary file.

The random suffix keeps concurrent calls with the same `file_name` from writing to the same object.

`register_file` and `storage.put_file` trust their caller. Never pass user input as `file_url` or `key`:

- `register_file` skips the check that a new row for an existing private file needs (see
  [configuration.md](configuration.md#file-rows)), so a `file_url` from user input could claim an orphaned
  private object.
- `storage.put_file` has no permission check and writes any key, including the objects of other files. Only
  the `s3storage_before_object_write` hooks run.

Build the URL or key in your own code, from a name you picked or got from `insert_file`.

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
| `s3storage_before_object_write` | overwriting a file's own object (`save_file(overwrite=True)`, `optimize_file`), writing a thumbnail, writing the new object when a file is made private or public, `insert_file` and `storage.put_file` |
