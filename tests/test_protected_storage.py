import hashlib
import io
import json
import shutil
import tempfile
import threading
import tracemalloc
import unittest
from pathlib import Path

from knowledge_bridge.protected_storage import (
    EncryptedProtectedStorageProvider, InvalidUnlockMaterial, ProtectedIntegrityError,
    StorageCancelledError, protected_workspace_storage_router,
    temporary_performance_acceptance,
)
from knowledge_bridge.storage import (
    OpenFileStorageProvider, ProviderAvailability, StorageAuthorization, StorageOperation,
    StoragePolicyResolver, StorageUnavailableError,
)
from knowledge_bridge.workspace import initialize_workspace, load_workspace


PASSPHRASE = "fictional-passphrase-2026"
NEW_PASSPHRASE = "fictional-new-passphrase-2026"
TEST_KDF = {"name": "argon2id", "iterations": 2, "lanes": 1,
            "memory_cost_kib": 19 * 1024, "length": 32}
READ_WRITE = StorageAuthorization.allow(
    StorageOperation.WRITE_ORIGINAL, StorageOperation.READ, StorageOperation.LIST,
    StorageOperation.LOCATE, StorageOperation.VERIFY,
)
EXPORT = StorageAuthorization.allow(StorageOperation.EXPORT, owner_confirmed=True,
                                    reason="fictional explicit export")


def _files_digest(root: Path) -> dict[str, str]:
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*")) if path.is_file()}


class ProtectedStorageLifecycleTests(unittest.TestCase):
    def test_initialize_lock_password_and_recovery_unlock(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = initialize_workspace(Path(temporary) / "workspace")
            provider = EncryptedProtectedStorageProvider(context)
            self.assertEqual("not_configured", provider.status().lifecycle_state)
            initialized = provider.initialize(PASSPHRASE, kdf_parameters=TEST_KDF)
            recovery = initialized.recovery_key
            self.assertTrue(initialized.audit_recorded)
            self.assertFalse(hasattr(provider, "recovery_key"))
            self.assertEqual("unlocked", provider.status().lifecycle_state)
            self.assertTrue(provider.status().protection_active)
            provider.write_original("private/fictional.txt", b"fictional protected value", READ_WRITE)
            reference = provider.list_objects("private/", READ_WRITE)[0]
            provider.lock()
            self.assertEqual("locked", provider.status().lifecycle_state)
            self.assertFalse(provider.status().protection_active)
            with self.assertRaises(StorageUnavailableError):
                provider.read_object("private/fictional.txt", READ_WRITE)
            with self.assertRaises(StorageUnavailableError):
                provider.write_original("private/blocked.txt", b"blocked", READ_WRITE)
            with self.assertRaises(StorageUnavailableError):
                provider.verify_integrity(reference, READ_WRITE)
            with self.assertRaises(InvalidUnlockMaterial):
                provider.unlock_with_passphrase("wrong-passphrase-long-enough")
            with self.assertRaises(InvalidUnlockMaterial):
                provider.unlock_with_recovery("jiedu-recovery-v1:" + "A" * 43)
            self.assertEqual("locked", provider.status().lifecycle_state)
            provider.unlock_with_passphrase(PASSPHRASE)
            self.assertEqual(b"fictional protected value",
                             provider.read_object("private/fictional.txt", READ_WRITE))
            provider.lock()
            provider.unlock_with_recovery(recovery)
            self.assertEqual(b"fictional protected value",
                             provider.read_object("private/fictional.txt", READ_WRITE))

    def test_change_passphrase_only_rewraps_key_and_old_password_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = initialize_workspace(Path(temporary) / "workspace")
            provider = EncryptedProtectedStorageProvider(context)
            recovery = provider.initialize(PASSPHRASE, kdf_parameters=TEST_KDF).recovery_key
            reference = provider.write_original("secret/object", b"immutable ciphertext object", READ_WRITE)
            before = _files_digest(provider.objects_root)
            provider.change_passphrase(NEW_PASSPHRASE)
            self.assertEqual(before, _files_digest(provider.objects_root))
            provider.lock()
            with self.assertRaises(InvalidUnlockMaterial):
                provider.unlock_with_passphrase(PASSPHRASE)
            provider.unlock_with_passphrase(NEW_PASSPHRASE)
            self.assertTrue(provider.verify_integrity(reference, READ_WRITE))
            provider.lock()
            provider.unlock_with_recovery(recovery)
            self.assertEqual(b"immutable ciphertext object",
                             provider.read_object(reference.object_id, READ_WRITE))

    def test_router_only_routes_protected_when_unlocked(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = initialize_workspace(Path(temporary) / "workspace")
            router = protected_workspace_storage_router(context)
            policy = StoragePolicyResolver().resolve(item="protected")
            with self.assertRaises(StorageUnavailableError):
                router.provider_for(policy)
            router.protected_provider.initialize(PASSPHRASE, kdf_parameters=TEST_KDF)
            self.assertIs(router.protected_provider, router.provider_for(policy))
            router.protected_provider.lock()
            with self.assertRaises(StorageUnavailableError):
                router.provider_for(policy)

    def test_workspace_copy_recovers_but_different_workspace_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = initialize_workspace(base / "source-workspace")
            provider = EncryptedProtectedStorageProvider(source)
            recovery = provider.initialize(PASSPHRASE, kdf_parameters=TEST_KDF).recovery_key
            provider.write_original("portable/object", b"portable fictional secret", READ_WRITE)
            provider.lock()
            copied_root = base / "copied-workspace"
            shutil.copytree(source.root, copied_root)
            copied = EncryptedProtectedStorageProvider(load_workspace(copied_root))
            copied.unlock_with_recovery(recovery)
            self.assertEqual(b"portable fictional secret",
                             copied.read_object("portable/object", READ_WRITE))
            different = initialize_workspace(base / "different-workspace")
            shutil.copytree(source.protected_root, different.protected_root)
            rejected = EncryptedProtectedStorageProvider(different)
            self.assertEqual("error", rejected.status().lifecycle_state)
            with self.assertRaises(StorageUnavailableError):
                rejected.unlock_with_recovery(recovery)


class ProtectedStorageObjectTests(unittest.TestCase):
    def test_encrypted_round_trip_list_verify_locate_and_explicit_export(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            context = initialize_workspace(base / "workspace")
            provider = EncryptedProtectedStorageProvider(context)
            recovery = provider.initialize(PASSPHRASE, kdf_parameters=TEST_KDF).recovery_key
            in_memory_master_key = bytes(provider._data_key)
            object_id = "项目/绝密标题/原始路径.txt"
            plaintext = "THIS-FICTIONAL-PLAINTEXT-MUST-NOT-LEAK".encode()
            reference = provider.write_original(object_id, plaintext, READ_WRITE)
            duplicate = provider.write_original(object_id, plaintext, READ_WRITE)
            self.assertEqual(reference, duplicate)
            with self.assertRaises(FileExistsError):
                provider.write_original(object_id, plaintext + b"-different", READ_WRITE)
            self.assertEqual(plaintext, provider.read_object(object_id, READ_WRITE))
            self.assertEqual([reference], provider.list_objects("项目/", READ_WRITE))
            self.assertTrue(provider.verify_integrity(reference, READ_WRITE))
            location = provider.locate_object(object_id, READ_WRITE)
            self.assertTrue(location.startswith("protected:"))
            self.assertNotIn("绝密标题", location)
            destination = base / "owner-export" / "explicit.txt"
            with self.assertRaises(PermissionError):
                provider.export_object(object_id, destination,
                                       StorageAuthorization.allow(StorageOperation.EXPORT))
            exported = provider.export_object(object_id, destination, EXPORT)
            self.assertEqual(plaintext, destination.read_bytes())
            self.assertEqual(reference.sha256, exported.sha256)
            protected_bytes = b"\n".join(path.read_bytes() for path in context.protected_root.rglob("*")
                                           if path.is_file())
            for forbidden in (plaintext, object_id.encode("utf-8"), PASSPHRASE.encode("utf-8"),
                              recovery.encode("utf-8"), in_memory_master_key,
                              str(base).encode("utf-8")):
                self.assertNotIn(forbidden, protected_bytes)
            audit = provider.audit_path.read_text("utf-8")
            self.assertNotIn(str(destination), audit)
            self.assertNotIn(object_id, audit)
            self.assertNotIn(recovery, audit)

    def test_single_byte_tamper_and_truncation_never_return_plaintext(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = initialize_workspace(Path(temporary) / "workspace")
            provider = EncryptedProtectedStorageProvider(context)
            provider.initialize(PASSPHRASE, kdf_parameters=TEST_KDF)
            plaintext = b"fictional-tamper-target" * 10000
            reference = provider.write_original("tamper/object", plaintext, READ_WRITE)
            token, folder, _ = provider._object("tamper/object")
            chunk = sorted((folder / "chunks").glob("*.bin"))[0]
            original = chunk.read_bytes()
            changed = bytearray(original); changed[len(changed) // 2] ^= 1; chunk.write_bytes(changed)
            with self.assertRaises(ProtectedIntegrityError):
                provider.read_object(reference.object_id, READ_WRITE)
            self.assertFalse(provider.verify_integrity(reference, READ_WRITE))
            chunk.write_bytes(original[:-1])
            with self.assertRaises(ProtectedIntegrityError):
                provider.read_object(reference.object_id, READ_WRITE)
            output = io.BytesIO()
            with self.assertRaises(ProtectedIntegrityError):
                provider.read_to_stream(reference.object_id, output, READ_WRITE)
            self.assertEqual(b"", output.getvalue())
            self.assertEqual(token, folder.name)

    def test_object_and_chunk_swaps_are_detected(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = initialize_workspace(Path(temporary) / "workspace")
            provider = EncryptedProtectedStorageProvider(context)
            provider.initialize(PASSPHRASE, kdf_parameters=TEST_KDF)
            payload_a = b"A" * (80 * 1024) + b"a" * (80 * 1024)
            payload_b = b"B" * (80 * 1024) + b"b" * (80 * 1024)
            provider.write_stream("swap/a", io.BytesIO(payload_a), READ_WRITE,
                                  total_size=len(payload_a), chunk_size=64 * 1024)
            provider.write_stream("swap/b", io.BytesIO(payload_b), READ_WRITE,
                                  total_size=len(payload_b), chunk_size=64 * 1024)
            _, folder_a, _ = provider._object("swap/a")
            _, folder_b, _ = provider._object("swap/b")
            a0 = folder_a / "chunks" / "00000000.bin"
            a1 = folder_a / "chunks" / "00000001.bin"
            raw0, raw1 = a0.read_bytes(), a1.read_bytes()
            a0.write_bytes(raw1); a1.write_bytes(raw0)
            with self.assertRaises(ProtectedIntegrityError):
                provider.read_object("swap/a", READ_WRITE)
            a0.write_bytes(raw0); a1.write_bytes(raw1)
            b0 = folder_b / "chunks" / "00000000.bin"
            other = b0.read_bytes(); b0.write_bytes(raw0); a0.write_bytes(other)
            with self.assertRaises(ProtectedIntegrityError):
                provider.read_object("swap/a", READ_WRITE)
            with self.assertRaises(ProtectedIntegrityError):
                provider.read_object("swap/b", READ_WRITE)

    def test_cancelled_stream_leaves_no_plaintext_or_temp_objects(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = initialize_workspace(Path(temporary) / "workspace")
            provider = EncryptedProtectedStorageProvider(context)
            provider.initialize(PASSPHRASE, kdf_parameters=TEST_KDF)
            plaintext = b"CANCELLED-FICTIONAL-PLAINTEXT" * 10000
            cancel = {"value": False}
            def progress(_): cancel["value"] = True
            with self.assertRaises(StorageCancelledError):
                provider.write_stream("cancel/private-title", io.BytesIO(plaintext), READ_WRITE,
                                      total_size=len(plaintext), chunk_size=64 * 1024,
                                      progress=progress, cancel=lambda: cancel["value"])
            self.assertEqual([], list(provider.objects_root.glob("*/*/*/manifest.bin")))
            self.assertEqual([], list(provider.objects_root.rglob("*.tmp")))
            protected_bytes = b"".join(path.read_bytes() for path in provider.root.rglob("*") if path.is_file())
            self.assertNotIn(b"CANCELLED-FICTIONAL-PLAINTEXT", protected_bytes)
            self.assertNotIn(b"private-title", protected_bytes)

    def test_explicit_migration_uses_stream_contract_and_requires_owner(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = initialize_workspace(Path(temporary) / "workspace")
            provider = EncryptedProtectedStorageProvider(context)
            provider.initialize(PASSPHRASE, kdf_parameters=TEST_KDF)
            target = OpenFileStorageProvider(Path(temporary) / "open-target")
            payload = b"fictional migration payload" * 5000
            provider.write_original("migration/source", payload, READ_WRITE)
            incomplete = StorageAuthorization.allow(
                StorageOperation.MIGRATE, StorageOperation.READ, StorageOperation.WRITE_ORIGINAL,
                StorageOperation.VERIFY, StorageOperation.DELETE)
            with self.assertRaises(PermissionError):
                provider.migrate_object("migration/source", target, "migrated.bin", incomplete)
            allowed = StorageAuthorization.allow(
                StorageOperation.MIGRATE, StorageOperation.READ, StorageOperation.WRITE_ORIGINAL,
                StorageOperation.VERIFY, StorageOperation.DELETE,
                owner_confirmed=True, reason="fictional explicit migration")
            moved = provider.migrate_object("migration/source", target, "migrated.bin", allowed)
            self.assertEqual(payload, target.read_object(
                moved.object_id, StorageAuthorization.allow(StorageOperation.READ)))
            with self.assertRaises(FileNotFoundError):
                provider.read_object("migration/source", READ_WRITE)

    def test_lock_interrupts_an_inflight_chunked_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = initialize_workspace(Path(temporary) / "workspace")
            provider = EncryptedProtectedStorageProvider(context)
            provider.initialize(PASSPHRASE, kdf_parameters=TEST_KDF)
            first_chunk = threading.Event()
            continue_write = threading.Event()
            outcome = {}

            def progress(_):
                first_chunk.set()
                continue_write.wait(5)

            def worker():
                try:
                    provider.write_stream(
                        "concurrent/fictional", io.BytesIO(b"x" * (512 * 1024)), READ_WRITE,
                        total_size=512 * 1024, chunk_size=64 * 1024, progress=progress)
                except Exception as exc:
                    outcome["error"] = exc

            thread = threading.Thread(target=worker)
            thread.start()
            self.assertTrue(first_chunk.wait(5))
            provider.lock()
            continue_write.set()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertIsInstance(outcome.get("error"), StorageUnavailableError)
            self.assertEqual([], list(provider.objects_root.glob("*/*/*/manifest.bin")))

    def test_large_stream_is_chunk_bounded_and_performance_is_repeatable(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = initialize_workspace(Path(temporary) / "workspace")
            provider = EncryptedProtectedStorageProvider(context)
            provider.initialize(PASSPHRASE, kdf_parameters=TEST_KDF)
            size = 12 * 1024 * 1024
            source = tempfile.TemporaryFile(mode="w+b")
            block = b"LARGE-FICTIONAL-BLOCK" * 4096
            remaining = size
            while remaining:
                piece = block[:min(len(block), remaining)]
                source.write(piece); remaining -= len(piece)
            source.seek(0)
            chunks = []
            tracemalloc.start()
            reference = provider.write_stream(
                "large/video.bin", source, READ_WRITE, total_size=size, chunk_size=256 * 1024,
                progress=lambda event: chunks.append(event.chunk_bytes))
            _, peak = tracemalloc.get_traced_memory(); tracemalloc.stop(); source.close()
            self.assertEqual(size, reference.size_bytes)
            self.assertLessEqual(max(chunks), 256 * 1024)
            self.assertLess(peak, size // 2)
            _, folder, _ = provider._object("large/video.bin")
            nonces = [path.read_bytes()[:12] for path in sorted((folder / "chunks").glob("*.bin"))]
            self.assertEqual(len(nonces), len(set(nonces)))
            self.assertTrue(provider.verify_integrity(reference, READ_WRITE))
            metrics = temporary_performance_acceptance(
                context, PASSPHRASE, small_count=8, large_bytes=2 * 1024 * 1024,
                chunk_size=128 * 1024)
            self.assertTrue(metrics["large_verified"])
            self.assertEqual(128 * 1024, metrics["max_plaintext_chunk_bytes"])


if __name__ == "__main__":
    unittest.main()
