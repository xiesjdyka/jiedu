import json
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from knowledge_bridge.knowledge_objects import KnowledgeObjectConflict, KnowledgeObjectError, KnowledgeObjectService
from knowledge_bridge.protected_storage import InvalidUnlockMaterial
from knowledge_bridge.protection_control import ProtectionLifecycleManager
from knowledge_bridge.workspace import initialize_workspace
from knowledge_bridge.storage import StorageOperation


PASSWORD = "fictional-current-password-2026"
NEW_PASSWORD = "fictional-reopened-password-2026"
KDF = {"name": "argon2id", "iterations": 2, "lanes": 1,
       "memory_cost_kib": 19 * 1024, "length": 32}


class ProtectionExitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.context = initialize_workspace(self.base / "vault")
        self.settings = self.base / "machine" / "protection-control.json"
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text(json.dumps({"format_version": 2, "workspaces": {
            self.context.workspace_id: {"protected_storage_enabled": False,
                                        "access_mode": "manual"}}}), "utf-8")
        self.manager = self._manager()
        self.manager.enable_new_storage(PASSWORD)
        self.service = KnowledgeObjectService(self.context, self.manager.provider)
        self.source = self.base / "external"
        (self.source / "notes").mkdir(parents=True)
        (self.source / "notes" / "memo.txt").write_text("unaltered source", "utf-8")
        preview = self.service.preview_import(str(self.source), "protected")
        self.service.execute_import(preview["preview_id"], owner_confirmed=True)

    def tearDown(self):
        self.manager.close()
        self.temp.cleanup()

    def _manager(self):
        return ProtectionLifecycleManager(self.context, settings_path=self.settings,
            default_auto_lock_seconds=0, failure_delay_base=0,
            initialization_kdf_parameters=KDF)

    def test_exit_exports_all_catalog_items_and_can_set_new_password(self):
        before = self.service._catalog()
        with self.assertRaises(InvalidUnlockMaterial):
            self.service.exit_protection_to_open(self.manager, "wrong-password")
        self.assertTrue(self.manager.storage_enabled)
        result = self.service.exit_protection_to_open(self.manager, PASSWORD)
        self.assertEqual("disabled", result["status"])
        self.assertEqual(len(before), result["exported"])
        self.assertTrue(Path(result["encrypted_backup"]).is_dir())
        for row in before:
            path = self.context.root / row["logical_path"]
            self.assertTrue(path.is_file())
            self.assertEqual(row["sha256"], __import__("hashlib").sha256(path.read_bytes()).hexdigest())
        self.assertEqual("unaltered source", (self.source / "notes" / "memo.txt").read_text("utf-8"))
        self.assertIsNone(self.service._registry()["catalog_head"])
        self.manager.close()
        self.manager = self._manager()
        self.assertFalse(self.manager.storage_enabled)
        self.assertEqual("disabled", self.manager.status()["state"])
        self.manager.enable_new_storage(NEW_PASSWORD)
        self.service.protected = self.manager.provider
        self.assertTrue(self.manager.provider.verify_passphrase(NEW_PASSWORD))
        self.assertEqual(0, self.service.list_objects(include_protected=True)["counts"]["protected"])

    def test_conflicting_plaintext_blocks_exit_and_keeps_ciphertext(self):
        original_catalog = self.service._catalog()
        row = original_catalog[0]
        path = self.context.root / row["logical_path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("different ordinary content", "utf-8")
        with self.assertRaises(KnowledgeObjectConflict):
            self.service.exit_protection_to_open(self.manager, PASSWORD)
        self.assertTrue(self.manager.storage_enabled)
        self.assertEqual(len(original_catalog), len(self.service._catalog()))
        self.assertEqual("different ordinary content", path.read_text("utf-8"))

    def _destinations(self):
        return [self.context.root / row["logical_path"] for row in self.service._catalog()]

    def _journal(self):
        return json.loads((self.context.state / "protection-exit.json").read_text("utf-8"))

    @unittest.skipUnless(os.name == "nt", "identity-bound deletion uses Windows handles")
    def test_disable_failure_rolls_back_only_new_plaintext(self):
        paths = self._destinations()
        before = self.service._registry()["catalog_head"]
        with patch.object(self.manager, "disable_after_export", side_effect=OSError("injected")):
            with self.assertRaises(KnowledgeObjectError):
                self.service.exit_protection_to_open(self.manager, PASSWORD)
        self.assertTrue(self.manager.storage_enabled)
        self.assertTrue(all(not path.exists() for path in paths))
        self.assertEqual(before, self.service._registry()["catalog_head"])
        self.assertEqual("rolled_back", self._journal()["status"])
        self.assertIsNone(self.manager.status()["exit_recovery"])
        self.assertEqual(len(paths), len(self.service._catalog()))

    @unittest.skipUnless(os.name == "nt", "identity-bound deletion uses Windows handles")
    def test_preexisting_matching_file_is_never_removed(self):
        row = self.service._catalog()[0]
        path = self.context.root / row["logical_path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.service.protected.read_object(row["content_object"],
            self.service._authorization(StorageOperation.READ)))
        identity = path.stat().st_ino
        with patch.object(self.manager, "disable_after_export", side_effect=OSError("injected")):
            with self.assertRaises(KnowledgeObjectError):
                self.service.exit_protection_to_open(self.manager, PASSWORD)
        self.assertEqual(identity, path.stat().st_ino)
        self.assertEqual("rolled_back", self._journal()["status"])

    def test_external_modified_copy_is_preserved_and_warns_after_restart(self):
        path = self._destinations()[0]
        def fail(**kwargs):
            path.write_text("external edit", "utf-8")
            raise OSError("injected")
        with patch.object(self.manager, "disable_after_export", side_effect=fail):
            with self.assertRaisesRegex(KnowledgeObjectError, "可能存在明文"):
                self.service.exit_protection_to_open(self.manager, PASSWORD)
        self.assertEqual("external edit", path.read_text("utf-8"))
        self.assertEqual("recovery_required", self._journal()["status"])
        self.manager.close()
        self.manager = self._manager()
        self.assertTrue(self.manager.status()["exit_recovery"]["plaintext_exposure_possible"])
        self.assertFalse(self.manager.status()["protection_active"])

    def test_external_replacement_even_with_same_hash_is_preserved(self):
        path = self._destinations()[0]
        new_identity = None
        def fail(**kwargs):
            nonlocal new_identity
            replacement = path.with_suffix(".replacement")
            replacement.write_bytes(path.read_bytes())
            os.replace(replacement, path)
            new_identity = path.stat().st_ino
            raise OSError("injected")
        with patch.object(self.manager, "disable_after_export", side_effect=fail):
            with self.assertRaises(KnowledgeObjectError):
                self.service.exit_protection_to_open(self.manager, PASSWORD)
        self.assertEqual(new_identity, path.stat().st_ino)
        self.assertEqual("recovery_required", self._journal()["status"])

    def test_concurrent_destination_is_not_overwritten(self):
        link = os.link
        raced = []
        def collide(source, destination):
            Path(destination).write_text("concurrent ordinary file", "utf-8")
            raced.append(Path(destination))
            return link(source, destination)
        with patch("knowledge_bridge.knowledge_objects.os.link", side_effect=collide):
            with self.assertRaises(KnowledgeObjectError):
                self.service.exit_protection_to_open(self.manager, PASSWORD)
        self.assertEqual("concurrent ordinary file", raced[0].read_text("utf-8"))
        self.assertTrue(self.manager.storage_enabled)

    def test_interruption_after_link_keeps_durable_identity_and_warning(self):
        link = os.link
        def interrupt(source, destination):
            link(source, destination)
            raise SystemExit("simulated termination before receipt update")
        with patch("knowledge_bridge.knowledge_objects.os.link", side_effect=interrupt):
            with self.assertRaises(SystemExit):
                self.service.exit_protection_to_open(self.manager, PASSWORD)
        journal = self._journal()
        self.assertEqual("publishing", journal["status"])
        receipt = journal["files"][0]
        path = self.context.root / receipt["destination"]
        self.assertEqual(receipt["identity"], [path.stat().st_dev, path.stat().st_ino])
        self.manager.close()
        self.manager = self._manager()
        self.assertTrue(self.manager.status()["exit_recovery"]["plaintext_exposure_possible"])

    @unittest.skipUnless(os.name == "nt", "identity-bound deletion uses Windows handles")
    def test_registry_write_failure_restores_catalog_and_removes_copies(self):
        before = self.service._registry()["catalog_head"]
        paths = self._destinations()
        save = self.service._save_registry
        def fail_after_write(value):
            save(value)
            if value["catalog_head"] is None:
                raise OSError("injected after registry write")
        with patch.object(self.service, "_save_registry", side_effect=fail_after_write):
            with self.assertRaises(KnowledgeObjectError):
                self.service.exit_protection_to_open(self.manager, PASSWORD)
        self.assertEqual(before, self.service._registry()["catalog_head"])
        self.assertTrue(all(not path.exists() for path in paths))
        self.assertEqual("rolled_back", self._journal()["status"])

    def test_post_commit_failure_keeps_plaintext_and_cleared_registry(self):
        paths = self._destinations()
        disable = self.manager.disable_after_export
        def fail_after_commit(**kwargs):
            disable(**kwargs)
            raise OSError("injected after settings commit")
        with patch.object(self.manager, "disable_after_export", side_effect=fail_after_commit):
            with self.assertRaises(KnowledgeObjectError):
                self.service.exit_protection_to_open(self.manager, PASSWORD)
        self.assertFalse(self.manager.storage_enabled)
        self.assertIsNone(self.service._registry()["catalog_head"])
        self.assertTrue(all(path.exists() for path in paths))
        self.assertEqual("recovery_required", self._journal()["status"])

    @unittest.skipUnless(os.name == "nt", "identity-bound deletion uses Windows handles")
    def test_actual_retirement_settings_failure_restores_encrypted_bytes(self):
        root = self.manager.provider.root
        original = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in root.rglob('*') if p.is_file()}
        paths = self._destinations()
        with patch.object(self.manager, "_save_workspace_settings", side_effect=OSError("settings failure")):
            with self.assertRaises(KnowledgeObjectError):
                self.service.exit_protection_to_open(self.manager, PASSWORD)
        after = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in root.rglob('*') if p.is_file()}
        self.assertEqual(original, after)
        self.assertTrue(self.manager.storage_enabled)
        self.assertTrue(all(not p.exists() for p in paths))
        self.manager.unlock_with_passphrase(PASSWORD)
        self.assertEqual(len(paths), len(self.service._catalog()))
        self.assertEqual("rolled_back", self._journal()["status"])

    def test_cleanup_failure_keeps_durable_warning(self):
        paths = self._destinations()
        with patch.object(self.manager, "disable_after_export", side_effect=OSError("injected")), \
                patch("knowledge_bridge.knowledge_objects.remove_verified_file", return_value="retained_busy"):
            with self.assertRaisesRegex(KnowledgeObjectError, "回滚不完整"):
                self.service.exit_protection_to_open(self.manager, PASSWORD)
        self.assertTrue(all(p.exists() for p in paths))
        self.assertEqual("recovery_required", self._journal()["status"])
        self.assertIsNotNone(self.service.list_objects()["exit_recovery"])

    @unittest.skipUnless(os.name == "nt", "identity-bound deletion uses Windows handles")
    def test_settings_error_after_durable_write_restores_mode_on_restart(self):
        save = self.manager._save_workspace_settings
        def fail_after_write():
            save()
            raise OSError("injected after settings replacement")
        with patch.object(self.manager, "_save_workspace_settings", side_effect=fail_after_write):
            with self.assertRaises(KnowledgeObjectError):
                self.service.exit_protection_to_open(self.manager, PASSWORD)
        self.assertEqual("rolled_back", self._journal()["status"])
        self.manager.close()
        self.manager = self._manager()
        self.assertTrue(self.manager.storage_enabled)
        self.manager.unlock_with_passphrase(PASSWORD)
        self.service.protected = self.manager.provider
        self.assertTrue(self.service._catalog())


if __name__ == "__main__": unittest.main()
