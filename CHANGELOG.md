# Changelog

## [0.2.1](https://github.com/EffizienzAgent/s3storage/compare/v0.2.0...v0.2.1) (2026-10-24)


### Bug Fixes

* harden file serving, private file access and shared objects ([e8df9de](https://github.com/EffizienzAgent/s3storage/commit/e8df9de9856767ceef4cb3702c13440b68308a63))
* object delete and write hooks for every object change ([343604b](https://github.com/EffizienzAgent/s3storage/commit/343604bf081c16e7513ac468e8c3fe7b2740d1ef))
* serve PDFs inline without a sandbox CSP ([27d94c1](https://github.com/EffizienzAgent/s3storage/commit/27d94c1569e5d1c2081ec44c16441f2b74229bed))

## [0.2.0](https://github.com/EffizienzAgent/s3storage/compare/v0.1.0...v0.2.0) (2026-10-24)


### Features

* `s3storage-verify`: read-only check of every File's object (exists, size, content hash; `--sample N`) and report of orphan objects ([#3](https://github.com/EffizienzAgent/s3storage/pull/3)) ([dffdb30](https://github.com/EffizienzAgent/s3storage/commit/dffdb3070264cbdfcf725dd541f1ba5a0f8458c6))
* `s3storage-migrate --to-local`: idempotent, resumable reverse migration from the bucket to local disk ([#3](https://github.com/EffizienzAgent/s3storage/pull/3)) ([dffdb30](https://github.com/EffizienzAgent/s3storage/commit/dffdb3070264cbdfcf725dd541f1ba5a0f8458c6))

## 0.1.0 (2026-10-24)


### Features

* S3 file storage for Frappe File documents ([5ce7a0a](https://github.com/EffizienzAgent/s3storage/commit/5ce7a0aa015edac6e7a70fa0e689e2cd564a8da4))

### Documentation

* professional setup and support section; use oss contact address ([eb03f44](https://github.com/EffizienzAgent/s3storage/commit/eb03f44ecc2042beaa0a7c20d008b2beecfac534))
