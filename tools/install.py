#!/usr/bin/env python3
"""Install the single-stack updater: read-only host validation, leave timer OFF.
No parallel Immich, file-library clone, rehearsal or automatic downgrade.
--activate verifies the package and host before enabling the existing daily timer.
"""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import uuid
from pathlib import Path

REVISION = ''
SOURCE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SOURCE_ROOT))
APP = Path('/opt/immich')
DEST = Path('/opt/immich-updater')
STATE = Path('/var/lib/immich-updater/state')
READY = STATE / 'install-ready.json'
TIMER = 'immich-updater.timer'
SERVICE = 'immich-updater.service'


class StopInstall(RuntimeError):
    pass


def call(*arguments, cwd=None, timeout=60, check=True):
    result = subprocess.run(arguments, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        # Command stderr may contain resolved application settings. Keep it private.
        raise StopInstall('Команда не прошла: ' + arguments[0] + ' (код ' + str(result.returncode) + ').')
    return result


def private(path, content):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise StopInstall('Недопустимая символическая ссылка: ' + str(path))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())


def logged_call(log_path, *arguments, **kwargs):
    """Keep package-command diagnostics private even when the command fails."""
    def text(value):
        if isinstance(value, bytes):
            return value.decode(errors='replace')
        return value or ''

    try:
        result = call(*arguments, check=False, **kwargs)
    except subprocess.TimeoutExpired as error:
        private(log_path, text(error.stdout) + text(error.stderr))
        raise StopInstall('Команда подготовки превысила время ожидания. Локальный журнал: ' + str(log_path)) from error
    private(log_path, text(result.stdout) + text(result.stderr))
    if result.returncode:
        raise StopInstall('Команда подготовки не прошла (код ' + str(result.returncode)
                          + '). Локальный журнал: ' + str(log_path))
    return result


PACKAGE_FILES = (
    '.gitignore', 'LICENSE', 'README.md', 'docs/REHEARSAL_VERIFICATION.md',
    'immich_updater.py', 'simple_update.py', 'availability_monitor.py', 'rehearsal.py', 'transaction.py', 'recovery_drill.py',
    'risk_checks.py', 'risk_policy.json', 'resource_policy.py', 'sample_rehearsal.py', 'requirements.txt', 'requirements-dev.txt',
    'systemd/immich-updater.service', 'systemd/immich-updater.timer',
    'tools/install.py', 'tests/test_simple.py', 'tests/test_candidate.py', 'tests/test_availability.py', 'tests/test_automation.py', 'tests/test_isolation.py',
    'tests/test_install.py', 'tests/test_resources.py', 'tests/test_samples.py', 'tests/run_live_acceptance.py', 'tests/seed_live_fixture.py',
    'tests/archive/strict_policy_v1.py',
)


def package():
    files = {}
    for relative in PACKAGE_FILES:
        path = SOURCE_ROOT / relative
        if not path.is_file() or path.is_symlink() or path.absolute()!=path.resolve():
            raise StopInstall('Missing or symlinked source file: ' + relative)
        files[relative] = path.read_bytes()
    return files


def authenticated_package(expected):
    files = package()
    if (SOURCE_ROOT/'.git').exists():
        for relative, content in files.items():
            published = subprocess.run(['git','-C',str(SOURCE_ROOT),'show',expected+':'+relative],
                                       capture_output=True,timeout=30)
            if published.returncode or published.stdout != content:
                raise StopInstall('Package bytes differ from the requested commit: '+relative)
    else:
        # The installed marker alone is not provenance. Bind to the private receipt
        # generated from commit-checked source, outside the installed checkout.
        if SOURCE_ROOT != DEST or not READY.is_file() or READY.is_symlink():
            raise StopInstall('A non-Git source requires its trusted installed package receipt.')
        receipt=json.loads(READY.read_text())
        if receipt.get('status')!='prepared' or receipt.get('revision')!=expected:
            raise StopInstall('Installed receipt does not authenticate this revision.')
        hashes={name:hashlib.sha256(body).hexdigest() for name,body in files.items()}
        if receipt.get('files')!=hashes:
            raise StopInstall('Installed source differs from its commit-checked package receipt.')
    return files


def source_revision(expected):
    if not expected or not re.fullmatch(r'[0-9a-f]{40}', expected):
        raise StopInstall('--expected-revision requires the exact 40-character published commit.')
    if (SOURCE_ROOT / '.git').exists():
        actual = call('git', '-C', str(SOURCE_ROOT), 'rev-parse', 'HEAD').stdout.strip()
    elif (SOURCE_ROOT / 'INSTALLATION_REVISION').is_file():
        actual = (SOURCE_ROOT / 'INSTALLATION_REVISION').read_text().strip()
    else:
        raise StopInstall('Cannot verify source revision.')
    if actual != expected:
        raise StopInstall('Source revision differs from the requested published revision.')
    authenticated_package(expected)
    return actual


def pending_updates():
    return (STATE/'transaction.json', STATE/'simple-update.json',
            APP/'.immich-updater-state'/'transaction.json', APP/'.immich-updater-state'/'simple-update.json',
            APP/'.immich-updater-interrupted.json', APP/'UPDATE_FAILED')


def safe_unit_path(path):
    for parent in path.parents:
        if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
            raise StopInstall('Systemd unit directory ancestry must be real directories.')
    if os.path.lexists(path) and not stat.S_ISREG(path.lstat().st_mode):
        raise StopInstall('Systemd unit destination must be a regular nonsymlink file.')


def write_unit(path, content):
    safe_unit_path(path)
    temporary=path.with_name('.'+path.name+'.new-'+uuid.uuid4().hex)
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o644)
    try:
        with os.fdopen(fd,'wb') as output:
            os.fchmod(output.fileno(),0o644);output.write(content);output.flush();os.fsync(output.fileno())
        safe_unit_path(path)
        os.replace(temporary,path)
        parent=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:os.fsync(parent)
        finally:os.close(parent)
    finally:
        if temporary.exists():temporary.unlink()


def gate(output):
    rows=[]
    for line in output.splitlines():
        try:row=json.loads(line)
        except json.JSONDecodeError:continue
        if isinstance(row,dict):rows.append(row)
    accepted=[row for row in rows if row.get('event')=='preflight' and row.get('profile')=='single-stack-v1'
              and row.get('source_mutations') is False and row.get('parallel_rehearsal') is False
              and row.get('photo_copy_required') is False and row.get('required_backup_free_bytes',0)>0
              and row.get('available_bytes',0)>=row['required_backup_free_bytes']
              and (row.get('runtime') or {}).get('services_running') is True
              and (row.get('runtime') or {}).get('ping') is True]
    if len(accepted)!=1 or not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+',(accepted[0].get('runtime') or {}).get('version','')):
        raise StopInstall('Нет подтверждённого read-only preflight single-stack-v1. Таймер выключен.')
    return accepted[0]['runtime']['version']


def prerequisites():
    print('Проверка ресурсов и Docker; конфигурация/пароли Immich не выводятся.', flush=True)
    if not APP.is_dir() or not (APP / '.env').is_file() or (APP / '.env').is_symlink():
        raise StopInstall('Ожидается существующий Immich в '+str(APP)+' с обычным .env.')
    if any(os.path.lexists(path) for path in pending_updates()):
        raise StopInstall('Найдено незавершённое/ошибочное старое обновление. Нельзя подменять его код до восстановления.')
    data = call('docker','version','--format','{{json .Server}}').stdout
    engine = json.loads(data)['Version']
    available = 0
    for line in Path('/proc/meminfo').read_text().splitlines():
        if line.startswith('MemAvailable:'):
            available = int(line.split()[1]) * 1024
    print(json.dumps({'docker_engine':engine,'available_memory_MiB':available//(1024*1024),'architecture':platform.machine()}), flush=True)
    if int(engine.split('.')[0]) < 24:
        raise StopInstall('Нужен Docker 24+ и Compose с up --wait; Docker/ОС не обновляются автоматически.')
    call('docker','compose','version',timeout=30)
    if platform.machine() in {'x86_64','amd64'}:
        cpu = Path('/proc/cpuinfo').read_text().splitlines()
        flags = [set(line.split(':',1)[1].split()) for line in cpu if line.startswith('flags')]
        required = {'cx16','lahf_lm','popcnt','ssse3','sse4_1','sse4_2','pni'}
        if not flags or any(not required <= processor for processor in flags):
            raise StopInstall('CPU виртуальной машины не подтверждает x86-64-v2 для Immich ML v3.')
    if DEST.is_symlink():
        raise StopInstall('/opt/immich-updater не должен быть ссылкой.')


def activate(release_lock=None):
    if not READY.is_file() or READY.is_symlink():
        raise StopInstall('Сначала запустите этот установщик без --activate; нужен успешный отчёт именно этой VM.')
    receipt = json.loads(READY.read_text())
    if receipt.get('status') != 'prepared' or receipt.get('profile')!='single-stack-v1' or receipt.get('revision') != REVISION:
        raise StopInstall('Нет подходящего успешного отчёта установки.')
    expected_files = receipt.get('files', {})
    if set(expected_files) != set(PACKAGE_FILES) or receipt.get('app_dir') != str(APP):
        raise StopInstall('Readiness is not bound to this complete package/application path.')
    for relative, digest in expected_files.items():
        path = DEST / relative
        if not path.is_file() or path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise StopInstall('Установленный код отличается от проверенного пакета: ' + relative)
    if any(os.path.lexists(path) for path in pending_updates()):
        raise StopInstall('Есть незавершённая транзакция; таймер автоматически не активируется.')
    # This entry point intentionally runs in system Python. Only its installed
    # private venv has application dependencies; never import them into this process.
    python = DEST / '.venv/bin/python'
    if not python.is_file():
        raise StopInstall('Installed updater venv is missing. Timer remains disabled.')
    checked = logged_call(STATE/'activation-preflight.log',str(python),str(DEST/'immich_updater.py'),
                          '--immich-dir',str(APP),'--state-dir',str(STATE),'--preflight-only',
                          cwd=str(DEST),timeout=180)
    gate(checked.stdout)
    if release_lock:
        release_lock()  # Persistent catch-up must not collide with the installer lock.
    call('systemctl','enable','--now',TIMER)
    enabled = call('systemctl','is-enabled',TIMER).stdout.strip()
    active = call('systemctl','is-active',TIMER).stdout.strip()
    if enabled != 'enabled' or active != 'active':
        raise StopInstall('Не подтверждены enabled/active для таймера.')
    print('TIMER_ENABLED: ежедневная автоматизация включена. Persistent может сразу догнать пропущенный запуск.', flush=True)
    print(call('systemctl','list-timers',TIMER,'--no-pager').stdout)
    print('Журнал: sudo journalctl -u immich-updater.service -n 60 --no-pager')


def install():
    # No application/service stop: only the unsafe old schedule is disarmed.
    status = call('systemctl','show',SERVICE,'--property=ActiveState','--value').stdout.strip()
    if status not in {'inactive','failed'}:
        raise StopInstall('Старый updater сейчас выполняется ('+status+'). Не прерываю его: дождитесь окончания и повторите установщик.')
    if call('systemctl','is-active',TIMER,check=False).returncode == 0:
        raise StopInstall('Старый таймер не остановлен.')
    print('OLD_TIMER_DISABLED: Immich продолжает работать; остановлено только расписание обновлений.', flush=True)
    if STATE.exists() and (STATE.is_symlink() or STATE.resolve()!=STATE.absolute() or STATE.stat().st_mode & 0o077):
        raise StopInstall('Существующий каталог состояния должен быть обычным и приватным (0700).')
    if READY.exists():
        if READY.is_symlink():raise StopInstall('Отчёт готовности не должен быть ссылкой.')
        previous = STATE / ('install-ready.before-' + datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.json')
        os.rename(READY,previous)  # failed reinstallation cannot reuse stale READY
    prerequisites()
    unit_dir=Path('/etc/systemd/system')
    drop_dir=unit_dir/(SERVICE+'.d')
    drop=drop_dir/'50-rehearsal-state.conf'
    for path in (unit_dir/SERVICE,unit_dir/TIMER,drop):safe_unit_path(path)
    contents = authenticated_package(REVISION)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    staging = Path('/opt') / ('immich-updater.staging-' + stamp)
    if staging.exists():
        raise StopInstall('Каталог подготовки уже существует; повторите позже, он не удаляется.')
    staging.mkdir(mode=0o700)
    for relative, content in contents.items():
        path = staging / relative
        path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        path.write_bytes(content)
        path.chmod(0o600)
    print('PACKAGE_STEP: venv', flush=True)
    logged_call(staging/'venv-create.log','python3','-m','venv',str(staging/'.venv'),timeout=120)
    python = str(staging/'.venv/bin/python')
    print('PACKAGE_STEP: dependencies', flush=True)
    logged_call(staging/'dependency-install.log',python,'-m','pip','install','--disable-pip-version-check','--timeout','30','--retries','2','-r',str(staging/'requirements.txt'),timeout=900)
    print('PACKAGE_STEP: unit-tests', flush=True)
    result = logged_call(staging/'package-tests.log',python,'-m','unittest','discover','-s','tests','-q',cwd=str(staging),timeout=180)
    print(result.stderr.strip(),flush=True)
    STATE.mkdir(mode=0o700,parents=True,exist_ok=True)
    if STATE.is_symlink() or STATE.stat().st_mode & 0o077:
        raise StopInstall('Каталог состояния должен быть обычным и приватным (0700).')
    log_path=STATE/('preflight-'+stamp+'.log')
    print('Проверка единственного рабочего стека и места для бэкапа БД. Клоны и репетиция отключены; обновление не запускается.',flush=True)
    prepared=logged_call(log_path,python,str(staging/'immich_updater.py'),'--immich-dir',str(APP),
                         '--state-dir',str(STATE),'--preflight-only',cwd=str(staging),timeout=180)
    print(prepared.stdout,flush=True)
    target=gate(prepared.stdout)
    backup = Path('/opt') / ('immich-updater.before-' + stamp)
    if DEST.exists():
        os.rename(DEST,backup)
    os.rename(staging,DEST)
    # Local installed revision is explicit; no false claim of a git checkout.
    private(DEST/'INSTALLATION_REVISION',REVISION+'\n')
    saved = STATE / ('units-before-' + stamp);saved.mkdir(mode=0o700)
    for filename in (SERVICE,TIMER):
        old=unit_dir/filename
        safe_unit_path(old)
        if old.is_file():shutil.copy2(old,saved/filename)
        write_unit(old,(DEST/'systemd'/filename).read_bytes())
    drop_dir.mkdir(mode=0o755,exist_ok=True)
    safe_unit_path(drop)
    if drop.exists():shutil.copy2(drop,saved/drop.name)
    drop_content='[Service]\nUser=root\nGroup=root\nEnvironment=IMMICH_DIR=/opt/immich\nEnvironment=IMMICH_SERVER_URL=http://localhost:2283\nEnvironment=IMMICH_UPDATER_STATE=/var/lib/immich-updater/state\nExecStart=\nExecStart=/usr/bin/flock -n /var/lib/immich-updater/update.lock /opt/immich-updater/.venv/bin/python /opt/immich-updater/immich_updater.py --immich-dir /opt/immich --server-url http://localhost:2283 --state-dir /var/lib/immich-updater/state --verbose\n'
    write_unit(drop,drop_content.replace('/opt/immich\n',str(APP)+'\n').replace('--immich-dir /opt/immich ', '--immich-dir '+str(APP)+' ').encode())
    call('systemctl','daemon-reload')
    call('systemd-analyze','verify',str(unit_dir/SERVICE),str(unit_dir/TIMER),timeout=60)
    effective = call('systemctl','show',SERVICE,'--property=ExecStart','--value').stdout
    if str(DEST/'immich_updater.py') not in effective or '--state-dir /var/lib/immich-updater/state' not in effective:
        raise StopInstall('Эффективный ExecStart не совпадает с проверенным установленным кодом/состоянием.')
    if call('systemctl','show',SERVICE,'--property=User','--value').stdout.strip()!='root':
        raise StopInstall('Служба должна выполнять логический бэкап БД и обновление от root.')
    if call('systemctl','show',SERVICE,'--property=OnFailure','--value').stdout.strip():
        raise StopInstall('В unit/drop-in есть внешний OnFailure; стороннее восстановление не разрешено. Таймер выключен.')
    if call('systemctl','is-active',TIMER,check=False).returncode == 0:
        raise StopInstall('Неожиданная активация таймера. Не выдаю готовность.')
    receipt={'status':'prepared','profile':'single-stack-v1','revision':REVISION,'installed':target,'production_upgrade':False,
             'prepare_log':str(log_path),'old_checkout':str(backup),'timer_enabled':False,
             'app_dir':str(APP),'files':{name:hashlib.sha256(data).hexdigest() for name,data in contents.items()}}
    private(READY,json.dumps(receipt,indent=2)+'\n')
    print('PREPARED_TIMER_DISABLED: single-stack updater установлен; рабочий стек и место для бэкапа БД проверены. Репетиции нет; Immich НЕ обновлён.',flush=True)
    print('Старый updater сохранён: '+str(backup),flush=True)
    print('Для включения расписания выполните тот же файл с --activate.',flush=True)


def self_test():
    files=package()
    if not {'immich_updater.py','simple_update.py','transaction.py'}<=set(files):raise StopInstall('Неполный пакет.')
    fixture={'event':'preflight','profile':'single-stack-v1','source_mutations':False,'parallel_rehearsal':False,
             'photo_copy_required':False,'required_backup_free_bytes':100,'available_bytes':101,
             'runtime':{'version':'v3.1.0','services_running':True,'ping':True}}
    if gate(json.dumps(fixture))!='v3.1.0':raise StopInstall('Invalid positive preflight gate.')
    for row in ({},{'event':'decision','decision':'skip'},{**fixture,'runtime':{}},{**fixture,'available_bytes':1}):
        try:gate(json.dumps(row))
        except StopInstall:pass
        else:raise StopInstall('Incomplete preflight admitted.')
    print(json.dumps({'self_test':'passed','profile':'single-stack-v1','package_files':len(files),
                      'no_parallel_rehearsal':True,'system_changes':False}))


def main():
    global APP, REVISION
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--activate',action='store_true')
    parser.add_argument('--self-test',action='store_true')
    parser.add_argument('--app-dir',default='/opt/immich')
    parser.add_argument('--expected-revision')
    args=parser.parse_args()
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+',args.app_dir):
        raise StopInstall('Application path must be absolute and contain no whitespace/control characters.')
    APP=Path(args.app_dir)
    if APP.is_symlink() or APP.resolve()!=APP.absolute():
        raise StopInstall('Application path must be a real directory without symlink parents.')
    if args.self_test:
        self_test();return 0
    REVISION=source_revision(args.expected_revision)
    if os.geteuid()!=0:
        raise StopInstall('Запустите через sudo python3. Пароль вводится только локально в вашей VM.')
    if sys.version_info<(3,10):raise StopInstall('Нужен Python 3.10+. ОС не обновляется этим установщиком.')
    os.umask(0o077)
    if not args.activate:
        call('systemctl','disable','--now',TIMER)
    lock=Path('/var/lib/immich-updater')
    lock.mkdir(mode=0o700,parents=True,exist_ok=True)
    fd=os.open(lock/'update.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if args.activate:activate(lambda: fcntl.flock(fd,fcntl.LOCK_UN))
        else:install()
    finally:os.close(fd)
    return 0


if __name__=='__main__':
    try:sys.exit(main())
    except (StopInstall,BlockingIOError,KeyboardInterrupt,subprocess.TimeoutExpired) as error:
        print('STOP: '+str(error),file=sys.stderr)
        if '--activate' in sys.argv:
            print('Проверьте systemctl status immich-updater.timer и journalctl -u immich-updater.service; активация могла запустить догоняющее обновление.',file=sys.stderr)
        else:
            print('Не включайте старый таймер вручную. Рабочее обновление этим режимом не запускалось.',file=sys.stderr)
        sys.exit(1)
