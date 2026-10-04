# Immich Updater

A delayed automatic updater for a stock four-service Immich Docker Compose stack.
The current `single-stack-v1` workflow operates on **one running installation**:
no parallel Immich, sampled library, migration rehearsal or automatic downgrade.
Short downtime and owner intervention after a failed update are an explicit tradeoff.

## Automatic policy

| Gate | Behaviour |
|---|---|
| Patch | Three full days since that stable release's `published_at`. |
| Minor/major | Seven full days for the base release; both are automatic. A patch entering a new branch must also have a mature feature/major base. |
| Choice | Highest eligible stable semantic version. Each release ages independently; no RC/beta/downgrade or mutable target tag. |
| Security | Check installed and candidate against official published, non-withdrawn advisories. Applicable high/critical or unresolved serious applicability veto; retrieval fails closed. |
| Urgent fix | Only a confirmed high/critical installed vulnerability plus a shipped corresponding fix can bypass age, not other gates. |
| Release prose | No issue search, manual per-release approval or keyword veto on `Breaking Changes`. Rehearsal is not performed. |
| Failure | A durable pending update blocks further updates until the owner inspects it. No restart/downgrade loop or old image over a migrated DB. |

## Execution

1. Bind the server to the stock local database and require its actual version to
   match selection, with a strictly newer target. Check the existing runtime and
   space for a full **logical database** backup.
   There is no clone-memory gate or traversal/copy of the photo library.
2. Resolve the selected release's official Compose template using neutral placeholders
   and an explicit template environment, never real DB passwords or storage paths.
   Copy already-resolved site settings/mounts, including named volumes and absent
   port publication, pull the explicit images while the old stack is still available,
   and pin local image IDs. Recheck security before downtime.
3. Require actual container bind/named-volume identities and read/write modes to
   match the resolved configuration before stopping any writer. Record the actual old
   images and privately save the original Compose file and `.env`, including metadata.
4. Stop only the `immich-server` writer and verify that it stopped. Dump the complete
   PostgreSQL database in custom format, fsync it, verify `PGDMP` and parse its table
   of contents with the installed `pg_restore`. An incomplete/failed backup never
   authorizes target startup. PostgreSQL remains up for the dump.
5. Write a durable mutation boundary, publish the pinned candidate as a private
   mode0600 file (it contains resolved environment values), and run normal
   `docker compose up -d --wait` for the same project. No second stack is started and
   production RAM, Node heap and database tuning are not reduced.
6. Check all expected containers, health where defined, exact pinned image IDs,
   the selected server version and ping API. Persist `IMMICH_VERSION` only after
   those checks and retain a private update receipt/backup.

Resolved Compose files escape literal `$` settings during materialization and
decode Compose's escaped config output; passwords, Unicode and paths do not pass
through a second interpolation layer. Creation of secret-containing temporary files
is private from the first open, regardless of the original Compose's old permissions.

If backup fails before migration, resume the unchanged old images when possible.
If target startup or post-checks fail, retain the actual state and backup for owner
inspection. The same candidate is not retried while its pending marker exists.
Failures log safe stage, source location, operation, exit status and allowlisted
error hints when available. They never publish raw stderr, full argv, credentials
or resolved configuration. A failure before `apply` does not claim a nonexistent
private update receipt. Migration-side failures retain boundary/diagnostic state.
A failed check does not prove the website is down; the external monitor checks
availability independently. A successful check is not full functional certification.

### Backup boundaries

The updater backs up the database, configuration and previous image identities.
**It does not back up photos/videos**, recursively copy media/cache directories,
perform a full-state restore drill or promise automatic recovery. Keep a separate
backup of original files: a database dump alone cannot recover a deleted original.
Backup admission is currently `2 × live database size + 64 MiB` free on the state
filesystem. Historical backups are retained; no automatic prune/deletion is enabled.

Immich [does not support downgrade](https://docs.immich.app/install/upgrading/).
Recovery after migration requires compatible DB restoration and intact originals,
not just older images. Do not restore an old DB over newly accepted uploads without
considering those writes. The [official backup instructions](https://docs.immich.app/administration/backup-and-restore/)
explain database/file compatibility and recovery. The old complete-filesystem backend
is preserved as historical code, not invoked by this updater.

## Requirements

- Linux, Python 3.10+, `python3-venv`, Git, Docker 24+ and Compose supporting `up --wait`.
- Exactly `immich-server`, `immich-machine-learning`, `database`, `redis`, standard
  `/data` and PostgreSQL mappings, a regular `.env` and one Compose file. Server DB
  host/user/database/password must match the local `database` service on port5432,
  including actual container settings; external DB/URL/file overrides are rejected.
- Supported stock CPU deployment; GPU/custom entrypoint or changed topology is not
  silently translated into another deployment.
- Root for the owner-run installer/update; private state outside all data mounts.
  No clone cgroup/systemd RAM controller or isolated clone gateway is required.
- Enough disk for the DB/config backups and selected images. No extra photo-copy
  capacity or fixed available-RAM threshold is required.

## Installation

Check out an exact published commit first; use the existing application directory:

```bash
sudo python3 tools/install.py --app-dir /opt/immich --expected-revision COMMIT_SHA
```

The installer disables the old daily timer, refuses an active updater or unfinished
legacy transaction, privately stages dependencies, runs package tests, and performs
**read-only** runtime/DB-backup-space validation. It does not stop or update Immich,
pull target images, create a clone or rehearse a migration. It preserves the previous
updater and emits `PREPARED_TIMER_DISABLED` only for an unchanged complete package.
Package bytes must match every packaged file in the requested commit, not just
`HEAD`; installed activation is bound to that private verified manifest. Symlinked
unit destinations/ancestry are rejected and unit writes are atomic. Package failures
retain private step logs. Never publish logs/config/dumps wholesale.

Activate only after that receipt:

```bash
sudo python3 /opt/immich-updater/tools/install.py --app-dir /opt/immich --expected-revision COMMIT_SHA --activate
```

Activation rechecks the installed manifest and live preflight using the private venv.
The existing single daily 09:35 timer remains in the server timezone. `Persistent=true`
may trigger a missed update **immediately**. The update service does not restart on
failure. Do not enable an old timer manually after `STOP`.

### Read-only commands

```bash
.venv/bin/python immich_updater.py --immich-dir /opt/immich --server-url http://localhost:2283 --dry-run
.venv/bin/python immich_updater.py --immich-dir /opt/immich --state-dir /var/lib/immich-updater/state --preflight-only
```

`--prepare-only` is now a deprecated alias for read-only `--preflight-only`, not a
rehearsal. `--as-of` is permitted only for `--dry-run`. Normal invocations own an
application lock; the application-bound `.immich-updater-interrupted.json`, default
and selected-state `simple-update.json`, and legacy transaction markers block updates.
Changing `--state-dir` does not bypass a failed/interrupted update.

## Outage notifications

`availability_monitor.py` is an optional, **separate external observer**, not an
updater repair loop. It uses a public `/api/server/ping` once per minute and emits
one actionable notice after three consecutive failures. Short outages are ignored;
healthy/repeated unhealthy checks are silent, and recovery silently rearms it.
Failed delivery remains pending with a ten-minute retry cooldown. Recovery discards
obsolete unsent outage notices. A remote send accepted just before a local crash can
repeat on retry; the delivery contract is at-least-once, not exactly-once.

The monitor reuses an existing `notification_outbox.send` module through an explicit
`--sender-directory` and explicit `--chat-id`/`--topic-id`; no credentials or recipient
are embedded in this repository. Run it on a different host if VM-wide failures must
be observed. A sleeping/offline monitor host or failed Telegram path delays alerts.
The monitor reports endpoint unavailability, not a proven server/migration cause.
It never restarts or edits the application. [Verification](docs/REHEARSAL_VERIFICATION.md)
separates updater tests, synthetic migrations, notification delivery and real-host deployment.

## Verification and history

```bash
.venv/bin/python -m unittest discover -s tests -q
.venv/bin/python tools/install.py --self-test
.venv/bin/python -m compileall -q immich_updater.py simple_update.py availability_monitor.py transaction.py tests tools
```

Legacy rehearsal code/tests/receipts remain for history and recovery investigation.
They are not the active controller or installer acceptance requirement.
`tests/run_simple_live.py` exercises only explicitly synthetic data for developer
acceptance; never point it at an existing deployment. Private fixtures, resolved
configuration, credentials and DB dumps are never public repository artifacts.

## License

MIT, retaining the original [LICENSE](LICENSE).
