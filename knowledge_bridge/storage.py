"""Storage-policy resolution and provider boundaries for Jiedu.

This module deliberately does not implement encryption.  A protected request must
fail closed until a reviewed protected provider is configured; it must never fall
back to the ordinary filesystem provider.
"""
from __future__ import annotations

import hashlib
import io
import os
import shutil
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import BinaryIO, Callable, Mapping

from .workspace import WorkspaceContext


class StoragePolicy(str, Enum):
    OPEN = "open"
    PROTECTED = "protected"
    PENDING = "pending"
    INHERIT = "inherit"

    @classmethod
    def parse(cls, value: object, *, missing: "StoragePolicy" | None = None) -> "StoragePolicy":
        if value is None or value == "":
            return missing or cls.INHERIT
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().casefold())
        except ValueError as exc:
            raise ValueError(f"未知存储策略：{value}") from exc


POLICY_PRECEDENCE = ("item", "session", "project", "import_task", "source", "workspace")


@dataclass(frozen=True)
class ResolvedStoragePolicy:
    policy: StoragePolicy
    decided_by: str
    examined: tuple[tuple[str, StoragePolicy], ...]
    defaulted: bool = False

    def audit_record(self) -> dict:
        return {
            "policy": self.policy.value,
            "decided_by": self.decided_by,
            "defaulted": self.defaulted,
            "precedence": list(POLICY_PRECEDENCE),
            "examined": [{"scope": scope, "policy": policy.value} for scope, policy in self.examined],
        }


class StoragePolicyResolver:
    """Resolve storage independently from content type or source semantics.

    Callers pass only explicit policy assignments.  Content classification is not
    an input, so a project, conversation, attachment, or raw archive cannot become
    protected merely because of what it is.
    """

    @staticmethod
    def _assignment(value: object) -> StoragePolicy:
        if isinstance(value, Mapping):
            value = value.get("storage_policy")
        return StoragePolicy.parse(value)

    def resolve(self, *, workspace: object = None, source: object = None,
                import_task: object = None, project: object = None,
                session: object = None, item: object = None) -> ResolvedStoragePolicy:
        values = {
            "workspace": workspace, "source": source, "import_task": import_task,
            "project": project, "session": session, "item": item,
        }
        examined = tuple((scope, self._assignment(values[scope])) for scope in POLICY_PRECEDENCE)
        for scope, policy in examined:
            if policy is not StoragePolicy.INHERIT:
                return ResolvedStoragePolicy(policy, scope, examined)
        # Compatibility contract: every existing object without a policy remains open.
        return ResolvedStoragePolicy(StoragePolicy.OPEN, "compatibility_default", examined, True)


class ProviderAvailability(str, Enum):
    AVAILABLE = "available"
    NOT_CONFIGURED = "not_configured"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


class StorageOperation(str, Enum):
    WRITE_ORIGINAL = "write_original"
    READ = "read"
    LIST = "list"
    LOCATE = "locate"
    VERIFY = "verify"
    EXPORT = "export"
    DELETE = "delete"
    MIGRATE = "migrate"


@dataclass(frozen=True)
class StorageAuthorization:
    permissions: frozenset[StorageOperation]
    owner_confirmed: bool = False
    reason: str = ""

    @classmethod
    def allow(cls, *operations: StorageOperation, owner_confirmed: bool = False,
              reason: str = "") -> "StorageAuthorization":
        return cls(frozenset(operations), owner_confirmed, reason)

    def require(self, operation: StorageOperation, *, destructive: bool = False) -> None:
        if operation not in self.permissions:
            raise PermissionError(f"存储操作未授权：{operation.value}")
        if destructive and not self.owner_confirmed:
            raise PermissionError(f"{operation.value} 需要 Owner 明确确认")


@dataclass(frozen=True)
class StorageCapabilities:
    read: bool
    write_original: bool
    list_objects: bool
    integrity_check: bool
    export: bool
    delete: bool
    migrate: bool
    confidentiality_enforced: bool
    ordinary_filesystem_visible: bool


@dataclass(frozen=True)
class StorageProviderStatus:
    provider_id: str
    availability: ProviderAvailability
    protection_active: bool
    detail: str
    capabilities: StorageCapabilities
    lifecycle_state: str | None = None


@dataclass(frozen=True)
class StoredObject:
    provider_id: str
    object_id: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class StorageProgress:
    operation: str
    processed_bytes: int
    total_bytes: int | None
    chunk_index: int
    chunk_bytes: int


class StorageUnavailableError(RuntimeError):
    pass


class StoragePolicyPendingError(RuntimeError):
    pass


class StorageCancelledError(RuntimeError):
    pass


class StorageProvider(ABC):
    provider_id: str

    @abstractmethod
    def status(self) -> StorageProviderStatus: ...

    @abstractmethod
    def write_original(self, object_id: str, data: bytes, authorization: StorageAuthorization) -> StoredObject: ...

    @abstractmethod
    def write_stream(self, object_id: str, source: BinaryIO, authorization: StorageAuthorization, *,
                     total_size: int | None = None, chunk_size: int = 1024 * 1024,
                     progress: Callable[[StorageProgress], None] | None = None,
                     cancel: Callable[[], bool] | None = None) -> StoredObject: ...

    @abstractmethod
    def read_object(self, object_id: str, authorization: StorageAuthorization) -> bytes: ...

    @abstractmethod
    def read_to_stream(self, object_id: str, destination: BinaryIO,
                       authorization: StorageAuthorization, *,
                       progress: Callable[[StorageProgress], None] | None = None,
                       cancel: Callable[[], bool] | None = None) -> StoredObject: ...

    @abstractmethod
    def list_objects(self, prefix: str, authorization: StorageAuthorization) -> list[StoredObject]: ...

    @abstractmethod
    def locate_object(self, object_id: str, authorization: StorageAuthorization) -> str: ...

    @abstractmethod
    def verify_integrity(self, reference: StoredObject, authorization: StorageAuthorization) -> bool: ...

    @abstractmethod
    def export_object(self, object_id: str, destination: Path,
                      authorization: StorageAuthorization) -> StoredObject: ...

    @abstractmethod
    def delete_object(self, object_id: str, authorization: StorageAuthorization) -> None: ...

    @abstractmethod
    def migrate_object(self, object_id: str, target: "StorageProvider", target_id: str,
                       authorization: StorageAuthorization) -> StoredObject: ...


class OpenFileStorageProvider(StorageProvider):
    """Ordinary visible files; interface permissions cannot prevent OS-level reads."""

    provider_id = "open-files"

    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()

    def status(self) -> StorageProviderStatus:
        return StorageProviderStatus(
            self.provider_id, ProviderAvailability.AVAILABLE, False,
            "普通文件存储可用；界渡权限只约束经界渡的操作，不能阻止其他程序直接读取。",
            StorageCapabilities(True, True, True, True, True, True, True, False, True), "available",
        )

    def _path(self, object_id: str) -> Path:
        if not object_id or Path(object_id).is_absolute():
            raise ValueError("对象 ID 必须是非空相对路径")
        path = (self.root / object_id).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("对象路径越出存储根目录") from exc
        return path

    @staticmethod
    def _reference(path: Path, root: Path) -> StoredObject:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk); size += len(chunk)
        return StoredObject(OpenFileStorageProvider.provider_id, path.relative_to(root).as_posix(),
                            digest.hexdigest(), size)

    def write_original(self, object_id: str, data: bytes, authorization: StorageAuthorization) -> StoredObject:
        return self.write_stream(object_id, io.BytesIO(data), authorization, total_size=len(data))

    def write_stream(self, object_id: str, source: BinaryIO, authorization: StorageAuthorization, *,
                     total_size: int | None = None, chunk_size: int = 1024 * 1024,
                     progress: Callable[[StorageProgress], None] | None = None,
                     cancel: Callable[[], bool] | None = None) -> StoredObject:
        authorization.require(StorageOperation.WRITE_ORIGINAL)
        if chunk_size <= 0:
            raise ValueError("分块大小必须为正数")
        path = self._path(object_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        digest = hashlib.sha256(); size = 0; index = 0
        try:
            with temporary.open("wb") as handle:
                while True:
                    if cancel and cancel():
                        raise StorageCancelledError("存储操作已取消")
                    chunk = source.read(chunk_size)
                    if chunk is None:
                        chunk = b""
                    if not isinstance(chunk, (bytes, bytearray)):
                        raise TypeError("源流必须返回 bytes")
                    if not chunk:
                        break
                    handle.write(chunk); digest.update(chunk); size += len(chunk)
                    if progress:
                        progress(StorageProgress("write", size, total_size, index, len(chunk)))
                    index += 1
                handle.flush(); os.fsync(handle.fileno())
            reference = StoredObject(self.provider_id, object_id, digest.hexdigest(), size)
            if path.exists():
                current = self._reference(path, self.root)
                if current.sha256 != reference.sha256 or current.size_bytes != reference.size_bytes:
                    raise FileExistsError("原始对象已存在且内容不同，拒绝覆盖")
                return current
            try:
                os.link(temporary, path)
            except FileExistsError:
                current = self._reference(path, self.root)
                if current.sha256 != reference.sha256 or current.size_bytes != reference.size_bytes:
                    raise FileExistsError("原始对象并发写入了不同内容")
                return current
            return reference
        finally:
            temporary.unlink(missing_ok=True)

    def read_object(self, object_id: str, authorization: StorageAuthorization) -> bytes:
        destination = io.BytesIO()
        self.read_to_stream(object_id, destination, authorization)
        return destination.getvalue()

    def read_to_stream(self, object_id: str, destination: BinaryIO,
                       authorization: StorageAuthorization, *,
                       progress: Callable[[StorageProgress], None] | None = None,
                       cancel: Callable[[], bool] | None = None) -> StoredObject:
        authorization.require(StorageOperation.READ)
        if not destination.seekable() or not destination.writable():
            raise ValueError("目标流必须可定位且可写")
        path = self._path(object_id)
        reference = self._reference(path, self.root)
        start = destination.tell(); processed = 0; index = 0; digest = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                while True:
                    if cancel and cancel():
                        raise StorageCancelledError("存储操作已取消")
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    written = destination.write(chunk)
                    if written is not None and written != len(chunk):
                        raise IOError("目标流未完整写入对象")
                    processed += len(chunk)
                    digest.update(chunk)
                    if progress:
                        progress(StorageProgress("read", processed, reference.size_bytes,
                                                 index, len(chunk)))
                    index += 1
            if processed != reference.size_bytes or digest.hexdigest() != reference.sha256:
                raise IOError("普通文件读取期间发生变化")
            return reference
        except Exception:
            destination.seek(start); destination.truncate()
            raise

    def list_objects(self, prefix: str, authorization: StorageAuthorization) -> list[StoredObject]:
        authorization.require(StorageOperation.LIST)
        base = self._path(prefix or ".")
        if not base.exists():
            return []
        paths = [base] if base.is_file() else sorted(path for path in base.rglob("*") if path.is_file())
        return [self._reference(path, self.root) for path in paths]

    def locate_object(self, object_id: str, authorization: StorageAuthorization) -> str:
        authorization.require(StorageOperation.LOCATE)
        path = self._path(object_id)
        if not path.is_file():
            raise FileNotFoundError(object_id)
        return str(path)

    def verify_integrity(self, reference: StoredObject, authorization: StorageAuthorization) -> bool:
        authorization.require(StorageOperation.VERIFY)
        path = self._path(reference.object_id)
        if not path.is_file():
            return False
        current = self._reference(path, self.root)
        return current.sha256 == reference.sha256 and current.size_bytes == reference.size_bytes

    def export_object(self, object_id: str, destination: Path,
                      authorization: StorageAuthorization) -> StoredObject:
        authorization.require(StorageOperation.EXPORT)
        source = self._path(object_id)
        if not source.is_file():
            raise FileNotFoundError(object_id)
        destination = destination.expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        raw = destination.read_bytes()
        return StoredObject("export", str(destination), hashlib.sha256(raw).hexdigest(), len(raw))

    def delete_object(self, object_id: str, authorization: StorageAuthorization) -> None:
        authorization.require(StorageOperation.DELETE, destructive=True)
        self._path(object_id).unlink()

    def migrate_object(self, object_id: str, target: StorageProvider, target_id: str,
                       authorization: StorageAuthorization) -> StoredObject:
        authorization.require(StorageOperation.MIGRATE, destructive=True)
        authorization.require(StorageOperation.READ)
        authorization.require(StorageOperation.DELETE, destructive=True)
        authorization.require(StorageOperation.WRITE_ORIGINAL)
        raw = self.read_object(object_id, authorization)
        created = target.write_original(target_id, raw, authorization)
        if not target.verify_integrity(created, authorization):
            raise IOError("迁移目标完整性校验失败，源对象保留")
        self.delete_object(object_id, authorization)
        return created


class UnavailableProtectedStorageProvider(StorageProvider):
    """Truthful placeholder: no key/provider means no protected write of any kind."""

    provider_id = "protected-unconfigured"

    def __init__(self, detail: str = "受保护存储尚未配置，未启用加密，也不会回退为明文。"): self.detail = detail

    def status(self) -> StorageProviderStatus:
        return StorageProviderStatus(
            self.provider_id, ProviderAvailability.NOT_CONFIGURED, False, self.detail,
            StorageCapabilities(False, False, False, False, False, False, False, False, False),
            "not_configured",
        )

    def _fail(self): raise StorageUnavailableError(self.detail)
    def write_original(self, object_id, data, authorization): self._fail()
    def write_stream(self, object_id, source, authorization, **kwargs): self._fail()
    def read_object(self, object_id, authorization): self._fail()
    def read_to_stream(self, object_id, destination, authorization, **kwargs): self._fail()
    def list_objects(self, prefix, authorization): self._fail()
    def locate_object(self, object_id, authorization): self._fail()
    def verify_integrity(self, reference, authorization): self._fail()
    def export_object(self, object_id, destination, authorization): self._fail()
    def delete_object(self, object_id, authorization): self._fail()
    def migrate_object(self, object_id, target, target_id, authorization): self._fail()


class StorageRouter:
    def __init__(self, open_provider: StorageProvider, protected_provider: StorageProvider,
                 resolver: StoragePolicyResolver | None = None):
        self.open_provider = open_provider
        self.protected_provider = protected_provider
        self.resolver = resolver or StoragePolicyResolver()

    def provider_for(self, resolved: ResolvedStoragePolicy) -> StorageProvider:
        if resolved.policy is StoragePolicy.OPEN:
            return self.open_provider
        if resolved.policy is StoragePolicy.PROTECTED:
            status = self.protected_provider.status()
            if status.availability is not ProviderAvailability.AVAILABLE or not status.protection_active:
                raise StorageUnavailableError(status.detail)
            return self.protected_provider
        raise StoragePolicyPendingError("存储策略尚待确认，拒绝写入任何提供者")


def workspace_storage_router(context: WorkspaceContext) -> StorageRouter:
    """Build today's honest boundary without creating `.jiedu/protected`."""
    return StorageRouter(OpenFileStorageProvider(context.root), UnavailableProtectedStorageProvider())
