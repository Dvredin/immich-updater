# Verification and deployment boundaries

## Current compact-4g-v3 headroom correction

The clone-only hard parent cap is now **1728 MiB** and the clone Node heap ceiling
is **448 MiB**. The separate **256 MiB** host/controller reserve and all recovery,
isolation, kernel-enforcement and OOM gates are retained. Required MemAvailable
is therefore **1984 MiB**, not a reduced threshold over an unchanged 1824 MiB clone.
Production limits/environment/worker topology are unchanged.

The exact staged package passed **187 tests** under Python 3.12/euid 0/umask 0077,
both normally and with simulated 1000 MiB MemAvailable. Boundary regressions include
1984 MiB accepted, one byte less deferred and observed 2053/2058 MiB admitted without
changing the reserve. A separate fixture verifies the smaller Node heap is clone-only.

A fresh native-ext4 synthetic fixture passed populated stock v3.1.0→v3.2.4 migration,
original/album API checks, metadata preservation, destructive clone DB/files/config/
old-image restoration, then a complete synthetic-source upgrade with password login
and version persistence. All four actual service PID ancestries were verified under
an outer **4 GiB**, zero-swap container slice; its peak was **3686952960 bytes** and
OOM/kill counters stayed zero. Clone parent/child caps and counters passed before
fault injection and after restored-old-runtime startup. Temporary kernel peak
charges are preserved in [the sanitized receipt](compact-4g-v3-acceptance.json).

This is a disk-backed container-overlap test, not a complete 4 GiB VM: the controller
and host OS are outside the outer slice. It uses synthetic data, not the owner's DB;
target-host preparation and full production-recovery capacity are still unverified.
The older receipts below remain historical evidence for their stated profiles.

## Sampled-media revision

The reproduction now retains a complete, unfiltered PostgreSQL dump and its
metadata baseline/sample candidates from the same exported snapshot, but copies
only bounded originals: at most 16 files, 128 MiB total, 64 MiB each from 64 candidates.
Stock storage sentinels retain their contents; thumbnails/model-cache trees are
not copied. External-library mounts are private sampled directories, never live
production binds. Descriptor-relative no-follow traversal guards every original
source component, including source-root parents; duplicate paths are skipped.
Captured original hashes remain independent of candidate-mutated bytes.

The exact 25-file staged payload passed 185 tests under Python 3.12/euid 0/umask 0077,
both normally and with simulated 1000 MiB MemAvailable. Negative tests reject
symlink swaps, path escapes, empty populated samples, byte/count overflow and
changed captured-original hashes. Pure controller/activation doubles verify that
sample preparation does not require complete media-copy capacity, but production
admission does; insufficient full rollback capacity is a retryable deferral and
cannot release the lock/enable the scheduler. An isolated `-I -S` activation
regression reproduced the missing system `requests` dependency before correction;
activation now runs the read-only production-storage probe through the installed
venv, keeps its diagnostics private and requires an affirmative byte-budget receipt.

The complete native sample integration passed real stock v3.1.0→v3.2.4 migration,
authenticated original/album API checks, metadata preservation, destructive clone
DB/file/config/old-image recovery, and a subsequent synthetic-source upgrade with
old password login and version persistence. The fixture had one 725-byte original
and a complete 18,373,977-byte custom-format DB dump; upper limits are independently
exercised by the filesystem unit fixtures, not by pretending this tiny JPEG is 128 MiB.
All running clone parent/child OOM counters were zero at acceptance; the configured
1824 MiB parent cap and zero swap remained enforced. Historical temporary kernel
peak overcharges are retained in the [sanitized receipt](sampled-media-acceptance.json).

The test used a disposable ext4 image with 4 GiB RAM backing precharged outside the
clone pool. Earlier plain/sparsely backed RAM-storage trials failed parent OOM and
were rejected, not promoted to successes or used to relax the budget. The synthetic
source waited for initial worker bootstrap before capture. This final result
validates the capped clone/controller path, not the host OS/controller/backing RAM
budget of an entire 4 GiB VM or the actual owner's DB/library. Production rollback
and real-host preparation remain separate acceptance requirements.

The bounded independent read-only review reported a source-parent-symlink race and
activation importing venv-only dependencies in system Python. Deterministic
filesystem/isolated-interpreter regressions reproduced the mechanisms and passed
after descriptor-relative no-follow traversal and installed-venv probing. It did
not review every older controller path or independently re-audit the corrected tree.

## Exercised integration — historical full-copy baseline

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
The final compact source suite passed **158 tests**, including shared-pool, child OOM,
pre-start enforcement, stopped-service, functional-check timing, ENOMEM deferral,
worker readiness, teardown and documented kernel-accounting regressions. Compilation,
installer self-test and whitespace checks also passed before publication.

A later installer correction passed **162 tests**, including real package-command
failure/timeout capture and simulated venv/dependency/test failure checks. The same
suite passed with a simulated host `MemAvailable` of 1000 MiB: pure isolation fixtures
no longer call the real host-memory gate. The production preflight and its explicit
insufficient-memory regressions remain unchanged. Before this correction the scarce-RAM
simulation reproduced one isolation-test error; that is an installer-test defect,
not evidence that a real clone can be admitted with insufficient host RAM.
Failed package steps now retain private diagnostics before raising, rather than
discarding captured subprocess output. This does not establish which package step
failed on any particular remote host and is not a new live migration/restore run.

The subsequent private-state fixture correction passed **163 tests** from the exact
23-file staged package under Python 3.12, effective UID 0 and the installer's inherited
`umask 0077`, both normally and with simulated scarce host memory. The failure was
reproduced first under that mask: `mkdir(mode=0755)` actually created a private `0700`
directory, so expecting the validator to reject it was incorrect. The fixture now
explicitly applies and checks `0755` before asserting rejection; a dedicated restrictive-
umask regression covers it. The production private-state validator, installer umask,
resource limits, controller and admission gates are unchanged. These are package/unit
checks, not a new real-host migration or restoration receipt.

## Historical compact-4g-v2 resource verification

The historical v2 profile used a native clone-only 1824 MiB shared parent RAM pool,
individual Docker ceilings and zero swap at both levels. Kernel PID ancestry must
confirm that each actual child belongs to the inspected bounded pool. Its server
also sets the supported Node heap ceiling without altering the worker topology.
Production limits/environment are retained; compact settings never enter the source candidate.

Resource checks have separate create-time and runtime modes. Every running acceptance
requires all four clone services plus local cgroup-v2 hard limits and OOM counters.
Create-time inspection occurs before the initial rehearsal, restore-drill startup and
clone transaction/recovery transitions. Runtime checks repeat after functional reads
and local key cleanup, before destructive fault injection can replace containers.
The drill retains the candidate's pre-rollback resource receipt separately from the
restored old runtime receipt. Host/OCI allocator ENOMEM is a safe logged deferral;
a clone application OOM is a failed candidate, not permission to weaken its limits.

A real isolated 64 MiB Node container stayed running after its child was OOM-killed.
The child was SIGKILLed. With Docker's OOM flag deliberately ignored in the
inspection object, the kernel `memory.events` branch still rejected acceptance.
The final probe used read-only rootfs, tmpfs-only image data mount, no network,
no production bind mount, no exposed port and no application account.

An independent bounded review of the compact change found stopped-service runtime
acceptance, missing post-functional OOM checks, late drill inspection and incorrect
ENOMEM quarantine. Focused regressions cover each correction. The review did not
independently re-audit the entire pre-existing controller.

The initial static-partition trials failed: a 768 MiB server OOM, insufficient
256 MiB Node worker heap, then PostgreSQL geodata insertion OOM at 320/448 MiB.
The new shared-pool profile does not increase its 1824 MiB total budget or waive
any acceptance check; it allows borrowing unused capacity between clone services.
The final complete fresh synthetic run passed with source and clones constrained
together in a 4 GiB, zero-swap lab slice. Real populated migration, authenticated
reads/writes, original bytes, metadata, old-password login, destructive full recovery
and the subsequent source upgrade/version pin all passed. The observed outer peak was
3,642,793,984 bytes (about 3.393 GiB), with no outer or clone OOM event. Actual source
and clone PID ancestry beneath that outer pool was checked while both were running.
The final clone rehearsal's historical peak was 1,912,713,216 bytes, its configured
pool limit 1,912,602,624 bytes, and current usage at acceptance 1,885,659,136 bytes;
that small historical overcharge is retained, not misreported as exact cap adherence.
The [sanitized machine receipt](compact-4g-acceptance.json) records the exact boundaries.

This is an aggregate container constraint on a larger lab host, not a complete VM:
controller/copy-process memory and the host OS are outside the test slice. It does not
certify another host's real library, free-resource/storage state or all future releases.


Stock HTTP readiness is not full background-worker readiness. In the real trial,
starting ML before geodata import finished caused a parent OOM during API probing.
Clone startup now waits for stock microservices bootstrap completion before ML;
unknown/changed bootstrap evidence is a safe refusal, not an arbitrary sleep or a
startup-setting patch. Relevant upstream v3.2.4 source:
[worker bootstrap](https://github.com/immich-app/immich/blob/v3.2.4/server/src/workers/microservices.ts),
[metadata initialization](https://github.com/immich-app/immich/blob/v3.2.4/server/src/services/metadata.service.ts),
[geodata import](https://github.com/immich-app/immich/blob/v3.2.4/server/src/repositories/map.repository.ts).

A subsequent trial passed startup and functional checks but the controller rejected
a historical parent peak above its configured cap. That comparison was incorrect:
[kernel cgroup-v2 documentation](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html)
states that `memory.max` is the hard-limit mechanism and usage may temporarily go
over it. Historical peaks are retained as measurements; actual configured limits,
current usage and parent/child OOM counters still control acceptance. Dedicated
regressions distinguish temporary historical peaks from current over-budget usage.

The narrow shared-pool read-only review found no verified blockers under the
local cgroup-v2/systemd backend. It used focused pure mocks, not live integration,
and did not independently audit the later worker-readiness/peak-semantics changes
or the entire pre-existing controller. The final live acceptance above independently
exercised the complete changed path after those final corrections.

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
