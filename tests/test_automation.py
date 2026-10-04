"""Current automatic policy and transactional fault regressions (simulated unless stated)."""

import contextlib
import datetime as dt
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import immich_updater as updater
from compose_runtime import UpdaterError
import risk_checks as risk
import transaction


NOW = dt.datetime(2026, 10, 3, 12, tzinfo=dt.timezone.utc)


def release(tag, days, body=""):
    return {
        "tag_name": tag,
        "published_at": (NOW - dt.timedelta(days=days)).isoformat(),
        "draft": False,
        "prerelease": False,
        "body": body,
    }


def advisory(
    raw="v3.2.2", repaired: object = "v3.2.4", severity: object = "high", **extra
):
    return {
        "ghsa_id": "GHSA-test",
        "state": "published",
        "severity": severity,
        "vulnerabilities": [
            {
                "package": {"name": "immich-server"},
                "vulnerable_version_range": raw,
                "patched_versions": repaired,
            }
        ],
        **extra,
    }


class Evidence:
    def __init__(self, releases, advisories=(), contains=True):
        self.releases, self.advisories, self.ancestry = (
            releases,
            list(advisories),
            contains,
        )

    def pages(self, path, **kwargs):
        if path == "/releases":
            return self.releases
        if path == "/security-advisories":
            return self.advisories
        raise AssertionError("Issue searches are not part of the automatic policy.")

    def contains(self, commit, tag):
        return self.ancestry


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)
        self.base = [release("v3.1.0", 40), release("v3.2.0", 20)]

    def choose(self, rows, advisories=(), current=(3, 1, 0), quarantine=None):
        return updater.choose(
            Evidence(self.base + rows, advisories), current, NOW, quarantine
        )

    def test_newest_eligible_not_latest_quiet_period(self):
        self.assertEqual(
            self.choose([release("v3.2.4", 2), release("v3.2.3", 4)]), ("v3.2.3", False)
        )

    def test_numeric_not_lexical_order(self):
        self.assertEqual(
            self.choose([release("v3.2.9", 5), release("v3.2.10", 4)]),
            ("v3.2.10", False),
        )

    def test_patch_exactly_three_days(self):
        self.assertEqual(self.choose([release("v3.2.4", 3)]), ("v3.2.4", False))

    def test_patch_before_three_days(self):
        self.assertEqual(self.choose([release("v3.2.4", 2.999)]), ("v3.2.0", False))

    def test_minor_exactly_seven_days(self):
        self.base = [release("v3.1.0", 40)]
        self.assertEqual(self.choose([release("v3.2.0", 7)]), ("v3.2.0", False))

    def test_minor_before_seven_days(self):
        self.base = [release("v3.1.0", 40)]
        self.assertIsNone(self.choose([release("v3.2.0", 6.999)]))

    def test_major_automatically_seven_days(self):
        self.assertEqual(self.choose([release("v4.0.0", 7)]), ("v4.0.0", False))

    def test_young_feature_base_not_laundered_by_patch(self):
        self.base = [release("v3.1.0", 40)]
        self.assertIsNone(self.choose([release("v3.2.0", 5), release("v3.2.1", 3)]))

    def test_missing_feature_base_is_unresolved(self):
        self.base = [release("v3.1.0", 40)]
        self.assertIsNone(self.choose([release("v3.2.1", 10)]))

    def test_major_base_required(self):
        self.assertEqual(
            self.choose([release("v4.1.0", 20), release("v4.1.1", 10)]),
            ("v3.2.0", False),
        )

    def test_no_downgrade_or_reinstall(self):
        self.assertIsNone(self.choose([release("v3.0.3", 50)], current=(3, 2, 0)))

    def test_prereleases_and_drafts_excluded(self):
        rows = [
            release("v4.0.0-rc.1", 20),
            dict(release("v4.0.0", 20), draft=True),
            dict(release("v5.0.0", 20), prerelease=True),
        ]
        self.assertEqual(self.choose(rows), ("v3.2.0", False))

    def test_malformed_tag_not_normalized(self):
        self.assertIsNone(updater.release_version(release("v03.2.4", 20)))

    def test_breaking_changes_are_not_textual_veto(self):
        self.assertEqual(
            self.choose(
                [
                    release(
                        "v3.2.4",
                        4,
                        "Breaking Changes: iOS 14 removed. Manual migration required.",
                    )
                ]
            ),
            ("v3.2.4", False),
        )

    def test_no_issue_endpoints_or_manual_exceptions(self):
        self.assertFalse(hasattr(risk.GitHub, "reports"))
        self.assertFalse(hasattr(risk, "issue_risks"))

    def test_high_affected_candidate_veto(self):
        self.assertEqual(
            self.choose([release("v3.2.2", 4)], [advisory()]), ("v3.2.0", False)
        )

    def test_critical_affected_candidate_veto(self):
        self.assertEqual(
            self.choose([release("v3.2.2", 4)], [advisory(severity="critical")]),
            ("v3.2.0", False),
        )

    def test_withdrawn_advisory_not_veto(self):
        self.assertEqual(
            self.choose([release("v3.2.2", 4)], [advisory(withdrawn_at="fixture")]),
            ("v3.2.2", False),
        )

    def test_medium_is_logged_not_veto(self):
        self.assertEqual(
            self.choose([release("v3.2.2", 4)], [advisory(severity="medium")]),
            ("v3.2.2", False),
        )

    def test_urgent_confirmed_high_fix_bypasses_age(self):
        self.assertEqual(
            self.choose(
                [release("v3.2.4", 0.1)],
                [advisory(raw=">=3.1.0 <3.2.4", repaired=">=3.2.4")],
            ),
            ("v3.2.4", True),
        )

    def test_unknown_severity_cannot_enable_urgent_bypass(self):
        self.assertEqual(
            self.choose(
                [release("v3.2.4", 0.1)],
                [advisory(raw=">=3.1.0 <3.2.4", repaired=">=3.2.4", severity=None)],
            ),
            None,
        )

    def test_urgent_does_not_override_candidate_security(self):
        rows = [
            advisory(raw=">=3.1.0 <3.2.4", repaired=">=3.2.4"),
            dict(advisory(raw="v3.2.4", repaired=None), ghsa_id="GHSA-other"),
        ]
        self.assertIsNone(self.choose([release("v3.2.4", 0.1)], rows))

    def test_quarantine_allows_new_candidate_not_daily_repeat(self):
        self.assertEqual(
            self.choose(
                [release("v3.2.4", 4), release("v3.2.3", 5)], quarantine={"v3.2.4": {}}
            ),
            ("v3.2.3", False),
        )

    def test_unparseable_serious_range_fail_closed(self):
        self.assertIsNone(
            self.choose(
                [release("v3.2.4", 4)], [advisory(raw="ambiguous", repaired=None)]
            )
        )

    def test_unknown_package_fail_closed(self):
        row = advisory()
        row["vulnerabilities"][0]["package"]["name"] = "unknown"
        self.assertIsNone(self.choose([release("v3.2.4", 4)], [row]))

    def test_commit_qualified_fix_ancestry(self):
        row = advisory(raw="> main@" + "a" * 40, repaired="> main@" + "b" * 40)
        blocks, fixes = risk.advisory_risks(
            Evidence([], contains=True), [row], "v3.2.4"
        )
        self.assertEqual(blocks, [])
        self.assertEqual(fixes, {"GHSA-test"})

    def test_unknown_ancestry_fails_not_safety(self):
        evidence = Evidence([])
        evidence.contains = Mock(side_effect=ValueError("unresolved"))
        row = advisory(raw="> main@" + "a" * 40, repaired="> main@" + "b" * 40)
        self.assertTrue(risk.advisory_risks(evidence, [row], "v3.2.4")[0])

    def test_duplicate_history_rejected(self):
        with self.assertRaises(ValueError):
            updater.load_stable_releases(Evidence([self.base[0], self.base[0]]), NOW)

    def test_naive_timestamp_rejected(self):
        with self.assertRaises(ValueError):
            risk.stamp("2026-10-03T12:00:00")

    def test_range_spellings(self):
        for expression in (">= 3.1.0, < 3.2.4", ">=v3.1.0 <v3.2.4"):
            self.assertTrue(risk.spec(expression).match(risk.version("v3.2.2")))

    def test_empty_range_clause_not_repaired(self):
        with self.assertRaises(ValueError):
            risk.spec(">=3.1.0,,<3.2.4")

    def test_pagination_hard_bound_fail_closed(self):
        gh = risk.GitHub()
        gh.get = Mock(return_value=([], True))
        with self.assertRaises(RuntimeError):
            gh.pages("/releases", limit=2)

    def test_api_failure_propagates_before_implementation(self):
        gh = Evidence([])
        gh.pages = Mock(side_effect=RuntimeError("unavailable"))
        with self.assertRaises(RuntimeError):
            updater.choose(gh, (3, 1, 0), NOW)


class FilesystemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_private_state_permissions(self):
        state = transaction.private_root(self.root / "state")
        self.assertEqual(state.stat().st_mode & 0o777, 0o700)
        public = self.root / "public"
        public.mkdir(mode=0o755)
        # mkdir's requested mode is masked by the installer's private umask.
        public.chmod(0o755)
        self.assertEqual(public.stat().st_mode & 0o777, 0o755)
        with self.assertRaises(UpdaterError):
            transaction.private_root(public)

    def test_private_state_permissions_under_private_umask(self):
        previous = os.umask(0o077)
        try:
            self.test_private_state_permissions()
        finally:
            os.umask(previous)

    def test_state_symlink_rejected(self):
        self.root.joinpath("link").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(UpdaterError):
            transaction.private_root(self.root / "link")

    def test_atomic_journal_roundtrip_mode(self):
        path = self.root / "journal"
        transaction.state_save(path, {"phase": "upgrading"})
        self.assertEqual(json.loads(path.read_text()), {"phase": "upgrading"})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_env_pin_retains_unrelated_fields_mode(self):
        path = self.root / ".env"
        path.write_text("IMMICH_VERSION=release\nOTHER=fixture\n")
        path.chmod(0o640)
        updater.persist_version(self.root, "v3.2.4")
        self.assertEqual(path.read_text(), "IMMICH_VERSION=v3.2.4\nOTHER=fixture\n")
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)


class RuntimeImageTests(unittest.TestCase):
    def test_checkpoint_uses_running_image_not_retargeted_tag(self):
        stack = Mock()
        stack.config.return_value = {
            "name": "fixture",
            "services": {
                name: {"image": "mutable:" + name} for name in transaction.SERVICES
            },
        }
        stack.call.return_value = b"one two three four"
        items = [
            {
                "Image": "sha256:" + "a" * 64,
                "Config": {
                    "Labels": {
                        "com.docker.compose.project": "fixture",
                        "com.docker.compose.service": name,
                    }
                },
            }
            for name in transaction.SERVICES
        ]
        with patch("transaction.run", return_value=json.dumps(items).encode()) as run:
            result = transaction.running_pinned(stack)
        for service in result["services"].values():
            self.assertEqual(service["image"], "sha256:" + "a" * 64)
        self.assertEqual(run.call_args.args[0][:2], ["docker", "inspect"])

    def test_missing_old_service_provenance_blocks(self):
        stack = Mock()
        stack.config.return_value = {"name": "fixture", "services": {}}
        stack.call.return_value = b"one"
        with (
            patch("transaction.run", return_value=json.dumps([]).encode()),
            self.assertRaises(UpdaterError),
        ):
            transaction.running_pinned(stack)


if __name__ == "__main__":
    unittest.main()
