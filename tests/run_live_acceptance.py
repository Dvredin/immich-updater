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
from transaction import apply, atomic_bytes, candidate_config, pinned
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
        login={'email':'synthetic-acceptance@example.invalid','password':secrets.token_urlsafe(24),'name':'Synthetic acceptance'}
        signup=stack.api('/api/auth/admin-sign-up',method='POST',body=login)
        if signup['status'] != 201:
            raise RehearsalError('Actual fixture administrator registration failed.')
        private_json(source/'fixture-credentials.json',{**login,'userId':signup['data']['id']})
        seed(path)
        before=invariants(stack,'postgres','immich')
        candidate=candidate_config(path,selected,root/'candidates')
        receipt=rehearse(path,selected,root/'rehearsals',candidate_path=candidate)
        if not receipt['tests']['sample_asset_read']:
            raise RehearsalError('A populated original must be tested.')
        if invariants(stack,'postgres','immich') != before:
            raise RehearsalError('Rehearsal modified the source test database.')
        drill=restore_drill(path,candidate,'v3.1.0',selected,root/'restore-drills')
        actual=apply(path,candidate,selected,'v3.1.0',root/'transactions')
        migrated_login=stack.api('/api/auth/login',method='POST',body={'email':login['email'],'password':login['password']})
        if migrated_login['status']!=201 or (migrated_login.get('data') or {}).get('userId')!=signup['data']['id']:
            raise RehearsalError('Existing synthetic password login failed after real migration.')
        if invariants(stack,'postgres','immich') != before:
            raise RehearsalError('Synthetic source upgrade changed retained metadata.')
        if (source/'.env').read_text() != 'IMMICH_VERSION='+selected+'\n':
            raise RehearsalError('Version was not persisted after success.')
        result={'stage':'passed','synthetic_fixture':True,'source':'v3.1.0','target':selected,
                'actual_nonempty_upgrade':True,'rehearsal':receipt['stage'],
                'destructive_full_state_restore':drill['stage'],'source_metadata_preserved':True,
                'version_pin_persisted':True,'existing_password_login_verified':True,'source_checks':actual,'lab_root':str(root),
                'owner_production_accessed':False}
        private_json(root/'acceptance-receipt.json',result)
        print(json.dumps(result,sort_keys=True))
        return result
    finally:
        # Keep files/volumes/receipts; stop and remove owned containers/networks only.
        stack.call('down','--timeout','30',timeout=90)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lab-root',required=True)
    parser.add_argument('--target',default='v3.2.4')
    parser.add_argument('--cgroup-parent',help='Explicit test-only slice covering source and private clones.')
    args=parser.parse_args()
    import resource_policy
    with patch('resource_policy.LAB_PARENT', args.cgroup_parent):
        acceptance(args.lab_root,args.target,args.cgroup_parent)
