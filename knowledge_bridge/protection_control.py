"""Owner-only lifecycle control for the protected storage provider.

The manager owns the in-memory provider key, short-lived browser administration
sessions and the optional protected-storage idle deadline.  Those are separate
gates: an expired Owner session never locks storage or revokes an authenticated
client's already granted access.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .protected_storage import (
    EncryptedProtectedStorageProvider, InvalidUnlockMaterial, ProtectionInitialization,
)
from .storage import StorageAuthorization, StorageOperation, StorageUnavailableError
from .workspace import WorkspaceContext
from .protection_exit import exit_recovery_status


DEFAULT_AUTO_LOCK_SECONDS = 0
DEFAULT_OWNER_SESSION_SECONDS = 10 * 60
MIN_AUTO_LOCK_SECONDS = 60
MAX_AUTO_LOCK_SECONDS = 8 * 60 * 60
CHALLENGE_TTL_SECONDS = 5 * 60
MAX_CHALLENGES = 12
SESSION_TOKEN_BYTES = 32
CSRF_TOKEN_BYTES = 32


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class BrowserCredentials:
    session_token: str
    csrf_token: str


@dataclass
class _OwnerSession:
    token_hash: str
    csrf_token: str
    csrf_hash: str
    workspace_id: str
    expires_at: float
    device_authenticated: bool = False


@dataclass
class _Challenge:
    token_hash: str
    csrf_token: str
    csrf_hash: str
    workspace_id: str
    expires_at: float


class UnlockRateLimited(RuntimeError):
    def __init__(self, retry_after_seconds: float):
        super().__init__("解锁暂时受限，请稍后重试")
        self.retry_after_seconds = max(0.05, retry_after_seconds)


class OwnerSessionRequired(PermissionError):
    pass


class ProtectionLifecycleManager:
    """Thread-safe lifecycle owner for exactly one workspace at a time."""

    def __init__(self, context: WorkspaceContext, *, settings_path: Path,
                 clock: Callable[[], float] = time.monotonic,
                 default_auto_lock_seconds: int = DEFAULT_AUTO_LOCK_SECONDS,
                 owner_session_seconds: int = DEFAULT_OWNER_SESSION_SECONDS,
                 minimum_auto_lock_seconds: int = MIN_AUTO_LOCK_SECONDS,
                 failure_delay_base: float = 0.35,
                 initialization_kdf_parameters: dict | None = None,
                 device_mode: bool = False):
        if not context.initialized or not context.workspace_id:
            raise ValueError("保护层生命周期需要已加载的工作区")
        self._lock = threading.RLock()
        self._clock = clock
        self._settings_path = settings_path.expanduser().resolve()
        self._minimum_auto_lock_seconds = int(minimum_auto_lock_seconds)
        self._failure_delay_base = float(failure_delay_base)
        self._initialization_kdf_parameters = initialization_kdf_parameters
        self._default_auto_lock_seconds = int(default_auto_lock_seconds)
        self._owner_session_seconds = max(1, int(owner_session_seconds))
        self._context = context
        self._provider = EncryptedProtectedStorageProvider(context)
        self._owner_session: _OwnerSession | None = None
        self._device_sessions: dict[str, _OwnerSession] = {}
        self._default_device_mode = device_mode
        self._device_error: str | None = None
        self.protection_session_id: str | None = None
        self.on_session_end: Callable[[], None] | None = None
        self._challenges: dict[str, _Challenge] = {}
        self._unlock_failures = 0
        self._next_unlock_at = 0.0
        (self._auto_lock_seconds, self._recovery_acknowledged_at,
         migrated_settings) = self._load_workspace_settings()
        self._auto_lock_deadline: float | None = None
        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None
        if migrated_settings:
            self._save_workspace_settings()
        self._load_device_mode()

    def _load_device_mode(self):
        from .device_keys import DeviceKeyStore
        row = (self._read_settings().get('workspaces') or {}).get(self.context.workspace_id, {})
        configured = self.provider.status().lifecycle_state != 'not_configured'
        default_enabled = configured or not self._default_device_mode
        self.storage_enabled = row.get('protected_storage_enabled', default_enabled) is not False
        self.device_mode = row.get('access_mode', 'device' if self._default_device_mode else 'manual') == 'device'
        self.device_store = DeviceKeyStore(self._settings_path.parent / 'device-keys', self.context.workspace_id)
        self._device_error = None
        if self.device_mode:
            self._auto_lock_seconds = 0
            self._try_device_unlock()

    def _try_device_unlock(self):
        if not self.storage_enabled:
            return
        if self.provider.status().lifecycle_state == 'locked' and self.device_store.path.exists():
            try:
                self.provider.unlock_device(self.device_store)
            except Exception:
                self._device_error = '设备托管无法读取；原密文未改变，可用旧口令或恢复密钥重新托管'

    def enroll_device(self):
        """Called only after OS authentication or successful secret verification."""
        with self._lock:
            if not self.storage_enabled:
                raise StorageUnavailableError('加密保护当前已关闭')
            self.provider.enroll_device(self.device_store)
            self.device_mode = True
            self._device_error = None
            self._auto_lock_seconds = 0
            self._auto_lock_deadline = None
            self._save_workspace_settings()
            self._audit('enable_device_custody', 'success')

    def disable_empty_storage(self):
        """Disable unused encryption without destroying its recovery configuration."""
        with self._lock:
            if any(p.is_file() for p in self.provider.objects_root.rglob('*')):
                raise ValueError('已有受保护对象，不能按空保护层关闭；需先单独安排保留或迁移')
            self.provider.lock()
            self.storage_enabled = False
            self.device_mode = True
            self._auto_lock_seconds = 0
            self._auto_lock_deadline = None
            self._device_error = None
            self._save_workspace_settings()
            self._audit('disable_unused_encryption', 'success')

    def verify_disable_passphrase(self, passphrase: str) -> None:
        """Require the current password even when the browser is already unlocked."""
        with self._lock:
            self._rate_limit_before_unlock()
            try:
                if not self.storage_enabled or not self.provider.verify_passphrase(passphrase):
                    raise InvalidUnlockMaterial('验证失败')
            except (InvalidUnlockMaterial, StorageUnavailableError, ValueError):
                self._unlock_failed('disable_password')
                raise InvalidUnlockMaterial('当前密码不正确，请稍后重试') from None
            self._unlock_succeeded()

    def disable_after_export(self, *, exit_id: str | None = None) -> str:
        """Retain all old ciphertext in a versioned recovery folder after verified export."""
        with self._lock:
            if not self.storage_enabled or self.provider.status().lifecycle_state != 'unlocked':
                raise StorageUnavailableError('请先解锁保护层')
            stamp=uuid.UUID(hex=exit_id).hex if exit_id else datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            backup=self.context.versions/'retired-protection'/stamp
            backup.parent.mkdir(parents=True,exist_ok=True)
            original=self.provider.root
            device_backup=None
            self.provider.lock()
            os.replace(original,backup)
            old_enabled=self.storage_enabled
            old_device_mode=self.device_mode
            old_auto_lock=self._auto_lock_seconds
            try:
                if self.device_store.path.exists():
                    device_backup=backup/'device-custody.json'
                    shutil.move(str(self.device_store.path),str(device_backup))
                self.storage_enabled=False
                self.device_mode=False
                self._auto_lock_seconds=0
                self._auto_lock_deadline=None
                self._device_error=None
                self._save_workspace_settings()
            except Exception:
                self.storage_enabled=old_enabled
                self.device_mode=old_device_mode
                self._auto_lock_seconds=old_auto_lock
                if device_backup and device_backup.exists():
                    self.device_store.path.parent.mkdir(parents=True,exist_ok=True)
                    shutil.move(str(device_backup),str(self.device_store.path))
                os.replace(backup,original)
                # An I/O error may be reported after the settings replacement
                # became durable. Restore the old mode too; the exit journal
                # caller verifies persisted state if this write also fails.
                try:
                    self._save_workspace_settings()
                except Exception:
                    pass
                raise
            self._provider=EncryptedProtectedStorageProvider(self.context)
            self._invalidate_session()
            # A failed audit must not masquerade as a pre-commit failure. The
            # caller retains a durable exit journal for post-commit recovery.
            self._audit('disable_after_verified_export','success')
            return str(backup)

    def enable_existing_storage(self):
        """Make a preserved protection configuration available again without changing it."""
        with self._lock:
            if self.provider.status().lifecycle_state == 'not_configured':
                raise ValueError('当前没有可恢复的加密配置，请先设置解锁口令')
            if self.storage_enabled:
                return
            self.storage_enabled = True
            self._device_error = None
            self._try_device_unlock()
            self._save_workspace_settings()
            self._audit('enable_existing_encryption', 'success')

    def enable_new_storage(self, passphrase: str) -> tuple[ProtectionInitialization, BrowserCredentials]:
        """Initialize protection only after an explicit user action while ordinary use is enabled."""
        with self._lock:
            if exit_recovery_status(self.context):
                raise ValueError('退出保护的恢复记录尚待核对，可能存在明文；处理完成后才能重新设置保护')
            if self.storage_enabled:
                raise ValueError('加密保护已经开启')
            previous_device_mode = self.device_mode
            retired = None
            if self.provider.status().lifecycle_state != 'not_configured':
                retired = self._retire_empty_configuration()
            try:
                # A password explicitly chosen in Settings means manual password
                # protection. Device custody has its own migration/setup path and
                # must not silently turn this choice into automatic unlock.
                self.device_mode = False
                initialized = self._provider.initialize(
                    passphrase, kdf_parameters=self._initialization_kdf_parameters)
            except Exception:
                self.device_mode = previous_device_mode
                if retired:
                    self._restore_retired_configuration(retired)
                raise
            self.storage_enabled = True
            self._save_workspace_settings()
            credentials = self._new_owner_session()
            self._unlock_succeeded()
            self._audit('enable_new_encryption', 'success')
            return initialized, credentials

    def _retire_empty_configuration(self):
        """Retire an unused keyring so a future password starts cleanly."""
        if any(p.is_file() for p in self.provider.objects_root.rglob('*')):
            raise ValueError('检测到受保护对象，不能丢弃原密钥；请先解锁并迁出内容')
        stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        target=self.context.versions/'retired-protection'/stamp
        target.parent.mkdir(parents=True,exist_ok=True)
        device_backup=None
        self.provider.lock()
        os.replace(self.provider.root,target)
        if self.device_store.path.exists():
            device_backup=target/'device-custody.json'
            shutil.move(str(self.device_store.path),str(device_backup))
        self._provider=EncryptedProtectedStorageProvider(self.context)
        self._audit('retire_unused_encryption_configuration','success')
        return target,device_backup

    def _restore_retired_configuration(self, retired):
        target,device_backup=retired
        if self.provider.root.exists():shutil.rmtree(self.provider.root)
        os.replace(target,self.provider.root)
        restored_device=self.provider.root/'device-custody.json' if device_backup else None
        if restored_device and restored_device.exists():
            self.device_store.path.parent.mkdir(parents=True,exist_ok=True)
            shutil.move(str(restored_device),str(self.device_store.path))
        self._provider=EncryptedProtectedStorageProvider(self.context)

    def _enroll_after_secret(self):
        try:
            self.enroll_device()
        except (OSError, ValueError, StorageUnavailableError):
            # In particular, initialization must still deliver its recovery key.
            self._device_error = '设备托管未完成；本次解密仍可用，原密文与恢复材料不变'

    def initialize_device(self):
        # Recovery is returned once. The random password is never stored or shown.
        return self.initialize(secrets.token_urlsafe(48))

    def enable_manual_lock(self, passphrase):
        with self._lock:
            self.provider.change_passphrase(passphrase)
            self.device_mode = False
            self._save_workspace_settings()
            self.lock('manual')
            self._audit('enable_manual_lock', 'success')

    def local_browser(self, existing_token=None):
        """Caller must prove the TCP peer belongs to the current Windows account."""
        with self._lock:
            if not self.device_mode:
                raise OwnerSessionRequired('已启用额外手动锁，请使用口令或恢复密钥')
            self._try_device_unlock()
            if self.storage_enabled and self.provider.status().lifecycle_state == 'unlocked' and not self.device_store.path.exists():
                self.enroll_device()
            return self._persistent_browser_session(existing_token, 'windows_browser_authenticated')

    def open_browser(self, existing_token=None):
        """Create an ordinary local session while encryption is disabled."""
        with self._lock:
            if self.storage_enabled:
                raise OwnerSessionRequired('普通本机模式仅在加密关闭时可用')
            return self._persistent_browser_session(existing_token, 'open_browser_started')

    def _persistent_browser_session(self, existing_token, audit_action):
        row = self._device_sessions.get(_token_hash(existing_token or ''))
        if row:
            return BrowserCredentials(existing_token, row.csrf_token)
        if len(self._device_sessions) >= 32:
            self._device_sessions.pop(next(iter(self._device_sessions)))
        token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
        csrf = secrets.token_urlsafe(CSRF_TOKEN_BYTES)
        row = _OwnerSession(_token_hash(token), csrf, _token_hash(csrf), self.context.workspace_id, 0, True)
        self._device_sessions[row.token_hash] = row
        if self.protection_session_id is None:
            self.protection_session_id = secrets.token_urlsafe(32)
        self._audit(audit_action, 'success')
        return BrowserCredentials(token, csrf)

    @property
    def context(self) -> WorkspaceContext:
        return self._context

    @property
    def provider(self) -> EncryptedProtectedStorageProvider:
        """Integration hook for the future router; callers never receive key bytes."""
        return self._provider

    def _read_settings(self) -> dict:
        try:
            value = json.loads(self._settings_path.read_text("utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _load_workspace_settings(self) -> tuple[int, str | None, bool]:
        settings = self._read_settings()
        workspace = ((settings.get("workspaces") or {}).get(
            self._context.workspace_id, {}) or {})
        # v1 coupled the Owner session and storage lock, and allowed the old
        # 60-second value to lock a normally running workspace.  v2 migrates
        # that legacy setting to standard process-lifetime mode (0 = off).
        legacy = int(settings.get("format_version") or 0) < 2
        value = 0 if legacy else workspace.get("auto_lock_seconds",
                                               self._default_auto_lock_seconds)
        try:
            value = int(value)
        except (TypeError, ValueError):
            value = self._default_auto_lock_seconds
        if value != 0:
            value = min(MAX_AUTO_LOCK_SECONDS,
                        max(self._minimum_auto_lock_seconds, value))
        acknowledged_at = workspace.get("recovery_acknowledged_at")
        if not isinstance(acknowledged_at, str) or not acknowledged_at.strip():
            acknowledged_at = None
        return value, acknowledged_at, legacy

    def _save_workspace_settings(self) -> None:
        value = self._read_settings()
        value["format_version"] = 2
        workspaces = value.setdefault("workspaces", {})
        previous = workspaces.get(self._context.workspace_id) or {}
        previous.update({
            "auto_lock_seconds": self._auto_lock_seconds,
            "lock_mode": "process" if self._auto_lock_seconds == 0 else "idle",
            "recovery_acknowledged_at": self._recovery_acknowledged_at,
        })
        if hasattr(self, 'device_mode'):
            previous['access_mode'] = 'device' if self.device_mode else 'manual'
        if hasattr(self, 'storage_enabled'):
            previous['protected_storage_enabled'] = self.storage_enabled
        workspaces[self._context.workspace_id] = previous
        self._settings_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._settings_path.with_name(
            f".{self._settings_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._settings_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _audit(self, action: str, result: str, failure_category: str | None = None) -> None:
        row = {"at": _utc_now(), "action": action, "result": result,
               "workspace_id": self._context.workspace_id}
        if failure_category:
            row["failure_category"] = failure_category
        path = self._context.audit / "protection-control.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _clean_challenges(self) -> None:
        now = self._clock()
        self._challenges = {key: value for key, value in self._challenges.items()
                            if value.expires_at > now}
        if len(self._challenges) > MAX_CHALLENGES:
            ordered = sorted(self._challenges.items(), key=lambda item: item[1].expires_at)
            self._challenges = dict(ordered[-MAX_CHALLENGES:])

    def issue_challenge(self, existing_token: str | None = None) -> BrowserCredentials:
        with self._lock:
            self._clean_challenges()
            existing = self._challenges.get(_token_hash(existing_token or ""))
            if existing and existing.workspace_id == self._context.workspace_id:
                return BrowserCredentials(existing_token or "", existing.csrf_token)
            token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
            csrf = secrets.token_urlsafe(CSRF_TOKEN_BYTES)
            hashed = _token_hash(token)
            self._challenges[hashed] = _Challenge(
                hashed, csrf, _token_hash(csrf), self._context.workspace_id,
                self._clock() + CHALLENGE_TTL_SECONDS)
            return BrowserCredentials(token, csrf)

    def require_challenge(self, token: str | None, csrf: str | None) -> None:
        with self._lock:
            self._clean_challenges()
            row = self._challenges.pop(_token_hash(token or ""), None)
            valid = bool(row and row.workspace_id == self._context.workspace_id and csrf and
                         secrets.compare_digest(row.csrf_hash, _token_hash(csrf)))
            if not valid:
                raise OwnerSessionRequired("操作确认已失效，请刷新页面后重试")

    def _new_owner_session(self) -> BrowserCredentials:
        if self.protection_session_id is None:
            self.protection_session_id = secrets.token_urlsafe(32)
        token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
        csrf = secrets.token_urlsafe(CSRF_TOKEN_BYTES)
        expires_at = self._clock() + self._owner_session_seconds
        self._owner_session = _OwnerSession(
            _token_hash(token), csrf, _token_hash(csrf), self._context.workspace_id, expires_at)
        self.note_protected_activity()
        self._challenges.clear()
        return BrowserCredentials(token, csrf)

    def _invalidate_session(self) -> None:
        self._owner_session = None
        self._device_sessions.clear()
        self.protection_session_id = None

    def _expire_owner_session_if_needed(self) -> bool:
        row = self._owner_session
        if row is None or self._clock() < row.expires_at:
            return False
        return True

    def browser_session(self, token: str | None, csrf: str | None = None, *,
                        require_csrf: bool = False) -> _OwnerSession:
        """Authenticated browsing lasts only for this unlocked service session."""
        with self._lock:
            self.expire_if_needed()
            row = self._device_sessions.get(_token_hash(token or '')) or self._owner_session
            valid = bool(row and token and row.workspace_id == self.context.workspace_id and
                         secrets.compare_digest(row.token_hash, _token_hash(token)))
            if require_csrf:
                valid = bool(valid and csrf and secrets.compare_digest(row.csrf_hash, _token_hash(csrf)))
            if not valid or (not row.device_authenticated and self.provider.status().lifecycle_state != 'unlocked'):
                raise OwnerSessionRequired('请在保护与隐私确认本机浏览身份')
            return row

    def owner_session(self, token: str | None, csrf: str | None = None, *,
                      require_csrf: bool = False, refresh: bool = False) -> _OwnerSession:
        with self._lock:
            self.expire_if_needed()
            self._expire_owner_session_if_needed()
            row = self._device_sessions.get(_token_hash(token or '')) or self._owner_session
            valid = bool(row and row.workspace_id == self._context.workspace_id and token and
                         secrets.compare_digest(row.token_hash, _token_hash(token)))
            if require_csrf:
                valid = bool(valid and csrf and secrets.compare_digest(
                    row.csrf_hash, _token_hash(csrf)))
            if not valid or (not row.device_authenticated and (row.expires_at <= self._clock() or self._provider.status().lifecycle_state != "unlocked")):
                raise OwnerSessionRequired("Owner 会话无效或已过期")
            if refresh:
                row.expires_at = self._clock() + self._owner_session_seconds
            return row

    def owner_csrf(self, token: str | None) -> str | None:
        """Return the in-memory CSRF capability for a valid same-workspace session."""
        with self._lock:
            try:
                row = self.owner_session(token)
            except OwnerSessionRequired:
                return None
            return row.csrf_token

    def _rate_limit_before_unlock(self) -> None:
        remaining = self._next_unlock_at - self._clock()
        if remaining > 0:
            raise UnlockRateLimited(remaining)

    def _unlock_failed(self, action: str) -> None:
        self._unlock_failures += 1
        exponent = min(5, max(0, self._unlock_failures - 1))
        delay = min(8.0, self._failure_delay_base * (2 ** exponent))
        self._next_unlock_at = self._clock() + delay
        self._audit(action, "failed", "unlock_rejected")

    def _unlock_succeeded(self) -> None:
        self._unlock_failures = 0
        self._next_unlock_at = 0.0

    def initialize(self, passphrase: str) -> tuple[ProtectionInitialization, BrowserCredentials]:
        with self._lock:
            if not self.storage_enabled:raise StorageUnavailableError('加密保护当前已关闭')
            if self._provider.status().lifecycle_state != "not_configured":
                raise FileExistsError("受保护存储已经配置，拒绝重复初始化")
            initialized = self._provider.initialize(
                passphrase,kdf_parameters=self._initialization_kdf_parameters)
            if self.device_mode:
                self._enroll_after_secret()
            credentials = self._new_owner_session()
            self._unlock_succeeded()
            self._audit("initialize", "success")
            return initialized, credentials

    def unlock_with_passphrase(self, passphrase: str) -> BrowserCredentials:
        with self._lock:
            if not self.storage_enabled:raise StorageUnavailableError('加密保护当前已关闭，无需输入口令')
            self._rate_limit_before_unlock()
            try:
                if self._provider.status().lifecycle_state == "unlocked":
                    if not self._provider.verify_passphrase(passphrase):
                        raise InvalidUnlockMaterial("解锁失败")
                else:
                    self._provider.unlock_with_passphrase(passphrase)
            except (InvalidUnlockMaterial, StorageUnavailableError, ValueError):
                self._unlock_failed("unlock_password")
                raise InvalidUnlockMaterial("解锁失败，请稍后重试") from None
            self._unlock_succeeded()
            if self.device_mode:
                self._enroll_after_secret()
            credentials = self._new_owner_session()
            self._audit("unlock_password", "success")
            return credentials

    def unlock_with_recovery(self, recovery_key: str) -> BrowserCredentials:
        with self._lock:
            if not self.storage_enabled:raise StorageUnavailableError('加密保护当前已关闭，无需恢复密钥')
            self._rate_limit_before_unlock()
            try:
                if self._provider.status().lifecycle_state == "unlocked":
                    if not self._provider.verify_recovery_key(recovery_key):
                        raise InvalidUnlockMaterial("解锁失败")
                else:
                    self._provider.unlock_with_recovery(recovery_key)
            except (InvalidUnlockMaterial, StorageUnavailableError, ValueError):
                self._unlock_failed("unlock_recovery")
                raise InvalidUnlockMaterial("解锁失败，请稍后重试") from None
            self._unlock_succeeded()
            if self.device_mode:
                self._enroll_after_secret()
            credentials = self._new_owner_session()
            self._audit("unlock_recovery", "success")
            return credentials

    def lock(self, reason: str = "manual") -> None:
        with self._lock:
            if reason in {'manual', 'auto_lock'} and self.device_mode:
                # Explicit lock persists across refresh/restart; never auto-undone.
                self.device_mode = False
                self._save_workspace_settings()
            was_unlocked = self._provider.status().lifecycle_state == "unlocked"
            self._provider.lock()
            self._invalidate_session()
            if self.on_session_end:
                self.on_session_end()
            self._auto_lock_deadline = None
            self._challenges.clear()
            if was_unlocked:
                self._audit("lock", "success", reason)

    def change_passphrase(self, current_passphrase: str, new_passphrase: str,
                          session_token: str, csrf: str) -> None:
        with self._lock:
            self.owner_session(session_token, csrf, require_csrf=True)
            self._rate_limit_before_unlock()
            try:
                if not self._provider.verify_passphrase(current_passphrase):
                    raise InvalidUnlockMaterial("解锁失败")
            except (InvalidUnlockMaterial, StorageUnavailableError, ValueError):
                self._unlock_failed("change_passphrase")
                raise InvalidUnlockMaterial("验证失败，请稍后重试") from None
            self._provider.change_passphrase(new_passphrase)
            self._unlock_succeeded()
            self.owner_session(session_token, csrf, require_csrf=True, refresh=True)
            self._audit("change_passphrase", "success")

    def acknowledge_recovery(self, session_token: str, csrf: str) -> None:
        with self._lock:
            self.owner_session(session_token, csrf, require_csrf=True, refresh=True)
            if self._recovery_acknowledged_at is None:
                self._recovery_acknowledged_at = _utc_now()
                self._save_workspace_settings()
            self._audit("acknowledge_recovery", "success")

    def set_auto_lock(self, seconds: int, session_token: str, csrf: str) -> None:
        with self._lock:
            self.owner_session(session_token, csrf, require_csrf=True)
            seconds = int(seconds)
            if seconds != 0 and not self._minimum_auto_lock_seconds <= seconds <= MAX_AUTO_LOCK_SECONDS:
                raise ValueError("自动锁定时间超出允许范围")
            self._auto_lock_seconds = seconds
            if seconds:
                self.device_mode = False
            self._save_workspace_settings()
            self._auto_lock_deadline = (self._clock() + seconds) if seconds else None
            self._audit("set_auto_lock", "success")

    def note_protected_activity(self) -> None:
        """Refresh only the optional storage-idle deadline, never Owner auth."""
        with self._lock:
            if (self._auto_lock_seconds > 0 and
                    self._provider.status().lifecycle_state == "unlocked"):
                self._auto_lock_deadline = self._clock() + self._auto_lock_seconds

    def expire_if_needed(self) -> bool:
        with self._lock:
            self._expire_owner_session_if_needed()
            if self._auto_lock_deadline is None or self._clock() < self._auto_lock_deadline:
                return False
            self.lock("auto_lock")
            return True

    def status(self, session_token: str | None = None) -> dict:
        with self._lock:
            self.expire_if_needed()
            provider = self._provider.status()
            owner_active = False
            browsing_active = False
            if session_token:
                try:
                    self.browser_session(session_token)
                    browsing_active = True
                except OwnerSessionRequired:
                    pass
                try:
                    self.owner_session(session_token)
                    owner_active = True
                except OwnerSessionRequired:
                    pass
            remaining = None
            if provider.lifecycle_state == "unlocked" and self._auto_lock_deadline is not None:
                remaining = max(0, int(self._auto_lock_deadline - self._clock()))
            object_count = None
            if provider.lifecycle_state == "unlocked" and browsing_active:
                authorization = StorageAuthorization.allow(StorageOperation.LIST)
                object_count = len(self._provider.list_objects("", authorization))
            exit_recovery = exit_recovery_status(self._context)
            return {
                "state": provider.lifecycle_state if self.storage_enabled else 'disabled',
                "storage_enabled": self.storage_enabled,
                "configured": provider.lifecycle_state != "not_configured",
                "available": provider.availability.value,
                "protection_active": provider.protection_active and not exit_recovery,
                "exit_recovery": exit_recovery,
                "owner_session_active": owner_active,
                "browser_session_active": browsing_active,
                "owner_session_remaining_seconds": (
                    max(0, int(self._owner_session.expires_at - self._clock()))
                    if owner_active and self._owner_session and not self.device_mode else None
                ),
                "access_mode": ('open' if not self.storage_enabled else
                                'device' if self.device_mode else 'manual'),
                "device_enrolled": self.device_store.path.exists(),
                "device_error": self._device_error,
                "migration_required": self.storage_enabled and self.device_mode and provider.lifecycle_state == 'locked',
                "local_identity": ('本机普通模式' if browsing_active and not self.storage_enabled else
                                   'Windows 当前账户' if browsing_active and self.device_mode else None),
                "recovery_acknowledged": self._recovery_acknowledged_at is not None,
                "recovery_acknowledged_at": self._recovery_acknowledged_at,
                "recovery_acknowledgement_status": (
                    "confirmed" if self._recovery_acknowledged_at else
                    "not_recorded" if provider.lifecycle_state != "not_configured" else
                    "not_applicable"
                ),
                "auto_lock_enabled": self._auto_lock_seconds > 0,
                "auto_lock_seconds": self._auto_lock_seconds,
                "auto_lock_remaining_seconds": remaining,
                "protected_object_count": object_count,
                "has_protected_objects": any(p.is_file() for p in self.provider.objects_root.rglob('*')),
                "workspace_id": self._context.workspace_id,
                "real_data_migrated": False,
                "detail": (exit_recovery["message"] if exit_recovery else
                           "加密保护已关闭；本机普通模式无需主人确认"
                           if not self.storage_enabled else provider.detail),
                "security_boundary": (
                    "普通文件按本机文件权限访问；本机管理操作仍执行同源与防伪请求校验。"
                    if not self.storage_enabled else
                    "保护静态密文；不防御掌握解锁材料、控制已解锁进程、读取进程内存或拥有管理员权限的程序。"
                ),
            }

    def switch_workspace(self, context: WorkspaceContext) -> None:
        with self._lock:
            self.lock("workspace_switch")
            self._context = context
            self._provider = EncryptedProtectedStorageProvider(context)
            (self._auto_lock_seconds, self._recovery_acknowledged_at,
             migrated_settings) = self._load_workspace_settings()
            if migrated_settings:
                self._save_workspace_settings()
            self._load_device_mode()
            self._unlock_failures = 0
            self._next_unlock_at = 0.0

    def start_auto_lock_worker(self, interval_seconds: float = 0.5) -> None:
        with self._lock:
            if self._worker and self._worker.is_alive():
                return
            self._stop_event.clear()
            self._worker = threading.Thread(target=self._worker_loop,
                                            args=(interval_seconds,), daemon=True,
                                            name="protected-storage-auto-lock")
            self._worker.start()

    def _worker_loop(self, interval_seconds: float) -> None:
        while not self._stop_event.wait(interval_seconds):
            try:
                self.expire_if_needed()
            except Exception:
                # Status exposes provider errors; the worker never keeps the Web service down.
                pass

    def close(self) -> None:
        self._stop_event.set()
        worker = self._worker
        if worker and worker is not threading.current_thread():
            worker.join(timeout=2)
        self.lock("service_stop")
