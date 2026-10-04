# Verification and deployment boundaries

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
The actual owner's VM has not installed or activated this workflow yet.

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
