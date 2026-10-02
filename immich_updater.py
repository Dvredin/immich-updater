#!/usr/bin/python3

"""Simple and dumb Immich server updater.

Compares the current server version with the version of the latest release on
Github. If there has been a major version update -OR- the release notes say
"breaking change" (case-insensitive) anywhere, then it aborts. Otherwise, if
there has been a version change, will do `docker pull`, `docker compose up -d`.

Limitations:
This script is DUMB. It's literally looking for a string in the release notes.
Also, this script only looks at the notes of the LATEST release. That means
that it needs to be run often (daily? weekly?) to make sure that it does not
miss a "breaking change" release between runs.
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

# How many days do you want to wait after the latest release before you
# update to it? (Allows the initial kinks to get worked out.)
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

# Retrieve the latest release info from github.
r = requests.get(
    "https://api.github.com/repos/immich-app/immich/releases/latest",
    allow_redirects=True, timeout=30)
r.raise_for_status()
release_data = r.json()

# Extract release version from 'tag_name' or 'name'.
# It will be a string in the form of 'v<major>.<minor>.<patch>'.
latest_version_str = release_data['tag_name']
if (release_data.get('draft') or release_data.get('prerelease')
        or re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+', latest_version_str) is None):
    print('Immich-Updater: Not a stable release. Will not update.')
    sys.exit(0)
latest_version = latest_version_str.lstrip('v').split('.')

# If major version has changed, assume there will be breaking changes.
if int(latest_version[0]) != int(curr_vers['major']):
    print('Immich-Updater: Detected a major version change.'
          ' Will not proceed with the update. Currently-installed version:'
          f' {curr_vers_str} / latest release: {latest_version_str}.')
    sys.exit(0)

# If no other changes, then can be done.
if tuple(map(int, latest_version)) <= (
        curr_vers['major'], curr_vers['minor'], curr_vers['patch']):
    if args.verbose or args.dry_run:
        print(f'Immich-Updater: Already at {curr_vers_str} or newer.')
    sys.exit(0)

# If there has been a minor version change, then need to check the release
# notes for a breaking changes.
# Do not do this for a patch update only, because the "breaking..." warning is
# repeated in patch updates if there was one in the minor update.
if int(latest_version[1]) != int(curr_vers['minor']):
    for line in release_data['body'].splitlines():
        # Dumb regex search for literaly "breaking change".
        # This has been a consistent pattern in the release notes for a while.
        if re.search('breaking change', line, re.IGNORECASE) is not None:
            # Create a breaking change flag file with the breaking version #
            if not args.dry_run:
                BCF.write_text(latest_version_str, encoding='utf-8')

            print('Immich-Updater: A breaking change has been detected when'
                  ' comparing the currently-installed version'
                  f' ({curr_vers_str}) to the latest release'
                  f' ({latest_version_str}). Will not proceed with the'
                  f" update.\n\nRemember to delete the '{BCF}' file,"
                  ' when updating manually.')
            sys.exit(0)

# One last check is the delay setting

# Grab the release publish date, and convert to a datetime object.
# Versions < 3.11 do not support TZ 'Z', so replace it with '+00:00'.
release_DT = datetime.fromisoformat(
    release_data['published_at'].replace('Z', '+00:00'))

# Has enough time elapsed?
if (datetime.now(timezone.utc) - release_DT).days < DELAY_DAYS:
    # No. Abort.
    if args.verbose or args.dry_run:
        print(f'Immich-Updater: Current {curr_vers_str}; latest '
              f'{latest_version_str}, published {release_data["published_at"]}. '
              f'Waiting until {(release_DT + timedelta(days=DELAY_DAYS)).isoformat()}.')
    sys.exit(0)

# If we made it this far, then there has been an update and no breaking
# changes have been detected. Ok to proceed with update.

if args.dry_run:
    print(f'Immich-Updater: Dry run: would update {curr_vers_str} to '
          f'{latest_version_str}, pin .env, and restart Docker Compose.')
    sys.exit(0)

# Refuse to start an update when its persistent version pin cannot be written.
env_path = Path(IMMICH_DIR, '.env')
if env_path.is_symlink() or not env_path.is_file():
    print('Immich-Updater: A regular .env file is required. Will not update.')
    sys.exit(1)

# Build a docker SH command
docker = sh.Command('docker')
docker = docker.bake(_cwd=IMMICH_DIR,
                     _env={**os.environ, 'IMMICH_VERSION': latest_version_str})

# pull
print(
    f'Immich-Updater: Updating from {curr_vers_str} to {latest_version_str}.')
try:
    out = docker('compose', 'pull')
except sh.ErrorReturnCode as e:
    err(e)

# Persist only after the selected images have been downloaded successfully.
# Keep the selected version if startup fails; automatic downgrades are unsafe.
persist_version(latest_version_str)

# reload
print('Immich-Updater: Reloading server.')
try:
    out = docker('compose', 'up', '-d')
except sh.ErrorReturnCode as e:
    err(e)

sys.exit(0)
