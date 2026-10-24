"""Stable conversation identity, evidence storage, attachments, and rescue imports."""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import shutil
import time
import uuid
import threading
from contextlib import contextmanager
from functools import wraps
from filelock import FileLock
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .workspace import WorkspaceContext, require_active_workspace
from .durable_state import read_object, write_object, StateCorruptError


ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
ROLE_HEADING = re.compile(r"(?mi)^(?:#{1,6}\s*)?(?:MESSAGE\s+([^\s｜|:-]+)\s*[｜|:-]\s*)?(USER|ASSISTANT|SYSTEM)\s*$")
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".svg"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}


@dataclass(frozen=True)
class SourceDescriptor:
    source_key: str
    product_family: str
    session_source: str
    client_type: str


@dataclass(frozen=True)
class NormalizedAttachment:
    """Source-neutral attachment reference; local_path is never persisted."""

    reference: str
    original_name: str
    local_path: Path | None = None
    mime_type: str | None = None
    media_type: str | None = None
    inline_data: bytes | None = None
    original_name_source: str = "source"


@dataclass(frozen=True)
class NormalizedMessage:
    """The only message contract accepted from source adapters."""

    message_id: str
    role: str
    content: str
    timestamp: str | None = None
    attachments: tuple[NormalizedAttachment, ...] = ()
    provenance: dict | None = None
    sync_position: object = None


_archive_locks={}
_archive_lock_guard=threading.Lock()

@contextmanager
def archive_transaction(context):
    """One reentrant interprocess boundary for source registration, writes and recovery."""
    context.state.mkdir(parents=True,exist_ok=True)
    path=str(context.state.resolve()/'archive-store.lock')
    with _archive_lock_guard:lock=_archive_locks.setdefault(path,FileLock(path))
    with lock:yield

def serialized_archive(function):
    @wraps(function)
    def wrapped(self,*args,**kwargs):
        with archive_transaction(self.context):return function(self,*args,**kwargs)
    return wrapped

class SourceRegistry:
    """Extensible mapping; product family controls folders, source controls identity."""

    BUILTINS = {
        "chatgpt": ("ChatGPT", "ChatGPT Web", "web"),
        "chatgpt_web": ("ChatGPT", "ChatGPT Web", "web"),
        "codex": ("ChatGPT", "Codex", "desktop_agent"),
        "hermes": ("Hermes", "Hermes", "desktop_agent"),
        "doubao_work": ("豆包", "豆包工作版", "desktop_agent"),
    }

    def __init__(self, context: WorkspaceContext):
        self.context = context
        self.path = context.state / "source-registry.json"

    @serialized_archive
    def register(self, source_key: str, product_family: str, session_source: str, client_type: str) -> SourceDescriptor:
        key = source_key.strip().casefold()
        if not key or not product_family.strip() or not session_source.strip() or not client_type.strip():
            raise ValueError("来源注册字段不能为空")
        custom = self._custom()
        custom[key] = {"product_family": product_family.strip(), "session_source": session_source.strip(), "client_type": client_type.strip()}
        _atomic_json(self.path, {"sources": custom})
        return self.resolve(key)

    @serialized_archive
    def resolve(self, source_key: str, *, product_family: str | None = None,
                session_source: str | None = None, client_type: str | None = None) -> SourceDescriptor:
        key = source_key.strip().casefold()
        custom = self._custom().get(key)
        if custom:
            return SourceDescriptor(key, custom["product_family"], custom["session_source"], custom["client_type"])
        if key in self.BUILTINS:
            family, source, kind = self.BUILTINS[key]
            return SourceDescriptor(key, family, source, kind)
        # Future clients keep their actual product name; they are never silently collapsed into “其他”.
        family = (product_family or source_key).strip()
        if not family:
            raise ValueError("未知来源必须提供明确的产品家族")
        return SourceDescriptor(key, family, (session_source or source_key).strip(), (client_type or "external_client").strip())

    @serialized_archive
    def _custom(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value.get("sources", {}) if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, value: dict) -> None:
    write_object(path, value, backup=path.name == 'archive-sessions.json')


def _safe_title(title: str | None, fallback: str = "未命名对话", limit: int = 100) -> str:
    value = ILLEGAL.sub("_", (title or "").strip()).rstrip(". ") or fallback
    if value.upper() in RESERVED:
        value = "_" + value
    return value[:limit].rstrip(". ") or fallback


def _session_key(descriptor: SourceDescriptor, source_instance: str, source_session_id: str) -> str:
    identity = "\0".join((descriptor.session_source, source_instance or "default", source_session_id))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _portable_metadata(value, key: str = ""):
    """Remove machine-specific absolute paths from portable ledgers, not message text."""
    if isinstance(value, dict):
        return {name: _portable_metadata(item, name) for name, item in value.items()}
    if isinstance(value, list):
        return [_portable_metadata(item, key) for item in value]
    if isinstance(value, str) and (re.match(r"^[A-Za-z]:[\\/]", value) or value.startswith(("/", "\\\\"))):
        return {"local_reference_name": Path(value).name, "portable": False}
    return value


class ArchiveStore:
    def __init__(self, context: WorkspaceContext | None = None, *, recover: bool = True):
        self.context = context or require_active_workspace()
        if not self.context.initialized:
            raise ValueError("尚未选择或初始化工作区")
        self.sources = SourceRegistry(self.context)
        self.recovery_report = self.recover_orphaned_manifest_temps() if recover else []

    @property
    def _recovery_root(self) -> Path:
        return self.context.audit / "recovery" / "orphaned-temp"

    @property
    def _recovery_audit(self) -> Path:
        return self.context.audit / "recovery" / "orphaned-temp.jsonl"

    @staticmethod
    def _manifest_identity(value: dict) -> tuple:
        return tuple(value.get(key) for key in (
            "session_key", "source_session_id", "product_family", "session_source"
        ))

    @staticmethod
    def _manifest_progress(value: dict) -> tuple:
        position = value.get("last_sync_position") if isinstance(value.get("last_sync_position"), dict) else {}
        return (str(value.get("updated_at") or ""), int(value.get("message_count") or 0),
                int(position.get("confirmed_offset") or 0))

    def _record_recovery(self, row: dict) -> None:
        signature = (row.get("source_path"), row.get("sha256"), row.get("decision"), row.get("reason"))
        try:
            for existing in self._message_rows(self._recovery_audit):
                if (existing.get("source_path"), existing.get("sha256"), existing.get("decision"),
                        existing.get("reason")) == signature:
                    return
        except (OSError, ValueError):
            pass
        self._recovery_audit.parent.mkdir(parents=True, exist_ok=True)
        with self._recovery_audit.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush(); os.fsync(handle.fileno())

    @serialized_archive
    def _move_recovery_temp(self, temporary: Path, reason: str, *, target: Path) -> dict:
        raw = temporary.read_bytes(); digest = hashlib.sha256(raw).hexdigest()
        self._recovery_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        destination = self._recovery_root / f"{stamp}_{digest[:12]}_{temporary.name}"
        shutil.move(str(temporary), str(destination))
        row = {
            "decision": "moved_to_recovery", "reason": reason, "recorded_at": _now(),
            "source_path": self.context.relative(temporary),
            "target_manifest": self.context.relative(target),
            "recovery_path": self.context.relative(destination),
            "size_bytes": len(raw), "sha256": digest,
        }
        self._record_recovery(row)
        return row

    @serialized_archive
    def _inspect_manifest_temp(self, temporary: Path, formal: Path) -> dict:
        raw = temporary.read_bytes(); digest = hashlib.sha256(raw).hexdigest()
        base = {"source_path": self.context.relative(temporary), "size_bytes": len(raw), "sha256": digest,
                "recorded_at": _now()}
        try:
            candidate = json.loads(raw.decode("utf-8"))
            current = json.loads(formal.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            row = base | {"decision": "retained_in_place", "reason": "invalid_or_missing_json"}
            self._record_recovery(row); return row
        if not isinstance(candidate, dict) or not isinstance(current, dict):
            row = base | {"decision": "retained_in_place", "reason": "manifest_not_object"}
            self._record_recovery(row); return row
        candidate_identity = self._manifest_identity(candidate); current_identity = self._manifest_identity(current)
        if not all(candidate_identity) or candidate_identity != current_identity:
            row = base | {"decision": "retained_in_place", "reason": "identity_mismatch"}
            self._record_recovery(row); return row
        current_progress = self._manifest_progress(current); candidate_progress = self._manifest_progress(candidate)
        current_is_newer = (current_progress[0] >= candidate_progress[0]
                            and current_progress[1] >= candidate_progress[1]
                            and current_progress[2] >= candidate_progress[2])
        required = {"session_key", "source_session_id", "product_family", "session_source", "title",
                    "relative_path", "updated_at", "sync_status"}
        if current_is_newer and required.issubset(current):
            return self._move_recovery_temp(temporary, "formal_manifest_newer_and_complete", target=formal)
        row = base | {"decision": "retained_in_place", "reason": "formal_manifest_not_safely_newer"}
        self._record_recovery(row); return row

    @serialized_archive
    def recover_orphaned_manifest_temps(self) -> dict:
        pattern = re.compile(r"^\.manifest\.json\.[0-9a-f]{32}\.tmp$")
        rows = []
        for item in self._registry().get("sessions", {}).values():
            try:
                metadata = (self.context.root / item["relative_path"] / "03_元数据").resolve()
                metadata.relative_to(self.context.raw_archive.resolve())
            except (KeyError, OSError, ValueError):
                continue
            if not metadata.is_dir():
                continue
            formal = metadata / "manifest.json"
            for temporary in sorted(path for path in metadata.iterdir()
                                    if path.is_file() and pattern.fullmatch(path.name)):
                rows.append(self._inspect_manifest_temp(temporary, formal))
        return {"checked": len(rows), "moved": sum(row.get("decision") == "moved_to_recovery" for row in rows),
                "retained": sum(row.get("decision") == "retained_in_place" for row in rows), "results": rows}

    @serialized_archive
    def _atomic_manifest(self, path: Path, value: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        previous = read_object(path) if path.exists() else None
        with temporary.open('w', encoding='utf-8', newline='\n') as handle:
            handle.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
            handle.flush(); os.fsync(handle.fileno())
        try:
            if previous is not None:
                write_object(path.with_name('manifest.json.last-good'), previous, backup=False)
        except OSError:
            self._move_recovery_temp(temporary, 'atomic_replace_failed', target=path)
            raise
        last_error = None
        for attempt in range(5):
            try:
                os.replace(temporary, path)
                return
            except PermissionError as exc:
                last_error = exc
                if attempt < 4:
                    time.sleep(0.05 * (attempt + 1))
        if temporary.exists():
            self._move_recovery_temp(temporary, "atomic_replace_failed", target=path)
        raise last_error or PermissionError("manifest 原子替换失败")

    @serialized_archive
    def _registry(self) -> dict:
        value = read_object(self.context.archive_registry, {"sessions": {}})
        if not isinstance(value.get('sessions'), dict):
            raise StateCorruptError('会话目录表已损坏；已停止写入以防重复建档')
        return value

    @serialized_archive
    def _save_registry(self, value: dict) -> None:
        _atomic_json(self.context.archive_registry, value)

    @serialized_archive
    def ensure_session(self, *, source_key: str, source_session_id: str, title: str | None,
                       source_instance: str = "default", acquisition_method: str = "bridge",
                       product_family: str | None = None, session_source: str | None = None,
                       client_type: str | None = None) -> dict:
        if not source_session_id.strip():
            raise ValueError("原始会话ID不能为空")
        if re.match(r"^[A-Za-z]:[\\/]", source_instance) or source_instance.startswith(("/", "\\\\")):
            raise ValueError("来源实例不能使用本机绝对路径")
        descriptor = self.sources.resolve(source_key, product_family=product_family,
                                          session_source=session_source, client_type=client_type)
        key = _session_key(descriptor, source_instance, source_session_id)
        registry = self._registry()
        sessions = registry.setdefault("sessions", {})
        existing = sessions.get(key)
        requested_title = (title or "未命名对话").strip() or "未命名对话"
        from .legacy_session_adoption import find_legacy,finish_adoption
        if existing and existing.get('legacy_adoption'):
            plan=find_legacy(self.context,source_key,source_instance,source_session_id)
            if plan:finish_adoption(self,existing,plan)
            return self.session(key)
        if existing:
            if requested_title != existing.get("title"):
                self._rename_session(existing, requested_title, key)
                self._save_registry(registry)
            return self.session(key)
        plan=find_legacy(self.context,source_key,source_instance,source_session_id)
        family_dir = self.context.raw_archive / _safe_title(descriptor.product_family, "未命名产品")
        folder = self.context.root/plan['folder'] if plan else self._available_folder(family_dir, requested_title, key)
        for name in ("00_原始数据", "01_对话", "02_附件", "03_元数据"):
            (folder / name).mkdir(parents=True, exist_ok=True)
        created = _now()
        relative = self.context.relative(folder)
        sessions[key] = {
            "session_key": key,
            "product_family": descriptor.product_family,
            "session_source": descriptor.session_source,
            "source_key": descriptor.source_key,
            "client_type": descriptor.client_type,
            "source_instance": source_instance,
            "source_session_id": source_session_id,
            "acquisition_method": acquisition_method,
            "title": requested_title,
            "title_history": [{"title": requested_title, "from": created, "to": None}],
            "relative_path": relative,
            "created_at": created,
            "updated_at": created,
            "last_sync_position": None,
            "sync_status": "created",
        }
        if plan:
            sessions[key]['legacy_adoption']={k:plan[k] for k in ['source_session_id','snapshot']}
            sessions[key]['title']=plan['manifest'].get('session_title') or plan['manifest'].get('title') or folder.name
            sessions[key]['source_current_title']=requested_title
            sessions[key]['legacy_original_fields']=plan['manifest']
        self._save_registry(registry)
        if plan:finish_adoption(self,sessions[key],plan)
        self._write_metadata(sessions[key])
        return self.session(key)

    def _available_folder(self, family_dir: Path, title: str, key: str, *, current: Path | None = None) -> Path:
        family_dir.mkdir(parents=True, exist_ok=True)
        preferred = family_dir / _safe_title(title)
        if preferred == current or not preferred.exists():
            return preferred
        return family_dir / f"{_safe_title(title, limit=88)} [{key[:8]}]"

    @serialized_archive
    def _rename_session(self, item: dict, requested_title: str, key: str) -> None:
        old_title = item.get("title")
        now = _now()
        history = item.setdefault("title_history", [])
        if history and history[-1].get("to") is None:
            history[-1]["to"] = now
        history.append({"title": requested_title, "from": now, "to": None})
        current = self.context.root / item["relative_path"]
        target = self._available_folder(current.parent, requested_title, key, current=current)
        rename_error = None
        if current != target:
            try:
                current.rename(target)
                item["relative_path"] = self.context.relative(target)
            except OSError as exc:
                rename_error = str(exc)
        item.update({"title": requested_title, "updated_at": now, "folder_rename_error": rename_error})
        self._write_metadata(item)
        self._render_conversation(item)

    @serialized_archive
    def append_message(self, session_key: str, *, message_id: str, role: str, content: str,
                       timestamp: str | None = None, attachment_paths: list[str | Path] | None = None,
                       provenance: dict | None = None, sync_position=None, materialize: bool = True) -> dict:
        if role.casefold() not in {"user", "assistant", "system", "tool"}:
            raise ValueError("不支持的消息角色")
        registry = self._registry()
        item = registry.get("sessions", {}).get(session_key)
        if not item:
            raise ValueError("会话不存在")
        raw_path = self.context.raw_message_store / f"{session_key}.jsonl"
        existing = {row.get("message_id"): row for row in self._message_rows(raw_path)}
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if message_id in existing:
            row = existing[message_id]
            if row.get("content_sha256") != digest or row.get("role") != role.casefold():
                raise ValueError("同一原始消息ID出现冲突内容")
            if materialize:
                self._materialize(item, raw_path)
            return {"saved": False, "duplicate": True, "record": row, "session": self.session(session_key)}
        attachments = [self._archive_attachment(item, Path(path), message_id) for path in (attachment_paths or [])]
        row = {
            "session_key": session_key,
            "source_message_id": message_id,
            "message_id": message_id,
            "message_order": len(existing) + 1,
            "source_position": _portable_metadata(sync_position),
            "role": role.casefold(),
            "timestamp": timestamp,
            "original_text": content,
            "content_sha256": digest,
            "attachments": attachments,
            "source": item["session_source"],
            "ingested_at": _now(),
            "provenance": _portable_metadata(provenance or {}),
        }
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        with raw_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        item.update({"updated_at": _now(), "last_sync_position": sync_position,
                     "sync_status": "synced", "message_count": len(existing) + 1})
        self._save_registry(registry)
        if materialize:
            self._materialize(item, raw_path)
        return {"saved": True, "duplicate": False, "record": row, "session": self.session(session_key)}

    @serialized_archive
    def append_normalized_message(self, session_key: str, message: NormalizedMessage, *,
                                  materialize: bool = True) -> dict:
        specs = list(message.attachments)
        result = self.append_message(
            session_key, message_id=message.message_id, role=message.role, content=message.content,
            timestamp=message.timestamp, provenance=message.provenance,
            sync_position=message.sync_position, materialize=materialize,
        )
        attachment_rows = self.reconcile_attachments(session_key, message.message_id, specs,
                                                     materialize=materialize)
        result["record"] = dict(result["record"], attachments=attachment_rows)
        return result

    @serialized_archive
    def reconcile_attachments(self, session_key: str, message_id: str,
                              attachments: list[NormalizedAttachment], *, materialize: bool = True) -> list[dict]:
        """Append attachment state transitions without rewriting immutable message evidence."""
        registry = self._registry(); item = registry.get("sessions", {}).get(session_key)
        if not item:
            raise ValueError("会话不存在")
        raw_path = self.context.raw_message_store / f"{session_key}.jsonl"
        if not any(row.get("message_id") == message_id for row in self._message_rows(raw_path)):
            raise ValueError("附件引用的消息不存在")
        ledger = self.context.root / item["relative_path"] / "03_元数据" / "attachments.jsonl"
        existing = self._message_rows(ledger)
        latest = {(row.get("source_message_id"), row.get("source_reference")): row for row in existing}
        first = {}
        for historical in existing:
            first.setdefault((historical.get("source_message_id"), historical.get("source_reference")), historical)
        changed = False
        from .attachment_reference_review import read_reviews, review_key
        reviews = read_reviews(self.context)
        for spec in attachments:
            previous = latest.get((message_id, spec.reference))
            row = None
            if previous and not previous.get('missing') and previous.get('sha256') and previous.get('stored_path'):
                # A captured historical entity cannot be replaced by a later file at the same path.
                archived = (self.context.root / previous['stored_path']).resolve()
                if self.context.raw_archive.resolve() not in archived.parents:
                    raise ValueError('附件归档路径越界')
                digest = hashlib.sha256()
                with archived.open('rb') as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                        digest.update(chunk)
                if digest.hexdigest() != previous['sha256']:
                    raise OSError('已归档附件SHA256校验失败；保留原件等待核查')
                row = dict(previous, status=previous.get('status') or 'available',
                           missing=False, size_bytes=previous.get('size_bytes') or archived.stat().st_size)
            review = reviews.get(review_key(session_key, message_id, spec.reference), {})
            if row is None and previous and review.get('decision') == 'unresolved' and spec.inline_data is None:
                # A reviewed historical gap needs identity evidence, not mere path reappearance.
                continue
            if row is None:
                row = self._archive_normalized_attachment(item, spec, message_id)
            original = first.get((message_id, spec.reference))
            if original and original.get("sha256") == row.get("sha256"):
                row["duplicate"] = bool(original.get("duplicate"))
            signature = lambda value: ((value or {}).get("status"), (value or {}).get("sha256"),
                                       (value or {}).get("stored_path"), (value or {}).get("size_bytes"),
                                       (value or {}).get("mime_type"),
                                       (value or {}).get("original_name_source"),
                                       bool((value or {}).get("duplicate")))
            if signature(previous) != signature(row):
                ledger.parent.mkdir(parents=True, exist_ok=True)
                with ledger.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
                    handle.flush(); os.fsync(handle.fileno())
                changed = True
            latest[(message_id, spec.reference)] = row
        if changed and materialize:
            self._render_conversation(item)
        return [latest[(message_id, spec.reference)] for spec in attachments]

    def _archive_normalized_attachment(self, item: dict, spec: NormalizedAttachment,
                                       message_id: str) -> dict:
        source = spec.local_path
        name = spec.original_name or (source.name if source else "未命名附件")
        extension = Path(name).suffix.casefold()
        row = {
            "original_name": name,
            "original_name_source": spec.original_name_source,
            "media_type": spec.media_type or self._media_type(extension),
            "mime_type": spec.mime_type or mimetypes.guess_type(name)[0] or "application/octet-stream",
            "source_message_id": message_id,
            "source_reference": spec.reference or name,
            "sha256": None, "stored_path": None,
            "size_bytes": None,
            "status": "missing", "missing": True, "duplicate": False,
            "checked_at": _now(),
        }
        if spec.inline_data is None and (source is None or not source.is_file()):
            return row
        from .attachment_integrity import store_verified
        folder = self.context.root / item["relative_path"] / "02_附件"; folder.mkdir(parents=True, exist_ok=True)
        destination, digest, size, duplicate = store_verified(
            folder, lambda sha: f'{sha}{extension}', source=source, data=spec.inline_data)
        row.update({"sha256": digest, "stored_path": self.context.relative(destination),
                    "size_bytes": size, "status": "available", "missing": False,
                    "duplicate": duplicate})
        return row

    @serialized_archive
    def attachment_status(self, session_key: str) -> list[dict]:
        return self.attachment_status_many([session_key])[session_key]

    @serialized_archive
    def attachment_status_many(self, session_keys) -> dict[str, list[dict]]:
        # Status refreshes examine every session. Read the shared registry once
        # under the same transaction, not once per session; ledgers stay live.
        registry=self._registry().get('sessions',{});result={}
        for session_key in dict.fromkeys(session_keys):
            item=registry.get(session_key)
            if not item:raise ValueError('会话不存在')
            ledger=self.context.root/item['relative_path']/'03_元数据'/'attachments.jsonl'
            latest={}
            for row in self._message_rows(ledger):
                latest[(row.get('source_message_id'),row.get('source_reference'))]=row
            result[session_key]=list(latest.values())
        return result

    @serialized_archive
    def update_session(self, session_key: str, *, sync_status: str | None = None,
                       sync_position=None, metadata: dict | None = None) -> dict:
        registry = self._registry(); item = registry.get("sessions", {}).get(session_key)
        if not item:
            raise ValueError("会话不存在")
        if sync_status is not None:
            item["sync_status"] = sync_status
        if sync_position is not None:
            item["last_sync_position"] = _portable_metadata(sync_position)
        if metadata:
            item.update(_portable_metadata(metadata))
        item["updated_at"] = _now(); self._save_registry(registry); self._write_metadata(item)
        return self.session(session_key)

    @serialized_archive
    def materialize_session(self, session_key: str) -> dict:
        item = self._registry().get("sessions", {}).get(session_key)
        if not item:
            raise ValueError("会话不存在")
        self._materialize(item, self.context.raw_message_store / f"{session_key}.jsonl")
        return self.session(session_key)

    @serialized_archive
    def record_message_position(self, session_key: str, message_id: str, position: dict) -> None:
        item = self._registry().get("sessions", {}).get(session_key)
        if not item:
            raise ValueError("会话不存在")
        ledger = self.context.root / item["relative_path"] / "03_元数据" / "message-positions.jsonl"
        latest = self._position_map(item).get(message_id)
        portable = _portable_metadata(position) | {"message_id": message_id, "observed_at": _now()}
        signature = lambda value: ((value or {}).get("source_file_identity"),
                                   (value or {}).get("source_object_id"),
                                   (value or {}).get("byte_offset"), (value or {}).get("byte_end"))
        if signature(latest) == signature(portable):
            return
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(portable, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush(); os.fsync(handle.fileno())

    @serialized_archive
    def _position_map(self, item: dict) -> dict[str, dict]:
        ledger = self.context.root / item["relative_path"] / "03_元数据" / "message-positions.jsonl"
        latest = {}
        for row in self._message_rows(ledger):
            if row.get("message_id"):
                latest[row["message_id"]] = row
        return latest

    def _archive_attachment(self, item: dict, source: Path, message_id: str) -> dict:
        name = source.name or "未命名附件"
        extension = source.suffix.casefold()
        base = {
            "original_name": name,
            "media_type": self._media_type(extension),
            "mime_type": mimetypes.guess_type(name)[0] or "application/octet-stream",
            "source_message_id": message_id,
            "source_reference": name,
            "sha256": None,
            "stored_path": None,
            "missing": True,
            "duplicate": False,
        }
        if not source.is_file():
            return base
        from .attachment_integrity import store_verified
        folder = self.context.root / item["relative_path"] / "02_附件"
        folder.mkdir(parents=True, exist_ok=True)
        destination, digest, _, duplicate = store_verified(
            folder, lambda sha: f"{sha[:16]}_{_safe_title(name, '附件', 120)}", source=source)
        base.update({"sha256": digest, "stored_path": self.context.relative(destination),
                     "missing": False, "duplicate": duplicate})
        return base

    @staticmethod
    def _media_type(extension: str) -> str:
        if extension in IMAGE_EXTENSIONS:
            return "image"
        if extension in VIDEO_EXTENSIONS:
            return "video"
        if extension in AUDIO_EXTENSIONS:
            return "audio"
        if extension in {".md", ".txt", ".rtf"}:
            return "text"
        return "file" if extension else "unknown"

    def _message_rows(self, path: Path) -> list[dict]:
        if not path.is_file():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows

    @serialized_archive
    def _materialize(self, item: dict, raw_path: Path) -> None:
        folder = self.context.root / item["relative_path"]
        metadata = folder / "03_元数据"
        metadata.mkdir(parents=True, exist_ok=True)
        portable_raw = metadata / "messages.jsonl"
        temporary = portable_raw.with_suffix(".tmp")
        rows = self._message_rows(raw_path)
        superseded={str((r.get("provenance") or {}).get("supersedes_message_id")) for r in rows if (r.get("provenance") or {}).get("supersedes_message_id")}
        rows=[r for r in rows if str(r.get("message_id")) not in superseded]
        latest_path = metadata / "message-latest.json"
        latest = _read_json(latest_path, {}).get("latest_by_source_message", {})
        if isinstance(latest, dict) and latest:
            def is_current(row):
                provenance=row.get('provenance') or {}
                identity=provenance.get('source_message_id')
                if identity is None and provenance.get('adapter')=='hermes_sqlite_readonly':
                    identity=str(provenance.get('hermes_row_id'))
                return identity not in latest or latest[identity].get('archive_message_id')==row.get('message_id')
            rows = [row for row in rows if is_current(row)]
        from .legacy_session_adoption import project_aliases
        rows=project_aliases(self.context,item,rows)
        positions = self._position_map(item)
        rows = [dict(row, source_position=positions.get(row.get("message_id"), row.get("source_position")))
                for row in rows]
        self._sort_message_rows(item, rows)
        temporary.write_text("".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
                                     for row in rows), encoding="utf-8")
        os.replace(temporary, portable_raw)
        self._render_conversation(item)
        self._write_metadata(item)

    @serialized_archive
    def _render_conversation(self, item: dict) -> None:
        folder = self.context.root / item["relative_path"]
        rows = self._message_rows(folder / "03_元数据" / "messages.jsonl")
        if not (folder / "03_元数据" / "messages.jsonl").is_file():
            rows = self._message_rows(self.context.raw_message_store / f"{item['session_key']}.jsonl")
        self._sort_message_rows(item, rows)
        attachment_latest = {
            (row.get("source_message_id"), row.get("source_reference")): row
            for row in self._message_rows(folder / "03_元数据" / "attachments.jsonl")
        }
        body = [f"# {item['title']}", ""]
        for row in rows:
            body.extend((f"## MESSAGE {row['message_id']}｜{row['role'].upper()}", "", row["original_text"], ""))
            message_attachments = [value for (message_id, _), value in attachment_latest.items()
                                   if message_id == row.get("message_id")] or row.get("attachments", [])
            shown=set()
            for attachment in message_attachments:
                key=(attachment.get('sha256'), attachment.get('original_name')) if attachment.get('sha256') else (
                    'reference', attachment.get('source_reference'))
                if key in shown:continue
                shown.add(key)
                body.append(f"- 附件：{attachment.get('original_name')}（{attachment.get('sha256') or '缺失'}）")
            if message_attachments:
                body.append("")
        target = folder / "01_对话" / "conversation.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        # Path.write_text(newline=None) used the platform's text newline
        # translation before this no-op check; preserve those exact bytes.
        rendered = ("\n".join(body).rstrip() + "\n").replace("\n", os.linesep).encode("utf-8")
        if target.is_file() and target.read_bytes() == rendered:
            return
        temporary = target.with_suffix(".tmp")
        temporary.write_bytes(rendered)
        os.replace(temporary, target)

    @staticmethod
    def _sort_message_rows(item: dict, rows: list[dict]) -> None:
        fragments = {(row.get('source_position') or {}).get('source_fragment') for row in rows}
        multifile = item.get('source_key') == 'codex' and len(fragments - {None}) > 1

        def order(row):
            if item.get('source_key')=='hermes' and (row.get('provenance') or {}).get('hermes_row_id') is not None:
                return (int(row['provenance']['hermes_row_id']),0)
            position = row.get('source_position') or {}
            offset = position.get('byte_offset', 2**63 - 1)
            sequence = int(row.get('message_order') or 0)
            if not multifile:
                return (offset, sequence)
            # Byte offsets only have meaning inside one physical source. A
            # timestamp is used only after parsing it, with deterministic source
            # order as the explicit fallback for missing/invalid timestamps.
            try:
                instant = datetime.fromisoformat(str(position.get('event_timestamp') or row.get('timestamp')).replace('Z', '+00:00'))
                if instant.tzinfo is None:
                    raise ValueError('timezone missing')
                timestamp = instant.timestamp()
            except (ValueError, TypeError, OverflowError):
                timestamp = float('inf')
            return (timestamp, position.get('source_fragment_order', ''), offset, sequence)

        rows.sort(key=order)

    @serialized_archive
    def _write_metadata(self, item: dict) -> None:
        folder = self.context.root / item["relative_path"]
        metadata = folder / "03_元数据" / "manifest.json"
        portable = {key: value for key, value in item.items() if key not in {"folder_rename_error"} or value}
        portable["identity_note"] = "来源标记仅用于同步、去重、补档与排障，不表达权限或长期身份。"
        self._atomic_manifest(metadata, portable)

    @serialized_archive
    def session(self, session_key: str) -> dict:
        item = self._registry().get("sessions", {}).get(session_key)
        if not item:
            raise ValueError("会话不存在")
        return dict(item) | {"archive_path": str(self.context.root / item["relative_path"])}

    @serialized_archive
    def import_folder(self, source_folder: str | Path, *, source_key: str,
                      title: str | None = None, source_instance: str = "rescue-import",
                      product_family: str | None = None) -> dict:
        source = Path(source_folder).expanduser().resolve()
        if not source.is_dir():
            raise ValueError("抢救导入源必须是文件夹")
        inventory = self._inventory(source)
        package_id = hashlib.sha256("".join(f"{x['path']}\0{x['sha256']}\n" for x in inventory).encode()).hexdigest()
        session = self.ensure_session(source_key=source_key, source_session_id=f"folder:{package_id}",
                                      title=title or source.name, source_instance=source_instance,
                                      acquisition_method="folder_rescue_import", product_family=product_family)
        folder = Path(session["archive_path"])
        package_root = folder / "00_原始数据" / "导入包" / package_id[:16]
        copied = False
        if not package_root.exists():
            temporary = package_root.with_name(package_root.name + ".partial")
            if temporary.exists():
                shutil.rmtree(temporary)
            shutil.copytree(source, temporary, copy_function=shutil.copy2)
            os.replace(temporary, package_root)
            copied = True
        recognized, unknown, links = [], [], []
        attachment_by_name = {Path(x["path"]).name.casefold(): x for x in inventory if Path(x["path"]).suffix.casefold() not in {".md", ".txt"}}
        for entry in inventory:
            relative = entry["path"]
            extension = Path(relative).suffix.casefold()
            if extension not in {".md", ".txt"}:
                continue
            path = package_root / Path(relative)
            text = path.read_text(encoding="utf-8-sig", errors="replace")
            matches = list(ROLE_HEADING.finditer(text))
            if not matches:
                unknown.append({"path": relative, "reason": "未识别到明确的消息角色结构"})
                continue
            recognized.append(relative)
            for index, match in enumerate(matches):
                content = text[match.end(): matches[index + 1].start() if index + 1 < len(matches) else len(text)].strip("\r\n")
                if not content:
                    continue
                message_id = str(match.group(1) or f"{relative}:{index + 1}")
                named = [value for name, value in attachment_by_name.items() if name in content.casefold()]
                attachment_paths = [package_root / Path(x["path"]) for x in named]
                result = self.append_message(session["session_key"], message_id=message_id, role=match.group(2).casefold(),
                                             content=content, attachment_paths=attachment_paths,
                                             provenance={"kind": "folder_rescue_import", "package_id": package_id,
                                                         "source_relative_path": relative})
                for attachment in result["record"].get("attachments", []):
                    links.append({"message_id": message_id, "attachment": attachment})
        recognized_set = set(recognized)
        linked_paths = {x["attachment"]["source_reference"].casefold() for x in links}
        for entry in inventory:
            if entry["path"] in recognized_set:
                continue
            if Path(entry["path"]).name.casefold() in linked_paths:
                continue
            if not any(x.get("path") == entry["path"] for x in unknown):
                unknown.append({"path": entry["path"], "reason": "未能可靠关联到具体消息"})
        report = {
            "package_id": package_id, "source_folder_name": source.name,
            "raw_package_path": self.context.relative(package_root), "raw_package_copied": copied,
            "file_count": len(inventory), "recognized_conversations": recognized,
            "attachment_links": links, "unrecognized": unknown,
            "status": "complete" if not unknown else "needs_identification", "source_unchanged": True,
            "imported_at": _now(),
        }
        _atomic_json(folder / "03_元数据" / f"import-{package_id[:16]}.json", report)
        return report | {"session": self.session(session["session_key"])}

    @staticmethod
    def _inventory(source: Path) -> list[dict]:
        rows = []
        for path in sorted((x for x in source.rglob("*") if x.is_file()), key=lambda x: x.relative_to(source).as_posix().casefold()):
            rows.append({"path": path.relative_to(source).as_posix(), "bytes": path.stat().st_size,
                         "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        return rows
