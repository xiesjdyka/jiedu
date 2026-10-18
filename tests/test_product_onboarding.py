import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from knowledge_bridge import automatic_extraction, knowledge_base, local_ai, memory_control


class ProductOnboardingTests(unittest.TestCase):
    def test_custom_local_client_has_independent_identity_and_permission(self):
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp)/"clients.json";permissions=Path(tmp)/"permissions.json"
            with patch.object(local_ai,"STATE_PATH",state),patch.object(memory_control,"INTERFACE_STATE",permissions):
                created=local_ai.create_custom_client("My Agent","my_agent")
                self.assertEqual("my_agent",created["client_id"])
                self.assertEqual("stdio",created["transport"])
                self.assertEqual("my_agent",created["env"]["KNOWLEDGE_BRIDGE_CLIENT_ID"])
                self.assertFalse(memory_control.interface_state()["clients"]["my_agent"]["enabled"])
                memory_control.update_client_permission("my_agent",True)
                self.assertTrue(memory_control.interface_state()["clients"]["my_agent"]["enabled"])
    def test_knowledge_base_initialize_and_dynamic_core(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "bridge-data" / "knowledge-base.json"
            audit = root / "bridge-data" / "knowledge-base-audit.json"
            vault = root / "vault"
            with patch.object(knowledge_base, "CONFIG_PATH", config), patch.object(knowledge_base, "AUDIT_PATH", audit):
                result = knowledge_base.initialize(str(vault), owner_confirmed=True)
                self.assertTrue(result["initialized"])
                self.assertTrue(all((vault / name).is_dir() for name in knowledge_base.DEFAULT_FOLDERS))
                self.assertEqual([], knowledge_base.markdown_files(vault))
                core = vault / "00_\u7cfb\u7edf" / "\u6211\u7684\u80cc\u666f.md"
                core.write_text("# \u6211\u7684\u80cc\u666f\n", encoding="utf-8")
                knowledge_base.save_core_files(["00_\u7cfb\u7edf/\u6211\u7684\u80cc\u666f.md"])
                instruction = knowledge_base.startup_instruction()
                self.assertIn("\u6211\u7684\u80cc\u666f", instruction["instruction"])
                audit_data = json.loads(audit.read_text(encoding="utf-8"))
                self.assertTrue(audit_data["purpose"].startswith("\u5b8c\u6574\u6027"))
                self.assertTrue(any(item["path"] == "00_\u7cfb\u7edf/\u6211\u7684\u80cc\u666f.md" for item in audit_data["files"]))

    def test_import_diagnostic_is_read_only_and_reports_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.md").write_text("same", encoding="utf-8")
            (root / "b.md").write_text("same", encoding="utf-8")
            (root / "image.xyz").write_bytes(b"unknown")
            before = {item.name: item.read_bytes() for item in root.iterdir()}
            result = knowledge_base.diagnose_import(str(root))
            after = {item.name: item.read_bytes() for item in root.iterdir()}
            self.assertEqual(before, after)
            self.assertEqual(3, result["file_count"])
            self.assertEqual(1, result["duplicate_group_count"])
            self.assertEqual(["image.xyz"], result["unknown_files"])
            self.assertFalse(result["classification"]["available"])

    def test_core_candidates_exclude_internal_recovery_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            visible = vault / "00_系统" / "背景.md"
            internal = vault / ".jiedu" / "versions" / "恢复副本.md"
            visible.parent.mkdir(parents=True)
            internal.parent.mkdir(parents=True)
            visible.write_text("user content", "utf-8")
            internal.write_text("internal backup", "utf-8")
            self.assertIn("00_系统/背景.md", knowledge_base.markdown_files(vault))
            self.assertNotIn(".jiedu/versions/恢复副本.md", knowledge_base.markdown_files(vault))

    def test_reconnecting_existing_workspace_keeps_user_selected_core(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "machine" / "knowledge-base.json"
            audit = root / "machine" / "knowledge-base-audit.json"
            vault = root / "vault"
            with patch.object(knowledge_base, "CONFIG_PATH", config), patch.object(knowledge_base, "AUDIT_PATH", audit):
                knowledge_base.initialize(str(vault), owner_confirmed=True)
                selected = vault / "Custom Notes" / "My core.md"
                selected.parent.mkdir(parents=True)
                selected.write_text("# My core", "utf-8")
                knowledge_base.save_core_files(["Custom Notes/My core.md"])
                knowledge_base.connect(str(vault))
                self.assertEqual(["Custom Notes/My core.md"], knowledge_base.load()["core_files"])
                knowledge_base.save_core_files([])
                self.assertEqual([], knowledge_base.load()["core_files"])

    def test_connect_existing_obsidian_folder_keeps_notes_and_no_core_requirement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = root / "obsidian"
            note = vault / "Projects" / "meeting.md"
            attachment = vault / "Projects" / "sketch.png"
            settings = vault / ".obsidian" / "app.json"
            note.parent.mkdir(parents=True)
            settings.parent.mkdir(parents=True)
            note.write_text("# Meeting\n[[Other]] ![](sketch.png)", "utf-8")
            attachment.write_bytes(b"attachment bytes")
            settings.write_text("{}", "utf-8")
            before = {path.relative_to(vault): path.read_bytes() for path in (note, attachment, settings)}
            config = root / "machine" / "knowledge-base.json"
            audit = root / "machine" / "knowledge-base-audit.json"
            with patch.object(knowledge_base, "CONFIG_PATH", config), patch.object(knowledge_base, "AUDIT_PATH", audit):
                knowledge_base.connect(str(vault))
                self.assertEqual([], knowledge_base.load()["core_files"])
                self.assertIn("Projects/meeting.md", knowledge_base.markdown_files(vault))
                for relative, data in before.items():
                    self.assertEqual(data, (vault / relative).read_bytes())

    def test_automatic_extraction_provider_contract_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            package_file = Path(tmp) / "package.md"
            package_file.write_text("source package", encoding="utf-8")
            config = {
                "provider_type": "openai-compatible", "enabled": True,
                "base_url": "https://example.invalid/v1", "model": "local-test-model",
                "api_key_secret_ref": "env:KB_TEST_KEY", "timeout": 30,
                "max_input": 10000, "extraction_prompt": "return candidates",
            }
            imported, seen = {}, {}

            def fake_import(payload, **kwargs):
                imported.update(payload); imported.update(kwargs)
                return {"imported": 1}

            class Response:
                def __enter__(self): return self
                def __exit__(self, *_): return False
                def read(self):
                    payload = {"version": "KB_MEMORY_CANDIDATES_V1", "package_id": "pkg-1", "candidates": []}
                    return json.dumps({"choices": [{"message": {"content": json.dumps(payload)}}]}).encode("utf-8")

            def fake_urlopen(request, timeout):
                seen.update(url=request.full_url, timeout=timeout, authorization=request.headers["Authorization"])
                return Response()

            with patch.object(automatic_extraction, "load_provider", return_value=config), \
                 patch.object(automatic_extraction, "export_package", return_value={"package_id": "pkg-1", "path": str(package_file)}), \
                 patch.object(automatic_extraction, "import_result", side_effect=fake_import), \
                 patch.object(automatic_extraction, "record_call"), \
                 patch.object(automatic_extraction.urllib.request, "urlopen", side_effect=fake_urlopen), \
                 patch.dict(os.environ, {"KB_TEST_KEY": "fixture-secret"}):
                result = automatic_extraction.extract(["session-1"])
            self.assertEqual(result["import"]["imported"], 1)
            self.assertEqual(seen, {"url": "https://example.invalid/v1/chat/completions", "timeout": 30, "authorization": "Bearer fixture-secret"})
            self.assertEqual(imported["provider"], "openai-compatible")
            self.assertEqual(imported["provider_model"], "local-test-model")


if __name__ == "__main__":
    unittest.main()
