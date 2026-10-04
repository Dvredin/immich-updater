# Verification and deployment boundaries

Operational maintenance: [architecture and runbook](OPERATIONS.md). Exact host
paths, installed revision and owner receipts live in the private deployment
handoff; test receipts below are not the current production-state ledger.

## Current workflow: single-stack-v1

The active updater no longer invokes parallel rehearsal, restore drills, clone RAM
admission or complete-media checkpoint capacity. It retains release age/security
selection, supported official Compose adaptation, image pinning, operation locking,
full logical DB/config backup before target startup, and post-start service/image/
version/ping checks. Failure after migration is retained for owner intervention;
there is no automatic image downgrade or DB restoration. Media backups are external.

Implementation checks cover read-only installer/activation preflight, incomplete
receipt refusal, missing/stale code manifests, package-command faults and private
logs, failed DB capture, buffered stream-FD transport, failed writer stop, changed
mounts, state overlap, failed startup/check retention and blocked repeated updates.
Controller regression traps forbid calls to old rehearsal/drill/full-copy/RAM gates.
Pure lifecycle mocks are not deployment evidence.

The availability observer's tests cover healthy silence, short failure suppression,
three-failure threshold, durable pending delivery before send, retry cooldown,
restart deduplication, recovery rearming and obsolete pending cancellation. A real
local HTTP server exercises request/response parsing. No application restart exists.

## Local maintenance cleanup (not deployed)

The active transport lives in `compose_runtime.py` and raises `UpdaterError`.
`transaction.py` now contains only private state and candidate/image preparation;
its rehearsal switch and cold-filesystem recovery controller are retired. Those
controllers and their resource/sample policies are preserved in `archive/`, with
explicit qualified imports. No archive compatibility shim is part of the runtime.

All 247 baseline test methods remain; five transport/import-isolation regressions
bring the full suite to 252: 146 active and 106 historical tests under `tests/legacy/`.
The installer payload shrank from 30 to 21 files and contains only active tests.
The synthetic runners and Pillow remain developer-only. Shared byte-sensitive
Node transport was preserved exactly; normal Docker command semantics, release
selection, DB backup/mutation boundaries and Redis volume checks remain unchanged.
Active Python sources are Ruff-formatted and pass its F diagnostics; archive code
is excluded from formatting. The timer's description now matches single-stack updates.

A fresh native Docker Redis fixture exercised the extracted transport and existing
mount capture: image-created volume, stored key, switch to an image without VOLUME,
two recreations preserving volume/key, and changed-volume refusal all passed.
No host ports or owner production data were used; fixture containers were removed.
A clean 21-file payload in a fresh Python 3.12.3 venv passed its 146 active tests
under euid0/umask0077 (one skipped: no host Node on the root PATH), installer self-test,
CLI help and compilation. The same Node transport passed real host Node execution
in the 252-test repository suite. Native Compose parser cases passed in both suites.
Fresh imports denied retired modules, with no archive files present in the package.
This is not a new full Immich migration or a deployed updater. See
[cleanup acceptance](cleanup-acceptance.json) for the scoped evidence.

## Historical hardening after independent review

### Redis image-volume compatibility repair

A subsequent read-only installer preflight identified a separate confirmed defect:
an older Redis image's implicit `/data` volume was rejected as undeclared mount
drift. The repair verifies the actual running image's immutable ID and volume
declaration, then captures the existing Docker volume as an explicit external
mapping. Candidate preparation, old-image checkpoint, pre-stop checks and later
updates retain that identity. Other mount drift and state overlap remain refusals.

The pre-cleanup Redis-repair revision passed 247 tests. Focused regressions cover image provenance,
missing/duplicate/unexpected mounts, explicit-name drift, reserved-key conflicts,
state overlap, native candidate adaptation and update/checkpoint retention.
A real Docker fixture created an implicit volume from a synthetic image based on
Valkey, stored and saved a key, and recreated the container using an image with
no volume declaration. The same volume and persisted key survived that change
and a second recreation; a changed captured volume name was rejected.
No host ports, owner stack, owner data or production configuration were used.
[Redis volume acceptance](redis-volume-acceptance.json) records this narrow live
test; it is not another full Immich migration or certification of the owner's VM.

### Previous hardening evidence

The first production attempt after installation failed with an opaque RehearsalError
following a successful preflight. Its exact root cause remains unproven: no private
owner configuration or raw Docker error was collected. Six source defects were
accepted, reproduced and corrected; [repair decisions](UPDATE_HARDENING.md) separate
compatibility failures, diagnostic loss and safety boundaries from that incident.

The corrected30-file payload passes235 tests under root/Python3.12/umask0077,
normal and simulated1000MiB memory. New native Compose parser regressions cover
literal dollar/braces/quotes/Unicode, inherited version conflicts, named volumes,
absent published ports and loopback-only mapping. Recording checks cover runtime
mount drift, private runtime publication and diagnostics without a private canary.

Fresh synthetic acceptance passed the actual3.1.0→3.2.4 migration with a deliberately
special-character PostgreSQL setting, login, metadata/original preservation,
destructive logical dump restoration, verified actual bind/named mounts, private
published Compose and retained failed-update/retry-blocking state. All4container PID
ancestries shared4GiB/swap0, peak2748407808bytes, OOM0; hostOS/controller outside.
[Sanitized repaired-runtime evidence](single-stack-hardening-acceptance.json) includes
runtime hashes. At this historical checkpoint, that repaired package had not yet
been installed on the owner's VM; later deployment status is recorded separately.
A clean independent re-audit verdict is not claimed; the executor verified repairs.

## Previous single-stack acceptance (historical revision)

Fresh native-disk synthetic single-stack acceptance passed an actual populated
`v3.1.0 → v3.2.4` update, password login, metadata/original preservation, destructive
logical backup restoration, and injected post-migration failure. Backup/journal
were retained and retries with both original and changed state paths were denied.
All four test-container PID ancestries were inside a shared4GiB/swap0 cap, with
peak2716323840bytes and no OOM events/kills. HostOS/controller stayed outside it.
Sanitized evidence and runtime hashes: [single-stack acceptance](single-stack-acceptance.json).
The package passed220 tests in exact29-file root/Python3.12/umask0077 stages,
normal and simulated1000MiB available-memory scenarios. The five reproduced review
findings are regressed: DB/settings/runtime provenance, application-bound interruption
marker, strict source-bound version agreement/no downgrade, nonsymlink atomic unit
writes and package bytes authenticated against the requested commit.
At this historical checkpoint, the owner's VM had not yet installed or activated
this workflow. This is not a statement about the current deployment.

## Historical evidence — retired rehearsal architecture

[Archived verification](archive/rehearsal-verification-v3.md) and
[archived README](archive/rehearsal-readme-v3.md) describe the old clone workflow only.
Their resource gates, sample limits, mandatory rehearsal and full-filesystem restore
admission do not apply to single-stack-v1. Older public receipts remain unmodified.

## Required target-host observations

- Exact source/installed revision and complete private package manifest.
- Effective single daily updater timer/service and expected application path.
- Read-only runtime and DB-only backup capacity receipt before activation.
- Actual current application version/health and the first real update's local receipt.
- Independent observer's live checks and exact-target notification receipt.

No synthetic test certifies every future migration, complete VM RAM behavior,
original-file disaster backup, mobile-client compatibility or all application logic.
