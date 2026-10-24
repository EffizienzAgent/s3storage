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
6. After some days without problems, delete the local `public/files` and `private/files` folders yourself.
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
