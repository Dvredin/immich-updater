# Immich updater: architecture and operations

The active implementation is `single-stack-v1`. This is the internal maintenance
runbook, not a claim that any particular future upgrade has succeeded. Deployment
receipts and the exact installed revision belong in the private
`DEPLOYMENT_HANDOFF.md`; public test evidence belongs in
[VERIFICATION.md](VERIFICATION.md).

## Components and boundaries

| Component | Responsibility |
|---|---|
| `immich_updater.py` | Read the installed version, select the highest eligible stable release, enforce release-age and official security gates, and coordinate one update. |
| `risk_checks.py` | Retrieve and interpret official release/security evidence. Serious unresolved applicability and failed retrieval block updating. |
| `compose_runtime.py` | Docker/Compose transport, literal-setting serialization and `UpdaterError` diagnostics; no clone or recovery dependencies. |
| `transaction.py` | Private state/config writes, official Compose adaptation, verified site/volume retention and immutable image pinning before downtime. |
| `archive/` | Explicitly retired rehearsal, resource/sample policy and full-state recovery. Never imported or installed by the active updater. |
| `simple_update.py` | Validate the running stack and data identities, capture a verified logical PostgreSQL backup and configuration, update the same Compose project, and retain failure boundaries. |
| `tools/install.py` | Authenticate the exact source/package, stage dependencies and tests, validate the real host read-only, and separately activate the timer. |
| `immich-updater.timer` / `.service` | Run once daily at 09:35 in the server timezone. `Persistent=true` can catch up after downtime; `Restart=no` prevents a service restart loop. |
| `availability_monitor.py` | Independently observe the public ping endpoint and send an actionable sustained-outage notice. It neither updates nor repairs the application. |

There is one production application stack. No parallel Immich, sample-library
rehearsal, whole-photo-library copy, production RAM reduction or automatic downgrade
is part of the active path. The current timer description reflects single-stack
updates. The installer retains the historical `50-rehearsal-state.conf` drop-in
filename to overwrite that existing file safely during upgrades; its content
contains the active command, not rehearsal. Inspect effective `ExecStart` rather
than inferring behavior from this compatibility filename.

## Release policy

- Patches wait three full days from their own stable release publication.
- Minor/major bases wait seven full days. Entering a new branch through a patch
  also requires the feature/major bases to have matured.
- The highest eligible stable semantic version is selected on each run. It is not
  a permanent target pin; drafts, prereleases, older and equal versions are excluded.
- Official applicable high/critical advisories, unresolved serious applicability,
  or inability to retrieve required evidence block the operation.
- Only a confirmed serious installed vulnerability and its corresponding shipped
  fix can bypass the age delay. Other gates still apply.
- Issues, release-text keywords and per-release manual approval lists are not gates.

## Installation is not an application upgrade

1. Check out the exact reviewed published commit.
2. Run `tools/install.py --app-dir <existing-app> --expected-revision <commit>` as
   root. It disables the old timer, stages a private package, runs tests and performs
   read-only runtime/DB-backup-space validation. It does not stop Immich or pull a
   target application's images.
3. Require `PREPARED_TIMER_DISABLED`. `STOP` is not readiness; preserve staging and
   its private diagnostics. Do not enable the old timer to bypass a refusal.
4. Invoke the installed `/opt/immich-updater/tools/install.py` with the same
   application path and commit plus `--activate`. It rechecks the installed private
   manifest and live preflight, then enables the existing daily timer.
5. `TIMER_ENABLED` proves enabled/active scheduling, not a successful migration.
   `Persistent=true` can start a missed update immediately, so activation can cause
   the short downtime already accepted for this deployment.

The private installed manifest is outside the source checkout. A matching revision
marker alone is insufficient: packaged bytes, source identity and application path
must also match. Documentation edits in a development checkout do not update the
installed package or authorize redeployment.

## Data identity and update transaction

Configured binds, named volumes and write modes must match actual container
mounts. The supported implicit-volume exception is narrow: Redis `/data`, writable
Docker volume, declared by the immutable image actually running. Capture its
existing name and materialize it as an external volume in both the candidate and
old-image checkpoint. Preserve it even if the new Redis/Valkey image does not
provide a `VOLUME` declaration. Other undeclared mounts and explicit identity drift
remain errors; backup-state storage must not overlap any captured data mount.

Before downtime, the updater adapts the official template using neutral placeholders,
copies already resolved site settings, and pulls/pins the selected images. Real
passwords and paths are not parsed as template inputs. Materialization escapes
literal dollars; secret-containing files are mode0600 from creation.

Immediately before stopping a writer, recheck source version, health and captured
identities. Stop the server writer only; keep PostgreSQL available for a complete
custom-format `pg_dump`. Fsync and validate its `PGDMP` header and archive table of
contents. Only a verified backup permits the durable migration boundary and target
`docker compose up -d --wait` on the same project.

After startup, require expected services, health where defined, exact image IDs,
version and ping. Persist `IMMICH_VERSION` and write the private successful receipt
only after those checks. A receipt's successful post-checks are not certification
of every mobile-client or application feature.

## Backup and failure contract

The updater retains a full logical DB backup, original Compose/`.env` with metadata,
and actual old image identities. Free-space admission is
`2 × live database size + 64 MiB`; image pulls also need disk space. No automatic
backup pruning is enabled. Photos/videos require a separate original-file backup.
A DB dump alone cannot recover a missing original.

If backup fails before migration, the unchanged old images may be resumed. Once
migration may have begun, retain the actual state and backup for owner inspection;
never run older images against a potentially migrated DB or automatically restore
an old DB over new uploads.

Application-bound interruption state and selected/default-state journals prevent
blind retries, including retries with a different state directory. Do not clear
markers merely to get another run. Inspect the safe stage/source/operation fields,
then determine the actual mutation boundary and recovery decision. Do not send raw
Docker inspection output, resolved Compose, `.env`, credentials or DB dumps to chat.

## Read-only checks

On the application VM:

```bash
sudo systemctl is-enabled immich-updater.timer
sudo systemctl is-active immich-updater.timer
sudo systemctl list-timers immich-updater.timer --no-pager
sudo systemctl show immich-updater.service --property=ActiveState,SubState,Result,ExecMainStatus
sudo journalctl -u immich-updater.service -n 60 --no-pager
```

Use the installed private venv with the actual application's directory:

```bash
sudo /opt/immich-updater/.venv/bin/python /opt/immich-updater/immich_updater.py --immich-dir /opt/immich --state-dir /var/lib/immich-updater/state --preflight-only
sudo /opt/immich-updater/.venv/bin/python /opt/immich-updater/immich_updater.py --immich-dir /opt/immich --server-url http://localhost:2283 --dry-run
```

`/opt/immich` is the generic example, not an assertion about the deployment's path.
Use `DEPLOYMENT_HANDOFF.md` for the real path. `--as-of` is allowed only with
`--dry-run`: a forecast uses currently published evidence and does not prove future
network availability, backup success or completion. `--prepare-only` is a deprecated
alias of read-only preflight, not a migration rehearsal.

To establish the first scheduled upgrade's outcome, inspect the new run rather
than an older timer `LAST` or service result. Require a fresh completed execution /
private update receipt and the intended live version plus ping. A working public
endpoint does not prove the update ran; a failed updater does not prove an outage.

## External observer and verification evidence

The observer checks public ping once per minute. Three consecutive failures trigger
one notice; healthy and repeated-unhealthy checks are silent. Recovery silently
rearms. Failed delivery retries after a ten-minute cooldown; remote delivery is
at-least-once, not exactly-once. Sleeping/offline observer hosts or failed Telegram
transport delay notification. Deployment-specific observer service/recipient belong
in the workspace availability runbook, not public source.

The cleaned source has 252 tests: 146 active and 106 preserved historical tests.
The installer copies a closed 21-file payload with active tests only; no archive,
clone policy, old controller, Pillow or live-fixture runner is required at runtime.
Fresh-process checks deny all retired imports. Native Compose parser and Node
transport checks, plus a real Redis volume/key lifecycle across two recreations,
supplement recording tests. Prior full synthetic Immich migrations and logical
restore tests are historical evidence, not a new migration of the cleaned source.
No synthetic fixture certifies the owner's scheduled migration. See the verification
document and its linked sanitized receipts.
