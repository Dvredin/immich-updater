"""Bounded sample copies, real file checks, mocked DB and lifecycle admission."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from archive import sample_rehearsal as samples
from archive.rehearsal import RehearsalError
from archive.resource_policy import ResourceUnavailable


class SampleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'));self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.root.chmod(0o700)
        self.source=self.root/'library';self.source.mkdir();(self.source/'upload').mkdir()
        (self.source/'upload'/'.immich').write_text('SYNTHETIC sentinel')
        self.photo=self.source/'upload'/'one.jpg';self.photo.write_bytes(b'SYNTHETIC jpeg bytes')
        (self.source/'upload'/'not-selected.jpg').write_bytes(b'UNSELECTED synthetic original')
        self.sandbox=self.root/('run-'+'a'*32);self.sandbox.mkdir(mode=0o700)
        self.identifier='00000000-0000-0000-0000-000000000001'
        self.rows=[{'id':self.identifier,'path':'/data/upload/one.jpg'}]
        self.config={'services':{'immich-server':{'volumes':[{'type':'bind','source':str(self.source),'target':'/data'}]},
              'database':{},'redis':{},'immich-machine-learning':{'volumes':[{'type':'volume','source':'cache','target':'/cache'}]}}}
    def build(self):return samples.sampled_mounts(self.config,self.sandbox,self.rows)
    def test_only_selected_files_and_sentinels_are_copied(self):
        mapping,manifest=self.build();dest=mapping[str(self.source)]
        self.assertEqual((dest/'upload'/'one.jpg').read_bytes(),self.photo.read_bytes())
        self.assertNotEqual((dest/'upload'/'one.jpg').stat().st_ino,self.photo.stat().st_ino)
        self.assertFalse((dest/'upload'/'not-selected.jpg').exists())
        self.assertEqual((dest/'upload'/'.immich').read_text(),'SYNTHETIC sentinel')
        self.assertEqual(list(mapping['volume:cache'].iterdir()),[])
        self.assertEqual(manifest['database_scope'],'full');self.assertEqual(manifest['media_scope'],'bounded_sample')
        self.assertEqual(manifest['copied_bytes'],self.photo.stat().st_size)
        (dest/'upload'/'one.jpg').write_bytes(b'CHANGED clone')
        self.assertEqual(self.photo.read_bytes(),b'SYNTHETIC jpeg bytes')
    def test_large_unselected_library_never_walked(self):
        (self.source/'upload'/'huge-video').write_bytes(b'not selected')
        with patch.object(Path,'rglob',side_effect=AssertionError('No full-library traversal')):
            self.build()
    def test_sample_symlink_to_production_or_another_file_is_rejected(self):
        self.photo.unlink();self.photo.symlink_to(self.source/'upload'/'not-selected.jpg')
        with self.assertRaises(RehearsalError):self.build()
    def test_directory_symlink_swap_before_file_open_is_rejected(self):
        alternate=self.root/'outside';alternate.mkdir();(alternate/'one.jpg').write_bytes(b'OUTSIDE must not copy')
        source=samples._safe_source(self.source,'upload/one.jpg')
        old=self.source/'moved';(self.source/'upload').rename(old)
        (self.source/'upload').symlink_to(alternate,target_is_directory=True)
        with self.assertRaises(OSError):
            samples._copy_file(source,self.sandbox/'escaped',1024,confined_root=self.source)
        self.assertFalse((self.sandbox/'escaped').exists())
    def test_path_escape_is_rejected(self):
        self.rows[0]['path']='/data/../outside'
        with self.assertRaises(RehearsalError):self.build()
    def test_unmapped_original_is_not_mounted_from_source(self):
        self.rows[0]['path']='/unmapped/one.jpg'
        with self.assertRaises(RehearsalError):self.build()
    def test_oversized_first_file_can_use_another_bounded_sample(self):
        second=self.source/'upload'/'two.jpg';second.write_bytes(b'x')
        self.rows.append({'id':'00000000-0000-0000-0000-000000000002','path':'/data/upload/two.jpg'})
        with patch.object(samples,'MAX_FILE_BYTES',2):mapping,manifest=self.build()
        self.assertEqual(manifest['assets'][0]['id'],self.rows[1]['id'])
        self.assertFalse((mapping[str(self.source)]/'upload'/'one.jpg').exists())
    def test_no_bounded_original_fails_closed_not_skip_success(self):
        with patch.object(samples,'MAX_BYTES',1),self.assertRaises(RehearsalError):self.build()
    def test_file_count_and_combined_bytes_are_bounded(self):
        self.rows=[]
        for i in range(30):
            name=str(i)+'.jpg';(self.source/'upload'/name).write_bytes(b'xx')
            self.rows.append({'id':f'00000000-0000-0000-0000-{i:012d}','path':'/data/upload/'+name})
        with patch.object(samples,'MAX_BYTES',10):_,manifest=self.build()
        self.assertLessEqual(len(manifest['assets']),samples.MAX_FILES);self.assertEqual(manifest['copied_bytes'],10)
    def test_duplicate_original_path_is_not_copied_twice(self):
        self.rows.append({**self.rows[0],'id':'00000000-0000-0000-0000-000000000002'})
        _,manifest=self.build();self.assertEqual(manifest['copied_files'],1)
    def test_full_database_budget_does_not_depend_on_photo_tree(self):
        db=Mock();db.sql.return_value='1000000'
        with patch.object(samples.shutil,'disk_usage',return_value=Mock(free=2**40)):
            required=samples.capacity(db,self.root)
        self.assertEqual(required,6*1000000+6*samples.MAX_BYTES+512*samples.MIB)
        db.sql.assert_called_once_with('SELECT pg_database_size(current_database());')
    def test_low_sample_storage_defers(self):
        db=Mock();db.sql.return_value='1000000'
        with patch.object(samples.shutil,'disk_usage',return_value=Mock(free=1)),self.assertRaises(ResourceUnavailable):
            samples.capacity(db,self.root)
    def test_unknown_database_size_is_not_accepted(self):
        for raw in ('','-1','bogus','0'):
            with self.subTest(raw=raw),self.assertRaises(RehearsalError):samples.capacity(Mock(sql=Mock(return_value=raw)),self.root)
    def test_candidates_use_captured_snapshot_and_bounded_query(self):
        db=Mock();db.sql.return_value=json.dumps(self.rows)
        self.assertEqual(samples.candidates(db,'postgres','immich','00000001-00000001-1'),self.rows)
        query=db.sql.call_args.args[0]
        self.assertIn("SET TRANSACTION SNAPSHOT '00000001-00000001-1'",query)
        self.assertIn('LIMIT 64',query)
    def test_auth_probe_uses_sample_and_rejects_changed_original(self):
        from archive.rehearsal import functional_checks
        mapping,manifest=self.build();asset=self.identifier
        stack=Mock()
        def api(path,**kwargs):
            if path=='/api/server/version':return {'status':200,'data':{'major':3,'minor':2,'patch':4}}
            if path.endswith('/original'):return {'status':200,'bytes':10,'sha256':'changed-digest'}
            return {'status':200}
        stack.api.side_effect=api
        stack.sql.side_effect=lambda query:'1' if 'count(DISTINCT' in query else '/data/upload/one.jpg'
        stack.call.return_value=b'changed-digest'
        with self.assertRaises(RehearsalError):functional_checks(stack,'v3.2.4','test-only',self.sandbox)
        self.assertTrue(any('/api/assets/'+asset+'/original' in call.args for call in stack.api.call_args_list))

    def test_invalid_snapshot_rejected_before_db_query(self):
        db=Mock()
        with self.assertRaises(RehearsalError):samples.candidates(db,'postgres','immich','unsafe')
        db.sql.assert_not_called()


if __name__=='__main__':unittest.main()
