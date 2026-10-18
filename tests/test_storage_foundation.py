import tempfile
import io
import unittest
from unittest.mock import patch
from pathlib import Path

from knowledge_bridge.archive_store import ArchiveStore
from knowledge_bridge.session_archive import read_message
from knowledge_bridge.storage import (
    OpenFileStorageProvider, ProviderAvailability, StorageAuthorization,
    StorageOperation, StoragePolicy, StoragePolicyPendingError,
    StoragePolicyResolver, StorageRouter, StorageUnavailableError,
    UnavailableProtectedStorageProvider, workspace_storage_router,
)
from knowledge_bridge.workspace import initialize_workspace, load_workspace


ALL_OPEN_OPERATIONS = StorageAuthorization.allow(
    StorageOperation.WRITE_ORIGINAL, StorageOperation.READ, StorageOperation.LIST,
    StorageOperation.LOCATE, StorageOperation.VERIFY, StorageOperation.EXPORT,
)


class StoragePolicyTests(unittest.TestCase):
    def test_existing_workspace_and_missing_policy_remain_open_without_migration(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = initialize_workspace(Path(temporary) / "workspace")
            marker = context.root / "03_知识" / "既有资料.md"
            marker.write_text("不迁移", encoding="utf-8")
            loaded = load_workspace(context.root)
            resolved = StoragePolicyResolver().resolve(item={"content_classification": "敏感"})
            self.assertEqual(StoragePolicy.OPEN, resolved.policy)
            self.assertEqual("compatibility_default", resolved.decided_by)
            self.assertTrue(resolved.defaulted)
            self.assertEqual("不迁移", marker.read_text(encoding="utf-8"))
            self.assertFalse(loaded.protected_root.exists())

    def test_policy_precedence_is_item_session_project_import_source_workspace(self):
        resolved = StoragePolicyResolver().resolve(
            workspace="protected", source="pending", import_task="open",
            project="protected", session="pending", item="open",
        )
        self.assertEqual(StoragePolicy.OPEN, resolved.policy)
        self.assertEqual("item", resolved.decided_by)
        inherited = StoragePolicyResolver().resolve(
            workspace="protected", source="open", import_task="inherit",
            project=None, session="inherit", item=None,
        )
        self.assertEqual(StoragePolicy.OPEN, inherited.policy)
        self.assertEqual("source", inherited.decided_by)
        self.assertEqual("open", inherited.audit_record()["policy"])

    def test_classification_and_source_do_not_implicitly_choose_storage(self):
        resolver = StoragePolicyResolver()
        for metadata in (
            {"content_classification": "raw_codex_conversation"},
            {"source": "ChatGPT", "project": "界渡"},
            {"mime_type": "image/png", "secret": True},
        ):
            self.assertEqual(StoragePolicy.OPEN, resolver.resolve(item=metadata).policy)
        self.assertEqual(StoragePolicy.PROTECTED,
                         resolver.resolve(item={"storage_policy": "protected", "source": "public"}).policy)


class StorageProviderTests(unittest.TestCase):
    def test_open_provider_round_trip_integrity_listing_and_export(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "open"
            provider = OpenFileStorageProvider(root)
            reference = provider.write_original("原始/证据.bin", b"fictional-evidence", ALL_OPEN_OPERATIONS)
            duplicate = provider.write_original("原始/证据.bin", b"fictional-evidence", ALL_OPEN_OPERATIONS)
            self.assertEqual(reference, duplicate)
            self.assertEqual(b"fictional-evidence", provider.read_object(reference.object_id, ALL_OPEN_OPERATIONS))
            self.assertEqual([reference], provider.list_objects("原始", ALL_OPEN_OPERATIONS))
            self.assertTrue(provider.verify_integrity(reference, ALL_OPEN_OPERATIONS))
            self.assertEqual(str((root / "原始" / "证据.bin").resolve()),
                             provider.locate_object(reference.object_id, ALL_OPEN_OPERATIONS))
            exported = provider.export_object(reference.object_id, Path(temporary) / "export" / "证据.bin",
                                                ALL_OPEN_OPERATIONS)
            self.assertEqual(reference.sha256, exported.sha256)
            status = provider.status()
            self.assertEqual(ProviderAvailability.AVAILABLE, status.availability)
            self.assertFalse(status.protection_active)
            self.assertTrue(status.capabilities.ordinary_filesystem_visible)

    def test_open_provider_streaming_contract_is_chunked_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = OpenFileStorageProvider(Path(temporary) / "open")
            payload = b"stream-fixture" * 10000
            progress = []
            reference = provider.write_stream(
                "stream/object.bin", io.BytesIO(payload), ALL_OPEN_OPERATIONS,
                total_size=len(payload), chunk_size=64 * 1024,
                progress=lambda event: progress.append(event.chunk_bytes))
            destination = io.BytesIO()
            read = provider.read_to_stream(reference.object_id, destination, ALL_OPEN_OPERATIONS)
            self.assertEqual(reference, read)
            self.assertEqual(payload, destination.getvalue())
            self.assertLessEqual(max(progress), 64 * 1024)
            self.assertEqual(reference, provider.write_stream(
                reference.object_id, io.BytesIO(payload), ALL_OPEN_OPERATIONS,
                total_size=len(payload), chunk_size=64 * 1024))

    def test_original_write_refuses_conflicting_overwrite_and_path_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = OpenFileStorageProvider(Path(temporary) / "open")
            provider.write_original("same.bin", b"one", ALL_OPEN_OPERATIONS)
            with self.assertRaises(FileExistsError):
                provider.write_original("same.bin", b"two", ALL_OPEN_OPERATIONS)
            with self.assertRaises(ValueError):
                provider.write_original("../escape.bin", b"bad", ALL_OPEN_OPERATIONS)

    def test_delete_and_migration_require_explicit_owner_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = OpenFileStorageProvider(Path(temporary) / "open")
            target = OpenFileStorageProvider(Path(temporary) / "target")
            provider.write_original("keep.bin", b"keep", ALL_OPEN_OPERATIONS)
            with self.assertRaises(PermissionError):
                provider.delete_object("keep.bin", StorageAuthorization.allow(StorageOperation.DELETE))
            self.assertTrue((Path(temporary) / "open" / "keep.bin").is_file())
            provider.delete_object("keep.bin", StorageAuthorization.allow(
                StorageOperation.DELETE, owner_confirmed=True, reason="fictional test cleanup"))
            self.assertFalse((Path(temporary) / "open" / "keep.bin").exists())
            provider.write_original("move.bin", b"move", ALL_OPEN_OPERATIONS)
            with self.assertRaises(PermissionError):
                provider.migrate_object("move.bin", target, "moved.bin", StorageAuthorization.allow(
                    StorageOperation.MIGRATE, StorageOperation.READ, StorageOperation.WRITE_ORIGINAL,
                    StorageOperation.VERIFY, StorageOperation.DELETE))
            migration = StorageAuthorization.allow(
                StorageOperation.MIGRATE, StorageOperation.READ, StorageOperation.WRITE_ORIGINAL,
                StorageOperation.VERIFY, StorageOperation.DELETE,
                owner_confirmed=True, reason="fictional migration",
            )
            moved = provider.migrate_object("move.bin", target, "moved.bin", migration)
            self.assertTrue(target.verify_integrity(moved, migration))
            self.assertFalse((Path(temporary) / "open" / "move.bin").exists())

    def test_unconfigured_protected_and_pending_fail_closed_without_plaintext(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = initialize_workspace(Path(temporary) / "workspace")
            router = workspace_storage_router(context)
            protected = StoragePolicyResolver().resolve(item="protected")
            with self.assertRaises(StorageUnavailableError):
                router.provider_for(protected)
            with self.assertRaises(StoragePolicyPendingError):
                router.provider_for(StoragePolicyResolver().resolve(item="pending"))
            self.assertFalse(context.protected_root.exists())
            self.assertEqual([], list(context.root.rglob("*protected*")))
            status = router.protected_provider.status()
            self.assertEqual(ProviderAvailability.NOT_CONFIGURED, status.availability)
            self.assertFalse(status.protection_active)
            self.assertFalse(status.capabilities.write_original)

    def test_router_never_falls_back_when_protected_provider_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            open_root = Path(temporary) / "ordinary"
            router = StorageRouter(OpenFileStorageProvider(open_root), UnavailableProtectedStorageProvider())
            with self.assertRaises(StorageUnavailableError):
                router.provider_for(StoragePolicyResolver().resolve(workspace="protected"))
            self.assertFalse(open_root.exists())

    def test_archive_store_public_flow_still_works_in_temporary_workspace(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = initialize_workspace(Path(temporary) / "workspace")
            attachment = Path(temporary) / "fictional.png"
            attachment.write_bytes(b"not-a-real-image")
            store = ArchiveStore(context)
            session = store.ensure_session(source_key="codex", source_session_id="fixture-session",
                                           title="虚构会话", source_instance="fixture")
            saved = store.append_message(session["session_key"], message_id="fixture-message", role="user",
                                         content="虚构内容", attachment_paths=[attachment])
            self.assertTrue(saved["saved"])
            materialized = store.materialize_session(session["session_key"])
            self.assertTrue((Path(materialized["archive_path"]) / "01_对话" / "conversation.md").is_file())
            self.assertFalse(context.protected_root.exists())

    def test_message_read_uses_session_identity_and_survives_later_appends(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = initialize_workspace(Path(temporary) / "workspace")
            store = ArchiveStore(context)
            first = store.ensure_session(source_key="codex", source_session_id="session-one",
                                         title="会话一", source_instance="fixture")
            store.append_message(first["session_key"], message_id="same-id", role="user",
                                 content="第一份原文")
            with patch("knowledge_bridge.session_archive.active_workspace", return_value=context), \
                 patch("knowledge_bridge.session_archive.CANONICAL_ROOT", context.raw_archive):
                before = read_message("same-id", "session-one")
                store.append_message(first["session_key"], message_id="later-id", role="assistant",
                                     content="后来追加的消息")
                after = read_message("same-id", "session-one")
            self.assertEqual("第一份原文", after["content"])
            self.assertEqual(before["version"], after["version"])

            second = store.ensure_session(source_key="codex", source_session_id="session-two",
                                          title="会话二", source_instance="fixture")
            store.append_message(second["session_key"], message_id="same-id", role="user",
                                 content="第二份原文")
            with patch("knowledge_bridge.session_archive.active_workspace", return_value=context), \
                 patch("knowledge_bridge.session_archive.CANONICAL_ROOT", context.raw_archive):
                self.assertEqual("第二份原文", read_message("same-id", "session-two")["content"])
                with self.assertRaisesRegex(ValueError, "多个会话"):
                    read_message("same-id")


if __name__ == "__main__":
    unittest.main()
