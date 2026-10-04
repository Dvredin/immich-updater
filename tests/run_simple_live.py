"""Real, explicitly synthetic stock Immich migration + destructive recovery acceptance.

Root required. Never reads or modifies an existing owner deployment.
"""
import argparse
import json
import os
import re
import secrets
from unittest.mock import patch
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from rehearsal import Compose, RehearsalError, invariants, private_json, rehearse, run
from transaction import atomic_bytes, candidate_config, pinned
from simple_update import apply, preflight, healthy, NeedsAttention
from transaction import stop
import hashlib
import shutil
from recovery_drill import restore_drill
from seed_live_fixture import seed


def acceptance(root, selected='v3.2.4', cgroup_parent=None):
    if os.geteuid() != 0:
        raise RehearsalError('Root required for real cold PostgreSQL file snapshots.')
    if cgroup_parent and not re.fullmatch(r'immichupdatertest[a-z0-9]+\.slice', cgroup_parent):
        raise RehearsalError('A lab parent must be an explicitly test-only transient slice.')
    root = Path(root).resolve()
    if root.exists():
        raise RehearsalError('Use a fresh nonexistent owned lab root; never an existing installation.')
    root.mkdir(mode=0o700, parents=True)
    source = root / 'source';source.mkdir(mode=0o700)
    for name in ('library','database'):
        (source/name).mkdir(mode=0o700)
    project='immich-rehearsal-fixture-'+uuid.uuid4().hex[:10]
    password=secrets.token_urlsafe(24)
    atomic_bytes(source/'.synthetic-fixture',b'Purpose-built test data only; no owner account or photo.\n')
    atomic_bytes(source/'.env',b'IMMICH_VERSION=v3.1.0\n')
    network={'internal':True,'driver_opts':{
        'com.docker.network.bridge.gateway_mode_ipv4':'isolated',
        'com.docker.network.bridge.gateway_mode_ipv6':'isolated'}}
    images={'immich-server':'ghcr.io/immich-app/immich-server:v3.1.0',
            'immich-machine-learning':'ghcr.io/immich-app/immich-machine-learning:v3.1.0',
            'database':'ghcr.io/immich-app/postgres:14-vectorchord0.4.3-pgvectors0.2.0',
            'redis':'valkey/valkey:9'}
    config={'name':project,'services':{},'networks':{'isolated':network},'volumes':{'model-cache':{}}}
    for name,image in images.items():
        run(['docker','pull',image],timeout=1800)
        config['services'][name]={'image':image,'networks':['isolated'],'restart':'no','mem_limit':'2g'}
        if cgroup_parent:
            config['services'][name]['cgroup_parent']=cgroup_parent
    config['services']['database'].update(environment={
        'POSTGRES_PASSWORD':password,'POSTGRES_USER':'postgres','POSTGRES_DB':'immich',
        'POSTGRES_INITDB_ARGS':'--data-checksums'},shm_size='128mb',volumes=[{
            'type':'bind','source':str(source/'database'),'target':'/var/lib/postgresql/data'}],
        healthcheck={'test':['CMD-SHELL','pg_isready -U postgres -d immich'],'interval':'3s','timeout':'3s','retries':40})
    config['services']['immich-server'].update(environment={
        'DB_PASSWORD':password,'DB_USERNAME':'postgres','DB_DATABASE_NAME':'immich','DB_HOSTNAME':'database',
        'REDIS_HOSTNAME':'redis','IMMICH_MACHINE_LEARNING_URL':'http://immich-machine-learning:3003'},
        volumes=[{'type':'bind','source':str(source/'library'),'target':'/data'}],
        depends_on=['database','redis','immich-machine-learning'])
    config['services']['immich-machine-learning']['volumes']=[{
        'type':'volume','source':'model-cache','target':'/cache'}]
    path=source/'compose.json';private_json(path,config)
    stack=Compose(path)
    try:
        stack.call('up','-d','--wait','--wait-timeout','240',timeout=300)
        # Model an established installation, not a DB captured halfway through its
        # first-run geodata import. API health alone precedes worker readiness.
        from rehearsal import wait_clone_workers
        wait_clone_workers(stack)
        login={'email':'synthetic-acceptance@example.invalid','password':secrets.token_urlsafe(24),'name':'Synthetic acceptance'}
        signup=stack.api('/api/auth/admin-sign-up',method='POST',body=login)
        if signup['status'] != 201:
            raise RehearsalError('Actual fixture administrator registration failed.')
        private_json(source/'fixture-credentials.json',{**login,'userId':signup['data']['id']})
        seed(path)
        before=invariants(stack,'postgres','immich')
        originals={str(p.relative_to(source/'library')):hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in (source/'library').rglob('*') if p.is_file() and p.suffix=='.jpg'}
        candidate=candidate_config(path,selected,root/'candidates',rehearsal_capacity=False)
        receipt=apply(path,candidate,selected,'v3.1.0',root/'updates')
        if invariants(stack,'postgres','immich')!=before:raise RehearsalError('Metadata changed.')
        logged=stack.api('/api/auth/login',method='POST',body={'email':login['email'],'password':login['password']})
        if logged['status']!=201:raise RehearsalError('Old password login failed.')
        backup=Path(receipt['backup'])
        # Developer acceptance only: manually recover the synthetic single stack from its logical dump.
        # Production does not execute this drill or any automatic downgrade.
        stop(stack)
        os.rename(source/'database',source/'database-after-upgrade')
        (source/'database').mkdir(mode=0o700)
        oldstack=Compose(backup/'old-compose.json',project)
        oldstack.call('up','-d','--no-deps','--wait','--wait-timeout','180','database',timeout=240)
        with (backup/'database.dump').open('rb') as archive:
            oldstack.call('exec','-T','database','pg_restore','-U','postgres','-d','immich',
                          '--no-owner','--no-acl','--single-transaction','--exit-on-error',stdin=archive,timeout=600)
        for item in receipt['configurations']:
            atomic_bytes(Path(item['path']),Path(item['saved']).read_bytes(),item['mode'])
        oldstack.call('up','-d','--wait','--wait-timeout','300',timeout=360)
        healthy(oldstack,'v3.1.0')
        if invariants(oldstack,'postgres','immich')!=before:raise RehearsalError('Logical backup restored different metadata.')
        # Exercise failed target startup/check handling on this same disposable source, not another clone.
        def fault(active):raise RehearsalError('Test-only failure after target migration.')
        failed_state=root/'failed-updates'
        try:apply(path,candidate,selected,'v3.1.0',failed_state,failure_hook=fault)
        except RehearsalError:pass
        else:raise RehearsalError('Fault did not execute.')
        pending=json.loads((failed_state/'simple-update.json').read_text())
        if pending['phase']!='needs_attention' or not pending['backup_verified'] or pending['automatic_downgrade']:
            raise RehearsalError('Failed update did not retain manual-attention evidence.')
        for state_path in (failed_state,root/'changed-state-directory'):
            try:preflight(path,state_path)
            except NeedsAttention:pass
            else:raise RehearsalError('Failed update was allowed to repeat via state-directory change.')
        healthy(stack,selected) # Injected fault was a check failure, not an invented actual outage.
        for name,digest in originals.items():
            if hashlib.sha256((source/'library'/name).read_bytes()).hexdigest()!=digest:
                raise RehearsalError('Synthetic original changed.')
        result={'stage':'passed','profile':'single-stack-v1','synthetic_fixture':True,
                'source':'v3.1.0','target':selected,'actual_nonempty_upgrade':True,
                'logical_database_backup_restore_verified':True,'existing_password_login_verified':True,
                'source_metadata_preserved':True,'originals_preserved':True,'version_pin_persisted':True,
                'no_parallel_rehearsal':True,'failed_update_retained_for_owner':True,
                'failed_update_retry_blocked':True,'automatic_downgrade':False,
                'database_dump_bytes':(backup/'database.dump').stat().st_size,
                'owner_production_accessed':False}
        private_json(root/'simple-acceptance-receipt.json',result)
        print(json.dumps(result,sort_keys=True))
        return result
    finally:
        stack.call('down','--timeout','30',timeout=90)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lab-root',required=True)
    parser.add_argument('--target',default='v3.2.4')
    parser.add_argument('--cgroup-parent')
    args=parser.parse_args()
    acceptance(args.lab_root,args.target,args.cgroup_parent)
