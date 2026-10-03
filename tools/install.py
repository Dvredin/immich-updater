#!/usr/bin/env python3
"""One-time installation of the rehearsed Immich updater on an existing host.
Default: disable old timer, isolated host acceptance, install code, leave timer OFF.
--activate: require the verified host receipt, then enable the single existing timer.
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
import subprocess
import sys
from pathlib import Path

REVISION = ''
SOURCE_ROOT = Path(__file__).resolve().parent.parent
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


PACKAGE_FILES = (
    '.gitignore', 'LICENSE', 'README.md', 'docs/REHEARSAL_VERIFICATION.md',
    'immich_updater.py', 'rehearsal.py', 'transaction.py', 'recovery_drill.py',
    'risk_checks.py', 'risk_policy.json', 'requirements.txt', 'requirements-dev.txt',
    'systemd/immich-updater.service', 'systemd/immich-updater.timer',
    'tools/install.py', 'tests/test_automation.py', 'tests/test_isolation.py',
    'tests/test_install.py', 'tests/run_live_acceptance.py', 'tests/seed_live_fixture.py',
    'tests/archive/strict_policy_v1.py',
)


def package():
    files = {}
    for relative in PACKAGE_FILES:
        path = SOURCE_ROOT / relative
        if not path.is_file() or path.is_symlink():
            raise StopInstall('Missing or symlinked source file: ' + relative)
        files[relative] = path.read_bytes()
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
    return actual


def gate(output):
    rows = []
    for line in output.splitlines():
        if line.startswith('{'):
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
    decisions = [row for row in rows if row.get('event') == 'decision' and row.get('decision') == 'verified_not_applied']
    if not decisions:
        raise StopInstall('Нет VERIFIED_NOT_APPLIED: пропуск/ошибка не является успешной репетицией. Таймер остаётся выключенным.')
    target = decisions[-1].get('target')
    rehearsed = any(row.get('event') == 'rehearsal' and row.get('stage') == 'passed'
                    and row.get('target') == target and row.get('runtime_isolation_verified') is True
                    and row.get('production_mutations') is False for row in rows)
    restored = any(row.get('event') == 'restore_drill' and row.get('stage') == 'passed'
                   and row.get('target') == target and row.get('production_mutations') is False
                   and all(row.get(key) is True for key in ('database_restored','files_restored','configuration_restored','old_image_and_health_verified'))
                   for row in rows)
    if not (rehearsed and restored):
        raise StopInstall('Репетиция/восстановление не подтверждены полностью. Таймер остаётся выключенным.')
    return target


def prerequisites():
    print('Проверка ресурсов и Docker; конфигурация/пароли Immich не выводятся.', flush=True)
    if not APP.is_dir() or not (APP / '.env').is_file() or (APP / '.env').is_symlink():
        raise StopInstall('Ожидается существующий Immich в '+str(APP)+' с обычным .env.')
    pending = [STATE / 'transaction.json', APP / '.immich-updater-state' / 'transaction.json', APP / 'UPDATE_FAILED']
    if any(path.exists() for path in pending):
        raise StopInstall('Найдено незавершённое/ошибочное старое обновление. Нельзя подменять его код до восстановления.')
    data = call('docker','version','--format','{{json .Server}}').stdout
    engine = json.loads(data)['Version']
    available = 0
    for line in Path('/proc/meminfo').read_text().splitlines():
        if line.startswith('MemAvailable:'):
            available = int(line.split()[1]) * 1024
    print(json.dumps({'docker_engine':engine,'available_memory_MiB':available//(1024*1024),'architecture':platform.machine()}), flush=True)
    if int(engine.split('.')[0]) < 28:
        raise StopInstall('Docker Engine старее 28: изолированный шлюз не подтверждён. Docker/ОС автоматически не обновляются.')
    if available < 6 * 1024**3:
        raise StopInstall('Для безопасной параллельной репетиции нужно 6 GiB свободной RAM; сейчас меньше. Рабочий Immich не остановлен.')
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
    if receipt.get('status') != 'prepared' or receipt.get('revision') != REVISION:
        raise StopInstall('Нет подходящего успешного отчёта установки.')
    expected_files = receipt.get('files', {})
    if set(expected_files) != set(PACKAGE_FILES) or receipt.get('app_dir') != str(APP):
        raise StopInstall('Readiness is not bound to this complete package/application path.')
    for relative, digest in expected_files.items():
        path = DEST / relative
        if not path.is_file() or path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise StopInstall('Установленный код отличается от проверенного пакета: ' + relative)
    if (STATE / 'transaction.json').exists():
        raise StopInstall('Есть незавершённая транзакция; таймер автоматически не активируется.')
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
    contents = package()
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
    try:
        call('python3','-m','venv',str(staging/'.venv'),timeout=120)
    except StopInstall:
        raise StopInstall('Не удалось создать venv. Если отсутствует модуль: sudo apt-get install python3-venv. Таймер выключен.')
    python = str(staging/'.venv/bin/python')
    pip = call(python,'-m','pip','install','--disable-pip-version-check','--timeout','30','--retries','2','-r',str(staging/'requirements.txt'),timeout=900)
    private(staging/'dependency-install.log',pip.stdout+pip.stderr)
    result = call(python,'-m','unittest','discover','-s','tests','-q',cwd=str(staging),timeout=180)
    print(result.stderr.strip(),flush=True)
    STATE.mkdir(mode=0o700,parents=True,exist_ok=True)
    if STATE.is_symlink() or STATE.stat().st_mode & 0o077:
        raise StopInstall('Каталог состояния должен быть обычным и приватным (0700).')
    # Read-only path/space validation surfaces a reason without dumping env values.
    probe = "from pathlib import Path\nfrom rehearsal import Compose, RehearsalError\nfrom transaction import rehearsal_capacity\nroot=Path('/opt/immich')\npaths=[root/n for n in ('docker-compose.yml','docker-compose.yaml','compose.yml','compose.yaml') if (root/n).is_file()]\nif len(paths)!=1:raise SystemExit('Ожидается один Compose-файл в каталоге Immich.')\ntry:\n    required=rehearsal_capacity(Compose(paths[0]).config(),'/var/lib/immich-updater/state')\n    print('FULL_COPY_BUDGET_BYTES='+str(required))\nexcept RehearsalError as error:\n    raise SystemExit(str(error))\n"
    probe = probe.replace("root=Path('/opt/immich')", "root=Path("+repr(str(APP))+")")
    checked = call(python,'-c',probe,cwd=str(staging),timeout=1200,check=False)
    if checked.returncode:
        print(checked.stdout+checked.stderr,flush=True)
        raise StopInstall('Проверка реального хранилища не прошла; основной Immich и его конфигурация не менялись.')
    print(checked.stdout.strip(),flush=True)
    log_path = STATE / ('prepare-' + stamp + '.log')
    print('Запущена --prepare-only. Делаются отдельные копии; рабочее обновление запрещено. Проверка может быть долгой.',flush=True)
    try:
        prepared = call(python,str(staging/'immich_updater.py'),'--immich-dir',str(APP),
                        '--server-url','http://localhost:2283','--state-dir',str(STATE),'--prepare-only',
                        cwd=str(staging),timeout=10800,check=False)
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or b''
        if isinstance(output,bytes):output=output.decode(errors='replace')
        private(log_path,output)
        raise StopInstall('Проверка превысила 3 часа. Таймер выключен, подготовка сохранена: '+str(staging))
    private(log_path,prepared.stdout+prepared.stderr)
    print(prepared.stdout,flush=True)
    if prepared.returncode:
        raise StopInstall('Репетиция завершилась ошибкой. Таймер выключен. Локальный журнал: '+str(log_path))
    target = gate(prepared.stdout)
    backup = Path('/opt') / ('immich-updater.before-' + stamp)
    if DEST.exists():
        os.rename(DEST,backup)
    os.rename(staging,DEST)
    # Local installed revision is explicit; no false claim of a git checkout.
    private(DEST/'INSTALLATION_REVISION',REVISION+'\n')
    saved = STATE / ('units-before-' + stamp);saved.mkdir(mode=0o700)
    unit_dir=Path('/etc/systemd/system')
    for filename in (SERVICE,TIMER):
        old=unit_dir/filename
        if old.is_file():shutil.copy2(old,saved/filename)
        shutil.copyfile(DEST/'systemd'/filename,old)
        old.chmod(0o644)
    drop_dir=unit_dir/(SERVICE+'.d');drop_dir.mkdir(mode=0o755,exist_ok=True)
    drop=drop_dir/'50-rehearsal-state.conf'
    if drop.exists():shutil.copy2(drop,saved/drop.name)
    drop_content='[Service]\nUser=root\nGroup=root\nEnvironment=IMMICH_DIR=/opt/immich\nEnvironment=IMMICH_SERVER_URL=http://localhost:2283\nEnvironment=IMMICH_UPDATER_STATE=/var/lib/immich-updater/state\nExecStart=\nExecStart=/usr/bin/flock -n /var/lib/immich-updater/update.lock /opt/immich-updater/.venv/bin/python /opt/immich-updater/immich_updater.py --immich-dir /opt/immich --server-url http://localhost:2283 --state-dir /var/lib/immich-updater/state --verbose\n'
    private(drop,drop_content.replace('/opt/immich\n',str(APP)+'\n').replace('--immich-dir /opt/immich ', '--immich-dir '+str(APP)+' '))
    drop.chmod(0o644)
    call('systemctl','daemon-reload')
    call('systemd-analyze','verify',str(unit_dir/SERVICE),str(unit_dir/TIMER),timeout=60)
    effective = call('systemctl','show',SERVICE,'--property=ExecStart','--value').stdout
    if str(DEST/'immich_updater.py') not in effective or '--state-dir /var/lib/immich-updater/state' not in effective:
        raise StopInstall('Эффективный ExecStart не совпадает с проверенным установленным кодом/состоянием.')
    if call('systemctl','show',SERVICE,'--property=User','--value').stdout.strip()!='root':
        raise StopInstall('Служба должна выполнять cold backup/restore от root.')
    if call('systemctl','show',SERVICE,'--property=OnFailure','--value').stdout.strip():
        raise StopInstall('В unit/drop-in есть внешний OnFailure. Автоматизация logs-only не подтверждена; таймер выключен.')
    if call('systemctl','is-active',TIMER,check=False).returncode == 0:
        raise StopInstall('Неожиданная активация таймера. Не выдаю готовность.')
    receipt={'status':'prepared','revision':REVISION,'target':target,'production_upgrade':False,
             'prepare_log':str(log_path),'old_checkout':str(backup),'timer_enabled':False,
             'app_dir':str(APP),'files':{name:hashlib.sha256(data).hexdigest() for name,data in contents.items()}}
    private(READY,json.dumps(receipt,indent=2)+'\n')
    print('PREPARED_TIMER_DISABLED: новый код установлен, репетиция и полный restore прошли; рабочий Immich НЕ обновлён.',flush=True)
    print('Старый updater сохранён: '+str(backup),flush=True)
    print('Для включения расписания выполните тот же файл с --activate.',flush=True)


def self_test():
    files=package()
    if not {'immich_updater.py','rehearsal.py','transaction.py','recovery_drill.py'} <= set(files):
        raise StopInstall('Неполный пакет.')
    fixture=[{'event':'decision','decision':'verified_not_applied','target':'v3.2.4'},
             {'event':'rehearsal','stage':'passed','target':'v3.2.4','runtime_isolation_verified':True,'production_mutations':False},
             {'event':'restore_drill','stage':'passed','target':'v3.2.4','production_mutations':False,'database_restored':True,'files_restored':True,'configuration_restored':True,'old_image_and_health_verified':True}]
    if gate('\n'.join(json.dumps(row) for row in fixture)) != 'v3.2.4':raise StopInstall('Неверный positive gate.')
    for rows in ([],[{'event':'decision','decision':'skip'}],fixture[:-1],fixture[1:]):
        try:gate('\n'.join(json.dumps(row) for row in rows))
        except StopInstall:pass
        else:raise StopInstall('Неверный negative gate.')
    print(json.dumps({'self_test':'passed','source_package_complete':True,'package_files':len(files),
                      'skip_and_partial_receipts_cannot_enable_timer':True,'system_changes':False}))


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
