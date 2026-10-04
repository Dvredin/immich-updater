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
import shlex
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from risk_checks import log, version
from archive.resource_policy import (settings, preflight_memory, verify_containers, ResourceError,
                             ResourceUnavailable, parent_slice, ensure_parent, release_parent, CLONE_LABEL, NODE_HEAP_MIB)

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
    diagnostic: dict[str, Any] = {}


def dollar_literals(value: Any, *, encode: bool) -> Any:
    """Compose config output escapes '$'; resolved models keep literal values."""
    if isinstance(value,str):return value.replace('$','$$') if encode else value.replace('$$','$')
    if isinstance(value,list):return [dollar_literals(v,encode=encode) for v in value]
    if isinstance(value,dict):return {k:dollar_literals(v,encode=encode) for k,v in value.items()}
    return value


def compose_bytes(config):
    return json.dumps(dollar_literals(config,encode=True),ensure_ascii=False).encode()


def command_failure(command, returncode, stderr):
    """Expose only operation/code/classifications, never command args or stderr."""
    operation='other'
    if command[:2]==['docker','pull']:operation='image_pull'
    elif command[:3]==['docker','image','inspect']:operation='image_inspect'
    elif command[:3]==['docker','volume','inspect']:operation='volume_inspect'
    elif command[:2]==['docker','inspect']:operation='container_inspect'
    elif command[:2]==['docker','compose']:
        for verb in ('config','ps','stop','up','exec','create','down'):
            if verb in command:operation='compose_'+verb;break
        if 'pg_dump' in command:operation='database_dump'
        elif 'pg_restore' in command:operation='database_archive_check'
    text=(stderr or b'').lower()
    if isinstance(text,str):text=text.encode()
    patterns={'invalid_interpolation':b'invalid interpolation|invalid template',
              'undefined_volume':b'undefined volume', 'disk_full':b'no space left',
              'registry_denied':b'unauthorized|denied', 'connection':b'connection|certificate|timeout',
              'permission':b'permission', 'manifest':b'manifest'}
    error=RehearsalError('Command failed; see safe operation/exit-status diagnostics.')
    error.diagnostic={'error_code':'command_failed','operation':operation,'exit_status':returncode,
                      'hints':[key for key,pattern in patterns.items() if re.search(pattern,text)]}
    return error


def private_json(path: Path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump(value, out, indent=2)
        out.flush()
        os.fsync(out.fileno())


def run(command, *, payload=None, timeout=60, stdout=None, stdin=None, resource_errors_transient=False, environment=None):
    result = subprocess.run(command, input=payload, stdin=stdin, stdout=stdout or subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=timeout,env=environment)
    if result.returncode:
        # Only clone lifecycle ENOMEM is transient. Never treat source mutation
        # failures or an actual clone OOM as successful resource deferral.
        if resource_errors_transient and re.search(
                rb'cannot allocate memory|not enough memory|out of memory.*(?:create|start)|ENOMEM',
                result.stderr or b'', re.IGNORECASE):
            raise ResourceUnavailable('Docker clone create/start failed with transient memory exhaustion; source unchanged.')
        # Docker/config errors may include environment values: do not print stderr.
        raise command_failure(command,result.returncode,result.stderr)
    return result.stdout or b''


class Compose:
    def __init__(self, path, project=None):
        self.path = Path(path).resolve()
        self.base = ['docker', 'compose', '--project-directory', str(self.path.parent), '-f', str(self.path)]
        if project:
            self.base += ['-p', project]

    def call(self, *arguments, **kwargs):
        if arguments and arguments[0] in {'create', 'up', 'start'}:
            try:
                local = json.loads(self.path.read_text())
            except (OSError, json.JSONDecodeError):
                local = {}
            if str(local.get('name', '')).startswith('immich-rehearsal-') and any(
                    (item.get('labels') or {}).get(CLONE_LABEL) == 'true'
                    for item in local.get('services', {}).values()):
                kwargs['resource_errors_transient'] = True
        return run(self.base + list(arguments), **kwargs)

    def config(self, *, environment=None):
        result=json.loads(self.call('config', '--format', 'json',environment=environment))
        return dollar_literals(result,encode=False)

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
            from archive.sample_rehearsal import candidates
            private_json(destination.parent / 'sample-candidates.json',
                         candidates(self, username, database, snapshot))
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


def compact_postgres_command(service):
    """Append only clone memory tuning, retaining the image's preload/config args."""
    command = service.get('command')
    if command is None:
        metadata = json.loads(run(['docker', 'image', 'inspect', service['image']]))[0]
        command = metadata.get('Config', {}).get('Cmd')
    if isinstance(command, str):
        command = shlex.split(command)
    if not isinstance(command, list) or not command or any(not isinstance(arg, str) for arg in command):
        raise RehearsalError('PostgreSQL startup arguments unavailable for compact memory tuning.')
    if Path(command[0]).name != 'postgres':
        raise RehearsalError('Unknown PostgreSQL startup command; compact tuning refused.')
    return list(command) + ['-c', 'shared_buffers=64MB', '-c', 'work_mem=4MB',
                            '-c', 'maintenance_work_mem=64MB']


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
        item = {'image': image, 'networks': ['isolated'], 'restart': 'no', 'dns': ['127.0.0.1']}
        item.update(settings(name))
        item['cgroup_parent'] = parent_slice(sandbox)
        env = dict(service.get('environment') or {})
        # Strip outbound credentials and endpoints; only stock DB/Redis settings survive.
        if name == 'immich-server':
            env = {'DB_PASSWORD': password, 'DB_USERNAME': db_user, 'DB_DATABASE_NAME': db_name,
                   'DB_HOSTNAME': 'database', 'DB_PORT': '5432', 'REDIS_HOSTNAME': 'redis',
                   'IMMICH_MACHINE_LEARNING_URL': 'http://immich-machine-learning:3003',
                   'NODE_OPTIONS': '--max-old-space-size=' + str(NODE_HEAP_MIB)}
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
        if name == 'database':
            item['command'] = compact_postgres_command(service)
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
                    raise RehearsalError('ML needs a private isolated cache.')
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
    manifests = [sandbox / 'sample-manifest.json']
    assets = []
    manifests += [parent / 'sample-manifest.json' for parent in sandbox.parents
                  if re.fullmatch(r'run-[0-9a-f]{32}', parent.name)]
    manifest = next((path for path in manifests if path.is_file()), None)
    if manifest:
        assets = json.loads(manifest.read_text()).get('assets', [])
        asset = assets[0]['id'] if assets else ''
        if asset:
            if not re.fullmatch(r'[0-9a-f-]{36}', asset):
                raise RehearsalError('Invalid sampled asset identifier.')
            owns = compose.sql("SELECT count(DISTINCT a.id) FROM public.asset a JOIN public.api_key k ON k.\"userId\"=a.\"ownerId\" WHERE a.id='" + asset + "' AND k.name='isolated-rehearsal';")
            if owns != '1':
                raise RehearsalError('Sample does not belong to the authenticated probe account.')
    else:
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
        if answer['sha256'] != expected or (manifest and answer['sha256'] != assets[0]['sha256']):
            raise RehearsalError('Sample original API bytes differ from the captured file.')
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
    try:
        verify_containers(inspect)
    except ResourceError as exc:
        raise RehearsalError(str(exc)) from exc
    return True


def wait_clone_workers(clone, timeout=300):
    """Stock HTTP health can precede microservices/geodata initialization."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # Logs are consumed locally only. Missing/changed stock bootstrap evidence
        # is an unsupported readiness path, never permission to overlap heavy startup.
        output = clone.call('logs', '--no-color', 'immich-server', timeout=30)
        if b'Immich Microservices is running' in output:
            return True
        time.sleep(1)
    raise RehearsalError('Stock background worker initialization did not complete before ML startup.')


def start_clone_staged(clone):
    """Use stock services sequentially; full acceptance still requires all four alive."""
    clone.call('up', '-d', '--no-recreate', '--wait', '--wait-timeout', '180',
               'database', 'redis', timeout=240)
    # ML model/runtime initialization does not need to overlap DB migrations and
    # geodata loading. Commands, service topology and final runtime remain intact.
    clone.call('up', '-d', '--no-recreate', '--no-deps', '--wait', '--wait-timeout', '300',
               'immich-server', timeout=360)
    wait_clone_workers(clone)
    clone.call('up', '-d', '--no-recreate', '--wait', '--wait-timeout', '180', timeout=240)


def clone_resources(clone):
    ids = clone.call('ps', '-aq').decode().split()
    if not ids:
        raise RehearsalError('No clone containers for resource verification.')
    try:
        return verify_containers(json.loads(run(['docker', 'inspect', *ids])), require_running=True)
    except ResourceError as exc:
        raise RehearsalError(str(exc)) from exc


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
    from archive.sample_rehearsal import capacity
    capacity(source, state_root.parent if not state_root.exists() else state_root)
    memory = preflight_memory()  # reserve source/host RAM before capture or clone startup
    log('resource_check', **memory, production_mutations=False)
    state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if state_root.stat().st_mode & 0o077:
        raise RehearsalError('Rehearsal state root must be private (0700).')
    sandbox = state_root / ('run-' + uuid.uuid4().hex)
    sandbox.mkdir(mode=0o700)
    private_json(sandbox / 'source-config.json', config)
    username, database, baseline = source.capture_database(sandbox / 'database.dump')
    from archive.sample_rehearsal import sampled_mounts
    rows = json.loads((sandbox / 'sample-candidates.json').read_text())
    mapping, sample = sampled_mounts(config, sandbox, rows)
    if baseline.get('assets', 0) and not sample['assets']:
        raise RehearsalError('Populated source requires at least one bounded original sample.')
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
        preflight_memory()  # copies/dumps may have coincided with new host workload
        ensure_parent(isolated)
        clone.call('create', timeout=180)
        verify_isolation(clone, sandbox)  # actual mounts/network BEFORE any process starts
        clone.call('up', '-d', '--no-recreate', '--wait', '--wait-timeout', '180', 'database', 'redis', timeout=240)
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
        start_clone_staged(clone)
        post = invariants(clone, username, database)
        if post != baseline:
            raise RehearsalError('Candidate changed sampled metadata counts during startup.')
        tests = functional_checks(clone, selected, token, sandbox)
        verify_isolation(clone, sandbox)
        receipt = {'stage': 'passed', 'target': selected, 'tests': tests,
                   'metadata_counts_preserved': True, 'runtime_isolation_verified': True,
                   'media_scope': 'bounded_sample', 'database_scope': 'full',
                   'production_checkpoint_verified': False,
                   'resource_limits_verified': True, 'resources': clone_resources(clone),
                   'production_mutations': False, 'sandbox': str(sandbox),
                   'sample': {k: v for k, v in sample.items() if k != 'assets'}}
        private_json(sandbox / 'receipt.json', receipt)
        log('rehearsal', **receipt)
        return receipt
    except Exception as exc:
        # Preserve diagnostics before owned containers are removed. The file is
        # private: logs can contain application settings and must never be printed.
        try:
            if not isinstance(clone.base, list):
                raise OSError('No Compose process metadata for private diagnostics.')
            diagnostics = subprocess.run(clone.base + ['logs', '--no-color', '--tail', '160'],
                                         capture_output=True, timeout=30)
            private_json(sandbox / 'failure-diagnostics.json', {
                'stdout': diagnostics.stdout.decode(errors='replace'),
                'stderr': diagnostics.stderr.decode(errors='replace')})
        except (OSError, subprocess.TimeoutExpired):
            pass
        private_json(sandbox / 'receipt.json', {'stage': 'failed', 'target': selected,
                                               'error_type': type(exc).__name__, 'production_mutations': False})
        raise
    finally:
        # Owned clone only; preserve files/receipts for inspection and do not delete data.
        clone.call('down', '--timeout', '30', timeout=90)
        release_parent(isolated)


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
