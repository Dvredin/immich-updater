"""Pure isolation and entrypoint ordering tests; no Docker/application side effects."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import immich_updater as updater
import rehearsal


class IsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'));self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.root.chmod(0o700)
        # Pure isolation fixtures must not depend on the installation host's RAM.
        memory=patch('rehearsal.preflight_memory',return_value={'profile':'test-only'})
        memory.start();self.addCleanup(memory.stop)
        for name in ('ensure_parent','release_parent'):
            mock=patch('rehearsal.'+name);mock.start();self.addCleanup(mock.stop)
        parent=patch('rehearsal.parent_slice',return_value='immichupdaterclone'+'a'*32+'.slice')
        parent.start();self.addCleanup(parent.stop)
        self.library=self.root/'files';self.library.mkdir()
        self.config={'services':{
            'immich-server':{'image':'ghcr.io/immich-app/immich-server:v3.1.0','ports':['2283:2283'],
                             'environment':{'SMTP_PASSWORD':'SHOULD-NOT-INHERIT'},
                             'volumes':[{'type':'bind','source':'/fixture-original','target':'/data'}]},
            'immich-machine-learning':{'image':'ghcr.io/immich-app/immich-machine-learning:v3.1.0',
                                       'volumes':[{'type':'volume','source':'model-cache','target':'/cache'}]},
            'database':{'image':'postgres:14','command':['postgres'],'environment':{},'volumes':[{
                'type':'bind','source':'/fixture-database','target':'/var/lib/postgresql/data'}]},
            'redis':{'image':'valkey/valkey:9'}}}
        self.mapping={'/fixture-original':self.library,'volume:model-cache':self.root/'cache'}
    def build(self):
        return rehearsal.build_isolated(self.config,self.root,'v3.2.4',self.mapping,'synthetic-private-password')
    def test_private_network_no_ports_or_outbound_credentials(self):
        result=self.build();network=result['networks']['isolated']
        self.assertTrue(network['internal']);self.assertEqual(network['driver_opts']['com.docker.network.bridge.gateway_mode_ipv4'],'isolated')
        server=result['services']['immich-server'];self.assertNotIn('ports',server)
        self.assertNotIn('SMTP_PASSWORD',server['environment'])
        self.assertEqual(server['image'],'ghcr.io/immich-app/immich-server:v3.2.4')
    def test_all_mounts_are_private_including_model_cache(self):
        for service in self.build()['services'].values():
            for mount in service.get('volumes',[]):
                self.assertEqual(mount['type'],'bind');self.assertIn(self.root,Path(mount['source']).parents)
    def test_missing_private_library_blocks(self):
        del self.mapping['/fixture-original']
        with self.assertRaises(rehearsal.RehearsalError):self.build()
    def test_missing_captured_model_cache_blocks(self):
        del self.mapping['volume:model-cache']
        with self.assertRaises(rehearsal.RehearsalError):self.build()
    def test_privileged_source_blocks(self):
        self.config['services']['immich-server']['privileged']=True
        with self.assertRaises(rehearsal.RehearsalError):self.build()
    def test_host_network_source_blocks(self):
        self.config['services']['immich-server']['network_mode']='host'
        with self.assertRaises(rehearsal.RehearsalError):self.build()
    def test_gpu_device_source_blocks(self):
        self.config['services']['immich-machine-learning']['devices']=['/dev/dri']
        with self.assertRaises(rehearsal.RehearsalError):self.build()
    def test_gpu_variant_not_silently_rehearsed_as_cpu(self):
        self.config['services']['immich-machine-learning']['image']='ghcr.io/immich-app/immich-machine-learning:v3.1.0-openvino'
        with self.assertRaises(rehearsal.RehearsalError):self.build()
    def test_unknown_topology_blocks(self):
        self.config['services']['sidecar']={'image':'fixture'}
        with self.assertRaises(rehearsal.RehearsalError):self.build()
    def test_executable_candidate_commands_and_healthchecks_preserved(self):
        self.config['services']['database']['command']=['postgres','-c','shared_preload_libraries=vchord.so']
        self.config['services']['database']['healthcheck']={'test':['CMD','pg_isready']}
        self.config['services']['immich-server']['entrypoint']=['/bin/sh','/candidate-start.sh']
        result=self.build()
        self.assertEqual(result['services']['database']['command'][:3],self.config['services']['database']['command'])
        self.assertIn('shared_buffers=64MB',result['services']['database']['command'])
        self.assertIn('work_mem=4MB',result['services']['database']['command'])
        self.assertEqual(result['services']['database']['healthcheck'],self.config['services']['database']['healthcheck'])
        self.assertEqual(result['services']['immich-server']['entrypoint'],self.config['services']['immich-server']['entrypoint'])
    def test_overlap_state_rejected_before_any_capture_or_write(self):
        config=copy.deepcopy(self.config);config['services']['immich-server']['volumes'][0]['source']=str(self.library)
        source=Mock();source.config.return_value=config;state=self.library/'updater-state'
        with patch('rehearsal.Compose',return_value=source),self.assertRaises(rehearsal.RehearsalError):
            rehearsal.rehearse(self.root/'unused.yml','v3.2.4',state)
        source.capture_database.assert_not_called();self.assertFalse(state.exists());self.assertEqual(list(self.library.iterdir()),[])
    def test_isolation_failure_after_create_prevents_any_start(self):
        config=copy.deepcopy(self.config);config['services']['immich-server']['volumes'][0]['source']=str(self.library)
        config['services']['immich-machine-learning']['volumes']=[]
        source=Mock();source.config.return_value=config
        def capture(path):
            path.write_bytes(b'TEST-ONLY-CAPTURE')
            (path.parent/'sample-candidates.json').write_text('[]')
            return 'postgres','immich',{}
        source.capture_database.side_effect=capture;clone=Mock()
        with patch('sample_rehearsal.capacity',return_value=1024),patch('rehearsal.Compose',side_effect=[source,clone]),patch('rehearsal.verify_isolation',side_effect=rehearsal.RehearsalError('bad actual network')),self.assertRaises(rehearsal.RehearsalError):
            rehearsal.rehearse(self.root/'unused.yml','v3.2.4',self.root/'runs')
        self.assertEqual([call.args[0] for call in clone.call.call_args_list],['create','down'])
    def test_runtime_host_port_detected(self):
        clone=Mock();clone.call.return_value=b'container';container={'HostConfig':{'PortBindings':{'2283/tcp':[{}]}},'Mounts':[],'NetworkSettings':{'Networks':{}}}
        with patch('rehearsal.run',return_value=json.dumps([container]).encode()),self.assertRaises(rehearsal.RehearsalError):
            rehearsal.verify_isolation(clone,self.root)
    def test_runtime_nonisolated_network_detected(self):
        clone=Mock();clone.call.return_value=b'container';container={'HostConfig':{},'Mounts':[],'NetworkSettings':{'Networks':{'n':{'NetworkID':'id'}}}}
        with patch('rehearsal.run',side_effect=[json.dumps([container]).encode(),json.dumps([{'Internal':True,'Options':{}}]).encode()]),self.assertRaises(rehearsal.RehearsalError):
            rehearsal.verify_isolation(clone,self.root)
    def test_runtime_production_mount_detected(self):
        clone=Mock();clone.call.return_value=b'container';container={'HostConfig':{},'Mounts':[{'Type':'bind','Source':'/fixture-production'}],'NetworkSettings':{'Networks':{}}}
        with patch('rehearsal.run',return_value=json.dumps([container]).encode()),self.assertRaises(rehearsal.RehearsalError):
            rehearsal.verify_isolation(clone,self.root)


class NodeTransportTests(unittest.TestCase):
    def test_real_node_empty_204_transport(self):
        # Real Node execution with explicitly simulated Fetch, no server response.
        import subprocess, shutil, hashlib
        node=shutil.which('node')
        if not node:self.skipTest('Host Node unavailable; real Docker integration covers transport separately.')
        script="globalThis.fetch=async()=>new Response(null,{status:204});\n"+rehearsal.NODE_HTTP
        result=subprocess.run([node,'--input-type=module','-e',script],input=b'{"path":"/test-only","method":"DELETE"}',capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr.decode())
        response=json.loads(result.stdout);self.assertEqual(response['status'],204)
        self.assertEqual(response['bytes'],0);self.assertEqual(response['sha256'],hashlib.sha256(b'').hexdigest())


class EntrypointTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'));self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);(self.root/'compose.yml').write_text('fixture');(self.root/'.env').write_text('IMMICH_VERSION=v3.1.0\n')
        self.args=updater.parser().parse_args(['--immich-dir',str(self.root),'--state-dir',str(self.root/'state')])
        memory=patch('resource_policy.preflight_memory',return_value={'profile':'test-only'});memory.start();self.addCleanup(memory.stop)
        capacity=patch('transaction.full_checkpoint_capacity',return_value=1024);capacity.start();self.addCleanup(capacity.stop)
        compose=patch('rehearsal.Compose',return_value=Mock(config=Mock(return_value={})));compose.start();self.addCleanup(compose.stop)
    def test_missing_full_production_checkpoint_defers_before_candidate_or_rehearsal(self):
        from resource_policy import ResourceUnavailable
        with patch('transaction.restore',return_value=None),patch('immich_updater.current_version',return_value=(3,1,0)),patch('immich_updater.choose',return_value=('v3.2.4',False)),patch('transaction.full_checkpoint_capacity',side_effect=ResourceUnavailable('test-only rollback storage')),patch('transaction.candidate_config') as candidate,patch('rehearsal.rehearse') as rehearse,patch('transaction.apply') as apply,contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(updater.run(self.args),0)
        candidate.assert_not_called();rehearse.assert_not_called();apply.assert_not_called()
        self.assertFalse((self.root/'state'/'quarantine.json').exists())
        self.assertIn('defer_rollback_storage',output.getvalue())

    def test_prepare_only_does_not_require_all_source_photo_copies(self):
        self.args.prepare_only=True
        with patch('transaction.restore',return_value=None),patch('immich_updater.current_version',return_value=(3,1,0)),patch('immich_updater.choose',return_value=('v3.2.4',False)),patch('transaction.full_checkpoint_capacity',side_effect=AssertionError('No production backup for sample testing')),patch('transaction.candidate_config',return_value='private-test-candidate'),patch('rehearsal.rehearse'),patch('recovery_drill.restore_drill'),patch('transaction.apply') as apply,contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(updater.run(self.args),0)
        apply.assert_not_called()

    def test_recovery_precedes_unavailable_source_api(self):
        with patch('transaction.restore',return_value='restored') as restore,patch('immich_updater.current_version',side_effect=RuntimeError('should not run')) as current,contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(updater.run(self.args),0);restore.assert_called_once();current.assert_not_called()
    def test_dry_run_creates_no_private_state_or_docker_work(self):
        self.args.dry_run=True
        with patch('immich_updater.current_version',return_value=(3,1,0)),patch('immich_updater.choose',return_value=('v3.2.4',False)),patch('transaction.candidate_config') as candidate,contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(updater.run(self.args),0);candidate.assert_not_called();self.assertFalse((self.root/'state').exists())
    def test_prepare_only_rehearses_and_drills_without_source_apply(self):
        self.args.prepare_only=True
        with patch('immich_updater.current_version',return_value=(3,1,0)),patch('immich_updater.choose',return_value=('v3.2.4',False)),patch('transaction.restore',return_value=None),patch('transaction.candidate_config',return_value='private-candidate'),patch('rehearsal.rehearse') as rehearse,patch('recovery_drill.restore_drill') as drill,patch('transaction.apply') as apply,contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(updater.run(self.args),0);rehearse.assert_called_once();drill.assert_called_once();apply.assert_not_called()
    def test_rehearsal_failure_quarantines_and_never_applies(self):
        with patch('immich_updater.current_version',return_value=(3,1,0)),patch('immich_updater.choose',return_value=('v3.2.4',False)),patch('transaction.restore',return_value=None),patch('transaction.candidate_config',return_value='private-candidate'),patch('rehearsal.rehearse',side_effect=rehearsal.RehearsalError('failed clone')),patch('transaction.apply') as apply,contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(rehearsal.RehearsalError):updater.run(self.args)
            apply.assert_not_called();self.assertIn('v3.2.4',json.loads((self.root/'state'/'quarantine.json').read_text()))
    def test_prefetch_failure_does_not_permanently_quarantine_release(self):
        with patch('immich_updater.current_version',return_value=(3,1,0)),patch('immich_updater.choose',return_value=('v3.2.4',False)),patch('transaction.restore',return_value=None),patch('transaction.candidate_config',side_effect=RuntimeError('temporary registry failure')),patch('transaction.apply') as apply,contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):updater.run(self.args)
            apply.assert_not_called();self.assertFalse((self.root/'state'/'quarantine.json').exists())
    def test_recovery_drill_failure_never_applies(self):
        with patch('immich_updater.current_version',return_value=(3,1,0)),patch('immich_updater.choose',return_value=('v3.2.4',False)),patch('transaction.restore',return_value=None),patch('transaction.candidate_config',return_value='private-candidate'),patch('rehearsal.rehearse'),patch('recovery_drill.restore_drill',side_effect=rehearsal.RehearsalError('failed recovery')),patch('transaction.apply') as apply,contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(rehearsal.RehearsalError):updater.run(self.args)
            apply.assert_not_called()


if __name__=='__main__':unittest.main()
