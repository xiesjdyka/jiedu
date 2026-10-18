"""Unified business objects for open, protected, and pending knowledge.

Protected metadata lives inside the encrypted provider.  The plaintext registry
contains only an opaque catalog pointer, progress state, and pending (not yet
protected) imports.  Existing open files remain ordinary workspace files.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .document_converter import DocumentConverter
from .protection_exit import exit_recovery_status, file_identity, remove_verified_file
from .storage import (
    OpenFileStorageProvider, ProviderAvailability, StorageAuthorization,
    StorageCancelledError, StorageOperation, StoragePolicy,
    StoragePolicyResolver, StoredObject,
)
from .workspace import FORMAL_LAYERS, WorkspaceContext


CATALOG_FORMAT = 1
REGISTRY_FORMAT = 1
TEXT_SUFFIXES = {".md", ".txt", ".json", ".csv", ".html", ".htm", ".xml", ".yaml", ".yml"}
IGNORED_PARTS = {".git", ".obsidian", ".knowledge-bridge", ".jiedu", "node_modules", ".venv", ".venv312", "__pycache__"}
KNOWLEDGE_OBJECT_ROOTS = FORMAL_LAYERS + ("99_收件箱",)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256(); size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk); size += len(chunk)
    return digest.hexdigest(), size


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
        for attempt in range(6):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                # Windows readers may briefly hold the destination without
                # delete sharing.  The unique temp remains complete and is
                # retried; no partial job or catalog state is exposed.
                time.sleep(.01 * (attempt + 1))
    finally:
        temporary.unlink(missing_ok=True)


def _safe_segment(value: str) -> str:
    value = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", value.strip()).strip(" .")
    return value[:96] or "导入资料"


class KnowledgeObjectError(RuntimeError):
    pass


class KnowledgeObjectConflict(KnowledgeObjectError):
    pass


class KnowledgeObjectService:
    def __init__(self, context: WorkspaceContext, protected_provider):
        if not context.initialized:
            raise ValueError("统一知识对象服务需要已初始化工作区")
        self.context = context
        self.protected = protected_provider
        self.open = OpenFileStorageProvider(context.root)
        self.resolver = StoragePolicyResolver()
        self.registry_path = context.state / "knowledge-objects.json"
        self.preview_root = context.state / "knowledge-object-previews"
        self._previews: dict[str, dict] = {}
        self.object_index_path = context.index_root("formal") / "knowledge-objects.json"
        self.audit_path = context.audit / "knowledge-objects.jsonl"
        self.recovery_root = context.queues / "knowledge-object-recovery"
        self.converter = DocumentConverter()
        from .object_reader import ObjectReader
        self.reader = ObjectReader(self)
        self._lock = threading.RLock()

    @staticmethod
    def _read_json(path: Path, fallback: dict) -> dict:
        try:
            value = json.loads(path.read_text("utf-8"))
            return value if isinstance(value, dict) else fallback
        except (OSError, ValueError):
            return fallback

    def _registry(self) -> dict:
        value = self._read_json(self.registry_path, {})
        if int(value.get("format_version") or REGISTRY_FORMAT) != REGISTRY_FORMAT:
            raise KnowledgeObjectError("知识对象目录版本不受支持")
        return {
            "format_version": REGISTRY_FORMAT,
            "workspace_id": self.context.workspace_id,
            "generation": int(value.get("generation") or 0),
            "catalog_head": value.get("catalog_head"),
            "pending": list(value.get("pending") or []),
            "open_metadata": dict(value.get("open_metadata") or {}),
            "updated_at": value.get("updated_at"),
        }

    def _save_registry(self, value: dict) -> None:
        saved = {
            "format_version": REGISTRY_FORMAT,
            "workspace_id": self.context.workspace_id,
            "generation": int(value.get("generation") or 0),
            "catalog_head": value.get("catalog_head"),
            "pending": list(value.get("pending") or []),
            "open_metadata": dict(value.get("open_metadata") or {}),
            "updated_at": _now(),
        }
        _atomic_json(self.registry_path, saved)
        # This is the atomic business/index switch.  It contains no protected
        # title, path, summary, or content object identity.
        _atomic_json(self.object_index_path, {
            "format_version": 1,
            "workspace_id": self.context.workspace_id,
            "generation": saved["generation"],
            "catalog_available": bool(saved.get("catalog_head")),
            "updated_at": saved["updated_at"],
        })

    @staticmethod
    def _authorization(*operations: StorageOperation, owner: bool = False, reason: str = "") -> StorageAuthorization:
        return StorageAuthorization.allow(*operations, owner_confirmed=owner, reason=reason)

    def _protected_ready(self) -> bool:
        status = self.protected.status()
        return status.availability is ProviderAvailability.AVAILABLE and status.protection_active

    def _catalog(self, registry: dict | None = None) -> list[dict]:
        registry = registry or self._registry()
        head = registry.get("catalog_head")
        if not head:
            return []
        if not self._protected_ready():
            raise PermissionError("受保护目录当前已锁定")
        raw = self.protected.read_object(head, self._authorization(StorageOperation.READ))
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise KnowledgeObjectError("受保护知识目录损坏") from exc
        if (value.get("format_version") != CATALOG_FORMAT or
                value.get("workspace_id") != self.context.workspace_id or
                not isinstance(value.get("items"), list)):
            raise KnowledgeObjectError("受保护知识目录身份或格式无效")
        return value["items"]

    def _write_catalog(self, items: list[dict], registry: dict) -> tuple[dict, StoredObject | None]:
        old_head = registry.get("catalog_head")
        new_head = f"system/knowledge-catalog/{uuid.uuid4().hex}"
        payload = json.dumps({
            "format_version": CATALOG_FORMAT,
            "workspace_id": self.context.workspace_id,
            "created_at": _now(),
            "items": items,
        }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        auth = self._authorization(StorageOperation.WRITE_ORIGINAL, StorageOperation.VERIFY)
        reference = self.protected.write_original(new_head, payload, auth)
        if not self.protected.verify_integrity(reference, auth):
            raise IOError("受保护知识目录完整性校验失败")
        next_registry = dict(registry)
        next_registry.update({
            "catalog_head": new_head,
            "generation": int(registry.get("generation") or 0) + 1,
        })
        try:
            self._save_registry(next_registry)
        except Exception:
            self.protected.delete_object(new_head, self._authorization(
                StorageOperation.DELETE, owner=True, reason="回滚未提交的知识目录"))
            raise
        return next_registry, reference

    def _audit(self, action: str, result: str, *, counts: dict | None = None,
               request_digest: str | None = None, source_preserved: bool | None = None) -> None:
        row = {"at": _now(), "action": action, "result": result,
               "workspace_id": self.context.workspace_id}
        if counts:
            row["counts"] = {str(k): int(v) for k, v in counts.items()}
        if request_digest:
            row["request_digest"] = request_digest
        if source_preserved is not None:
            row["source_preserved"] = bool(source_preserved)
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush(); os.fsync(handle.fileno())

    def _workspace_relative(self, value: str) -> tuple[str, Path]:
        candidate = Path(value)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("知识对象路径必须是工作区内相对路径")
        relative = candidate.as_posix().lstrip("./")
        parts = Path(relative).parts
        if not parts or parts[0] == "90_原始档案" or any(part in IGNORED_PARTS or part.startswith(".") for part in parts):
            raise ValueError("程序内部文件和原始对话档案不进入普通知识对象")
        unresolved = self.context.root / relative
        current = self.context.root
        for part in Path(relative).parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("符号链接不能作为知识对象")
        path = unresolved.resolve()
        try:
            path.relative_to(self.context.root)
        except ValueError as exc:
            raise ValueError("知识对象路径越出工作区") from exc
        return relative, path

    def _open_items(self) -> list[dict]:
        rows = []
        metadata = self._registry().get('open_metadata',{})
        # Evidence archives are live append-only assets with their own stable
        # session identity.  They never enter this business catalog, even when
        # another client is currently transporting additional messages.
        for folder, dirs, files in os.walk(self.context.root, topdown=True, followlinks=False):
            dirs[:] = sorted((name for name in dirs if name != "90_原始档案" and
                              name not in IGNORED_PARTS and not name.startswith(".") and
                              not (Path(folder) / name).is_symlink()), key=str.casefold)
            for name in sorted(files, key=str.casefold):
                if name.startswith("."):
                    continue
                path = Path(folder) / name
                if path.is_symlink():
                    continue
                try:
                    path.resolve().relative_to(self.context.root)
                except ValueError:
                    continue
                relative_parts = path.relative_to(self.context.root).parts
                if any(part in IGNORED_PARTS or part.startswith(".") for part in relative_parts):
                    continue
                relative = Path(*relative_parts).as_posix()
                size = path.stat().st_size
                digest = None  # Listing must not hash every attachment.
                rows.append({
                    **metadata.get(relative,{}),
                    "entry_id": "open-" + hashlib.sha256(relative.casefold().encode()).hexdigest()[:24],
                    "policy": StoragePolicy.OPEN.value,
                    "title": path.name,
                    "logical_path": relative,
                    "summary": f"{path.suffix.lower().lstrip('.') or '文件'} · {size} 字节",
                    "size_bytes": size,
                    "sha256": digest,
                    "source_kind": metadata.get(relative,{}).get('source_kind',"workspace_file"),
                    "readable": path.suffix.lower() in TEXT_SUFFIXES,
                })
        return rows

    @staticmethod
    def _public_protected_item(item: dict) -> dict:
        return {key: item.get(key) for key in (
            "entry_id", "policy", "title", "logical_path", "summary",
            "size_bytes", "sha256", "source_kind", "created_at", "readable",
            "source_entry_id", "source_sha256", "derived_entry_ids", "conversion",
        )}

    def list_objects(self, *, include_protected: bool = False) -> dict:
        registry = self._registry()
        open_items = self._open_items()
        pending = [{**row, "policy": StoragePolicy.PENDING.value, "readable": False,
                    "saved": False, "summary": "仅登记来源，原件尚未保存到知识库"}
                   for row in registry.get("pending", [])]
        status = self.protected.status()
        protected_items: list[dict] = []
        protected_state = status.lifecycle_state or status.availability.value
        if include_protected and self._protected_ready():
            protected_items = [self._public_protected_item(row) for row in self._catalog(registry)]
        return {
            "workspace_id": self.context.workspace_id,
            "exit_recovery": exit_recovery_status(self.context),
            "generation": registry["generation"],
            "protection_state": protected_state,
            "protected_metadata_visible": bool(include_protected and self._protected_ready()),
            "counts": {
                "open": len(open_items),
                "pending": len(pending),
                "protected": len(protected_items) if include_protected and self._protected_ready() else None,
            },
            "items": open_items + pending + protected_items,
            "read_limits": self.reader.limits(),
            "locked_message": None if self._protected_ready() else "解锁并确认 Owner 身份后才能查看受保护内容的名称和路径。",
        }

    def object_detail(self, entry_id: str, *, include_protected: bool = False) -> dict:
        for row in self._open_items():
            if row["entry_id"] == entry_id:
                _, path = self._workspace_relative(row["logical_path"])
                content = path.read_text("utf-8-sig", errors="replace") if row["readable"] else None
                return {**row, "content": content, "binary": not row["readable"]}
        pending = next((row for row in self._registry().get("pending", [])
                        if row.get("entry_id") == entry_id), None)
        if pending:
            return {**pending, "policy": "pending", "content": None,
                    "registration_only": True, "saved": False, "readable": False,
                    "source_directory": str(Path(pending["source_path"]).parent) if pending.get("source_path") else "",
                    "message": "仅登记来源，原件尚未保存到知识库；请重新扫描并选择普通收件箱或受保护保存。"}
        if not include_protected or not self._protected_ready():
            raise PermissionError("受保护内容当前不可读取")
        item = next((row for row in self._catalog() if row.get("entry_id") == entry_id), None)
        if not item:
            raise FileNotFoundError(entry_id)
        raw = self.protected.read_object(item["content_object"], self._authorization(StorageOperation.READ))
        content = raw.decode("utf-8-sig", errors="replace") if item.get("readable") else None
        return {**self._public_protected_item(item), "content": content,
                "binary": not bool(item.get("readable"))}

    def exit_protection_to_open(self, manager, passphrase: str) -> dict:
        """Publish without replacement; journal before exposing any plaintext."""
        if exit_recovery_status(self.context):
            raise KnowledgeObjectConflict("上次退出保护尚待恢复，请先核对 protection-exit.json 中的记录")
        manager.verify_disable_passphrase(passphrase)
        with self._lock:
            if exit_recovery_status(self.context):
                raise KnowledgeObjectConflict("上次退出保护尚待恢复，请先核对 protection-exit.json 中的记录")
            if self.protected is not manager.provider or not self._protected_ready():
                raise PermissionError("请先用当前密码解锁保护层")
            registry = self._registry()
            catalog = self._catalog(registry)
            if not catalog:
                manager.disable_empty_storage()
                return {"status": "disabled", "exported": 0, "source_preserved": True}
            plans = []
            existing = []
            seen = {}
            for row in catalog:
                relative, destination = self._workspace_relative(row["logical_path"])
                digest = row.get("sha256")
                if not isinstance(digest, str) or len(digest) != 64:
                    raise KnowledgeObjectConflict("受保护原件缺少完整性校验值")
                key = os.path.normcase(relative)
                if key in seen:
                    if seen[key] != (digest, row["size_bytes"]):
                        raise KnowledgeObjectConflict("受保护资料存在同路径不同内容，请先处理冲突")
                    continue
                seen[key] = (digest, row["size_bytes"])
                if destination.exists():
                    if not destination.is_file() or _sha_file(destination) != (digest, row["size_bytes"]):
                        raise KnowledgeObjectConflict(f"普通文件与受保护原件冲突：{relative}")
                    existing.append((destination, file_identity(destination), digest, row["size_bytes"]))
                    continue
                plans.append((row, relative, destination))
            transaction_id = uuid.uuid4().hex
            stage = self.recovery_root / "protection-exit" / transaction_id
            journal_path = self.context.state / "protection-exit.json"
            journal = {"format_version": 1, "workspace_id": self.context.workspace_id,
                       "transaction_id": transaction_id, "status": "preparing",
                       "created_at": _now(), "original_registry": registry,
                       "keyring_identity": file_identity(self.protected.keyring_path),
                       "settings_path": str(manager._settings_path),
                       "stage": str(stage.relative_to(self.context.root)),
                       "encrypted_backup": str((self.context.versions / "retired-protection" /
                                                transaction_id).relative_to(self.context.root)),
                       "files": [], "plaintext_exposure_possible": True}
            _atomic_json(journal_path, journal)
            registry_changed = False
            try:
                stage.mkdir(parents=True, exist_ok=False)
                for index, (row, relative, _) in enumerate(plans):
                    temporary = stage / str(index)
                    self.protected.export_object(row["content_object"], temporary,
                        self._authorization(StorageOperation.EXPORT, owner=True,
                                            reason="用户关闭密码保护前恢复普通文件"))
                    receipt = {"destination": relative, "stage_name": str(index),
                               "identity": file_identity(temporary),
                               "sha256": row["sha256"], "size_bytes": row["size_bytes"],
                               "publish_started": False, "published": False}
                    journal["files"].append(receipt)
                    _atomic_json(journal_path, journal)
                    if _sha_file(temporary) != (row["sha256"], row["size_bytes"]):
                        raise IOError("受保护资料导出后校验失败；原密文未变")
                journal["status"] = "publishing"
                _atomic_json(journal_path, journal)
                for receipt in journal["files"]:
                    relative, destination = self._workspace_relative(receipt["destination"])
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    receipt["publish_started"] = True
                    _atomic_json(journal_path, journal)
                    try:
                        # A hard link is an atomic no-replace publication. Keep
                        # the staging link as a file-identity recovery receipt.
                        os.link(stage / receipt["stage_name"], destination)
                    except FileExistsError:
                        receipt["publish_started"] = False
                        raise KnowledgeObjectConflict(f"导出期间出现同名文件：{relative}") from None
                    receipt["published"] = True
                    _atomic_json(journal_path, journal)
                    if (file_identity(destination) != receipt["identity"] or
                            _sha_file(destination) != (receipt["sha256"], receipt["size_bytes"])):
                        raise IOError("普通文件落盘后校验失败；原密文未变")
                for destination, identity, digest, size in existing:
                    if file_identity(destination) != identity or _sha_file(destination) != (digest, size):
                        raise KnowledgeObjectConflict("预存普通文件在退出期间发生变化")
                next_registry = dict(registry)
                next_registry["catalog_head"] = None
                next_registry["generation"] = registry["generation"] + 1
                metadata = dict(registry.get("open_metadata") or {})
                for row in catalog:
                    metadata[row["logical_path"]] = {
                        "source_kind": row.get("source_kind", "protected_exit"),
                        "conversion": row.get("conversion"),
                        "derived_entry_ids": row.get("derived_entry_ids", []),
                    }
                next_registry["open_metadata"] = metadata
                journal["status"] = "committing"
                _atomic_json(journal_path, journal)
                registry_changed = True
                self._save_registry(next_registry)
                backup = manager.disable_after_export(exit_id=transaction_id)
                self.protected = manager.provider
                # Do not roll back a committed settings/keyring transition if
                # cleanup or an audit write later fails.
                cleanup = self._cleanup_exit_files(journal, stage, rollback=False)
                journal["status"] = "recovery_required" if cleanup else "completed"
                journal["remaining"] = cleanup
                if not cleanup:
                    journal.pop("files", None)
                    journal.pop("original_registry", None)
                    journal["plaintext_exposure_possible"] = False
                _atomic_json(journal_path, journal)
                self._audit("exit_protection_to_open", "success",
                            counts={"objects": len(catalog)}, source_preserved=True)
                return {"status": "disabled", "exported": len(catalog),
                        "source_preserved": True, "encrypted_backup": backup,
                        "exit_recovery": exit_recovery_status(self.context)}
            except Exception as exc:
                remaining = []
                if manager.storage_enabled:
                    try:
                        settings = manager._read_settings().get("workspaces", {}).get(self.context.workspace_id, {})
                        settings_restored = settings.get("protected_storage_enabled") is True
                    except Exception:
                        settings_restored = False
                    if not settings_restored:
                        remaining.append({"kind": "settings_restore_required"})
                    try:
                        if (file_identity(self.protected.keyring_path) != journal["keyring_identity"] or
                                (self.context.root / journal["encrypted_backup"]).exists()):
                            remaining.append({"kind": "keyring_restore_required"})
                    except OSError:
                        remaining.append({"kind": "keyring_restore_required"})
                    if registry_changed:
                        try:
                            self._save_registry(registry)
                        except Exception:
                            remaining.append({"kind": "registry_restore_failed"})
                    remaining.extend(self._cleanup_exit_files(journal, stage, rollback=True))
                    journal["status"] = "recovery_required" if remaining else "rolled_back"
                else:
                    # The disable commit completed. Keep exported files and the
                    # cleared catalog; journal reconciliation is now required.
                    remaining.append({"kind": "post_commit_failure"})
                    journal["status"] = "recovery_required"
                journal["remaining"] = remaining
                journal["error_type"] = type(exc).__name__
                if not remaining:
                    journal.pop("files", None)
                    journal.pop("original_registry", None)
                    journal["plaintext_exposure_possible"] = False
                try:
                    _atomic_json(journal_path, journal)
                except Exception:
                    remaining.append({"kind": "journal_update_failed"})
                message = ("已切换普通模式，但退出收尾未完成；普通副本保留，恢复记录：" + str(journal_path)
                           if not manager.storage_enabled else
                           "退出保护未完成；回滚不完整，可能存在明文，恢复记录：" + str(journal_path)
                           if remaining else "退出保护未完成；本次普通副本已回滚，原密文与目录保留")
                raise KnowledgeObjectError(message) from exc

    def _cleanup_exit_files(self, journal: dict, stage: Path, *, rollback: bool) -> list[dict]:
        """Never remove a preexisting/replaced/modified destination by pathname."""
        remaining = []
        for receipt in reversed(journal["files"]):
            targets = [("stage", stage / receipt["stage_name"])]
            if rollback and receipt["publish_started"]:
                targets.insert(0, ("destination", self.context.root / receipt["destination"]))
            for kind, path in targets:
                try:
                    # Reject redirected parents before the identity-bound open.
                    if path.resolve().parent != path.absolute().parent:
                        raise OSError("恢复路径已被重定向")
                    result = remove_verified_file(path, receipt["identity"],
                                                  receipt["sha256"], receipt["size_bytes"])
                except Exception:
                    result = "retained_cleanup_failed"
                if result not in {"removed", "absent"}:
                    remaining.append({"kind": kind, "path": str(path.relative_to(self.context.root)),
                                      "reason": result})
        try:
            if stage.exists():
                if any(stage.iterdir()):
                    remaining.append({"kind": "staging_files_remain", "path": str(stage)})
                else:
                    stage.rmdir()
        except OSError:
            remaining.append({"kind": "staging_cleanup_failed", "path": str(stage)})
        return remaining

    def _external_files(self, source: str, *, trusted_snapshot: bool = False) -> tuple[Path, list[Path]]:
        root = Path(source).expanduser().resolve()
        if not root.is_dir():
            raise ValueError("请选择真实存在的外部资料文件夹")
        try:
            root.relative_to(self.context.root)
        except ValueError:
            pass
        else:
            allowed=(self.context.cache/'butler-import').resolve()
            if not trusted_snapshot or allowed not in root.parents:
                raise ValueError("导入来源必须位于当前工作区之外")
        files = [item for item in root.rglob("*") if item.is_file() and not item.is_symlink()
                 and not any(part.startswith(".") for part in item.relative_to(root).parts)
                 and not any((root.joinpath(*item.relative_to(root).parts[:index])).is_symlink()
                             for index in range(1, len(item.relative_to(root).parts)))]
        return root, sorted(files, key=lambda item: item.as_posix().casefold())

    def preview_import(self, source: str, default_policy: str = "pending",
                       overrides: dict[str, str] | None = None, *, _trusted_snapshot: bool = False) -> dict:
        with self._lock:
            root, files = self._external_files(source,trusted_snapshot=_trusted_snapshot)
            overrides = overrides or {}
            registry = self._registry()
            catalog = self._catalog(registry) if self._protected_ready() else []
            protected_by_path = {row["logical_path"]: row for row in catalog}
            pending_keys = {(row.get("source_path"), row.get("sha256")) for row in registry["pending"]}
            task_default = StoragePolicy.parse(default_policy)
            rows = []
            base = f"99_收件箱/导入/{_safe_segment(root.name)}"
            for path in files:
                relative = path.relative_to(root).as_posix()
                digest, size = _sha_file(path)
                resolved = self.resolver.resolve(
                    workspace=StoragePolicy.OPEN,
                    import_task=task_default,
                    item=overrides.get(relative, StoragePolicy.INHERIT.value),
                )
                logical_path = f"{base}/{relative}"
                conversion = self.converter.inspect(path)
                derived_path = None
                if conversion["status"] not in {"source_markdown", "unsupported", "converter_unavailable"}:
                    derived_path = f"{base}/_派生知识/{relative}.md"
                conflict = None; idempotent = False
                if resolved.policy is StoragePolicy.OPEN:
                    _, target = self._workspace_relative(logical_path)
                    if target.exists():
                        current_sha, current_size = _sha_file(target)
                        idempotent = current_sha == digest and current_size == size
                        if not idempotent:
                            conflict = "公开目标已存在且内容不同"
                elif resolved.policy is StoragePolicy.PROTECTED:
                    if not self._protected_ready():
                        conflict = "需要先解锁保护层"
                    elif logical_path in protected_by_path:
                        current = protected_by_path[logical_path]
                        idempotent = current.get("sha256") == digest and int(current.get("size_bytes") or -1) == size
                        if not idempotent:
                            conflict = "受保护目录已有同名不同内容对象"
                elif resolved.policy is StoragePolicy.PENDING:
                    idempotent = (str(path), digest) in pending_keys
                rows.append({
                    "item_key": hashlib.sha256((str(path) + "\0" + digest).encode()).hexdigest(),
                    "relative_path": relative,
                    "source_path": str(path),
                    "logical_path": logical_path,
                    "title": path.name,
                    "sha256": digest,
                    "size_bytes": size,
                    "policy": resolved.policy.value,
                    "decided_by": resolved.decided_by,
                    "conflict": conflict,
                    "idempotent": idempotent,
                    "readable": path.suffix.lower() in TEXT_SUFFIXES,
                    "conversion": conversion,
                    "derived_path": derived_path,
                })
            preview_id = uuid.uuid4().hex
            request_digest = hashlib.sha256(json.dumps([
                str(root), task_default.value,
                [(row["relative_path"], row["sha256"], row["policy"]) for row in rows],
            ], ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
            preview = {
                "format_version": 1, "kind": "import", "preview_id": preview_id,
                "workspace_id": self.context.workspace_id, "created_at": _now(),
                "source_root": str(root), "default_policy": task_default.value,
                "request_digest": request_digest, "items": rows,
                "counts": {value: sum(row["policy"] == value for row in rows)
                           for value in ("open", "protected", "pending")},
                "total_bytes": sum(row["size_bytes"] for row in rows),
                "conflicts": sum(bool(row["conflict"]) for row in rows),
                "external_source_unchanged": True,
                "conversion_available": any(row["conversion"]["status"] in {"ready", "source_markdown"}
                                            for row in rows),
            }
            self._remember_preview(preview_id, preview)
            return preview

    def preview_migration(self, paths: list[str]) -> dict:
        with self._lock:
            if not paths:
                raise ValueError("请至少选择一个公开对象")
            registry = self._registry()
            catalog = self._catalog(registry) if self._protected_ready() else []
            protected_by_path = {row["logical_path"]: row for row in catalog}
            rows = []
            for value in dict.fromkeys(paths):
                relative, path = self._workspace_relative(value)
                if not path.is_file():
                    rows.append({"logical_path": relative, "migratable": False,
                                 "reason": "公开源不存在", "size_bytes": 0})
                    continue
                digest, size = _sha_file(path)
                conflict = None
                if not self._protected_ready():
                    conflict = "需要先解锁保护层"
                elif relative in protected_by_path:
                    current = protected_by_path[relative]
                    if current.get("sha256") != digest or int(current.get("size_bytes") or -1) != size:
                        conflict = "保护层已有同路径不同内容对象"
                rows.append({
                    "item_key": hashlib.sha256((relative + "\0" + digest).encode()).hexdigest(),
                    "logical_path": relative, "title": path.name,
                    "sha256": digest, "size_bytes": size,
                    "migratable": conflict is None, "reason": conflict,
                    "readable": path.suffix.lower() in TEXT_SUFFIXES,
                    "estimated_target": "受保护知识树中的原位置",
                })
            preview_id = uuid.uuid4().hex
            request_digest = hashlib.sha256(json.dumps(
                [(row["logical_path"], row.get("sha256")) for row in rows],
                ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
            preview = {
                "format_version": 1, "kind": "migration", "preview_id": preview_id,
                "workspace_id": self.context.workspace_id, "created_at": _now(),
                "request_digest": request_digest, "items": rows,
                "object_count": len(rows), "total_bytes": sum(row.get("size_bytes", 0) for row in rows),
                "conflicts": sum(not row.get("migratable", False) for row in rows),
                "needs_unlock": not self._protected_ready(), "needs_owner_confirmation": True,
                "execution_order": ["复制到保护层", "完整性验证", "原子切换知识目录", "最后处理公开源"],
            }
            self._remember_preview(preview_id, preview)
            return preview

    def _remember_preview(self, preview_id: str, preview: dict) -> None:
        # Preview has source names before the user chooses a storage policy.
        # Keep it in process memory; restart requires a new scan.
        while len(self._previews) >= 32:
            self._previews.pop(next(iter(self._previews)))
        self._previews[preview_id] = preview

    def _preview(self, preview_id: str, kind: str) -> tuple[dict, Path]:
        if not re.fullmatch(r"[0-9a-f]{32}", preview_id or ""):
            raise ValueError("预览身份无效")
        path = self.preview_root / f"{preview_id}.json"
        value = self._previews.get(preview_id, {})
        if value.get("workspace_id") != self.context.workspace_id or value.get("kind") != kind:
            raise ValueError("预览不存在或不属于当前工作区")
        return value, path

    @staticmethod
    def _emit(callback: Callable[[dict], None] | None, stage: str, completed: int,
              total: int, message: str) -> None:
        if callback:
            callback({"stage": stage, "completed": completed, "total": total,
                      "message": message, "pages_total": None, "pages_completed": None})

    def execute_import(self, preview_id: str, *, owner_confirmed: bool,
                       progress: Callable[[dict], None] | None = None,
                       cancel: Callable[[], bool] | None = None) -> dict:
        if not owner_confirmed:
            raise PermissionError("导入执行需要 Owner 明确确认")
        with self._lock:
            preview, preview_path = self._preview(preview_id, "import")
            if preview.get("conflicts"):
                raise KnowledgeObjectConflict("导入预览包含冲突，请先处理")
            rows = preview["items"]; registry = self._registry()
            from .local_ocr import OCR_FORMATS
            protection_generation = self.protected._key_generation
            has_protected = any(r['policy']=='protected' for r in rows)
            user_cancel = cancel
            def cancel():
                return bool((user_cancel and user_cancel()) or (has_protected and self.protected._key_generation != protection_generation))
            def conversion_progress(value):
                if progress:progress(value)
            old_registry = json.loads(json.dumps(registry, ensure_ascii=False))
            catalog = self._catalog(registry) if self._protected_ready() else []
            created_open: list[str] = []; created_protected: list[str] = []
            rollback_new=[]
            created_catalog: str | None = None
            additions: list[dict] = []; pending = list(registry["pending"])
            catalog_changed = False
            conversion_report: list[dict] = []
            total = len(rows)
            try:
                for index, row in enumerate(rows, 1):
                    if cancel and cancel():
                        raise StorageCancelledError("导入已取消")
                    source = Path(row["source_path"])
                    digest, size = _sha_file(source)
                    if digest != row["sha256"] or size != row["size_bytes"]:
                        raise KnowledgeObjectConflict("导入源在预览后发生变化")
                    self._emit(progress, "copy", index - 1, total, f"正在处理第 {index} 项")
                    if row["idempotent"]:
                        derived = row.get("derived_path")
                        prior=next((r for r in catalog if r['logical_path']==row['logical_path']),None)
                        prior_conversion=(prior or {}).get('conversion',{}) if row['policy']=='protected' else registry.get('open_metadata',{}).get(row['logical_path'],{}).get('conversion',{})
                        retry_ocr = (source.suffix.lower() in OCR_FORMATS and prior_conversion.get('status') not in {'converted','empty'})
                        if not retry_ocr and (row["policy"] != "open" or not derived or (self.context.root / derived).is_file()):
                            continue
                    if row["policy"] == "open":
                        existed = (self.context.root / row["logical_path"]).exists()
                        with source.open("rb") as handle:
                            reference = self.open.write_stream(
                                row["logical_path"], handle,
                                self._authorization(StorageOperation.WRITE_ORIGINAL), total_size=size)
                        if not existed:
                            created_open.append(row["logical_path"])
                        if reference.sha256 != digest or not self.open.verify_integrity(reference, self._authorization(StorageOperation.VERIFY)):
                            raise IOError("公开导入完整性校验失败")
                        if source.suffix.lower() in OCR_FORMATS:
                            converted = self.converter.convert(self.context.root / row["logical_path"],progress=conversion_progress,cancel=cancel)
                        else:converted = self.converter.convert(self.context.root / row["logical_path"])
                        if cancel():raise StorageCancelledError('转换后已取消或保护会话失效')
                        report = {"relative_path": row["relative_path"], **converted.public(),
                                  "derived_path": row.get("derived_path"), "original_path": row["logical_path"]}
                        if converted.status in {"converted","partial"} and converted.markdown is not None and row.get("derived_path"):
                            provenance = (
                                "---\n"
                                "jiedu_derived: true\n"
                                f"source_path: {json.dumps(row['logical_path'], ensure_ascii=False)}\n"
                                f"source_sha256: {row['sha256']}\n"
                                f"source_entry_id: open-{hashlib.sha256(row['logical_path'].casefold().encode()).hexdigest()[:24]}\n"
                                f"converter: {converted.converter}\n"
                                f"source_format: {converted.source_format}\n"
                                f"conversion_metadata: {json.dumps(converted.metadata,ensure_ascii=False)}\n"
                                "---\n\n"
                            )
                            payload = (provenance + converted.markdown.rstrip() + "\n").encode("utf-8")
                            # Keep earlier partial derivatives immutable. A changed
                            # retry gets a content-addressed sibling; identical retry reuses it.
                            if (self.context.root/row['derived_path']).exists() and (self.context.root/row['derived_path']).read_bytes()!=payload:
                                row['derived_path']=row['derived_path'][:-3]+'.'+hashlib.sha256(payload).hexdigest()[:16]+'.md'
                                report['derived_path']=row['derived_path']
                            derived_target = self.context.root / row["derived_path"]
                            derived_existed = derived_target.exists()
                            if derived_existed:
                                existing = derived_target.read_text("utf-8-sig", errors="replace")
                                if f"source_sha256: {row['sha256']}" not in existing:
                                    raise KnowledgeObjectConflict("派生知识目标已存在且来源不同")
                            else:
                                reference = self.open.write_stream(
                                    row["derived_path"], io.BytesIO(payload),
                                    self._authorization(StorageOperation.WRITE_ORIGINAL), total_size=len(payload))
                                if not self.open.verify_integrity(reference, self._authorization(StorageOperation.VERIFY)):
                                    raise IOError("派生知识完整性校验失败")
                                created_open.append(row["derived_path"])
                            report["status"] = converted.status
                        if converted.status not in {"converted","partial"}:
                            report["derived_path"] = None
                        conversion_report.append(report)
                        original_id='open-'+hashlib.sha256(row['logical_path'].casefold().encode()).hexdigest()[:24]
                        public_metadata=registry.setdefault('open_metadata',{})
                        public_metadata[row['logical_path']]={'conversion':converted.public(),'derived_entry_ids':[]}
                        if report.get('derived_path'):
                            derived_id='open-'+hashlib.sha256(report['derived_path'].casefold().encode()).hexdigest()[:24]
                            public_metadata[row['logical_path']]['derived_entry_ids']=[derived_id]
                            public_metadata[report['derived_path']]={'conversion':converted.public(),
                                'source_entry_id':original_id,'source_sha256':digest,'source_kind':'open_derived'}
                    elif row["policy"] == "protected":
                        if not self._protected_ready():
                            raise PermissionError("保护层已锁定，不能导入受保护对象")
                        prior=next((r for r in catalog if r['logical_path']==row['logical_path']),None) if row['idempotent'] else None
                        if prior:
                            original=prior;object_id=prior['content_object'];catalog_changed=True
                        else:
                            object_id = f"content/{uuid.uuid4().hex}"
                            rollback_new.append(self.protected.prepare_import_rollback(object_id,self._authorization(StorageOperation.WRITE_ORIGINAL,StorageOperation.DELETE,owner=True)))
                            with source.open("rb") as handle:
                                reference = self.protected.write_stream(object_id, handle,
                                    self._authorization(StorageOperation.WRITE_ORIGINAL), total_size=size)
                            created_protected.append(object_id)
                            if reference.sha256 != digest or not self.protected.verify_integrity(reference, self._authorization(StorageOperation.VERIFY)):
                                raise IOError("受保护导入完整性校验失败")
                            additions.append({
                            "entry_id": uuid.uuid4().hex, "policy": "protected",
                            "title": row["title"], "logical_path": row["logical_path"],
                            "summary": f"{Path(row['title']).suffix.lower().lstrip('.') or '文件'} · {size} 字节",
                            "size_bytes": size, "sha256": digest,
                            "source_kind": "external_import", "created_at": _now(),
                            "readable": row["readable"], "content_object": object_id,
                        })
                            original = additions[-1]
                        if size <= self.converter.MEMORY_INPUT_LIMIT:
                            raw = self.protected.read_object(object_id,self._authorization(StorageOperation.READ))
                            if source.suffix.lower() in OCR_FORMATS:
                                converted = self.converter.convert_bytes(raw, source.suffix,progress=conversion_progress,cancel=cancel)
                            else:converted = self.converter.convert_bytes(raw, source.suffix)
                            del raw
                        else:
                            from .document_converter import ConversionResult
                            converted = ConversionResult('size_limit',source.suffix.lstrip('.'),'memory-bounded',
                                reason='原件已保存；内存转换输入上限 16 MiB，文本原件仍可有界读取')
                        if cancel():raise StorageCancelledError('转换后已取消或保护会话失效')
                        original['conversion'] = converted.public()
                        report = {'status':converted.status, 'original_entry_id':original['entry_id'],
                                  'protected':True, 'reason':converted.reason}
                        if converted.status in {'converted','partial'} and converted.markdown is not None:
                            if cancel and cancel(): raise StorageCancelledError('转换后已取消，回滚本次导入')
                            payload = ('---\njiedu_derived: true\nsource_path: ' +
                                json.dumps(row['logical_path'], ensure_ascii=False) +
                                '\nsource_sha256: ' + digest + '\nsource_entry_id: ' + original['entry_id'] +
                                '\nconverter: ' + converted.converter + '\n---\n\n' + converted.markdown).encode('utf-8')
                            derived_id = f'content/{uuid.uuid4().hex}'
                            existing_derived=next((item for item in catalog if item.get('source_entry_id')==original['entry_id'] and item.get('sha256')==hashlib.sha256(payload).hexdigest()),None)
                            if existing_derived:
                                report['derived_entry_id']=existing_derived['entry_id']
                                conversion_report.append(report)
                                continue
                            rollback_new.append(self.protected.prepare_import_rollback(derived_id,self._authorization(StorageOperation.WRITE_ORIGINAL,StorageOperation.DELETE,owner=True)))
                            ref = self.protected.write_original(derived_id, payload,
                                self._authorization(StorageOperation.WRITE_ORIGINAL))
                            created_protected.append(derived_id)
                            if not self.protected.verify_integrity(ref, self._authorization(StorageOperation.VERIFY)):
                                raise IOError('受保护派生文档校验失败')
                            derived_entry_id=uuid.uuid4().hex
                            additions.append({**original, 'entry_id': derived_entry_id,
                                'title': row['title'] + '.md', 'logical_path': row['derived_path'],
                                'summary': f'Markdown · {len(payload)} 字节',
                                'size_bytes': len(payload), 'sha256': hashlib.sha256(payload).hexdigest(),
                                'readable': True, 'content_object': derived_id,
                                'source_kind': 'protected_derived', 'source_entry_id': original['entry_id'],
                                'source_sha256':digest, 'derived_entry_ids':[]})
                            original['derived_entry_ids']=original.get('derived_entry_ids',[])+[derived_entry_id]
                            report['derived_entry_id']=derived_entry_id
                        conversion_report.append(report)
                    else:
                        pending.append({
                            "entry_id": uuid.uuid4().hex, "title": row["title"],
                            "logical_path": row["logical_path"], "summary": "等待人工决定存储位置",
                            "size_bytes": size, "sha256": digest,
                            "source_kind": "external_import", "source_path": row["source_path"],
                            "created_at": _now(),
                        })
                    pending = [item for item in pending if not (
                        item.get("source_path") == row["source_path"] and
                        item.get("sha256") == digest and row["policy"] != "pending")]
                    self._emit(progress, "verify", index, total, f"已验证 {index}/{total} 项")
                if cancel and cancel():
                    raise StorageCancelledError("导入已取消，已回滚本次新增文件")
                registry["pending"] = pending
                if additions or catalog_changed or registry != old_registry:
                    if additions or catalog_changed:
                        with self.protected._lifecycle_lock:
                            if cancel():raise StorageCancelledError('提交前保护会话失效')
                            registry, catalog_reference = self._write_catalog(catalog + additions, registry)
                            created_catalog = catalog_reference.object_id if catalog_reference else None
                    else:
                        registry["generation"] += 1; self._save_registry(registry)
                preview_path.unlink(missing_ok=True)
                self._previews.pop(preview_id, None)
                counts = {value: sum(row["policy"] == value for row in rows)
                          for value in ("open", "protected", "pending")}
                # Include idempotent results so a repeat import still has usable links.
                reported={item["relative_path"] for item in conversion_report if 'relative_path' in item}
                for row in rows:
                    if row['policy']=='protected' and not any(item.get('original_entry_id') == candidate.get('entry_id')
                            for candidate in catalog+additions if candidate.get('logical_path')==row['logical_path']
                            for item in conversion_report):
                        original=next((item for item in catalog+additions if item.get('logical_path')==row['logical_path']),None)
                        if original:
                            conversion_report.append({'status':'already_imported','protected':True,
                                'original_entry_id':original['entry_id'],
                                'derived_entry_id':next(iter(original.get('derived_entry_ids',[])),None)})
                    if row["policy"]=="open" and row["relative_path"] not in reported:
                        derived=row.get("derived_path")
                        conversion_report.append({"relative_path":row["relative_path"],"status":"already_imported",
                            "original_path":row["logical_path"],"derived_path":derived if derived and (self.context.root/derived).is_file() else None})
                conversion_counts = {status: sum(row["status"] == status for row in conversion_report)
                                     for status in sorted({row["status"] for row in conversion_report})}
                self._audit("import", "success", counts=counts,
                            request_digest=preview["request_digest"], source_preserved=True)
                message = ("来源登记完成，原件尚未保存" if counts["pending"] == len(rows) and rows else
                           "资料处理完成，部分仅登记来源" if counts["pending"] else
                           "导入完成，外部源保持不变")
                self._emit(progress, "complete", total, total, message)
                return {"status": "completed", "counts": counts, "source_preserved": True,
                        "message": message,
                        "generation": registry["generation"],
                        "conversion": {"counts": conversion_counts, "items": conversion_report,
                                       "local_only": True, "ocr_uploaded": False}}
            except Exception:
                for rollback in reversed(rollback_new):
                    rollback()
                for relative in reversed(created_open):
                    try:
                        self.open.delete_object(relative, self._authorization(
                            StorageOperation.DELETE, owner=True, reason="回滚失败导入"))
                    except Exception:
                        pass
                for object_id in reversed(created_protected):
                    try:
                        self.protected.delete_object(object_id, self._authorization(
                            StorageOperation.DELETE, owner=True, reason="回滚失败导入"))
                    except Exception:
                        pass
                if created_catalog:
                    try:
                        self.protected.delete_object(created_catalog, self._authorization(
                            StorageOperation.DELETE, owner=True, reason="回滚失败导入的目录版本"))
                    except Exception:
                        pass
                self._save_registry(old_registry)
                self._audit("import", "failed", request_digest=preview["request_digest"],
                            source_preserved=True)
                raise

    def execute_migration(self, preview_id: str, *, owner_confirmed: bool,
                          progress: Callable[[dict], None] | None = None,
                          cancel: Callable[[], bool] | None = None) -> dict:
        if not owner_confirmed:
            raise PermissionError("迁移需要 Owner 明确确认")
        with self._lock:
            preview, preview_path = self._preview(preview_id, "migration")
            if preview.get("conflicts"):
                raise KnowledgeObjectConflict("迁移预览包含冲突或不可迁移项")
            if not self._protected_ready():
                raise PermissionError("保护层已锁定，不能执行迁移")
            rows = preview["items"]; registry = self._registry()
            old_registry = json.loads(json.dumps(registry, ensure_ascii=False))
            old_catalog = self._catalog(registry)
            created: list[str] = []; additions = []
            recovery = self.recovery_root / preview_id
            total = len(rows); committed = False; new_head = None
            try:
                for index, row in enumerate(rows, 1):
                    if cancel and cancel():
                        raise StorageCancelledError("迁移已取消")
                    relative, source = self._workspace_relative(row["logical_path"])
                    digest, size = _sha_file(source)
                    if digest != row["sha256"] or size != row["size_bytes"]:
                        raise KnowledgeObjectConflict("公开源在预览后发生变化")
                    self._emit(progress, "copy", index - 1, total, f"正在复制第 {index} 项到保护层")
                    object_id = f"content/{uuid.uuid4().hex}"
                    with source.open("rb") as handle:
                        reference = self.protected.write_stream(
                            object_id, handle,
                            self._authorization(StorageOperation.WRITE_ORIGINAL), total_size=size)
                    if not self.protected.verify_integrity(reference, self._authorization(StorageOperation.VERIFY)):
                        raise IOError("迁移目标完整性校验失败，公开源已保留")
                    created.append(object_id)
                    additions.append({
                        "entry_id": uuid.uuid4().hex, "policy": "protected",
                        "title": row["title"], "logical_path": relative,
                        "summary": f"{source.suffix.lower().lstrip('.') or '文件'} · {size} 字节",
                        "size_bytes": size, "sha256": digest,
                        "source_kind": "open_migration", "created_at": _now(),
                        "readable": row["readable"], "content_object": object_id,
                    })
                    self._emit(progress, "verify", index, total, f"已验证 {index}/{total} 项")
                registry, head_reference = self._write_catalog(old_catalog + additions, registry)
                new_head = head_reference.object_id if head_reference else None
                committed = True
                self._emit(progress, "switch", total, total, "知识目录与业务索引已原子切换")
                for index, row in enumerate(rows, 1):
                    relative, source = self._workspace_relative(row["logical_path"])
                    backup = recovery / relative
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, backup)
                    if _sha_file(backup) != (row["sha256"], row["size_bytes"]):
                        raise IOError("公开源恢复副本校验失败")
                    source.unlink()
                    self._emit(progress, "cleanup", index, total, f"已完成公开源处理 {index}/{total}")
                shutil.rmtree(recovery, ignore_errors=True)
                preview_path.unlink(missing_ok=True)
                self._audit("migrate_open_to_protected", "success",
                            counts={"objects": total}, request_digest=preview["request_digest"],
                            source_preserved=False)
                self._emit(progress, "complete", total, total, "迁移完成")
                return {"status": "completed", "object_count": total,
                        "source_preserved": False, "generation": registry["generation"]}
            except Exception:
                for row in rows:
                    relative, source = self._workspace_relative(row["logical_path"])
                    backup = recovery / relative
                    if not source.exists() and backup.is_file():
                        source.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(backup, source)
                if committed:
                    self._save_registry(old_registry)
                for object_id in reversed(created + ([new_head] if new_head else [])):
                    try:
                        self.protected.delete_object(object_id, self._authorization(
                            StorageOperation.DELETE, owner=True, reason="回滚失败迁移"))
                    except Exception:
                        pass
                shutil.rmtree(recovery, ignore_errors=True)
                self._audit("migrate_open_to_protected", "failed",
                            counts={"objects": total}, request_digest=preview["request_digest"],
                            source_preserved=True)
                raise


class KnowledgeObjectJobManager:
    """Small persistent job facade for progress and restart-safe failure reporting."""
    def __init__(self, service: KnowledgeObjectService):
        self.service = service
        self.root = service.context.queues / "knowledge-object-jobs"
        self._lock = threading.RLock(); self._cancel: dict[str, threading.Event] = {}
        self.recover_interrupted()

    def _path(self, job_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", job_id or ""):
            raise ValueError("任务身份无效")
        return self.root / f"{job_id}.json"

    def _save(self, row: dict) -> None:
        row["updated_at"] = _now(); _atomic_json(self._path(row["job_id"]), row)

    def recover_interrupted(self) -> None:
        if not self.root.exists():
            return
        for path in self.root.glob("*.json"):
            row = self.service._read_json(path, {})
            if row.get("status") in {"queued", "running"}:
                row.update({"status": "interrupted", "stage": "recovery",
                            "message": "界渡重启中断了任务；公开源保持不变，可重新预览后执行。",
                            "source_preserved": True})
                _atomic_json(path, row)

    def start(self, kind: str, preview_id: str, *, owner_confirmed: bool) -> dict:
        if kind not in {"import", "migration"}:
            raise ValueError("未知知识对象任务")
        if not owner_confirmed:
            raise PermissionError("执行任务需要 Owner 明确确认")
        job_id = uuid.uuid4().hex
        row = {"job_id": job_id, "kind": kind, "status": "queued", "stage": "queued",
               "completed": 0, "total": 0, "message": "任务已进入本机队列",
               "created_at": _now(), "source_preserved": True}
        self.root.mkdir(parents=True, exist_ok=True); self._save(row)
        event = threading.Event(); self._cancel[job_id] = event
        def update(progress: dict):
            with self._lock:
                current = self.get(job_id)
                current.update(progress); current["status"] = "running"; self._save(current)
        def worker():
            try:
                with self._lock:
                    current = self.get(job_id); current["status"] = "running"; self._save(current)
                operation = (self.service.execute_import if kind == "import"
                             else self.service.execute_migration)
                result = operation(preview_id, owner_confirmed=True, progress=update,
                                   cancel=event.is_set)
                with self._lock:
                    current = self.get(job_id); current.update({"status": "completed",
                        "stage": "complete", "message": result.get("message", "任务完成"), "result": result,
                        "source_preserved": result.get("source_preserved")}); self._save(current)
            except StorageCancelledError as exc:
                with self._lock:
                    current = self.get(job_id); current.update({"status": "cancelled",
                        "stage": "rollback", "message": "导入已取消，本次新增内容已回滚", "source_preserved": True}); self._save(current)
            except Exception as exc:
                with self._lock:
                    current = self.get(job_id); current.update({"status": "failed",
                        "stage": "rollback", "message": "任务未完成，已尝试回滚；源文件保留，请重新预览", "source_preserved": True}); self._save(current)
            finally:
                self._cancel.pop(job_id, None)
        threading.Thread(target=worker, daemon=True, name=f"knowledge-object-{kind}-{job_id[:8]}").start()
        return row

    def recent(self, limit: int = 10) -> list[dict]:
        paths=sorted(self.root.glob("*.json"),key=lambda path:path.stat().st_mtime,reverse=True) if self.root.exists() else []
        return [self.service._read_json(path,{}) for path in paths[:limit]]

    def get(self, job_id: str) -> dict:
        row = self.service._read_json(self._path(job_id), {})
        if row.get("job_id") != job_id:
            raise FileNotFoundError(job_id)
        return row

    def cancel(self, job_id: str) -> dict:
        with self._lock:
            row = self.get(job_id)
            if row.get("status") not in {"queued", "running"}:
                return row
            event = self._cancel.get(job_id)
            if not event:
                raise KnowledgeObjectError("任务不在当前进程运行，公开源保持不变")
            event.set()
            row["message"] = "正在安全取消并回滚"
            self._save(row)
            return row
