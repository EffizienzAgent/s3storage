# Bucket versioning and lifecycle

`bench backup --with-files` only includes local files. Once the files are in a bucket, protect them with
bucket versioning: a deleted or overwritten object stays restorable as a noncurrent version.

A lifecycle rule keeps the bucket from growing forever. The example keeps noncurrent versions for 30 days,
removes delete markers that no longer hide anything and cleans up abandoned multipart uploads.

## Example with the AWS CLI

```sh
ENDPOINT=https://s3.example.com
REGION=eu-central-1
BUCKET=my-frappe-files
aws=(aws --endpoint-url "$ENDPOINT" --region "$REGION")

"${aws[@]}" s3api put-bucket-versioning --bucket "$BUCKET" \
  --versioning-configuration Status=Enabled

"${aws[@]}" s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" \
  --lifecycle-configuration '{
    "Rules": [{
      "ID": "retention",
      "Status": "Enabled",
      "Filter": {},
      "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
      "Expiration": {"ExpiredObjectDeleteMarker": true},
      "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7}
    }]
  }'

"${aws[@]}" s3api get-bucket-lifecycle-configuration --bucket "$BUCKET"
```

For Hetzner Object Storage, use `ENDPOINT=https://fsn1.your-objectstorage.com` and `REGION=fsn1` (or your
location). Ceph RGW accepts the rules but returns them in a different format, so tools that wait for the
rules to come back unchanged (such as some Terraform/Pulumi providers) may time out. The AWS CLI works.

## Restoring a file

```sh
"${aws[@]}" s3api list-object-versions --bucket "$BUCKET" --prefix private/files/contract.pdf
"${aws[@]}" s3api copy-object --bucket "$BUCKET" --key private/files/contract.pdf \
  --copy-source "$BUCKET/private/files/contract.pdf?versionId=<version id>"
```

If the `File` row was deleted too, restore it from the database backup.

## Access

Give the app's key read, write and delete access to the bucket only. It does not need to change versioning,
lifecycle or bucket policies.
