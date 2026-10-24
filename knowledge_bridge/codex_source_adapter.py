"""Read-only, versioned adapter for Codex local sessions.

Codex-private event shapes stop at this module. ArchiveStore receives only
NormalizedMessage and NormalizedAttachment objects.
"""
from __future__ import annotations

import hashlib
import base64
import json
import mimetypes
import os
import re
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from filelock import FileLock

from .archive_store import ArchiveStore, NormalizedAttachment, NormalizedMessage
from .redaction import redact_text
from .workspace import WorkspaceContext, local_path, require_active_workspace
from .durable_state import read_object, write_object, StateCorruptError


PARSER_VERSION = "codex-jsonl-v2.2"
VISIBILITY_POLICY_VERSION = 2
REVIEW_POLICY_VERSION = 2
ARCHIVE_POLICIES = {"selected", "all", "paused"}
DEFAULT_BACKGROUND_BATCH_SIZE = 6
DEFAULT_MANUAL_BATCH_SIZE = 8
VISIBLE_ASSISTANT_PHASES = {"commentary", "final_answer", "final"}
ATTACHMENT_TYPES = {"input_image", "input_file", "image", "file", "local_image", "attachment"}
KNOWN_RESPONSE_INTERNAL = {"reasoning", "custom_tool_call", "custom_tool_call_output", "function_call", "function_call_output"}
KNOWN_TOP_LEVEL_INTERNAL = {"session_meta", "event_msg", "world_state", "turn_context", "compacted"}
INTERNAL_USER_ENVELOPES = ("recommended_plugins", "environment_context", "app-context")
INTERNAL_USER_PREFIXES = (
    "The following is the Codex agent history",
)
SYNC_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, value: dict) -> None:
    write_object(path, value, backup=path.name in {'codex-source-adapter.json', 'instance.json'})


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _fingerprint(raw: bytes) -> str:
    return hashlib.sha256(raw.rstrip(b"\r\n")).hexdigest()


def _default_codex_home() -> Path:
    override = os.environ.get('KNOWLEDGE_BRIDGE_CODEX_HOME')
    if override:
        return Path(override).expanduser().resolve()
    candidates = [Path(os.environ['CODEX_HOME'])] if os.environ.get('CODEX_HOME') else []
    candidates.append(Path.home() / '.codex')
    for path in candidates:
        root = path.expanduser().resolve()
        if any(root.glob('thread_history_*.sqlite')) or any(
                (root / folder).is_dir() and next((root / folder).rglob('rollout-*.jsonl'), None)
                for folder in ('sessions', 'archived_sessions')):
            return root
    return candidates[0].expanduser().resolve()


@dataclass(frozen=True)
class DiscoveredSession:
    session_id: str
    path: Path
    relative_path: str
    meta_fingerprint: str
    title: str
    title_source: str
    size: int
    mtime_ns: int
    object_id: str
    fragments: tuple = ()


class CodexSourceAdapter:
    """Discovers and incrementally syncs Codex sessions under workspace policy."""

    parser_version = PARSER_VERSION
    max_event_bytes = 8 * 1024 * 1024
    stream_page_bytes = 8 * 1024 * 1024
    page_bytes = 1024 * 1024
    page_events = 2048
    processing_budget_seconds = .35
    read_chunk_bytes = 64 * 1024

    def __init__(self, context: WorkspaceContext | None = None, *,
                 codex_home: Path | None = None, machine_data: Path | None = None,
                 status_only: bool = False):
        self.context = context or require_active_workspace()
        self.codex_home = (codex_home or _default_codex_home()).expanduser().resolve()
        self.machine_data = (machine_data or local_path("codex-adapter")).expanduser().resolve()
        self.state_path = self.context.sync / "codex-source-adapter.json"
        self.audit_path = self.context.audit / "codex-sync.jsonl"
        self.review_path = self.context.queues / "codex-needs-review.jsonl"
        self.locator_path = self.machine_data / "attachment-locators.json"
        self.instance_path = self.machine_data / "instance.json"
        self.store = ArchiveStore(self.context,recover=not status_only)
        self._lock = SYNC_LOCK
        self._process_lock = FileLock(str(self.context.sync / 'codex-source-adapter.lock'))
        self._work_cursor = None
        self._read_target = None
        self.discovery_issues = []

    def _state(self) -> dict:
        value = read_object(self.state_path)
        if self.state_path.exists() and (not isinstance(value.get('sessions'), dict)
                or (value.get('archive_policy') not in ARCHIVE_POLICIES and 'enabled' not in value)):
            raise StateCorruptError('Codex 归档状态结构损坏；已停止采集，原进度不得清空')
        legacy_selected = list(dict.fromkeys(value.get("authorized_session_ids", [])))
        policy = value.get("archive_policy")
        if policy not in ARCHIVE_POLICIES:
            policy = "selected" if value.get("enabled") else "paused"
        resume_policy = value.get("resume_policy")
        if resume_policy not in {"selected", "all"}:
            resume_policy = "selected"
        return {
            "adapter": "codex", "parser_version": PARSER_VERSION,
            "archive_policy": policy, "resume_policy": resume_policy,
            "selected_session_ids": list(dict.fromkeys(value.get("selected_session_ids", legacy_selected))),
            "excluded_session_ids": list(dict.fromkeys(value.get("excluded_session_ids", []))),
            "sessions": value.get("sessions", {}) if isinstance(value.get("sessions", {}), dict) else {},
            "last_sync_at": value.get("last_sync_at"), "last_sync_message_count": value.get("last_sync_message_count", 0),
            "last_message_sync_at": value.get("last_message_sync_at"),
            "last_error": value.get("last_error"), "updated_at": value.get("updated_at"),
            "status_snapshot": value.get("status_snapshot") if isinstance(value.get("status_snapshot"), dict) else None,
            "history_batch": value.get("history_batch"),
            "scheduler": value.get("scheduler", {"turn": 0, "attempt": 0}),
            "live_progress": value.get("live_progress", {"saved_messages": 0, "processed_bytes": 0}),
            "discovery_issues": value.get('discovery_issues', []),
        }

    def _save_state(self, state: dict) -> None:
        state.pop("enabled", None)
        state.pop("authorized_session_ids", None)
        old = read_object(self.state_path)
        if {k:v for k,v in old.items() if k != 'updated_at'} == {k:v for k,v in state.items() if k != 'updated_at'}:
            return
        state["updated_at"] = _now(); _atomic_json(self.state_path, state)

    def enable_for_first_pairing(self) -> bool:
        """Start local history and live collection only for a new Codex source.

        Any existing state, including an explicit pause or selected scope, belongs
        to the owner and must survive a later pairing or repair unchanged.
        """
        with self._lock, self._process_lock.acquire(timeout=30):
            if self.state_path.exists():
                return False
            state = self._state()
            state["archive_policy"] = "all"
            state["resume_policy"] = "all"
            self._save_state(state)
            return True

    def _instance_id(self) -> str:
        value = read_object(self.instance_path)
        if self.instance_path.exists() and not value.get('instance_id'):
            raise StateCorruptError('Codex 来源身份损坏；已停止采集，不能创建替代身份')
        if not value.get("instance_id"):
            value = {"instance_id": str(uuid.uuid4()), "created_at": _now()}
            _atomic_json(self.instance_path, value)
        return str(value["instance_id"])

    @staticmethod
    def _is_placeholder_title(title: str, session_id: str) -> bool:
        value = title.strip()
        return bool(re.fullmatch(rf"Codex\s*会话\s*{re.escape(session_id[:8])}", value, re.IGNORECASE))

    def _titles(self) -> dict[str, str]:
        result = {}
        index = self.codex_home / "session_index.jsonl"
        try:
            with index.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(row, dict):
                        continue
                    session_id = row.get("id")
                    title = row.get("thread_name") or row.get("title")
                    if session_id and isinstance(title, str) and title.strip():
                        result[str(session_id)] = title.strip()
        except OSError:
            pass
        return result

    def _existing_titles(self) -> dict[str, tuple[str, str]]:
        result = {}
        for item in self.store._registry().get("sessions", {}).values():
            if item.get("source_key") == "codex" and item.get("source_session_id") and item.get("title"):
                title_source = str(item.get("title_source") or "existing_manifest")
                if title_source in {"derived_first_user_message", "fallback_timestamp"}:
                    continue
                result[str(item["source_session_id"])] = (
                    str(item["title"]), title_source
                )
        return result

    @staticmethod
    def _clean_derived_title(text: str) -> str | None:
        for raw_line in text.splitlines():
            line = raw_line.strip().lstrip("#>*- ").strip()
            if not line or line.startswith("<"):
                continue
            lowered = line.casefold()
            if any(token in lowered for token in (
                    "environment_context", "app-context", "recommended_plugins", "turn_context",
                    "sandbox", "permission_profile", "cwd=", "api_key", "token=", "secret=")):
                continue
            if re.fullmatch(r"(?:[a-zA-Z]:[\\/]|/|file:).+", line):
                continue
            line = re.sub(r"\s+", " ", line).strip()
            if line:
                return line[:100].rstrip(". ") or None
        return None

    def _derived_title(self, path: Path, session_id: str, timestamp: str | None) -> tuple[str, str]:
        try:
            with path.open("rb") as handle:
                handle.readline(self.max_event_bytes + 1)
                for _ in range(2048):
                    if handle.tell() >= self.page_bytes:
                        break
                    raw = handle.readline(self.page_bytes + 1)
                    if not raw or not raw.endswith(b"\n"):
                        break
                    try:
                        event = json.loads(raw)
                    except ValueError:
                        continue
                    parsed = self._normalize_event(event, session_id)
                    if parsed.get("status") != "message" or parsed["message"].role != "user":
                        continue
                    title = self._clean_derived_title(parsed["message"].content)
                    if title:
                        return title, "derived_first_user_message"
        except OSError:
            pass
        try:
            moment = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            moment = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        return f"未命名对话 {moment.astimezone().strftime('%Y-%m-%d %H%M')}", "fallback_timestamp"

    def discover(self) -> dict[str, DiscoveredSession]:
        self.discovery_issues = []
        titles = self._titles(); existing_titles = self._existing_titles(); found: dict[str, DiscoveredSession] = {}
        paths = (path for name in ('sessions', 'archived_sessions')
                 for path in (self.codex_home / name).rglob('rollout-*.jsonl'))
        groups = {}
        for path in paths:
            try:
                with path.open("rb") as handle:
                    first = handle.readline(self.max_event_bytes + 1)
                if not first.endswith(b"\n"):
                    self.discovery_issues.append({'source_file':path.relative_to(self.codex_home).as_posix(), 'reason':'来源首行不完整或超过读取上限'})
                    continue
                row = json.loads(first)
                payload = row.get("payload") if isinstance(row, dict) else None
                if not isinstance(row, dict) or row.get("type") != "session_meta" or not isinstance(payload, dict) or not payload.get("id"):
                    self.discovery_issues.append({'source_file':path.relative_to(self.codex_home).as_posix(), 'reason':'来源头格式未知，尚不能确认会话身份'})
                    continue
                # Subagents and guardian decisions are execution evidence for a
                # parent task, not owner conversations. Their original JSONL
                # remains untouched in Codex storage and must not reappear as
                # independent "未命名对话" entries.
                from .conversation_visibility import codex_exclusion
                if codex_exclusion(payload):
                    continue
                session_id = str(payload["id"]); stat = path.stat()
                official = titles.get(session_id)
                if official and not self._is_placeholder_title(official, session_id):
                    title, title_source = official, "session_index"
                elif session_id in existing_titles:
                    title, title_source = existing_titles[session_id]
                else:
                    title, title_source = self._derived_title(path, session_id, payload.get("timestamp") or row.get("timestamp"))
                relative = path.relative_to(self.codex_home).as_posix()
                candidate = DiscoveredSession(
                    session_id, path, relative, _fingerprint(first),
                    title, title_source, stat.st_size, stat.st_mtime_ns,
                    f"{stat.st_dev}:{stat.st_ino}",
                )
                groups.setdefault(session_id, {})[candidate.object_id] = candidate
            except (OSError, ValueError, KeyError) as exc:
                self.discovery_issues.append({'source_file':path.relative_to(self.codex_home).as_posix(), 'reason':'来源无法读取或头部无法解析', 'error_type':type(exc).__name__})
                continue
        for sid, group in groups.items():
            sources = tuple(sorted(group.values(), key=lambda value: value.relative_path))
            primary = max(sources, key=lambda value: (value.mtime_ns, value.size))
            found[sid] = replace(primary, fragments=sources)
        return found

    def configure(self, *, policy: str | None = None, enabled: bool | None = None,
                  authorize: list[str] | None = None, revoke: list[str] | None = None,
                  exclude: list[str] | None = None, include: list[str] | None = None,
                  owner_confirmed: bool = False, validate_before_write=None) -> dict:
        with self._lock, self._process_lock.acquire(timeout=30):
            if validate_before_write is not None:validate_before_write()
            return self._configure(policy=policy, enabled=enabled, authorize=authorize, revoke=revoke,
                                   exclude=exclude, include=include, owner_confirmed=owner_confirmed)

    def _configure(self, *, policy=None, enabled=None, authorize=None, revoke=None,
                   exclude=None, include=None, owner_confirmed=False):
        if not owner_confirmed:
            raise PermissionError("启用、暂停或调整 Codex 归档范围需要 Owner 确认")
        state = self._state()
        if policy is not None and policy not in ARCHIVE_POLICIES:
            raise ValueError("归档策略必须是 selected、all 或 paused")
        if policy is not None:
            if policy == "paused":
                if state["archive_policy"] != "paused":
                    state["resume_policy"] = state["archive_policy"]
            else:
                state["resume_policy"] = policy
            state["archive_policy"] = policy
        if enabled is not None:
            if enabled:
                state["archive_policy"] = state.get("resume_policy") or "selected"
            else:
                if state["archive_policy"] != "paused":
                    state["resume_policy"] = state["archive_policy"]
                state["archive_policy"] = "paused"
        current = set(state["selected_session_ids"])
        discovered = self.discover()
        state['discovery_issues'] = self.discovery_issues
        for session_id in authorize or []:
            if session_id not in discovered:
                raise ValueError(f"未发现 Session：{session_id}")
            current.add(session_id)
        current.difference_update(revoke or [])
        excluded = set(state["excluded_session_ids"])
        excluded.update(exclude or []); excluded.difference_update(include or [])
        state["selected_session_ids"] = sorted(current)
        state["excluded_session_ids"] = sorted(excluded)
        self._save_state(state)
        result = self.status(discovered=discovered)
        state["status_snapshot"] = result
        self._save_state(state)
        return result

    @staticmethod
    def _effective_ids(state: dict, discovered: dict[str, DiscoveredSession]) -> list[str]:
        policy = state["resume_policy"] if state["archive_policy"] == "paused" else state["archive_policy"]
        candidates = set(discovered) if policy == "all" else set(state["selected_session_ids"])
        return sorted(candidates - set(state["excluded_session_ids"]),
                      key=lambda sid: discovered.get(sid).mtime_ns if discovered.get(sid) else 0,
                      reverse=True)

    @staticmethod
    def _needs_sync(source: DiscoveredSession | None, cursor: dict) -> bool:
        if source is None or not cursor:
            return True
        if cursor.get("current_status") in {"processing", "failed", "source_missing", "gap_detected", "partial_line_pending"}:
            return True
        return bool(
            int(cursor.get("confirmed_offset") or 0) != source.size
            or cursor.get("file_mtime_ns") != source.mtime_ns
            or cursor.get('source_file') != source.relative_path
            or cursor.get("parser_version") != PARSER_VERSION
            or cursor.get("missing_attachments")
            or cursor.get("resolved_title") != source.title
            or cursor.get("title_source") != source.title_source
            or cursor.get("visibility_policy_version") != VISIBILITY_POLICY_VERSION
            or cursor.get("review_policy_version") != REVIEW_POLICY_VERSION
        )

    def status(self, *, discovered: dict[str, DiscoveredSession] | None = None, _state_override=None) -> dict:
        result=self._status(discovered=discovered,_state_override=_state_override)
        from .codex_sqlite_history import summary as sqlite_summary
        result['sqlite'] = sqlite_summary(self)
        from .local_source_status import _codex_locations
        result['source_locations'] = _codex_locations(self)
        history = result.get('history') or {}
        counts = history.get('counts') or {}
        result['source_progress'] = {
            'history': {'unit': 'files', 'known_total': history.get('fragment_count') or None,
                        'checked': history.get('completed_fragments', 0),
                        'archived_messages': (history.get('archived_messages') or 0) +
                                             ((result.get('live') or {}).get('saved_messages') or 0),
                        'excluded': counts.get('excluded', 0),
                        'failed_missing': counts.get('failed', 0) + counts.get('source_missing', 0),
                        'total_is_dynamic': True},
            'sqlite': result['sqlite'],
            'live': result.get('live') or {},
        }
        from .attachment_reference_stats import summarize
        rows=[]
        policy_state = _state_override if _state_override is not None else self._state()
        scope = policy_state['resume_policy'] if policy_state['archive_policy'] == 'paused' else policy_state['archive_policy']
        allowed = set(policy_state['selected_session_ids'])
        excluded = set(policy_state['excluded_session_ids'])
        sessions = {item['session_key']: item for item in self.store._registry().get('sessions', {}).values()
                    if item.get('source_key') == 'codex' and item.get('source_session_id') not in excluded
                    and (scope == 'all' or item.get('source_session_id') in allowed)}
        keys=list(sessions)
        attachment_rows=self.store.attachment_status_many(keys)
        for session_key,item in sessions.items():
            for row in attachment_rows[session_key]:
                rows.append(dict(row,session_key=session_key,source_session_id=item['source_session_id']))
        from .attachment_reference_review import read_reviews
        result['attachment_references']=summarize(rows,_read_json(self.locator_path,{}),read_reviews(self.context))
        return result

    def _status(self, *, discovered: dict[str, DiscoveredSession] | None = None, _state_override=None) -> dict:
        from .codex_history import history_summary, is_caught_up
        state = _state_override if _state_override is not None else self._state()
        policy = state['resume_policy'] if state['archive_policy'] == 'paused' else state['archive_policy']
        active_rows = [row for sid, row in state['sessions'].items()
                       if sid not in state['excluded_session_ids'] and (policy == 'all' or sid in state['selected_session_ids'])]
        from .codex_format_repair import summary as format_summary
        format_ids = (self._effective_ids(state, discovered) if discovered is not None else
                      (state.get('status_snapshot') or {}).get('authorized_session_ids', []))
        format_review = format_summary(self, format_ids)
        verified_bodies = set(format_review['verified_session_ids'])
        def displayed_cursor(sid):
            row = dict(state['sessions'].get(sid, {}))
            if sid in verified_bodies and row.get('body_status') == 'needs_review':
                row['body_status'] = 'complete'
            return row
        progress = {'history': history_summary(state, format_review), 'live': state.get('live_progress', {}),
                    'format_review': format_review,
                    'needs_review_event_count': format_review['unresolved_events'],
                    'file_delivery_review_error':state.get('file_delivery_review_error'),
                    'discovery_issues': state.get('discovery_issues', []),
                    'attachment_failed_count': sum(x.get('attachment_failed_count', 0) for x in active_rows),
                    'body_complete_session_count': sum(displayed_cursor(sid).get('body_status') == 'complete' for sid in format_ids)}
        if discovered is None:
            snapshot = state.get("status_snapshot")
            if snapshot:
                return {**snapshot, **progress, "archive_policy": state["archive_policy"],
                        "resume_policy": state["resume_policy"],
                        "enabled": state["archive_policy"] != "paused",
                        "last_error": state.get("last_error"),
                        "snapshot_updated_at": state.get("updated_at")}
            # Cold-start fallback is intentionally derived only from the
            # persisted cursors. Discovery remains the background worker's job.
            persisted = state.get("sessions", {})
            ids = list(persisted)
            cursors = list(persisted.values())
            return {**progress,
                "adapter": "codex", "enabled": state["archive_policy"] != "paused",
                "archive_policy": state["archive_policy"], "resume_policy": state["resume_policy"],
                "parser_version": PARSER_VERSION, "discovered_session_count": len(ids),
                "discovered_sessions": [{"session_id": sid,
                    "title": row.get("resolved_title") or f"Codex 会话 {sid[:8]}",
                    "title_source": row.get("title_source"), "authorized": True, "excluded": False}
                    for sid, row in persisted.items()],
                "authorized_session_count": len(ids), "authorized_session_ids": ids,
                "selected_session_ids": state["selected_session_ids"], "excluded_session_count": 0,
                "excluded_session_ids": state["excluded_session_ids"], "last_sync_at": state.get("last_sync_at"),
                "last_sync_message_count": state.get("last_sync_message_count", 0),
                "last_message_sync_at": state.get("last_message_sync_at"), "current_cursors": persisted,
                "processing_session_count": sum(x.get("current_status") == "processing" for x in cursors),
                "caught_up_session_count": sum(x.get("current_status") == "synced" for x in cursors),
                "failed_session_count": sum(x.get("current_status") == "failed" for x in cursors),
                "total_archived_message_count": sum(int(x.get("archived_message_count") or 0) for x in cursors),
                "total_attachment_count": sum(int(x.get("attachment_count") or 0) for x in cursors),
                "title_source_counts": {}, "needs_review_event_count": 0, "needs_review_session_count": 0,
                "pending_backfill_count": 0, "missing_attachment_count": sum(
                    len(x.get("missing_attachments", [])) for x in cursors),
                "gap_detected": any(x.get("current_status") == "gap_detected" for x in cursors),
                "gap_session_ids": [x.get("source_session_id") for x in cursors
                                    if x.get("current_status") == "gap_detected"],
                "last_error": state.get("last_error"), "snapshot_updated_at": state.get("updated_at"),
            }
        effective_ids = self._effective_ids(state, discovered)
        cursors = [state["sessions"].get(sid, {}) for sid in effective_ids]
        missing = sum(len(cursor.get("missing_attachments", [])) for cursor in cursors)
        gaps = [cursor for cursor in cursors if cursor.get("current_status") == "gap_detected"]
        processing = sum(cursor.get("current_status") == "processing" for cursor in cursors)
        failed = sum(cursor.get("current_status") == "failed" for cursor in cursors)
        caught_up = sum(is_caught_up(self, discovered.get(sid), displayed_cursor(sid))
                        for sid in effective_ids)
        pending = max(0, len(effective_ids) - processing - failed - caught_up)
        title_sources = {"session_index": 0, "derived_first_user_message": 0, "fallback_timestamp": 0}
        for session_id in effective_ids:
            source = discovered.get(session_id)
            if source:
                title_sources[source.title_source] = title_sources.get(source.title_source, 0) + 1
        return {**progress,
            "adapter": "codex", "enabled": state["archive_policy"] != "paused",
            "archive_policy": state["archive_policy"], "resume_policy": state["resume_policy"],
            "parser_version": PARSER_VERSION,
            "discovered_session_count": len(discovered),
            "discovered_fragment_count": sum(len(item.fragments) or 1 for item in discovered.values()),
            "discovered_sessions": [{"session_id": item.session_id, "title": item.title,
                                      "title_source": item.title_source,
                                      "fragment_count": len(item.fragments) or 1,
                                      "authorized": item.session_id in effective_ids,
                                      "excluded": item.session_id in state["excluded_session_ids"]}
                                     for item in sorted(discovered.values(), key=lambda value: value.mtime_ns, reverse=True)],
            "authorized_session_count": len(effective_ids), "authorized_session_ids": effective_ids,
            "selected_session_ids": state["selected_session_ids"],
            "excluded_session_count": len(set(discovered) & set(state["excluded_session_ids"])),
            "excluded_session_ids": state["excluded_session_ids"],
            "last_sync_at": state.get("last_sync_at"),
            "last_sync_message_count": state.get("last_sync_message_count", 0),
            "last_message_sync_at": state.get("last_message_sync_at"),
            "current_cursors": {sid: displayed_cursor(sid) for sid in effective_ids},
            "processing_session_count": processing, "caught_up_session_count": caught_up,
            "failed_session_count": failed,
            "total_archived_message_count": sum(int(cursor.get("archived_message_count") or 0) for cursor in cursors),
            "total_attachment_count": sum(int(cursor.get("attachment_count") or 0) for cursor in cursors),
            "title_source_counts": title_sources,
            "needs_review_event_count": format_review['unresolved_events'],
            "needs_review_session_count": len(format_review['unresolved_by_session']),
            "pending_backfill_count": pending, "missing_attachment_count": missing,
            "gap_detected": bool(gaps), "gap_session_ids": [x.get("source_session_id") for x in gaps],
            "last_error": state.get("last_error"),
        }

    def sync(self, *, manual: bool = False, batch_size: int | None = None) -> dict:
        from .codex_history import sync
        return sync(self, manual=manual, batch_size=batch_size)

    def _sync_one(self, state: dict, source: DiscoveredSession) -> dict:
        from .codex_history import key
        cursor = self._work_cursor if self._work_cursor is not None else state["sessions"].setdefault(source.session_id, {"source_session_id": source.session_id})
        target = min(source.size, self._read_target) if self._read_target is not None else source.size
        session = self.store.ensure_session(
            source_key="codex", source_session_id=source.session_id, title=source.title,
            source_instance=self._instance_id(), acquisition_method="codex_local_jsonl_readonly",
        )
        cursor['session_key'] = session['session_key']
        previous_offset = int(cursor.get("confirmed_offset") or 0)
        title_changed = (cursor.get("resolved_title") != source.title
                         or cursor.get("title_source") != source.title_source)
        position_ledger = Path(session["archive_path"]) / "03_元数据" / "message-positions.jsonl"
        position_backfill = bool(previous_offset and cursor.get('archived_message_count', 0)
                                 and not position_ledger.exists() and not cursor.get('initial_tail_cursor')
                                 and not cursor.get('position_backfill_started'))
        semantic_backfill = bool(previous_offset and 'unknown_event_count' not in cursor and not cursor.get('initial_tail_cursor'))
        if previous_offset and cursor.get("visibility_policy_version") != VISIBILITY_POLICY_VERSION:
            physical_sources = {token.split('::')[0] for token in state['sessions'][source.session_id].get('fragments', {})}
            if source.size > self.page_bytes or len(physical_sources) > 1:
                raise ValueError('旧版可见性规则需专项复核；未删除其他源分片的正文')
            self._repair_visibility(session, source)
        if previous_offset and cursor.get("review_policy_version") != REVIEW_POLICY_VERSION:
            if source.size > self.page_bytes:
                raise ValueError('旧版异常索引需专项复核；本次未执行无界全量扫描')
            self._reindex_review(source)
        safe_incremental = self._can_resume(source, cursor) and not position_backfill and not semantic_backfill
        if (safe_incremental and target == previous_offset
                and not cursor.get("missing_attachments") and cursor.get("file_mtime_ns") == source.mtime_ns):
            current = self.store.session(session["session_key"])
            attachments = self.store.attachment_status(session["session_key"])
            cursor.update({
                "current_status": "needs_review" if cursor.get('unknown_event_count') else "synced", "last_error": None,
                "source_file": source.relative_path,
                "resolved_title": source.title, "title_source": source.title_source,
                "visibility_policy_version": VISIBILITY_POLICY_VERSION,
                "review_policy_version": REVIEW_POLICY_VERSION,
                "archived_message_count": int(current.get("message_count") or 0),
                "attachment_count": len(attachments),
                "missing_attachments": [row for row in attachments if row.get("missing")],
            })
            self.store.update_session(session["session_key"], sync_status=cursor['current_status'],
                                      sync_position=cursor, metadata={"title_source": source.title_source})
            return {"session_id": source.session_id, "session_key": session["session_key"],
                    "archive_path": session["archive_path"], "status": "title_updated" if title_changed else "unchanged",
                    "changed": title_changed,
                    "saved_messages": 0, "duplicate_messages": 0, "unsupported_events": 0,
                    "previous_offset": previous_offset, "confirmed_offset": previous_offset,
                    "pending_partial_bytes": 0, "missing_attachment_count": 0,
                    "gap_detected_during_sync": False}
        start = previous_offset if safe_incremental else 0
        if not safe_incremental:
            cursor.pop('large_record', None)
        gap = bool(cursor and previous_offset and not safe_incremental and not position_backfill and not semantic_backfill)
        if gap:
            cursor.update({"current_status": "gap_detected", "missing_intervals": [{
                "from": previous_offset, "reason": "source_truncated_replaced_moved_or_fingerprint_mismatch", "detected_at": _now()
            }]})
            self._save_state(state)
        complete, pending = self._complete_region(source.path, start, target, source)
        unknown = int(cursor.get('unknown_event_count', 0)) if safe_incremental else 0
        saved = duplicates = unsupported = 0; last_message_id = cursor.get("last_normalized_message_id")
        last_start = start; last_end = start; last_fingerprint = cursor.get("last_complete_event_fingerprint")
        processing_began=time.monotonic();processed_events=0
        for raw, line_start, line_end in complete:
            if processed_events and time.monotonic()-processing_began>=self.processing_budget_seconds:break
            processed_events+=1
            from .codex_large_records import StreamedInternal
            last_start, last_end = line_start, line_end
            if isinstance(raw, StreamedInternal):
                last_fingerprint = raw.fingerprint
                cursor['streamed_internal_event_count'] = cursor.get('streamed_internal_event_count', 0) + 1
                continue
            last_fingerprint = _fingerprint(raw)
            try:
                event = json.loads(raw)
            except ValueError:
                unsupported += 1; self._review(source.session_id, last_fingerprint, "invalid_json", line_start, {})
                continue
            if isinstance(event, dict) and event.get('type') == 'turn_context':
                turn_payload = event.get('payload')
                if isinstance(turn_payload, dict) and turn_payload.get('turn_id'):
                    cursor['source_turn_id'] = str(turn_payload['turn_id'])
            parsed = self._normalize_event(event, source.session_id)
            if parsed["status"] == "message":
                message: NormalizedMessage = parsed["message"]
                if message.role == 'assistant' and cursor.get('source_turn_id'):
                    message = replace(message, provenance={**(message.provenance or {}),
                                                           'source_turn_id': cursor['source_turn_id']})
                from .codex_sqlite_history import match_jsonl
                matched = match_jsonl(self, source.session_id, session['session_key'], message)
                if matched:
                    message = replace(message, message_id=matched,
                                      provenance={**(message.provenance or {}),
                                                  'jsonl_message_id': message.message_id,
                                                  'cross_source_match': 'sqlite'})
                message = replace(message, sync_position={"byte_offset": line_start, "byte_end": line_end})
                position = {
                    "source_file": source.relative_path, "source_file_identity": source.meta_fingerprint,
                    "source_object_id": source.object_id, "byte_offset": line_start, "byte_end": line_end,
                    "source_fragment": key(source), "source_fragment_order": source.relative_path,
                    "event_timestamp": message.timestamp,
                }
                cursor['failure_stage'] = 'body'
                # Commit body first, then attachments. An attachment copy error must not
                # misrepresent successfully saved text or lose the retry position.
                result = self.store.append_normalized_message(session["session_key"], replace(message, attachments=()), materialize=False)
                self.store.record_message_position(session["session_key"], message.message_id, position)
                saved += int(result["saved"]); duplicates += int(result["duplicate"])
                last_message_id = message.message_id
                self._remember_attachment_locators(source.session_id, message)
                self._append_public_evidence(session, event, message)
                cursor['failure_stage'] = 'attachments'
                try:
                    self.store.reconcile_attachments(session['session_key'], message.message_id, message.attachments, materialize=False)
                except Exception:
                    cursor['attachment_failed_count'] = 1
                    cursor['archived_message_count'] = int(self.store.session(session['session_key']).get('message_count') or 0)
                    self.store.materialize_session(session['session_key'])
                    raise
                cursor['failure_stage'] = 'body'
            elif parsed["status"] == "needs_review":
                unsupported += 1; self._review(source.session_id, last_fingerprint, parsed["reason"], line_start, parsed.get("metadata", {}))
        cursor['failure_stage'] = 'attachments'
        try:
            attachment_rows = self._retry_attachments(source.session_id, session["session_key"])
        except Exception:
            cursor['attachment_failed_count'] = 1
            raise
        attachment_version=hashlib.sha256(json.dumps(sorted([(str(r.get('source_message_id')),str(r.get('source_reference')),r.get('sha256'),r.get('stored_path'),bool(r.get('missing'))) for r in attachment_rows]),ensure_ascii=False).encode()).hexdigest()
        if saved or duplicates or position_backfill or semantic_backfill or cursor.get('attachment_projection_version')!=attachment_version:
            self.store.materialize_session(session["session_key"])
        cursor['attachment_projection_version']=attachment_version
        missing = [row for row in attachment_rows if row.get("missing")]
        current = self.store.session(session["session_key"])
        stat = source.path.stat(); cursor.update({
            "source_session_id": source.session_id, "source_file": source.relative_path,
            "source_file_identity": source.meta_fingerprint, "source_object_id": source.object_id,
            "confirmed_offset": last_end if complete else start,
            "last_event_start": last_start if complete else cursor.get("last_event_start"),
            "last_complete_event_fingerprint": last_fingerprint,
            "last_normalized_message_id": last_message_id, "file_size": stat.st_size, "file_mtime_ns": stat.st_mtime_ns,
            "parser_version": PARSER_VERSION, "last_successful_sync": _now(),
            "pending_partial_bytes": pending, "missing_attachments": [{
                "message_id": row.get("source_message_id"), "reference": row.get("source_reference"),
                "original_name": row.get("original_name"),
            } for row in missing],
            "current_status": "reading" if last_end < target and not pending else "needs_review" if unknown + unsupported else "gap_recovered" if gap else "partial_line_pending" if pending else "synced",
            "unknown_event_count": unknown + unsupported,
            "attachment_failed_count": 0, "failure_stage": None,
            "last_error": None, "resolved_title": source.title, "title_source": source.title_source,
            "visibility_policy_version": VISIBILITY_POLICY_VERSION,
            "review_policy_version": REVIEW_POLICY_VERSION,
            "archived_message_count": int(current.get("message_count") or 0),
            "attachment_count": len(attachment_rows),
        })
        if not gap:
            cursor["missing_intervals"] = []
        if position_backfill:
            cursor['position_backfill_started'] = True
        if complete and cursor.get('large_record', {}).get('scan_offset', source.size+1) <= cursor['confirmed_offset']:
            cursor.pop('large_record', None)
        self.store.update_session(session["session_key"], sync_status=cursor["current_status"],
                                  sync_position=cursor, metadata={
                                      "parser_version": PARSER_VERSION, "codex_instance_id": self._instance_id(),
                                      "collection_method": "codex_local_jsonl_readonly",
                                      "missing_attachment_count": len(missing), "title_source": source.title_source,
                                  })
        return {"session_id": source.session_id, "session_key": session["session_key"],
                "archive_path": session["archive_path"], "status": cursor["current_status"],
                "changed": True,
                "saved_messages": saved, "duplicate_messages": duplicates,
                "unsupported_events": unsupported, "previous_offset": previous_offset,
                "confirmed_offset": cursor["confirmed_offset"], "pending_partial_bytes": pending,
                "missing_attachment_count": len(missing), "gap_detected_during_sync": gap}

    def _repair_visibility(self, session: dict, source: DiscoveredSession) -> dict:
        """Remove rows admitted by an older visibility rule, without retaining their body."""
        allowed = set()
        with source.path.open("rb") as handle:
            for raw in handle:
                if not raw.endswith(b"\n"):
                    continue
                try:
                    parsed = self._normalize_event(json.loads(raw), source.session_id)
                except ValueError:
                    continue
                if parsed.get("status") == "message":
                    allowed.add(parsed["message"].message_id)
        raw_path = self.context.raw_message_store / f"{session['session_key']}.jsonl"
        rows = self.store._message_rows(raw_path)
        removed = [row for row in rows if str(row.get("message_id")) not in allowed]
        if not removed:
            return {"removed": 0}
        kept = [row for row in rows if str(row.get("message_id")) in allowed]
        self._rewrite_jsonl(raw_path, kept)
        folder = Path(session["archive_path"])
        removed_ids = {str(row.get("message_id")) for row in removed}
        public = folder / "00_原始数据" / "codex-public-events.jsonl"
        self._rewrite_jsonl(public, [row for row in self.store._message_rows(public)
                                     if str(row.get("message_id")) not in removed_ids])
        attachment_ledger = folder / "03_元数据" / "attachments.jsonl"
        self._rewrite_jsonl(attachment_ledger, [row for row in self.store._message_rows(attachment_ledger)
                                                if str(row.get("source_message_id")) not in removed_ids])
        audit = self.context.audit / "codex-visibility-recovery.jsonl"
        audit.parent.mkdir(parents=True, exist_ok=True)
        with audit.open("a", encoding="utf-8", newline="\n") as handle:
            for row in removed:
                record = {
                    "source_session_id": source.session_id,
                    "message_id": row.get("message_id"),
                    "content_sha256": row.get("content_sha256"),
                    "reason": "older_visibility_rule_internal_injection",
                    "visibility_policy_version": VISIBILITY_POLICY_VERSION,
                    "repaired_at": _now(),
                }
                handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.store.update_session(session["session_key"], metadata={"message_count": len(kept)})
        self.store.materialize_session(session["session_key"])
        return {"removed": len(removed)}

    def _reindex_review(self, source: DiscoveredSession) -> None:
        with source.path.open("rb") as handle:
            offset = 0
            for raw in handle:
                start = offset; offset += len(raw)
                if not raw.endswith(b"\n"):
                    continue
                try:
                    parsed = self._normalize_event(json.loads(raw), source.session_id)
                except ValueError:
                    self._review(source.session_id, _fingerprint(raw), "invalid_json", start, {})
                    continue
                if parsed.get("status") == "needs_review":
                    self._review(source.session_id, _fingerprint(raw), parsed["reason"], start,
                                 parsed.get("metadata", {}))

    @staticmethod
    def _rewrite_jsonl(path: Path, rows: list[dict]) -> None:
        if not path.exists() and not rows:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text("".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
                                     for row in rows), encoding="utf-8")
        os.replace(temporary, path)

    def _can_resume(self, source: DiscoveredSession, cursor: dict) -> bool:
        offset = int(cursor.get("confirmed_offset") or 0)
        if not offset:
            return True
        if cursor.get("parser_version") != PARSER_VERSION or cursor.get("source_file_identity") != source.meta_fingerprint:
            return False
        if cursor.get("source_object_id") and cursor.get("source_object_id") != source.object_id:
            return False
        if source.size < offset:
            return False
        if source.size == cursor.get('file_size') and source.mtime_ns != cursor.get('file_mtime_ns'):
            return False
        start = cursor.get("last_event_start"); expected = cursor.get("last_complete_event_fingerprint")
        if start is None or not expected:
            return False
        try:
            with source.path.open("rb") as handle:
                if offset-int(start) > self.max_event_bytes:
                    from .codex_large_records import fingerprint
                    handle.seek(offset-1)
                    return handle.read(1) == b'\n' and fingerprint(handle, int(start), offset) == expected
                handle.seek(int(start)); raw = handle.readline(self.max_event_bytes + 1)
                if len(raw) > self.max_event_bytes:
                    from .codex_large_records import fingerprint
                    handle.seek(offset-1)
                    return handle.read(1) == b'\n' and fingerprint(handle, int(start), offset) == expected
            return bool(raw.endswith(b"\n") and _fingerprint(raw) == expected)
        except OSError:
            return False

    def _discover_authorized(self, state: dict) -> dict[str, DiscoveredSession]:
        titles = self._titles(); result = {}; unresolved = []
        for session_id in state["authorized_session_ids"]:
            relative = state["sessions"].get(session_id, {}).get("source_file")
            path = (self.codex_home / relative).resolve() if relative else None
            try:
                if path is None:
                    raise OSError
                path.relative_to(self.codex_home)
                with path.open("rb") as handle:
                    first = handle.readline()
                row = json.loads(first); payload = row.get("payload", {})
                if row.get("type") != "session_meta" or payload.get("id") != session_id:
                    raise ValueError
                stat = path.stat(); result[session_id] = DiscoveredSession(
                    session_id, path, path.relative_to(self.codex_home).as_posix(), _fingerprint(first),
                    titles.get(session_id) or f"Codex 会话 {session_id[:8]}", "legacy_discovery",
                    stat.st_size, stat.st_mtime_ns,
                    f"{stat.st_dev}:{stat.st_ino}",
                )
            except (OSError, ValueError, KeyError):
                unresolved.append(session_id)
        if unresolved:
            full = self.discover()
            result.update({session_id: full[session_id] for session_id in unresolved if session_id in full})
        return result

    def _complete_region(self, path: Path, start: int, target: int | None = None,
                         source: DiscoveredSession | None = None) -> tuple[list[tuple[bytes, int, int]], int]:
        rows = []
        target = path.stat().st_size if target is None else target
        with path.open("rb") as handle:
            if source is not None:
                stat = os.fstat(handle.fileno())
                first = handle.readline(self.max_event_bytes + 1)
                if f'{stat.st_dev}:{stat.st_ino}' != source.object_id or _fingerprint(first) != source.meta_fingerprint:
                    raise ValueError('来源在发现后被替换，未读取到其他会话名下')
            handle.seek(start); position = start
            if source is not None and (self._work_cursor or {}).get('large_record', {}).get('start') == start:
                from .codex_large_records import scan
                record, end, pending = scan(self, handle, source, start, target)
                return ([(record, start, end)] if record else []), pending
            while position < target and len(rows) < self.page_events and position - start < self.page_bytes:
                from .codex_large_records import CHUNK, internal_prefix, scan
                raw = handle.readline(min(self.read_chunk_bytes, self.max_event_bytes + 1, target - position))
                if not raw:
                    break
                if source is not None and not raw.endswith(b'\n') and internal_prefix(raw):
                    if rows:
                        return rows, 0
                    record, end, pending = scan(self, handle, source, position, target)
                    return ([(record, position, end)] if record else []), pending
                if not raw.endswith(b'\n') and len(raw) < target-position:
                    handle.seek(position)
                    raw = handle.readline(min(self.max_event_bytes + 1, target-position))
                if len(raw) > self.max_event_bytes:
                    if rows:
                        return rows, 0
                    if source is None:
                        raise ValueError('大记录必须携带已验证的来源身份读取')
                    from .codex_large_records import scan
                    record, end, pending = scan(self, handle, source, position, target)
                    return ([(record, position, end)] if record else []), pending
                end = position + len(raw)
                if not raw.endswith(b"\n"):
                    return rows, len(raw)
                rows.append((raw, position, end)); position = end
        return rows, 0

    def _normalize_event(self, event: dict, session_id: str) -> dict:
        if not isinstance(event, dict):
            return {'status': 'needs_review', 'reason': 'non_object_event', 'metadata': {}}
        from .conversation_visibility import message_structure_reason
        from .codex_event_formats import auxiliary_kind, legacy_public_assistant, public_question, FORMAT_POLICY
        question = public_question(event, session_id)
        if question:
            if question.get('status') == 'needs_review':
                return question
            item=event['payload']['item']
            structure_reason=message_structure_reason(item.get('channel'),item.get('recipient'))
            if structure_reason:
                if structure_reason.startswith('internal_'):return {'status':'ignored','category':structure_reason}
                return {'status':'needs_review','reason':structure_reason,
                        'metadata':{'channel':item.get('channel'),'recipient':item.get('recipient')}}
            return {'status': 'message', 'message': NormalizedMessage(
                message_id=question['id'], role='assistant', content=question['text'], timestamp=event.get('timestamp'),
                provenance={'adapter': 'codex_source_adapter', 'parser_version': PARSER_VERSION,
                            'source_session_id': session_id, 'format_policy_version': FORMAT_POLICY,
                            'public_question_card': True,'knowledge_eligible':True,'message_structure_policy':1,
                            'source_channel':item.get('channel'),'source_recipient':item.get('recipient')})}
        auxiliary = auxiliary_kind(event)
        if auxiliary:
            return {'status': 'ignored', 'category': auxiliary}
        event_type = event.get("type"); payload = event.get("payload")
        if event_type in KNOWN_TOP_LEVEL_INTERNAL:
            return {"status": "ignored"}
        if event_type != "response_item" or not isinstance(payload, dict):
            return {"status": "needs_review", "reason": "unknown_top_level_event",
                    "metadata": {"event_type": str(event_type)}}
        payload_type = payload.get("type")
        if payload_type in KNOWN_RESPONSE_INTERNAL:
            return {"status": "ignored"}
        if payload_type != "message":
            return {"status": "needs_review", "reason": "unknown_response_item",
                    "metadata": {"payload_type": str(payload_type)}}
        role = str(payload.get("role") or "").casefold(); phase = payload.get("phase")
        if role in {"developer", "system"}:
            return {"status": "ignored"}
        if role not in {"user", "assistant"}:
            return {"status": "needs_review", "reason": "uncertain_message_role", "metadata": {"role": role}}
        structure_reason=message_structure_reason(payload.get('channel'),payload.get('recipient'))
        if structure_reason:
            if structure_reason.startswith('internal_'):
                return {'status':'ignored','category':structure_reason}
            return {'status':'needs_review','reason':structure_reason,
                    'metadata':{'channel':payload.get('channel'),'recipient':payload.get('recipient')}}
        legacy_public = role == 'assistant' and legacy_public_assistant(payload)
        if role == "assistant" and phase not in VISIBLE_ASSISTANT_PHASES and not legacy_public:
            return {"status": "needs_review", "reason": "uncertain_assistant_phase", "metadata": {"phase": phase}}
        text_parts: list[str] = []; attachments: list[NormalizedAttachment] = []
        unknown_content = []
        for content_index, item in enumerate(payload.get("content") or []):
            if not isinstance(item, dict):
                unknown_content.append(type(item).__name__); continue
            kind = item.get("type")
            if kind in {"input_text", "output_text"}:
                text_parts.append(str(item.get("text") or ""))
            elif kind in ATTACHMENT_TYPES:
                try:
                    attachments.append(self._attachment(item, content_index))
                except (ValueError, TypeError):
                    return {"status": "needs_review", "reason": "invalid_attachment_reference",
                            "metadata": {"attachment_type": str(kind),
                                         "attachment_keys": sorted(str(key) for key in item)}}
            else:
                unknown_content.append(str(kind))
        if unknown_content:
            return {"status": "needs_review", "reason": "unknown_message_content",
                    "metadata": {"content_types": unknown_content, "role": role}}
        if role == "user" and self._is_internal_user_context(text_parts):
            return {"status": "ignored"}
        message_id = str(payload.get("id") or hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest())
        attachments.extend(self._delivery_attachments('\n\n'.join(text_parts), role, 'final' if legacy_public else phase))
        return {"status": "message", "message": NormalizedMessage(
            message_id=message_id, role=role, content="\n\n".join(text_parts),
            timestamp=event.get("timestamp"), attachments=tuple(attachments),
            provenance={"adapter": "codex_source_adapter", "parser_version": PARSER_VERSION,
                        "source_session_id": session_id, "assistant_phase": phase,
                        "source_channel": payload.get('channel'), "source_recipient": payload.get('recipient'),
                        "knowledge_eligible": True, "message_structure_policy": 1,
                        "format_policy_version": FORMAT_POLICY,
                        "legacy_public_envelope": legacy_public},
        )}

    def _delivery_attachments(self, text, role, phase=None):
        """Only explicit public delivery links, never arbitrary path mentions."""
        if role != 'assistant' or phase not in {'final', 'final_answer'}:
            return []
        text = re.sub(r'```.*?```|~~~.*?~~~|`[^`\n]*`', '', text, flags=re.S)
        text = '\n'.join(line for line in text.splitlines() if not line.lstrip().startswith('>'))
        allowed = {'.pdf','.docx','.xlsx','.xls','.csv','.tsv','.pptx','.zip','.7z','.tar','.gz',
                   '.mp4','.mov','.mkv','.mp3','.wav','.m4a','.png','.jpg','.jpeg','.webp','.gif',
                   '.svg','.html','.json','.txt','.md','.tex'}
        result=[]; seen=set()
        for match in re.finditer(r'!?\[([^\]\n]*)\]\(\s*(?:<([^>\n]+)>|([^\s()]+))\s*\)', text):
            locator = urllib.parse.unquote(match.group(2) or match.group(3))
            if locator.startswith('file:') and urllib.parse.urlparse(locator).netloc not in {'','localhost'}:continue
            if not (Path(locator).is_absolute() or locator.startswith('file:')):continue
            path = self._resolve_attachment(locator)
            if path is None or path.suffix.casefold() not in allowed or str(path) in seen:continue
            seen.add(str(path))
            result.append(self._attachment({'type':'file','local_path':str(path),'name':path.name,
                          'id':'delivery:'+hashlib.sha256(str(path).encode('utf-8')).hexdigest()}))
        return result

    def _review_file_deliveries(self, eligible_ids):
        """Bounded reread of already accepted messages; source import cursors stay intact."""
        review_path = self.locator_path.with_name('file-delivery-review.json')
        review = _read_json(review_path, {'offsets':{},'next':0})
        sessions = sorted((item for item in self.store._registry().get('sessions',{}).values()
                           if item.get('source_key')=='codex' and item.get('source_session_id') in eligible_ids),
                          key=lambda item:item['session_key'])
        if not sessions:return
        item = sessions[int(review.get('next',0)) % len(sessions)]
        review['next']=(int(review.get('next',0))+1)%len(sessions)
        # A failed attachment must not starve every later session's review.
        _atomic_json(review_path,review)
        key=item['session_key']; path=self.store.context.raw_message_store/f'{key}.jsonl'
        offset=int(review.setdefault('offsets',{}).get(key,0)); changed=False
        if path.is_file():
            with path.open('rb') as handle:
                prefix=hashlib.sha256(handle.read(256)).hexdigest()
                if review.setdefault('prefixes',{}).get(key)!=prefix or offset>path.stat().st_size:offset=0
                review['prefixes'][key]=prefix
                handle.seek(offset)
                for _ in range(32):
                    raw=handle.readline()
                    if not raw or not raw.endswith(b'\n'):break
                    if not raw.strip():offset=handle.tell();continue
                    row=json.loads(raw)
                    specs=self._delivery_attachments(row.get('original_text') or row.get('content') or '',
                                                     row.get('role'),(row.get('provenance') or {}).get('assistant_phase'))
                    if specs:
                        message=NormalizedMessage(message_id=row['message_id'],role=row['role'],
                            content=row.get('original_text',''),attachments=tuple(specs))
                        self._remember_attachment_locators(item['source_session_id'],message)
                        self.store.reconcile_attachments(key,row['message_id'],specs,materialize=False)
                        changed=True
                    offset=handle.tell()
        # Missing deliveries are retried even when no source log grows.
        before=self.store.attachment_status(key)
        previous_cursor=self._work_cursor
        try:
            self._work_cursor=review.setdefault('retry',{}).setdefault(key,{})
            self._retry_attachments(item['source_session_id'],key)
        finally:self._work_cursor=previous_cursor
        after=self.store.attachment_status(key)
        if changed or before!=after:self.store.materialize_session(key)
        review['offsets'][key]=offset
        _atomic_json(review_path,review)
        return {'session_id':item['source_session_id'],'attachment_count':sum(not x.get('missing') for x in after),
                'missing_attachments':[x for x in after if x.get('missing')]}

    @staticmethod
    def _is_internal_user_context(parts: list[str]) -> bool:
        if not parts:
            return False
        if any(value.lstrip().startswith(INTERNAL_USER_PREFIXES) for value in parts):
            return True
        def enclosed(value: str) -> bool:
            value = value.strip()
            return any(value.startswith(f"<{name}>") and value.endswith(f"</{name}>")
                       for name in INTERNAL_USER_ENVELOPES)
        return all(enclosed(value) for value in parts)

    @staticmethod
    def _media_type(mime: str | None, kind: str, name: str) -> str:
        major = str(mime or "").split("/", 1)[0].casefold()
        if major in {"image", "video", "audio", "text"}:
            return major
        if "image" in kind:
            return "image"
        if "video" in kind:
            return "video"
        if "audio" in kind:
            return "audio"
        return "file" if Path(name).suffix else "unknown"

    @staticmethod
    def _extension_for_mime(mime: str | None) -> str:
        extension = mimetypes.guess_extension(str(mime or "").split(";", 1)[0].strip()) or ""
        return ".jpg" if extension == ".jpe" else extension

    def _attachment(self, item: dict, content_index: int = 0) -> NormalizedAttachment:
        locator = next((item.get(key) for key in ("local_path", "path", "file_path", "image_path", "url", "image_url")
                        if isinstance(item.get(key), str) and item.get(key)), "")
        kind = str(item.get("type") or "attachment").casefold()
        inline_data = None; data_mime = None
        if locator.startswith("data:"):
            try:
                header, encoded = locator.split(",", 1)
                declaration = header[5:]
                data_mime = declaration.split(";", 1)[0] or None
                inline_data = (base64.b64decode(encoded, validate=True)
                               if ";base64" in declaration.casefold()
                               else urllib.parse.unquote_to_bytes(encoded))
            except (ValueError, TypeError):
                raise ValueError("无效的内嵌附件")
            if not inline_data:
                raise ValueError("空的内嵌附件")
        path = None if inline_data is not None else self._resolve_attachment(locator)
        parsed_name = Path(urllib.parse.urlparse(locator).path).name if locator and not locator.startswith("data:") else ""
        mime = str(item.get("mime_type") or item.get("mime") or data_mime or
                   mimetypes.guess_type(str(item.get("name") or item.get("filename") or parsed_name))[0] or
                   "application/octet-stream")
        digest = hashlib.sha256(inline_data).hexdigest() if inline_data is not None else None
        generated = f"{kind.replace('input_', '')}-{digest[:12]}{self._extension_for_mime(mime)}" if digest else ""
        supplied_name = item.get("name") or item.get("filename") or parsed_name
        name = str(supplied_name or generated or "未命名附件")
        explicit_reference = item.get("id") or item.get("attachment_id")
        if explicit_reference:
            reference = str(explicit_reference)
        elif digest:
            reference = f"inline:{digest}:{content_index}"
        else:
            locator_fingerprint = hashlib.sha256(locator.encode("utf-8")).hexdigest() if locator else "missing"
            reference = f"locator:{locator_fingerprint}:{content_index}"
        media = self._media_type(mime, kind, name)
        return NormalizedAttachment(reference=reference, original_name=name, local_path=path,
                                    mime_type=mime, media_type=media, inline_data=inline_data,
                                    original_name_source="source" if supplied_name else "generated_from_content")

    def _resolve_attachment(self, locator: str) -> Path | None:
        if not locator:
            return None
        if locator.startswith("data:"):
            return None
        if locator.startswith("file:"):
            locator = urllib.parse.unquote(urllib.parse.urlparse(locator).path).lstrip("/") if os.name == "nt" else urllib.parse.unquote(urllib.parse.urlparse(locator).path)
        path = Path(locator).expanduser()
        if path.is_absolute():
            return path.resolve()
        root = (self.codex_home / "attachments").resolve(); candidate = (root / path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return None
        return candidate

    def _remember_attachment_locators(self, session_id: str, message: NormalizedMessage) -> None:
        locators = _read_json(self.locator_path, {})
        for attachment in message.attachments:
            if attachment.inline_data is not None:
                continue
            key = f"{session_id}\0{message.message_id}\0{attachment.reference}"
            locators[key] = {
                "local_path": str(attachment.local_path) if attachment.local_path else None,
                "original_name": attachment.original_name, "mime_type": attachment.mime_type,
                "media_type": attachment.media_type,
                "original_name_source": attachment.original_name_source,
            }
        if any(attachment.inline_data is None for attachment in message.attachments):
            _atomic_json(self.locator_path, locators)

    def _retry_attachments(self, session_id: str, session_key: str) -> list[dict]:
        locators = _read_json(self.locator_path, {}); grouped: dict[str, list[NormalizedAttachment]] = {}
        current = self.store.attachment_status(session_key)
        available = {(row.get('source_message_id'), row.get('source_reference')) for row in current if not row.get('missing')}
        pending = [(key, value) for key, value in locators.items()
                   if key.startswith(session_id + '\0') and tuple(key.split('\0')[1:]) not in available]
        cursor = self._work_cursor if self._work_cursor is not None else {}
        start = cursor.get('attachment_retry_index', 0) % max(1, len(pending))
        selected = (pending[start:] + pending[:start])[:4]
        cursor['attachment_retry_index'] = (start + len(selected)) % max(1, len(pending))
        for key, locator_value in selected:
            parts = key.split("\0")
            if len(parts) != 3 or parts[0] != session_id:
                continue
            _, message_id, reference = parts
            if isinstance(locator_value, dict):
                locator = locator_value.get("local_path"); original_name = locator_value.get("original_name") or reference
                mime_type = locator_value.get("mime_type"); media_type = locator_value.get("media_type")
                original_name_source = locator_value.get("original_name_source") or "source"
            else:
                locator = locator_value; original_name = Path(locator).name if locator else reference
                mime_type = None; media_type = None; original_name_source = "source"
            path = Path(locator) if locator else None
            grouped.setdefault(message_id, []).append(NormalizedAttachment(
                reference=reference, original_name=original_name, local_path=path,
                mime_type=mime_type, media_type=media_type,
                original_name_source=original_name_source,
            ))
        for message_id, attachments in grouped.items():
            self.store.reconcile_attachments(session_key, message_id, attachments, materialize=False)
        return self.store.attachment_status(session_key)

    def _append_public_evidence(self, session: dict, event: dict, message: NormalizedMessage) -> None:
        target = Path(session["archive_path"]) / "00_原始数据" / "codex-public-events.jsonl"
        # Retain a bounded per-session identity set and consume only appended bytes.
        # Replacements, truncation and same-size edits invalidate the cached prefix.
        cache = getattr(self, '_public_evidence_cache', {})
        self._public_evidence_cache = cache
        key = str(target)
        saved = cache.get(key, {'offset': 0, 'ids': set(), 'identity': None, 'mtime': None})
        if target.exists():
            stat = target.stat(); identity = (stat.st_dev, stat.st_ino)
            if saved['identity'] != identity or stat.st_size < saved['offset'] or (stat.st_size == saved['offset'] and saved['mtime'] != stat.st_mtime_ns):
                saved = {'offset': 0, 'ids': set(), 'identity': identity, 'mtime': None}
            with target.open('rb') as stream:
                stream.seek(saved['offset'])
                for raw in stream:
                    if not raw.endswith(b'\n'):
                        raise ValueError('原始证据末行不完整；保留原文件，等待核对')
                    if raw.strip(): saved['ids'].add(json.loads(raw).get('message_id'))
                    saved['offset'] = stream.tell()
            saved['mtime'] = stat.st_mtime_ns
        else:
            saved = {'offset': 0, 'ids': set(), 'identity': None, 'mtime': None}
        if len(cache) >= 32 and key not in cache: cache.pop(next(iter(cache)))
        cache[key] = saved
        if message.message_id in saved['ids']: return
        row = {"timestamp": event.get("timestamp"), "type": "visible_message", "message_id": message.message_id,
               "role": message.role, "content": message.content,
               "attachment_references": [x.reference for x in message.attachments], "parser_version": PARSER_VERSION}
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush(); os.fsync(handle.fileno())
        stat = target.stat()
        saved['ids'].add(message.message_id)
        saved.update(offset=stat.st_size, identity=(stat.st_dev, stat.st_ino), mtime=stat.st_mtime_ns)

    def _review(self, session_id: str, fingerprint: str, reason: str, offset: int, metadata: dict) -> None:
        source_offset=offset
        known=getattr(self,'_review_known',set());review_read_offset=getattr(self,'_review_offset',0)
        if self.review_path.is_file():
            stat=self.review_path.stat();identity=(stat.st_dev,stat.st_ino)
            if getattr(self,'_review_identity',None)!=identity or stat.st_size<review_read_offset:known=set();review_read_offset=0
            with self.review_path.open('rb') as stream:
                stream.seek(review_read_offset)
                while True:
                    raw=stream.readline()
                    if not raw or not raw.endswith(b'\n'):break
                    row=json.loads(raw);known.add((row.get('source_session_id'),row.get('event_fingerprint')));review_read_offset=stream.tell()
            self._review_identity=identity
        else:
            known=set();review_read_offset=0;self._review_identity=None
        self._review_known=known;self._review_offset=review_read_offset
        if (session_id, fingerprint) in known:
            return
        self.review_path.parent.mkdir(parents=True, exist_ok=True)
        row = {"source": "Codex", "source_session_id": session_id, "event_fingerprint": fingerprint,
               "source_offset": source_offset, "reason": reason, "metadata": metadata, "status": "needs_review", "detected_at": _now()}
        with self.review_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush();os.fsync(handle.fileno())
        known.add((session_id,fingerprint));self._review_offset=self.review_path.stat().st_size
        stat=self.review_path.stat();self._review_identity=(stat.st_dev,stat.st_ino)

    def _audit(self, result: dict) -> None:
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        # Cursors already live in durable state and manifests. Do not copy the
        # entire workspace snapshot into every ten-second diagnostic record.
        row = {"adapter": "codex", "at": _now(), **{k:v for k,v in result.items() if k != 'adapter_status'}}
        row = json.loads(json.dumps(row, ensure_ascii=False))
        # Archive paths are machine-local diagnostics and do not belong in portable audit.
        for session in row.get("sessions", []):
            session.pop("archive_path", None)
        if self.audit_path.exists() and self.audit_path.stat().st_size >= 8 * 1024 * 1024:
            # One rolling segment. Oversized legacy logs are preserved separately.
            if self.audit_path.stat().st_size > 64 * 1024 * 1024:
                destination = self.audit_path.with_name(f'codex-sync.legacy-{uuid.uuid4().hex}.jsonl')
            else:
                destination = self.audit_path.with_name('codex-sync.previous.jsonl')
            os.replace(self.audit_path, destination)
        with self.audit_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def run(interval: float = 2.0, stop_event: threading.Event | None = None) -> None:
    stop_event = stop_event or threading.Event()
    try:
        adapter = CodexSourceAdapter()
    except Exception:
        return
    while not stop_event.is_set():
        tick_started=time.monotonic()
        try:
            adapter.sync(batch_size=DEFAULT_BACKGROUND_BATCH_SIZE)
        except Exception:
            pass
        stop_event.wait(max(0.05,max(1.0,interval)-(time.monotonic()-tick_started)))


def status_summary(context=None):
    """Read the worker's persisted summary; never rescan source logs or attachment ledgers."""
    context=context or require_active_workspace()
    try:
        state=read_object(context.sync/'codex-source-adapter.json')
        if (context.sync/'codex-source-adapter.json').exists() and (state.get('archive_policy') not in ARCHIVE_POLICIES or not isinstance(state.get('sessions'),dict)):
            raise StateCorruptError('Codex 归档状态结构损坏；请核验原进度后恢复')
        registry=read_object(context.archive_registry, {'sessions':{}})
        if not isinstance(registry.get('sessions'),dict):
            raise StateCorruptError('会话目录表结构损坏；已停止写入以防重复建档')
    except StateCorruptError as exc:
        return {'adapter':'codex','status':'state_corrupt','enabled':False,'archive_policy':None,
                'last_error':str(exc),'recovery_required':True,'snapshot_available':False}
    snapshot=state.get('status_snapshot') or {}
    policy=state.get('archive_policy','paused')
    from .codex_history import history_summary
    format_review=snapshot.get('format_review') or {}
    history=history_summary(state,format_review)
    fields={'adapter','parser_version','discovered_session_count','discovered_fragment_count','authorized_session_count','excluded_session_count','last_sync_at','last_message_sync_at','last_sync_message_count','total_archived_message_count','total_attachment_count','missing_attachment_count','failed_session_count','needs_review_event_count','needs_review_session_count','gap_detected','body_complete_session_count','format_review'}
    result={k:v for k,v in snapshot.items() if k in fields}
    result.update(adapter='codex',archive_policy=policy,resume_policy=state.get('resume_policy','selected'),enabled=policy!='paused',last_error=state.get('last_error'),snapshot_updated_at=snapshot.get('last_sync_at'),configuration_updated_at=state.get('updated_at'),last_sync_at=state.get('last_sync_at'),live=state.get('live_progress',{}))
    result['total_archived_message_count'] = sum(int(item.get('message_count') or 0)
        for item in registry['sessions'].values() if item.get('source_key') == 'codex')
    result['history']={k:v for k,v in history.items() if k!='items'}
    issue_items=[row for row in history.get('items',[]) if row.get('status') in {'failed','needs_review','source_missing','excluded'}]
    result['history']['issue_total']=len(issue_items)
    result['history']['items']=[{k:v for k,v in row.items() if k in {'session_id','status','error','source_file'}} for row in issue_items[:30]]
    result['history']['details_limited']=len(issue_items)>30
    result['discovered_sessions']=[{k:v for k,v in row.items() if k in {'session_id','title','title_source','authorized','excluded','fragment_count'}} for row in snapshot.get('discovered_sessions',[])]
    result['current_cursors']={sid:{k:v for k,v in row.items() if k in {'body_status','archived_message_count','attachment_failed_count','last_error'}}|{'missing_attachment_count':len(row.get('missing_attachments',[]))} for sid,row in state.get('sessions',{}).items()}
    for sid in format_review.get('verified_session_ids',[]):
        if sid in result['current_cursors'] and result['current_cursors'][sid].get('body_status')=='needs_review':
            result['current_cursors'][sid]['body_status']='complete'
    references=snapshot.get('attachment_references') or {}
    result['attachment_references']={k:v for k,v in references.items() if k not in {'unavailable'}}
    backlog=[]
    eligible={str(row.get('session_id')) for row in snapshot.get('discovered_sessions',[]) if row.get('authorized') and not row.get('excluded')}
    for sid,session in state.get('sessions',{}).items():
        if sid not in eligible:continue
        by_file={}
        for fragment in session.get('fragments',{}).values():
            path=fragment.get('source_file');saved=by_file.setdefault(path,{'size':0,'offset':0})
            saved['size']=max(saved['size'],int(fragment.get('file_size') or 0));saved['offset']=max(saved['offset'],int(fragment.get('confirmed_offset') or 0))
        for path,saved in by_file.items():
            pending=max(0,saved['size']-saved['offset'])
            if pending:backlog.append({'session_id':sid,'source_file':path,'pending_bytes':pending})
    result['live_backlog']={'pending_bytes':sum(r['pending_bytes'] for r in backlog),'source_fragments':len(backlog),'details':backlog[:30]}
    result['details_loaded']=False
    result['snapshot_available']=bool(snapshot)
    result['notice']='显示后台最近一次核对的状态；页面请求不重新扫描原文或附件。文件引用详情按需读取。'
    return result


def status_issues(context=None, offset=0, limit=30):
    """Page persisted import issues without scanning source conversations."""
    context=context or require_active_workspace()
    offset=max(0,int(offset));limit=max(1,min(100,int(limit)))
    state=read_object(context.sync/'codex-source-adapter.json')
    from .codex_history import history_summary
    review=(state.get('status_snapshot') or {}).get('format_review') or {}
    history=history_summary(state,review)
    rows=[{k:v for k,v in row.items() if k in {'session_id','status','error','source_file'}}
          for row in history.get('items',[]) if row.get('status') in {'failed','needs_review','source_missing','excluded'}]
    return {'items':rows[offset:offset+limit],'total':len(rows),'offset':offset,
            'has_more':offset+limit<len(rows),'next_offset':offset+limit if offset+limit<len(rows) else None}
