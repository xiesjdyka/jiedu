"""Conservative read-only collector for persisted Doubao Work trajectories.

The local trajectory is an execution log rather than a guaranteed copy of the
visible chat.  Rows enter the public conversation only after an exact
workspace-local UI calibration; all other plausible rows stay in a review
ledger outside search and conversation rendering.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .archive_store import ArchiveStore, NormalizedAttachment, NormalizedMessage
from .doubao_indexeddb_source import (
    CACHE_PUBLICATION_PROFILE, SnapshotBusyError, UnsupportedDoubaoSchema, cache_signature, consistent_snapshot,
    default_cache_paths, fetch_signed_attachment, read_snapshot, redacted_attachment_reference,
    MAX_ATTACHMENT_BYTES,
)
from .source_connections import SourceConnections
from .workspace import WorkspaceContext, require_active_workspace


PARSER_VERSION = "doubao-trajectory-v1"
CACHE_PARSER_VERSION = "doubao-indexeddb-v2"
CLASSIFIER_VERSION = "trajectory-ui-calibrated-v2"
SYNC_LOCK = threading.Lock()
VALIDATION_SAMPLE_LIMIT = 500
VALIDATION_ATTACHMENT_LIMIT = 1_500
VALIDATION_LEDGER_BYTE_LIMIT = 32 * 1024 * 1024


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path, default):
    try:
        value = json.loads(path.read_text("utf-8"))
        return value
    except (OSError, ValueError):
        return default


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", "utf-8")
    os.replace(temporary, path)


def _atomic_jsonl(path: Path, rows: list[dict]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
                  for row in rows).encode("utf-8")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(raw)
    os.replace(temporary, path)
    return len(raw)


def _strict_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line_no, line in enumerate(path.read_text("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise ValueError(f"验证样本账本损坏：{path.name} 第 {line_no} 行") from exc
        if not isinstance(row, dict):
            raise ValueError(f"验证样本账本格式无效：{path.name} 第 {line_no} 行")
        rows.append(row)
    return rows


def _default_root() -> Path:
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return local / "Doubao" / "User Data" / "Default" / ".doubao" / "agent_mode" / "workspace" / ".sessions"


class DoubaoSessionAdapter:
    """Incrementally copy the supported, persisted public subset into ArchiveStore."""

    def __init__(self, context: WorkspaceContext | None = None, *, source_root: Path | None = None,
                 source_instance: str | None = None, title_overrides: dict[str, str] | None = None,
                 identity_root: Path | None = None, cache_leveldb: Path | None = None,
                 cache_blob: Path | None = None, cache_wrapper_factory=None):
        self.context = context or require_active_workspace()
        self.source_root = (source_root or _default_root()).expanduser().resolve()
        self.source_instance = source_instance
        self.identity_root = identity_root
        default_leveldb, default_blob = default_cache_paths()
        # A custom trajectory fixture must never read the operator's real cache.
        self.cache_leveldb = cache_leveldb or (None if source_root is not None else default_leveldb)
        self.cache_blob = cache_blob or (None if source_root is not None else default_blob)
        self.cache_wrapper_factory = cache_wrapper_factory
        self.cache_snapshot = self.context.sync / "doubao-indexeddb-snapshot"
        self.cache_candidate = self.context.sync / "doubao-indexeddb-candidate"
        self.cache_validation_root = self.context.sync / "doubao-indexeddb-validation"
        self.cache_validation_messages = self.cache_validation_root / "messages.jsonl"
        self.cache_validation_attachments = self.cache_validation_root / "attachments.jsonl"
        self.cache_validation_manifest = self.cache_validation_root / "manifest.json"
        self.calibration_path = self.context.state / "doubao-work-calibration.json"
        self._explicit_titles = dict(title_overrides or {})
        self.state_path = self.context.sync / "doubao-work-adapter.json"
        self.store = ArchiveStore(self.context, recover=False)
        self._lock = SYNC_LOCK

    def _state(self) -> dict:
        value = _read_json(self.state_path, {})
        return {
            "adapter": "doubao_work",
            "parser_version": PARSER_VERSION,
            "streams": value.get("streams", {}) if isinstance(value.get("streams"), dict) else {},
            "last_sync_at": value.get("last_sync_at"),
            "last_error": value.get("last_error"),
            "calibration_sha256": value.get("calibration_sha256"),
            "status_snapshot": value.get("status_snapshot") if isinstance(value.get("status_snapshot"), dict) else None,
            "cache": value.get("cache", {}) if isinstance(value.get("cache"), dict) else {},
        }

    def _save(self, state: dict) -> None:
        state["updated_at"] = _now()
        _atomic_json(self.state_path, state)

    def _calibration(self) -> dict:
        value = _read_json(self.calibration_path, {})
        titles = value.get("title_overrides", {}) if isinstance(value.get("title_overrides"), dict) else {}
        visible = value.get("visible_messages", {}) if isinstance(value.get("visible_messages"), dict) else {}
        identity_matches = value.get("identity_matches", {}) if isinstance(value.get("identity_matches"), dict) else {}
        return {"format_version": 1, "title_overrides": titles | self._explicit_titles,
                "visible_messages": visible, "identity_matches": identity_matches}

    def _calibration_digest(self) -> str:
        value = self._calibration()
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                         separators=(",", ":")).encode("utf-8")).hexdigest()

    def _bound_instance(self) -> tuple[str | None, list[str], str | None]:
        if self.source_instance:
            return self.source_instance, [self.source_instance], None
        connections = SourceConnections(self.context).load().get("connections", {})
        candidates = sorted(key for key, value in connections.items()
                            if isinstance(value, dict) and value.get("source_key") == "doubao_work")
        try:
            from .client_access import ClientIdentityRegistry, ClientPermissionService
            registry = ClientIdentityRegistry(self.context, self.identity_root)
            active = {row["internal_id"] for row in registry.list_clients() if row.get("status") != "revoked"}
            permissions = ClientPermissionService(registry)
            matches = []
            for key in candidates:
                if key not in active:
                    continue
                policy = next((row.get("decision") for row in permissions.policies(key)
                               if row.get("scope") == "evidence" and row.get("operation") == "raw_archive"), None)
                if policy == "allow":
                    matches.append(key)
        except Exception:
            matches = []
        if len(matches) > 1:
            return None, matches, "multiple_active_doubao_bindings_require_selection"
        if not matches:
            reason = "bound_client_revoked_or_collection_paused" if candidates else "no_doubao_binding"
            return None, matches, reason
        return matches[0], matches, None

    def discover(self) -> list[dict]:
        found = []
        if not self.source_root.is_dir():
            return found
        for session_dir in sorted(self.source_root.iterdir()):
            if not session_dir.is_dir() or not session_dir.name.isdigit():
                continue
            for path in sorted(session_dir.glob("agents/*/system/trajectory.jsonl")):
                try:
                    stat = path.stat()
                    agent_id = path.parents[1].name
                    if not agent_id or not (session_dir / "board.md").is_file():
                        continue
                    found.append({"session_id": session_dir.name, "agent_id": agent_id,
                                  "path": path, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
                except OSError:
                    continue
        return found

    @staticmethod
    def _complete_boundary(raw: bytes) -> int:
        if not raw:
            return 0
        last = raw.rfind(b"\n")
        return last + 1 if last >= 0 else 0

    @staticmethod
    def _attachments(row: dict) -> tuple[NormalizedAttachment, ...]:
        refs = row.get("image_link_list") or []
        if not isinstance(refs, list):
            return ()
        result = []
        for item in refs:
            if isinstance(item, str):
                reference = item
            elif isinstance(item, dict):
                reference = item.get("url") or item.get("path")
            else:
                reference = None
            if not reference:
                continue
            name = Path(str(reference).replace("\\", "/")).name or "图片附件"
            result.append(NormalizedAttachment(reference=str(reference), original_name=name,
                                               original_name_source="trajectory_reference"))
        return tuple(result)

    def _normalized(self, row: dict, session_id: str, agent_id: str, byte_start: int,
                    byte_end: int, line_no: int) -> tuple[NormalizedMessage | None, dict | None]:
        role = str(row.get("role") or "").casefold()
        content = row.get("content")
        if not isinstance(content, str) or not content.strip():
            return None, None
        if role == "user":
            pass
        elif role == "assistant" and not row.get("tool_calls"):
            pass
        else:
            return None, None
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        message_id = f"doubao:{session_id}:{agent_id}:{byte_start}:{digest[:16]}"
        stream_id = f"{session_id}/{agent_id}"
        calibration = self._calibration().get("visible_messages", {}).get(stream_id, [])
        evidence = next((item for item in calibration if isinstance(item, dict)
                         and int(item.get("line") or -1) == line_no
                         and item.get("role") == role
                         and item.get("content_sha256") == digest), None)
        identity_match = next((item for item in self._calibration().get("identity_matches", {}).get(stream_id, [])
                               if isinstance(item, dict) and int(item.get("line") or -1) == line_no
                               and item.get("role") == role and item.get("content_sha256") == digest), None)
        candidate = {"candidate_id": message_id, "role": role, "content": content,
                     "content_sha256": digest, "source_session_id": session_id,
                     "source_agent_id": agent_id, "source_line": line_no,
                     "byte_offset": byte_start, "byte_end": byte_end,
                     "review_status": "ui_calibrated" if evidence else "unverified_trajectory_candidate",
                     "reason": "角色字段与无工具调用不足以证明界面可见性",
                     "calibration_evidence": evidence,
                     "identity_match_evidence": identity_match}
        if not evidence:
            return None, candidate
        return NormalizedMessage(
            message_id=message_id, role=role, content=content,
            attachments=self._attachments(row),
            sync_position={"source_file_identity": f"doubao:{session_id}:{agent_id}",
                           "source_object_id": str(line_no), "byte_offset": byte_start,
                           "byte_end": byte_end},
            provenance={"acquisition": "local_readonly_trajectory", "source_session_id": session_id,
                        "source_agent_id": agent_id, "source_line": line_no,
                        "classifier": CLASSIFIER_VERSION, "original_role": role,
                        "assistant_tool_calls": bool(row.get("tool_calls")),
                        "visibility": "ui_calibrated", "calibration_evidence": evidence,
                        "history_coverage": "partial_visible_history"},
        ), candidate

    def _existing_session(self, instance: str, session_id: str, agent_id: str) -> dict | None:
        for item in self.store._registry().get("sessions", {}).values():
            if (item.get("source_key") == "doubao_work" and item.get("source_instance") == instance
                    and (item.get("source_session_identity") == session_id
                         or item.get("source_session_id") in {session_id, f"{session_id}:{agent_id}"})):
                return item
        return None

    def _ensure_session(self, instance: str, session_id: str, agent_id: str, title: str) -> dict:
        existing = self._existing_session(instance, session_id, agent_id)
        source_id = existing.get("source_session_id") if existing else session_id
        return self.store.ensure_session(source_key="doubao_work", source_session_id=source_id,
            title=title, source_instance=instance, acquisition_method="local_readonly_trajectory")

    def _ensure_cache_session(self, instance: str, conversation_id: str, title: str) -> dict:
        existing = self._existing_session(instance, conversation_id, "indexeddb")
        source_id = existing.get("source_session_id") if existing else conversation_id
        return self.store.ensure_session(source_key="doubao_work", source_session_id=source_id,
            title=title, source_instance=instance, acquisition_method="local_readonly_indexeddb_snapshot")

    def _promote_cache_snapshot(self) -> None:
        previous = self.cache_snapshot.with_name("doubao-indexeddb-snapshot.previous")
        managed = self.context.sync.resolve()
        for path in (previous, self.cache_snapshot, self.cache_candidate):
            if path.resolve().parent != managed:
                raise RuntimeError("缓存快照路径超出工作区管理目录")
        if previous.exists():
            shutil.rmtree(previous)
        if self.cache_snapshot.exists():
            os.replace(self.cache_snapshot, previous)
        os.replace(self.cache_candidate, self.cache_snapshot)
        if previous.exists():
            shutil.rmtree(previous)

    def _capture_cache_validation_samples(self, instance: str, decoded, snapshot_signature: str) -> dict:
        """Persist filtered IndexedDB samples outside archives and searchable layers."""
        message_rows = _strict_jsonl(self.cache_validation_messages)
        attachment_rows = _strict_jsonl(self.cache_validation_attachments)
        existing = {}
        for row in message_rows:
            key = (str(row.get("conversation_id") or ""), str(row.get("message_id") or ""),
                   int(row.get("source_sequence") or 0))
            if not all(key[:2]):
                raise ValueError("验证样本账本缺少稳定消息身份")
            existing[key] = row
        attachment_keys = {
            (str(row.get("sample_id") or ""), int(row.get("attachment_index") or 0),
             str(row.get("reference_sha256") or "")) for row in attachment_rows
        }
        captured_at = _now(); new_messages = []; new_attachments = []
        for message in decoded.messages:
            role = str(message.role or "").casefold()
            if role not in {"user", "assistant"}:
                continue
            if role == "assistant" and not bool(message.completed):
                continue
            if not message.content and not message.attachments:
                continue
            references = []
            for item in message.attachments:
                reference = str(item.get("reference") or "")
                if not reference:
                    continue
                references.append({
                    "block_type": int(item.get("block_type") or 0),
                    "name": str(item.get("name") or Path(reference).name or "附件"),
                    "reference": redacted_attachment_reference(reference),
                    "reference_sha256": hashlib.sha256(reference.encode("utf-8")).hexdigest(),
                })
            identity = (str(message.conversation_id), str(message.message_id), int(message.sequence))
            digest = hashlib.sha256(message.content.encode("utf-8")).hexdigest()
            attachment_fingerprint = hashlib.sha256(json.dumps(references, ensure_ascii=False,
                sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
            sample_id = hashlib.sha256(("\0".join(map(str, identity))).encode("utf-8")).hexdigest()
            row = {
                "format_version": 1, "sample_id": sample_id,
                "source_instance": instance,
                "conversation_id": identity[0], "conversation_title": decoded.titles.get(identity[0]),
                "message_id": identity[1], "source_sequence": identity[2],
                "role": role, "content": message.content, "content_sha256": digest,
                "order": int(message.order), "completed": bool(message.completed),
                "task_id": message.task_id, "attachment_count": len(references),
                "attachment_fingerprint": attachment_fingerprint,
                "captured_at": captured_at, "source_snapshot_signature": snapshot_signature,
                "parser_version": CACHE_PARSER_VERSION,
                "filter_evidence": {key: message.provenance.get(key)
                                    for key in ("cache_stage", "user_type")
                                    if message.provenance.get(key) is not None},
                "review_status": "awaiting_real_ui_sample_validation",
            }
            prior = existing.get(identity)
            if prior:
                if (prior.get("content_sha256") != digest or prior.get("role") != role or
                        prior.get("attachment_fingerprint") != attachment_fingerprint):
                    raise ValueError("同一豆包消息版本的验证样本发生冲突")
            else:
                new_messages.append(row); existing[identity] = row
            for index, item in enumerate(references):
                key = (sample_id, index, item["reference_sha256"])
                if key in attachment_keys:
                    continue
                new_attachments.append({
                    "format_version": 1, "sample_id": sample_id, "attachment_index": index,
                    "conversation_id": identity[0], "message_id": identity[1],
                    "source_sequence": identity[2], **item, "captured_at": captured_at,
                    "source_snapshot_signature": snapshot_signature,
                    "parser_version": CACHE_PARSER_VERSION,
                    "review_status": "awaiting_real_attachment_validation",
                })
                attachment_keys.add(key)
        combined_messages = message_rows + new_messages
        combined_attachments = attachment_rows + new_attachments
        projected = sum(len(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 1
                        for row in combined_messages + combined_attachments)
        full = (len(combined_messages) > VALIDATION_SAMPLE_LIMIT or
                len(combined_attachments) > VALIDATION_ATTACHMENT_LIMIT or
                projected > VALIDATION_LEDGER_BYTE_LIMIT)
        relative = self.context.relative(self.cache_validation_root)
        if full:
            return {
                "ok": False, "status": "validation_capture_full",
                "validation_sample_count": len(message_rows),
                "validation_attachment_count": len(attachment_rows),
                "new_validation_samples": 0,
                "validation_path": relative,
                "validation_capacity": {"messages": VALIDATION_SAMPLE_LIMIT,
                    "attachments": VALIDATION_ATTACHMENT_LIMIT, "bytes": VALIDATION_LEDGER_BYTE_LIMIT},
                "error": "豆包结构化验证区已满；未覆盖已保留样本，未提升新快照",
            }
        if not new_messages and not new_attachments and self.cache_validation_manifest.is_file():
            return {
                "ok": True, "status": "validation_samples_preserved",
                "validation_sample_count": len(combined_messages),
                "validation_attachment_count": len(combined_attachments),
                "new_validation_samples": 0, "new_validation_attachments": 0,
                "validation_path": relative,
                "validation_capacity": {"messages": VALIDATION_SAMPLE_LIMIT,
                    "attachments": VALIDATION_ATTACHMENT_LIMIT, "bytes": VALIDATION_LEDGER_BYTE_LIMIT},
            }
        message_bytes = _atomic_jsonl(self.cache_validation_messages, combined_messages)
        attachment_bytes = _atomic_jsonl(self.cache_validation_attachments, combined_attachments)
        _atomic_json(self.cache_validation_manifest, {
            "format_version": 1, "purpose": "doubao_indexeddb_real_sample_validation_only",
            "searchable": False, "public_archive": False, "candidate_knowledge": False,
            "message_count": len(combined_messages), "attachment_count": len(combined_attachments),
            "message_bytes": message_bytes, "attachment_bytes": attachment_bytes,
            "capacity": {"messages": VALIDATION_SAMPLE_LIMIT,
                "attachments": VALIDATION_ATTACHMENT_LIMIT, "bytes": VALIDATION_LEDGER_BYTE_LIMIT},
            "parser_version": CACHE_PARSER_VERSION, "updated_at": captured_at,
        })
        return {
            "ok": True, "status": "validation_samples_preserved",
            "validation_sample_count": len(combined_messages),
            "validation_attachment_count": len(combined_attachments),
            "new_validation_samples": len(new_messages),
            "new_validation_attachments": len(new_attachments),
            "validation_path": relative,
            "validation_capacity": {"messages": VALIDATION_SAMPLE_LIMIT,
                "attachments": VALIDATION_ATTACHMENT_LIMIT, "bytes": VALIDATION_LEDGER_BYTE_LIMIT},
        }

    def _sync_cache(self, instance: str, state: dict) -> dict:
        if self.cache_leveldb is None or not self.cache_leveldb.is_dir():
            return {"status": "cache_source_not_found", "source_found": False, "saved": 0}
        previous = dict(state.get("cache") or {})
        try:
            observed = hashlib.sha256(repr(cache_signature(self.cache_leveldb,
                self.cache_blob or Path("."))).encode()).hexdigest()
            if (previous.get("signature") == observed
                    and previous.get("parser_version") == CACHE_PARSER_VERSION
                    and previous.get("publication_profile") == CACHE_PUBLICATION_PROFILE
                    and previous.get("validation_samples_preserved") is True
                    and previous.get("status") == "structured_cache_detected_awaiting_body_validation"):
                return previous | {"saved": 0, "unchanged": True}
            copied = consistent_snapshot(self.cache_leveldb, self.cache_blob or Path("."),
                                         self.cache_candidate, attempts=3)
            decoded = read_snapshot(self.cache_candidate, wrapper_factory=self.cache_wrapper_factory)
            if decoded.current_decode_errors:
                return {"status": "cache_current_value_decode_failed", "source_found": True,
                        "saved": 0, "cursor_preserved": True,
                        "current_decode_errors": decoded.current_decode_errors,
                        "ignored_historical_decode_errors": decoded.ignored_historical_decode_errors,
                        "error": "当前有效缓存值无法解码，已保留上一份快照和游标"}
        except SnapshotBusyError as exc:
            return {"status": "cache_busy_retry_later", "source_found": True, "saved": 0,
                    "cursor_preserved": True, "error": str(exc)}
        except UnsupportedDoubaoSchema as exc:
            return {"status": "cache_schema_changed_paused", "source_found": True, "saved": 0,
                    "cursor_preserved": True, "error": str(exc)}
        except Exception as exc:
            return {"status": "cache_decode_failed_preserved", "source_found": True, "saved": 0,
                    "cursor_preserved": True, "error": f"{type(exc).__name__}: {exc}"}

        try:
            validation = self._capture_cache_validation_samples(instance, decoded, copied["signature"])
        except Exception as exc:
            return {"status": "validation_capture_failed", "source_found": True, "saved": 0,
                    "cursor_preserved": True, "validation_samples_preserved": False,
                    "validation_path": self.context.relative(self.cache_validation_root),
                    "error": f"{type(exc).__name__}: {exc}"}
        if not validation["ok"]:
            return {"source_found": True, "saved": 0, "cursor_preserved": True,
                    "validation_samples_preserved": False, **validation}

        saved = 0; touched = set()
        publishable = decoded.messages if decoded.publication_ready else ()
        for message in publishable:
            title = decoded.titles.get(message.conversation_id) or f"豆包对话 {message.conversation_id}"
            session = self._ensure_cache_session(instance, message.conversation_id, title)
            attachment_specs = []
            for item in message.attachments:
                reference = str(item.get("reference") or "")
                if not reference:
                    continue
                name = str(item.get("name") or Path(reference).name or "附件")
                portable_reference = redacted_attachment_reference(reference)
                path = Path(reference)
                if path.is_absolute() and path.is_file() and path.stat().st_size <= MAX_ATTACHMENT_BYTES:
                    attachment_specs.append(NormalizedAttachment(reference=portable_reference, original_name=name,
                        original_name_source="indexeddb_local_attachment", local_path=path))
                    continue
                if reference.startswith("https://"):
                    try:
                        raw, mime, remote_name = fetch_signed_attachment(reference)
                        attachment_specs.append(NormalizedAttachment(reference=portable_reference,
                            original_name=name or remote_name,
                            original_name_source="indexeddb_signed_attachment",
                            mime_type=mime, inline_data=raw))
                        continue
                    except (OSError, ValueError):
                        pass
                attachment_specs.append(NormalizedAttachment(reference=portable_reference, original_name=name,
                    original_name_source="indexeddb_message_reference"))
            attachments = tuple(attachment_specs)
            base_message_id = f"doubao-cache:{message.conversation_id}:{message.message_id}"
            raw_path = self.context.raw_message_store / f"{session['session_key']}.jsonl"
            existing = [row for row in self.store._message_rows(raw_path)
                        if (row.get("provenance") or {}).get("source_message_id") == message.message_id]
            digest = hashlib.sha256(message.content.encode("utf-8")).hexdigest()
            same = next((row for row in existing if row.get("content_sha256") == digest
                         and row.get("role") == message.role), None)
            archive_message_id = same.get("message_id") if same else (
                base_message_id if not existing else f"{base_message_id}:v{message.sequence}")
            normalized = NormalizedMessage(
                message_id=archive_message_id,
                role=message.role, content=message.content, attachments=attachments,
                sync_position={"source_file_identity": "doubao-indexeddb",
                               "source_object_id": message.message_id,
                               "byte_offset": message.order},
                provenance={"acquisition": "local_readonly_indexeddb_snapshot",
                    "source_conversation_id": message.conversation_id,
                    "source_message_id": message.message_id, "source_sequence": message.sequence,
                    "source_task_id": message.task_id, "completion_verified": message.completed,
                    "visibility": "structured_public_content_block",
                    "parser_version": CACHE_PARSER_VERSION, **message.provenance},
            )
            result = self.store.append_normalized_message(session["session_key"], normalized, materialize=False)
            latest_path = self.context.root / session["relative_path"] / "03_元数据" / "message-latest.json"
            latest_state = _read_json(latest_path, {"format_version": 1, "latest_by_source_message": {}})
            latest_map = latest_state.setdefault("latest_by_source_message", {})
            prior = latest_map.get(message.message_id) if isinstance(latest_map.get(message.message_id), dict) else {}
            if int(message.sequence) >= int(prior.get("source_sequence") or -1):
                latest_map[message.message_id] = {"archive_message_id": archive_message_id,
                    "source_sequence": message.sequence, "content_sha256": digest,
                    "updated_at": _now()}
                _atomic_json(latest_path, latest_state)
            saved += int(result["saved"]); touched.add(session["session_key"])
        for session_key in touched:
            self.store.update_session(session_key, sync_status="synced",
                sync_position={"indexeddb_sequence": decoded.max_sequence}, metadata={
                    "history_coverage": "ephemeral_structured_cache_partial",
                    "assistant_tail_coverage": "only_explicitly_completed_tasks",
                    "source_format": CACHE_PARSER_VERSION,
                    "cache_decode_error_count": decoded.decode_errors,
                    "completeness": "已归档结构化缓存中可验证的公开消息；被源端清理的历史值不猜测补全",
                })
            self.store.materialize_session(session_key)
        try:
            self._promote_cache_snapshot()
        except Exception as exc:
            return {**validation, "status": "cache_snapshot_promotion_failed", "source_found": True,
                    "saved": saved, "cursor_preserved": True,
                    "validation_samples_preserved": True,
                    "error": f"{type(exc).__name__}: {exc}"}
        validation_state = {"validation_status": validation.get("status"),
                            **{key: value for key, value in validation.items()
                               if key not in {"ok", "status"}}}
        current = {"status": "structured_cache_detected_awaiting_body_validation", "source_found": True, "saved": saved,
                   "message_count": len(decoded.messages), "conversation_count": len(decoded.titles),
                   "max_sequence": decoded.max_sequence, "decode_errors": decoded.decode_errors,
                   "current_decode_errors": decoded.current_decode_errors,
                   "ignored_historical_decode_errors": decoded.ignored_historical_decode_errors,
                   "current_state_verified": decoded.current_state_verified,
                   "publication_ready": decoded.publication_ready,
                   "publication_profile": CACHE_PUBLICATION_PROFILE,
                   "task_records": decoded.task_records, "schemas": list(decoded.schemas),
                   "signature": copied["signature"], "snapshot_attempts": copied["attempts"],
                   "parser_version": CACHE_PARSER_VERSION, "last_sync_at": _now(),
                   "validation_samples_preserved": True,
                   **validation_state}
        state["cache"] = current
        return current

    def _write_review_candidates(self, session: dict, candidates: list[dict]) -> None:
        path = self.context.root / session["relative_path"] / "03_元数据" / "visibility-review.jsonl"
        existing = {}
        if path.is_file():
            for line in path.read_text("utf-8").splitlines():
                try:
                    row = json.loads(line)
                    if row.get("candidate_id"):
                        existing[row["candidate_id"]] = row
                except ValueError:
                    continue
        for row in candidates:
            existing[row["candidate_id"]] = row
        _atomic_json(path.with_suffix(".json"), {"candidates": list(existing.values()),
            "notice": "未经逐条界面校准，不进入公开对话或搜索"})
        temporary = path.with_suffix(".tmp")
        temporary.write_text("".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
                                     for row in existing.values()), "utf-8")
        os.replace(temporary, path)

    def _evidence_for(self, session_id: str, agent_id: str, line_no: int,
                      role: str, content_sha256: str) -> dict | None:
        rows = self._calibration().get("visible_messages", {}).get(f"{session_id}/{agent_id}", [])
        return next((item for item in rows if isinstance(item, dict)
                     and int(item.get("line") or -1) == int(line_no)
                     and item.get("role") == role
                     and item.get("content_sha256") == content_sha256), None)

    def _refresh_review_evidence(self, session: dict) -> None:
        path=self.context.root/session["relative_path"]/"03_元数据"/"visibility-review.jsonl"
        if not path.is_file():
            return
        rows=[];changed=False
        for line in path.read_text("utf-8").splitlines():
            if not line.strip():
                continue
            row=json.loads(line);stream=f"{row.get('source_session_id','')}/{row.get('source_agent_id','')}"
            match=next((item for item in self._calibration().get("identity_matches",{}).get(stream,[])
                        if isinstance(item,dict) and int(item.get("line") or -1)==int(row.get("source_line") or -2)
                        and item.get("role")==row.get("role") and item.get("content_sha256")==row.get("content_sha256")),None)
            if row.get("identity_match_evidence") != match:
                row["identity_match_evidence"]=match;changed=True
            rows.append(row)
        if changed:
            temporary=path.with_suffix(".tmp")
            temporary.write_text("".join(json.dumps(row,ensure_ascii=False,separators=(",", ":"))+"\n" for row in rows),"utf-8")
            os.replace(temporary,path)

    def _reconcile_existing_visibility(self) -> dict:
        """Move previously over-broad public rows into a recoverable review ledger."""
        migrated = public = 0
        for session in list(self.store._registry().get("sessions", {}).values()):
            if session.get("source_key") != "doubao_work":
                continue
            identity=str(session.get("source_session_identity") or session.get("source_session_id") or "").split(":",1)[0]
            calibrated_title=self._calibration().get("title_overrides",{}).get(identity)
            if calibrated_title:
                session=self.store.ensure_session(source_key="doubao_work",
                    source_session_id=session["source_session_id"],title=calibrated_title,
                    source_instance=session["source_instance"],acquisition_method="local_readonly_trajectory")
            self._refresh_review_evidence(session)
            raw_path = self.context.raw_message_store / f"{session['session_key']}.jsonl"
            rows = self.store._message_rows(raw_path)
            if not rows:
                continue
            keep=[]; review=[]
            for row in rows:
                provenance = row.get("provenance") or {}
                session_id = str(provenance.get("source_session_id") or session.get("source_session_identity") or "")
                agent_id = str(provenance.get("source_agent_id") or session.get("source_agent_identity") or "")
                line_no = int(provenance.get("source_line") or 0)
                role = str(row.get("role") or "")
                digest = str(row.get("content_sha256") or hashlib.sha256(str(row.get("original_text") or "").encode()).hexdigest())
                evidence = self._evidence_for(session_id, agent_id, line_no, role, digest)
                if evidence:
                    row["provenance"] = provenance | {"classifier": CLASSIFIER_VERSION,
                        "visibility": "ui_calibrated", "calibration_evidence": evidence}
                    keep.append(row); public += 1
                else:
                    identity_match=next((item for item in self._calibration().get("identity_matches",{}).get(f"{session_id}/{agent_id}",[])
                                         if isinstance(item,dict) and int(item.get("line") or -1)==line_no
                                         and item.get("role")==role and item.get("content_sha256")==digest),None)
                    review.append({"candidate_id": row.get("message_id"), "role": role,
                        "content": row.get("original_text") or "", "content_sha256": digest,
                        "source_session_id": session_id, "source_agent_id": agent_id,
                        "source_line": line_no,
                        "byte_offset": (row.get("source_position") or {}).get("byte_offset"),
                        "byte_end": (row.get("source_position") or {}).get("byte_end"),
                        "review_status": "unverified_trajectory_candidate",
                        "reason": "旧版仅按 role/tool_calls 归档，未获得逐条界面可见性证据",
                        "calibration_evidence": None,"identity_match_evidence":identity_match})
            if review:
                self._write_review_candidates(session, review); migrated += len(review)
            temporary=raw_path.with_suffix(".tmp")
            temporary.write_text("".join(json.dumps(row,ensure_ascii=False,separators=(",", ":"))+"\n" for row in keep),"utf-8")
            os.replace(temporary,raw_path)
            evidence_paths=sorted({str((row.get("provenance") or {}).get("calibration_evidence",{}).get("evidence_reference"))
                                   for row in keep if (row.get("provenance") or {}).get("calibration_evidence",{}).get("evidence_reference")})
            self.store.update_session(session["session_key"],sync_status="awaiting_visibility_calibration",
                metadata={"message_count":len(keep),"public_verified_message_count":len(keep),
                          "unverified_visibility_candidate_count":len(review),
                          "related_evidence_paths":evidence_paths,
                          "public_message_classifier":CLASSIFIER_VERSION,
                          "visibility_policy":"exact_workspace_ui_calibration_only",
                          "completeness":"仅逐条界面校准的消息进入公开对话；其余轨迹候选已隔离待核对"})
            self.store.materialize_session(session["session_key"])
        return {"quarantined":migrated,"public_verified":public}

    def _sync_stream(self, source: dict, instance: str, state: dict) -> dict:
        session_id, agent_id, path = source["session_id"], source["agent_id"], source["path"]
        stream_id = f"{session_id}/{agent_id}"
        previous = dict(state["streams"].get(stream_id) or {})
        if (previous.get("source_size") == source["size"]
                and previous.get("source_mtime_ns") == source["mtime_ns"]
                and previous.get("parser_version") == PARSER_VERSION
                and state.get("calibration_sha256") == self._calibration_digest()):
            return {"stream_id": stream_id, "saved": 0, "changed": False, **previous}
        raw = path.read_bytes(); boundary = self._complete_boundary(raw); complete = raw[:boundary]
        confirmed = int(previous.get("confirmed_offset") or 0)
        if confirmed > boundary or (confirmed and previous.get("confirmed_prefix_sha256") !=
                                    hashlib.sha256(complete[:confirmed]).hexdigest()):
            previous.update({"status": "source_changed_requires_review", "last_error": "源记录已截断或前缀发生变化，已暂停续传",
                             "pending_partial_bytes": len(raw) - boundary, "observed_size": len(raw)})
            state["streams"][stream_id] = previous
            return {"stream_id": stream_id, "saved": 0, "changed": True, **previous}

        offset = confirmed; line_no = complete[:confirmed].count(b"\n")
        saved = included = unverified = excluded_tools = excluded_other = invalid = 0
        calibration = self._calibration()
        title = calibration.get("title_overrides", {}).get(session_id) or f"豆包工作 {session_id}"
        title_source = "workspace_calibrated_title" if session_id in calibration.get("title_overrides", {}) else "generated_source_title_unavailable"
        session = None
        if previous.get("archive_session_key"):
            session = self._ensure_session(instance, session_id, agent_id, title)
        review_candidates = []
        while offset < boundary:
            end = complete.find(b"\n", offset) + 1
            chunk = complete[offset:end]; line_no += 1
            try:
                row = json.loads(chunk.decode("utf-8-sig" if offset == 0 else "utf-8"))
            except (UnicodeError, ValueError):
                invalid = len(chunk); line_no -= 1
                break
            if not isinstance(row, dict):
                invalid = len(chunk); line_no -= 1
                break
            role = str(row.get("role") or "").casefold()
            message, candidate = self._normalized(row, session_id, agent_id, offset, end, line_no)
            if message:
                if session is None:
                    session = self._ensure_session(instance, session_id, agent_id, title)
                result = self.store.append_normalized_message(session["session_key"], message, materialize=False)
                saved += int(result["saved"]); included += 1
            elif candidate:
                if session is None:
                    session = self._ensure_session(instance, session_id, agent_id, title)
                review_candidates.append(candidate); unverified += 1
            elif role == "tool" or (role == "assistant" and row.get("tool_calls")):
                excluded_tools += 1
            else:
                excluded_other += 1
            offset = end

        status = "partial_line_pending" if boundary < len(raw) else "caught_up"
        if invalid:
            status = "invalid_json_pending"
        if session:
            if review_candidates:
                self._write_review_candidates(session, review_candidates)
            existing_review = self.context.root / session["relative_path"] / "03_元数据" / "visibility-review.jsonl"
            review_count = sum(1 for line in existing_review.read_text("utf-8").splitlines() if line.strip()) if existing_review.is_file() else 0
            metadata = {
                "title_source": title_source,
                "history_coverage": "partial_visible_history",
                "assistant_tail_coverage": "unverified_or_missing",
                "public_message_classifier": CLASSIFIER_VERSION,
                "completeness": "仅逐条界面校准的消息进入公开对话；其余轨迹候选隔离待核对；公开助手尾部可能缺失",
                "source_format": PARSER_VERSION,
                "source_session_identity": session_id,
                "source_agent_identity": agent_id,
                "agent_streams": sorted(set((self.store.session(session["session_key"]).get("agent_streams") or []) + [agent_id])),
                "cross_agent_ordering": "unknown_requires_ui_calibration_before_public_merge",
                "unverified_visibility_candidate_count": review_count,
                "visibility_policy": "exact_workspace_ui_calibration_only",
                "excluded_tool_or_process_rows": int(previous.get("excluded_tool_or_process_rows") or 0) + excluded_tools,
                "excluded_other_rows": int(previous.get("excluded_other_rows") or 0) + excluded_other,
                "pending_partial_bytes": len(raw) - boundary,
            }
            self.store.update_session(session["session_key"], sync_status=status,
                sync_position={"confirmed_offset": offset, "source_file_identity": f"doubao:{session_id}:{agent_id}"},
                metadata=metadata)
            self.store.materialize_session(session["session_key"])

        current = {
            "session_id": session_id, "agent_id": agent_id, "status": status,
            "confirmed_offset": offset,
            "confirmed_prefix_sha256": hashlib.sha256(complete[:offset]).hexdigest(),
            "line_count": line_no, "pending_partial_bytes": len(raw) - boundary,
            "invalid_json_bytes": invalid, "last_saved_messages": saved,
            "archive_session_key": session["session_key"] if session else previous.get("archive_session_key"),
            "excluded_tool_or_process_rows": int(previous.get("excluded_tool_or_process_rows") or 0) + excluded_tools,
            "excluded_other_rows": int(previous.get("excluded_other_rows") or 0) + excluded_other,
            "source_size": source["size"], "source_mtime_ns": source["mtime_ns"],
            "parser_version": PARSER_VERSION, "unverified_rows": unverified,
            "title_source": title_source, "last_sync_at": _now(), "last_error": None,
        }
        state["streams"][stream_id] = current
        return {"stream_id": stream_id, "saved": saved, "included_rows": included,
                "changed": True, **current}

    def sync_once(self) -> dict:
        with self._lock:
            state = self._state(); instance, matches, binding_reason = self._bound_instance(); discovered = self.discover()
            if not instance:
                result = {"adapter": "doubao_work", "status": binding_reason or ("supported_source_found_unbound" if discovered else "source_not_found"),
                          "source_found": bool(discovered), "discovered_stream_count": len(discovered),
                          "bound_client_count": len(matches), "saved_messages": 0,
                          "limitations": ["需要唯一且未撤销、归档权限为允许的豆包工作版连接"]}
                if state.get("status_snapshot") != result:
                    state["status_snapshot"] = result; state["last_error"] = None; self._save(state)
                return result
            calibration_digest=self._calibration_digest(); migration={"quarantined":0,"public_verified":0}
            if state.get("calibration_sha256") != calibration_digest:
                migration=self._reconcile_existing_visibility()
                state["calibration_sha256"]=calibration_digest
            rows=[]; saved=0; error=None
            cache = self._sync_cache(instance, state)
            saved += int(cache.get("saved") or 0)
            error = cache.get("error")
            for source in discovered:
                try:
                    row=self._sync_stream(source, instance, state); rows.append(row); saved += int(row.get("saved") or 0)
                except Exception as exc:
                    error=str(exc); rows.append({"stream_id": f"{source['session_id']}/{source['agent_id']}", "status": "failed", "saved": 0, "error": error})
            source_found = bool(discovered) or bool(cache.get("source_found"))
            cache_active = cache.get("status") == "structured_cache_detected_awaiting_body_validation"
            cache_attention = cache.get("status") in {"validation_capture_failed", "validation_capture_full",
                "cache_snapshot_promotion_failed", "cache_current_value_decode_failed",
                "cache_schema_changed_paused", "cache_decode_failed_preserved"}
            result = {"adapter": "doubao_work", "status": cache.get("status") if cache_attention else ("structured_cache_detected_awaiting_body_validation" if cache_active else ("candidate_collection_active_public_visibility_limited" if discovered else cache.get("status", "source_not_found"))),
                      "source_found": source_found, "discovered_stream_count": len(discovered),
                      "bound_client_count": len(matches), "saved_messages": saved, "streams": rows,
                      "structured_cache": cache,
                      "last_sync_at": _now(), "last_error": error,
                      "history_coverage": "ephemeral_structured_cache_partial" if cache_active else "partial_visible_history", "visibility_migration": migration,
                      "limitations": ["结构化缓存会被豆包清理，已缺失的历史不猜测补全", "助手消息仅在明确完成标记后公开", "工具、思考与内部过程已过滤", "附件暂保留引用，未验证文件不自动下载"]}
            if (rows and all(not row.get("changed", True) for row in rows) and not error
                    and not any(migration.values())
                    and cache.get("unchanged")
                    and (state.get("status_snapshot") or {}).get("status") == result["status"]):
                return result
            state["last_sync_at"] = result["last_sync_at"]; state["last_error"] = error
            state["status_snapshot"] = result; self._save(state); return result

    def status(self) -> dict:
        state = self._state(); snapshot = state.get("status_snapshot")
        public_count = 0; review_count = 0
        for session in self.store._registry().get("sessions", {}).values():
            if session.get("source_key") != "doubao_work":
                continue
            public_count += int(session.get("message_count") or 0)
            review_path = self.context.root / session["relative_path"] / "03_元数据" / "visibility-review.jsonl"
            if review_path.is_file():
                review_count += sum(1 for line in review_path.read_text("utf-8").splitlines() if line.strip())
        coverage = {"public_message_count": public_count, "review_message_count": review_count}
        if snapshot:
            return snapshot | coverage | {"snapshot_updated_at": _read_json(self.state_path, {}).get("updated_at")}
        instance, matches, reason = self._bound_instance(); discovered = self.discover()
        return {"adapter": "doubao_work", "status": "ready" if instance else (reason or "supported_source_found_unbound"),
                "source_found": bool(discovered), "discovered_stream_count": len(discovered),
                "bound_client_count": len(matches), "saved_messages": 0,
                "history_coverage": "partial_visible_history", **coverage}


_DEFAULT: DoubaoSessionAdapter | None = None


def _adapter() -> DoubaoSessionAdapter:
    global _DEFAULT
    context = require_active_workspace()
    if _DEFAULT is None or _DEFAULT.context.workspace_id != context.workspace_id:
        _DEFAULT = DoubaoSessionAdapter(context)
    return _DEFAULT


def status() -> dict:
    return _adapter().status()


def run(interval_seconds: float = 5.0, stop_event: threading.Event | None = None) -> None:
    stopper = stop_event or threading.Event()
    wake = threading.Event(); observer = None
    try:
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
            leveldb, blob = default_cache_paths()
            class Handler(FileSystemEventHandler):
                def on_any_event(self, event):
                    if not getattr(event, "is_directory", False):
                        wake.set()
            watched = [path for path in (leveldb, blob) if path.is_dir()]
            if watched:
                observer = Observer()
                handler = Handler()
                for path in watched:
                    observer.schedule(handler, str(path), recursive=True)
                observer.start()
        except Exception:
            observer = None
        while not stopper.is_set():
            try:
                _adapter().sync_once()
            except Exception:
                pass
            deadline = time.monotonic() + max(30.0, interval_seconds)
            while not stopper.is_set() and not wake.wait(0.5):
                if time.monotonic() >= deadline:
                    break
            if wake.is_set():
                wake.clear(); stopper.wait(0.35)  # coalesce one IndexedDB write burst
    finally:
        if observer is not None:
            observer.stop(); observer.join(timeout=2)
