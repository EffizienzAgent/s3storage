# Migrating existing files

You can turn the app on before any file is copied. Every read (downloads, `File.get_content()`, email
images, PDFs) falls back to the local file when the object is not in the bucket yet. New uploads go to the
bucket only.

1. Install the app and set the [config keys](configuration.md).
2. Check: upload a public and a private file, open both, delete one, and check the bucket.
3. Dry run:

   ```sh
   bench --site <site> s3storage-migrate --dry-run
   ```

4. Upload:

   ```sh
   bench --site <site> s3storage-migrate
   ```

5. Run it again. The counts should now show only `already_uploaded` and `in_s3_only`.
6. Check the bucket with [`s3storage-verify`](#verify).
7. After some days without problems, delete the local `public/files` and `private/files` folders yourself.
   The command never deletes local files.

The same function is available as `bench --site <site> execute s3storage.migrate.run --kwargs '{"dry_run": true}'`.

## What it does

The command reads every distinct `file_url` and `thumbnail_url` of all `File` rows. For each one it:

- skips the object if its size and MD5 already match the local file,
- uploads the local file otherwise,
- checks the upload with a HEAD request.

## Output

```json
{
 "active": true,
 "direction": "to_s3",
 "dry_run": false,
 "counts": {"uploaded": 120, "already_uploaded": 3, "missing": 1},
 "problems": ["missing: /private/files/old.pdf"]
}
```

| Count | Meaning |
|---|---|
| `would_upload` | Dry run: would be uploaded |
| `uploaded` | Uploaded and verified |
| `already_uploaded` | Object exists with the same size and MD5 |
| `in_s3_only` | No local file, but the object exists |
| `missing` | Neither a local file nor an object |
| `hash_mismatch` | The local file does not match the `File`'s `content_hash` (it is still uploaded) |
| `verify_failed` | The HEAD request after upload did not match |
| `unsafe_url` | The `file_url` is rejected, see [configuration.md](configuration.md#object-keys) |
| `failed` | Unexpected error, see the Error Log |

`problems` lists up to 100 affected URLs. Frappe stores some private thumbnails at an odd path, so they may
show up as `missing`. That is harmless.

## Verify

```sh
bench --site <site> s3storage-verify
bench --site <site> s3storage-verify --sample 200
```

The command only reads. It never writes to the bucket, the database or the disk. It checks every distinct
`file_url` and `thumbnail_url` of all `File` rows:

- the object exists,
- its size matches the `File`'s `file_size` (when set),
- its MD5 matches the `File`'s `content_hash`. Each object is streamed for this, so a full run downloads
  the whole bucket. `--sample N` hashes only N randomly picked files. `--sample 0` skips hashing.

It also lists the bucket under `public/files/` and `private/files/` and reports objects that no `File` row
refers to. It never deletes these orphans. An upload that is still running can show up as an orphan for a
moment.

```json
{
 "active": true,
 "sample": null,
 "counts": {"hashed": 118, "ok": 120, "local_only": 2, "missing": 1, "orphan": 3},
 "problems": ["missing: /private/files/old.pdf"],
 "orphans": {"count": 3, "bytes": 52811, "keys": ["public/files/tmp.png"]}
}
```

| Count | Meaning |
|---|---|
| `ok` | Object exists and matches every check that ran |
| `hashed` | Object was streamed and hashed |
| `local_only` | No object yet, but a local file exists (not migrated yet) |
| `missing` | Neither an object nor a local file |
| `size_mismatch` | Object size differs from `file_size` |
| `hash_mismatch` | Object MD5 differs from `content_hash` |
| `unsafe_url` | The `file_url` is rejected, see [configuration.md](configuration.md#object-keys) |
| `failed` | Unexpected error, see the Error Log |
| `orphan` | Object without a `File` row |

`problems` and `orphans.keys` list up to 100 entries each. The command exits with status 1 when `problems` is
not empty, so you can run it from cron or a monitoring job.

## Moving back to local disk

`s3storage-migrate --to-local` is the reverse migration. It downloads every object to the path where Frappe
keeps the file on local disk: `/files/<name>` to `sites/<site>/public/files/<name>` and `/private/files/<name>`
to `sites/<site>/private/files/<name>`.

The `File` rows need no change. `file_url` and `thumbnail_url` are the same in both layouts, so the rows
already look exactly as they would on a site that never used S3.

1. Dry run:

   ```sh
   bench --site <site> s3storage-migrate --to-local --dry-run
   ```

2. Download:

   ```sh
   bench --site <site> s3storage-migrate --to-local
   ```

3. Stop new uploads: `bench --site <site> set-maintenance-mode on` and `bench --site <site> disable-scheduler`.
   While the app is active, new files still go to the bucket only.
4. Run step 2 again. It only downloads what is new. The counts should now show only `already_local` and
   `local_only`.
5. Remove the `s3_files_*` keys from the site config. Frappe now reads and writes local disk.
6. `bench --site <site> set-maintenance-mode off` and `bench --site <site> enable-scheduler`.
7. Keep the bucket until you are sure. The command never deletes objects.

The command is idempotent and can be resumed. A file is first written to `<name>.s3storage-part` and only
renamed to its final name after its size and MD5 match the object, so an interrupted run leaves no half
file behind. Files that already exist with the same size and MD5 are skipped.

| Count | Meaning |
|---|---|
| `would_download` | Dry run: would be downloaded |
| `downloaded` | Downloaded and verified |
| `already_local` | The local file has the same size and MD5 as the object |
| `local_only` | No object, but a local file exists |
| `missing` | Neither an object nor a local file |
| `local_differs` | A local file exists with other content. It is kept, not overwritten |
| `hash_mismatch` | The object does not match the `File`'s `content_hash` (it is still downloaded) |
| `verify_failed` | The download did not match the object's size and MD5 and was discarded |
| `unsafe_url` | The `file_url` is rejected |
| `failed` | Unexpected error, see the Error Log |

For `local_differs`, compare both versions. While the app is active, users get the object, not the local
file. Delete or move the local file and run the command again to download the object.
