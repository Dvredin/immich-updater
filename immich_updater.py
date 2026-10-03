#!/usr/bin/env python3
"""Automatic delayed Immich updates, isolated rehearsal and full-state recovery."""
import argparse
import fcntl
import hashlib
import json
import os
import re
import signal
import stat
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from risk_checks import GitHub, SERIOUS, advisory_risks, log, stamp, version

IMMICH_DIR = '/opt/immich'
PATCH_DAYS = 3
FEATURE_DAYS = 7

def release_version(release):
    if release.get('draft') or release.get('prerelease'):
        return None
    try:
        if not release['tag_name'].startswith('v'):
            return None
        v = version(release['tag_name'])
        return v.major, v.minor, v.patch
    except (KeyError, ValueError):
        return None


def load_stable_releases(github, as_of):
    releases = []
    seen = set()
    for release in github.pages('/releases'):
        v = release_version(release)
        if v is None:
            continue
        if not release.get('published_at'):
            raise ValueError('Stable release has no publication time.')
        published = stamp(release['published_at'])
        if release['tag_name'] in seen:
            raise ValueError('Duplicate stable release; history changed during scan.')
        seen.add(release['tag_name'])
        if published <= as_of:
            releases.append((v, release, published))
    return releases


def current_version(server_url):
    response = requests.get(server_url.rstrip('/') + '/api/server/version', timeout=30)
    response.raise_for_status()
    data = response.json()
    fields = [data[k] for k in ('major', 'minor', 'patch')]
    if any(type(field) is not int or field < 0 for field in fields):
        raise ValueError('Invalid installed version response.')
    return tuple(fields)


def tag(v):
    return 'v' + '.'.join(map(str, v))


def age_blocks(current, candidate, published, releases, as_of):
    days = PATCH_DAYS if candidate[2] else FEATURE_DAYS
    blocks = []
    eligible_at = published + timedelta(days=days)
    if as_of < eligible_at:
        blocks.append('release_wait_until=' + eligible_at.isoformat())
    # A young feature/major release cannot evade its week via a quick .1 patch.
    if candidate[:2] != current[:2]:
        bases = {v: p for v, _, p in releases if v[2] == 0}
        required = {(candidate[0], candidate[1], 0)}
        if candidate[0] != current[0]:
            required.add((candidate[0], 0, 0))
        required.update(v for v in bases if current < v <= candidate)
        for base in sorted(required):
            if base not in bases:
                blocks.append('missing_feature_release=' + tag(base))
            elif as_of < bases[base] + timedelta(days=FEATURE_DAYS):
                blocks.append('feature_wait=' + tag(base))
    return blocks

def persist_version(directory, selected):
    env_path = directory / '.env'
    if env_path.is_symlink() or not env_path.is_file():
        raise ValueError('A regular .env file is required.')
    metadata = env_path.stat()
    original = env_path.read_bytes()
    text = original.decode('utf-8')
    pattern = r'^(?:export[ \t]+)?IMMICH_VERSION[ \t]*=.*$'
    if re.search(pattern, text, re.MULTILINE):
        updated = re.sub(pattern, f'IMMICH_VERSION={selected}', text, flags=re.MULTILINE)
    else:
        updated = text + ('' if not text or text.endswith('\n') else '\n')
        updated += f'IMMICH_VERSION={selected}\n'
    if updated.encode() == original:
        return
    fd, _ = tempfile.mkstemp(prefix='.env.before-immich-updater-', dir=directory)
    with os.fdopen(fd, 'wb') as backup:
        backup.write(original)
        backup.flush()
        os.fsync(backup.fileno())
    fd, temporary = tempfile.mkstemp(prefix='.env.immich-updater-', dir=directory)
    try:
        with os.fdopen(fd, 'wb') as output:
            os.fchown(output.fileno(), metadata.st_uid, metadata.st_gid)
            os.fchmod(output.fileno(), stat.S_IMODE(metadata.st_mode))
            output.write(updated.encode())
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, env_path)
        parent_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def choose(github, current, as_of, quarantine=None):
    releases = load_stable_releases(github, as_of)
    advisories = github.pages('/security-advisories', limit=3)
    current_tag = tag(current)
    installed_blocks, _ = advisory_risks(github, advisories, current_tag)
    serious_ids = {row['ghsa_id'] for row in advisories if row.get('severity') in SERIOUS
                   and row.get('state') == 'published' and not row.get('withdrawn_at')}
    urgent_ids = {reason.split(':', 1)[0] for reason in installed_blocks
                  if reason.endswith(': affected') and reason.split(':', 1)[0] in serious_ids}
    log('installed_risk', installed=current_tag, advisories=installed_blocks,
        urgent_security_ids=sorted(urgent_ids))
    for candidate, row, published in sorted(releases, key=lambda e: e[0], reverse=True):
        if candidate <= current:
            continue
        selected = row['tag_name']
        blocks, fixes = advisory_risks(github, advisories, selected)
        if selected in (quarantine or {}):
            blocks.append('failed_candidate_quarantine')
        urgent = bool(urgent_ids) and urgent_ids <= fixes and not blocks
        blocks += [] if urgent else age_blocks(current, candidate, published, releases, as_of)
        log('candidate', installed=current_tag, target=selected,
            published_at=published.isoformat(), urgent=urgent,
            decision='blocked' if blocks else 'eligible', reasons=blocks)
        if not blocks:
            return selected, urgent
    return None


def compose_file(directory, explicit):
    if explicit:
        path = Path(explicit).resolve()
        if path.parent != directory.resolve():
            raise ValueError('Compose file must be directly inside the application directory.')
        return path
    found = [directory / name for name in ('docker-compose.yml', 'docker-compose.yaml', 'compose.yml', 'compose.yaml')
             if (directory / name).is_file()]
    if len(found) != 1:
        raise ValueError('Exactly one Compose file is required, or supply --compose-file.')
    return found[0]


def run(args):
    directory = Path(args.immich_dir).resolve()
    state_dir = Path(args.state_dir).absolute() if args.state_dir else directory / '.immich-updater-state'
    from transaction import apply, candidate_config, private_root, restore, state_save
    from rehearsal import rehearse
    from recovery_drill import restore_drill
    if not args.dry_run:
        private_root(state_dir)
        restored = restore(state_dir)  # BEFORE reading possibly-unavailable application API.
        if restored:
            log('decision', decision='recovered', reason='Interrupted transaction processed; no further upgrade this run.')
            return 0
    as_of = stamp(args.as_of) if args.as_of else datetime.now(timezone.utc)
    current = current_version(args.server_url)
    quarantine_path = state_dir / 'quarantine.json'
    quarantine = json.loads(quarantine_path.read_text()) if quarantine_path.is_file() else {}
    log('check', installed=tag(current), as_of=as_of.isoformat(), dry_run=args.dry_run,
        patch_days=PATCH_DAYS, feature_days=FEATURE_DAYS)
    chosen = choose(GitHub(), current, as_of, quarantine)
    if chosen is None:
        log('decision', installed=tag(current), decision='skip', target=None, reason='No newer candidate passed age/security gates.')
        return 0
    selected, urgent = chosen
    log('decision', installed=tag(current), decision='dry_run' if args.dry_run else 'rehearse',
        target=selected, urgent=urgent)
    if args.dry_run:
        return 0
    source_path = compose_file(directory, args.compose_file)
    # Refuse unknown legacy failed-update state; never silently clear a migrated DB.
    if (directory / 'UPDATE_FAILED').exists():
        raise ValueError('Legacy failed-update marker requires recovery before this updater can take ownership.')
    # Fetch/pull/preflight failures are infrastructure failures, not evidence of
    # a bad release. Retry them later without permanently quarantining the tag.
    candidate = candidate_config(source_path, selected, state_dir / 'candidates')
    try:
        rehearse(source_path, selected, state_dir / 'rehearsals', candidate_path=candidate)
        restore_drill(source_path, candidate, tag(current), selected, state_dir / 'restore-drills')
        if args.prepare_only:
            log('decision', decision='verified_not_applied', target=selected, production_mutations=False)
            return 0
        # Source advisories can change during a long rehearsal. No stale evidence fallback.
        refreshed = GitHub()
        advisories = refreshed.pages('/security-advisories', limit=3)
        blocks, _ = advisory_risks(refreshed, advisories, selected)
        if blocks:
            raise ValueError('Candidate acquired an unresolved/serious security advisory during rehearsal.')
        apply(source_path, candidate, selected, tag(current), state_dir)
    except BaseException as exc:
        quarantine[selected] = {'installed': tag(current), 'error_type': type(exc).__name__,
                                'recorded_at': datetime.now(timezone.utc).isoformat()}
        state_save(quarantine_path, quarantine)
        log('quarantine', target=selected, automatic_retry=False, error_type=type(exc).__name__)
        raise
    return 0


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--immich-dir', default=os.environ.get('IMMICH_DIR', IMMICH_DIR))
    result.add_argument('--server-url', default=os.environ.get('IMMICH_SERVER_URL', 'http://localhost:2283'))
    result.add_argument('--compose-file')
    result.add_argument('--state-dir', default=os.environ.get('IMMICH_UPDATER_STATE'))
    result.add_argument('--dry-run', action='store_true')
    result.add_argument('--prepare-only', action='store_true', help='Run isolated upgrade and recovery tests only; never update source.')
    result.add_argument('--verbose', action='store_true', help='Compatibility option: local decision logging is always enabled.')
    result.add_argument('--as-of', help='Forecast time with timezone; dry-run only.')
    return result


def main():
    args = parser().parse_args()
    if args.as_of and not args.dry_run:
        parser().error('--as-of is allowed only with --dry-run.')
    if args.prepare_only and args.dry_run:
        parser().error('--prepare-only and --dry-run are mutually exclusive.')
    lock_fd = None
    try:
        if not args.dry_run:
            lock_fd = os.open(Path(args.immich_dir) / '.immich-updater.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            def terminate(signum, frame):
                raise KeyboardInterrupt('Termination: transaction recovery required.')
            signal.signal(signal.SIGTERM, terminate)
        return run(args)
    except BlockingIOError:
        log('decision', decision='skip', reason='Another update owns the application lock.')
        return 0
    except BaseException as exc:
        if isinstance(exc, SystemExit):
            raise
        # Never expose Docker stderr, resolved configuration or request credentials.
        log('failure', error_type=type(exc).__name__, decision='fail_closed',
            reason='Inspect private local receipts; no outbound notification.')
        return 1
    finally:
        if lock_fd is not None:
            os.close(lock_fd)


if __name__ == '__main__':
    sys.exit(main())
