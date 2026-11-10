"""Exercise private MCPB generation, renewal and its real Windows stdio entry."""
from io import BytesIO
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

from knowledge_bridge import claude_extension as extension
from knowledge_bridge import client_config
from knowledge_bridge.client_access import ClientIdentityRegistry, ClientPermissionService
from knowledge_bridge.client_pairing import PairingTransactionManager
from knowledge_bridge.workspace import initialize_workspace


class ClaudeBundleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="claude-mcpb-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "synthetic-workspace"
        self.control = self.root / "synthetic-control"
        self.bundle = self.control / "client-config/jiedu-claude.mcpb"
        self.profile = self.root / "Claude"
        self.profile.mkdir()
        self.sentinel = self.profile / "claude_desktop_config.json"
        self.sentinel.write_bytes(b'{"synthetic":"preserve"}')
        database = self.profile / "IndexedDB/https_claude.ai_0.indexeddb.leveldb"
        patches = (
            patch.object(extension, "BUNDLE", self.bundle),
            patch.object(client_config, "VAULT", self.workspace),
            patch.object(client_config, "local_path", lambda *parts: self.control.joinpath(*parts)),
            patch("knowledge_bridge.claude_source_adapter.profile_paths",
                  return_value={"fixture": ("测试", database, database.with_suffix(".blob"))}),
        )
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.registry = ClientIdentityRegistry(initialize_workspace(self.workspace), self.control)
        self.permissions = ClientPermissionService(self.registry)
        self.manager = PairingTransactionManager(self.registry, self.permissions)
        self.identity = self.registry.create("synthetic_claude", "Fixture", "claude", "stdio")
        self.claimed_id = self.identity["claimed_id"]
        self.internal_id = self.identity["internal_id"]
        self.secret = self.identity["secret"]
        self.permissions.set_policy(self.internal_id, "formal", "read", "deny")

    def write_fixture(self, manifest, script=None):
        self.bundle.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(self.bundle, "w") as archive:
            archive.writestr("manifest.json", json.dumps(manifest))
            if script is not None:
                archive.writestr(extension.ENTRY_POINT, script)

    def prepare(self):
        return extension.prepare_bundle(self.claimed_id, self.secret)

    def test_corrupt_launch_is_not_a_valid_download(self):
        for defect in ("missing", "empty", "entry-point", "argv", "crc"):
            with self.subTest(defect=defect):
                manifest = extension._manifest(self.claimed_id, self.secret)
                script = extension._SHIM
                if defect == "missing":
                    script = None
                elif defect == "empty":
                    script = ""
                elif defect == "entry-point":
                    manifest["server"]["entry_point"] = "server/missing.js"
                elif defect == "argv":
                    manifest["server"]["mcp_config"]["args"] = ["server/missing.js"]
                self.write_fixture(manifest, script)
                if defect == "crc":
                    self.bundle.write_bytes(self.bundle.read_bytes().replace(b'"use strict";', b'"bad strict";', 1))
                self.assertIsNone(extension.inspect_bundle())
                self.assertEqual(self.claimed_id, extension.inspect_bundle(identity_only=True)["client_id"])

    def test_refresh_recovers_old_bundle_with_original_identity_and_permissions(self):
        manifest = extension._manifest(self.claimed_id, self.secret)
        manifest["version"] = "1.1.8"
        self.write_fixture(manifest)
        before = self.permissions.policies(self.internal_id)
        result = self.manager.quick_connect("claude")
        self.assertEqual("reused", result["outcome"])
        self.assertEqual(self.internal_id, result["client"]["internal_id"])
        self.assertTrue(self.registry.configured_secret_matches(self.internal_id, self.secret))
        self.assertEqual(before, self.permissions.policies(self.internal_id))
        current = self.bundle.read_bytes()
        with zipfile.ZipFile(BytesIO(current)) as archive:
            self.assertEqual("1.1.9", json.loads(archive.read("manifest.json"))["version"])
            self.assertIsNone(archive.testzip())
        self.manager.quick_connect("claude")
        self.assertEqual(current, self.bundle.read_bytes())
        self.assertEqual(b'{"synthetic":"preserve"}', self.sentinel.read_bytes())

    def test_rotation_replaces_package_only_after_confirmation(self):
        old = self.prepare().read_bytes()
        policies = self.permissions.policies(self.internal_id)
        with patch("knowledge_bridge.client_pairing.config_preview") as preview, \
             patch("knowledge_bridge.client_pairing.config_apply") as apply:
            plan = self.manager.begin_rotation(self.internal_id)
            self.assertTrue(plan["extension_replacement"])
            self.assertIn("会替换之前的扩展包", plan["config"]["diff"])
            self.assertNotIn("secret", plan)
            self.assertEqual(old, self.bundle.read_bytes())
            self.manager.cancel(plan["rotation_id"])
            self.assertEqual(old, self.bundle.read_bytes())
            plan = self.manager.begin_rotation(self.internal_id)
            result = self.manager.commit_rotation(plan["rotation_id"])
            preview.assert_not_called()
            apply.assert_not_called()
        self.assertEqual(self.internal_id, result["client"]["internal_id"])
        self.assertTrue(result["extension_ready"])
        self.assertNotIn("secret", result)
        self.assertNotEqual(old, self.bundle.read_bytes())
        self.assertFalse(self.registry.configured_secret_matches(self.internal_id, self.secret))
        self.assertTrue(self.registry.configured_secret_matches(self.internal_id, extension.inspect_bundle()["secret"]))
        self.assertEqual(policies, self.permissions.policies(self.internal_id))
        self.assertEqual(b'{"synthetic":"preserve"}', self.sentinel.read_bytes())

    def test_rotation_failure_restores_original_package_and_credential(self):
        old = self.prepare().read_bytes()
        plan = self.manager.begin_rotation(self.internal_id)
        with patch.object(self.registry, "commit_rotation", side_effect=ValueError("fixture failure")):
            with self.assertRaisesRegex(ValueError, "fixture failure"):
                self.manager.commit_rotation(plan["rotation_id"])
        self.assertEqual(old, self.bundle.read_bytes())
        self.assertTrue(self.registry.configured_secret_matches(self.internal_id, self.secret))

    def test_audit_failure_after_persistence_keeps_new_package_and_reports_state(self):
        old = self.prepare().read_bytes()
        plan = self.manager.begin_rotation(self.internal_id)
        with patch.object(self.registry, "audit", side_effect=OSError("fixture audit failure")):
            with self.assertRaisesRegex(ValueError, "新扩展包已保存"):
                self.manager.commit_rotation(plan["rotation_id"])
        self.assertNotEqual(old, self.bundle.read_bytes())
        self.assertFalse(self.registry.configured_secret_matches(self.internal_id, self.secret))
        self.assertTrue(self.registry.configured_secret_matches(self.internal_id, extension.inspect_bundle()["secret"]))

    def test_rotation_refuses_changed_package_and_keeps_other_identity(self):
        self.prepare()
        plan = self.manager.begin_rotation(self.internal_id)
        foreign = extension.build_bundle("different_fixture", "different_synthetic_secret")
        self.bundle.write_bytes(foreign)
        with self.assertRaisesRegex(ValueError, "已变化"):
            self.manager.commit_rotation(plan["rotation_id"])
        self.assertEqual(foreign, self.bundle.read_bytes())
        self.assertTrue(self.registry.configured_secret_matches(self.internal_id, self.secret))
        with self.assertRaisesRegex(ValueError, "其他连接"):
            self.manager.begin_rotation(self.internal_id)

    def extract_launch(self):
        extracted = self.root / "含空格 Node 入口"
        with zipfile.ZipFile(self.bundle) as archive:
            self.assertIsNone(archive.testzip())
            manifest = json.loads(archive.read("manifest.json"))
            archive.extractall(extracted)
        server = manifest["server"]
        config = server["mcp_config"]
        entry = extracted / server["entry_point"]
        self.assertTrue(entry.is_file())
        self.assertEqual("node", config["command"])
        args = [value.replace("${__dirname}", str(extracted)) for value in config["args"]]
        self.assertEqual(entry, Path(args[0]))
        return [shutil.which("node"), *args], {**os.environ, **config["env"]}

    @unittest.skipUnless(shutil.which("node"), "Node is required for the MCPB entry test")
    def test_extracted_entry_runs_real_mcp_and_only_protocol_reaches_stdout(self):
        self.prepare()
        command, env = self.extract_launch()
        args = json.loads(env["JIEDU_ARGS_JSON"])
        self.assertEqual(["-m", "knowledge_bridge.cli", "mcp", "--transport", "stdio"], args[-5:])
        output = queue.Queue()
        with tempfile.TemporaryFile() as stderr, subprocess.Popen(
            command, env=env, cwd=self.root, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr
        ) as process:
            def read_stdout():
                for line in process.stdout:
                    output.put(line)
                output.put(None)
            reader = threading.Thread(target=read_stdout, daemon=True)
            reader.start()
            def send(method, params, request_id=None):
                message = {"jsonrpc": "2.0", "method": method, "params": params}
                if request_id is not None:
                    message["id"] = request_id
                process.stdin.write(json.dumps(message).encode() + b"\n")
                process.stdin.flush()
            try:
                send("initialize", {"protocolVersion":"2024-11-05", "capabilities":{},
                                    "clientInfo":{"name":"synthetic-node-test","version":"1.0.0"}}, 1)
                initial = json.loads(output.get(timeout=35))
                self.assertEqual(1, initial["id"])
                self.assertIn("serverInfo", initial["result"])
                send("notifications/initialized", {})
                send("tools/list", {}, 2)
                tools = json.loads(output.get(timeout=20))
                self.assertEqual(2, tools["id"])
                self.assertTrue(tools["result"]["tools"])
            finally:
                process.stdin.close()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                reader.join(timeout=5)
            self.assertEqual(0, process.returncode)
            self.assertIsNone(output.get(timeout=5), "Non-protocol bytes followed the two responses")
            stderr.seek(0)
            self.assertNotIn(self.secret.encode(), stderr.read())

    @unittest.skipUnless(shutil.which("node"), "Node is required for the MCPB entry test")
    def test_failures_are_on_stderr_without_launch_data(self):
        self.prepare()
        command, env = self.extract_launch()
        cases = [
            ({"JIEDU_COMMAND": ""}, b"JIEDU_COMMAND"),
            ({"JIEDU_ARGS_JSON": "[1]"}, b"JIEDU_ARGS_JSON"),
            ({"JIEDU_ENV_JSON": "[]"}, b"JIEDU_ENV_JSON"),
            ({"JIEDU_COMMAND": str(self.root / "missing-python.exe")}, b"ENOENT"),
            ({"JIEDU_CWD": str(self.root / "missing-directory")}, b"ENOENT"),
            ({"JIEDU_COMMAND": sys.executable, "JIEDU_ARGS_JSON": '["-B","-c","raise SystemExit(7)"]'}, b"code 7"),
        ]
        for changes, expected in cases:
            with self.subTest(expected=expected):
                result = subprocess.run(command, env=env | changes, cwd=self.root, input=b"",
                                        capture_output=True, timeout=15)
                self.assertNotEqual(0, result.returncode)
                self.assertEqual(b"", result.stdout)
                self.assertIn(expected, result.stderr)
                self.assertNotIn(self.secret.encode(), result.stderr)


if __name__ == "__main__":
    unittest.main()
