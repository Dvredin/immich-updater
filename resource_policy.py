"""Native clone-only shared memory pool; never alters production limits."""
from pathlib import Path
import re
import subprocess

MIB = 1024 * 1024
PROFILE = 'compact-4g-v3'
# Services borrow idle capacity within ONE smaller, enforced parent pool.
LIMIT_MIB = {'immich-server': 1408, 'database': 1024,
             'immich-machine-learning': 384, 'redis': 32}
CLONE_BUDGET_MIB = 1728
HOST_RESERVE_MIB = 256
NODE_HEAP_MIB = 448
CLONE_LABEL = 'io.immich-updater.rehearsal'
PROC_ROOT = Path('/proc')
CGROUP_ROOT = Path('/sys/fs/cgroup')
LAB_PARENT = None  # patched only by the explicitly synthetic acceptance harness
PARENT_RE = re.compile(r'(?:immichupdatertest[a-z0-9]+-)?immichupdaterclone[0-9a-f]{32}\.slice')


class ResourceError(RuntimeError):
    pass


class ResourceUnavailable(ResourceError):
    """Transient host pressure, not proof that a release is broken."""


def memory_available():
    for line in (PROC_ROOT / 'meminfo').read_text().splitlines():
        if line.startswith('MemAvailable:'):
            return int(line.split()[1]) * 1024
    raise ResourceUnavailable('MemAvailable cannot be verified; rehearsal deferred.')


def preflight_memory(available=None):
    available = memory_available() if available is None else available
    required = (CLONE_BUDGET_MIB + HOST_RESERVE_MIB) * MIB
    receipt = {'profile': PROFILE, 'clone_budget_MiB': CLONE_BUDGET_MIB,
               'host_reserve_MiB': HOST_RESERVE_MIB,
               'required_available_MiB': required // MIB,
               'available_memory_MiB': available // MIB}
    if available < required:
        raise ResourceUnavailable('Compact rehearsal needs ' + str(required // MIB)
                                  + ' MiB available RAM including host reserve; available '
                                  + str(available // MIB) + ' MiB. No source lifecycle change.')
    return receipt


def parent_slice(sandbox):
    name = Path(sandbox).name
    if not re.fullmatch(r'run-[0-9a-f]{32}', name):
        raise ResourceError('Shared clone pool requires an owned rehearsal root.')
    if LAB_PARENT is not None and not re.fullmatch(r'immichupdatertest[a-z0-9]+\.slice', LAB_PARENT):
        raise ResourceError('Invalid synthetic lab parent.')
    prefix = LAB_PARENT[:-6] + '-' if LAB_PARENT else ''
    return prefix + 'immichupdaterclone' + name[4:] + '.slice'


def parent_path(name):
    if not isinstance(name, str) or not PARENT_RE.fullmatch(name):
        raise ResourceError('Missing or untrusted clone parent identity.')
    parts = name[:-6].split('-')
    path = CGROUP_ROOT.joinpath(*['-'.join(parts[:i]) + '.slice' for i in range(1, len(parts) + 1)]).resolve()
    if CGROUP_ROOT.resolve() not in path.parents:
        raise ResourceError('Clone parent escapes its controller.')
    return path


def _systemctl(*args):
    result = subprocess.run(['systemctl', *args], capture_output=True, timeout=30)
    if result.returncode:
        raise ResourceError('Cannot configure or release native clone memory pool.')
    return result.stdout


def configured_parent(config):
    services = config.get('services') or {}
    if set(services) != set(LIMIT_MIB) or any(
            (item.get('labels') or {}).get(CLONE_LABEL) != 'true' for item in services.values()):
        raise ResourceError('Shared pool refuses mixed or non-clone services.')
    parents = {item.get('cgroup_parent') for item in services.values()}
    if len(parents) != 1:
        raise ResourceError('All clone services must share one memory pool.')
    name = parents.pop()
    parent_path(name)
    return name


def ensure_parent(config):
    name = configured_parent(config)
    path = parent_path(name)
    if path.exists():
        # An existing live pool is inspected, never retuned or reset to erase evidence.
        return parent_metrics(name)
    _systemctl('start', name)
    _systemctl('set-property', '--runtime', name,
               'MemoryMax=' + str(CLONE_BUDGET_MIB * MIB), 'MemorySwapMax=0', 'MemoryAccounting=yes')
    return parent_metrics(name)


def release_parent(config):
    # Call only AFTER successful owned-container down. Never kill an unrelated runtime.
    name = configured_parent(config)
    path = parent_path(name)
    if path.exists():
        try:
            if any(item.read_text().strip() for item in path.rglob('cgroup.procs')):
                raise ResourceError('Clone pool still contains processes; refuse to stop it.')
        except OSError as exc:
            raise ResourceError('Cannot verify empty clone pool for safe release.') from exc
    _systemctl('stop', name)


def settings(name):
    if name not in LIMIT_MIB:
        raise ResourceError('Unknown clone service; no resource budget.')
    maximum = LIMIT_MIB[name] * MIB
    return {'mem_limit': maximum, 'memswap_limit': maximum, 'mem_swappiness': 0,
            'oom_kill_disable': False, 'oom_score_adj': 1000, 'cpus': 1,
            'labels': {CLONE_LABEL: 'true'}}


def kernel_metrics(group, maximum):
    try:
        if (group / 'memory.max').read_text().strip() != str(maximum):
            raise ResourceError('Kernel clone memory.max does not match the inspected limit.')
        if (group / 'memory.swap.max').read_text().strip() != '0':
            raise ResourceError('Kernel clone swap is not disabled.')
        events = dict(line.split() for line in (group / 'memory.events').read_text().splitlines())
        if 'oom_kill' not in events or 'oom' not in events:
            raise ResourceError('Kernel clone OOM counters unavailable.')
        if int(events['oom_kill']) or int(events['oom']):
            raise ResourceError('Clone cgroup recorded an OOM, including child-process OOM.')
        current = int((group / 'memory.current').read_text())
        peak_path = group / 'memory.peak'
        peak = int(peak_path.read_text()) if peak_path.is_file() else None
        # memory.max is the enforced kernel hard limit, but documented transient
        # overcharges can make historical memory.peak slightly larger. Preserve
        # that measurement; do not mistake it for disabled enforcement or an OOM.
        if current > maximum:
            raise ResourceError('Current kernel clone accounting exceeds the configured budget.')
        return {'current_bytes': current, 'peak_bytes': peak, 'oom_events': 0,
                'kernel_limit_bytes': maximum, 'swap_limit_bytes': 0}
    except (OSError, ValueError) as exc:
        raise ResourceError('Cannot verify local clone cgroup memory accounting.') from exc


def parent_metrics(name):
    return kernel_metrics(parent_path(name), CLONE_BUDGET_MIB * MIB)


def cgroup_metrics(container, maximum):
    state = container.get('State') or {}
    if state.get('OOMKilled'):
        raise ResourceError('Clone container was OOM-killed.')
    if not state.get('Running'):
        return None
    pid = state.get('Pid')
    if type(pid) is not int or pid <= 0:
        raise ResourceError('Running clone process identity unavailable.')
    try:
        lines = (PROC_ROOT / str(pid) / 'cgroup').read_text().splitlines()
        paths = [line[3:] for line in lines if line[:3] == '0' + ':' + ':']
        if len(paths) != 1 or not paths[0].startswith('/'):
            raise ResourceError('Local cgroup-v2 clone controller cannot be verified.')
        group = (CGROUP_ROOT / paths[0].lstrip('/')).resolve()
        parent = parent_path((container.get('HostConfig') or {}).get('CgroupParent'))
        if parent not in group.parents:
            raise ResourceError('Clone process is outside its enforced shared memory pool.')
        return kernel_metrics(group, maximum)
    except (OSError, ValueError) as exc:
        raise ResourceError('Cannot verify local clone cgroup identity.') from exc


def verify_containers(containers, *, require_running=False):
    """Check both the shared pool and each child at create-time and runtime."""
    found = {}
    parents = set()
    for container in containers:
        labels = container.get('Config', {}).get('Labels') or {}
        name = labels.get('com.docker.compose.service')
        if labels.get(CLONE_LABEL) != 'true' or name not in LIMIT_MIB or name in found:
            raise ResourceError('Clone resource identity is missing, mixed or duplicated.')
        host = container.get('HostConfig') or {}
        parent = host.get('CgroupParent')
        parent_path(parent)
        parents.add(parent)
        maximum = LIMIT_MIB[name] * MIB
        if host.get('Memory') != maximum or host.get('MemorySwap') != maximum:
            raise ResourceError('Actual clone RAM/swap limits differ from the compact budget.')
        if host.get('OomKillDisable') or host.get('OomScoreAdj') != 1000:
            raise ResourceError('Clone OOM protection/priority is unsafe.')
        if (host.get('RestartPolicy') or {}).get('Name') not in {'no', ''}:
            raise ResourceError('A failed clone must not automatically restart and erase OOM evidence.')
        if require_running and not (container.get('State') or {}).get('Running'):
            raise ResourceError('Every runtime clone service must be running for resource acceptance.')
        found[name] = {'limit_bytes': maximum, 'kernel': cgroup_metrics(container, maximum)}
    if set(found) != set(LIMIT_MIB) or len(parents) != 1:
        raise ResourceError('All four compact clone services must share one inspected pool.')
    return {'profile': PROFILE, 'total_limit_bytes': CLONE_BUDGET_MIB * MIB,
            'child_ceiling_sum_bytes': sum(LIMIT_MIB.values()) * MIB,
            'parent': parent_metrics(parents.pop()), 'swap_disabled': True, 'services': found}
