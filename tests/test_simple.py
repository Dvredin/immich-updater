"""Single-stack safety regressions; recording Docker doubles, never a deployment."""

import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import immich_updater as updater
import simple_update as simple
from compose_runtime import UpdaterError


class SingleStackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.getenv("TMPDIR"))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = self.root / "app"
        self.app.mkdir()
        self.path = self.app / "compose.json"
        self.path.write_text("{}")
        (self.app / ".env").write_text("IMMICH_VERSION=v3.1.0\n")
        self.state = self.root / "state"
        self.config = {
            "name": "synthetic",
            "services": {
                name: {"image": "sha256:" + "a" * 64, "volumes": []}
                for name in (
                    "database",
                    "redis",
                    "immich-server",
                    "immich-machine-learning",
                )
            },
        }
        self.config["services"]["database"]["environment"] = {
            "POSTGRES_USER": "postgres",
            "POSTGRES_DB": "immich",
        }
        self.candidate = self.root / "candidate.json"
        self.candidate.write_text(json.dumps(self.config))
        self.stack = Mock()
        self.stack.config.return_value = copy.deepcopy(self.config)
        self.stack.call.return_value = b""
        self.stack.sql.return_value = "1024"
        self.receipt = {
            "profile": simple.PROFILE,
            "required_backup_free_bytes": simple.MIB,
            "available_bytes": 10 * simple.MIB,
            "source_mutations": False,
            "parallel_rehearsal": False,
            "photo_copy_required": False,
            "runtime": {"version": "v3.1.0", "ping": True, "services_running": True},
        }

    def apply(self, dump=None, fault=None):
        if dump is None:

            def synthetic_dump(stack, path):
                Path(path).write_bytes(b"PGDMP test-only archive")
                Path(path).chmod(0o600)

            dump = synthetic_dump

        def healthy(stack, selected=None, expected_images=None, **kwargs):
            return {"version": selected or "v3.1.0", "ping": True}

        with (
            patch("simple_update.Compose", return_value=self.stack),
            patch("simple_update.runtime_config", return_value=self.config),
            patch("simple_update.preflight", return_value=self.receipt),
            patch("simple_update.running_pinned", return_value=self.config),
            patch("simple_update.dump_database", side_effect=dump),
            patch("simple_update.healthy", side_effect=healthy),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            return simple.apply(
                self.path,
                self.candidate,
                "v3.2.4",
                "v3.1.0",
                self.state,
                failure_hook=fault,
            )

    def test_success_has_fresh_dump_config_backup_and_no_photo_copy(self):
        result = self.apply()
        self.assertTrue(result["backup_verified"])
        self.assertFalse(result["media_backup"])
        self.assertFalse((self.state / simple.JOURNAL).exists())
        self.assertEqual((self.app / ".env").read_text(), "IMMICH_VERSION=v3.2.4\n")
        self.assertEqual(len(result["configurations"]), 2)
        self.assertEqual(
            (Path(result["backup"]) / "database.dump").stat().st_mode & 0o777, 0o600
        )

    def test_failed_backup_never_starts_target_or_changes_config(self):
        before = self.path.read_bytes()
        with self.assertRaises(UpdaterError):
            self.apply(dump=Mock(side_effect=UpdaterError("test-only dump failure")))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual((self.app / ".env").read_text(), "IMMICH_VERSION=v3.1.0\n")
        self.assertFalse((self.state / simple.JOURNAL).exists())

    def test_runtime_compose_is_private_even_if_source_was_public(self):
        self.path.chmod(0o644)
        self.apply()
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_actual_mount_drift_denied_before_api(self):
        self.config["services"]["database"]["volumes"] = [
            {
                "type": "bind",
                "source": str(self.root / "new-db"),
                "target": "/var/lib/postgresql/data",
            }
        ]
        items = []
        for name, service in self.config["services"].items():
            items.append(
                {
                    "Config": {
                        "Labels": {
                            "com.docker.compose.project": "synthetic",
                            "com.docker.compose.service": name,
                        },
                        "Env": [
                            k + "=" + str(v)
                            for k, v in service.get("environment", {}).items()
                        ],
                    },
                    "State": {"Running": True},
                    "Mounts": [
                        {
                            "Type": "bind",
                            "Source": str(self.root / "old-db"),
                            "Destination": "/var/lib/postgresql/data",
                            "RW": True,
                        }
                    ]
                    if name == "database"
                    else [],
                }
            )
        self.stack.config.return_value = self.config
        self.stack.call.return_value = b"test-only-id"
        with patch("simple_update.run", return_value=json.dumps(items).encode()):
            with self.assertRaises(UpdaterError):
                simple.healthy(self.stack)
        self.stack.api.assert_not_called()

    def test_actual_matching_bind_and_named_mounts_pass(self):
        cfg = copy.deepcopy(self.config)
        cfg["volumes"] = {"db": {"name": "synthetic-db"}}
        cfg["services"]["database"]["volumes"] = [
            {"type": "volume", "source": "db", "target": "/var/lib/postgresql/data"}
        ]
        cfg["services"]["immich-server"]["volumes"] = [
            {
                "type": "bind",
                "source": str(self.root / "library"),
                "target": "/data",
                "read_only": True,
            }
        ]
        simple.verify_mounts(
            cfg,
            "database",
            {
                "Mounts": [
                    {
                        "Type": "volume",
                        "Name": "synthetic-db",
                        "Destination": "/var/lib/postgresql/data",
                        "RW": True,
                    }
                ]
            },
        )
        simple.verify_mounts(
            cfg,
            "immich-server",
            {
                "Mounts": [
                    {
                        "Type": "bind",
                        "Source": str(self.root / "library"),
                        "Destination": "/data",
                        "RW": False,
                    }
                ]
            },
        )
        with self.assertRaises(UpdaterError):
            simple.verify_mounts(
                cfg,
                "immich-server",
                {
                    "Mounts": [
                        {
                            "Type": "bind",
                            "Source": str(self.root / "library"),
                            "Destination": "/data",
                            "RW": True,
                        }
                    ]
                },
            )

    def test_interruption_keeps_application_marker_and_records_mutation_boundary(self):
        with self.assertRaises(KeyboardInterrupt):
            self.apply(fault=Mock(side_effect=KeyboardInterrupt()))
        self.assertTrue((self.app / simple.APPLICATION_MARKER).exists())
        state = json.loads((self.state / simple.JOURNAL).read_text())
        self.assertTrue(state["diagnostic"]["mutation_started"])
        self.assertEqual(state["phase"], "needs_attention")
        with self.assertRaises(simple.NeedsAttention):
            simple.preflight(self.path, self.root / "alternate")

    def test_runtime_named_volume_identity_drift_is_denied(self):
        self.config["services"]["database"]["volumes"] = [
            {"type": "volume", "source": "db", "target": "/var/lib/postgresql/data"}
        ]
        self.config["volumes"] = {"db": {"name": "expected-db"}}
        items = []
        for name, service in self.config["services"].items():
            items.append(
                {
                    "Config": {
                        "Labels": {
                            "com.docker.compose.project": "synthetic",
                            "com.docker.compose.service": name,
                        },
                        "Env": [
                            k + "=" + str(v)
                            for k, v in service.get("environment", {}).items()
                        ],
                    },
                    "State": {"Running": True},
                    "Mounts": [
                        {
                            "Type": "volume",
                            "Name": "actual-different-db",
                            "Source": "/unused",
                            "Destination": "/var/lib/postgresql/data",
                            "RW": True,
                        }
                    ]
                    if name == "database"
                    else [],
                }
            )
        self.stack.config.return_value = self.config
        self.stack.call.return_value = b"test-only-id"
        with patch("simple_update.run", return_value=json.dumps(items).encode()):
            with self.assertRaises(UpdaterError):
                simple.healthy(self.stack)

    def test_candidate_added_port_never_stops_source(self):
        cfg = copy.deepcopy(self.config)
        cfg["services"]["immich-server"]["ports"] = [
            {"target": 2283, "published": "2283"}
        ]
        self.candidate.write_text(json.dumps(cfg))
        with self.assertRaises(UpdaterError):
            self.apply()
        self.stack.call.assert_not_called()

    def test_failed_target_retains_backup_and_blocks_repeat_without_downgrade(self):
        with self.assertRaises(UpdaterError):
            self.apply(fault=Mock(side_effect=UpdaterError("test-only failed check")))
        journal = json.loads((self.state / simple.JOURNAL).read_text())
        self.assertEqual(journal["phase"], "needs_attention")
        self.assertTrue(journal["backup_verified"])
        self.assertFalse(journal["automatic_downgrade"])
        with self.assertRaises(simple.NeedsAttention):
            simple.preflight(self.path, self.state)
        self.assertEqual((self.app / ".env").read_text(), "IMMICH_VERSION=v3.1.0\n")

    def test_failure_before_writer_stop_does_not_mutate_config(self):
        self.stack.call.side_effect = UpdaterError("test-only failed stop")
        with self.assertRaises(UpdaterError):
            self.apply()
        self.assertEqual(self.path.read_text(), "{}")

    def test_low_backup_space_never_stops_source(self):
        with (
            patch("simple_update.Compose", return_value=self.stack),
            patch("simple_update.shutil.disk_usage", return_value=Mock(free=1)),
        ):
            with self.assertRaises(UpdaterError):
                simple.preflight(self.path, self.state)
        self.stack.call.assert_not_called()

    def test_capacity_is_database_only_with_no_media_walk(self):
        with patch("simple_update.shutil.disk_usage", return_value=Mock(free=10**9)):
            receipt = simple.backup_capacity(self.stack, self.state)
        self.assertEqual(
            receipt["required_backup_free_bytes"], 2 * 1024 + 64 * simple.MIB
        )
        self.assertFalse(receipt["photo_copy_required"])

    def test_named_volume_state_overlap_denied(self):
        cfg = copy.deepcopy(self.config)
        cfg["services"]["database"]["volumes"] = [
            {"type": "volume", "source": "db", "target": "/var/lib/postgresql/data"}
        ]
        cfg["volumes"] = {"db": {"name": "synthetic_db"}}
        with patch(
            "simple_update.run",
            return_value=json.dumps([{"Mountpoint": str(self.root)}]).encode(),
        ):
            with self.assertRaises(UpdaterError):
                simple.location(cfg, self.state)

    def test_bind_state_overlap_denied(self):
        cfg = copy.deepcopy(self.config)
        cfg["services"]["immich-server"]["volumes"] = [
            {"type": "bind", "source": str(self.root), "target": "/data"}
        ]
        with self.assertRaises(UpdaterError):
            simple.location(cfg, self.state)

    def test_candidate_changed_mounts_never_stops(self):
        cfg = copy.deepcopy(self.config)
        cfg["services"]["database"]["volumes"] = [
            {
                "type": "bind",
                "source": "/synthetic-only",
                "target": "/var/lib/postgresql/data",
            }
        ]
        self.candidate.write_text(json.dumps(cfg))
        with self.assertRaises(UpdaterError):
            self.apply()
        self.stack.call.assert_not_called()

    def test_candidate_top_level_volume_identity_swap_never_stops(self):
        self.config["volumes"] = {
            "data": {"name": "captured-source-volume", "external": True}
        }
        cfg = copy.deepcopy(self.config)
        cfg["volumes"]["data"]["name"] = "different-target-volume"
        self.candidate.write_text(json.dumps(cfg))
        with self.assertRaises(UpdaterError):
            self.apply()
        self.stack.call.assert_not_called()

    def test_captured_redis_volume_reaches_runtime_and_old_checkpoint(self):
        logical = "immich-updater-redis-data"
        self.config["services"]["redis"]["volumes"] = [
            {"type": "volume", "source": logical, "target": "/data"}
        ]
        self.config["volumes"] = {
            logical: {"name": "synthetic-existing-redis-volume", "external": True}
        }
        self.candidate.write_text(json.dumps(self.config))
        with patch(
            "simple_update.run",
            return_value=json.dumps(
                [{"Mountpoint": str(self.root / "redis-volume")}]
            ).encode(),
        ):
            result = self.apply()
        checkpoint = json.loads(
            (Path(result["backup"]) / "old-compose.json").read_text()
        )
        active = json.loads(self.path.read_text())
        for config in (checkpoint, active):
            self.assertEqual(config["volumes"], self.config["volumes"])
            self.assertEqual(
                config["services"]["redis"]["volumes"],
                self.config["services"]["redis"]["volumes"],
            )

    def test_dump_invalid_magic_is_not_verified(self):
        def call(*args, **kwargs):
            if "stdout" in kwargs:
                kwargs["stdout"].write(b"not a dump")

        self.stack.call.side_effect = call
        with self.assertRaises(UpdaterError):
            simple.dump_database(self.stack, self.root / "bad.dump")

    def test_dump_command_failure_is_not_verified(self):
        self.stack.call.side_effect = UpdaterError("test-only dump error")
        with self.assertRaises(UpdaterError):
            simple.dump_database(self.stack, self.root / "failed.dump")

    def test_dump_is_validated_by_real_command_interface(self):
        events = []

        def call(*args, **kwargs):
            events.append(args)
            if "stdout" in kwargs:
                kwargs["stdout"].write(b"PGDMPtest-only")

        self.stack.call.side_effect = call
        simple.dump_database(self.stack, self.root / "synthetic.dump")
        self.assertTrue(
            any("pg_restore" in event and "--list" in event for event in events)
        )

    def test_backup_magic_read_preserves_subprocess_fd_offset(self):
        import subprocess, sys

        def call(*args, **kwargs):
            if "stdout" in kwargs:
                kwargs["stdout"].write(b"PGDMP" + b"x" * 8192)
            elif "stdin" in kwargs:
                result = subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        "-S",
                        "-c",
                        'import os,sys;sys.exit(os.read(0,5)!=b"PGDMP")',
                    ],
                    stdin=kwargs["stdin"],
                    timeout=10,
                )
                if result.returncode:
                    raise UpdaterError("Stream descriptor skipped archive header")

        self.stack.call.side_effect = call
        simple.dump_database(self.stack, self.root / "transport.dump")

    def test_backup_database_must_match_server_database(self):
        self.config["services"]["database"]["environment"]["POSTGRES_DB"] = "postgres"
        self.config["services"]["immich-server"]["environment"] = {
            "DB_DATABASE_NAME": "immich"
        }
        with self.assertRaises(UpdaterError):
            simple.db_identity(self.config)

    def test_backup_external_db_and_user_mismatch_are_denied(self):
        for env in (
            {"DB_URL": "postgresql://synthetic.invalid/immich"},
            {"DB_HOSTNAME": "external.invalid"},
            {"DB_USERNAME": "other"},
            {"DB_PORT": "5433"},
        ):
            with self.subTest(env=env):
                self.config["services"]["immich-server"]["environment"] = env
                with self.assertRaises(UpdaterError):
                    simple.db_identity(self.config)

    def test_changed_state_directory_cannot_bypass_default_journal(self):
        default = self.app / ".immich-updater-state"
        default.mkdir()
        (default / simple.JOURNAL).write_text(
            '{"phase":"needs_attention","mutation_started":true}'
        )
        with patch("simple_update.Compose", return_value=self.stack):
            with self.assertRaises(simple.NeedsAttention):
                simple.preflight(self.path, self.state)
        self.stack.call.assert_not_called()

    def test_source_bound_version_disagreement_never_starts_target(self):
        self.receipt["runtime"]["version"] = "v3.3.0"
        with self.assertRaises(UpdaterError):
            self.apply()
        self.stack.call.assert_not_called()

    def test_failed_update_blocks_a_different_state_directory(self):
        with self.assertRaises(UpdaterError):
            self.apply(fault=Mock(side_effect=UpdaterError("test-only failure")))
        with self.assertRaises(simple.NeedsAttention):
            simple.preflight(self.path, self.root / "other-state")

    def test_equal_or_older_target_is_denied(self):
        for target in ("v3.1.0", "v3.0.9"):
            with self.subTest(target=target), self.assertRaises(UpdaterError):
                simple.require_upgrade("v3.1.0", "v3.1.0", target)

    def test_database_runtime_drift_is_denied_before_api(self):
        items = []
        for name, service in self.config["services"].items():
            env = copy.deepcopy(service.get("environment", {}))
            if name == "immich-server":
                env["DB_DATABASE_NAME"] = "unbacked_database"
            items.append(
                {
                    "Config": {
                        "Labels": {
                            "com.docker.compose.project": "synthetic",
                            "com.docker.compose.service": name,
                        },
                        "Env": [k + "=" + str(v) for k, v in env.items()],
                    },
                    "State": {"Running": True},
                }
            )
        self.stack.call.return_value = b"test-only-id"
        with patch("simple_update.run", return_value=json.dumps(items).encode()):
            with self.assertRaises(UpdaterError):
                simple.healthy(self.stack)
        self.stack.api.assert_not_called()

    def test_file_based_database_override_is_denied_without_reading_it(self):
        self.config["services"]["immich-server"]["environment"] = {
            "DB_DATABASE_NAME_FILE": "/never-open/test-only"
        }
        with self.assertRaises(UpdaterError):
            simple.db_identity(self.config)

    def test_legacy_transaction_refuses_without_api(self):
        self.state.mkdir()
        (self.state / "transaction.json").write_text("{}")
        with self.assertRaises(simple.NeedsAttention):
            simple.preflight(self.path, self.state)


class RedisImageVolumeTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "name": "synthetic",
            "services": {"redis": {"image": "old-redis", "volumes": []}},
        }
        self.item = {
            "Image": "sha256:" + "a" * 64,
            "Config": {
                "Labels": {
                    "com.docker.compose.project": "synthetic",
                    "com.docker.compose.service": "redis",
                }
            },
            "Mounts": [
                {
                    "Type": "volume",
                    "Name": "synthetic-private-volume",
                    "Destination": "/data",
                    "RW": True,
                }
            ],
        }
        self.image = {"Config": {"Volumes": {"/data": {}}}}

    def normalize(self):
        with patch(
            "simple_update.run", return_value=json.dumps([self.image]).encode()
        ) as run:
            result = simple.verify_mounts(self.config, "redis", self.item)
        self.assertEqual(
            run.call_args.args[0], ["docker", "image", "inspect", self.item["Image"]]
        )
        return result

    def test_image_declared_redis_data_is_pinned_as_existing_external_volume(self):
        before = copy.deepcopy(self.config)
        result = self.normalize()
        mount = result["services"]["redis"]["volumes"][0]
        self.assertEqual(mount["target"], "/data")
        self.assertEqual(mount["type"], "volume")
        declaration = result["volumes"][mount["source"]]
        self.assertEqual(
            declaration, {"name": "synthetic-private-volume", "external": True}
        )
        self.assertEqual(self.config, before)
        with patch(
            "simple_update.run",
            side_effect=AssertionError("Explicit mount needs no image inference"),
        ):
            self.assertEqual(simple.verify_mounts(result, "redis", self.item), result)

    def test_missing_image_volume_declaration_refuses(self):
        self.image["Config"]["Volumes"] = {}
        with self.assertRaises(UpdaterError):
            self.normalize()

    def test_missing_immutable_running_image_refuses_without_inspection(self):
        self.item["Image"] = "old-redis:tag"
        with patch(
            "simple_update.run",
            side_effect=AssertionError("Do not inspect a mutable image"),
        ):
            with self.assertRaises(UpdaterError):
                simple.verify_mounts(self.config, "redis", self.item)

    def test_only_redis_data_can_be_inferred(self):
        for change in (
            "bind",
            "read_only",
            "extra",
            "other_service",
            "missing_name",
            "duplicate",
        ):
            with self.subTest(change=change):
                cfg = copy.deepcopy(self.config)
                item = copy.deepcopy(self.item)
                if change == "bind":
                    item["Mounts"][0].update(Type="bind", Source="/synthetic-only")
                elif change == "read_only":
                    item["Mounts"][0]["RW"] = False
                elif change == "extra":
                    item["Mounts"].append(
                        {
                            "Type": "volume",
                            "Name": "other",
                            "Destination": "/extra",
                            "RW": True,
                        }
                    )
                elif change == "missing_name":
                    item["Mounts"][0].pop("Name")
                elif change == "duplicate":
                    item["Mounts"].append(copy.deepcopy(item["Mounts"][0]))
                else:
                    cfg["services"]["database"] = cfg["services"].pop("redis")
                name = "database" if change == "other_service" else "redis"
                with patch(
                    "simple_update.run", return_value=json.dumps([self.image]).encode()
                ):
                    with self.assertRaises(UpdaterError):
                        simple.verify_mounts(cfg, name, item)

    def test_existing_declared_mount_identity_cannot_be_adopted(self):
        self.config["services"]["redis"]["volumes"] = [
            {"type": "volume", "source": "data", "target": "/data"}
        ]
        self.config["volumes"] = {"data": {"name": "different-configured-volume"}}
        with patch(
            "simple_update.run",
            side_effect=AssertionError("Explicit drift cannot be inferred"),
        ):
            with self.assertRaises(UpdaterError):
                simple.verify_mounts(self.config, "redis", self.item)

    def test_reserved_volume_key_collision_refuses(self):
        self.config["volumes"] = {"immich-updater-redis-data": {"name": "other-volume"}}
        with self.assertRaises(UpdaterError):
            self.normalize()

    def test_runtime_capture_normalizes_but_refuses_duplicate_or_wrong_project(self):
        stack = Mock()
        stack.config.return_value = self.config
        stack.call.return_value = b"one"

        def run(argv, **kwargs):
            return json.dumps(
                [self.image]
                if argv[:3] == ["docker", "image", "inspect"]
                else [self.item]
            ).encode()

        with patch("simple_update.run", side_effect=run):
            result = simple.runtime_config(stack)
        self.assertTrue(result["volumes"])
        for items in (
            [self.item, self.item],
            [
                {
                    **self.item,
                    "Config": {
                        "Labels": {
                            "com.docker.compose.project": "other",
                            "com.docker.compose.service": "redis",
                        }
                    },
                }
            ],
        ):
            with patch("simple_update.run", return_value=json.dumps(items).encode()):
                with self.assertRaises(UpdaterError):
                    simple.runtime_config(stack)

    def test_captured_volume_identity_drift_stays_denied(self):
        result = self.normalize()
        self.item["Mounts"][0]["Name"] = "replacement-volume"
        with patch(
            "simple_update.run",
            side_effect=AssertionError("Do not infer explicit drift"),
        ):
            with self.assertRaises(UpdaterError):
                simple.verify_mounts(result, "redis", self.item)

    def test_normalized_redis_volume_participates_in_state_overlap_guard(self):
        result = self.normalize()
        with patch(
            "simple_update.run",
            return_value=json.dumps(
                [{"Mountpoint": "/synthetic/private-volume"}]
            ).encode(),
        ):
            with self.assertRaises(UpdaterError):
                simple.location(result, Path("/synthetic/private-volume/state"))


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.getenv("TMPDIR"))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "compose.yml").write_text("TEST-ONLY")
        (self.root / ".env").write_text("TEST-ONLY")
        self.args = updater.parser().parse_args(
            ["--immich-dir", str(self.root), "--state-dir", str(self.root / "state")]
        )

    def test_update_path_never_calls_ram_rehearsal_recovery_or_full_copy(self):
        import builtins

        original_import = builtins.__import__
        retired = {
            "archive",
            "rehearsal",
            "resource_policy",
            "sample_rehearsal",
            "recovery_drill",
        }

        def active_import(name, *args, **kwargs):
            if name.split(".")[0] in retired:
                raise AssertionError("Active update imported retired code: " + name)
            return original_import(name, *args, **kwargs)

        gh = Mock()
        gh.pages.return_value = []
        with (
            patch("immich_updater.GitHub", return_value=gh),
            patch("immich_updater.current_version", return_value=(3, 1, 0)),
            patch("immich_updater.choose", return_value=("v3.2.4", False)),
            patch(
                "simple_update.preflight",
                return_value={"runtime": {"version": "v3.1.0"}},
            ),
            patch(
                "transaction.candidate_config", return_value="candidate"
            ) as candidate,
            patch("simple_update.apply") as apply,
            patch("builtins.__import__", side_effect=active_import),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(updater.run(self.args), 0)
        candidate.assert_called_once_with(
            self.root / "compose.yml", "v3.2.4", self.root / "state" / "candidates"
        )
        apply.assert_called_once()

    def test_preflight_only_has_no_selection_pulls_or_update(self):
        self.args.preflight_only = True
        with (
            patch(
                "simple_update.preflight", return_value={"profile": "single-stack-v1"}
            ),
            patch("immich_updater.choose", side_effect=AssertionError("No selection")),
            patch(
                "transaction.candidate_config", side_effect=AssertionError("No pulls")
            ),
            patch("simple_update.apply", side_effect=AssertionError("No update")),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(updater.run(self.args), 0)

    def test_dry_run_does_not_create_state_or_docker_work(self):
        self.args.dry_run = True
        with (
            patch("immich_updater.current_version", return_value=(3, 1, 0)),
            patch("immich_updater.choose", return_value=("v3.2.4", False)),
            patch(
                "transaction.candidate_config", side_effect=AssertionError("No pulls")
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(updater.run(self.args), 0)
        self.assertFalse((self.root / "state").exists())

    def test_pending_update_blocks_before_unavailable_api(self):
        state = self.root / "state"
        state.mkdir()
        (state / simple.JOURNAL).write_text("{}")
        with patch(
            "immich_updater.current_version", side_effect=AssertionError("No repeat")
        ):
            with self.assertRaises(simple.NeedsAttention):
                updater.run(self.args)

    def test_wrong_selection_endpoint_never_pulls_or_starts_target(self):
        with (
            patch("immich_updater.current_version", return_value=(3, 1, 0)),
            patch("immich_updater.choose", return_value=("v3.2.4", False)),
            patch(
                "simple_update.preflight",
                return_value={"runtime": {"version": "v3.3.0"}},
            ),
            patch("transaction.candidate_config") as candidate,
            patch("simple_update.apply") as apply,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(UpdaterError):
                updater.run(self.args)
        candidate.assert_not_called()
        apply.assert_not_called()

    def test_application_marker_blocks_before_api_with_new_state_dir(self):
        (self.root / simple.APPLICATION_MARKER).write_text("{}")
        with patch(
            "immich_updater.current_version", side_effect=AssertionError("No repeat")
        ):
            with self.assertRaises(simple.NeedsAttention):
                updater.run(self.args)


if __name__ == "__main__":
    unittest.main()
