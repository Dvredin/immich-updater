"""Bounded original-file samples around a full, consistent database migration.

No source file is mounted into a clone. This is rehearsal coverage, not a production
checkpoint: the complete production rollback backend remains a separate gate.
"""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat

MIB = 1024 * 1024
MAX_FILES = 16
MAX_BYTES = 128 * MIB
MAX_FILE_BYTES = 64 * MIB
CANDIDATES = 64
STORAGE_DIRS = ('upload', 'library', 'thumbs', 'encoded-video', 'profile', 'backups')
PROFILE = 'sampled-media-v1'


def candidates(compose, username, database, snapshot):
    from rehearsal import RehearsalError
    if not re.fullmatch(r'[0-9A-Fa-f]{8}-[0-9A-Fa-f]{8}-[0-9]+', snapshot):
        raise RehearsalError('Invalid sample snapshot.')
    # Same account as the authenticated probe. Only metadata, no full-library walk.
    query = '''SELECT CAST(coalesce(json_agg(row_to_json(s)), CAST('[]' AS json)) AS text) FROM (
SELECT a.id, a."originalPath" AS path FROM public.asset a
WHERE a."deletedAt" IS NULL AND a."ownerId"=(SELECT id FROM public."user"
WHERE "deletedAt" IS NULL AND status='active' ORDER BY "isAdmin" DESC,"createdAt" LIMIT 1)
ORDER BY a."createdAt",a.id LIMIT ''' + str(CANDIDATES) + ') s;'
    query = "BEGIN ISOLATION LEVEL REPEATABLE READ; SET TRANSACTION SNAPSHOT '" + snapshot + "';\n" + query + '\nCOMMIT;'
    rows = json.loads(compose.sql(query, database, username))
    if not isinstance(rows, list) or len(rows) > CANDIDATES:
        raise RehearsalError('Invalid sampled asset metadata.')
    for row in rows:
        if not isinstance(row, dict) or not re.fullmatch(r'[0-9a-f-]{36}', row.get('id', '')) or not isinstance(row.get('path'), str):
            raise RehearsalError('Invalid sampled asset metadata.')
    return rows


def _safe_source(root, relative):
    from rehearsal import RehearsalError
    root = Path(root)
    relative = PurePosixPath(relative)
    if relative.is_absolute() or '..' in relative.parts or not relative.parts:
        raise RehearsalError('Sample path escapes its source mount.')
    path = root.joinpath(*relative.parts)
    if root.is_symlink() or root.resolve() != root.absolute() or not root.is_dir():
        raise RehearsalError('Sample source must be an ordinary directory.')
    if path.resolve() != path.absolute() or root.resolve() not in path.resolve().parents:
        raise RehearsalError('Sample path contains a symlink or escapes its mount.')
    return path


def _copy_file(source, destination, ceiling, *, confined_root=None):
    from rehearsal import RehearsalError
    # Open every source directory without following symlinks, then the leaf from
    # that anchored directory. A concurrent rename cannot redirect a photo read
    # into some other source tree between validation and open.
    handles = []
    try:
        if confined_root is not None:
            root = Path(confined_root)
            relative = source.relative_to(root)
            handles.append(os.open('/', os.O_RDONLY | os.O_DIRECTORY))
            for part in root.parts[1:]:
                handles.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=handles[-1]))
            for part in relative.parts[:-1]:
                handles.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=handles[-1]))
            fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=handles[-1])
        else:
            fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    finally:
        for handle in reversed(handles):os.close(handle)
    with os.fdopen(fd, 'rb') as src:
        before = os.fstat(src.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > ceiling:
            raise RehearsalError('Sample file exceeds the bounded copy policy.')
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        digest = hashlib.sha256(); size = 0
        outfd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(outfd, 'wb') as dst:
            while chunk := src.read(MIB):
                size += len(chunk)
                if size > ceiling:
                    raise RehearsalError('Sample grew beyond its bounded copy policy.')
                digest.update(chunk); dst.write(chunk)
            dst.flush(); os.fsync(dst.fileno())
        after = os.fstat(src.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise RehearsalError('Sample changed during copy; source unchanged.')
    return size, digest.hexdigest()


def sampled_mounts(config, sandbox, rows):
    """Create private empty mount roots, stock sentinels and a bounded file sample."""
    from rehearsal import RehearsalError, private_json
    sandbox = Path(sandbox)
    mapping = {}; targets = []
    for name, service in config['services'].items():
        if name == 'database':
            continue
        for mount in service.get('volumes', []):
            if mount.get('target') == '/etc/localtime':
                continue
            if mount.get('type') == 'bind':
                root = Path(mount['source'])
                if not root.is_dir() or root.is_symlink() or root.absolute() != root.resolve():
                    raise RehearsalError('Sample mount source is not an ordinary directory.')
                key = str(root.resolve())
            elif mount.get('type') == 'volume' and name == 'immich-machine-learning' and mount.get('target') == '/cache':
                key = 'volume:' + mount['source']; root = None
            else:
                raise RehearsalError('Unsupported sampled rehearsal mount.')
            if key not in mapping:
                dest = sandbox / 'files' / str(len(mapping))
                dest.mkdir(mode=0o700, parents=True)
                mapping[key] = dest
            if name == 'immich-server':
                target = PurePosixPath(mount['target'])
                if not target.is_absolute() or '..' in target.parts:
                    raise RehearsalError('Invalid container sample mount.')
                targets.append((target, root, mapping[key]))
                if str(target) == '/data':
                    # Sentinel files only; never recursively walk/copy the library.
                    for sub in ('', *STORAGE_DIRS):
                        (mapping[key] / sub).mkdir(mode=0o700, exist_ok=True)
                        relative = str(PurePosixPath(sub) / '.immich')
                        marker = _safe_source(root, relative)
                        if marker.exists():
                            _copy_file(marker, mapping[key] / relative, 4096, confined_root=root)
    if sum(str(item[0]) == '/data' for item in targets) != 1:
        raise RehearsalError('Sample profile requires the stock /data mount.')
    selected = []; copied = 0; skipped = 0; seen = set()
    for row in rows:
        if len(selected) == MAX_FILES:
            break
        path = PurePosixPath(row['path'])
        if not path.is_absolute() or '..' in path.parts:
            raise RehearsalError('Asset sample path is not confined.')
        matches = [item for item in targets if item[0] in path.parents]
        if not matches:
            raise RehearsalError('Asset sample has no private mapped mount.')
        target, root, destination = max(matches, key=lambda item: len(item[0].parts))
        relative = path.relative_to(target)
        source = _safe_source(root, relative)
        if source in seen:
            skipped += 1; continue
        if not source.is_file():
            skipped += 1; continue
        size = source.stat().st_size
        if size <= 0 or size > MAX_FILE_BYTES or copied + size > MAX_BYTES:
            skipped += 1; continue
        size, digest = _copy_file(source, destination / relative, min(MAX_FILE_BYTES, MAX_BYTES - copied), confined_root=root)
        copied += size; seen.add(source)
        selected.append({'id': row['id'], 'sha256': digest, 'bytes': size})
    if rows and not selected:
        raise RehearsalError('No readable original fits the sample policy; no source mutation.')
    manifest = {'profile': PROFILE, 'database_scope': 'full', 'media_scope': 'bounded_sample',
                'assets': selected, 'copied_files': len(selected), 'copied_bytes': copied, 'skipped_candidates': skipped,
                'max_files': MAX_FILES, 'max_bytes': MAX_BYTES, 'ml_cache_scope': 'empty',
                'production_checkpoint': False}
    private_json(sandbox / 'sample-manifest.json', manifest)
    return mapping, manifest


def capacity(compose, state_dir):
    """Budget database copies/recovery and fixed media allowance, never all photos."""
    from rehearsal import RehearsalError
    from resource_policy import ResourceUnavailable
    raw = compose.sql('SELECT pg_database_size(current_database());')
    if not isinstance(raw, str) or not re.fullmatch(r'[0-9]+', raw) or int(raw) <= 0:
        raise RehearsalError('Cannot verify database storage for sampled rehearsal.')
    database_bytes = int(raw)
    # Includes retained candidate/old rehearsal, dump, PG/WAL, clone checkpoint and
    # restoration staging. Actual available space is rechecked before each capture.
    required = 6 * database_bytes + 6 * MAX_BYTES + 512 * MIB
    free = shutil.disk_usage(state_dir).free
    if free < required:
        raise ResourceUnavailable('Sample rehearsal storage deferred: required ' + str(required)
                                  + ' bytes, available ' + str(free) + '; source unchanged.')
    return required
