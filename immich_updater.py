#!/usr/bin/python3

"""Install the newest stable Immich version published at least seven days ago.

Newer, younger releases do not postpone eligible updates. Major upgrades and
release notes containing "breaking change" across the upgrade path require
manual review. The warning check is a text heuristic, not a safety guarantee.
"""

import argparse
import os
import re
import stat
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
import requests
import sh


### CHANGE THESE VALUES ###

# Where do the Immich docker-compose.yml and .env files live?
IMMICH_DIR = '/opt/immich'

# Name of the file to create in IMMICH_DIR to signify a prior aborted update
BREAKING_CHANGE_FLAG = 'BREAKING_CHANGE'

# Minimum age of each individual release before it can be installed.
DELAY_DAYS = 7

############################


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--immich-dir', default=IMMICH_DIR)
parser.add_argument('--server-url', default='http://localhost:2283')
parser.add_argument('--dry-run', action='store_true',
                    help='Check the release without changing files or Docker.')
parser.add_argument('--verbose', action='store_true')
args = parser.parse_args()
IMMICH_DIR = args.immich_dir


def release_version(release):
    """Return a numeric stable version, excluding drafts and prereleases."""
    if release.get('draft') or release.get('prerelease'):
        return None
    match = re.fullmatch(r'v([0-9]+)\.([0-9]+)\.([0-9]+)',
                         release.get('tag_name', ''))
    return tuple(map(int, match.groups())) if match else None


def load_stable_releases():
    """Read all pages; fail closed if a bounded scan cannot finish."""
    releases = []
    for page in range(1, 11):
        response = requests.get(
            'https://api.github.com/repos/immich-app/immich/releases',
            params={'per_page': 100, 'page': page}, timeout=30)
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list):
            raise ValueError('GitHub did not return a release list.')
        for release in rows:
            version = release_version(release)
            if version is not None and release.get('published_at'):
                published = datetime.fromisoformat(
                    release['published_at'].replace('Z', '+00:00'))
                if published.tzinfo is None:
                    raise ValueError('Release publication time has no timezone.')
                releases.append((version, release, published))
        if not response.links.get('next'):
            return releases
    raise RuntimeError('Release history exceeds ten pages; refusing an incomplete selection.')


def persist_version(tag):
    """Pin the selected version; keep a private backup of the existing .env."""
    env_path = Path(IMMICH_DIR, '.env')
    if env_path.is_symlink() or not env_path.is_file():
        raise ValueError('A regular .env file is required in the Immich directory.')
    metadata = env_path.stat()
    original = env_path.read_bytes()
    text = original.decode('utf-8')
    pattern = r'^(?:export[ \t]+)?IMMICH_VERSION[ \t]*=.*$'
    if re.search(pattern, text, re.MULTILINE):
        updated = re.sub(pattern, f'IMMICH_VERSION={tag}', text,
                         flags=re.MULTILINE)
    else:
        updated = text + ('' if not text or text.endswith('\n') else '\n')
        updated += f'IMMICH_VERSION={tag}\n'
    if updated.encode('utf-8') == original:
        return
    fd, _ = tempfile.mkstemp(prefix='.env.before-immich-updater-',
                             dir=IMMICH_DIR)
    with os.fdopen(fd, 'wb') as backup:
        backup.write(original)
    fd, temporary = tempfile.mkstemp(prefix='.env.immich-updater-',
                                     dir=IMMICH_DIR)
    try:
        with os.fdopen(fd, 'wb') as output:
            os.fchown(output.fileno(), metadata.st_uid, metadata.st_gid)
            os.fchmod(output.fileno(), stat.S_IMODE(metadata.st_mode))
            output.write(updated.encode('utf-8'))
        os.replace(temporary, env_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def err(err_obj: sh.ErrorReturnCode):
    """Prints error messages and quits."""
    print('Error: Failed to run previous command with error code "'
          f'{err_obj.exit_code}". Error message:')
    print(err_obj.stderr)
    sys.exit(1)


# Create a Path object for the breaking-change flag file
BCF = Path(IMMICH_DIR, BREAKING_CHANGE_FLAG)

# Check if there was a breaking change in a past release.
# Do this first, since it does not require remote web requests
if BCF.is_file():
    print("Detected a prior breaking change.\n\n"
          f'Remember to delete the "{BCF}" file, when updating manually.')
    sys.exit(0)

# Retrieve currently-install version from the API.
# JSON dictionary object with 'major', 'minor', and 'patch' keys.
r = requests.get(args.server_url.rstrip('/') + '/api/server/version', timeout=30)
r.raise_for_status()
curr_vers = r.json()
curr_vers_str = (f'v{curr_vers["major"]}.{curr_vers["minor"]}'
                 f'.{curr_vers["patch"]}')

# Choose the highest stable version whose own seven-day waiting period passed.
stable_releases = load_stable_releases()
cutoff = datetime.now(timezone.utc) - timedelta(days=DELAY_DAYS)
eligible = [entry for entry in stable_releases if entry[2] <= cutoff]
if not eligible:
    if args.verbose or args.dry_run:
        print(f'Immich-Updater: Current {curr_vers_str}; no stable release '
              f'is at least {DELAY_DAYS} days old yet.')
    sys.exit(0)
target_version, release_data, _ = max(eligible, key=lambda entry: entry[0])
target_version_str = release_data['tag_name']
current_version = (curr_vers['major'], curr_vers['minor'], curr_vers['patch'])

# If major version has changed, assume there will be breaking changes.
if target_version[0] != curr_vers['major']:
    print('Immich-Updater: Detected a major version change.'
          ' Will not proceed with the update. Currently-installed version:'
          f' {curr_vers_str} / newest eligible release: {target_version_str}.')
    sys.exit(0)

# If no other changes, then can be done.
if target_version <= current_version:
    if args.verbose or args.dry_run:
        print(f'Immich-Updater: Current {curr_vers_str} is already at or newer '
              f'than the newest eligible release {target_version_str}.')
    sys.exit(0)

# Check intermediate releases too, since an upgrade may skip older versions.
# Ignore repeated minor-upgrade warnings within the already installed minor.
for version, release, _ in stable_releases:
    if (current_version < version <= target_version
            and version[1] != current_version[1]
            and re.search('breaking change', release.get('body') or '', re.IGNORECASE)):
        if not args.dry_run:
            BCF.write_text(release['tag_name'], encoding='utf-8')
        print('Immich-Updater: A breaking change has been detected in '
              f'{release["tag_name"]} between {curr_vers_str} and '
              f'{target_version_str}. Will not proceed with the update.\n\n'
              f"Remember to delete '{BCF}' after a reviewed manual update.")
        sys.exit(0)

if args.dry_run:
    print(f'Immich-Updater: Dry run: would update {curr_vers_str} to '
          f'{target_version_str}, pin .env, and restart Docker Compose.')
    sys.exit(0)

# Refuse to start an update when its persistent version pin cannot be written.
env_path = Path(IMMICH_DIR, '.env')
if env_path.is_symlink() or not env_path.is_file():
    print('Immich-Updater: A regular .env file is required. Will not update.')
    sys.exit(1)

# Build a docker SH command
docker = sh.Command('docker')
docker = docker.bake(_cwd=IMMICH_DIR,
                     _env={**os.environ, 'IMMICH_VERSION': target_version_str})

# pull
print(
    f'Immich-Updater: Updating from {curr_vers_str} to {target_version_str}.')
try:
    out = docker('compose', 'pull')
except sh.ErrorReturnCode as e:
    err(e)

# Persist only after the selected images have been downloaded successfully.
# Keep the selected version if startup fails; automatic downgrades are unsafe.
persist_version(target_version_str)

# reload
print('Immich-Updater: Reloading server.')
try:
    out = docker('compose', 'up', '-d')
except sh.ErrorReturnCode as e:
    err(e)

sys.exit(0)
