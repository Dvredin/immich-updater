"""Full cold-checkpoint updates and crash recovery. All recovery state is private.

No image-only downgrades: a failed migration restores files + database + config.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import stat
import uuid
from pathlib import Path

import requests

from rehearsal import (Compose, RehearsalError, clone_private_tree, functional_checks,
                       install_local_probe_key, invariants, private_json, run)
from risk_checks import log, version

SERVICES = {'immich-server', 'immich-machine-learning', 'database', 'redis'}


def tree_hashes(root):
    result = {}
    for path in sorted(Path(root).rglob('*')):
        if path.is_file() and not path.is_symlink():
            digest = hashlib.sha256()
            with path.open('rb') as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
            result[str(path.relative_to(root))] = digest.hexdigest()
    return result


def sync_parent(path):
    fd = os.open(Path(path).parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_bytes(path, data, mode=0o600):
    path = Path(path)
    target = path.with_name('.' + path.name + '.new-' + uuid.uuid4().hex)
    with target.open('xb') as out:
        os.chmod(target, mode)
        out.write(data)
        out.flush()
        os.fsync(out.fileno())
    os.replace(target, path)
    sync_parent(path)


def state_save(path, state):
    atomic_bytes(path, json.dumps(state, sort_keys=True).encode())


def private_root(path):
    path = Path(path)
    if path.is_symlink() or path.absolute() != path.resolve():
        raise RehearsalError('State root and its parents must not be symbolic links.')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_mode & 0o077:
        raise RehearsalError('State root must be mode 0700.')
    return path.resolve()


def pinned(config):
    result = copy.deepcopy(config)
    for name, service in result['services'].items():
        data = json.loads(run(['docker', 'image', 'inspect', service['image']]))[0]
        # Content-addressed local image ID; never re-pull mutable tags during recovery.
        service['image'] = data['Id']
        service['pull_policy'] = 'never'
    return result


def running_pinned(stack):
    """Checkpoint the images actually used by old containers, never moved tags."""
    config = stack.config()
    ids = stack.call('ps', '-aq').decode().split()
    if not ids:
        raise RehearsalError('No old runtime containers for checkpoint provenance.')
    items = json.loads(run(['docker', 'inspect', *ids]))
    found = {}
    for item in items:
        labels = item.get('Config', {}).get('Labels') or {}
        if labels.get('com.docker.compose.project') != config['name']:
            raise RehearsalError('Old container belongs to another project.')
        name = labels.get('com.docker.compose.service')
        if name not in SERVICES or name in found:
            raise RehearsalError('Old runtime has extra/duplicate services.')
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', item.get('Image', '')):
            raise RehearsalError('Old runtime has invalid content-addressed image identity.')
        found[name] = item['Image']
    if set(found) != SERVICES:
        raise RehearsalError('Every stock old service must have a concrete container.')
    result = copy.deepcopy(config)
    for name, image in found.items():
        result['services'][name]['image'] = image
        result['services'][name]['pull_policy'] = 'never'
    return result


def candidate_config(source_path, selected, state_dir):
    """Use exact official release Compose while retaining this site's stock mappings.

    Fetching/pulling happens before a checkpoint. The application itself is offline
    during the cold checkpoint and never has access to the rehearsal's network.
    """
    version(selected)
    source = Compose(source_path)
    original = source.config()
    if set(original.get('services', {})) != SERVICES:
        raise RehearsalError('Unsupported production service layout.')
    for name, item in original['services'].items():
        if any(item.get(key) for key in ('privileged', 'devices', 'network_mode', 'pid', 'ipc', 'cap_add', 'entrypoint')):
            raise RehearsalError('Source hardware/host/custom-entrypoint features need a verified backend.')
        if name == 'immich-machine-learning' and re.search(r'-(cuda|rocm|openvino|armnn|rknn)(?:@|$)', item.get('image','')):
            raise RehearsalError('Accelerated source cannot be silently changed to CPU.')
    requested_state = Path(state_dir).absolute()
    for root in mounted_roots(original):
        if root == requested_state or root in requested_state.parents or requested_state in root.parents:
            raise RehearsalError('Candidate state storage overlaps source data; no captured settings may be written.')
    state_dir = private_root(state_dir)
    rehearsal_capacity(original, state_dir)
    work = state_dir / ('candidate-' + uuid.uuid4().hex)
    work.mkdir(mode=0o700)
    response = requests.get('https://raw.githubusercontent.com/immich-app/immich/' + selected + '/docker/docker-compose.yml', timeout=30)
    response.raise_for_status()
    if not response.content or len(response.content) > 262144:
        raise RehearsalError('Invalid official Compose template size.')
    template = work / 'official.yml'
    atomic_bytes(template, response.content)
    db_env = original['services']['database'].get('environment', {})
    server_mounts = {m['target']: m for m in original['services']['immich-server'].get('volumes', [])}
    db_mounts = {m['target']: m for m in original['services']['database'].get('volumes', [])}
    if '/data' not in server_mounts or '/var/lib/postgresql/data' not in db_mounts:
        raise RehearsalError('Unsupported stock data layout.')
    if any('\n' in str(v) or '\r' in str(v) for v in db_env.values()):
        raise RehearsalError('Multiline database settings are unsupported.')
    # Template interpolation is contained in private files; values never reach logs.
    values = {'IMMICH_VERSION': selected, 'UPLOAD_LOCATION': server_mounts['/data']['source'],
              'DB_DATA_LOCATION': db_mounts['/var/lib/postgresql/data']['source'],
              'DB_PASSWORD': db_env.get('POSTGRES_PASSWORD', ''),
              'DB_USERNAME': db_env.get('POSTGRES_USER', 'postgres'),
              'DB_DATABASE_NAME': db_env.get('POSTGRES_DB', 'immich')}
    atomic_bytes(work / '.env', ('\n'.join(k + '=' + json.dumps(str(v)) for k, v in values.items()) + '\n').encode())
    resolved = Compose(template).config()
    if set(resolved.get('services', {})) != SERVICES:
        raise RehearsalError('New official topology requires an unsupported adapter; production unchanged.')
    resolved['name'] = original['name']
    for name, service in resolved['services'].items():
        old = original['services'][name]
        if 'container_name' in old:
            service['container_name'] = old['container_name']
        else:
            service.pop('container_name', None)
        # Keep official image, command and health checks, but retain site configuration.
        service['environment'] = copy.deepcopy(old.get('environment', {}))
        service['volumes'] = copy.deepcopy(old.get('volumes', []))
        for field in ('ports', 'restart', 'networks', 'user', 'mem_limit', 'cpus', 'shm_size'):
            if field in old:
                service[field] = copy.deepcopy(old[field])
        if name.startswith('immich-'):
            wanted = 'ghcr.io/immich-app/' + name + ':' + selected
            if service.get('image') != wanted:
                raise RehearsalError('Official template is not bound to the selected stable images.')
        if name == 'database':
            image = service.get('image', '')
            if not (image.startswith('ghcr.io/immich-app/postgres:') or image.startswith('tensorchord/') or image.startswith('postgres:')):
                raise RehearsalError('Unexpected upstream PostgreSQL image.')
    for field in ('volumes', 'networks'):
        if field in original:
            resolved[field] = copy.deepcopy(original[field])
    path = work / 'candidate.json'
    private_json(path, resolved)
    # Pull before stopping any source service. Only the explicit chosen images.
    images = {service['image'] for service in resolved['services'].values()}
    for image in sorted(images):
        run(['docker', 'pull', image], timeout=1800)
    resolved = pinned(resolved)
    rehearsal_capacity(original, state_dir)  # image pulls may have consumed free space
    private_json(path, resolved)
    return path


def mounted_roots(config):
    roots = set()
    for name, service in config['services'].items():
        for mount in service.get('volumes', []):
            if mount.get('read_only') or mount.get('target') == '/etc/localtime':
                continue
            if mount['type'] == 'bind':
                path = Path(mount['source'])
            elif mount['type'] == 'volume':
                logical = mount['source']
                actual = config.get('volumes', {}).get(logical, {}).get('name')
                if not actual:
                    raise RehearsalError('Named volume lacks resolved Docker identity.')
                item = json.loads(run(['docker', 'volume', 'inspect', actual]))[0]
                if item.get('Driver') != 'local' or item.get('Options'):
                    raise RehearsalError('Only plain local named volumes can be fully checkpointed.')
                path = Path(item['Mountpoint'])
            else:
                raise RehearsalError('Unsupported mutable mount.')
            if not path.is_dir() or path.is_symlink() or path.resolve() != path.absolute():
                raise RehearsalError('Mutable mounts must be existing ordinary directories with no symlink parents.')
            if os.path.ismount(path):
                raise RehearsalError('A mutable mountpoint cannot be atomically directory-restored; use a snapshot backend.')
            if str(path) in {'/', '/home', '/opt', '/var', '/var/lib', '/etc', '/usr'}:
                raise RehearsalError('Refusing a broad/system mount root.')
            roots.add(path.resolve())
    for a in roots:
        if any(a in b.parents for b in roots if a != b):
            raise RehearsalError('Overlapping mutable mounts cannot be restored independently.')
    return sorted(roots)


def rehearsal_capacity(config, state_dir):
    """Budget retained rehearsal, drill, checkpoint and recovery before any capture."""
    roots = mounted_roots(config)
    for service in config['services'].values():
        for mount in service.get('volumes', []):
            if mount.get('read_only') and mount.get('type') == 'bind' and mount.get('target') != '/etc/localtime':
                path = Path(mount['source']).resolve()
                if not path.is_dir():
                    raise RehearsalError('Only directory external libraries can be privately cloned.')
                if path not in roots:
                    roots.append(path)
    total = sum(int(run(['du', '-sx', '--block-size=1', str(root)], timeout=300).decode().split()[0]) for root in roots)
    required = 6 * total + 512 * 1024 * 1024
    if shutil.disk_usage(state_dir).free < required:
        raise RehearsalError('Insufficient space for isolated rehearsals and full checkpoint/recovery; source unchanged.')
    return required


def prove_stopped(stack):
    ids = stack.call('ps', '-aq').decode().split()
    if ids:
        items = json.loads(run(['docker', 'inspect', *ids]))
        if any(item['State']['Running'] for item in items):
            raise RehearsalError('Stack did not fully stop; refusing filesystem mutation.')


def stop(stack):
    stack.call('stop', '--timeout', '60', timeout=180)
    prove_stopped(stack)


def assert_exclusive(roots, project):
    ids = run(['docker', 'ps', '-q']).decode().split()
    if not ids:
        return
    items = json.loads(run(['docker', 'inspect', *ids]))
    for item in items:
        if item.get('Config', {}).get('Labels', {}).get('com.docker.compose.project') == project:
            continue
        for mount in item.get('Mounts', []):
            source = Path(mount['Source']).resolve()
            if any(source == root or source in root.parents or root in source.parents for root in roots):
                raise RehearsalError('Another running container shares mutable production state.')


def runtime_checks(stack, selected, baseline):
    env = stack.config()['services']['database'].get('environment', {})
    username, database = env.get('POSTGRES_USER', 'postgres'), env.get('POSTGRES_DB', 'immich')
    if invariants(stack, username, database) != baseline:
        raise RehearsalError('Post-start metadata changed unexpectedly.')
    token = install_local_probe_key(stack, username, database)
    try:
        return functional_checks(stack, selected, token, Path(stack.path).parent)
    finally:
        import hashlib
        digest = hashlib.sha256(token.encode()).hexdigest()
        stack.sql("DELETE FROM public.api_key WHERE key=decode('" + digest + "','hex');", database, username)


def checkpoint(stack, source_path, state_dir, baseline, installed):
    source_path = Path(source_path).resolve()
    state_dir = private_root(state_dir)
    cfg = running_pinned(stack)
    roots = mounted_roots(cfg)
    if not roots or any(state_dir == root or root in state_dir.parents for root in roots):
        raise RehearsalError('Checkpoint location overlaps source data.')
    # No backup should silently include an entire unrelated application folder.
    if any(source_path == root or root in source_path.parents for root in roots):
        raise RehearsalError('Compose config must be outside mutable data mounts.')
    assert_exclusive(roots, cfg['name'])
    # Allow checkpoint plus recovery staging without deleting old or failed data.
    total = sum(int(run(['du', '-sx', '--block-size=1', str(root)], timeout=300).decode().split()[0]) for root in roots)
    if shutil.disk_usage(state_dir).free < 2 * total + 512 * 1024 * 1024:
        raise RehearsalError('Insufficient space for full checkpoint and non-destructive recovery staging.')
    dest = state_dir / ('checkpoint-' + uuid.uuid4().hex)
    dest.mkdir(mode=0o700)
    private_json(dest / 'old-compose.json', cfg)
    configurations = []
    for path in (source_path, source_path.parent / '.env'):
        if path.is_symlink() or not path.is_file():
            raise RehearsalError('Regular compose and .env files required.')
        saved = dest / ('config-' + str(len(configurations)))
        atomic_bytes(saved, path.read_bytes())
        meta = path.stat()
        configurations.append({'path': str(path), 'saved': str(saved), 'mode': stat.S_IMODE(meta.st_mode),
                               'uid': meta.st_uid, 'gid': meta.st_gid})
    state = {'phase': 'stopping', 'installed': installed, 'source': str(source_path),
             'checkpoint': str(dest), 'old_compose': str(dest / 'old-compose.json'),
             'baseline': baseline, 'configurations': configurations, 'roots': [],
             'project': cfg['name']}
    journal = state_dir / 'transaction.json'
    state_save(journal, state)
    # Freeze all application writers first, then capture metadata from the still-live DB.
    stack.call('stop', '--timeout', '60', 'immich-server', timeout=120)
    server_ids = stack.call('ps', '-q', 'immich-server').decode().split()
    if server_ids:
        raise RehearsalError('Application writer failed to stop; no data capture or mutation allowed.')
    db_env = cfg['services']['database'].get('environment', {})
    state['baseline'] = invariants(stack, db_env.get('POSTGRES_USER', 'postgres'), db_env.get('POSTGRES_DB', 'immich'))
    state_save(journal, state)
    stop(stack)
    state['phase'] = 'capturing'
    state_save(journal, state)
    for i, root in enumerate(roots):
        archive = dest / ('mount-' + str(i))
        clone_private_tree(root, archive)
        state['roots'].append({'live': str(root), 'archive': str(archive), 'status': 'captured'})
        state_save(journal, state)
    run(['sync', '-f', str(dest)], timeout=120)
    state['phase'] = 'prepared'
    state_save(journal, state)
    return state


def restore(state_dir):
    """Idempotent crash recovery under the caller's application lock."""
    state_dir = private_root(state_dir)
    journal = state_dir / 'transaction.json'
    if not journal.exists():
        return None
    state = json.loads(journal.read_text())
    if state['phase'] == 'committed':
        # Candidate was verified with all ingress ports CLOSED. Once committed,
        # activating it may admit new writes: retry activation, never erase them.
        active = Compose(state['source'], state['project'])
        active.call('up', '-d', '--wait', '--wait-timeout', '240', timeout=300)
        answer = active.api('/api/server/version')
        wanted = tuple(int(x) for x in state['target'][1:].split('.'))
        data = answer.get('data') or {}
        if answer['status'] != 200 or tuple(data.get(k) for k in ('major', 'minor', 'patch')) != wanted:
            raise RehearsalError('Committed candidate activation pending; retain journal and data.')
        atomic_bytes(Path(state['checkpoint']) / 'update-receipt.json', json.dumps({
            'stage': 'updated', 'target': state['target'], 'activation_recovered': True,
            'consistent_full_checkpoint': True}).encode())
        journal.unlink()
        sync_parent(journal)
        return 'committed'
    if state['phase'] == 'rolled_back':
        old = Compose(state['old_compose'], state['project'])
        old.call('up', '-d', '--wait', '--wait-timeout', '240', timeout=300)
        answer = old.api('/api/server/version')
        wanted = tuple(int(x) for x in state['installed'][1:].split('.'))
        data = answer.get('data') or {}
        if answer['status'] != 200 or tuple(data.get(k) for k in ('major', 'minor', 'patch')) != wanted:
            raise RehearsalError('Restored old stack activation pending; no repeated file restoration.')
        if state.get('target'):
            qp = state_dir / 'quarantine.json'
            q = json.loads(qp.read_text()) if qp.exists() else {}
            q[state['target']] = {'installed': state['installed'], 'error_type': 'InterruptedOrFailedTransaction'}
            state_save(qp, q)
        atomic_bytes(Path(state['checkpoint']) / 'recovery-receipt.json', json.dumps({
            'stage': 'restored', 'installed': state['installed'], 'full_mutable_state': True,
            'health_and_metadata_verified': True, 'file_bytes_verified_before_start': state.get('file_bytes_verified', False)}).encode())
        journal.unlink()
        sync_parent(journal)
        return 'restored'
    unchanged = state['phase'] in {'stopping', 'capturing', 'prepared'}
    old = Compose(state['old_compose'], state['project'])
    # Use source project's labels even if an interrupted configuration write occurred.
    stop(old)
    if state['phase'] == 'checking_restore':
        # Failed/interrupted old startup may mutate DB/files while ingress was
        # still closed. A fresh attempt must restore the immutable checkpoint.
        state['restore_attempt'] = uuid.uuid4().hex
        for item in state['roots']:
            item['status'] = 'captured'
        state['phase'] = 'restoring'
        state_save(journal, state)
    if state['phase'] not in {'stopping', 'capturing', 'prepared'}:
        state['phase'] = 'restoring'
        state_save(journal, state)
        for item in state['roots']:
            live, archive = Path(item['live']), Path(item['archive'])
            suffix = Path(state['checkpoint']).name + ('-' + state['restore_attempt'] if state.get('restore_attempt') else '')
            staged = live.with_name(live.name + '.restore-' + suffix)
            failed = live.with_name(live.name + '.failed-' + suffix)
            if item.get('status') == 'restored':
                continue
            if not failed.exists():
                if staged.exists() and tree_hashes(staged) != tree_hashes(archive):
                    os.rename(staged, staged.with_name(staged.name + '.incomplete-' + uuid.uuid4().hex))
                if not staged.exists():
                    clone_private_tree(archive, staged)
                    run(['sync', '-f', str(staged)], timeout=120)
                if tree_hashes(staged) != tree_hashes(archive):
                    raise RehearsalError('Staged restore is incomplete; leave original stopped state untouched.')
            if not failed.exists():
                os.rename(live, failed)
                sync_parent(live)
            if not live.exists():
                if not staged.exists():
                    raise RehearsalError('Restore payload missing; remain stopped.')
                os.rename(staged, live)
                sync_parent(live)
            item['status'] = 'restored'
            state_save(journal, state)
        for item in state['roots']:
            if tree_hashes(item['live']) != tree_hashes(item['archive']):
                raise RehearsalError('Restored file bytes differ from the cold checkpoint; remain stopped.')
        for item in state['configurations']:
            path = Path(item['path'])
            atomic_bytes(path, Path(item['saved']).read_bytes(), item['mode'])
            os.chown(path, item['uid'], item['gid'])
    # Validate restored old version with ingress CLOSED as well. Reopening it is
    # an irreversible admission boundary for new client writes, not another rollback.
    old_cfg = old.config()
    for service in old_cfg['services'].values():
        service.pop('ports', None)
    probation_path = Path(state['checkpoint']) / 'restore-probation.json'
    private_json(probation_path, old_cfg)
    probation = Compose(probation_path, state['project'])
    if not unchanged:
        state['phase'] = 'checking_restore'
        state_save(journal, state)
    try:
        probation.call('up', '-d', '--force-recreate', '--wait', '--wait-timeout', '240', timeout=300)
        if unchanged:
            env = probation.config()['services']['database'].get('environment', {})
            state['baseline'] = invariants(probation, env.get('POSTGRES_USER', 'postgres'), env.get('POSTGRES_DB', 'immich'))
        runtime_checks(probation, state['installed'], state['baseline'])
    except BaseException:
        stop(probation)  # never leave failed rollback verification running
        raise
    state['phase'] = 'rolled_back'
    state['file_bytes_verified'] = not unchanged
    state_save(journal, state)
    # Complete activation through the durable post-restore branch, never copy old
    # bytes over a restored stack after it may already have accepted client writes.
    return restore(state_dir)


def apply(source_path, candidate_path, selected, installed, state_dir, *, inject_failure=False, failure_hook=None):
    """Caller must own application lock and have passed rehearsal + restore drill."""
    source = Compose(source_path)
    cfg = source.config()
    env = cfg['services']['database'].get('environment', {})
    username, database = env.get('POSTGRES_USER', 'postgres'), env.get('POSTGRES_DB', 'immich')
    baseline = invariants(source, username, database)
    candidate = json.loads(Path(candidate_path).read_text())
    if candidate['name'] != cfg['name'] or mounted_roots(candidate) != mounted_roots(cfg):
        raise RehearsalError('Candidate must preserve exact source project and mutable mounts.')
    state_dir = private_root(state_dir)
    if (state_dir / 'transaction.json').exists():
        raise RehearsalError('Unfinished transaction must be recovered before another update.')
    try:
        state = checkpoint(source, source_path, state_dir, baseline, installed)
        baseline = state['baseline']
        state['phase'] = 'upgrading'
        state['target'] = selected
        state_save(state_dir / 'transaction.json', state)
        probation = copy.deepcopy(candidate)
        for service in probation['services'].values():
            service.pop('ports', None)
        atomic_bytes(source_path, json.dumps(probation).encode())
        active = Compose(source_path, cfg['name'])
        active.call('up', '-d', '--wait', '--wait-timeout', '240', timeout=300)
        tests = runtime_checks(active, selected, baseline)
        if inject_failure:
            if failure_hook:
                failure_hook(active)
            # Explicit lab fault: migration and file mutation happened BEFORE rollback.
            raise RehearsalError('Injected test-only post-migration failure.')
        from immich_updater import persist_version
        persist_version(Path(source_path).parent, selected)
        # Commit before exposing validated source ports. After this boundary, any
        # accepted user writes belong to the new state and MUST NOT be rolled back.
        atomic_bytes(source_path, json.dumps(candidate).encode())
        state['phase'] = 'committed'
        state_save(state_dir / 'transaction.json', state)
        active.call('up', '-d', '--wait', '--wait-timeout', '240', timeout=300)
        answer = active.api('/api/server/version')
        data = answer.get('data') or {}
        if answer['status'] != 200 or tuple(data.get(k) for k in ('major', 'minor', 'patch')) != tuple(int(x) for x in selected[1:].split('.')):
            raise RehearsalError('Committed candidate activation pending; recovery will retry without losing writes.')
        atomic_bytes(Path(state['checkpoint']) / 'update-receipt.json', json.dumps({
            'stage': 'updated', 'target': selected, 'tests': tests,
            'consistent_full_checkpoint': True}).encode())
        (state_dir / 'transaction.json').unlink()
        sync_parent(state_dir / 'transaction.json')
        log('execution', stage='complete', target=selected, consistent_full_checkpoint=True)
        return tests
    except BaseException:
        # Including SIGTERM translated by CLI and Ctrl-C. SIGKILL resumes from journal.
        restore(state_dir)
        raise
