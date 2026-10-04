"""Pure recording and native Compose parser regressions; never starts containers."""
import copy,json,os,shutil,subprocess,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
import transaction
from rehearsal import Compose,RehearsalError,compose_bytes,run
from simple_update import failure_details

TEMPLATE='''name: immich
services:
  immich-server:
    image: ghcr.io/immich-app/immich-server:${IMMICH_VERSION:-release}
    volumes: ["${UPLOAD_LOCATION}:/data"]
    env_file: [.env]
    ports: ["2283:2283"]
  immich-machine-learning:
    image: ghcr.io/immich-app/immich-machine-learning:${IMMICH_VERSION:-release}
    volumes: ["model-cache:/cache"]
  database:
    image: ghcr.io/immich-app/postgres:14-test-only
    environment:
      POSTGRES_PASSWORD: ${DB_PASSWORD}
      POSTGRES_USER: ${DB_USERNAME}
      POSTGRES_DB: ${DB_DATABASE_NAME}
    volumes: ["${DB_DATA_LOCATION}:/var/lib/postgresql/data"]
  redis:
    image: valkey/valkey:9
volumes:
  model-cache:
'''

class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'));self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.app=self.root/'app';self.app.mkdir();self.path=self.app/'compose.json'
        self.config={'name':'synthetic','services':{
            'immich-server':{'image':'old-server','environment':{'DB_USERNAME':'postgres','DB_DATABASE_NAME':'immich'},'volumes':[{'type':'bind','source':str(self.root/'library'),'target':'/data'}]},
            'immich-machine-learning':{'image':'old-ml','volumes':[]},
            'database':{'image':'old-db','environment':{'POSTGRES_USER':'postgres','POSTGRES_DB':'immich'},'volumes':[{'type':'bind','source':str(self.root/'database'),'target':'/var/lib/postgresql/data'}]},
            'redis':{'image':'old-redis'}},'volumes':{}}
        self.path.write_text(json.dumps(self.config));self.pulls=[]
    def candidate(self):
        original=copy.deepcopy(self.config);real_compose=Compose
        def compose(path,*args,**kwargs):
            if Path(path)==self.path:return Mock(config=Mock(return_value=copy.deepcopy(original)))
            return real_compose(path,*args,**kwargs)
        def run(command,**kwargs):
            if command[:2]==['docker','pull']:self.pulls.append(command[2]);return b''
            raise AssertionError('No unplanned Docker operation: '+command[1])
        response=Mock(content=TEMPLATE.encode())
        with patch('transaction.Compose',side_effect=compose),patch('transaction.run',side_effect=run),patch('transaction.pinned',side_effect=lambda cfg:cfg),patch('transaction.requests.get',return_value=response):
            path=transaction.candidate_config(self.path,'v3.2.4',self.root/'state',rehearsal_capacity=False)
        return json.loads(path.read_text())
    def require_parser(self):
        if not shutil.which('docker'):self.skipTest('Native Compose parser is not installed')
        result=subprocess.run(['docker','compose','version'],capture_output=True,timeout=10)
        if result.returncode:self.skipTest('Native Compose parser is unavailable')
    def test_unsupported_source_fields_rejected_without_pulls(self):
        self.config['services']['immich-server']['privileged']=True
        with self.assertRaises(RehearsalError):self.candidate()
        self.assertEqual(self.pulls,[])
    def test_native_template_ignores_real_literal_settings_and_inherited_pin(self):
        self.require_parser()
        literal="synthetic-${AUDIT_UNSET!}-'$OTHER-кириллица-\\slash"
        self.config['services']['database']['environment']['POSTGRES_PASSWORD']=literal
        self.config['services']['immich-server']['environment']['DB_PASSWORD']=literal
        self.config['services']['immich-server']['volumes'][0]['source']=str(self.root/'фото-$OTHER')
        with patch.dict(os.environ,{'IMMICH_VERSION':'v3.1.0','DB_PASSWORD':'conflicting-test-only-value'}):result=self.candidate()
        self.assertEqual(result['services']['database']['environment']['POSTGRES_PASSWORD'],literal)
        self.assertEqual(result['services']['immich-server']['volumes'],self.config['services']['immich-server']['volumes'])
        self.assertEqual(result['services']['immich-server']['image'],'ghcr.io/immich-app/immich-server:v3.2.4')
    def test_named_volumes_resolve_before_original_mapping_is_copied(self):
        self.require_parser()
        for service,target,name in (('database','/var/lib/postgresql/data','db'),('immich-server','/data','media')):
            self.config['services'][service]['volumes']=[{'type':'volume','source':name,'target':target}]
            self.config['volumes'][name]={'name':'synthetic_'+name}
        result=self.candidate()
        self.assertEqual(result['volumes'],self.config['volumes'])
        for name in ('database','immich-server'):self.assertEqual(result['services'][name]['volumes'],self.config['services'][name]['volumes'])
    def test_no_ports_in_source_does_not_publish_default_port(self):
        self.require_parser();result=self.candidate()
        self.assertNotIn('ports',result['services']['immich-server'])
    def test_loopback_port_mapping_is_preserved(self):
        self.require_parser();self.config['services']['immich-server']['ports']=[{'target':2283,'published':'2283','host_ip':'127.0.0.1','protocol':'tcp'}]
        result=self.candidate();self.assertEqual(result['services']['immich-server']['ports'],self.config['services']['immich-server']['ports'])
    def test_materialized_runtime_and_recovery_keep_literal_dollar_settings(self):
        self.require_parser()
        value="synthetic-${AUDIT_UNSET!}-$OTHER-$$-кириллица-quote'"
        cfg=copy.deepcopy(self.config)
        cfg['services']['database']['environment']['POSTGRES_PASSWORD']=value
        path=self.root/'materialized.json';path.write_bytes(compose_bytes(cfg))
        actual=Compose(path).config()
        self.assertEqual(actual['services']['database']['environment']['POSTGRES_PASSWORD'],value)
    def test_subprocess_failure_is_distinct_and_does_not_publish_argv_or_stderr(self):
        sentinel=b'test-only-private-canary'
        for argv,stderr,operation,hint in ((['docker','compose','config'],b'invalid template '+sentinel,'compose_config','invalid_interpolation'),
            (['docker','pull','private-canary-image'],b'denied '+sentinel,'image_pull','registry_denied'),
            (['docker','compose','exec','database','pg_dump'],b'no space left '+sentinel,'database_dump','disk_full'),
            (['docker','compose','up'],b'permission '+sentinel,'compose_up','permission')):
            with self.subTest(operation=operation),patch('rehearsal.subprocess.run',return_value=subprocess.CompletedProcess(argv,7,b'',stderr)):
                try:run(argv)
                except RehearsalError as error:fields=failure_details(error,'test_phase')
                else:self.fail('Failure was not raised')
            self.assertEqual(fields['operation'],operation);self.assertEqual(fields['exit_status'],7)
            self.assertIn(hint,fields['hints']);self.assertNotIn('canary',json.dumps(fields))
    def test_main_journal_keeps_safe_diagnostics_without_exception_content(self):
        from rehearsal import run as command_run
        import immich_updater as updater
        import contextlib,io
        output=io.StringIO()
        def fault(args):
            args.execution_stage='candidate_preparation'
            command_run(['docker','pull','private-canary-image'])
        with patch('sys.argv',['immich_updater.py','--preflight-only']),patch('immich_updater.run',side_effect=fault),patch('rehearsal.subprocess.run',return_value=subprocess.CompletedProcess([],9,b'',b'denied private-canary-value')),contextlib.redirect_stdout(output):
            self.assertEqual(updater.main(),1)
        record=json.loads(output.getvalue())
        self.assertEqual(record['stage'],'candidate_preparation');self.assertEqual(record['operation'],'image_pull')
        self.assertEqual(record['exit_status'],9);self.assertNotIn('canary',output.getvalue())
    def test_arbitrary_exception_body_is_never_public_diagnostics(self):
        fields=failure_details(RehearsalError('test-only-private-canary'),'candidate_preparation')
        self.assertNotIn('canary',json.dumps(fields));self.assertEqual(fields['error_code'],'validation_failed')

if __name__=='__main__':unittest.main()
