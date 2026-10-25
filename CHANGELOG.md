# Changelog

## [0.3.1](https://github.com/EffizienzAgent/s3storage/compare/v0.3.0...v0.3.1) (2026-10-25)


### Bug Fixes

* harden large-file uploads after the 0.3.0 review ([#13](https://github.com/EffizienzAgent/s3storage/pull/13)) ([ba8fcff](https://github.com/EffizienzAgent/s3storage/commit/ba8fcff3c9cabd4eae908f936356a517b5727798)), closes [#11](https://github.com/EffizienzAgent/s3storage/issues/11)

## [0.3.0](https://github.com/EffizienzAgent/s3storage/compare/v0.2.1...v0.3.0) (2026-10-25)


### Features

* support files of any size ([336a38c](https://github.com/EffizienzAgent/s3storage/commit/336a38cdceb5a257dd3d3edaef0b719aaef51bad))
* support files of any size ([6c9aa57](https://github.com/EffizienzAgent/s3storage/commit/6c9aa57296b1f92b739d72203c697a9647333d81)), closes [#8](https://github.com/EffizienzAgent/s3storage/issues/8)

## [0.2.1](https://github.com/EffizienzAgent/s3storage/compare/v0.2.0...v0.2.1) (2026-10-24)


### Bug Fixes

* **serve:** the response type comes from `file_url` only; every response sends `nosniff`, inline types get a strict sandbox CSP (PDFs inline without CSP so browsers render them), everything else downloads as an attachment ([#6](https://github.com/EffizienzAgent/s3storage/pull/6)) ([e8df9de](https://github.com/EffizienzAgent/s3storage/commit/e8df9de9856767ceef4cb3702c13440b68308a63), [27d94c1](https://github.com/EffizienzAgent/s3storage/commit/27d94c1569e5d1c2081ec44c16441f2b74229bed))
* **private files:** downloads require a `File` whose `file_url` equals the request path; saves cannot change `file_url`, `thumbnail_url` or `content_hash` ([#6](https://github.com/EffizienzAgent/s3storage/pull/6)) ([e8df9de](https://github.com/EffizienzAgent/s3storage/commit/e8df9de9856767ceef4cb3702c13440b68308a63))
* **shared objects:** making a file private or public copies a shared object instead of deleting it; the new hooks `s3storage_before_object_delete` and `s3storage_before_object_write` run before every object delete or overwrite ([#6](https://github.com/EffizienzAgent/s3storage/pull/6)) ([e8df9de](https://github.com/EffizienzAgent/s3storage/commit/e8df9de9856767ceef4cb3702c13440b68308a63), [343604b](https://github.com/EffizienzAgent/s3storage/commit/343604bf081c16e7513ac468e8c3fe7b2740d1ef))
* **migrate:** `--to-local` writes a unique temp file per download ([#6](https://github.com/EffizienzAgent/s3storage/pull/6)) ([e8df9de](https://github.com/EffizienzAgent/s3storage/commit/e8df9de9856767ceef4cb3702c13440b68308a63))

## [0.2.0](https://github.com/EffizienzAgent/s3storage/compare/v0.1.0...v0.2.0) (2026-10-24)


### Features

* `s3storage-verify`: read-only check of every File's object (exists, size, content hash; `--sample N`) and report of orphan objects ([#3](https://github.com/EffizienzAgent/s3storage/pull/3)) ([dffdb30](https://github.com/EffizienzAgent/s3storage/commit/dffdb3070264cbdfcf725dd541f1ba5a0f8458c6))
* `s3storage-migrate --to-local`: idempotent, resumable reverse migration from the bucket to local disk ([#3](https://github.com/EffizienzAgent/s3storage/pull/3)) ([dffdb30](https://github.com/EffizienzAgent/s3storage/commit/dffdb3070264cbdfcf725dd541f1ba5a0f8458c6))

## 0.1.0 (2026-10-24)


### Features

* S3 file storage for Frappe File documents ([5ce7a0a](https://github.com/EffizienzAgent/s3storage/commit/5ce7a0aa015edac6e7a70fa0e689e2cd564a8da4))

### Documentation

* professional setup and support section; use oss contact address ([eb03f44](https://github.com/EffizienzAgent/s3storage/commit/eb03f44ecc2042beaa0a7c20d008b2beecfac534))
