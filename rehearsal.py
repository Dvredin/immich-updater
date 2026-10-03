#!/usr/bin/env python3
"""Isolated stock-Immich upgrade rehearsal. Never starts/stops production."""
from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import json
import os
import re
import secrets
import select
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from risk_checks import log, version

NODE_HTTP = r"""
let s='';for await(const b of process.stdin)s+=b;
const q=JSON.parse(s), headers=q.headers||{};
let body;
if(q.file){const fs=await import('node:fs/promises');const bytes=await fs.readFile(q.file);
const form=new FormData();form.append('assetData',new Blob([bytes]),q.filename||'rehearsal.jpg');
for(const [k,v] of Object.entries(q.fields||{}))form.append(k,v);body=form;
}else if(q.body!==undefined){headers['content-type']='application/json';body=JSON.stringify(q.body);}
const r=await fetch('http://127.0.0.1:2283'+q.path,{method:q.method||'GET',headers,body});
const c=await import('node:crypto'), hash=c.createHash('sha256');
let length=0, chunks=[];
if(r.body){for await(const chunk of r.body){length+=chunk.length;hash.update(chunk);if(length<=1048576)chunks.push(chunk);else chunks=[];}}
let data;if(length<=1048576){try{data=JSON.parse(Buffer.concat(chunks).toString())}catch{}}
process.stdout.write(JSON.stringify({status:r.status,data,bytes:length,sha256:hash.digest('hex')}));
"""


class RehearsalError(RuntimeError):
    pass


def private_json(path: Path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump(value, out, indent=2)
        out.flush()
        os.fsync(out.fileno())


def run(command, *, payload=None, timeout=60, stdout=None, stdin=None):
    result = subprocess.run(command, input=payload, stdin=stdin, stdout=stdout or subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=timeout)
    if result.returncode:
        # Docker/config errors may include environment values: do not print stderr.
        raise RehearsalError(f'Command failed (exit {result.returncode}); private execution state retained.')
    return result.stdout or b''


class Compose:
    def __init__(self, path, project=None):
        self.path = Path(path).resolve()
        self.base = ['docker', 'compose', '--project-directory', str(self.path.parent), '-f', str(self.path)]
        if project:
            self.base += ['-p', project]

    def call(self, *arguments, **kwargs):
        return run(self.base + list(arguments), **kwargs)

    def config(self):
        return json.loads(self.call('config', '--format', 'json'))

    def sql(self, query, database=None, username=None):
        if database is None or username is None:
            env = self.config()['services']['database'].get('environment', {})
            database = database or env.get('POSTGRES_DB', 'immich')
            username = username or env.get('POSTGRES_USER', 'postgres')
        return self.call('exec', '-T', 'database', 'psql', '-X', '-qAt', '-v', 'ON_ERROR_STOP=1',
                         '-U', username, '-d', database, payload=query.encode(), timeout=60).decode().strip()

    def api(self, path, method='GET', body=None, headers=None, file=None, fields=None):
        q = {'path': path, 'method': method, 'headers': headers or {}}
        if body is not None:
            q['body'] = body
        if file:
            q.update(file=file, fields=fields or {})
        data = self.call('exec', '-T', 'immich-server', 'node', '--input-type=module', '-e', NODE_HTTP,
                         payload=json.dumps(q).encode(), timeout=90)
        return json.loads(data)

    def capture_database(self, destination):
        config = self.config()['services']['database']['environment']
        username = config.get('POSTGRES_USER', 'postgres')
        database = config.get('POSTGRES_DB', 'immich')
        for identifier in (username, database):
            if not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_]*', identifier):
                raise RehearsalError('Unsupported database/user identifier.')
        command = self.base + ['exec', '-T', 'database', 'psql', '-X', '-qAt', '-v', 'ON_ERROR_STOP=1', '-U', username, '-d', database]
        # Keep a real exported repeatable-read snapshot open. Dump and metadata
        # comparison must see EXACTLY the same transaction, even during uploads.
        session = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if session.stdin is None or session.stdout is None:
            session.kill()
            session.wait(timeout=10)
            raise RehearsalError('Database snapshot pipes unavailable.')
        try:
            session.stdin.write(b'BEGIN ISOLATION LEVEL REPEATABLE READ; SELECT pg_export_snapshot();\n')
            session.stdin.flush()
            if not select.select([session.stdout], [], [], 30)[0]:
                raise RehearsalError('Database snapshot export timed out.')
            snapshot = session.stdout.readline().decode().strip()
            if not re.fullmatch(r'[0-9A-Fa-f]{8}-[0-9A-Fa-f]{8}-[0-9]+', snapshot):
                raise RehearsalError('Database snapshot export failed.')
            command = self.base + ['exec', '-T', 'database', 'pg_dump', '-U', username, '-d', database,
                                   '--format=custom', '--no-owner', '--no-privileges', '--snapshot=' + snapshot]
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as output:
                run(command, stdout=output, timeout=600)
                output.flush()
                os.fsync(output.fileno())
            baseline = invariants(self, username, database, snapshot=snapshot)
        finally:
            try:
                session.communicate(input=b'ROLLBACK;\n\\q\n', timeout=10)
            except (subprocess.TimeoutExpired, BrokenPipeError):
                session.kill()
                session.wait(timeout=10)
        if destination.stat().st_size < 32:
            raise RehearsalError('Database capture is empty.')
        with destination.open('rb') as archive:
            if archive.read(5) != b'PGDMP':
                raise RehearsalError('Database capture is not a custom PostgreSQL archive.')
        return username, database, baseline


def clone_private_tree(source: Path, destination: Path):
    source = Path(source)
    if source.is_symlink() or source.absolute() != source.resolve(strict=True):
        raise RehearsalError('Source symlink or symlink parent is unsupported.')
    source = source.resolve(strict=True)
    if not source.is_dir() or source == Path('/'):
        raise RehearsalError('A concrete source directory is required.')
    if destination.exists() or source in destination.resolve().parents:
        raise RehearsalError('Clone destination overlaps source or already exists.')
    destination.mkdir(mode=0o700, parents=True)
    # CoW where supported; otherwise real byte copies. Never hard links/symlinks.
    run(['cp', '-a', '--reflink=auto', '--sparse=always', str(source) + '/.', str(destination)], timeout=1800)
    for path in destination.rglob('*'):
        if path.is_symlink():
            target = path.resolve()
            if target != destination.resolve() and destination.resolve() not in target.parents:
                raise RehearsalError('Clone contains a symlink escaping its private root.')


def build_isolated(config, sandbox: Path, selected, mount_map, password):
    version(selected)
    services = config.get('services') or {}
    if set(services) != {'immich-server', 'immich-machine-learning', 'database', 'redis'}:
        raise RehearsalError('Only the four-service stock Compose layout is supported.')
    project = 'immich-rehearsal-' + uuid.uuid4().hex[:12]
    output = {'name': project, 'services': {}, 'networks': {'isolated': {'internal': True, 'driver_opts': {'com.docker.network.bridge.gateway_mode_ipv4': 'isolated', 'com.docker.network.bridge.gateway_mode_ipv6': 'isolated'}}}}
    database = services['database'].get('environment') or {}
    db_user, db_name = database.get('POSTGRES_USER', 'postgres'), database.get('POSTGRES_DB', 'immich')
    for value in (db_user, db_name):
        if not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_]*', value):
            raise RehearsalError('Unsupported database/user identifier.')
    for name, service in services.items():
        if any(service.get(key) for key in ('privileged', 'devices', 'network_mode', 'pid', 'ipc', 'cap_add')):
            raise RehearsalError('Unisolatable device/host privilege setting.')
        image = service.get('image') or ''
        if name == 'immich-server':
            if not (image.startswith('ghcr.io/immich-app/immich-server:') or image.startswith('sha256:')):
                raise RehearsalError('Official server image is required.')
            image = 'ghcr.io/immich-app/immich-server:' + selected
        if name == 'immich-machine-learning':
            if not (image.startswith('ghcr.io/immich-app/immich-machine-learning:') or image.startswith('sha256:')):
                raise RehearsalError('Official ML image is required.')
            # Portable CPU clone: hardware passthrough is not silently inherited.
            suffix = image.split(':', 1)[1]
            if any(suffix.endswith(x) for x in ('-cuda', '-rocm', '-armnn', '-openvino', '-rknn')):
                raise RehearsalError('Hardware-accelerated production needs a separately verified rehearsal backend.')
            image = 'ghcr.io/immich-app/immich-machine-learning:' + selected
        item = {'image': image, 'networks': ['isolated'], 'restart': 'no', 'dns': ['127.0.0.1'],
                'mem_limit': '2g' if name.startswith('immich') else '1g', 'cpus': 2}
        env = dict(service.get('environment') or {})
        # Strip outbound credentials and endpoints; only stock DB/Redis settings survive.
        if name == 'immich-server':
            env = {'DB_PASSWORD': password, 'DB_USERNAME': db_user, 'DB_DATABASE_NAME': db_name,
                   'DB_HOSTNAME': 'database', 'DB_PORT': '5432', 'REDIS_HOSTNAME': 'redis',
                   'IMMICH_MACHINE_LEARNING_URL': 'http://immich-machine-learning:3003'}
        elif name == 'database':
            env = {'POSTGRES_PASSWORD': password, 'POSTGRES_USER': db_user, 'POSTGRES_DB': db_name,
                   'POSTGRES_INITDB_ARGS': '--data-checksums'}
            item['shm_size'] = '128mb'
        elif name == 'redis':
            env = {}
        elif name == 'immich-machine-learning':
            env = {}
        if env:
            item['environment'] = env
        # Rehearse the executable release, not only its image. Preserve safe
        # startup commands, entrypoints and health checks from the exact template.
        for field in ('command', 'entrypoint', 'healthcheck', 'user', 'shm_size', 'working_dir', 'stop_grace_period', 'init'):
            if field in service:
                item[field] = copy.deepcopy(service[field])
        volumes = []
        for mount in service.get('volumes') or []:
            target = mount['target']
            if name == 'database':
                if target != '/var/lib/postgresql/data':
                    raise RehearsalError('Unsupported database mount layout.')
                source = sandbox / 'database'
            elif mount.get('type') == 'bind' and target == '/etc/localtime':
                continue
            elif mount.get('type') == 'bind':
                key = str(Path(mount['source']).resolve())
                if key not in mount_map:
                    raise RehearsalError('Every data/external-library mount needs a private clone.')
                source = mount_map[key]
            elif mount.get('type') == 'volume' and name == 'immich-machine-learning' and target == '/cache':
                key = 'volume:' + mount['source']
                if key not in mount_map:
                    raise RehearsalError('Cached ML models must be captured into the isolated copy.')
                source = mount_map[key]
            else:
                raise RehearsalError('Unsupported mount; refusing to inherit an external volume.')
            source.mkdir(mode=0o700, parents=True, exist_ok=True)
            if sandbox.resolve() not in source.resolve().parents:
                raise RehearsalError('Clone mount escapes sandbox.')
            volumes.append({'type': 'bind', 'source': str(source.resolve()), 'target': target,
                            'read_only': bool(mount.get('read_only', False))})
        if volumes:
            item['volumes'] = volumes
        if name == 'immich-server':
            item['depends_on'] = ['database', 'redis', 'immich-machine-learning']
        output['services'][name] = item
    return output


def invariants(compose: Compose, username, database, snapshot=None):
    # Actual app metadata only, never dumped to the conversational log.
    columns = compose.sql("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name='asset_face';", database, username).splitlines()
    if 'personId' in columns:
        link_join = 'f."personId"=p.id'
    elif 'personGroupId' in columns:
        link_join = 'f."personGroupId"=p."personGroupId"'
    else:
        raise RehearsalError('Unknown face relationship schema; comparison cannot be certified.')
    statement = f"""
SELECT json_build_object(
 'users',(SELECT count(*) FROM public."user"),
 'assets',(SELECT count(*) FROM public.asset),
 'albums',(SELECT count(*) FROM public.album),
 'album_links',(SELECT count(*) FROM public.album_asset),
 'album_identity',(SELECT md5(coalesce(string_agg(CAST(id AS text)||"albumName",'' ORDER BY id),'')) FROM public.album),
 'album_relationships',(SELECT md5(coalesce(string_agg(\"albumId\"::text||\"assetId\"::text,'' ORDER BY \"albumId\",\"assetId\"),'')) FROM public.album_asset),
 'people',(SELECT count(*) FROM public.person),
 'faces',(SELECT count(*) FROM public.asset_face),
 'person_links',(SELECT count(*) FROM public.asset_face f JOIN public.person p ON {link_join} JOIN public.asset a ON a.id=f."assetId" AND a."ownerId"=p."ownerId"),
 'asset_identity',(SELECT md5(coalesce(string_agg(id::text||"ownerId"::text||"originalPath"||"originalFileName",'' ORDER BY id),'')) FROM public.asset),
 'person_metadata',(SELECT md5(coalesce(string_agg("ownerId"::text||name||coalesce("birthDate"::text,''),'' ORDER BY "ownerId",name,"birthDate"),'')) FROM public.person),
 'face_links',(SELECT md5(coalesce(string_agg(f.id::text||f."assetId"::text||p."ownerId"::text||p.name,'' ORDER BY f.id,p."ownerId"),'')) FROM public.asset_face f JOIN public.person p ON {link_join} JOIN public.asset a ON a.id=f."assetId" AND a."ownerId"=p."ownerId")
)::text;
"""
    if snapshot:
        if not re.fullmatch(r'[0-9A-Fa-f]{8}-[0-9A-Fa-f]{8}-[0-9]+', snapshot):
            raise RehearsalError('Invalid exported snapshot token.')
        statement = "BEGIN ISOLATION LEVEL REPEATABLE READ; SET TRANSACTION SNAPSHOT '" + snapshot + "';\n" + statement + '\nCOMMIT;'
    return json.loads(compose.sql(statement, database, username))


def install_local_probe_key(compose: Compose, username, database):
    user = compose.sql('SELECT id FROM public."user" WHERE "deletedAt" IS NULL AND status=\'active\' ORDER BY "isAdmin" DESC,"createdAt" LIMIT 1;', database, username)
    if not re.fullmatch(r'[0-9a-f-]{36}', user):
        raise RehearsalError('Clone has no usable user for authenticated functional checks.')
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode()).hexdigest()
    identifier = str(uuid.uuid4())
    compose.sql(f"INSERT INTO public.api_key(id,name,key,\"userId\",permissions) VALUES ('{identifier}','isolated-rehearsal',decode('{digest}','hex'),'{user}',ARRAY['all']::varchar[]);", database, username)
    return token


def functional_checks(compose: Compose, selected, token, sandbox: Path):
    headers = {'x-api-key': token}
    answer = compose.api('/api/server/version')
    wanted = tuple(int(x) for x in selected[1:].split('.'))
    data = answer.get('data') or {}
    if answer['status'] != 200 or tuple(data.get(x) for x in ('major', 'minor', 'patch')) != wanted:
        raise RehearsalError('Clone version API did not confirm the candidate.')
    for endpoint in ('/api/users/me', '/api/albums', '/api/assets/statistics'):
        if compose.api(endpoint, headers=headers)['status'] != 200:
            raise RehearsalError('Authenticated clone read failed.')
    asset = compose.sql("SELECT a.id FROM public.asset a JOIN public.api_key k ON k.\"userId\"=a.\"ownerId\" WHERE a.\"deletedAt\" IS NULL AND k.name='isolated-rehearsal' ORDER BY a.\"createdAt\" LIMIT 1;")
    if asset:
        if not re.fullmatch(r'[0-9a-f-]{36}', asset):
            raise RehearsalError('Invalid sampled asset identifier.')
        answer = compose.api('/api/assets/' + asset + '/original', headers=headers)
        if answer['status'] != 200 or answer['bytes'] == 0:
            raise RehearsalError('Clone could not read a sample original asset.')
        original_path = compose.sql("SELECT \"originalPath\" FROM public.asset WHERE id='" + asset + "';")
        expected = compose.call('exec', '-T', 'immich-server', 'node', '-e',
            "const fs=require('fs'),c=require('crypto');const h=c.createHash('sha256');const s=fs.createReadStream(process.argv[1]);s.on('data',b=>h.update(b));s.on('end',()=>console.log(h.digest('hex')));", original_path).decode().strip()
        if answer['sha256'] != expected:
            raise RehearsalError('Sample original API bytes differ from the stored file.')
    # Clone-only album write verifies authenticated mutation, without touching originals.
    created = compose.api('/api/albums', method='POST', body={'albumName': 'isolated-rehearsal-write'}, headers=headers)
    if created['status'] not in (200, 201) or not (created.get('data') or {}).get('id'):
        raise RehearsalError('Clone-only write operation failed.')
    identifier = created['data']['id']
    removed = compose.api('/api/albums/' + identifier, method='DELETE', headers=headers)
    if removed['status'] not in (200, 204):
        raise RehearsalError('Clone-only write cleanup failed.')
    return {'version': selected, 'authenticated_reads': True, 'sample_asset_read': bool(asset),
            'clone_album_write': True}


def verify_isolation(clone, sandbox):
    ids = clone.call('ps', '-aq').decode().split()
    if not ids:
        raise RehearsalError('No clone containers exist for isolation validation.')
    inspect = json.loads(run(['docker', 'inspect', *ids]))
    for container in inspect:
        host = container['HostConfig']
        if host.get('PortBindings') or host.get('Privileged') or host.get('NetworkMode') == 'host':
            raise RehearsalError('Clone exposes host ports or privilege.')
        for mount in container['Mounts']:
            if mount['Type'] != 'bind' or sandbox.resolve() not in Path(mount['Source']).resolve().parents:
                raise RehearsalError('Clone mounts state outside its private root.')
        for network_name, network_id in container['NetworkSettings']['Networks'].items():
            identifier = network_id.get('NetworkID') or network_name
            network = json.loads(run(['docker', 'network', 'inspect', identifier]))[0]
            options = network.get('Options') or {}
            if (not network.get('Internal') or options.get('com.docker.network.bridge.gateway_mode_ipv4') != 'isolated'
                    or options.get('com.docker.network.bridge.gateway_mode_ipv6') != 'isolated'):
                raise RehearsalError('Clone network is not internal/host-isolated.')
    return True


def rehearse(source_path, selected, state_root, candidate_path=None):
    version(selected)
    if not selected.startswith('v'):
        raise RehearsalError('Exact published v-prefixed release tag required.')
    source = Compose(source_path)
    config = source.config()
    requested = Path(state_root).absolute()
    state_root = requested.resolve()
    if requested != state_root or requested.is_symlink():
        raise RehearsalError('State root or parent is a symbolic link.')
    # Reject overlapping state storage BEFORE mkdir, dump or any file write.
    for service in config['services'].values():
        for mount in service.get('volumes', []):
            if mount.get('type') == 'bind':
                original = Path(mount['source']).resolve()
            elif mount.get('type') == 'volume':
                actual = config.get('volumes', {}).get(mount['source'], {}).get('name')
                if not actual:
                    raise RehearsalError('Volume identity unavailable before capture.')
                item = json.loads(run(['docker', 'volume', 'inspect', actual]))[0]
                original = Path(item['Mountpoint']).resolve()
            else:
                raise RehearsalError('Unsupported source mount.')
            if original == state_root or original in state_root.parents or state_root in original.parents:
                raise RehearsalError('State root overlaps source mount; no capture permitted.')
    state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if state_root.stat().st_mode & 0o077:
        raise RehearsalError('Rehearsal state root must be private (0700).')
    sandbox = state_root / ('run-' + uuid.uuid4().hex)
    sandbox.mkdir(mode=0o700)
    private_json(sandbox / 'source-config.json', config)
    username, database, baseline = source.capture_database(sandbox / 'database.dump')
    mapping = {}
    for name, service in config['services'].items():
        if name == 'database':
            continue
        for mount in service.get('volumes') or []:
            if mount.get('type') == 'bind' and mount.get('target') != '/etc/localtime':
                original = Path(mount['source']).resolve()
                if str(original) not in mapping:
                    destination = sandbox / 'files' / str(len(mapping))
                    clone_private_tree(original, destination)
                    mapping[str(original)] = destination
            elif mount.get('type') == 'volume' and name == 'immich-machine-learning' and mount.get('target') == '/cache':
                logical = mount['source']
                actual = config.get('volumes', {}).get(logical, {}).get('name')
                if not actual:
                    raise RehearsalError('ML volume identity unavailable.')
                data = json.loads(run(['docker', 'volume', 'inspect', actual]))[0]
                if data.get('Driver') != 'local' or data.get('Options'):
                    raise RehearsalError('External ML cache driver is unsupported.')
                destination = sandbox / 'files' / str(len(mapping))
                clone_private_tree(Path(data['Mountpoint']), destination)
                mapping['volume:' + logical] = destination
    template = Compose(candidate_path).config() if candidate_path else config
    isolated = build_isolated(template, sandbox, selected, mapping, secrets.token_urlsafe(32))
    if candidate_path:
        for name in isolated['services']:
            isolated['services'][name]['image'] = template['services'][name]['image']
    path = sandbox / 'compose.json'
    private_json(path, isolated)
    clone = Compose(path, isolated['name'])
    private_json(sandbox / 'receipt.json', {'stage': 'captured', 'target': selected,
                                           'production_mutations': False})
    try:
        clone.call('create', timeout=180)
        verify_isolation(clone, sandbox)  # actual mounts/network BEFORE any process starts
        clone.call('up', '-d', '--wait', '--wait-timeout', '180', 'database', 'redis', timeout=240)
        verify_isolation(clone, sandbox)
        # Restore only into this fresh, private PostgreSQL instance.
        with (sandbox / 'database.dump').open('rb') as archive:
            clone.call('exec', '-T', 'database', 'pg_restore', '-U', username, '-d', database,
                       '--no-owner', '--no-privileges', '--single-transaction', '--exit-on-error',
                       stdin=archive, timeout=600)
        restored = invariants(clone, username, database)
        if restored != baseline:
            raise RehearsalError('Restored metadata counts differ from the captured database.')
        token = install_local_probe_key(clone, username, database)
        clone.call('up', '-d', '--wait', '--wait-timeout', '240', timeout=300)
        post = invariants(clone, username, database)
        if post != baseline:
            raise RehearsalError('Candidate changed sampled metadata counts during startup.')
        tests = functional_checks(clone, selected, token, sandbox)
        verify_isolation(clone, sandbox)
        receipt = {'stage': 'passed', 'target': selected, 'tests': tests,
                   'metadata_counts_preserved': True, 'runtime_isolation_verified': True,
                   'production_mutations': False, 'sandbox': str(sandbox)}
        private_json(sandbox / 'receipt.json', receipt)
        log('rehearsal', **receipt)
        return receipt
    except Exception as exc:
        private_json(sandbox / 'receipt.json', {'stage': 'failed', 'target': selected,
                                               'error_type': type(exc).__name__, 'production_mutations': False})
        raise
    finally:
        # Owned clone only; preserve files/receipts for inspection and do not delete data.
        clone.call('down', '--timeout', '30', timeout=90)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-compose', required=True)
    parser.add_argument('--target', required=True)
    parser.add_argument('--state-dir', required=True)
    args = parser.parse_args()
    try:
        rehearse(args.source_compose, args.target, args.state_dir)
        return 0
    except Exception as exc:
        log('rehearsal', stage='failed', error_type=type(exc).__name__, production_mutations=False)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
