import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from knowledge_bridge.knowledge_objects import (
    KnowledgeObjectConflict,
    KnowledgeObjectJobManager,
    KnowledgeObjectService,
)
from knowledge_bridge.protected_storage import EncryptedProtectedStorageProvider
from knowledge_bridge.storage import StorageCancelledError, StorageOperation
from knowledge_bridge.workspace import initialize_workspace


PASSPHRASE = "fictional-knowledge-object-passphrase-2026"
TEST_KDF = {
    "name": "argon2id", "iterations": 2, "lanes": 1,
    "memory_cost_kib": 19 * 1024, "length": 32,
}


class KnowledgeObjectTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.context = initialize_workspace(self.base / "workspace")
        self.provider = EncryptedProtectedStorageProvider(self.context)
        self.provider.initialize(PASSPHRASE, kdf_parameters=TEST_KDF)
        self.service = KnowledgeObjectService(self.context, self.provider)
        self.external = self.base / "external"
        self.external.mkdir()

    def tearDown(self):
        self.provider.lock()
        self.temporary.cleanup()

    def _write_external(self, relative: str, content: str) -> Path:
        path = self.external / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_unified_list_hides_protected_metadata_while_locked(self):
        public = self.context.knowledge / "公开.md"
        public.write_text("公开正文", encoding="utf-8")
        secret = self._write_external("绝密标题.md", "fictional protected body")
        preview = self.service.preview_import(str(self.external), "protected")
        self.service.execute_import(preview["preview_id"], owner_confirmed=True)

        unlocked = self.service.list_objects(include_protected=True)
        self.assertEqual(1, unlocked["counts"]["protected"])
        self.assertIn("绝密标题.md", json.dumps(unlocked, ensure_ascii=False))
        self.provider.lock()
        locked = self.service.list_objects(include_protected=True)
        self.assertIsNone(locked["counts"]["protected"])
        self.assertNotIn("绝密标题.md", json.dumps(locked, ensure_ascii=False))
        self.assertNotIn(secret.read_text("utf-8"), self.service.registry_path.read_text("utf-8"))

    def test_open_mixed_import_preserves_originals_and_writes_traceable_derivatives(self):
        text = self._write_external("notes/readme.txt", "fictional mixed import sentinel")
        rtf = self.external / "report.rtf"
        rtf.write_text(r"{\rtf1\ansi Fictional AnyDoc report sentinel}", encoding="ascii")
        original_hashes = {path.name: path.read_bytes() for path in (text, rtf)}
        preview = self.service.preview_import(str(self.external), "open")
        self.assertEqual({"ready"}, {row["conversion"]["status"] for row in preview["items"]})
        result = self.service.execute_import(preview["preview_id"], owner_confirmed=True)
        self.assertEqual("completed", result["status"])
        self.assertTrue(result["source_preserved"])
        self.assertFalse(result["conversion"]["ocr_uploaded"])
        self.assertEqual(original_hashes["readme.txt"], text.read_bytes())
        self.assertEqual(original_hashes["report.rtf"], rtf.read_bytes())
        derived = list((self.context.inbox / "导入" / self.external.name / "_派生知识").rglob("*.md"))
        self.assertEqual(2, len(derived))
        combined = "\n".join(path.read_text("utf-8") for path in derived)
        self.assertIn("jiedu_derived: true", combined)
        self.assertIn("source_sha256:", combined)
        self.assertIn("fictional mixed import sentinel", combined)
        self.assertIn("Fictional AnyDoc report sentinel", combined)

    def test_unsupported_file_is_preserved_with_explicit_conversion_result(self):
        binary = self.external / "sample.bin"
        binary.write_bytes(b"fictional-binary")
        preview = self.service.preview_import(str(self.external), "open")
        self.assertEqual("unsupported", preview["items"][0]["conversion"]["status"])
        result = self.service.execute_import(preview["preview_id"], owner_confirmed=True)
        self.assertEqual(1, result["conversion"]["counts"]["unsupported"])
        self.assertTrue((self.context.inbox / "导入" / self.external.name / "sample.bin").is_file())

    def test_live_evidence_archive_is_excluded_from_object_browse_and_migration(self):
        archive = self.context.raw_archive / "ChatGPT" / "抖音工作流"
        archive.mkdir(parents=True)
        (archive / "conversation.jsonl").write_text(
            '{"session_id":"chatgpt:6a69a62a-880c-83e8-b41f-649c83799121"}\n',
            encoding="utf-8",
        )
        listing = self.service.list_objects(include_protected=True)
        rendered = json.dumps(listing, ensure_ascii=False)
        self.assertNotIn("抖音工作流", rendered)
        self.assertNotIn("6a69a62a-880c-83e8-b41f-649c83799121", rendered)
        with self.assertRaises(ValueError):
            self.service.preview_migration(["90_原始档案/ChatGPT/抖音工作流/conversation.jsonl"])
        with self.assertRaises(ValueError):
            self.service.preview_migration([str(archive / "conversation.jsonl")])
        with self.assertRaises(ValueError):
            self.service.preview_migration(["03_知识/../../outside.md"])

    def test_existing_obsidian_notes_and_attachments_remain_visible_without_relocation(self):
        note = self.context.root / "My Notes" / "meeting.md"
        image = self.context.root / "My Notes" / "sketch.png"
        config = self.context.root / ".obsidian" / "app.json"
        note.parent.mkdir(parents=True)
        config.parent.mkdir(parents=True)
        note.write_text("# Meeting\nSee [[Other]] and ![](sketch.png)", "utf-8")
        image.write_bytes(b"fictional image bytes")
        config.write_text('{}', 'utf-8')
        items = self.service.list_objects()["items"]
        paths = {row["logical_path"] for row in items}
        self.assertIn("My Notes/meeting.md", paths)
        self.assertIn("My Notes/sketch.png", paths)
        self.assertNotIn(".obsidian/app.json", paths)
        detail = self.service.object_detail(next(row["entry_id"] for row in items if row["logical_path"] == "My Notes/meeting.md"))
        self.assertIn("[[Other]]", detail["content"])
        self.assertEqual('{}', config.read_text('utf-8'))

    def test_workspace_symlink_is_not_a_migration_object(self):
        link = self.context.knowledge / "link.md"
        link.write_text("fictional link target", encoding="utf-8")
        original_is_symlink = Path.is_symlink

        def identify_link(path):
            return path == link or original_is_symlink(path)

        with patch.object(Path, "is_symlink", identify_link):
            with self.assertRaises(ValueError):
                self.service.preview_migration(["03_知识/link.md"])

    def test_import_policy_chain_overrides_and_preserves_external_source(self):
        sources = {
            "公开.md": self._write_external("公开.md", "open body"),
            "保护.md": self._write_external("保护.md", "protected body"),
            "待定.md": self._write_external("待定.md", "pending body"),
        }
        before = {name: path.read_bytes() for name, path in sources.items()}
        preview = self.service.preview_import(
            str(self.external), "protected",
            {"公开.md": "open", "待定.md": "pending"},
        )
        self.assertEqual({"open": 1, "protected": 1, "pending": 1}, preview["counts"])
        result = self.service.execute_import(preview["preview_id"], owner_confirmed=True)
        self.assertTrue(result["source_preserved"])
        self.assertTrue((self.context.inbox / "导入" / "external" / "公开.md").is_file())
        self.assertEqual(before, {name: path.read_bytes() for name, path in sources.items()})

    def test_same_name_conflict_and_repeat_import_are_deterministic(self):
        source = self._write_external("资料.md", "first")
        first = self.service.preview_import(str(self.external), "open")
        self.service.execute_import(first["preview_id"], owner_confirmed=True)
        repeat = self.service.preview_import(str(self.external), "open")
        self.assertTrue(repeat["items"][0]["idempotent"])
        self.assertEqual(0, repeat["conflicts"])
        self.service.execute_import(repeat["preview_id"], owner_confirmed=True)
        source.write_text("changed", encoding="utf-8")
        conflict = self.service.preview_import(str(self.external), "open")
        self.assertEqual(1, conflict["conflicts"])
        with self.assertRaises(KnowledgeObjectConflict):
            self.service.execute_import(conflict["preview_id"], owner_confirmed=True)

    def test_migration_switches_catalog_before_removing_public_source(self):
        public = self.context.knowledge / "迁移.md"
        public.write_text("migration body", encoding="utf-8")
        preview = self.service.preview_migration(["03_知识/迁移.md"])
        events = []
        result = self.service.execute_migration(
            preview["preview_id"], owner_confirmed=True,
            progress=lambda row: events.append(row["stage"]),
        )
        self.assertEqual("completed", result["status"])
        self.assertFalse(public.exists())
        self.assertLess(events.index("switch"), events.index("cleanup"))
        index = json.loads(self.service.object_index_path.read_text("utf-8"))
        self.assertTrue(index["catalog_available"])
        detail = next(row for row in self.service.list_objects(include_protected=True)["items"]
                      if row["logical_path"] == "03_知识/迁移.md")
        self.assertEqual("migration body", self.service.object_detail(
            detail["entry_id"], include_protected=True)["content"])

    def test_migration_cleanup_failure_rolls_back_pointer_and_public_source(self):
        public = self.context.knowledge / "回滚.md"
        public.write_text("rollback body", encoding="utf-8")
        preview = self.service.preview_migration(["03_知识/回滚.md"])
        original_unlink = Path.unlink

        def fail_public_unlink(path, *args, **kwargs):
            if path == public:
                raise OSError("fictional cleanup failure")
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", fail_public_unlink):
            with self.assertRaises(OSError):
                self.service.execute_migration(preview["preview_id"], owner_confirmed=True)
        self.assertEqual("rollback body", public.read_text("utf-8"))
        self.assertEqual(0, self.service._registry()["generation"])
        self.assertFalse(json.loads(self.service.object_index_path.read_text("utf-8"))["catalog_available"])
        self.assertEqual([], self.service._catalog())

    def test_cancel_before_copy_changes_nothing(self):
        public = self.context.knowledge / "取消.md"
        public.write_text("cancel body", encoding="utf-8")
        preview = self.service.preview_migration(["03_知识/取消.md"])
        with self.assertRaises(StorageCancelledError):
            self.service.execute_migration(
                preview["preview_id"], owner_confirmed=True, cancel=lambda: True)
        self.assertEqual("cancel body", public.read_text("utf-8"))
        self.assertEqual([], self.service._catalog())

    def test_plaintext_state_and_audit_do_not_contain_protected_metadata(self):
        self._write_external("private-name-sentinel.md", "private-body-sentinel")
        preview = self.service.preview_import(str(self.external), "protected")
        self.service.execute_import(preview["preview_id"], owner_confirmed=True)
        for path in (self.service.registry_path, self.service.object_index_path, self.service.audit_path):
            text = path.read_text("utf-8")
            self.assertNotIn("private-name-sentinel", text)
            self.assertNotIn("private-body-sentinel", text)

    def test_import_failure_after_catalog_switch_removes_new_catalog_and_content(self):
        self._write_external("保护.md", "protected rollback body")
        preview = self.service.preview_import(str(self.external), "protected")
        with patch.object(self.service, "_audit", side_effect=OSError("fictional audit failure")):
            with self.assertRaises(OSError):
                self.service.execute_import(preview["preview_id"], owner_confirmed=True)
        self.assertIsNone(self.service._registry()["catalog_head"])
        self.assertEqual([], self.service._catalog())
        self.assertEqual([], self.provider.list_objects(
            "content/", self.service._authorization(StorageOperation.LIST)))
        self.assertEqual([], self.provider.list_objects(
            "system/knowledge-catalog/", self.service._authorization(StorageOperation.LIST)))

    def test_job_restart_marks_unfinished_work_interrupted(self):
        manager = KnowledgeObjectJobManager(self.service)
        manager.root.mkdir(parents=True, exist_ok=True)
        job_id = "a" * 32
        manager._save({"job_id": job_id, "kind": "migration", "status": "running"})
        recovered = KnowledgeObjectJobManager(self.service).get(job_id)
        self.assertEqual("interrupted", recovered["status"])
        self.assertTrue(recovered["source_preserved"])


WEB_SCRIPT = r'''
import json, os, time
from pathlib import Path
from fastapi.testclient import TestClient
from knowledge_bridge.workspace import initialize_workspace

root = Path(os.environ["KB_TEST_WORKSPACE"])
context = initialize_workspace(root)
(context.knowledge / "owner-gated.md").write_text("owner gated migration", encoding="utf-8")
from knowledge_bridge.web import app
origin = "http://127.0.0.1:8765"
result = {}
with TestClient(app, base_url=origin) as client:
    listing = client.get("/api/knowledge-objects")
    result["listing"] = listing.status_code
    result["cache"] = listing.headers.get("cache-control")
    preview = client.post("/api/knowledge-objects/migration/preview",
        json={"paths":["03_知识/owner-gated.md"]}, headers={"Origin":origin})
    result["preview"] = preview.status_code
    blocked = client.post("/api/knowledge-objects/jobs/migration",
        json={"preview_id":"0"*32}, headers={"Origin":origin})
    result["blocked"] = blocked.status_code
    status = client.get("/api/protection/status").json()
    initialized = client.post("/api/protection/initialize",
        json={"passphrase":"fictional-web-owner-passphrase-2026","confirmation":"fictional-web-owner-passphrase-2026"},
        headers={"Origin":origin,"X-Jiedu-CSRF":status["csrf_token"]})
    result["initialized"] = initialized.status_code
    csrf = initialized.json()["csrf_token"]
    preview = client.post("/api/knowledge-objects/migration/preview",
        json={"paths":["03_知识/owner-gated.md"]}, headers={"Origin":origin})
    started = client.post("/api/knowledge-objects/jobs/migration",
        json={"preview_id":preview.json()["preview_id"]},
        headers={"Origin":origin,"X-Jiedu-CSRF":csrf})
    result["started"] = started.status_code
    job = started.json()
    for _ in range(100):
        job = client.get("/api/knowledge-objects/jobs/"+job["job_id"]).json()
        if job["status"] not in {"queued","running"}: break
        time.sleep(.03)
    result["job"] = job["status"]
    visible = client.get("/api/knowledge-objects").json()
    result["protected_visible"] = visible["counts"]["protected"]
print(json.dumps(result, ensure_ascii=False))
'''


class KnowledgeObjectWebTests(unittest.TestCase):
    def test_http_owner_gate_no_store_and_successful_migration(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            environment = os.environ.copy()
            environment.update({
                "KB_TEST_WORKSPACE": str(base / "workspace"),
                "KNOWLEDGE_BRIDGE_VAULT": str(base / "workspace"),
                "KNOWLEDGE_BRIDGE_LOCAL_DATA": str(base / "local"),
                "KNOWLEDGE_BRIDGE_PROTECTION_TEST_MODE": "1",
                "PYTHONUTF8": "1",
            })
            project_root = Path(__file__).resolve().parents[1]
            runtime = project_root / ".venv312" / "Scripts" / "python.exe"
            completed = subprocess.run(
                [str(runtime if runtime.exists() else Path(sys.executable)), "-c", WEB_SCRIPT],
                cwd=project_root, env=environment, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=60,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            data = json.loads(completed.stdout.strip().splitlines()[-1])
            self.assertEqual(200, data["listing"])
            self.assertIn("no-store", data["cache"])
            self.assertEqual(409, data["preview"])
            self.assertEqual(403, data["blocked"])
            self.assertEqual(200, data["initialized"])
            self.assertEqual(200, data["started"])
            self.assertEqual("completed", data["job"])
            self.assertEqual(1, data["protected_visible"])


if __name__ == "__main__":
    unittest.main()
