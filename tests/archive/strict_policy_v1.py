"""Explicitly simulated HTTP evidence and recording Docker; no real deployment."""

import contextlib
import copy
import datetime
import fcntl
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
import sh

import immich_updater as updater
import risk_checks as risk

SCRIPT = Path(updater.__file__)
NOW = datetime.datetime(2026, 10, 2, 12, tzinfo=datetime.timezone.utc)


class FixedDatetime(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz else NOW.replace(tzinfo=None)


class RecordingDocker:
    def __init__(self):
        self.baked = {}
        self.calls = []
        self.fail = ""
        self.images = ""
        self.empty_backup = False

    def bake(self, **kwargs):
        self.baked.update(kwargs)
        return self

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        stage = args[1] if len(args) > 1 else ''
        if self.fail == stage:
            raise sh.ErrorReturnCode_1('fixture docker', b'', b'fixture error SECRET-MUST-NOT-LOG')
        if stage == 'config':
            return self.images or ('ghcr.io/immich-app/immich-server:' + self.baked['_env']['IMMICH_VERSION'] + '\n')
        if stage == 'exec' and not self.empty_backup:
            kwargs['_out'].write(b'-- Test-only simulated PostgreSQL dump\n')
        return ''


def advisory(raw='v3.2.2', patched: object='v3.2.4', severity: object='high', **fields):
    result = {'ghsa_id': 'GHSA-test', 'state': 'published', 'severity': severity,
              'html_url': 'https://github.com/immich-app/immich/security/advisories/GHSA-test',
              'vulnerabilities': [{'package': {'name': 'immich-server', 'ecosystem': 'docker'},
                                   'vulnerable_version_range': raw, 'patched_versions': patched}]}
    result.update(fields)
    return result


def report(number=31871, server='v3.2.4', platform='Server', **fields):
    result = {'number': number, 'title': 'Data loss when merging people', 'state': 'closed',
              'html_url': f'https://github.com/immich-app/immich/issues/{number}',
              'body': f'### The bug\n\nBoth records disappear.\n\n### Version of Immich Server\n\n{server}\n\n### Platform with the issue\n\n- [x] {platform}\n'}
    result.update(fields)
    return result


def comment(body, association='MEMBER'):
    return {'body': body, 'author_association': association,
            'html_url': 'https://github.com/immich-app/immich/issues/31871#issuecomment-test'}


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
        self.release = self.release_fixture('v3.2.4', 8)
        self.extra_releases = [self.release_fixture('v3.2.0', 20), self.release_fixture('v3.1.0', 40)]
        self.release_pages = None
        self.advisories = []
        self.reports = []
        self.comments = []
        self.incomplete_search = False
        self.search_total = None
        self.health_version = {'major': 3, 'minor': 2, 'patch': 4}
        self.health_mismatch = False
        self.docker = RecordingDocker()
        self.policy = self.directory / 'policy.json'
        self.policy.write_text(json.dumps({'schema_version': 1, 'excluded_versions': [], 'issue_reviews': {}}))
        self.compare_status = 'ahead'

    @staticmethod
    def release_fixture(tag, days_old, **fields):
        result = {'tag_name': tag, 'body': 'Bug fixes.', 'draft': False, 'prerelease': False,
                  'published_at': (NOW - datetime.timedelta(days=days_old)).isoformat()}
        result.update(fields)
        return result

    def run_script(self, *args, http_error=False):
        output = io.StringIO()
        reads = 0
        def reply(data, more=False):
            response = Mock()
            response.json.return_value = data
            response.links = {'next': {'url': 'fixture'}} if more else {}
            return response
        def get_response(url, **kwargs):
            nonlocal reads
            params = kwargs.get('params') or {}
            if url.endswith('/api/server/version'):
                reads += 1
                if reads == 1 or self.health_mismatch:
                    return reply(self.current if reads == 1 else self.health_version)
                selected = self.docker.baked['_env']['IMMICH_VERSION']
                parts = [int(v) for v in selected[1:].split('.')]
                return reply(dict(zip(('major', 'minor', 'patch'), parts)))
            if http_error:
                raise requests.HTTPError('test-only unavailable upstream')
            if url == risk.API + '/releases':
                pages = self.release_pages if self.release_pages is not None else [[self.release, *self.extra_releases]]
                page = params.get('page', 1)
                return reply(pages[page - 1], page < len(pages))
            if url == risk.API + '/security-advisories':
                return reply(self.advisories)
            if url == 'https://api.github.com/search/issues':
                return reply({'items': self.reports, 'total_count': len(self.reports) if self.search_total is None else self.search_total,
                              'incomplete_results': self.incomplete_search})
            if '/comments' in url:
                return reply(self.comments)
            if '/compare/' in url:
                return reply({'status': self.compare_status})
            raise AssertionError('Unexpected fixture HTTP URL: ' + url)
        argv = ['immich_updater.py', '--immich-dir', str(self.directory),
                '--policy-file', str(self.policy), *args]
        with patch('sys.argv', argv), patch('requests.get', side_effect=get_response) as get, \
                patch('sh.Command', return_value=self.docker), \
                patch('datetime.datetime', FixedDatetime), patch('time.sleep'), \
                contextlib.redirect_stdout(output):
            try:
                runpy.run_path(str(SCRIPT), run_name='__main__')
            except SystemExit as exc:
                code = exc.code
            else:
                code = 0
        text = output.getvalue()
        events = [json.loads(line) for line in text.splitlines() if line.startswith('{')]
        return code, events, get

    def stages(self):
        return [args[1] for args, _ in self.docker.calls]

    def decision(self, events):
        return next(e for e in reversed(events) if e['event'] == 'decision')

    def test_four_day_old_patch_is_eligible(self):
        self.release = self.release_fixture('v3.2.4', 4)
        code, _, _ = self.run_script()
        self.assertEqual(code, 0)
        self.assertIn('up', self.stages())

    def test_exactly_seven_days_feature_is_eligible(self):
        self.release = self.release_fixture('v3.2.0', 7)
        self.extra_releases = [self.release_fixture('v3.1.0', 40)]
        self.health_version.update(patch=0)
        code, _, _ = self.run_script()
        self.assertEqual(code, 0)
        self.assertEqual(self.stages(), ['config', 'exec', 'pull', 'up'])

    def test_checked_tag_is_passed_to_all_docker_commands(self):
        code, _, _ = self.run_script()
        self.assertEqual(code, 0)
        self.assertEqual(self.docker.baked['_env']['IMMICH_VERSION'], 'v3.2.4')
        self.assertEqual(self.stages(), ['config', 'exec', 'pull', 'up'])

    def test_version_pin_is_persisted_without_changing_other_settings(self):
        self.run_script()
        self.assertEqual(self.env.read_bytes(), b'IMMICH_VERSION=v3.2.4\nUNCHANGED=test-fixture\n')
        self.assertEqual(self.env.stat().st_mode & 0o777, 0o640)
        backups = list(self.directory.glob('.env.before-immich-updater-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), self.original)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)

    def test_dry_run_does_not_modify_files_or_run_docker(self):
        before = sorted(p.name for p in self.directory.iterdir())
        code, events, _ = self.run_script('--dry-run')
        self.assertEqual(code, 0)
        self.assertEqual(self.decision(events)['decision'], 'dry_run')
        self.assertEqual(self.docker.calls, [])
        self.assertEqual(before, sorted(p.name for p in self.directory.iterdir()))
        self.assertEqual(self.env.read_bytes(), self.original)

    def test_major_upgrade_is_automatic_after_seven_days(self):
        self.release = self.release_fixture('v4.0.0', 7)
        self.extra_releases.append(self.release_fixture('v3.9.0', 10))
        self.health_version.update(major=4, minor=0, patch=0)
        code, events, _ = self.run_script()
        self.assertEqual(code, 0)
        self.assertEqual(self.decision(events)['target'], 'v4.0.0')
        self.assertIn('up', self.stages())

    def test_downgrade_stops(self):
        self.current.update(minor=3)
        self.extra_releases.append(self.release_fixture('v3.3.0', 40))
        code, events, _ = self.run_script()
        self.assertEqual(code, 0)
        self.assertEqual(self.decision(events)['target'], None)
        self.assertEqual(self.docker.calls, [])

    def test_prerelease_stops(self):
        self.release['prerelease'] = True
        self.extra_releases = [self.release_fixture('v3.1.0', 40)]
        self.run_script()
        self.assertEqual(self.docker.calls, [])

    def test_breaking_change_dry_run_does_not_write_flag(self):
        self.current.update(minor=2, patch=2)
        self.release['body'] = 'Breaking changes: manual deployment required.'
        _, events, _ = self.run_script('--dry-run')
        self.assertEqual(self.decision(events)['decision'], 'skip')
        self.assertFalse((self.directory / 'BREAKING_CHANGE').exists())
        self.assertEqual(self.docker.calls, [])

    def test_existing_breaking_flag_blocks_but_still_checks_installed_risk(self):
        (self.directory / 'BREAKING_CHANGE').write_text('review required')
        _, events, get = self.run_script()
        self.assertTrue(get.called)
        self.assertTrue(any(e['event'] == 'installed_risk' for e in events))
        self.assertEqual(self.docker.calls, [])

    def test_failed_pull_leaves_env_untouched_and_does_not_restart(self):
        self.docker.fail = 'pull'
        code, events, _ = self.run_script()
        self.assertEqual(code, 1)
        self.assertEqual(self.env.read_bytes(), self.original)
        self.assertEqual(self.stages(), ['config', 'exec', 'pull'])
        self.assertNotIn('SECRET-MUST-NOT-LOG', json.dumps(events))

    def test_http_failure_cannot_trigger_update(self):
        code, events, _ = self.run_script(http_error=True)
        self.assertEqual(code, 1)
        self.assertEqual(events[-1]['decision'], 'fail_closed')
        self.assertEqual(self.docker.calls, [])

    def test_young_latest_does_not_block_older_eligible_release(self):
        self.release = self.release_fixture('v3.2.4', 1)
        self.extra_releases.append(self.release_fixture('v3.2.3', 8))
        self.health_version.update(patch=3)
        _, events, _ = self.run_script()
        self.assertEqual(self.decision(events)['target'], 'v3.2.3')

    def test_highest_eligible_version_wins_not_date_or_lexical_order(self):
        self.release = self.release_fixture('v3.2.11', 1)
        self.extra_releases += [self.release_fixture('v3.2.9', 8), self.release_fixture('v3.2.10', 9)]
        self.health_version.update(patch=10)
        self.run_script()
        self.assertEqual(self.docker.baked['_env']['IMMICH_VERSION'], 'v3.2.10')

    def test_next_release_updates_only_when_its_own_patch_delay_passes(self):
        self.current.update(minor=2, patch=3)
        self.release = self.release_fixture('v3.2.4', 2)
        self.run_script()
        self.assertEqual(self.docker.calls, [])
        self.release = self.release_fixture('v3.2.4', 3)
        self.run_script()
        self.assertEqual(self.docker.baked['_env']['IMMICH_VERSION'], 'v3.2.4')

    def test_one_second_before_three_days_is_not_eligible(self):
        self.current.update(minor=2, patch=3)
        self.release['published_at'] = (NOW - datetime.timedelta(days=3) + datetime.timedelta(seconds=1)).isoformat()
        self.run_script()
        self.assertEqual(self.docker.calls, [])

    def test_prerelease_and_draft_do_not_hide_eligible_stable(self):
        self.release = self.release_fixture('v3.3.0-rc.1', 9, prerelease=True)
        self.extra_releases += [self.release_fixture('v3.2.5', 10, draft=True), self.release_fixture('v3.2.3', 8)]
        self.health_version.update(patch=3)
        self.run_script()
        self.assertEqual(self.docker.baked['_env']['IMMICH_VERSION'], 'v3.2.3')

    def test_pagination_can_find_an_older_eligible_release(self):
        self.release_pages = [[self.release_fixture('v3.2.4', 1)], [self.release_fixture('v3.2.3', 8), *self.extra_releases]]
        self.health_version.update(patch=3)
        self.run_script()
        self.assertEqual(self.docker.baked['_env']['IMMICH_VERSION'], 'v3.2.3')

    def test_empty_release_list_fails_closed_on_missing_installed_scope(self):
        self.release_pages = [[]]
        code, _, _ = self.run_script()
        self.assertEqual(code, 1)
        self.assertEqual(self.docker.calls, [])

    def test_intermediate_breaking_change_blocks_skipping_that_release(self):
        self.extra_releases[0]['body'] = 'Breaking change: manual configuration required.'
        self.run_script()
        self.assertEqual(self.docker.calls, [])

    def test_younger_release_warning_does_not_block_eligible_target(self):
        self.extra_releases.append(self.release_fixture('v3.3.0', 1, body='Breaking change: future migration.'))
        self.run_script()
        self.assertEqual(self.docker.baked['_env']['IMMICH_VERSION'], 'v3.2.4')

    def test_incomplete_history_fails_closed_at_page_limit(self):
        self.release_pages = [[self.release] for _ in range(11)]
        code, events, _ = self.run_script()
        self.assertEqual(code, 1)
        self.assertEqual(events[-1]['decision'], 'fail_closed')
        self.assertEqual(self.docker.calls, [])

    def test_feature_wait_cannot_be_laundered_via_patch(self):
        self.release = self.release_fixture('v3.2.4', 3)
        self.extra_releases[0] = self.release_fixture('v3.2.0', 4)
        _, events, _ = self.run_script()
        self.assertEqual(self.decision(events)['decision'], 'skip')

    def test_future_timestamp_cannot_perform_update(self):
        code, _, get = self.run_script('--as-of', NOW.isoformat())
        self.assertEqual(code, 2)
        get.assert_not_called()
        self.assertEqual(self.docker.calls, [])

    def test_high_advisory_vetoes_old_eligible_release(self):
        self.current.update(minor=2, patch=1)
        self.release = self.release_fixture('v3.2.2', 8)
        self.advisories = [advisory()]
        self.run_script()
        self.assertEqual(self.docker.calls, [])

    def test_withdrawn_advisory_does_not_veto(self):
        self.advisories = [advisory('>=3.0.0', withdrawn_at=NOW.isoformat())]
        self.run_script()
        self.assertIn('up', self.stages())

    def test_unparseable_high_range_blocks(self):
        self.advisories = [advisory('<=latest', patched=None)]
        self.run_script()
        self.assertEqual(self.docker.calls, [])

    def test_low_risk_is_logged_but_does_not_veto(self):
        self.advisories = [advisory('>=3.0.0', patched=None, severity='low')]
        _, events, _ = self.run_script()
        self.assertIn('up', self.stages())
        self.assertTrue(any(e['event'] == 'advisory' and e['status'] == 'affected' for e in events))

    def test_confirmed_installed_vulnerability_can_bypass_delay(self):
        self.current.update(minor=2, patch=2)
        self.release = self.release_fixture('v3.2.4', 0)
        self.advisories = [advisory()]
        _, events, _ = self.run_script()
        self.assertTrue(self.decision(events)['urgent'])
        self.assertIn('up', self.stages())

    def test_urgent_fix_cannot_bypass_serious_issue(self):
        self.current.update(minor=2, patch=2)
        self.release = self.release_fixture('v3.2.4', 0)
        self.advisories = [advisory()]
        self.reports = [report()]
        _, events, _ = self.run_script()
        self.assertEqual(self.decision(events)['decision'], 'skip')
        self.assertEqual(self.docker.calls, [])

    def test_unconfirmed_installed_risk_cannot_bypass_delay(self):
        self.current.update(minor=2, patch=2)
        self.release = self.release_fixture('v3.2.4', 0)
        self.advisories = [advisory('nonsense', patched='v3.2.4')]
        self.run_script()
        self.assertEqual(self.docker.calls, [])

    def test_commit_fix_ancestry_is_checked(self):
        self.advisories = [advisory('> main@4ffa26c9', '> main@4eb1003', 'critical')]
        _, events, get = self.run_script()
        self.assertIn('up', self.stages())
        self.assertTrue(any('/compare/4eb1003...v3.2.4' in c.args[0] for c in get.call_args_list))

    def test_closed_issue_is_not_a_shipped_fix(self):
        self.current.update(minor=2, patch=2)
        self.reports = [report()]
        self.comments = [comment('This might be fixed in the latest release candidate. Please close it for now.')]
        self.run_script()
        self.assertNotIn('up', self.stages())

    def test_main_only_fix_cannot_pass(self):
        self.current.update(minor=2, patch=2)
        self.reports = [report()]
        self.comments = [comment("The fix is in main. It'll release in v3.3.0.")]
        self.run_script()
        self.assertNotIn('up', self.stages())

    def test_published_maintainer_fix_can_pass(self):
        self.reports = [report(server='v3.2.0')]
        self.comments = [comment('Fixed in v3.2.4.')]
        self.run_script()
        self.assertIn('up', self.stages())

    def test_nonmaintainer_claim_cannot_pass(self):
        self.current.update(minor=2, patch=2)
        self.reports = [report()]
        self.comments = [comment('Fixed in v3.2.4.', 'NONE')]
        self.run_script()
        self.assertNotIn('up', self.stages())

    def test_maintainer_not_bug_assessment_can_pass(self):
        self.reports = [report()]
        self.comments = [comment('This is not an Immich issue.')]
        self.run_script()
        self.assertIn('up', self.stages())

    def test_mobile_only_issue_does_not_veto_server(self):
        self.reports = [report(platform='Mobile')]
        self.run_script()
        self.assertIn('up', self.stages())

    def test_reviewed_fix_requires_unchanged_published_evidence(self):
        self.current.update(minor=2, patch=2)
        self.reports = [report()]
        review = {'schema_version': 1, 'excluded_versions': [], 'issue_reviews': {'31871': {
            'fixed_from': 'v3.2.4', 'release_body_sha256': hashlib.sha256(b'Bug fixes.').hexdigest(),
            'evidence_url': 'https://github.com/immich-app/immich/releases/tag/v3.2.4'}}}
        self.policy.write_text(json.dumps(review))
        self.run_script()
        self.assertIn('up', self.stages())
        self.docker.calls.clear()
        self.release['body'] = 'Edited notes.'
        self.run_script()
        self.assertEqual(self.docker.calls, [])

    def test_incomplete_issue_search_fails_closed(self):
        self.incomplete_search = True
        code, _, _ = self.run_script()
        self.assertEqual(code, 1)
        self.assertEqual(self.docker.calls, [])

    def test_issue_declared_count_mismatch_fails_closed(self):
        self.search_total = 1
        code, _, _ = self.run_script()
        self.assertEqual(code, 1)
        self.assertEqual(self.docker.calls, [])

    def test_explicit_exclusion_vetoes(self):
        self.policy.write_text(json.dumps({'schema_version': 1, 'excluded_versions': ['>=3.2.0'], 'issue_reviews': {}}))
        self.run_script()
        self.assertEqual(self.docker.calls, [])

    def test_bad_compose_pin_stops_before_backup_or_pull(self):
        self.docker.images = 'ghcr.io/immich-app/immich-server:release\n'
        code, _, _ = self.run_script()
        self.assertEqual(code, 1)
        self.assertEqual(self.stages(), ['config'])
        self.assertEqual(self.env.read_bytes(), self.original)

    def test_failed_backup_stops_before_pull_and_pin(self):
        self.docker.fail = 'exec'
        code, _, _ = self.run_script()
        self.assertEqual(code, 1)
        self.assertEqual(self.stages(), ['config', 'exec'])
        self.assertEqual(self.env.read_bytes(), self.original)

    def test_empty_backup_stops_before_pull(self):
        self.docker.empty_backup = True
        code, _, _ = self.run_script()
        self.assertEqual(code, 1)
        self.assertNotIn('pull', self.stages())

    def test_successful_backup_is_private_and_readable(self):
        self.run_script()
        backups = list(self.directory.glob('.immich-updater-backups/*/database.sql.gz'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(backups[0].parent.stat().st_mode & 0o777, 0o700)
        with gzip.open(backups[0], 'rb') as source:
            self.assertIn(b'Test-only', source.read())

    def test_failed_start_keeps_pin_and_recovery_marker_without_downgrade(self):
        self.docker.fail = 'up'
        code, _, _ = self.run_script()
        self.assertEqual(code, 1)
        self.assertIn(b'IMMICH_VERSION=v3.2.4', self.env.read_bytes())
        self.assertTrue((self.directory / 'UPDATE_FAILED').exists())
        self.assertEqual(self.stages(), ['config', 'exec', 'pull', 'up'])

    def test_post_start_version_mismatch_is_failure_not_success(self):
        self.health_mismatch = True
        self.health_version = self.current.copy()
        with patch('time.monotonic', side_effect=[0, 1000]):
            code, _, _ = self.run_script()
        self.assertEqual(code, 1)
        self.assertTrue((self.directory / 'UPDATE_FAILED').exists())

    def test_real_sh_backup_transport_preserves_gzip_and_utf8(self):
        import sys
        payload = '-- TEST-ONLY synthetic database output: Привет\n'
        def tiny_command(*args, **kwargs):
            return sh.Command(sys.executable)('-c', 'import sys; sys.stdout.write(' + repr(payload) + ')', **kwargs)
        with contextlib.redirect_stdout(io.StringIO()):
            destination = updater.fresh_backup(self.directory, tiny_command, 'database')
        with gzip.open(destination / 'database.sql.gz', 'rb') as source:
            self.assertEqual(source.read(), payload.encode())

    def test_rc_version_claim_is_not_repaired_into_stable_fix(self):
        self.current.update(minor=2, patch=2)
        self.reports = [report()]
        self.comments = [comment('Fixed in v3.2.4-rc.1.')]
        self.run_script()
        self.assertEqual(self.docker.calls, [])

    def test_quoted_user_claim_is_not_a_maintainer_assessment(self):
        self.current.update(minor=2, patch=2)
        self.reports = [report()]
        self.comments = [comment('> Fixed in v3.2.4.\nWe cannot confirm that yet.')]
        self.run_script()
        self.assertEqual(self.docker.calls, [])

    def test_later_maintainer_reopens_assessment(self):
        self.current.update(minor=2, patch=2)
        self.reports = [report()]
        self.comments = [comment('Fixed in v3.2.4.'), comment('This is still reproducible and not fixed.')]
        self.run_script()
        self.assertEqual(self.docker.calls, [])

    def test_unknown_severity_cannot_authorize_urgent_bypass(self):
        self.current.update(minor=2, patch=2)
        self.release = self.release_fixture('v3.2.4', 0)
        self.advisories = [advisory(severity=None)]
        _, events, _ = self.run_script()
        self.assertEqual(self.decision(events)['decision'], 'skip')
        self.assertEqual(self.docker.calls, [])

    def test_missing_platform_for_found_serious_issue_is_unresolved(self):
        self.current.update(minor=2, patch=2)
        data = report()
        data['body'] = data['body'].split('### Platform with the issue')[0]
        self.reports = [data]
        _, events, _ = self.run_script()
        self.assertEqual(self.decision(events)['decision'], 'skip')
        self.assertEqual(self.docker.calls, [])
        self.assertTrue(any(e['event'] == 'issue' and e['status'] == 'unresolved_report_platform' for e in events))

    def test_rc_server_image_is_rejected_before_backup(self):
        self.docker.images = 'ghcr.io/immich-app/immich-server:v3.2.4-rc.1\n'
        code, _, _ = self.run_script()
        self.assertEqual(code, 1)
        self.assertEqual(self.stages(), ['config'])
        self.assertEqual(self.env.read_bytes(), self.original)

    def test_rc_ml_image_is_rejected_before_backup(self):
        self.docker.images = ('ghcr.io/immich-app/immich-server:v3.2.4\n'
                              'ghcr.io/immich-app/immich-machine-learning:v3.2.4-rc.1\n')
        code, _, _ = self.run_script()
        self.assertEqual(code, 1)
        self.assertEqual(self.stages(), ['config'])

    def test_verified_ml_acceleration_suffix_can_pass(self):
        self.docker.images = ('ghcr.io/immich-app/immich-server:v3.2.4\n'
                              'ghcr.io/immich-app/immich-machine-learning:v3.2.4-cuda\n')
        code, _, _ = self.run_script()
        self.assertEqual(code, 0)
        self.assertIn('up', self.stages())

    def test_old_review_cannot_mask_new_maintainer_reopening(self):
        self.current.update(minor=2, patch=2)
        self.reports = [report()]
        self.comments = [comment('This is still reproducible and not fixed.')]
        self.policy.write_text(json.dumps({'schema_version': 1, 'excluded_versions': [], 'issue_reviews': {'31871': {
            'fixed_from': 'v3.2.4', 'release_body_sha256': hashlib.sha256(b'Bug fixes.').hexdigest(),
            'evidence_url': 'https://github.com/immich-app/immich/releases/tag/v3.2.4'}}}))
        self.run_script()
        self.assertEqual(self.docker.calls, [])

    def test_existing_failure_marker_blocks_next_attempt(self):
        (self.directory / 'UPDATE_FAILED').write_text('v3.2.4')
        self.run_script()
        self.assertEqual(self.docker.calls, [])

    def test_concurrent_manual_run_cannot_mutate(self):
        fd = os.open(self.directory / '.immich-updater.lock', os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        code, events, get = self.run_script()
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]['decision'], 'skip')
        get.assert_not_called()
        self.assertEqual(self.docker.calls, [])


class RangeTests(unittest.TestCase):
    def test_documented_range_spellings(self):
        cases = [('v3.2.2', 'v3.2.2', True), ('< v1.132.0', 'v3.1.0', False),
                 ('>= v1.132.0', 'v3.1.0', True), ('2.6.x', 'v2.6.8', True),
                 ('>=1.2.0, <2.0.0', 'v1.3.0', True)]
        for raw, target, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(risk.spec(raw).match(risk.version(target)), expected)

    def test_malformed_identifiers_are_not_repaired(self):
        for raw in ['3.02.2', 'v3.2.2 trailing', 'v3.2', 'vv3.2.2', 'v3.2.2-rc.1']:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                risk.version(raw)
        for raw in ['<=latest', '>=3.0.0,,<4.0.0', 'v3.02.2']:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                risk.spec(raw)


if __name__ == '__main__':
    unittest.main()
