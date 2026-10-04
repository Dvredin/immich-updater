"""Compact memory gates with real temporary cgroup files and simulated container metadata."""
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import immich_updater as updater
from archive import resource_policy as resources
from archive import rehearsal


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.proc = self.root / 'proc'; self.proc.mkdir()
        self.cg = self.root / 'cgroups'; self.cg.mkdir()
        self.parent_name='immichupdaterclone'+'a'*32+'.slice'
        self.parent=self.cg/self.parent_name;self.parent.mkdir()
        for name,value in {'memory.max':str(resources.CLONE_BUDGET_MIB*resources.MIB),
                           'memory.swap.max':'0','memory.events':'oom 0\noom_kill 0\n',
                           'memory.current':'1024','memory.peak':'2048'}.items():
            (self.parent/name).write_text(value)
        for name in ('ensure_parent','release_parent','wait_clone_workers'):
            mock=patch('archive.rehearsal.'+name);mock.start();self.addCleanup(mock.stop)
        self.items = []
        for pid, (name, limit) in enumerate(resources.LIMIT_MIB.items(), 200):
            maximum = limit * resources.MIB
            group = self.parent / ('fixture-' + str(pid)); group.mkdir()
            values = {'memory.max': str(maximum), 'memory.swap.max': '0',
                      'memory.events': 'low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n',
                      'memory.current': '1024', 'memory.peak': '2048'}
            for filename, value in values.items(): (group / filename).write_text(value)
            directory = self.proc / str(pid); directory.mkdir()
            (directory / 'cgroup').write_text('0' + ':' + ':' + '/' + self.parent_name + '/fixture-' + str(pid) + '\n')
            self.items.append({'Config': {'Labels': {'com.docker.compose.service': name,
                resources.CLONE_LABEL: 'true'}}, 'HostConfig': {'Memory': maximum,
                'MemorySwap': maximum, 'OomKillDisable': False, 'OomScoreAdj': 1000,
                'RestartPolicy': {'Name': 'no'}, 'CgroupParent':self.parent_name},
                'State': {'Running': True, 'Pid': pid, 'OOMKilled': False}})
        paths = patch.multiple(resources, PROC_ROOT=self.proc, CGROUP_ROOT=self.cg)
        paths.start(); self.addCleanup(paths.stop)
    def verify(self): return resources.verify_containers(self.items)
    def test_preflight_does_not_stop_or_write_source_on_pressure(self):
        source=Mock();source.config.return_value={'services':{}}
        with patch('archive.sample_rehearsal.capacity',return_value=1024),patch('archive.rehearsal.Compose',return_value=source),patch('archive.rehearsal.preflight_memory',side_effect=resources.ResourceUnavailable('test-only low RAM')):
            with self.assertRaises(resources.ResourceUnavailable):rehearsal.rehearse(self.root/'unused.yml','v3.2.4',self.root/'never-created')
        source.capture_database.assert_not_called();source.call.assert_not_called()
        self.assertFalse((self.root/'never-created').exists())

    def test_runtime_rejects_stopped_service_even_if_docker_oom_flag_false(self):
        self.items[0]['State']={'Running':False,'Pid':0,'OOMKilled':False}
        (self.parent/'fixture-200'/'memory.events').write_text('oom 1\noom_kill 1\n')
        with self.assertRaises(resources.ResourceError):resources.verify_containers(self.items,require_running=True)
    def test_resource_check_repeated_after_functional_reads_and_cleanup(self):
        from archive import transaction
        stack=Mock();stack.config.return_value={'services':{
            'database':{'environment':{},'labels':{resources.CLONE_LABEL:'true'}}}}
        stack.path=self.root/'compose.json'
        def inspect(_):return resources.verify_containers(self.items,require_running=True)
        def mutate(*args):
            (self.parent/'fixture-200'/'memory.events').write_text('oom 1\noom_kill 1\n')
            return {'authenticated_reads':True}
        with patch('archive.rehearsal.clone_resources',side_effect=inspect) as inspector,patch('archive.transaction.invariants',return_value={}),patch('archive.transaction.install_local_probe_key',return_value='test-only'),patch('archive.transaction.functional_checks',side_effect=mutate),self.assertRaises(resources.ResourceError):
            transaction.runtime_checks(stack,'v3.2.4',{})
        self.assertEqual(inspector.call_count,2)
        self.assertTrue(stack.sql.called)  # local probe cleanup ran before final acceptance
    def test_clone_transaction_checks_created_containers_before_up(self):
        from archive import transaction
        stack=Mock();stack.path=self.root/('run-'+'a'*32)/'compose.json'
        stack.config.return_value={'services':{'database':{'labels':{resources.CLONE_LABEL:'true'}}}}
        events=[];stack.call.side_effect=lambda *args,**kwargs:events.append(args[0])
        with patch('archive.rehearsal.verify_isolation',side_effect=lambda *args:events.append('inspect')):
            transaction.start_checked(stack,'up','-d','--force-recreate')
        self.assertEqual(events,['create','inspect','up','up','up'])
        self.assertNotIn('--force-recreate',stack.call.call_args.args)
        started=[call.args for call in stack.call.call_args_list if call.args[0]=='up']
        self.assertEqual(started[0][-2:],('database','redis'))
        self.assertEqual(started[1][-1],'immich-server');self.assertIn('--no-deps',started[1])
        self.assertTrue(all('--no-recreate' in args for args in started))
    def test_clone_failed_create_inspection_never_starts(self):
        from archive import transaction
        stack=Mock();stack.path=self.root/('run-'+'a'*32)/'compose.json'
        stack.config.return_value={'services':{'database':{'labels':{resources.CLONE_LABEL:'true'}}}}
        with patch('archive.rehearsal.verify_isolation',side_effect=rehearsal.RehearsalError('test-only bad limit')),self.assertRaises(rehearsal.RehearsalError):
            transaction.start_checked(stack,'up','-d')
        self.assertEqual([call.args[0] for call in stack.call.call_args_list],['create'])
    def test_nested_rollback_configuration_uses_original_rehearsal_root(self):
        from archive import transaction
        sandbox=self.root/('run-'+'b'*32)
        stack=Mock();stack.path=sandbox/'transactions'/'checkpoint-test'/'restore-probation.json'
        stack.config.return_value={'services':{'database':{'labels':{resources.CLONE_LABEL:'true'}}}}
        with patch('archive.rehearsal.verify_isolation') as inspector:
            transaction.start_checked(stack,'up','-d')
        inspector.assert_called_once_with(stack,sandbox)
    def test_clone_without_owned_private_root_refuses_before_creation(self):
        from archive import transaction
        stack=Mock();stack.path=self.root/'owner-compose.json'
        stack.config.return_value={'services':{'database':{'labels':{resources.CLONE_LABEL:'true'}}}}
        with self.assertRaises(rehearsal.RehearsalError):
            transaction.start_checked(stack,'up','-d')
        stack.call.assert_not_called()

    def test_production_start_does_not_recreate_or_apply_clone_policy(self):
        from archive import transaction
        stack=Mock();stack.config.return_value={'services':{'database':{}}}
        with patch('archive.rehearsal.verify_isolation') as inspector:
            transaction.start_checked(stack,'up','-d','--force-recreate')
        inspector.assert_not_called();self.assertEqual(stack.call.call_count,1)
        self.assertEqual(stack.call.call_args.args,('up','-d','--force-recreate'))
    def test_actual_clone_lifecycle_enomem_maps_to_transient_pressure(self):
        result=Mock(returncode=1,stderr=b'OCI runtime create failed: cannot allocate memory PRIVATE_SENTINEL',stdout=b'')
        with patch('archive.rehearsal.subprocess.run',return_value=result):
            with self.assertRaises(resources.ResourceUnavailable) as failure:
                rehearsal.run(['docker','compose','create'],resource_errors_transient=True)
        self.assertNotIn('PRIVATE_SENTINEL',str(failure.exception))
    def test_source_command_enomem_is_not_silent_resource_skip(self):
        result=Mock(returncode=1,stderr=b'OCI runtime create failed: cannot allocate memory',stdout=b'')
        with patch('archive.rehearsal.subprocess.run',return_value=result),self.assertRaises(rehearsal.RehearsalError):
            rehearsal.run(['docker','compose','up'])
    def test_clone_oom_exit_is_not_transient_allocator_error(self):
        result=Mock(returncode=137,stderr=b'container exited with OOMKilled',stdout=b'')
        with patch('archive.rehearsal.subprocess.run',return_value=result),self.assertRaises(rehearsal.RehearsalError):
            rehearsal.run(['docker','compose','up'],resource_errors_transient=True)

    def test_postgres_image_config_and_preload_arguments_preserved(self):
        default=['postgres','-c','config_file=/etc/postgresql/postgresql.conf']
        metadata=[{'Config':{'Cmd':default}}]
        with patch('archive.rehearsal.run',return_value=json.dumps(metadata).encode()):
            command=rehearsal.compact_postgres_command({'image':'sha256:test-only'})
        self.assertEqual(command[:len(default)],default)
        self.assertEqual(command[len(default):],['-c','shared_buffers=64MB','-c','work_mem=4MB','-c','maintenance_work_mem=64MB'])
    def test_unknown_postgres_startup_command_is_not_replaced(self):
        with self.assertRaises(rehearsal.RehearsalError):
            rehearsal.compact_postgres_command({'command':['custom-wrapper','postgres']})
    def test_explicit_postgres_arguments_are_copied_without_mutating_template(self):
        service={'command':['postgres','-c','shared_preload_libraries=vchord.so']}
        before=copy.deepcopy(service)
        command=rehearsal.compact_postgres_command(service)
        self.assertEqual(service,before);self.assertEqual(command[:3],before['command'])

    def test_staged_start_preserves_stock_services_and_limits_startup_overlap(self):
        stack=Mock()
        rehearsal.start_clone_staged(stack)
        calls=[call.args for call in stack.call.call_args_list]
        self.assertEqual(len(calls),3)
        self.assertTrue(all('--no-recreate' in call for call in calls))
        self.assertEqual(calls[0][-2:],('database','redis'))
        self.assertEqual(calls[1][-1],'immich-server')
        self.assertIn('--no-deps',calls[1])
        self.assertIn('--wait',calls[2])
    def test_staged_server_failure_does_not_start_ml_or_accept_partial_runtime(self):
        stack=Mock();stack.call.side_effect=[b'',rehearsal.RehearsalError('test-only server failure')]
        with self.assertRaises(rehearsal.RehearsalError):rehearsal.start_clone_staged(stack)
        self.assertEqual(stack.call.call_count,2)
        self.assertFalse(any('immich-machine-learning' in call.args for call in stack.call.call_args_list))

    def test_stock_worker_initialization_is_required_before_ml_start(self):
        # Run the actual bounded log readiness function, not its setUp double.
        import importlib
        spec=importlib.util.spec_from_file_location('test_readiness_module',rehearsal.__file__)
        native=importlib.util.module_from_spec(spec);spec.loader.exec_module(native)
        stack=Mock();stack.call.side_effect=[b'HTTP healthy but geodata still importing',b'Immich Microservices is running [v3.2.4]']
        with patch.object(native.time,'sleep') as pause:
            self.assertTrue(native.wait_clone_workers(stack,timeout=5))
        self.assertEqual(stack.call.call_count,2);pause.assert_called_once_with(1)
    def test_missing_worker_bootstrap_refuses_readiness(self):
        spec=importlib.util.spec_from_file_location('test_readiness_absent',rehearsal.__file__)
        native=importlib.util.module_from_spec(spec);spec.loader.exec_module(native)
        stack=Mock();stack.call.return_value=b'HTTP healthy only'
        with patch.object(native.time,'monotonic',side_effect=[0,1,6]),patch.object(native.time,'sleep'),self.assertRaises(native.RehearsalError):
            native.wait_clone_workers(stack,timeout=5)
        self.assertEqual(stack.call.call_count,1)

    def test_kernel_documented_transient_historical_peak_is_recorded_not_oom(self):
        transient=resources.CLONE_BUDGET_MIB*resources.MIB+4096
        (self.parent/'memory.peak').write_text(str(transient))
        result=self.verify()
        self.assertEqual(result['parent']['peak_bytes'],transient)
        self.assertEqual(result['parent']['oom_events'],0)
    def test_current_usage_over_budget_still_rejects_acceptance(self):
        (self.parent/'memory.current').write_text(str(resources.CLONE_BUDGET_MIB*resources.MIB+4096))
        with self.assertRaises(resources.ResourceError):self.verify()

    def test_unlimited_shared_parent_blocks_even_with_correct_children(self):
        (self.parent/'memory.max').write_text('max')
        with self.assertRaises(resources.ResourceError):self.verify()
    def test_parent_oom_cannot_be_erased_by_fresh_child_cgroups(self):
        (self.parent/'memory.events').write_text('oom 1\noom_kill 1\n')
        with self.assertRaises(resources.ResourceError):self.verify()
    def test_shared_parent_swap_must_also_be_zero(self):
        (self.parent/'memory.swap.max').write_text('max')
        with self.assertRaises(resources.ResourceError):self.verify()
    def test_process_outside_shared_parent_refuses_acceptance(self):
        (self.proc/'200'/'cgroup').write_text('0'+':'+':/outside-scope\n')
        with self.assertRaises(resources.ResourceError):self.verify()
    def test_missing_or_system_parent_identity_refused(self):
        for name in (None,'system.slice','../../system.slice','immichupdatercloneINVALID.slice'):
            with self.subTest(name=name),self.assertRaises(resources.ResourceError):resources.parent_path(name)
    def test_lab_parent_nests_pool_below_outer_test_slice(self):
        with patch.object(resources,'LAB_PARENT','immichupdatertestfixture.slice'):
            name=resources.parent_slice(self.root/('run-'+'b'*32))
        self.assertEqual(name,'immichupdatertestfixture-immichupdaterclone'+'b'*32+'.slice')
        self.assertEqual(resources.parent_path(name),self.cg/'immichupdatertestfixture.slice'/name)
    def pool_config(self):
        return {'services':{name:{'labels':{resources.CLONE_LABEL:'true'},'cgroup_parent':self.parent_name} for name in resources.LIMIT_MIB}}
    def test_existing_pool_limits_or_oom_not_silently_changed(self):
        (self.parent/'memory.max').write_text('max')
        with patch.object(resources,'_systemctl') as control,self.assertRaises(resources.ResourceError):
            resources.ensure_parent(self.pool_config())
        control.assert_not_called()
    def test_parent_teardown_refuses_any_remaining_process_before_stop(self):
        (self.parent/'cgroup.procs').write_text('123\n')
        with patch.object(resources,'_systemctl') as control,self.assertRaises(resources.ResourceError):
            resources.release_parent(self.pool_config())
        control.assert_not_called()
    def test_parent_teardown_stops_only_the_empty_owned_pool(self):
        (self.parent/'cgroup.procs').write_text('')
        with patch.object(resources,'_systemctl') as control:
            resources.release_parent(self.pool_config())
        control.assert_called_once_with('stop',self.parent_name)
    def test_mixed_production_services_cannot_configure_a_pool(self):
        cfg=self.pool_config();cfg['services']['database']['labels']={}
        with patch.object(resources,'_systemctl') as control,self.assertRaises(resources.ResourceError):
            resources.ensure_parent(cfg)
        control.assert_not_called()

    def test_exact_total_compact_budget(self):
        self.assertEqual(resources.CLONE_BUDGET_MIB,1728)
        self.assertEqual(resources.HOST_RESERVE_MIB,256)
        self.assertEqual(resources.PROFILE,'compact-4g-v3')
        self.assertGreater(sum(resources.LIMIT_MIB.values()),resources.CLONE_BUDGET_MIB)
        self.assertEqual(resources.preflight_memory(2134 * resources.MIB)['required_available_MiB'],1984)
    def test_observed_owner_available_memory_passes_with_reserve_intact(self):
        for available in (2053,2058):
            result=resources.preflight_memory(available * resources.MIB)
            self.assertEqual(result['clone_budget_MiB'],1728)
            self.assertEqual(result['host_reserve_MiB'],256)
            self.assertEqual(result['required_available_MiB'],1984)
    def test_exact_available_boundary_passes(self):
        self.assertEqual(resources.preflight_memory(1984 * resources.MIB)['available_memory_MiB'],1984)
    def test_one_byte_under_boundary_defers(self):
        with self.assertRaises(resources.ResourceUnavailable): resources.preflight_memory(1984 * resources.MIB - 1)
    def test_real_meminfo_parse(self):
        (self.proc / 'meminfo').write_text('MemTotal: 4000000 kB\nMemAvailable: 2185216 kB\n')
        self.assertEqual(resources.memory_available(),2185216 * 1024)
    def test_missing_available_is_not_inferred_from_free_or_swap(self):
        (self.proc / 'meminfo').write_text('MemFree: 9000000 kB\nSwapFree: 9000000 kB\n')
        with self.assertRaises(resources.ResourceUnavailable): resources.memory_available()
    def test_all_settings_disable_swap_and_prioritize_clone_as_oom_victim(self):
        for name in resources.LIMIT_MIB:
            settings = resources.settings(name)
            self.assertEqual(settings['mem_limit'], settings['memswap_limit'])
            self.assertEqual(settings['oom_score_adj'],1000)
            self.assertFalse(settings['oom_kill_disable'])
    def test_unknown_service_no_fallback_budget(self):
        with self.assertRaises(resources.ResourceError): resources.settings('unknown')
    def test_running_kernel_limits_and_counters_verified(self):
        result = self.verify()
        self.assertEqual(result['total_limit_bytes'],1728 * resources.MIB)
        self.assertTrue(result['swap_disabled'])
        self.assertTrue(all(row['kernel']['oom_events']==0 for row in result['services'].values()))
    def test_create_time_verification_does_not_read_nonexistent_pid(self):
        for item in self.items: item['State']={'Running':False,'Pid':0,'OOMKilled':False}
        self.assertTrue(self.verify()['swap_disabled'])
    def test_uncapped_host_config_blocks_before_start(self):
        self.items[0]['HostConfig']['Memory']=0
        with self.assertRaises(resources.ResourceError): self.verify()
    def test_default_double_swap_budget_is_rejected(self):
        self.items[0]['HostConfig']['MemorySwap'] *= 2
        with self.assertRaises(resources.ResourceError): self.verify()
    def test_kernel_ignoring_swap_policy_is_rejected(self):
        (self.parent/'fixture-200'/'memory.swap.max').write_text('max')
        with self.assertRaises(resources.ResourceError): self.verify()
    def test_kernel_ignoring_ram_policy_is_rejected(self):
        (self.parent/'fixture-200'/'memory.max').write_text('max')
        with self.assertRaises(resources.ResourceError): self.verify()
    def test_child_oom_rejected_even_with_running_container(self):
        (self.parent/'fixture-200'/'memory.events').write_text('oom 1\noom_kill 1\n')
        self.assertFalse(self.items[0]['State']['OOMKilled'])
        with self.assertRaises(resources.ResourceError): self.verify()
    def test_docker_oomkilled_is_rejected_even_without_pid(self):
        self.items[0]['State']={'Running':False,'Pid':0,'OOMKilled':True}
        with self.assertRaises(resources.ResourceError): self.verify()
    def test_missing_oom_counter_cannot_pass(self):
        (self.parent/'fixture-200'/'memory.events').write_text('high 0\n')
        with self.assertRaises(resources.ResourceError): self.verify()
    def test_unreadable_or_nonlocal_pid_cannot_pass(self):
        self.items[0]['State']['Pid']=9999999
        with self.assertRaises(resources.ResourceError): self.verify()
    def test_v1_cgroup_cannot_silently_fall_back(self):
        (self.proc/'200'/'cgroup').write_text('4:memory:/test-only\n')
        with self.assertRaises(resources.ResourceError): self.verify()
    def test_incomplete_service_set_rejected(self):
        self.items.pop()
        with self.assertRaises(resources.ResourceError): self.verify()
    def test_production_container_rejected_by_clone_resource_inspector(self):
        self.items[0]['Config']['Labels'].pop(resources.CLONE_LABEL)
        with self.assertRaises(resources.ResourceError): self.verify()
    def test_restart_policy_cannot_erase_failed_clone_evidence(self):
        self.items[0]['HostConfig']['RestartPolicy']['Name']='always'
        with self.assertRaises(resources.ResourceError): self.verify()
    def test_missing_clone_oom_priority_rejected(self):
        self.items[0]['HostConfig']['OomScoreAdj']=0
        with self.assertRaises(resources.ResourceError): self.verify()
    def test_peak_can_be_missing_but_oom_counters_must_exist(self):
        (self.parent/'fixture-200'/'memory.peak').unlink()
        self.assertIsNone(self.verify()['services']['immich-server']['kernel']['peak_bytes'])


# Retired rehearsal admission entrypoint: archive/rehearsal-resource-entrypoint-v1.py.

if __name__ == '__main__': unittest.main()
