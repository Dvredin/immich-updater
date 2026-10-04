"""Installer tests: real source packaging, simulated host lifecycle, no deployment."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / "tools/install.py"
spec = importlib.util.spec_from_file_location("updater_installer", SOURCE)
if spec is None or spec.loader is None:
    raise RuntimeError("Installer module unavailable")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def good_receipt():
    return json.dumps(
        {
            "event": "preflight",
            "profile": "single-stack-v1",
            "source_mutations": False,
            "parallel_rehearsal": False,
            "photo_copy_required": False,
            "required_backup_free_bytes": 1024,
            "available_bytes": 2048,
            "runtime": {"version": "v3.1.0", "services_running": True, "ping": True},
        }
    )


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.revision_patch = patch.object(module, "REVISION", "a" * 40)
        self.revision_patch.start()
        self.addCleanup(self.revision_patch.stop)
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.dest = self.root / "new"
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.ready = self.state / "ready.json"
        self.app = self.root / "app"
        self.app.mkdir()
        (self.app / ".env").write_text("TEST-ONLY")
        (self.app / "compose.yml").write_text("TEST-ONLY")

    def ready_installation(self):
        for relative, content in module.package().items():
            path = self.dest / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        python = self.dest / ".venv/bin/python"
        python.parent.mkdir(parents=True)
        python.write_text("TEST-ONLY recording interpreter")
        self.ready.write_text(
            json.dumps(
                {
                    "status": "prepared",
                    "profile": "single-stack-v1",
                    "revision": module.REVISION,
                    "app_dir": str(self.app),
                    "files": {
                        name: module.hashlib.sha256(data).hexdigest()
                        for name, data in module.package().items()
                    },
                }
            )
        )

    def activate(self, call, release=None):
        with (
            patch.multiple(
                module,
                DEST=self.dest,
                APP=self.app,
                STATE=self.state,
                READY=self.ready,
                call=call,
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            return module.activate(release)

    def test_source_package_real_compile(self):
        files = module.package()
        self.assertIn("compose_runtime.py", files)
        self.assertIn("docs/OPERATIONS.md", files)
        retired = {
            "rehearsal.py",
            "resource_policy.py",
            "sample_rehearsal.py",
            "recovery_drill.py",
            "risk_policy.json",
        }
        self.assertTrue(retired.isdisjoint(files))
        self.assertFalse(
            any(
                name.startswith(("archive/", "tests/archive/", "tests/legacy/"))
                for name in files
            )
        )
        self.assertFalse(
            any("live" in name or name == "requirements-dev.txt" for name in files)
        )
        for name, data in files.items():
            if name.endswith(".py"):
                compile(data, name, "exec")

    def test_gate_positive(self):
        self.assertEqual(module.gate(good_receipt()), "v3.1.0")

    def test_gate_skip_is_not_acceptance(self):
        with self.assertRaises(module.StopInstall):
            module.gate('{"event":"decision","decision":"skip"}')

    def test_gate_malformed_is_not_acceptance(self):
        with self.assertRaises(module.StopInstall):
            module.gate("{this is not json}")

    def test_gate_source_mutated_blocks(self):
        row = json.loads(good_receipt())
        row["source_mutations"] = True
        with self.assertRaises(module.StopInstall):
            module.gate(json.dumps(row))

    def test_gate_failed_runtime_blocks(self):
        row = json.loads(good_receipt())
        row["runtime"]["ping"] = False
        with self.assertRaises(module.StopInstall):
            module.gate(json.dumps(row))

    def test_gate_insufficient_db_backup_space_blocks(self):
        row = json.loads(good_receipt())
        row["available_bytes"] = 1
        with self.assertRaises(module.StopInstall):
            module.gate(json.dumps(row))

    def test_activation_missing_host_acceptance_never_calls_systemctl(self):
        call = Mock()
        with self.assertRaises(module.StopInstall):
            self.activate(call)
        call.assert_not_called()

    def test_activation_modified_installed_code_never_calls_systemctl(self):
        self.ready_installation()
        (self.dest / "immich_updater.py").write_text("altered")
        call = Mock()
        with self.assertRaises(module.StopInstall):
            self.activate(call)
        call.assert_not_called()

    def test_activation_pending_transaction_blocks(self):
        self.ready_installation()
        (self.state / "transaction.json").write_text("{}")
        call = Mock()
        with self.assertRaises(module.StopInstall):
            self.activate(call)
        call.assert_not_called()

    def test_failed_runtime_preflight_cannot_enable_timer(self):
        self.ready_installation()
        call = Mock(
            return_value=subprocess.CompletedProcess(
                [], 1, "", "TEST-ONLY runtime/DB backup admission failed"
            )
        )
        release = Mock()
        with self.assertRaises(module.StopInstall):
            self.activate(call, release)
        release.assert_not_called()
        self.assertEqual(len(call.call_args_list), 1)
        self.assertEqual(call.call_args.args[0], str(self.dest / ".venv/bin/python"))
        self.assertEqual(
            (self.state / "activation-preflight.log").stat().st_mode & 0o777, 0o600
        )

    def test_zero_exit_without_positive_preflight_receipt_cannot_enable_timer(self):
        self.ready_installation()
        call = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
        release = Mock()
        with self.assertRaises(module.StopInstall):
            self.activate(call, release)
        release.assert_not_called()
        self.assertEqual(len(call.call_args_list), 1)

    def test_activation_works_from_stdlib_only_outer_interpreter(self):
        self.ready_installation()
        script = r"""
import contextlib,importlib.util,io,json,subprocess,sys
from pathlib import Path
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('installer',sys.argv[1])
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
m.DEST=Path(sys.argv[2]);m.APP=Path(sys.argv[3]);m.STATE=Path(sys.argv[4]);m.READY=Path(sys.argv[5]);m.REVISION=sys.argv[6]
events=[]
def call(*args,**kwargs):
    events.append(args)
    if '--preflight-only' in args:
        if args[0]!=str(m.DEST/'.venv/bin/python') or kwargs.get('cwd')!=str(m.DEST):raise RuntimeError('Wrong probe interpreter/cwd')
        return subprocess.CompletedProcess(args,0,json.dumps({'event':'preflight','profile':'single-stack-v1','source_mutations':False,'parallel_rehearsal':False,'photo_copy_required':False,'required_backup_free_bytes':1024,'available_bytes':2048,'runtime':{'version':'v3.1.0','services_running':True,'ping':True}}),'')
    if args[:2]==('systemctl','is-enabled'):return subprocess.CompletedProcess(args,0,'enabled\n','')
    return subprocess.CompletedProcess(args,0,'active\n','')
with patch.object(m,'call',call),contextlib.redirect_stdout(io.StringIO()):m.activate()
if not events or '--preflight-only' not in events[0]:raise RuntimeError('No installed-venv admission probe')
if any(name in sys.modules for name in ('requests','semantic_version','compose_runtime','transaction')):raise RuntimeError('Activation imported app dependencies into outer interpreter')
print(json.dumps({'stdlib_outer':True,'installed_venv_probe':True,'host_lifecycle':'mocked'}))
"""
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-S",
                "-c",
                script,
                str(SOURCE),
                str(self.dest),
                str(self.app),
                str(self.state),
                str(self.ready),
                module.REVISION,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["installed_venv_probe"])

    def test_activation_releases_lock_before_persistent_timer_start(self):
        self.ready_installation()
        events = []

        def call(*args, **kwargs):
            events.append(args[1])
            if "--preflight-only" in args:
                return subprocess.CompletedProcess(args, 0, good_receipt(), "")
            return subprocess.CompletedProcess(
                args, 0, "enabled\n" if args[1] == "is-enabled" else "active\n", ""
            )

        self.activate(call, lambda: events.append("lock-released"))
        self.assertEqual(
            events[:3],
            [str(self.dest / "immich_updater.py"), "lock-released", "enable"],
        )

    def test_active_old_updater_not_killed_or_replaced(self):
        call = Mock(return_value=Mock(stdout="active\n", returncode=0))
        with (
            patch.multiple(module, call=call, DEST=self.dest),
            self.assertRaises(module.StopInstall),
        ):
            module.install()
        self.assertEqual([item.args[1] for item in call.call_args_list], ["show"])
        self.assertFalse(self.dest.exists())

    def test_failed_reinstall_archives_old_ready_instead_of_reusing_it(self):
        self.ready.write_text('{"status":"prepared"}')

        def call(*args, **kwargs):
            return Mock(
                stdout="inactive\n", returncode=3 if args[1] == "is-active" else 0
            )

        with (
            patch.multiple(
                module,
                call=call,
                DEST=self.dest,
                APP=self.app,
                STATE=self.state,
                READY=self.ready,
                prerequisites=Mock(
                    side_effect=module.StopInstall("TEST insufficient RAM")
                ),
            ),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaises(module.StopInstall),
        ):
            module.install()
        self.assertFalse(self.ready.exists())
        self.assertTrue(list(self.state.glob("install-ready.before-*.json")))

    def install_simulated(self, prepared_output, failed_step=None, unit_setup=None):
        self.dest.mkdir()
        (self.dest / "old-marker").write_text("untouched")
        calls = []
        self.recorded_calls = calls

        def call(*args, **kwargs):
            calls.append(args)
            if (
                failed_step
                and "-m" in args
                and args[args.index("-m") + 1] == failed_step
            ):
                return Mock(
                    stdout="TEST-ONLY package stdout\n",
                    stderr="TEST-ONLY package stderr\n",
                    returncode=7,
                )
            if args[:3] == ("systemctl", "show", module.SERVICE):
                prop = args[3]
                values = {
                    "--property=ActiveState": "inactive\n",
                    "--property=ExecStart": str(self.dest / "immich_updater.py")
                    + " --state-dir /var/lib/immich-updater/state",
                    "--property=User": "root\n",
                    "--property=OnFailure": "",
                }
                return Mock(stdout=values[prop], stderr="", returncode=0)
            if args[:2] == ("systemctl", "is-active"):
                return Mock(stdout="inactive\n", stderr="", returncode=3)
            if "--preflight-only" in args:
                return Mock(stdout=prepared_output, stderr="", returncode=0)
            return Mock(
                stdout="TEST-ONLY mocked successful command", stderr="", returncode=0
            )

        real_path = Path

        def path(*values):
            if values == ("/opt",):
                return self.root / "opt"
            if values == ("/etc/systemd/system",):
                return self.root / "systemd"
            return real_path(*values)

        (self.root / "opt").mkdir()
        (self.root / "systemd").mkdir()
        if unit_setup:
            unit_setup(self.root / "systemd")
        with (
            patch.multiple(
                module,
                call=call,
                DEST=self.dest,
                APP=self.app,
                STATE=self.state,
                READY=self.ready,
                prerequisites=Mock(),
                authenticated_package=lambda revision: module.package(),
                Path=path,
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            module.install()
        return calls

    def test_prepare_exit_zero_but_skip_cannot_swap_code_or_enable_timer(self):
        with self.assertRaises(module.StopInstall):
            self.install_simulated('{"event":"decision","decision":"skip"}')
        self.assertEqual((self.dest / "old-marker").read_text(), "untouched")
        self.assertFalse(self.ready.exists())
        self.assertEqual(list((self.root / "systemd").iterdir()), [])

    def test_success_installs_exact_package_and_leaves_timer_disabled(self):
        calls = self.install_simulated(good_receipt())
        self.assertEqual(json.loads(self.ready.read_text())["status"], "prepared")
        self.assertEqual(
            (self.dest / "INSTALLATION_REVISION").read_text().strip(), module.REVISION
        )
        self.assertTrue(list((self.root / "opt").glob("immich-updater.before-*")))
        self.assertFalse(any(args[:2] == ("systemctl", "enable") for args in calls))
        self.assertTrue(
            (
                self.root
                / "systemd"
                / "immich-updater.service.d"
                / "50-rehearsal-state.conf"
            ).is_file()
        )

    def test_unit_symlink_cannot_overwrite_unrelated_file(self):
        sentinel = self.root / "sentinel"
        sentinel.write_text("untouched")

        def setup(root):
            (root / module.SERVICE).symlink_to(sentinel)

        with self.assertRaises(module.StopInstall):
            self.install_simulated(good_receipt(), unit_setup=setup)
        self.assertEqual(sentinel.read_text(), "untouched")
        self.assertFalse(self.ready.exists())
        self.assertEqual((self.dest / "old-marker").read_text(), "untouched")

    def test_unit_parent_symlink_is_denied(self):
        outside = self.root / "outside"
        outside.mkdir()

        def setup(root):
            (root / (module.SERVICE + ".d")).symlink_to(
                outside, target_is_directory=True
            )

        with self.assertRaises(module.StopInstall):
            self.install_simulated(good_receipt(), unit_setup=setup)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse(self.ready.exists())

    def test_revision_authenticates_source_bytes_not_just_head(self):
        source = self.root / "git-source"
        source.mkdir()
        (source / "README.md").write_bytes(b"published test-only bytes")

        def git(*args):
            return subprocess.run(
                ["git", "-C", str(source), *args],
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            ).stdout.strip()

        git("init", "-q")
        git("add", "README.md")
        git(
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "test-only package",
        )
        revision = git("rev-parse", "HEAD")
        with patch.multiple(module, SOURCE_ROOT=source, PACKAGE_FILES=("README.md",)):
            self.assertEqual(module.source_revision(revision), revision)
            (source / "README.md").write_bytes(b"modified after published commit")
            with self.assertRaises(module.StopInstall):
                module.source_revision(revision)

    def test_installed_source_revision_requires_verified_receipt_bytes(self):
        self.ready_installation()
        (self.dest / "INSTALLATION_REVISION").write_text(module.REVISION + "\n")
        with patch.multiple(
            module, SOURCE_ROOT=self.dest, DEST=self.dest, READY=self.ready
        ):
            self.assertEqual(module.source_revision(module.REVISION), module.REVISION)
            (self.dest / "simple_update.py").write_bytes(
                b"test-only modified installed bytes"
            )
            with self.assertRaises(module.StopInstall):
                module.source_revision(module.REVISION)

    def test_unverified_revision_marker_is_not_package_provenance(self):
        (self.root / "INSTALLATION_REVISION").write_text(module.REVISION + "\n")
        with patch.multiple(
            module, SOURCE_ROOT=self.root, PACKAGE_FILES=("INSTALLATION_REVISION",)
        ):
            with self.assertRaises(module.StopInstall):
                module.source_revision(module.REVISION)

    def test_installer_refuses_default_simple_journal(self):
        default = self.app / ".immich-updater-state"
        default.mkdir()
        (default / "simple-update.json").write_text("{}")
        call = Mock()
        with (
            patch.multiple(
                module, APP=self.app, STATE=self.state, DEST=self.dest, call=call
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(module.StopInstall):
                module.prerequisites()
        call.assert_not_called()

    def test_dependency_failure_prevents_code_swap_and_activation(self):
        self.package_failure("pip", "dependency-install.log")

    def test_unit_test_failure_keeps_diagnostics_and_old_installation(self):
        self.package_failure("unittest", "package-tests.log")

    def test_venv_failure_keeps_diagnostics_and_old_installation(self):
        self.package_failure("venv", "venv-create.log")

    def package_failure(self, step, filename):
        with self.assertRaises(module.StopInstall) as error:
            self.install_simulated(good_receipt(), failed_step=step)
        self.assertEqual((self.dest / "old-marker").read_text(), "untouched")
        self.assertFalse(self.ready.exists())
        self.assertFalse(
            any(
                "--prepare-only" in args or args[:2] == ("systemctl", "enable")
                for args in self.recorded_calls
            )
        )
        self.assertEqual(list((self.root / "systemd").iterdir()), [])
        log = next((self.root / "opt").glob("immich-updater.staging-*")) / filename
        self.assertEqual(
            log.read_text(), "TEST-ONLY package stdout\nTEST-ONLY package stderr\n"
        )
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        self.assertIn(str(log), str(error.exception))
        self.assertNotIn("TEST-ONLY package", str(error.exception))

    def test_logged_real_subprocess_failure_is_private_and_preserved(self):
        log = self.root / "command.log"
        script = "import sys;print('TEST-ONLY stdout');print('TEST-ONLY stderr',file=sys.stderr);sys.exit(7)"
        with self.assertRaises(module.StopInstall) as error:
            module.logged_call(
                log, sys.executable, "-I", "-S", "-c", script, timeout=10
            )
        self.assertEqual(log.read_text(), "TEST-ONLY stdout\nTEST-ONLY stderr\n")
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        self.assertIn(str(log), str(error.exception))
        self.assertNotIn("TEST-ONLY", str(error.exception))

    def test_logged_real_subprocess_timeout_preserves_partial_output(self):
        log = self.root / "timeout.log"
        script = "import sys,time;print('TEST-ONLY partial stdout',flush=True);print('TEST-ONLY partial stderr',file=sys.stderr,flush=True);time.sleep(30)"
        with self.assertRaises(module.StopInstall) as error:
            module.logged_call(
                log, sys.executable, "-I", "-S", "-c", script, timeout=0.5
            )
        self.assertEqual(
            log.read_text(), "TEST-ONLY partial stdout\nTEST-ONLY partial stderr\n"
        )
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        self.assertIn(str(log), str(error.exception))
        self.assertNotIn("TEST-ONLY", str(error.exception))

    def test_revision_format_rejected_before_lookup(self):
        call = Mock()
        with patch.object(module, "call", call), self.assertRaises(module.StopInstall):
            module.source_revision("abc")
        call.assert_not_called()

    def test_revision_mismatch_blocks(self):
        with (
            patch.object(module, "call", Mock(return_value=Mock(stdout="b" * 40))),
            self.assertRaises(module.StopInstall),
        ):
            module.source_revision("a" * 40)

    def test_missing_package_file_blocks(self):
        with (
            patch.object(module, "SOURCE_ROOT", self.root),
            self.assertRaises(module.StopInstall),
        ):
            module.package()

    def test_activation_other_application_path_blocks(self):
        self.ready_installation()
        data = json.loads(self.ready.read_text())
        data["app_dir"] = "/different/deployment"
        self.ready.write_text(json.dumps(data))
        call = Mock()
        with self.assertRaises(module.StopInstall):
            self.activate(call)
        call.assert_not_called()

    def test_activation_missing_hashes_blocks(self):
        self.ready_installation()
        data = json.loads(self.ready.read_text())
        data.pop("files")
        self.ready.write_text(json.dumps(data))
        call = Mock()
        with self.assertRaises(module.StopInstall):
            self.activate(call)
        call.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
