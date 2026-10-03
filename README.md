# Immich Updater

A small fork of [dpantel/immich-updater](https://github.com/dpantel/immich-updater) for delayed Docker Compose updates.

## Behaviour

- Read the stable Immich release history on GitHub and select the **highest version published at least seven full days ago**.
- Each release has its own waiting period. A newer release does not reset or postpone an older eligible release: if `v3.2.3` is eight days old and `v3.2.4` is one day old, install `v3.2.3`. Once `v3.2.4` reaches seven days, it becomes eligible too.
- If several releases are already eligible, install the highest version directly rather than replaying every intermediate version. Already installed or older versions are not reinstalled.
- Pass the exact checked version to both `docker compose pull` and `docker compose up -d`.
- After a successful pull, save the selected `IMMICH_VERSION` in the stack's `.env`. This keeps later manual Compose commands on the installed version.
- Preserve the other `.env` settings and its ownership/permissions. Keep a mode-0600 backup named `.env.before-immich-updater-*` in the stack directory.
- Do not automatically cross a major-version boundary, install a prerelease, or downgrade.
- Keep the upstream text-based breaking-change guard and check intermediate stable releases on the upgrade path too. `--dry-run` never changes files or Docker.

## Requirements

- Python 3.9 or newer, Linux
- Docker Compose
- An existing working Immich Compose stack with a regular `.env` file
- Permission to run Docker and write the stack directory and `.env`

Install the Python dependencies in a virtual environment:

```bash
git clone https://github.com/Dvredin/immich-updater.git
cd immich-updater
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Check your instance without changing it:

```bash
.venv/bin/python immich_updater.py --immich-dir /opt/immich --dry-run
```

Run an eligible update:

```bash
.venv/bin/python immich_updater.py --immich-dir /opt/immich --verbose
```

The local API defaults to `http://localhost:2283`. Use `--server-url URL` if needed. Set `DELAY_DAYS` in the script if a different waiting period is desired.

## Daily systemd timer

The example units assume the updater checkout and its virtual environment live at `/opt/immich-updater`, and the Compose stack is `/opt/immich`. Adapt those paths before installation. The service uses a lock to prevent overlapping scheduled/manual service runs, has a 30-minute timeout, and runs as root for Docker access. Do not run a second updater for the same stack.

```bash
sudo install -m 0644 systemd/immich-updater.service /etc/systemd/system/
sudo install -m 0644 systemd/immich-updater.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now immich-updater.timer
systemctl list-timers immich-updater.timer
```

The timer checks daily at **09:35 in the server's timezone**. `Persistent=true` catches up after downtime. It enables the timer, not an immediate forced upgrade. Read its results with:

```bash
journalctl -u immich-updater.service
```

## Safety and limitations

The breaking-change check searches stable release notes between the installed version and the selected target for the text `breaking change` when crossing a minor-version boundary. Repeated warnings inside the already installed minor version are ignored. This is a heuristic and can still miss differently worded migration requirements. A detected warning writes a `BREAKING_CHANGE` flag that blocks later automatic runs. After a reviewed manual update, remove that flag yourself.

The release history is paginated and bounded to ten pages of 100 entries. An HTTP error, malformed publication timestamp, or history that cannot be fully scanned within that bound stops the update instead of installing an incompletely selected target.

This script does **not** update Compose definitions, migrate PostgreSQL, back up the database/media, or guarantee that an eligible release is bug-free. Maintain a separate, tested Immich backup process and read the [official upgrade instructions](https://docs.immich.app/install/upgrading/). A `.env` backup is not a database backup. Those files can contain credentials; keep them private and never commit them.

A failed pull leaves `.env` unchanged and does not restart containers. If startup fails after a successful pull, the selected version remains pinned; automatic downgrade is deliberately avoided because Immich does not support it. The script logs the command failure but does not perform a post-start health check or send notifications.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q immich_updater.py tests
git diff --check
```

The regression tests use explicitly simulated HTTP replies and a recording Docker stub; they do not update a real server.

## License

MIT, retaining the original copyright notice in [LICENSE](LICENSE).
