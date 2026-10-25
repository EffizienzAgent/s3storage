# Configuration

All settings live in the site config (`sites/<site>/site_config.json`), not in a DocType. Secrets stay out of
the database and can be written by your deployment, for example from a Kubernetes Secret.

## File storage

| Key | Example | Meaning |
|---|---|---|
| `s3_files_endpoint` | `https://s3.example.com` | S3 API endpoint |
| `s3_files_region` | `eu-central-1` | Region name sent with each request |
| `s3_files_bucket` | `my-frappe-files` | Bucket for the site's files |
| `s3_files_access_key` | | Access key |
| `s3_files_secret_key` | | Secret key |

The app is active only when **all five** keys are set. If one is missing, Frappe keeps using local disk, so
the order of deploying the app and setting the keys does not matter.

The client uses path-style addressing, three retries, a 5 s connect timeout and a 30 s read timeout.
Checksums are only sent when an operation requires them, which keeps it compatible with Ceph RGW.

The bucket should be private. The app never needs public-read access.

## Large files

| Key | Default | Meaning |
|---|---|---|
| `s3storage_part_size` | `8388608` (8 MiB) | Multipart threshold and part size for uploads and copies, 5 MiB to 512 MiB |
| `s3storage_max_concurrency` | `4` | Parts transferred in parallel per upload or copy, 1 to 16 |
| `s3storage_chunk_size` | `1048576` (1 MiB) | Chunk size for downloads, hashing and spooling, 4 KiB to 64 MiB |

Values outside the range are clamped to it. Zero, negative or invalid values use the default.

Uploads at or above the part size use multipart upload and copies use multipart copy, so there is no 5 GB
limit. S3 allows 10,000 parts, so the part size grows automatically for files above about 80 GB. A failed
multipart upload is aborted, so no incomplete parts are left behind.

A worker that is killed during an upload cannot abort it. Its parts stay in the bucket, invisible but
billed, until an `AbortIncompleteMultipartUpload` lifecycle rule removes them, so set one up (see
[bucket-lifecycle.md](bucket-lifecycle.md)). A worker killed after the upload but before the commit leaves a
complete object without a `File` row, which [`s3storage-verify`](migration.md#verify) reports as an orphan.

Memory per transfer stays bounded: an upload from a path reads the file from disk part by part, an upload from
a file object holds at most `s3storage_max_concurrency` parts in memory, and a download holds one chunk.
Downloads support `Range` requests, so browsers, players and download managers can seek and resume.

Every object stores the MD5 of its content as `md5` metadata, also for multipart uploads, whose ETag is not an
MD5. Copies, for example when a file is made private or public, keep it and the `ContentType`. Verify and
migration use it to compare objects with local files.

Some paths still hold a whole file in memory because Frappe core needs its content as bytes:

- Browser uploads (`upload_file`) and `File` rows saved with `content`, bounded by Frappe's `max_file_size`
- `File.get_content()`, `optimize_file`, thumbnails, `unzip` (each member of the archive), email attachments
  and inline images, Typst PDFs

To store large generated files, use `insert_file` (see [extending.md](extending.md#large-files)).

The client is boto3. Use `urllib3>=2.5.0`, which fixes two redirect-handling security issues (CVE-2025-50181, CVE-2025-50182).

### Example: Hetzner Object Storage

```json
{
 "s3_files_endpoint": "https://fsn1.your-objectstorage.com",
 "s3_files_region": "fsn1",
 "s3_files_bucket": "my-frappe-files",
 "s3_files_access_key": "...",
 "s3_files_secret_key": "..."
}
```

### Example: MinIO

```json
{
 "s3_files_endpoint": "http://minio:9000",
 "s3_files_region": "us-east-1",
 "s3_files_bucket": "frappe-files",
 "s3_files_access_key": "...",
 "s3_files_secret_key": "..."
}
```

## Named targets

Other apps can use more buckets through named targets (see [extending.md](extending.md)). A target named
`<name>` reads the keys `s3_<name>_endpoint`, `s3_<name>_region`, `s3_<name>_bucket`, `s3_<name>_access_key`
and `s3_<name>_secret_key`. The file storage itself is the target `files`.

If a target sets neither `s3_<name>_access_key` nor `s3_<name>_secret_key`, it uses the credentials of the
`files` target. Target names may contain lowercase letters, digits and underscores.

## Object keys

| `file_url` | Object key |
|---|---|
| `/files/report.pdf` | `public/files/report.pdf` |
| `/private/files/contract.pdf` | `private/files/contract.pdf` |

The key is the stored `file_url` path as is. It is never URL-decoded. A `file_url` that contains `%`, `?`,
`#`, a backslash or a control character is rejected (`UnsafeFileURL`): uploads fail, downloads return 403 and
the migration reports it as `unsafe_url`.

## How files are served

The response type is decided by the stored `file_url`, never by `file_name` or the object's metadata.
Every response sends `X-Content-Type-Options: nosniff` and a `Content-Security-Policy`.

| File type | Response |
|---|---|
| PNG, JPEG, GIF, WebP, plain text, audio, video | Inline, with its own type and `sandbox; default-src 'none'` |
| PDF | Inline, without a CSP: browsers do not render PDFs in a sandboxed document. Frappe rejects PDF uploads that contain JavaScript. |
| `.html`, `.htm` (while `s3storage_html_inline` is on) | Inline as `text/html` with `sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads` |
| Everything else, including SVG and XML types | `attachment` as `application/octet-stream` with `sandbox; default-src 'none'` |

The HTML sandbox never includes `allow-same-origin`. A page runs in an opaque origin: scripts work, but it
cannot read the site's cookies, local storage or API.

An HTML file still opens as a full page on the site's domain, so an uploaded page could be used for phishing
(for example a fake login form). If users you do not trust can upload files, set `s3storage_html_inline` to `0`.

| Key | Default | Meaning |
|---|---|---|
| `s3storage_html_inline` | `1` | Serve HTML files inline in the script sandbox. Set `0` to download them instead. |

Private files are only served for a `File` row whose `file_url` matches the request path exactly (the database
match is case-insensitive, the app's check is not) and that the user may read.

## File rows

A `File`'s `file_url`, `thumbnail_url` and `content_hash` cannot be changed through a normal save. A new row
that points at an existing private file is only allowed if the user can read a `File` with exactly that URL.

## Web server

With the standard Frappe nginx config, nginx serves files that still exist on local disk and passes a miss
on to Frappe, which then streams the object from the bucket. No nginx change is needed. Files nginx serves
from local disk do not get the headers above, so run the migration and remove the local copies.
