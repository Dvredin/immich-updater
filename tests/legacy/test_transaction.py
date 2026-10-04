"""Historical clone/cold-checkpoint regressions, excluded from installed payload."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from archive import rehearsal, transaction


class CloneFilesystemTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
    def test_copied_file_is_not_hardlinked_to_source(self):
        source=self.root/'source';source.mkdir();(source/'photo').write_bytes(b'original')
        dest=self.root/'copy';rehearsal.clone_private_tree(source,dest)
        self.assertNotEqual((source/'photo').stat().st_ino,(dest/'photo').stat().st_ino)
        (dest/'photo').write_bytes(b'changed');self.assertEqual((source/'photo').read_bytes(),b'original')
    def test_escape_symlink_rejected(self):
        source=self.root/'source';source.mkdir();(source/'escape').symlink_to('/etc')
        with self.assertRaises(rehearsal.RehearsalError):rehearsal.clone_private_tree(source,self.root/'copy')
    def test_source_symlink_rejected(self):
        source=self.root/'source';source.mkdir();link=self.root/'link';link.symlink_to(source)
        with self.assertRaises(rehearsal.RehearsalError):rehearsal.clone_private_tree(link,self.root/'copy')
    def test_overlap_clone_rejected(self):
        source=self.root/'source';source.mkdir()
        with self.assertRaises(rehearsal.RehearsalError):rehearsal.clone_private_tree(source,source/'copy')


class CapacityTests(unittest.TestCase):
    def test_storage_failure_is_preflight_not_partial_data_copy(self):
        with patch('archive.transaction.mounted_roots',return_value=[Path('/synthetic')]),patch('archive.transaction.run',return_value=b'1000000000 synthetic'),patch('archive.transaction.shutil.disk_usage',return_value=Mock(free=1)):
            with self.assertRaises(__import__('archive.resource_policy', fromlist=['ResourceUnavailable']).ResourceUnavailable):transaction.full_checkpoint_capacity({'services':{}},'/synthetic-state')
    def test_sufficient_storage_passes(self):
        with patch('archive.transaction.mounted_roots',return_value=[Path('/synthetic')]),patch('archive.transaction.run',return_value=b'1000 synthetic'),patch('archive.transaction.shutil.disk_usage',return_value=Mock(free=10**12)):
            self.assertGreater(transaction.full_checkpoint_capacity({'services':{}},'/synthetic-state'),1000)


class RecoveryStateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'));self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.root.chmod(0o700)
        self.live=self.root/'live';self.live.mkdir();(self.live/'photo').write_text('BROKEN')
        self.archive=self.root/'snapshot';self.archive.mkdir();(self.archive/'photo').write_text('ORIGINAL')
        self.config=self.root/'compose.json';self.config.write_text('NEW')
        self.saved=self.root/'saved';self.saved.write_text('OLD')
        self.state={'phase':'upgrading','project':'fixture','installed':'v3.1.0','baseline':{},
                    'checkpoint':str(self.root),'old_compose':str(self.config),
                    'roots':[{'live':str(self.live),'archive':str(self.archive),'status':'captured'}],
                    'configurations':[{'path':str(self.config),'saved':str(self.saved),'mode':0o600,'uid':os.getuid(),'gid':os.getgid()}]}
        transaction.state_save(self.root/'transaction.json',self.state)
        self.stack=Mock()
        self.stack.config.return_value={'services':{'database':{'environment':{}}}}
        self.stack.api.return_value={'status':200,'data':{'major':3,'minor':1,'patch':0}}
    def recover(self, **overrides):
        defaults={'Compose':Mock(return_value=self.stack),'stop':Mock(),'runtime_checks':Mock(),'run':Mock(return_value=b'')}
        defaults.update(overrides)
        with patch.multiple(transaction,**defaults),contextlib.redirect_stdout(io.StringIO()):
            return transaction.restore(self.root)
    def test_actual_copy_restores_bytes_and_config_before_start(self):
        self.assertEqual(self.recover(),'restored')
        self.assertEqual((self.live/'photo').read_text(),'ORIGINAL');self.assertEqual(self.config.read_text(),'OLD')
        self.assertFalse((self.root/'transaction.json').exists());self.assertTrue(self.stack.call.called)
    def test_stop_failure_never_copies_or_starts(self):
        with self.assertRaises(rehearsal.RehearsalError):self.recover(stop=Mock(side_effect=rehearsal.RehearsalError('failed stop')))
        self.assertEqual((self.live/'photo').read_text(),'BROKEN');self.stack.call.assert_not_called()
    def test_failed_restore_copy_never_starts_partial_runtime(self):
        with self.assertRaises(OSError):self.recover(clone_private_tree=Mock(side_effect=OSError('disk full')))
        self.stack.call.assert_not_called();self.assertTrue((self.root/'transaction.json').exists())
    def test_failed_restore_verification_never_starts(self):
        with self.assertRaises(rehearsal.RehearsalError):self.recover(tree_hashes=Mock(side_effect=[{'photo':'bad'},{'photo':'good'}]))
        self.stack.call.assert_not_called()
    def test_crash_after_old_directory_rename_recovers(self):
        staged=self.live.with_name(self.live.name+'.restore-'+self.root.name)
        rehearsal.clone_private_tree(self.archive,staged)
        failed=self.live.with_name(self.live.name+'.failed-'+self.root.name)
        os.rename(self.live,failed)
        self.assertEqual(self.recover(),'restored');self.assertEqual((self.live/'photo').read_text(),'ORIGINAL')
    def test_crash_after_staged_rename_before_journal_recovers(self):
        staged=self.live.with_name(self.live.name+'.restore-'+self.root.name);rehearsal.clone_private_tree(self.archive,staged)
        failed=self.live.with_name(self.live.name+'.failed-'+self.root.name);os.rename(self.live,failed);os.rename(staged,self.live)
        self.assertEqual(self.recover(),'restored');self.assertEqual((self.live/'photo').read_text(),'ORIGINAL')
    def test_committed_crash_cleans_journal_without_downgrade(self):
        self.state['phase']='committed';self.state['source']=str(self.config);self.state['target']='v3.2.4'
        self.stack.api.return_value={'status':200,'data':{'major':3,'minor':2,'patch':4}}
        transaction.state_save(self.root/'transaction.json',self.state)
        self.assertEqual(self.recover(),'committed');self.assertTrue(self.stack.call.called);self.assertEqual((self.live/'photo').read_text(),'BROKEN')
    def test_partial_staging_is_archived_and_recopied_before_live_rename(self):
        staged=self.live.with_name(self.live.name+'.restore-'+self.root.name);staged.mkdir();(staged/'photo').write_text('PARTIAL')
        self.assertEqual(self.recover(),'restored');self.assertEqual((self.live/'photo').read_text(),'ORIGINAL')
        self.assertTrue(list(self.root.glob('live.restore-*.incomplete-*')))
    def test_failed_rollback_start_is_stopped_and_next_retry_restores_again(self):
        stop=Mock()
        def corrupt(*args):
            (self.live/'photo').write_text('MUTATED-DURING-FAILED-OLD-START')
            raise rehearsal.RehearsalError('old startup failed')
        with self.assertRaises(rehearsal.RehearsalError):self.recover(runtime_checks=Mock(side_effect=corrupt),stop=stop)
        self.assertEqual(stop.call_count,2)
        self.assertEqual(json.loads((self.root/'transaction.json').read_text())['phase'],'checking_restore')
        self.assertEqual(self.recover(),'restored');self.assertEqual((self.live/'photo').read_text(),'ORIGINAL')
    def test_post_restore_activation_retries_without_erasing_new_writes(self):
        self.state['phase']='rolled_back';self.state['file_bytes_verified']=True
        transaction.state_save(self.root/'transaction.json',self.state)
        (self.live/'new-upload').write_text('NEW ACCEPTED WRITE')
        self.assertEqual(self.recover(),'restored');self.assertEqual((self.live/'photo').read_text(),'BROKEN')
        self.assertEqual((self.live/'new-upload').read_text(),'NEW ACCEPTED WRITE')
    def test_failed_committed_activation_retains_state_and_new_writes(self):
        self.state['phase']='committed';self.state['source']=str(self.config);self.state['target']='v3.2.4'
        transaction.state_save(self.root/'transaction.json',self.state)
        self.stack.call.side_effect=rehearsal.RehearsalError('activation failed')
        with self.assertRaises(rehearsal.RehearsalError):self.recover()
        self.assertEqual((self.live/'photo').read_text(),'BROKEN');self.assertTrue((self.root/'transaction.json').exists())
    def test_failed_old_health_retains_journal(self):
        with self.assertRaises(rehearsal.RehearsalError):self.recover(runtime_checks=Mock(side_effect=rehearsal.RehearsalError('failed health')))
        self.assertTrue((self.root/'transaction.json').exists())
    def test_early_capture_failure_does_not_restore_partial_archives(self):
        self.state['phase']='capturing';transaction.state_save(self.root/'transaction.json',self.state)
        self.assertEqual(self.recover(invariants=Mock(return_value={})),'restored')
        self.assertEqual((self.live/'photo').read_text(),'BROKEN')  # unchanged production, NOT a partial old snapshot


if __name__ == '__main__':
    unittest.main()
