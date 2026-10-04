# Immich Updater

A delayed unattended updater for the stock four-service Immich Docker Compose deployment. Reuses Docker Compose, PostgreSQL backup tools, GNU `cp`/CoW, the official release Compose template, GitHub advisories and `semantic-version`; no issue triage, notification service or per-release approval.

A successful synthetic test does not verify another host's storage/runtime. Complete that host's non-production `--prepare-only` rehearsal and recovery gate before enabling unattended updates. Historical strict issue-filter tests under `tests/archive/` are not the current policy.

## Automatic policy

| Gate | Behaviour |
|---|---|
| Patch | Three full days since that release's `published_at`. |
| Minor/major | Seven full days; both are automatic. A quick `.1` also requires its feature/major base releases to have matured. |
| Choice | Highest eligible stable semantic version. New releases do not restart old releases' waiting periods. No RC/beta/downgrade. |
| Security | Check installed and candidate against published, non-withdrawn official advisories. Applicable high/critical findings veto; unknown serious applicability or unavailable/incomplete sources fail closed. |
| Urgent fix | A confirmed high/critical vulnerability in the installed version can waive age only for an explicitly shipped corresponding fix that passes every other gate. Unknown severity does not authorize urgency. |
| Issues/release prose | No issue search, manual bug exception list or regex veto on `Breaking Changes`. Compatibility is exercised on the isolated copy. |
| Failed candidate | Persist a quarantine record, do not repeat that exact failed version each day. A new eligible release gets its own tests. |
| Delivery | Local structured stdout/systemd journal and private receipts only; no Telegram/email/webhook notification. |

## Execution

1. Resolve this installation's paths and download the **exact release's** official Compose template. Preserve stock site mounts/settings, adopt its official images/commands/health checks, and pull/pin images by content address before touching production. Unknown topology is a safe skip/failure, not a guessed migration.
2. Capture the complete custom-format PostgreSQL dump and comparison baseline in one exported snapshot. Clone only a bounded original-file sample: at most 16 files, 128 MiB total, 64 MiB per file, chosen from 64 DB candidates. Preserve their relative paths and stock storage sentinel files. Every clone mount is private; external libraries are sampled too, never mounted from production. ML starts with an empty private cache, so this does not certify production model inference. No full-photo-library walk/copy occurs during rehearsal.
3. Run new PostgreSQL/Redis and restore the dump on a private Docker bridge configured `internal` with IPv4/IPv6 **isolated gateway modes**. There are no host ports, Docker socket, shared production volumes, devices or outbound endpoints/credentials. Inspect actual network/mount isolation before starting the candidate application and again afterwards.
4. Start the candidate on the copied database/files. Check migrations, selected version, authenticated API reads, sample-original content hash, clone-only album write/delete and metadata identity/counts/album/face relationships across the old/new schema. Authentication uses a local temporary API key; this does not test external OAuth or every user interaction.
5. Automatically build another private old-version copy and run a **real full-state recovery drill**: migrate it, intentionally delete a clone album/change clone file bytes, inject a post-migration failure, restore that test build's cold files/database/config/images and verify old runtime health. This restores all state of the small build, not the complete production media library. Production remains untouched throughout.
6. Re-fetch security advisories. Freeze the production application writer; read the fresh baseline from its still-running DB, stop every stack container and verify stop. Capture all writable media, PostgreSQL, model-cache/local-volume directories plus Compose/`.env` together. Keep the old image IDs. A failed capture resumes the unchanged old stack; it must not restore a partial checkpoint.
7. Journal the upgrade **before** mutation, publish the tested pinned Compose config **with every host port removed**, and start/check the candidate while new client writes remain blocked. Persist `IMMICH_VERSION` in `.env`, commit the verified new state, then activate its original host ports. Once activation may admit user writes, recovery retries the committed candidate instead of restoring an older snapshot and losing those writes. The rendered Compose file is JSON (valid YAML), mode 0600, and contains resolved settings; it is secret-bearing and never logged or committed.
8. A startup/post-start failure stops the complete stack and restores **all mutable directories, DB, configuration and old images together**. Original candidate-mutated directories are renamed and preserved. Compare restored file bytes with the cold checkpoint **while stopped**, then restart/check the old application. Generated thumbnails/storage sentinel files may legitimately change after old startup; original asset/config bytes must remain restored.
9. Crash recovery is processed under the application lock **before** attempting to read the application version or fetch upstream data. A committed journal is cleaned without a downgrade. An interrupted/failed target is quarantined. Failed stop/copy/config verification prevents starting partial restored state.

There is downtime during the fresh production checkpoint and the production update. New writes cannot reach the stopped application. Generic startup, metadata and sampled file checks are not a guarantee against rare logic bugs, future requirements or arbitrary schema changes. A separate disaster backup remains necessary; same-host recovery is not protection against losing the host/storage.

## Host requirements

- Linux, Python 3.10+, root for complete PostgreSQL/named-volume cold copies; Docker Compose with `up --wait`, local cgroup v2, Docker systemd cgroup driver and readable clone memory controllers.
- Docker Engine supporting bridge `gateway_mode_ipv4=isolated` and `gateway_mode_ipv6=isolated` on an internal bridge. Unsupported options must block rehearsal; never replace this with ordinary shared networking.
- Exactly the stock `immich-server`, `immich-machine-learning`, `database`, `redis` services, normal `/data` and PostgreSQL directory mappings, a regular `.env` and one Compose file (or `--compose-file`).
- Mutable data roots must be disjoint ordinary local directories, not broad system paths, symlink parents, mountpoints, or externally-managed volume drivers. GPU/device passthrough and changed service topology require another verified backend; they are not silently emulated as production compatibility.
- Rehearsal space is based on the live PostgreSQL database size and a fixed sample allowance, not the complete photo library. Current conservative admission is `6 × database size + 6 × 128 MiB + 512 MiB`, covering dumps, sequential test instances and their recovery state. Recheck before each capture; unknown/insufficient capacity fails closed. Retained past runs still consume space and are not silently deleted.
- Production rollback remains independent: a full cold copy of mutable production directories and room for restore staging are mandatory before a source upgrade. Read-only external originals do not need a production rollback copy. No sample receipt certifies production backup; activation refuses when the supported complete-mutable-state backend cannot fit. No snapshot backend or guaranteed CoW savings are implied.
- Source images must still exist locally for content-addressed recovery. Do not prune images/checkpoints during an active transaction.

## Compact rehearsal on a 4 GiB host

Rehearsal and recovery copies run sequentially. Each copy has its own native
systemd/cgroup-v2 parent pool with a **1824 MiB total hard RAM limit**, zero swap,
and these additional child ceilings:

| Clone service | Individual ceiling |
|---|---:|
| Server | 1408 MiB |
| PostgreSQL | 1024 MiB |
| Machine learning | 384 MiB |
| Redis | 32 MiB |

Child ceilings intentionally sum to more than the shared pool: a service can borrow
currently unused capacity, but the complete clone cannot exceed **1824 MiB**.
This avoids reserving idle RAM for one service while another OOMs during startup.
Within each clone, PostgreSQL/Redis start first, then the stock server starts with
ML still stopped. HTTP health alone is insufficient: ML starts only after the stock
microservices bootstrap has completed, so its initialization does not overlap geodata
import. Missing/changed bootstrap evidence fails closed. All four must be running and
pass resource checks for acceptance. This staging applies only to isolated copies,
not production startup. Already inspected containers are not recreated during startup.

The kernel documents temporary accounting overcharges even with `memory.max` enforced.
Receipts retain historical peaks; acceptance requires exact configured parent/child
limits, zero OOM counters and current usage within budget, rather than mistaking a
historical transient peak for an unenforced limit. The host/controller reserve is
separate from the configured clone pool.
Both parent and child enforcement, kernel ancestry and OOM counters must pass.
Temporary pool settings apply only to the clone, never production or a global slice.

The preflight requires **2080 MiB available RAM**: the shared clone pool plus a 256 MiB
host/controller reserve. This replaces the former fixed 6 GiB free-RAM gate, not the
rehearsal or recovery gates. Current source containers and their resource policy are
not modified. Source resource/cgroup settings are retained during the eventual update.

Every clone has swap disabled, no automatic restart and increased OOM-victim priority.
The clone server uses the supported Node `--max-old-space-size=512` heap ceiling
per Node process; this does not change production environment or worker topology.
Clone PostgreSQL retains its original/image startup arguments and appends only
`shared_buffers=64MB`, `work_mem=4MB`, `maintenance_work_mem=64MB`; image preload
configuration must remain intact. These tuning arguments never enter production.
Actual Docker limits are inspected before startup; after startup, local cgroup-v2
`memory.max`, `memory.swap.max` and OOM counters are checked. A child-process OOM
cannot be accepted merely because the container init remains alive. Host RAM pressure
before a rehearsal is a logged automatic deferral, not a permanently quarantined release.
A clone that cannot pass under its limits does not authorize a production update.

This is a bounded clone policy, not a promise that every future release, ML model or
large library fits 4 GiB. The host's real copied-data acceptance is still required.

## Installation on an existing host

Clone this repository and check out the exact published commit you intend to install.
Use the actual directory of the existing Immich Compose deployment, not a new empty directory:

```bash
sudo systemctl disable --now immich-updater.timer
sudo python3 tools/install.py --app-dir /opt/immich --expected-revision COMMIT_SHA
```

The installer verifies the source revision, checks Docker 28+, x86-64-v2 where applicable,
the compact clone memory budget and database/sample capacity, creates a private staging venv, runs the tests,
and runs `--prepare-only` on private copies. It preserves the previous updater, does not
upgrade production and leaves the timer disabled. A skip is not host acceptance.

Only after `PREPARED_TIMER_DISABLED`, enable automation separately:

```bash
sudo python3 /opt/immich-updater/tools/install.py --app-dir /opt/immich --expected-revision COMMIT_SHA --activate
sudo journalctl -u immich-updater.service -n 60 --no-pager
```

Activation requires the matching host receipt, unchanged installed code and separately verified capacity for a complete production checkpoint/recovery. A passing sampled rehearsal is not a production rollback certificate. `Persistent=true`
can trigger a missed run immediately. A `STOP` means do not enable the old timer manually;
inspect the local preparation log. Python 3.10+, `python3-venv`, Git and the supported
Docker/Compose installation must already be available; the installer does not upgrade Docker/OS.
Package preparation prints `PACKAGE_STEP` for venv creation, dependency installation and
unit tests. Each step saves stdout/stderr privately in the staging directory as
`venv-create.log`, `dependency-install.log` or `package-tests.log`, including failures
and partial timeout output. A failed step leaves the old updater in place, does not
start rehearsal or enable the timer, and reports the exact local diagnostic path.
Do not publish those logs wholesale; subsequent storage/application logs may contain
resolved private configuration.

## Commands

Install dependencies in the existing checkout/venv:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Read-only decision (no Docker commands, locks, files, pulls or source writes):

```bash
.venv/bin/python immich_updater.py --immich-dir /opt/immich --server-url http://localhost:2283 --dry-run
```

One-time host acceptance: full isolated rehearsal and destructive **clone-only** recovery test, **no production update**:

```bash
sudo .venv/bin/python immich_updater.py --immich-dir /opt/immich --state-dir /var/lib/immich-updater/state --prepare-only
```

Normal unattended invocation after that host is verified:

```bash
sudo .venv/bin/python immich_updater.py --immich-dir /opt/immich --state-dir /var/lib/immich-updater/state
```

The updater repeats rehearsal and the clone-only restore gate for every chosen candidate, so there is no per-release manual procedure. `--as-of <timezone-aware timestamp>` forecasts selection and is permitted only with `--dry-run`; future upstream evidence may change the result. State defaults to `<app>/.immich-updater-state` unless `--state-dir`/`IMMICH_UPDATER_STATE` is set. An existing legacy `UPDATE_FAILED` requires genuine recovery, not just deleting the marker.

## Timer and private logs

`systemd/immich-updater.timer` remains the **single** daily 09:35 timer in the server's timezone, with missed-run catch-up. Do not enable a second updater or leave an older unsafe script running while installing this revision. Keep existing stack-path drop-ins and set `IMMICH_UPDATER_STATE=/var/lib/immich-updater/state` in the service environment. The oneshot runs as root with private umask, a three-hour main timeout, time for termination recovery and local restart handling; there is no outbound error-forwarding hook.

Before claiming installed, verify the actual VM's revision, service/drop-ins, Python/Docker, source mounts/capacity, `--prepare-only` receipt, timer enabled/active state, timezone/next run and application version. Enabling a timer is not a successful update.

## Verification

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q immich_updater.py risk_checks.py resource_policy.py sample_rehearsal.py rehearsal.py transaction.py recovery_drill.py tests tools
git diff --check
```

Unit tests are explicitly simulated evidence/lifecycle calls, with real local file-copy/atomic-journal checks. The synthetic live fixture is separately marked and isolated: `tests/seed_live_fixture.py` refuses an unmarked/non-internal stack. It uses Pillow only for that fixture, not the production updater. Never point fixture setup at production data. Live receipts/checkpoints contain private state and must not be published.

## License

MIT, retaining the original copyright notice in [LICENSE](LICENSE).
