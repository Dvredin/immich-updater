"""Regression checks using test-only HTTP responses and a recording Docker stub."""

import contextlib
import datetime
import io
import os
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
import sh


SCRIPT = Path(__file__).resolve().parents[1] / 'immich_updater.py'
NOW = datetime.datetime(2026, 10, 2, 12, tzinfo=datetime.timezone.utc)


class FixedDatetime(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz else NOW.replace(tzinfo=None)


class RecordingDocker:
    def __init__(self, fail_pull=False):
        self.baked = {}
        self.calls = []
        self.fail_pull = fail_pull

    def bake(self, **kwargs):
        self.baked.update(kwargs)
        return self

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.fail_pull and args == ('compose', 'pull'):
            raise sh.ErrorReturnCode_1('docker compose pull', b'', b'fixture failure')
        return ''


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.env = self.directory / '.env'
        self.original = b'IMMICH_VERSION=release\nUNCHANGED=test-fixture\n'
        self.env.write_bytes(self.original)
        self.env.chmod(0o640)
        self.current = {'major': 3, 'minor': 1, 'patch': 0}
        self.release = {
            'tag_name': 'v3.2.4', 'body': 'Bug fixes.',
            'published_at': (NOW - datetime.timedelta(days=8)).isoformat(),
            'draft': False, 'prerelease': False,
        }
        self.docker = RecordingDocker()

    def run_script(self, *args, http_error=False):
        replies = [Mock(), Mock()]
        replies[0].json.return_value = self.current
        replies[1].json.return_value = self.release
        if http_error:
            replies[1].raise_for_status.side_effect = requests.HTTPError('fixture')
        output = io.StringIO()
        argv = ['immich_updater.py', '--immich-dir', str(self.directory), *args]
        with patch('sys.argv', argv), patch.dict(os.environ, {'IMMICH_DIR': str(self.directory)}), \
                patch('requests.get', side_effect=replies) as get, \
                patch('sh.Command', return_value=self.docker), \
                patch('datetime.datetime', FixedDatetime), \
                contextlib.redirect_stdout(output):
            try:
                runpy.run_path(str(SCRIPT), run_name='__main__')
            except SystemExit as exc:
                code = exc.code
            else:
                code = 0
        return code, output.getvalue(), get

    def test_four_day_old_release_waits_for_seven_days(self):
        self.release['published_at'] = (NOW - datetime.timedelta(days=4)).isoformat()
        code, _, _ = self.run_script()
        self.assertEqual(code, 0)
        self.assertEqual(self.docker.calls, [])
        self.assertEqual(self.env.read_bytes(), self.original)

    def test_exactly_seven_days_is_eligible(self):
        self.release['published_at'] = (NOW - datetime.timedelta(days=7)).isoformat()
        code, _, _ = self.run_script()
        self.assertEqual(code, 0)
        self.assertEqual([args for args, _ in self.docker.calls],
                         [('compose', 'pull'), ('compose', 'up', '-d')])

    def test_checked_tag_is_passed_to_both_docker_commands(self):
        code, _, _ = self.run_script()
        self.assertEqual(code, 0)
        self.assertEqual(self.docker.baked['_env']['IMMICH_VERSION'], 'v3.2.4')
        self.assertEqual(len(self.docker.calls), 2)

    def test_version_pin_is_persisted_without_changing_other_settings(self):
        self.run_script()
        self.assertEqual(self.env.read_bytes(),
                         b'IMMICH_VERSION=v3.2.4\nUNCHANGED=test-fixture\n')
        self.assertEqual(self.env.stat().st_mode & 0o777, 0o640)
        backups = list(self.directory.glob('.env.before-immich-updater-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), self.original)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)

    def test_dry_run_does_not_modify_files_or_run_docker(self):
        code, output, _ = self.run_script('--dry-run')
        self.assertEqual(code, 0)
        self.assertIn('Dry run', output)
        self.assertEqual(self.docker.calls, [])
        self.assertEqual(sorted(p.name for p in self.directory.iterdir()), ['.env'])
        self.assertEqual(self.env.read_bytes(), self.original)

    def test_major_upgrade_stops(self):
        self.release['tag_name'] = 'v4.0.0'
        self.run_script()
        self.assertEqual(self.docker.calls, [])

    def test_downgrade_stops(self):
        self.current.update(minor=3)
        self.run_script()
        self.assertEqual(self.docker.calls, [])
        self.assertEqual(self.env.read_bytes(), self.original)

    def test_prerelease_stops(self):
        self.release['prerelease'] = True
        self.run_script()
        self.assertEqual(self.docker.calls, [])

    def test_breaking_change_dry_run_does_not_write_flag(self):
        self.release['body'] = 'Breaking changes: manual deployment required.'
        code, _, _ = self.run_script('--dry-run')
        self.assertEqual(code, 0)
        self.assertFalse((self.directory / 'BREAKING_CHANGE').exists())
        self.assertEqual(self.docker.calls, [])

    def test_breaking_change_flag_blocks_later_runs_without_network(self):
        self.release['body'] = 'Breaking change: manual deployment required.'
        self.run_script()
        self.assertTrue((self.directory / 'BREAKING_CHANGE').exists())
        _, _, get = self.run_script()
        get.assert_not_called()
        self.assertEqual(self.docker.calls, [])

    def test_failed_pull_leaves_env_untouched_and_does_not_restart(self):
        self.docker.fail_pull = True
        code, _, _ = self.run_script()
        self.assertEqual(code, 1)
        self.assertEqual(self.env.read_bytes(), self.original)
        self.assertEqual([args for args, _ in self.docker.calls], [('compose', 'pull')])

    def test_http_failure_cannot_trigger_update(self):
        with self.assertRaises(requests.HTTPError):
            self.run_script(http_error=True)
        self.assertEqual(self.docker.calls, [])


if __name__ == '__main__':
    unittest.main()
