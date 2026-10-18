"""Versioned authenticated protected storage for explicitly initialized workspaces.

Security boundary:
- AES-256-GCM provides confidentiality and integrity for key slots, manifests,
  and independently authenticated chunks.
- Argon2id derives the password wrapping key.  A separate random recovery key
  wraps the same workspace data key.
- No password, recovery key, or plaintext data key is persisted.
- This protects data at rest.  It does not defend an unlocked process from an
  administrator, SYSTEM, process-memory access, a stolen unlock secret, or the
  not-yet-built client/UI identity gates.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import io
import json
import os
import shutil
import tempfile
import time
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Callable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .storage import (
    OpenFileStorageProvider, ProviderAvailability, StorageAuthorization,
    StorageCapabilities, StorageOperation, StorageProvider, StorageProviderStatus,
    StorageCancelledError, StorageProgress, StorageRouter, StorageUnavailableError, StoredObject,
)
from .workspace import WorkspaceContext


PROTECTED_FORMAT_VERSION = 1
OBJECT_FORMAT_VERSION = 1
KEYRING_FORMAT_VERSION = 1
DEFAULT_CHUNK_SIZE = 1024 * 1024
MIN_CHUNK_SIZE = 64 * 1024
MAX_CHUNK_SIZE = 16 * 1024 * 1024
DATA_KEY_BYTES = 32
NONCE_BYTES = 12
RECOVERY_PREFIX = "jiedu-recovery-v1:"
KDF_DEFAULTS = {"name": "argon2id", "iterations": 3, "lanes": 4,
                "memory_cost_kib": 64 * 1024, "length": 32}
KDF_MINIMUMS = {"iterations": 2, "lanes": 1, "memory_cost_kib": 19 * 1024, "length": 32}
KDF_MAXIMUMS = {"iterations": 10, "lanes": 16, "memory_cost_kib": 512 * 1024, "length": 32}


ProgressCallback = Callable[["StorageProgress"], None]
CancelCallback = Callable[[], bool]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    try:
        return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError, TypeError) as exc:
        raise ValueError("无效的 Base64 编码") from exc


def _canonical(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _wipe(value: bytearray | None) -> None:
    if value is not None:
        for index in range(len(value)):
            value[index] = 0


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8") + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_passphrase(passphrase: str) -> None:
    if not isinstance(passphrase, str) or len(passphrase) < 12:
        raise ValueError("解锁口令至少需要 12 个字符")


def _validate_kdf(parameters: dict) -> dict:
    if parameters.get("name") != "argon2id":
        raise ValueError("保护层只接受 Argon2id 口令派生")
    result = {name: int(parameters[name]) for name in ("iterations", "lanes", "memory_cost_kib", "length")}
    for name, minimum in KDF_MINIMUMS.items():
        if result[name] < minimum:
            raise ValueError(f"Argon2id 参数低于安全下限：{name}")
    for name, maximum in KDF_MAXIMUMS.items():
        if result[name] > maximum:
            raise ValueError(f"Argon2id 参数高于安全上限：{name}")
    if result["length"] != DATA_KEY_BYTES:
        raise ValueError("Argon2id 输出长度必须为 32 字节")
    return {"name": "argon2id", **result}


def _derive_password_key(passphrase: str, salt: bytes, parameters: dict,
                         workspace_id: str) -> bytearray:
    checked = _validate_kdf(parameters)
    encoded = bytearray(passphrase.encode("utf-8"))
    try:
        derived = Argon2id(
            salt=salt, length=checked["length"], iterations=checked["iterations"],
            lanes=checked["lanes"], memory_cost=checked["memory_cost_kib"],
            ad=_canonical({"purpose": "jiedu-password-kek", "workspace_id": workspace_id,
                           "format_version": PROTECTED_FORMAT_VERSION}),
        ).derive(bytes(encoded))
        return bytearray(derived)
    finally:
        _wipe(encoded)


def _slot_aad(workspace_id: str, slot: str) -> bytes:
    return _canonical({"purpose": "jiedu-data-key-wrap", "workspace_id": workspace_id,
                       "protected_format_version": PROTECTED_FORMAT_VERSION,
                       "keyring_format_version": KEYRING_FORMAT_VERSION, "slot": slot})


def _subkey(master_key: bytes, workspace_id: str, purpose: str) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(), length=DATA_KEY_BYTES, salt=None,
        info=_canonical({"purpose": purpose, "workspace_id": workspace_id,
                         "protected_format_version": PROTECTED_FORMAT_VERSION}),
    ).derive(master_key)


def _seal_slot(data_key: bytes, wrapping_key: bytes, workspace_id: str, slot: str) -> dict:
    nonce = os.urandom(NONCE_BYTES)
    ciphertext = AESGCM(wrapping_key).encrypt(nonce, data_key, _slot_aad(workspace_id, slot))
    return {"algorithm": "AES-256-GCM", "nonce": _b64(nonce), "wrapped_data_key": _b64(ciphertext)}


def _open_slot(slot_value: dict, wrapping_key: bytes, workspace_id: str, slot: str) -> bytearray:
    try:
        if slot_value.get("algorithm") != "AES-256-GCM":
            raise ValueError("unsupported algorithm")
        raw = AESGCM(wrapping_key).decrypt(
            _unb64(slot_value["nonce"]), _unb64(slot_value["wrapped_data_key"]),
            _slot_aad(workspace_id, slot),
        )
    except (InvalidTag, KeyError, TypeError, ValueError) as exc:
        raise InvalidUnlockMaterial("解锁材料不正确或密钥封装已损坏") from exc
    if len(raw) != DATA_KEY_BYTES:
        raise InvalidUnlockMaterial("解锁材料不正确或密钥封装已损坏")
    return bytearray(raw)


@dataclass(frozen=True)
class ProtectionInitialization:
    recovery_key: str
    status: StorageProviderStatus
    format_version: int
    audit_recorded: bool


class InvalidUnlockMaterial(StorageUnavailableError):
    pass


class ProtectedIntegrityError(IOError):
    pass


class EncryptedProtectedStorageProvider(StorageProvider):
    provider_id = "jiedu-protected-aesgcm-v1"

    def __init__(self, context: WorkspaceContext):
        if not context.initialized or not context.workspace_id:
            raise ValueError("受保护存储需要已初始化的工作区")
        self.context = context
        self.root = context.protected_root
        self.format_path = self.root / "format.json"
        self.keyring_path = self.root / "keyring.json"
        self.objects_root = self.root / "objects"
        self.audit_path = context.audit / "protected-storage.jsonl"
        self._data_key: bytearray | None = None
        self._key_generation = 0
        self._lifecycle_lock = threading.RLock()
        self._error: str | None = None
        self._inspect_configuration()

    def _inspect_configuration(self) -> None:
        if not self.root.exists():
            return
        try:
            format_value = self._read_json(self.format_path)
            keyring = self._read_json(self.keyring_path)
            if int(format_value.get("format_version")) != PROTECTED_FORMAT_VERSION:
                raise ValueError("不支持的保护层格式版本")
            if int(keyring.get("format_version")) != KEYRING_FORMAT_VERSION:
                raise ValueError("不支持的密钥封装格式版本")
            if format_value.get("workspace_id") != self.context.workspace_id:
                raise ValueError("保护层属于另一个 Workspace ID")
            if keyring.get("workspace_id") != self.context.workspace_id:
                raise ValueError("密钥封装属于另一个 Workspace ID")
            _validate_kdf(keyring.get("password_kdf") or {})
            if not isinstance(keyring.get("slots"), dict):
                raise ValueError("密钥封装缺少槽位")
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self._error = str(exc) or "保护层配置损坏"

    @staticmethod
    def _read_json(path: Path) -> dict:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("保护层配置必须是 JSON 对象")
        return value

    def status(self) -> StorageProviderStatus:
        capabilities = StorageCapabilities(
            read=self._data_key is not None, write_original=self._data_key is not None,
            list_objects=self._data_key is not None, integrity_check=self._data_key is not None,
            export=self._data_key is not None, delete=self._data_key is not None,
            migrate=self._data_key is not None, confidentiality_enforced=self.root.exists() and not self._error,
            ordinary_filesystem_visible=False,
        )
        if self._error:
            return StorageProviderStatus(self.provider_id, ProviderAvailability.ERROR, False,
                                         "保护层配置损坏或与工作区身份不一致", capabilities, "error")
        if not self.root.exists():
            return StorageProviderStatus(self.provider_id, ProviderAvailability.NOT_CONFIGURED, False,
                                         "受保护存储尚未初始化", capabilities, "not_configured")
        if self._data_key is None:
            return StorageProviderStatus(self.provider_id, ProviderAvailability.UNAVAILABLE, False,
                                         "受保护存储已配置但当前锁定", capabilities, "locked")
        return StorageProviderStatus(self.provider_id, ProviderAvailability.AVAILABLE, True,
                                     "受保护存储已解锁并通过密钥封装校验", capabilities, "unlocked")

    def initialize(self, passphrase: str, *, kdf_parameters: dict | None = None) -> ProtectionInitialization:
        if self.root.exists():
            raise FileExistsError("保护层已经存在，拒绝重新初始化")
        _validate_passphrase(passphrase)
        parameters = _validate_kdf(kdf_parameters or KDF_DEFAULTS)
        data_key = bytearray(os.urandom(DATA_KEY_BYTES))
        recovery_key = bytearray(os.urandom(DATA_KEY_BYTES))
        salt = os.urandom(16)
        password_key = _derive_password_key(passphrase, salt, parameters, self.context.workspace_id)
        temporary = self.root.with_name(f".{self.root.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.mkdir(parents=False)
            (temporary / "objects").mkdir()
            format_value = {
                "format": "JIEDU_PROTECTED_STORE", "format_version": PROTECTED_FORMAT_VERSION,
                "object_format_version": OBJECT_FORMAT_VERSION,
                "workspace_id": self.context.workspace_id, "created_at": _now(),
                "cipher": "AES-256-GCM", "chunk_size": DEFAULT_CHUNK_SIZE,
                "subkey_derivation": "HKDF-SHA256",
            }
            keyring = {
                "format": "JIEDU_PROTECTED_KEYRING", "format_version": KEYRING_FORMAT_VERSION,
                "workspace_id": self.context.workspace_id, "created_at": _now(), "updated_at": _now(),
                "password_kdf": {**parameters, "salt": _b64(salt)},
                "slots": {
                    "password": _seal_slot(bytes(data_key), bytes(password_key),
                                           self.context.workspace_id, "password"),
                    "recovery": _seal_slot(bytes(data_key), bytes(recovery_key),
                                           self.context.workspace_id, "recovery"),
                },
            }
            _atomic_json(temporary / "format.json", format_value)
            _atomic_json(temporary / "keyring.json", keyring)
            os.replace(temporary, self.root)
            self._data_key = bytearray(data_key)
            self._key_generation += 1
            self._error = None
            try:
                self._audit("initialize")
                audit_recorded = True
            except OSError:
                # The store is already committed.  Returning the one-time recovery
                # material is safer than turning an audit failure into permanent loss.
                audit_recorded = False
            return ProtectionInitialization(
                RECOVERY_PREFIX + _b64(bytes(recovery_key)), self.status(),
                PROTECTED_FORMAT_VERSION, audit_recorded,
            )
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        finally:
            _wipe(data_key)
            _wipe(recovery_key)
            _wipe(password_key)

    def _configured_keyring(self) -> dict:
        status = self.status()
        if status.lifecycle_state == "error":
            raise StorageUnavailableError(status.detail)
        if status.lifecycle_state == "not_configured":
            raise StorageUnavailableError("受保护存储尚未初始化")
        return self._read_json(self.keyring_path)

    def unlock_with_passphrase(self, passphrase: str) -> StorageProviderStatus:
        self.lock()
        keyring = self._configured_keyring()
        kdf = keyring["password_kdf"]
        password_key = _derive_password_key(passphrase, _unb64(kdf["salt"]), kdf,
                                            self.context.workspace_id)
        try:
            self._data_key = _open_slot(keyring["slots"]["password"], bytes(password_key),
                                        self.context.workspace_id, "password")
            self._key_generation += 1
            self._self_check()
            self._audit("unlock_password")
            return self.status()
        except Exception:
            self.lock()
            raise
        finally:
            _wipe(password_key)

    def unlock_with_recovery(self, recovery_material: str) -> StorageProviderStatus:
        self.lock()
        keyring = self._configured_keyring()
        recovery_key: bytearray | None = None
        try:
            if not isinstance(recovery_material, str) or not recovery_material.startswith(RECOVERY_PREFIX):
                raise InvalidUnlockMaterial("恢复密钥格式不正确")
            try:
                recovery_key = bytearray(_unb64(recovery_material[len(RECOVERY_PREFIX):]))
            except ValueError as exc:
                raise InvalidUnlockMaterial("恢复密钥格式不正确") from exc
            if len(recovery_key) != DATA_KEY_BYTES:
                raise InvalidUnlockMaterial("恢复密钥格式不正确")
            self._data_key = _open_slot(keyring["slots"]["recovery"], bytes(recovery_key),
                                        self.context.workspace_id, "recovery")
            self._key_generation += 1
            self._self_check()
            self._audit("unlock_recovery")
            return self.status()
        except Exception:
            self.lock()
            raise
        finally:
            _wipe(recovery_key)

    def verify_passphrase(self, passphrase: str) -> bool:
        """Verify the current password slot without changing lifecycle state."""
        keyring = self._configured_keyring()
        kdf = keyring["password_kdf"]
        password_key = _derive_password_key(passphrase, _unb64(kdf["salt"]), kdf,
                                            self.context.workspace_id)
        recovered: bytearray | None = None
        try:
            recovered = _open_slot(keyring["slots"]["password"], bytes(password_key),
                                   self.context.workspace_id, "password")
            if self._data_key is None:
                return True
            return hmac.compare_digest(bytes(recovered), bytes(self._data_key))
        finally:
            _wipe(recovered)
            _wipe(password_key)

    def verify_recovery_key(self, recovery_material: str) -> bool:
        """Verify a recovery slot without locking or replacing the active key."""
        keyring = self._configured_keyring()
        recovery_key: bytearray | None = None
        recovered: bytearray | None = None
        try:
            if not isinstance(recovery_material, str) or not recovery_material.startswith(RECOVERY_PREFIX):
                raise InvalidUnlockMaterial("恢复密钥格式不正确")
            recovery_key = bytearray(_unb64(recovery_material[len(RECOVERY_PREFIX):]))
            if len(recovery_key) != DATA_KEY_BYTES:
                raise InvalidUnlockMaterial("恢复密钥格式不正确")
            recovered = _open_slot(keyring["slots"]["recovery"], bytes(recovery_key),
                                   self.context.workspace_id, "recovery")
            if self._data_key is None:
                return True
            return hmac.compare_digest(bytes(recovered), bytes(self._data_key))
        except ValueError as exc:
            raise InvalidUnlockMaterial("恢复密钥格式不正确") from exc
        finally:
            _wipe(recovered)
            _wipe(recovery_key)

    def change_passphrase(self, new_passphrase: str) -> None:
        self._require_unlocked()
        _validate_passphrase(new_passphrase)
        keyring = self._configured_keyring()
        parameters = _validate_kdf(keyring["password_kdf"])
        salt = os.urandom(16)
        password_key = _derive_password_key(new_passphrase, salt, parameters, self.context.workspace_id)
        try:
            keyring["password_kdf"] = {**parameters, "salt": _b64(salt)}
            keyring["slots"]["password"] = _seal_slot(
                bytes(self._data_key), bytes(password_key), self.context.workspace_id, "password")
            keyring["updated_at"] = _now()
            self._audit("change_passphrase_authorized")
            _atomic_json(self.keyring_path, keyring)
        finally:
            _wipe(password_key)

    def enroll_device(self, store) -> None:
        with self._lifecycle_lock:
            key = self._require_unlocked()
            keyring = self._configured_keyring()
            store.save(key, keyring)
            if not hmac.compare_digest(store.read(keyring), key):
                raise StorageUnavailableError('设备托管验证失败，原密钥仍保留')
            self._audit('device_custody_verified')

    def unlock_device(self, store) -> None:
        with self._lifecycle_lock:
            key = bytearray(store.read(self._configured_keyring()))
            try:
                self._data_key = bytearray(key)
                self._key_generation += 1
                self._self_check()
                self._audit('unlock_device')
            except Exception:
                self.lock()
                raise
            finally:
                _wipe(key)

    def lock(self) -> StorageProviderStatus:
        with self._lifecycle_lock:
            self._key_generation += 1
            _wipe(self._data_key)
            self._data_key = None
        return self.status()

    def _require_unlocked(self) -> bytes:
        status = self.status()
        if status.availability is not ProviderAvailability.AVAILABLE or not status.protection_active:
            raise StorageUnavailableError(status.detail)
        return bytes(self._data_key)

    def _key_snapshot(self) -> tuple[bytes, int]:
        with self._lifecycle_lock:
            return _subkey(self._require_unlocked(), self.context.workspace_id,
                           "object-encryption"), self._key_generation

    def _require_generation(self, generation: int) -> None:
        with self._lifecycle_lock:
            if self._data_key is None or self._key_generation != generation:
                raise StorageUnavailableError("受保护存储已锁定，当前操作已停止")

    def _self_check(self) -> None:
        key = _subkey(self._require_unlocked(), self.context.workspace_id, "object-encryption")
        marker = b"jiedu-protected-self-check"
        nonce = os.urandom(NONCE_BYTES)
        aad = _canonical({"purpose": "self-check", "workspace_id": self.context.workspace_id,
                          "format_version": PROTECTED_FORMAT_VERSION})
        if AESGCM(key).decrypt(nonce, AESGCM(key).encrypt(nonce, marker, aad), aad) != marker:
            raise StorageUnavailableError("受保护存储自检失败")

    def _object_token(self, object_id: str) -> str:
        if not isinstance(object_id, str) or not object_id.strip():
            raise ValueError("对象 ID 不能为空")
        token_key = _subkey(self._require_unlocked(), self.context.workspace_id, "object-token")
        return hmac.new(token_key,
                        b"jiedu-object-token\0" + object_id.encode("utf-8"), hashlib.sha256).hexdigest()

    def _object_dir_from_token(self, token: str) -> Path:
        return self.objects_root / token[:2] / token[2:4] / token

    def _manifest_aad(self, token: str) -> bytes:
        return _canonical({"purpose": "object-manifest", "workspace_id": self.context.workspace_id,
                           "protected_format_version": PROTECTED_FORMAT_VERSION,
                           "object_format_version": OBJECT_FORMAT_VERSION, "object_token": token})

    def _chunk_aad(self, token: str, object_version: str, index: int) -> bytes:
        return _canonical({"purpose": "object-chunk", "workspace_id": self.context.workspace_id,
                           "protected_format_version": PROTECTED_FORMAT_VERSION,
                           "object_format_version": OBJECT_FORMAT_VERSION, "object_token": token,
                           "object_version": object_version, "chunk_index": index})

    @staticmethod
    def _check_cancel(cancel: CancelCallback | None) -> None:
        if cancel and cancel():
            raise StorageCancelledError("受保护存储操作已取消")

    @staticmethod
    def _progress(callback: ProgressCallback | None, operation: str, processed: int,
                  total: int | None, index: int, chunk_bytes: int) -> None:
        if callback:
            callback(StorageProgress(operation, processed, total, index, chunk_bytes))

    def _seal_manifest(self, folder: Path, token: str, manifest: dict) -> None:
        nonce = os.urandom(NONCE_BYTES)
        key = _subkey(self._require_unlocked(), self.context.workspace_id, "object-encryption")
        ciphertext = AESGCM(key).encrypt(
            nonce, _canonical(manifest), self._manifest_aad(token))
        path = folder / "manifest.bin"
        with path.open("wb") as handle:
            handle.write(nonce + ciphertext)
            handle.flush()
            os.fsync(handle.fileno())

    def _open_manifest(self, folder: Path, token: str) -> dict:
        try:
            raw = (folder / "manifest.bin").read_bytes()
            if len(raw) <= NONCE_BYTES:
                raise ProtectedIntegrityError("受保护对象清单损坏")
            key = _subkey(self._require_unlocked(), self.context.workspace_id, "object-encryption")
            plaintext = AESGCM(key).decrypt(
                raw[:NONCE_BYTES], raw[NONCE_BYTES:], self._manifest_aad(token))
            value = json.loads(plaintext.decode("utf-8"))
            if not isinstance(value, dict) or value.get("format_version") != OBJECT_FORMAT_VERSION:
                raise ProtectedIntegrityError("受保护对象格式无效")
            if value.get("object_token") != token:
                raise ProtectedIntegrityError("受保护对象身份不一致")
            if not isinstance(value.get("object_id"), str) or not isinstance(value.get("object_version"), str):
                raise ProtectedIntegrityError("受保护对象身份字段无效")
            if self._object_token(value["object_id"]) != token:
                raise ProtectedIntegrityError("受保护对象身份校验失败")
            if not isinstance(value.get("chunks"), list):
                raise ProtectedIntegrityError("受保护对象分块清单无效")
            if not isinstance(value.get("plaintext_sha256"), str) or len(value["plaintext_sha256"]) != 64:
                raise ProtectedIntegrityError("受保护对象摘要无效")
            if int(value.get("plaintext_size", -1)) < 0:
                raise ProtectedIntegrityError("受保护对象长度无效")
            for index, row in enumerate(value["chunks"]):
                if not isinstance(row, dict) or row.get("name") != f"{index:08d}.bin":
                    raise ProtectedIntegrityError("受保护对象分块路径无效")
            return value
        except ProtectedIntegrityError:
            raise
        except (InvalidTag, OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
            raise ProtectedIntegrityError("受保护对象清单损坏或身份校验失败") from exc

    def write_original(self, object_id: str, data: bytes,
                       authorization: StorageAuthorization) -> StoredObject:
        return self.write_stream(object_id, io.BytesIO(data), authorization, total_size=len(data))

    def write_stream(self, object_id: str, source: BinaryIO, authorization: StorageAuthorization, *,
                     total_size: int | None = None, chunk_size: int = DEFAULT_CHUNK_SIZE,
                     progress: ProgressCallback | None = None,
        cancel: CancelCallback | None = None) -> StoredObject:
        authorization.require(StorageOperation.WRITE_ORIGINAL)
        key, generation = self._key_snapshot()
        if not MIN_CHUNK_SIZE <= chunk_size <= MAX_CHUNK_SIZE:
            raise ValueError("分块大小超出允许范围")
        token = self._object_token(object_id)
        final = self._object_dir_from_token(token)
        final.parent.mkdir(parents=True, exist_ok=True)
        temporary = final.parent / f".{uuid.uuid4().hex}.tmp"
        object_version = uuid.uuid4().hex
        digest = hashlib.sha256()
        size = 0
        chunks = []
        try:
            (temporary / "chunks").mkdir(parents=True)
            index = 0
            while True:
                self._check_cancel(cancel)
                self._require_generation(generation)
                plaintext = source.read(chunk_size)
                if plaintext is None:
                    plaintext = b""
                if not isinstance(plaintext, (bytes, bytearray)):
                    raise TypeError("源流必须返回 bytes")
                if not plaintext:
                    break
                nonce = os.urandom(NONCE_BYTES)
                ciphertext = AESGCM(key).encrypt(
                    nonce, bytes(plaintext), self._chunk_aad(token, object_version, index))
                chunk_name = f"{index:08d}.bin"
                chunk_path = temporary / "chunks" / chunk_name
                with chunk_path.open("wb") as handle:
                    handle.write(nonce + ciphertext)
                    handle.flush()
                    os.fsync(handle.fileno())
                digest.update(plaintext)
                size += len(plaintext)
                chunks.append({"index": index, "name": chunk_name,
                               "ciphertext_sha256": hashlib.sha256(nonce + ciphertext).hexdigest(),
                               "ciphertext_size": len(nonce) + len(ciphertext),
                               "plaintext_size": len(plaintext)})
                self._progress(progress, "encrypt", size, total_size, index, len(plaintext))
                index += 1
            self._require_generation(generation)
            manifest = {
                "format": "JIEDU_PROTECTED_OBJECT", "format_version": OBJECT_FORMAT_VERSION,
                "object_id": object_id, "object_token": token, "object_version": object_version,
                "plaintext_sha256": digest.hexdigest(), "plaintext_size": size,
                "chunk_size": chunk_size, "chunks": chunks, "created_at": _now(),
            }
            self._seal_manifest(temporary, token, manifest)
            reference = StoredObject(self.provider_id, object_id, digest.hexdigest(), size)
            if final.exists():
                current = self._verified_existing_reference(object_id)
                if current.sha256 != reference.sha256 or current.size_bytes != reference.size_bytes:
                    raise FileExistsError("受保护对象已存在且内容不同，拒绝覆盖")
                return current
            self._audit("write_authorized", token=token, size=size, sha256=digest.hexdigest())
            try:
                os.replace(temporary, final)
            except OSError:
                if not final.exists():
                    raise
                current = self._verified_existing_reference(object_id)
                if current.sha256 != reference.sha256 or current.size_bytes != reference.size_bytes:
                    raise FileExistsError("受保护对象并发写入了不同内容")
                return current
            return reference
        finally:
            shutil.rmtree(temporary, ignore_errors=True)

    def _reference_from_folder(self, folder: Path, token: str) -> StoredObject:
        manifest = self._open_manifest(folder, token)
        return StoredObject(self.provider_id, manifest["object_id"],
                            manifest["plaintext_sha256"], int(manifest["plaintext_size"]))

    def _verified_existing_reference(self, object_id: str) -> StoredObject:
        reference = self._reference_from_folder(self._object_dir_from_token(self._object_token(object_id)), self._object_token(object_id))
        if not self.verify_integrity(reference, StorageAuthorization.allow(StorageOperation.VERIFY)):
            raise ProtectedIntegrityError("已有受保护对象完整性校验失败")
        return reference

    def _object(self, object_id: str) -> tuple[str, Path, dict]:
        token = self._object_token(object_id)
        folder = self._object_dir_from_token(token)
        if not folder.is_dir():
            raise FileNotFoundError(object_id)
        return token, folder, self._open_manifest(folder, token)

    def _decrypt_to_verified_temp(self, object_id: str, authorization: StorageAuthorization, *,
                                  progress: ProgressCallback | None = None,
                                  cancel: CancelCallback | None = None) -> tuple[BinaryIO, StoredObject, int]:
        authorization.require(StorageOperation.READ)
        key, generation = self._key_snapshot()
        token, folder, manifest = self._object(object_id)
        expected_names = [row["name"] for row in manifest.get("chunks", [])]
        actual_names = sorted(path.name for path in (folder / "chunks").glob("*.bin"))
        if actual_names != expected_names:
            raise ProtectedIntegrityError("受保护对象分块缺失、增加或顺序异常")
        stage = tempfile.TemporaryFile(mode="w+b")
        digest = hashlib.sha256()
        size = 0
        try:
            for expected_index, row in enumerate(manifest.get("chunks", [])):
                self._check_cancel(cancel)
                self._require_generation(generation)
                if int(row.get("index", -1)) != expected_index:
                    raise ProtectedIntegrityError("受保护对象分块序号异常")
                raw = (folder / "chunks" / row["name"]).read_bytes()
                if hashlib.sha256(raw).hexdigest() != row.get("ciphertext_sha256"):
                    raise ProtectedIntegrityError("受保护对象密文摘要不一致")
                if len(raw) <= NONCE_BYTES or len(raw) != int(row.get("ciphertext_size", -1)):
                    raise ProtectedIntegrityError("受保护对象密文被截断")
                try:
                    plaintext = AESGCM(key).decrypt(
                        raw[:NONCE_BYTES], raw[NONCE_BYTES:],
                        self._chunk_aad(token, manifest["object_version"], expected_index),
                    )
                except InvalidTag as exc:
                    raise ProtectedIntegrityError("受保护对象认证失败") from exc
                if len(plaintext) != int(row.get("plaintext_size", -1)):
                    raise ProtectedIntegrityError("受保护对象明文长度校验失败")
                stage.write(plaintext)
                digest.update(plaintext)
                size += len(plaintext)
                self._progress(progress, "decrypt", size, int(manifest["plaintext_size"]),
                               expected_index, len(plaintext))
            if size != int(manifest["plaintext_size"]) or digest.hexdigest() != manifest["plaintext_sha256"]:
                raise ProtectedIntegrityError("受保护对象整体完整性校验失败")
            self._require_generation(generation)
            stage.seek(0)
            return stage, StoredObject(self.provider_id, object_id, digest.hexdigest(), size), generation
        except Exception:
            stage.close()
            raise

    def read_object(self, object_id: str, authorization: StorageAuthorization) -> bytes:
        # Catalogs and bounded conversions must never spool plaintext to disk.
        value = b"".join(self.iter_verified_chunks(object_id, authorization))
        return value

    def _range_context(self, object_id, authorization):
        authorization.require(StorageOperation.READ)
        with self._lifecycle_lock:
            key, generation = self._key_snapshot()
            token, folder, manifest = self._object(object_id)
        total = 0
        for index, row in enumerate(manifest["chunks"]):
            length = int(row.get("plaintext_size", -1))
            if row.get("index") != index or not 0 <= length <= MAX_CHUNK_SIZE:
                raise ProtectedIntegrityError("受保护对象分块长度或序号异常")
            total += length
        if total != manifest["plaintext_size"]:
            raise ProtectedIntegrityError("受保护对象分块总长度异常")
        return key, generation, token, folder, manifest

    def _verified_chunk(self, context, index):
        key, generation, token, folder, manifest = context
        self._require_generation(generation)
        row = manifest["chunks"][index]
        with (folder / "chunks" / row["name"]).open("rb") as handle:
            raw = handle.read(MAX_CHUNK_SIZE + NONCE_BYTES + 17)
        if (len(raw) != row.get("ciphertext_size") or len(raw) > MAX_CHUNK_SIZE + NONCE_BYTES + 16
                or hashlib.sha256(raw).hexdigest() != row.get("ciphertext_sha256")):
            raise ProtectedIntegrityError("受保护对象密文分块校验失败")
        try:
            plaintext = AESGCM(key).decrypt(raw[:NONCE_BYTES], raw[NONCE_BYTES:],
                self._chunk_aad(token, manifest["object_version"], index))
        except (InvalidTag, ValueError) as exc:
            raise ProtectedIntegrityError("受保护对象分块认证失败") from exc
        if len(plaintext) != row["plaintext_size"]:
            raise ProtectedIntegrityError("受保护对象分块明文长度异常")
        self._require_generation(generation)
        return plaintext

    def iter_verified_chunks(self, object_id, authorization):
        """Internal full verification; consumers must not publish before exhaustion."""
        context = self._range_context(object_id, authorization)
        manifest = context[-1]; folder = context[-2]
        if sorted(p.name for p in (folder / "chunks").glob("*.bin")) != [r["name"] for r in manifest["chunks"]]:
            raise ProtectedIntegrityError("受保护对象分块缺失或增加")
        digest = hashlib.sha256()
        for index in range(len(manifest["chunks"])):
            plaintext = self._verified_chunk(context, index)
            digest.update(plaintext)
            yield plaintext
        if digest.hexdigest() != manifest["plaintext_sha256"]:
            raise ProtectedIntegrityError("受保护对象整体摘要异常")
        self._require_generation(context[1])

    def read_range(self, object_id, authorization, *, offset=0, length=65536,
                   version=None, max_size=64*1024*1024):
        """Authenticate only intersecting existing AES-GCM chunks, never a plaintext cache.

        Range integrity covers the returned chunks and authenticated manifest. It
        is not a claim that unread chunks have been scrubbed for disk corruption.
        """
        if offset < 0 or not 0 <= length <= 65540: raise ValueError("范围超出读取预算")
        context = self._range_context(object_id, authorization)
        manifest = context[-1]; total = manifest["plaintext_size"]
        if total > max_size: raise ValueError("对象超过 64 MiB 有界读取上限")
        current_version = manifest["object_version"] + ":" + manifest["plaintext_sha256"]
        if version is not None and version != current_version: raise ValueError("对象版本已变化，请重新开始读取")
        if offset > total: raise ValueError("读取位置超出对象末尾")
        pieces=[]; position=0; stop=min(total, offset+length)
        for index,row in enumerate(manifest["chunks"]):
            end=position+row["plaintext_size"]
            if position < stop and end > offset:
                plaintext=self._verified_chunk(context,index)
                pieces.append(plaintext[max(0,offset-position):min(len(plaintext),stop-position)])
            position=end
            if position >= stop: break
        self._require_generation(context[1])
        return {"data":b"".join(pieces), "version":current_version, "sha256":manifest["plaintext_sha256"],
                "total_bytes":total, "offset":offset, "integrity":"authenticated_manifest_and_returned_chunks"}

    def read_to_stream(self, object_id: str, destination: BinaryIO,
                       authorization: StorageAuthorization, *,
                       progress: ProgressCallback | None = None,
                       cancel: CancelCallback | None = None) -> StoredObject:
        if not destination.seekable() or not destination.writable():
            raise ValueError("目标流必须可定位且可写，以便失败时回滚")
        start = destination.tell()
        destination.seek(0, os.SEEK_END)
        if destination.tell() != start:
            destination.seek(start)
            raise ValueError("目标流必须从当前末尾开始写入")
        destination.seek(start)
        stage, reference, generation = self._decrypt_to_verified_temp(
            object_id, authorization, progress=progress, cancel=cancel)
        try:
            while True:
                self._check_cancel(cancel)
                self._require_generation(generation)
                chunk = stage.read(DEFAULT_CHUNK_SIZE)
                if not chunk:
                    break
                written = destination.write(chunk)
                if written is not None and written != len(chunk):
                    raise IOError("目标流未完整写入受保护对象")
            self._require_generation(generation)
            return reference
        except Exception:
            destination.seek(start)
            destination.truncate()
            raise
        finally:
            stage.close()

    def list_objects(self, prefix: str, authorization: StorageAuthorization) -> list[StoredObject]:
        authorization.require(StorageOperation.LIST)
        self._require_unlocked()
        if not self.objects_root.exists():
            return []
        rows = []
        for manifest_path in sorted(self.objects_root.glob("*/*/*/manifest.bin")):
            token = manifest_path.parent.name
            reference = self._reference_from_folder(manifest_path.parent, token)
            if reference.object_id.startswith(prefix):
                rows.append(reference)
        return sorted(rows, key=lambda row: row.object_id)

    def locate_object(self, object_id: str, authorization: StorageAuthorization) -> str:
        authorization.require(StorageOperation.LOCATE)
        token, _, _ = self._object(object_id)
        return f"protected:{token}"

    def verify_integrity(self, reference: StoredObject, authorization: StorageAuthorization) -> bool:
        authorization.require(StorageOperation.VERIFY)
        self._require_unlocked()
        read_authorization = StorageAuthorization.allow(StorageOperation.READ)
        try:
            digest=hashlib.sha256(); size=0
            for chunk in self.iter_verified_chunks(reference.object_id,read_authorization):
                digest.update(chunk); size+=len(chunk)
            return digest.hexdigest() == reference.sha256 and size == reference.size_bytes
        except (OSError, ValueError, StorageUnavailableError):
            return False

    def export_object(self, object_id: str, destination: Path,
                      authorization: StorageAuthorization) -> StoredObject:
        authorization.require(StorageOperation.EXPORT)
        if not authorization.owner_confirmed:
            raise PermissionError("受保护对象导出需要 Owner 明确确认")
        destination = destination.expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("w+b") as handle:
                reference = self.read_to_stream(
                    object_id, handle, StorageAuthorization.allow(StorageOperation.READ))
                handle.flush()
                os.fsync(handle.fileno())
            self._audit("export_authorized", token=self._object_token(object_id),
                        size=reference.size_bytes, sha256=reference.sha256,
                        destination_sha256=hashlib.sha256(str(destination).encode("utf-8")).hexdigest())
            os.replace(temporary, destination)
            return StoredObject("export", str(destination), reference.sha256, reference.size_bytes)
        finally:
            temporary.unlink(missing_ok=True)

    def delete_object(self, object_id: str, authorization: StorageAuthorization) -> None:
        authorization.require(StorageOperation.DELETE, destructive=True)
        token, folder, _ = self._object(object_id)
        self._audit("delete_authorized", token=token)
        shutil.rmtree(folder)

    def prepare_import_rollback(self, object_id: str, authorization: StorageAuthorization):
        """Private transaction receipt for a NEW random object, usable after lock.

        No key is retained. Existing objects cannot receive this receipt. The
        caller discards it on commit and invokes it only on transaction failure.
        """
        authorization.require(StorageOperation.WRITE_ORIGINAL)
        authorization.require(StorageOperation.DELETE, destructive=True)
        folder=self._object_dir_from_token(self._object_token(object_id))
        if folder.exists():raise FileExistsError('回滚凭据只适用于本次新增对象')
        root=self.objects_root.resolve()
        def rollback():
            if folder.exists():
                folder.resolve().relative_to(root)
                if folder.is_symlink() or any(p.is_symlink() or (hasattr(p,'is_junction') and p.is_junction()) for p in [folder,*folder.rglob('*')]):
                    raise ProtectedIntegrityError('回滚对象存在链接，拒绝递归删除')
                shutil.rmtree(folder)
        return rollback

    def migrate_object(self, object_id: str, target: StorageProvider, target_id: str,
                       authorization: StorageAuthorization) -> StoredObject:
        authorization.require(StorageOperation.MIGRATE, destructive=True)
        authorization.require(StorageOperation.READ)
        authorization.require(StorageOperation.WRITE_ORIGINAL)
        authorization.require(StorageOperation.VERIFY)
        authorization.require(StorageOperation.DELETE, destructive=True)
        # This compatibility method preserves the existing interface.  Large-object
        # production migration must use the future transactional migration service.
        stage, _, _ = self._decrypt_to_verified_temp(object_id, authorization)
        try:
            created = target.write_stream(target_id, stage, authorization)
            if not target.verify_integrity(created, authorization):
                raise IOError("迁移目标完整性校验失败，受保护源对象保留")
            self.delete_object(object_id, authorization)
            return created
        finally:
            stage.close()

    def _audit(self, action: str, *, token: str | None = None, size: int | None = None,
               sha256: str | None = None, destination_sha256: str | None = None) -> None:
        row = {"at": _now(), "action": action, "provider": self.provider_id,
               "workspace_id": self.context.workspace_id}
        if token:
            row["object_token"] = token
        if size is not None:
            row["size_bytes"] = size
        if sha256:
            row["plaintext_sha256"] = sha256
        if destination_sha256:
            row["destination_sha256"] = destination_sha256
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def protected_workspace_storage_router(context: WorkspaceContext) -> StorageRouter:
    """Explicit opt-in constructor; the default router remains fail-closed/unconfigured."""
    return StorageRouter(OpenFileStorageProvider(context.root),
                         EncryptedProtectedStorageProvider(context))


def temporary_performance_acceptance(context: WorkspaceContext, passphrase: str, *,
                                     small_count: int = 32, large_bytes: int = 16 * 1024 * 1024,
                                     chunk_size: int = 256 * 1024) -> dict:
    """Repeatable fictional-data benchmark for temporary workspaces only."""
    if "temp" not in str(context.root).casefold():
        raise ValueError("性能验收只允许临时工作区")
    provider = EncryptedProtectedStorageProvider(context)
    if provider.status().lifecycle_state == "not_configured":
        provider.initialize(passphrase)
    elif provider.status().lifecycle_state != "unlocked":
        provider.unlock_with_passphrase(passphrase)
    authorization = StorageAuthorization.allow(
        StorageOperation.WRITE_ORIGINAL, StorageOperation.READ, StorageOperation.LIST,
        StorageOperation.VERIFY,
    )
    small_payload = b"fictional-small-object\0" * 64
    started = time.perf_counter()
    references = [provider.write_original(f"benchmark/small/{index}", small_payload, authorization)
                  for index in range(small_count)]
    small_write_seconds = time.perf_counter() - started
    started = time.perf_counter()
    for reference in references:
        if provider.read_object(reference.object_id, authorization) != small_payload:
            raise ProtectedIntegrityError("小对象性能验收读取不一致")
    small_read_seconds = time.perf_counter() - started
    large_source = tempfile.TemporaryFile(mode="w+b")
    block = b"JIEDU-FICTIONAL-LARGE-BLOCK\0" * 1024
    remaining = large_bytes
    while remaining:
        piece = block[:min(len(block), remaining)]
        large_source.write(piece)
        remaining -= len(piece)
    large_source.seek(0)
    observed_chunks = []
    started = time.perf_counter()
    large_reference = provider.write_stream(
        "benchmark/large/attachment", large_source, authorization,
        total_size=large_bytes, chunk_size=chunk_size,
        progress=lambda event: observed_chunks.append(event.chunk_bytes),
    )
    encrypt_seconds = time.perf_counter() - started
    large_source.close()
    destination = tempfile.TemporaryFile(mode="w+b")
    started = time.perf_counter()
    provider.read_to_stream("benchmark/large/attachment", destination, authorization)
    decrypt_seconds = time.perf_counter() - started
    destination.close()
    started = time.perf_counter()
    verified = provider.verify_integrity(large_reference, authorization)
    verify_seconds = time.perf_counter() - started
    return {
        "small_count": small_count, "small_write_seconds": round(small_write_seconds, 6),
        "small_read_seconds": round(small_read_seconds, 6), "large_bytes": large_bytes,
        "chunk_size": chunk_size, "max_plaintext_chunk_bytes": max(observed_chunks, default=0),
        "large_encrypt_seconds": round(encrypt_seconds, 6),
        "large_decrypt_seconds": round(decrypt_seconds, 6),
        "large_verify_seconds": round(verify_seconds, 6), "large_verified": verified,
    }
