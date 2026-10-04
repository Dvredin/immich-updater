"""Installer tests: real source packaging, simulated host lifecycle, no deployment."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

SOURCE=Path(__file__).resolve().parents[1]/'tools/install.py'
spec=importlib.util.spec_from_file_location('updater_installer',SOURCE)
if spec is None or spec.loader is None:raise RuntimeError('Installer module unavailable')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


def good_receipt():
    return '\n'.join(json.dumps(row) for row in [
        {'event':'decision','decision':'verified_not_applied','target':'v3.2.4'},
        {'event':'rehearsal','stage':'passed','target':'v3.2.4','media_scope':'bounded_sample','database_scope':'full','production_checkpoint_verified':False,'runtime_isolation_verified':True,'resource_limits_verified':True,'production_mutations':False},
        {'event':'restore_drill','stage':'passed','target':'v3.2.4','media_scope':'bounded_sample','database_scope':'full','production_checkpoint_verified':False,'production_mutations':False,
         'database_restored':True,'files_restored':True,'configuration_restored':True,'old_image_and_health_verified':True,'resource_limits_verified':True}])


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.revision_patch=patch.object(module,'REVISION','a'*40);self.revision_patch.start();self.addCleanup(self.revision_patch.stop)
        self.temp=tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'));self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.dest=self.root/'new';self.state=self.root/'state';self.state.mkdir(mode=0o700)
        self.ready=self.state/'ready.json';self.app=self.root/'app';self.app.mkdir();(self.app/'.env').write_text('TEST-ONLY')
        (self.app/'compose.yml').write_text('TEST-ONLY')
    def ready_installation(self):
        for relative,content in module.package().items():
            path=self.dest/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(content)
        python=self.dest/'.venv/bin/python';python.parent.mkdir(parents=True);python.write_text('TEST-ONLY recording interpreter')
        self.ready.write_text(json.dumps({'status':'prepared','revision':module.REVISION,'app_dir':str(self.app),
            'files':{name:module.hashlib.sha256(data).hexdigest() for name,data in module.package().items()}}))
    def activate(self,call,release=None):
        with patch.multiple(module,DEST=self.dest,APP=self.app,STATE=self.state,READY=self.ready,call=call),contextlib.redirect_stdout(io.StringIO()):
            return module.activate(release)
    def test_source_package_real_compile(self):
        files=module.package()
        self.assertIn('rehearsal.py',files)
        for name,data in files.items():
            if name.endswith('.py'):compile(data,name,'exec')
    def test_gate_missing_clone_resource_verification_blocks(self):
        for stage in (1,2):
            rows=[json.loads(line) for line in good_receipt().splitlines()]
            rows[stage].pop('resource_limits_verified')
            with self.assertRaises(module.StopInstall):module.gate('\n'.join(json.dumps(row) for row in rows))

    def test_gate_positive(self):
        self.assertEqual(module.gate(good_receipt()),'v3.2.4')
    def test_gate_skip_is_not_acceptance(self):
        with self.assertRaises(module.StopInstall):module.gate('{"event":"decision","decision":"skip"}')
    def test_gate_malformed_is_not_acceptance(self):
        with self.assertRaises(module.StopInstall):module.gate('{this is not json}')
    def test_gate_restore_different_target_is_not_acceptance(self):
        rows=[json.loads(line) for line in good_receipt().splitlines()];rows[-1]['target']='v3.2.3'
        with self.assertRaises(module.StopInstall):module.gate('\n'.join(json.dumps(row) for row in rows))
    def test_gate_missing_restored_configuration_blocks(self):
        rows=[json.loads(line) for line in good_receipt().splitlines()];rows[-1]['configuration_restored']=False
        with self.assertRaises(module.StopInstall):module.gate('\n'.join(json.dumps(row) for row in rows))
    def test_gate_source_mutated_blocks(self):
        rows=[json.loads(line) for line in good_receipt().splitlines()];rows[-1]['production_mutations']=True
        with self.assertRaises(module.StopInstall):module.gate('\n'.join(json.dumps(row) for row in rows))
    def test_activation_missing_host_acceptance_never_calls_systemctl(self):
        call=Mock()
        with self.assertRaises(module.StopInstall):self.activate(call)
        call.assert_not_called()
    def test_activation_modified_installed_code_never_calls_systemctl(self):
        self.ready_installation();(self.dest/'immich_updater.py').write_text('altered')
        call=Mock()
        with self.assertRaises(module.StopInstall):self.activate(call)
        call.assert_not_called()
    def test_activation_pending_transaction_blocks(self):
        self.ready_installation();(self.state/'transaction.json').write_text('{}')
        call=Mock()
        with self.assertRaises(module.StopInstall):self.activate(call)
        call.assert_not_called()
    def test_sample_preparation_cannot_enable_timer_without_production_rollback_storage(self):
        self.ready_installation()
        call=Mock(return_value=subprocess.CompletedProcess([],1,'','TEST-ONLY production rollback storage unavailable'))
        release=Mock()
        with self.assertRaises(module.StopInstall):self.activate(call,release)
        release.assert_not_called()
        self.assertEqual(len(call.call_args_list),1)
        self.assertEqual(call.call_args.args[0],str(self.dest/'.venv/bin/python'))
        self.assertEqual((self.state/'activation-storage.log').stat().st_mode&0o777,0o600)

    def test_zero_exit_without_positive_storage_receipt_cannot_enable_timer(self):
        self.ready_installation();call=Mock(return_value=subprocess.CompletedProcess([],0,'',''));release=Mock()
        with self.assertRaises(module.StopInstall):self.activate(call,release)
        release.assert_not_called();self.assertEqual(len(call.call_args_list),1)

    def test_gate_missing_scope_does_not_claim_full_production_validation(self):
        rows=[json.loads(line) for line in good_receipt().splitlines()]
        rows[1].pop('media_scope')
        with self.assertRaises(module.StopInstall):module.gate('\n'.join(json.dumps(row) for row in rows))

    def test_activation_works_from_stdlib_only_outer_interpreter(self):
        self.ready_installation()
        script = r'''
import contextlib,importlib.util,io,json,subprocess,sys
from pathlib import Path
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('installer',sys.argv[1])
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
m.DEST=Path(sys.argv[2]);m.APP=Path(sys.argv[3]);m.STATE=Path(sys.argv[4]);m.READY=Path(sys.argv[5]);m.REVISION=sys.argv[6]
events=[]
def call(*args,**kwargs):
    events.append(args)
    if '-c' in args:
        if args[0]!=str(m.DEST/'.venv/bin/python') or kwargs.get('cwd')!=str(m.DEST):raise RuntimeError('Wrong probe interpreter/cwd')
        return subprocess.CompletedProcess(args,0,'PRODUCTION_ROLLBACK_BUDGET_BYTES=1024\n','')
    if args[:2]==('systemctl','is-enabled'):return subprocess.CompletedProcess(args,0,'enabled\n','')
    return subprocess.CompletedProcess(args,0,'active\n','')
with patch.object(m,'call',call),contextlib.redirect_stdout(io.StringIO()):m.activate()
if not events or '-c' not in events[0]:raise RuntimeError('No installed-venv admission probe')
if any(name in sys.modules for name in ('requests','semantic_version','rehearsal','transaction')):raise RuntimeError('Activation imported app dependencies into outer interpreter')
print(json.dumps({'stdlib_outer':True,'installed_venv_probe':True,'host_lifecycle':'mocked'}))
'''
        result=subprocess.run([sys.executable,'-I','-S','-c',script,str(SOURCE),str(self.dest),str(self.app),str(self.state),str(self.ready),module.REVISION],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue(json.loads(result.stdout)['installed_venv_probe'])

    def test_activation_releases_lock_before_persistent_timer_start(self):
        self.ready_installation();events=[]
        def call(*args,**kwargs):
            events.append(args[1])
            if '-c' in args:return subprocess.CompletedProcess(args,0,'PRODUCTION_ROLLBACK_BUDGET_BYTES=1024\n','')
            return subprocess.CompletedProcess(args,0,'enabled\n' if args[1]=='is-enabled' else 'active\n','')
        self.activate(call,lambda:events.append('lock-released'))
        self.assertEqual(events[:3],['-c','lock-released','enable'])
    def test_active_old_updater_not_killed_or_replaced(self):
        call=Mock(return_value=Mock(stdout='active\n',returncode=0))
        with patch.multiple(module,call=call,DEST=self.dest),self.assertRaises(module.StopInstall):module.install()
        self.assertEqual([item.args[1] for item in call.call_args_list],['show'])
        self.assertFalse(self.dest.exists())
    def test_failed_reinstall_archives_old_ready_instead_of_reusing_it(self):
        self.ready.write_text('{"status":"prepared"}')
        def call(*args,**kwargs):
            return Mock(stdout='inactive\n',returncode=3 if args[1]=='is-active' else 0)
        with patch.multiple(module,call=call,DEST=self.dest,APP=self.app,STATE=self.state,READY=self.ready,prerequisites=Mock(side_effect=module.StopInstall('TEST insufficient RAM'))),contextlib.redirect_stdout(io.StringIO()),self.assertRaises(module.StopInstall):module.install()
        self.assertFalse(self.ready.exists());self.assertTrue(list(self.state.glob('install-ready.before-*.json')))
    def install_simulated(self,prepared_output,failed_step=None):
        self.dest.mkdir();(self.dest/'old-marker').write_text('untouched')
        calls=[];self.recorded_calls=calls
        def call(*args,**kwargs):
            calls.append(args)
            if failed_step and '-m' in args and args[args.index('-m')+1]==failed_step:
                return Mock(stdout='TEST-ONLY package stdout\n',stderr='TEST-ONLY package stderr\n',returncode=7)
            if args[:3]==('systemctl','show',module.SERVICE):
                prop=args[3]
                values={'--property=ActiveState':'inactive\n','--property=ExecStart':str(self.dest/'immich_updater.py')+' --state-dir /var/lib/immich-updater/state','--property=User':'root\n','--property=OnFailure':''}
                return Mock(stdout=values[prop],stderr='',returncode=0)
            if args[:2]==('systemctl','is-active'):return Mock(stdout='inactive\n',stderr='',returncode=3)
            if '-c' in args:
                self.assertEqual(kwargs.get('cwd'),str(next((self.root/'opt').glob('immich-updater.staging-*'))))
                self.assertIn(repr(str(self.app)),args[args.index('-c')+1])
            if '--prepare-only' in args:return Mock(stdout=prepared_output,stderr='',returncode=0)
            return Mock(stdout='TEST-ONLY mocked successful command',stderr='',returncode=0)
        real_path=Path
        def path(*values):
            if values==('/opt',):return self.root/'opt'
            if values==('/etc/systemd/system',):return self.root/'systemd'
            return real_path(*values)
        (self.root/'opt').mkdir();(self.root/'systemd').mkdir()
        with patch.multiple(module,call=call,DEST=self.dest,APP=self.app,STATE=self.state,READY=self.ready,prerequisites=Mock(),Path=path),contextlib.redirect_stdout(io.StringIO()):
            module.install()
        return calls
    def test_prepare_exit_zero_but_skip_cannot_swap_code_or_enable_timer(self):
        with self.assertRaises(module.StopInstall):self.install_simulated('{"event":"decision","decision":"skip"}')
        self.assertEqual((self.dest/'old-marker').read_text(),'untouched');self.assertFalse(self.ready.exists())
        self.assertEqual(list((self.root/'systemd').iterdir()),[])
    def test_success_installs_exact_package_and_leaves_timer_disabled(self):
        calls=self.install_simulated(good_receipt())
        self.assertEqual(json.loads(self.ready.read_text())['status'],'prepared')
        self.assertEqual((self.dest/'INSTALLATION_REVISION').read_text().strip(),module.REVISION)
        self.assertTrue(list((self.root/'opt').glob('immich-updater.before-*')))
        self.assertFalse(any(args[:2]==('systemctl','enable') for args in calls))
        self.assertTrue((self.root/'systemd'/'immich-updater.service.d'/'50-rehearsal-state.conf').is_file())

    def test_dependency_failure_prevents_code_swap_and_activation(self):
        self.package_failure('pip','dependency-install.log')

    def test_unit_test_failure_keeps_diagnostics_and_old_installation(self):
        self.package_failure('unittest','package-tests.log')

    def test_venv_failure_keeps_diagnostics_and_old_installation(self):
        self.package_failure('venv','venv-create.log')

    def package_failure(self,step,filename):
        with self.assertRaises(module.StopInstall) as error:
            self.install_simulated(good_receipt(),failed_step=step)
        self.assertEqual((self.dest/'old-marker').read_text(),'untouched')
        self.assertFalse(self.ready.exists())
        self.assertFalse(any('--prepare-only' in args or args[:2]==('systemctl','enable')
                             for args in self.recorded_calls))
        self.assertEqual(list((self.root/'systemd').iterdir()),[])
        log=next((self.root/'opt').glob('immich-updater.staging-*'))/filename
        self.assertEqual(log.read_text(),'TEST-ONLY package stdout\nTEST-ONLY package stderr\n')
        self.assertEqual(log.stat().st_mode&0o777,0o600)
        self.assertIn(str(log),str(error.exception))
        self.assertNotIn('TEST-ONLY package',str(error.exception))

    def test_logged_real_subprocess_failure_is_private_and_preserved(self):
        log=self.root/'command.log'
        script="import sys;print('TEST-ONLY stdout');print('TEST-ONLY stderr',file=sys.stderr);sys.exit(7)"
        with self.assertRaises(module.StopInstall) as error:
            module.logged_call(log,sys.executable,'-I','-S','-c',script,timeout=10)
        self.assertEqual(log.read_text(),'TEST-ONLY stdout\nTEST-ONLY stderr\n')
        self.assertEqual(log.stat().st_mode&0o777,0o600)
        self.assertIn(str(log),str(error.exception));self.assertNotIn('TEST-ONLY',str(error.exception))

    def test_logged_real_subprocess_timeout_preserves_partial_output(self):
        log=self.root/'timeout.log'
        script="import sys,time;print('TEST-ONLY partial stdout',flush=True);print('TEST-ONLY partial stderr',file=sys.stderr,flush=True);time.sleep(30)"
        with self.assertRaises(module.StopInstall) as error:
            module.logged_call(log,sys.executable,'-I','-S','-c',script,timeout=0.5)
        self.assertEqual(log.read_text(),'TEST-ONLY partial stdout\nTEST-ONLY partial stderr\n')
        self.assertEqual(log.stat().st_mode&0o777,0o600)
        self.assertIn(str(log),str(error.exception));self.assertNotIn('TEST-ONLY',str(error.exception))

    def test_revision_format_rejected_before_lookup(self):
        call=Mock()
        with patch.object(module,'call',call),self.assertRaises(module.StopInstall):
            module.source_revision('abc')
        call.assert_not_called()

    def test_revision_mismatch_blocks(self):
        with patch.object(module,'call',Mock(return_value=Mock(stdout='b'*40))),self.assertRaises(module.StopInstall):
            module.source_revision('a'*40)

    def test_missing_package_file_blocks(self):
        with patch.object(module,'SOURCE_ROOT',self.root),self.assertRaises(module.StopInstall):
            module.package()

    def test_activation_other_application_path_blocks(self):
        self.ready_installation()
        data=json.loads(self.ready.read_text());data['app_dir']='/different/deployment'
        self.ready.write_text(json.dumps(data));call=Mock()
        with self.assertRaises(module.StopInstall):self.activate(call)
        call.assert_not_called()

    def test_activation_missing_hashes_blocks(self):
        self.ready_installation()
        data=json.loads(self.ready.read_text());data.pop('files')
        self.ready.write_text(json.dumps(data));call=Mock()
        with self.assertRaises(module.StopInstall):self.activate(call)
        call.assert_not_called()


if __name__=='__main__':unittest.main(verbosity=2)
