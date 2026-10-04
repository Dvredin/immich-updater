"""Automatic destructive recovery test, restricted to an owned isolated clone."""
import copy
import hashlib
import json
import uuid
from pathlib import Path

from rehearsal import Compose, RehearsalError, invariants, private_json, rehearse, clone_resources, verify_isolation, start_clone_staged
from resource_policy import preflight_memory, ensure_parent, release_parent
from transaction import apply, atomic_bytes, pinned, private_root
from risk_checks import log


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


def restore_drill(source_path, candidate_path, installed, selected, state_root):
    root = private_root(state_root)
    template = root / ('drill-template-' + uuid.uuid4().hex + '.json')
    private_json(template, pinned(Compose(source_path).config()))
    captured = rehearse(source_path, installed, root / 'drill-copies', candidate_path=template)
    sandbox = Path(captured['sandbox'])
    path = sandbox / 'compose.json'
    clone = Compose(path)
    original = clone.config()
    if not original['name'].startswith('immich-rehearsal-') or any(
            not network.get('internal') for network in original.get('networks', {}).values()):
        raise RehearsalError('Recovery drill refuses non-rehearsal runtime.')
    for service in original['services'].values():
        for mount in service.get('volumes', []):
            if mount['type'] != 'bind' or sandbox not in Path(mount['source']).resolve().parents:
                raise RehearsalError('Recovery drill mounts outside its owned sandbox.')
    atomic_bytes(sandbox / '.env', ('IMMICH_VERSION=' + installed + '\n').encode())
    upgraded = copy.deepcopy(original)
    candidate = json.loads(Path(candidate_path).read_text())
    for name, service in upgraded['services'].items():
        service['image'] = candidate['services'][name]['image']
        service['pull_policy'] = 'never'
    prepared = sandbox / 'candidate.json'
    private_json(prepared, upgraded)
    preflight_memory()
    try:
        ensure_parent(original)
        clone.call('create', timeout=180)
        verify_isolation(clone, sandbox)  # fresh containers, inspected BEFORE startup
        start_clone_staged(clone)
        clone_resources(clone)
        env = original['services']['database'].get('environment', {})
        user, database = env.get('POSTGRES_USER', 'postgres'), env.get('POSTGRES_DB', 'immich')
        baseline = invariants(clone, user, database)
        library = next(Path(m['source']) for m in original['services']['immich-server']['volumes'] if m['target'] == '/data')
        before = tree_hashes(library)
        config_bytes = path.read_bytes()
        env_bytes = (sandbox / '.env').read_bytes()
        candidate_resources = {}
        def corrupt(active):
            candidate_resources.update(clone_resources(active))  # before recreating old recovery containers
            # Mutate real clone DB and files only after target migrations/health ran.
            active.sql('DELETE FROM public.album;', database, user)
            atomic_bytes(library / '.test-only-rollback-mutation', b'intentionally modified clone file\n')
            sample = next(iter(library.rglob('*.jpg')), None)
            if sample:
                with sample.open('ab') as out:
                    out.write(b'CORRUPTED-BY-EXPLICIT-RESTORE-DRILL')
        try:
            apply(path, prepared, selected, installed, sandbox / 'transactions',
                  inject_failure=True, failure_hook=corrupt)
        except RehearsalError as exc:
            if str(exc) != 'Injected test-only post-migration failure.':
                raise
        else:
            raise RehearsalError('Fault injection did not execute.')
        checkpoints = list((sandbox / 'transactions').glob('checkpoint-*'))
        if len(checkpoints) != 1 or not (checkpoints[0] / 'recovery-receipt.json').is_file():
            raise RehearsalError('No verified full-state recovery receipt.')
        if invariants(clone, user, database) != baseline:
            raise RehearsalError('Restore drill did not restore actual metadata.')
        archived_library = next(p for p in checkpoints[0].glob('mount-*') if (p / '.immich').exists() or (p / 'upload').exists())
        recovery = json.loads((checkpoints[0] / 'recovery-receipt.json').read_text())
        # Background jobs legitimately regenerate thumbnails after old startup. All
        # cold file bytes were checked while stopped; originals/config must still match.
        immutable = lambda hashes: {k: v for k, v in hashes.items() if Path(k).name != '.immich' and not k.startswith(('thumbs/', 'encoded-video/', 'backups/'))}
        if (not recovery.get('file_bytes_verified_before_start') or
                immutable(tree_hashes(library)) != immutable(tree_hashes(archived_library)) or
                path.read_bytes() != config_bytes or (sandbox / '.env').read_bytes() != env_bytes):
            raise RehearsalError('Restore drill did not restore original files/configuration state.')
        if not candidate_resources:
            raise RehearsalError('Candidate resource checks missing before destructive recovery injection.')
        receipt = {'stage': 'passed', 'installed': installed, 'target': selected,
                   'post_migration_failure_injected': True, 'database_restored': True,
                   'files_restored': True, 'configuration_restored': True,
                   'media_scope': 'bounded_sample', 'database_scope': 'full',
                   'production_checkpoint_verified': False,
                   'old_image_and_health_verified': True, 'production_mutations': False,
                   'resource_limits_verified': True, 'resources': clone_resources(clone),
                   'candidate_resources_before_rollback': candidate_resources}
        private_json(sandbox / 'restore-drill-receipt.json', receipt)
        log('restore_drill', **receipt)
        return receipt
    finally:
        clone.call('down', '--timeout', '30', timeout=90)
        release_parent(original)
