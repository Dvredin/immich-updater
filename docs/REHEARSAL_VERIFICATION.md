# Verification and deployment boundaries

## Exercised integration

The automatic transition was exercised on an explicitly synthetic stock four-service
Immich deployment from **v3.1.0 to v3.2.4**, using the real published container images.

- A JPEG was uploaded through the multipart API and an album created through the API.
  Synthetic person/face metadata was seeded on the old schema.
- Migrations preserved recorded asset/album/person/face metadata and relationships.
  Authenticated reads, sampled original-file bytes/hash and album create/delete passed.
  The pre-existing synthetic account password login passed after migration.
- A destructive, isolated recovery drill changed clone metadata and original-file bytes,
  injected a post-migration failure, and restored the complete cold database/files/configuration
  and previous image IDs. Byte comparisons ran while stopped, before old runtime startup.
- A complete subsequent upgrade of the synthetic source passed and persisted the selected version.
- The integration runner's foreground transport expired during committed activation.
  Its durable journal/artifacts were inspected and resumed through real recovery.
  Recovery retried the committed candidate rather than rolling back already-admitted writes.
  Metadata/original reads/version pin and password login passed afterwards.
- Actual Docker mount/network inspection confirmed private copies, no published clone
  ports, internal networking and isolated IPv4/IPv6 gateway modes before application startup.

An independent read-only safety review identified incomplete restore staging, rollback-start
failure handling, baseline races, late isolation inspection, state/mount overlap,
mutable-tag rollback provenance and dropped executable template settings. Focused regressions
cover the corrections. This is not a claim of an independent audit of every subsequent edit.

## Local checks

The original controller suite passed 80 tests. The publication adds separately exercised
installer source-package, readiness, revision-pin and simulated lifecycle regressions.
Unit lifecycle/HTTP doubles are simulated evidence, not a live installation.
Compilation and whitespace checks also run before publication.

## Required checks on each real host

- Docker/CPU/memory and supported local writable-directory layout.
- Storage capacity for retained rehearsals, checkpoints and recovery staging.
- Successful `--prepare-only` rehearsal and clone-only full-state recovery receipt.
- Effective systemd unit/drop-ins, exact installed code, one timer and next run.
- Actual application health/version after the first scheduled production update.

Local synthetic acceptance does not certify another installation, arbitrary future migrations,
external OAuth, every ML model, rare application bugs or host/storage-loss disaster recovery.
Keep separate disaster backups. Private live receipts, resolved configurations, credentials,
database dumps and media are never repository artifacts.
