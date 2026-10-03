"""Regression checks using test-only HTTP responses and a recording Docker stub."""

import contextlib
import copy
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
        self.extra_releases = []
        self.release_pages = None

    def run_script(self, *args, http_error=False):
        replies = [Mock(), Mock()]
        replies[0].json.return_value = self.current
        replies[1].json.return_value = self.release
        if http_error:
            replies[1].raise_for_status.side_effect = requests.HTTPError('fixture')
        output = io.StringIO()
        def get_response(url, **kwargs):
            if url.endswith('/api/server/version'):
                return replies[0]
            if url.endswith('/releases/latest'):
                return replies[1]  # Reproduce the original latest-only bug.
            if url == 'https://api.github.com/repos/immich-app/immich/releases':
                page_number = kwargs.get('params', {}).get('page', 1)
                pages = self.release_pages or [[self.release, *self.extra_releases]]
                reply = Mock()
                reply.json.return_value = pages[page_number - 1]
                reply.links = {'next': {'url': 'fixture'}} if page_number < len(pages) else {}
                reply.raise_for_status.side_effect = replies[1].raise_for_status.side_effect
                return reply
            raise AssertionError('Unexpected test HTTP URL: ' + url)

        argv = ['immich_updater.py', '--immich-dir', str(self.directory), *args]
        with patch('sys.argv', argv), patch.dict(os.environ, {'IMMICH_DIR': str(self.directory)}), \
                patch('requests.get', side_effect=get_response) as get, \
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

    def release_fixture(self, tag, days_old, **overrides):
        release = copy.deepcopy(self.release)
        release.update(tag_name=tag,
                       published_at=(NOW - datetime.timedelta(days=days_old)).isoformat(),
                       draft=False, prerelease=False)
        release.update(overrides)
        return release

    def test_young_latest_does_not_block_older_eligible_release(self):
        self.release = self.release_fixture('v3.2.4', 1)
        self.extra_releases = [self.release_fixture('v3.2.3', 8)]
        code, _, _ = self.run_script()
        self.assertEqual(code, 0)
        self.assertEqual(self.docker.baked['_env']['IMMICH_VERSION'], 'v3.2.3')
        self.assertIn(b'IMMICH_VERSION=v3.2.3\n', self.env.read_bytes())

    def test_highest_eligible_version_wins_not_date_or_lexical_order(self):
        self.release = self.release_fixture('v3.2.11', 1)
        self.extra_releases = [self.release_fixture('v3.2.9', 8),
                               self.release_fixture('v3.2.10', 9)]
        self.run_script()
        self.assertEqual(self.docker.baked['_env']['IMMICH_VERSION'], 'v3.2.10')

    def test_next_release_updates_only_when_its_own_week_passes(self):
        self.current.update(minor=2, patch=3)
        self.release = self.release_fixture('v3.2.4', 6)
        self.extra_releases = [self.release_fixture('v3.2.3', 10)]
        self.run_script()
        self.assertEqual(self.docker.calls, [])
        self.release['published_at'] = (NOW - datetime.timedelta(days=7)).isoformat()
        self.run_script()
        self.assertEqual(self.docker.baked['_env']['IMMICH_VERSION'], 'v3.2.4')

    def test_one_second_before_seven_days_is_not_eligible(self):
        self.release['published_at'] = (NOW - datetime.timedelta(days=7)
                                       + datetime.timedelta(seconds=1)).isoformat()
        self.run_script()
        self.assertEqual(self.docker.calls, [])

    def test_prerelease_and_draft_do_not_hide_eligible_stable(self):
        self.release = self.release_fixture('v3.3.0-rc.1', 9, prerelease=True)
        self.extra_releases = [self.release_fixture('v3.2.5', 10, draft=True),
                               self.release_fixture('v3.2.3', 8)]
        self.run_script()
        self.assertEqual(self.docker.baked['_env']['IMMICH_VERSION'], 'v3.2.3')

    def test_pagination_can_find_an_older_eligible_release(self):
        self.release_pages = [[self.release_fixture('v3.2.4', 1)],
                              [self.release_fixture('v3.2.3', 8)]]
        self.run_script()
        self.assertEqual(self.docker.baked['_env']['IMMICH_VERSION'], 'v3.2.3')

    def test_empty_release_list_does_not_update(self):
        self.release_pages = [[]]
        code, _, _ = self.run_script()
        self.assertEqual(code, 0)
        self.assertEqual(self.docker.calls, [])

    def test_intermediate_breaking_change_blocks_skipping_that_release(self):
        self.extra_releases = [self.release_fixture('v3.2.0', 9,
                               body='Breaking change: manual configuration.')]
        self.run_script()
        self.assertEqual(self.docker.calls, [])
        self.assertEqual((self.directory / 'BREAKING_CHANGE').read_text(), 'v3.2.0')

    def test_younger_release_warning_does_not_block_eligible_target(self):
        self.extra_releases = [self.release_fixture('v3.3.0', 1,
                               body='Breaking change: future migration.')]
        self.run_script()
        self.assertEqual(self.docker.baked['_env']['IMMICH_VERSION'], 'v3.2.4')

    def test_incomplete_history_fails_closed_at_page_limit(self):
        self.release_pages = [[self.release] for _ in range(11)]
        with self.assertRaisesRegex(RuntimeError, 'exceeds ten pages'):
            self.run_script()
        self.assertEqual(self.docker.calls, [])
        self.assertEqual(self.env.read_bytes(), self.original)


if __name__ == '__main__':
    unittest.main()
