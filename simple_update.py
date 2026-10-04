"""Single-stack updates: logical DB/config backup; no rehearsal or automatic downgrade.

After target startup may have migrated the DB, failed/interrupted updates require
owner intervention. Never restore an old DB over newly admitted user uploads.
"""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import stat
import uuid
from pathlib import Path

from rehearsal import Compose, RehearsalError, run
from transaction import atomic_bytes, private_root, running_pinned, state_save
from risk_checks import log, version

PROFILE = 'single-stack-v1'
JOURNAL = 'simple-update.json'
APPLICATION_MARKER = '.immich-updater-interrupted.json'
MIB = 1024 * 1024


class NeedsAttention(RehearsalError):
    pass


def location(config, state_dir):
    """Contain only our private metadata, never a source/external data directory."""
    state = Path(state_dir).absolute()
    if state != state.resolve():
        raise RehearsalError('Private state path must not traverse symlinks.')
    for service in config['services'].values():
        for mount in service.get('volumes', []):
            if mount.get('target') == '/etc/localtime':
                continue
            if mount.get('type') == 'bind':
                source = Path(mount['source']).resolve()
            elif mount.get('type') == 'volume':
                actual = config.get('volumes', {}).get(mount['source'], {}).get('name')
                if not actual:
                    raise RehearsalError('Named volume identity is unresolved.')
                item = json.loads(run(['docker', 'volume', 'inspect', actual], timeout=30))[0]
                source = Path(item['Mountpoint']).resolve()
            else:
                continue
            if source == state or source in state.parents or state in source.parents:
                raise RehearsalError('Backup state overlaps application data; source unchanged.')
    return state


def db_identity(config):
    env = config['services']['database'].get('environment', {})
    server = config['services']['immich-server'].get('environment', {})
    # Stock defaults are documented upstream; URLs/file-based settings override
    # them and are unsupported here rather than silently backing up another DB.
    if any(k.startswith('DB_') and k.endswith('_FILE') for k in server) or 'DB_URL' in server:
        raise RehearsalError('URL/file-based database settings require an unsupported backup adapter.')
    if any(k.startswith('POSTGRES_') and k.endswith('_FILE') for k in env):
        raise RehearsalError('File-based PostgreSQL settings are unsupported.')
    user, database = env.get('POSTGRES_USER'), env.get('POSTGRES_DB')
    if not all(isinstance(value,str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', value) for value in (user, database)):
        raise RehearsalError('Unsupported database identifier.')
    if (server.get('DB_HOSTNAME','database') != 'database' or str(server.get('DB_PORT','5432')) != '5432'
        or server.get('DB_USERNAME','postgres') != user or server.get('DB_DATABASE_NAME','immich') != database
        or server.get('DB_PASSWORD') != env.get('POSTGRES_PASSWORD')):
        raise RehearsalError('Server database settings do not match the stock database being backed up.')
    return user, database


def pending_paths(source_path, state_dir):
    app = Path(source_path).parent
    return (app/APPLICATION_MARKER, Path(state_dir)/JOURNAL,
            app/'.immich-updater-state'/JOURNAL, Path(state_dir)/'transaction.json',
            app/'.immich-updater-state'/'transaction.json', app/'UPDATE_FAILED')


def check_pending(source_path, state_dir):
    if any(os.path.lexists(path) for path in pending_paths(source_path,state_dir)):
        raise NeedsAttention('Interrupted update requires owner inspection; changing state directory cannot bypass it.')


def require_upgrade(observed, installed, selected):
    if observed != installed or version(selected) <= version(observed):
        raise RehearsalError('Source-bound installed version changed or target is not newer; no downgrade allowed.')


def backup_capacity(stack, state_dir):
    config = stack.config()
    location(config, state_dir)
    user, database = db_identity(config)
    size = stack.sql('SELECT pg_database_size(current_database());', database, user)
    if not re.fullmatch(r'[1-9][0-9]*', size):
        raise RehearsalError('Cannot establish DB backup capacity.')
    # Full logical DB only; never du/walk/copy the photo or ML-cache trees.
    required = 2 * int(size) + 64 * MIB
    path = Path(state_dir)
    while not path.exists():
        path = path.parent
    available = shutil.disk_usage(path).free
    if available < required:
        raise RehearsalError('Insufficient space for fresh database/config backup; source unchanged.')
    return {'profile': PROFILE, 'database_size_bytes': int(size),
            'required_backup_free_bytes': required, 'available_bytes': available,
            'photo_copy_required': False, 'parallel_rehearsal': False}


def healthy(stack, selected=None, expected_images=None):
    config = stack.config()
    ids = stack.call('ps', '-aq').decode().split()
    if not ids:
        raise RehearsalError('No application containers.')
    found = {}
    for item in json.loads(run(['docker', 'inspect', *ids], timeout=30)):
        labels = item.get('Config', {}).get('Labels') or {}
        name = labels.get('com.docker.compose.service')
        if labels.get('com.docker.compose.project') != config['name'] or name in found:
            raise RehearsalError('Application runtime identity is ambiguous.')
        state = item.get('State', {})
        if not state.get('Running') or state.get('OOMKilled') or (state.get('Health') or {}).get('Status') not in {None, 'healthy'}:
            raise RehearsalError('An application service is not running/healthy.')
        if expected_images and item.get('Image') != expected_images.get(name):
            raise RehearsalError('Started image differs from the selected pinned image.')
        found[name] = item
    if set(found) != set(config['services']):
        raise RehearsalError('Missing or extra application service.')
    expected_db = db_identity(config)
    runtime = copy.deepcopy(config)
    for name in ('immich-server','database'):
        values = found[name].get('Config',{}).get('Env')
        if not isinstance(values,list) or any(not isinstance(v,str) or '=' not in v for v in values):
            raise RehearsalError('Actual database runtime settings are unavailable.')
        runtime['services'][name]['environment'] = dict(v.split('=',1) for v in values)
    if db_identity(runtime) != expected_db:
        raise RehearsalError('Running database identity differs from resolved configuration.')
    answer = stack.api('/api/server/version')
    data = answer.get('data') or {}
    fields = tuple(data.get(k) for k in ('major', 'minor', 'patch'))
    if answer.get('status') != 200 or any(type(x) is not int or x < 0 for x in fields):
        raise RehearsalError('Application version API is unavailable.')
    current = 'v' + '.'.join(map(str, fields))
    if selected and current != selected:
        raise RehearsalError('Application version differs from the selected release.')
    ping = stack.api('/api/server/ping')
    if ping.get('status') != 200 or (ping.get('data') or {}).get('res') != 'pong':
        raise RehearsalError('Application ping API failed.')
    return {'version': current, 'services_running': True, 'ping': True}


def preflight(source_path, state_dir):
    check_pending(source_path,state_dir)
    stack = Compose(source_path)
    receipt = backup_capacity(stack, state_dir)
    receipt['runtime'] = healthy(stack)
    receipt['source_mutations'] = False
    return receipt


def dump_database(stack, destination):
    user, database = db_identity(stack.config())
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as out:
        stack.call('exec', '-T', 'database', 'pg_dump', '-U', user, '-d', database,
                   '--format=custom', '--no-owner', '--no-acl', stdout=out, timeout=1800)
        out.flush(); os.fsync(out.fileno())
    # This stream is also passed as a subprocess FD. Buffered prefix reads can leave
    # its OS offset ahead of logical tell()/seek() and corrupt pg_restore input.
    with Path(destination).open('rb', buffering=0) as archive:
        if archive.read(5) != b'PGDMP':
            raise RehearsalError('Fresh database backup is not a custom-format archive.')
        archive.seek(0)
        stack.call('exec', '-T', 'database', 'pg_restore', '--list', stdin=archive, timeout=120)


def apply(source_path, candidate_path, selected, installed, state_dir, *, failure_hook=None):
    """One production stack. Caller owns its operation lock. No automatic DB restore."""
    version(selected); version(installed)
    source_path, candidate_path = Path(source_path), Path(candidate_path)
    source = Compose(source_path)
    old_config = source.config()
    root = private_root(location(old_config, state_dir))
    checked = preflight(source_path, root)
    require_upgrade(checked['runtime']['version'], installed, selected)
    candidate = json.loads(candidate_path.read_text())
    if db_identity(candidate) != db_identity(old_config):
        raise RehearsalError('Candidate changed the database being backed up.')
    if candidate.get('name') != old_config.get('name') or set(candidate['services']) != set(old_config['services']):
        raise RehearsalError('Candidate project/service identity changed.')
    for name, service in candidate['services'].items():
        if service.get('volumes', []) != old_config['services'][name].get('volumes', []):
            raise RehearsalError('Candidate data mounts changed.')
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', service.get('image', '')):
            raise RehearsalError('Candidate images must be immutable local IDs.')
    old_pinned = running_pinned(source)
    backup = root / ('db-backup-' + uuid.uuid4().hex)
    backup.mkdir(mode=0o700)
    atomic_bytes(backup/'old-compose.json', json.dumps(old_pinned).encode())
    saved = []
    for path in (source_path, source_path.parent/'.env'):
        if path.is_symlink() or not path.is_file() or path.absolute()!=path.resolve():
            raise RehearsalError('Regular nonsymlink configuration files required.')
        destination = backup/('config-' + str(len(saved)))
        atomic_bytes(destination, path.read_bytes())
        meta = path.stat()
        saved.append({'path':str(path),'saved':str(destination),'mode':stat.S_IMODE(meta.st_mode),
                      'uid':meta.st_uid,'gid':meta.st_gid})
    state = {'profile':PROFILE,'phase':'stopping_writer','source':str(source_path),
             'project':old_config['name'],'installed':installed,'target':selected,
             'backup':str(backup),'configurations':saved,'database_scope':'full',
             'media_backup':False,'automatic_downgrade':False,'mutation_started':False}
    journal = root/JOURNAL
    marker = source_path.parent/APPLICATION_MARKER
    # Recheck the actual source after pulls/config preparation, directly before stop.
    require_upgrade(healthy(source)['version'], installed, selected)
    state_save(journal, state)
    state_save(marker, {'profile':PROFILE,'journal':str(journal),'state_dir':str(root)})
    def clear_pending():
        from transaction import sync_parent
        for path in (journal,marker):
            path.unlink();sync_parent(path)
    stopped = False
    try:
        source.call('stop', '--timeout', '60', 'immich-server', timeout=120)
        if source.call('ps', '--status', 'running', '-q', 'immich-server').strip():
            raise RehearsalError('Application writer did not stop; no backup/upgrade allowed.')
        stopped = True
        state['phase']='backing_up'; state_save(journal,state)
        dump_database(source, backup/'database.dump')
        state['backup_verified']=True
        state['phase']='upgrading'
        # Durable boundary precedes any target configuration/start that may migrate DB.
        state['mutation_started']=True; state_save(journal,state)
        meta=source_path.stat()
        atomic_bytes(source_path,json.dumps(candidate).encode(),stat.S_IMODE(meta.st_mode))
        os.chown(source_path,meta.st_uid,meta.st_gid)
        active = Compose(source_path,old_config['name'])
        active.call('up','-d','--wait','--wait-timeout','900',timeout=1200)
        if failure_hook:
            failure_hook(active)
        tests=healthy(active,selected,{name:s['image'] for name,s in candidate['services'].items()})
        from immich_updater import persist_version
        persist_version(source_path.parent, selected)
        state['phase']='updated';state['checks']=tests
        state_save(backup/'update-receipt.json',state)
        clear_pending()
        log('execution',stage='complete',profile=PROFILE,target=selected,
            database_backup_verified=True,media_backup=False,parallel_rehearsal=False)
        return state
    except BaseException as exc:
        state['error_type']=type(exc).__name__
        if state['mutation_started']:
            state['phase']='needs_attention'
            state_save(journal,state)
            log('execution',stage='needs_attention',target=selected,backup=str(backup),
                automatic_retry=False,automatic_downgrade=False)
        elif stopped:
            # Read-only dump failed before target startup: resume only the unchanged old stack.
            try:
                Compose(backup/'old-compose.json',old_config['name']).call('up','-d','--wait','--wait-timeout','240',timeout=300)
                healthy(Compose(source_path),installed)
                state['phase']='aborted_before_update';state_save(backup/'update-receipt.json',state)
                clear_pending()
            except BaseException:
                state['phase']='needs_attention';state_save(journal,state)
        else:
            state['phase']='needs_attention';state_save(journal,state)
        raise
