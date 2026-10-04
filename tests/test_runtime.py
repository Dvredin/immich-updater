"""Active transport checks: real subprocess/Node plus retired-import isolation."""

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

from compose_runtime import (
    NODE_HTTP,
    UpdaterError,
    compose_bytes,
    dollar_literals,
    run,
)
from simple_update import failure_details


class RuntimeTests(unittest.TestCase):
    def test_fresh_active_imports_without_any_historical_modules(self):
        script = """
import importlib.abc
import sys

class NoRetiredCode(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'archive', 'rehearsal', 'resource_policy', 'sample_rehearsal', 'recovery_drill'}:
            raise AssertionError('Retired import: ' + fullname)

sys.path.insert(0, sys.argv[1])
sys.meta_path.insert(0, NoRetiredCode())
import immich_updater
import simple_update
import transaction
import availability_monitor
assert not hasattr(transaction, 'restore')
assert not hasattr(transaction, 'apply')
assert not hasattr(transaction, 'full_checkpoint_capacity')
assert not hasattr(simple_update.Compose, 'capture_database')
assert simple_update.UpdaterError is transaction.UpdaterError
print('ACTIVE_IMPORTS_ISOLATED')
"""
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                script,
                str(Path(__file__).resolve().parents[1]),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "ACTIVE_IMPORTS_ISOLATED")

    def test_literal_config_roundtrip(self):
        model = {
            "services": {
                "database": {"environment": {"TEST_ONLY": "$x-${literal}-$$-кириллица"}}
            }
        }
        self.assertEqual(
            dollar_literals(json.loads(compose_bytes(model)), encode=False), model
        )

    def test_real_subprocess_binary_input_output(self):
        result = run(
            [
                sys.executable,
                "-I",
                "-S",
                "-c",
                "import sys;sys.stdout.buffer.write(sys.stdin.buffer.read())",
            ],
            payload=b"PGDMP\x00\xff",
            timeout=10,
        )
        self.assertEqual(result, b"PGDMP\x00\xff")

    def test_real_subprocess_error_stays_redacted(self):
        with self.assertRaises(UpdaterError) as caught:
            run(
                [
                    sys.executable,
                    "-I",
                    "-S",
                    "-c",
                    "import sys;sys.stderr.write('test-only-private-canary');sys.exit(7)",
                ],
                timeout=10,
            )
        fields = failure_details(caught.exception, "transport_test")
        self.assertEqual(fields["exit_status"], 7)
        self.assertEqual(fields["error_code"], "command_failed")
        self.assertNotIn("canary", str(caught.exception) + json.dumps(fields))

    def test_native_node_empty_204_transport(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Host Node unavailable; no real Node execution claimed.")
        script = (
            "globalThis.fetch=async()=>new Response(null,{status:204});\n" + NODE_HTTP
        )
        result = subprocess.run(
            [node, "--input-type=module", "-e", script],
            input=b'{"path":"/test-only","method":"DELETE"}',
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        response = json.loads(result.stdout)
        self.assertEqual(response["status"], 204)
        self.assertEqual(response["bytes"], 0)
        self.assertEqual(response["sha256"], hashlib.sha256(b"").hexdigest())


if __name__ == "__main__":
    unittest.main()
