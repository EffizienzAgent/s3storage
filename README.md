S3-compatible file storage for the Frappe Framework

# s3storage

`s3storage` stores the bytes of Frappe `File` documents in an S3-compatible bucket instead of the site's
`public/files` and `private/files` folders. It works with any S3 API: AWS S3, MinIO, Ceph RGW,
Cloudflare R2 and others that support path-style addressing.

- **URLs stay the same.** `file_url` keeps `/files/<name>` and `/private/files/<name>`. The object key
  mirrors it: `public/files/<name>` and `private/files/<name>`. Links in notes and mails keep working.
- **Private files keep Frappe's permissions.** `/private/files/...` still goes through Frappe's permission
  check and access log, then streams from the bucket. Browsers never get a presigned URL, so the bucket
  stays private.
- **Public files** are streamed from the bucket with ETag and cache headers.
- **Safe keys.** A `file_url` with `%`, `?`, `#`, a backslash or a control character is rejected instead of
  mapped, so a double-encoded URL can never reach another file's object.
- **Dedupe.** Files with the same content share one object, which is deleted with the last `File` row.
  Name collisions get a content-hash suffix.
- **Gradual migration.** Every read falls back to the local file, so you can turn the app on first and
  upload the existing files later with `bench --site <site> s3storage-migrate`.
- **Verify and go back.** `s3storage-verify` checks every object's existence, size and hash and lists
  orphans, read-only. `s3storage-migrate --to-local` moves the files back to local disk.
  See [docs/migration.md](docs/migration.md).
- **Off by default.** Until all config keys are set, Frappe keeps using local disk.

Also covered: thumbnails, `optimize_file`, making a file private or public, unzip, rollback, inline email
images and Typst PDFs.

## Installation

```bash
bench get-app https://github.com/EffizienzAgent/s3storage --branch version-16
bench --site <site> install-app s3storage
```

**Compatibility:** Frappe v16. The `version-16` branch always points to the latest release; tagged releases
and the changelog are on the [releases page](https://github.com/EffizienzAgent/s3storage/releases).

`boto3` is installed as a Python dependency.

## Configure

Set five keys in the site config:

```sh
bench --site <site> set-config s3_files_endpoint https://s3.example.com
bench --site <site> set-config s3_files_region eu-central-1
bench --site <site> set-config s3_files_bucket my-frappe-files
bench --site <site> set-config s3_files_access_key <access key>
bench --site <site> set-config s3_files_secret_key <secret key>
```

See [docs/configuration.md](docs/configuration.md) for details,
[docs/migration.md](docs/migration.md) to move existing files and
[docs/bucket-lifecycle.md](docs/bucket-lifecycle.md) for versioning and lifecycle rules.

## For app developers

Other apps can reuse the S3 connection and protect files from deletion. See
[docs/extending.md](docs/extending.md):

- `s3storage.client.get_client(target)` returns a boto3 client and bucket config for a named target.
- The hook `s3storage_before_file_delete` runs before a `File`'s bytes are deleted and may refuse.

## Limits

- `bench backup --with-files` only includes local files. Use bucket versioning instead.
- The Chrome PDF generator reads only local files.
- Files are streamed through the Frappe workers. That suits office documents and images, not large media.

## Professional setup & support

Need help setting this up or integrating it into your Frappe stack? Write to oss@effizienzagent.de (English or German). German-speaking SMEs: see [mittelstandshub.de](https://mittelstandshub.de).

## License

MIT, see [license.txt](license.txt).
