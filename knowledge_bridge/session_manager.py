from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .workspace import active_workspace


_WORKSPACE = active_workspace()
SESSION_ROOT = _WORKSPACE.state
SESSIONS_PATH = _WORKSPACE.sessions_state
ARCHIVE_ROOT = _WORKSPACE.raw_message_store
_lock = threading.RLock()


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def estimate_tokens(text: str) -> int:
    """Local conservative estimate; never presented as provider usage."""
    if not text:
        return 0
    cjk = sum(1 for char in text if "\u3400" <= char <= "\u9fff")
    other = len(text) - cjk
    return max(1, round(cjk * 0.9 + other / 4))


def _atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", "utf-8")
    try:
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.02 * (attempt + 1))
    finally:
        if tmp.exists():
            tmp.unlink()


def _load(path: Path = SESSIONS_PATH) -> dict:
    try:
        data = json.loads(path.read_text("utf-8"))
        return data if isinstance(data, dict) else {"sessions": {}}
    except (OSError, ValueError):
        return {"sessions": {}}


def _save(data: dict, path: Path = SESSIONS_PATH) -> None:
    _atomic_json(path, data)


def _archive_path(session_id: str, root: Path = ARCHIVE_ROOT) -> Path:
    safe = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:20]
    return root / f"{safe}.jsonl"


def _read_archive(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def _append_archive(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def session_start(session_id: str, client_id: str, source: str, memory_mode: str = "readonly",
                  current_project: str | None = None, session_title: str | None = None, *, state_path: Path = SESSIONS_PATH,
                  archive_root: Path = ARCHIVE_ROOT) -> dict:
    if memory_mode not in {"readonly", "full"}:
        raise ValueError("记忆模式只能是 readonly 或 full")
    if not session_id.strip() or not client_id.strip() or not source.strip():
        raise ValueError("session_id、client_id 和 source 不能为空")
    with _lock:
        data = _load(state_path)
        existing = data.setdefault("sessions", {}).get(session_id)
        if existing:
            if existing["client_id"] != client_id or existing["source"] != source:
                raise ValueError("会话ID已存在且来源冲突")
            if session_title is not None and existing.get("session_title") != session_title:
                existing["session_title"] = session_title
                _save(data, state_path)
            return existing
        now = utcnow()
        item = {
            "session_id": session_id, "session_title": session_title, "client_id": client_id, "source": source,
            "started_at": now, "last_activity_at": now,
            "user_message_count": 0, "assistant_message_count": 0, "turn_count": 0,
            "actual_tokens": None, "estimated_tokens": 0,
            "last_bootstrap_at": None, "last_weight_boost_at": None,
            "last_weight_boost_turn": None, "current_project": current_project,
            "memory_mode": memory_mode, "raw_archive_status": "active" if memory_mode == "full" else "unavailable",
            "refresh_status": "normal", "ended_at": None,
            "core_status":"pending","core_required":True,"core_version":None,"core_read_at":None,"core_read_mode":None,
        }
        data["sessions"][session_id] = item
        _save(data, state_path)
        return item


def update_session_metadata(session_id: str, *, session_title: str | None = None,
                            source_last_activity_at: str | None = None,
                            state_path: Path = SESSIONS_PATH) -> dict:
    """Synchronize display metadata without changing message evidence or per-session counters."""
    with _lock:
        data = _load(state_path)
        session = data.get("sessions", {}).get(session_id)
        if not session:
            raise ValueError("会话不存在")
        session["session_title"] = session_title or None
        if source_last_activity_at:
            session["last_activity_at"] = source_last_activity_at
        _save(data, state_path)
        return session_status(session_id, state_path=state_path)


def session_append(session_id: str, message_id: str, role: str, content: str, timestamp: str,
                   attachment_refs: list[str] | None = None, token_usage: int | None = None,
                   provenance: dict | None = None, *, state_path: Path = SESSIONS_PATH,
                   archive_root: Path = ARCHIVE_ROOT, materialize_archive: bool = True) -> dict:
    if role not in {"user", "assistant"} and not (role=="system" and (provenance or {}).get("adapter")=="hermes_sqlite_readonly" and (provenance or {}).get("display_semantics")=="system_notice"):
        raise ValueError("role只能是 user、assistant 或已核验的Hermes系统通知")
    if not message_id.strip() or not isinstance(content, str):
        raise ValueError("message_id不能为空，content必须为原始字符串")
    if token_usage is not None and (not isinstance(token_usage, int) or token_usage < 0):
        raise ValueError("token_usage必须为非负整数")
    should_materialize = materialize_archive and state_path == SESSIONS_PATH and archive_root == ARCHIVE_ROOT
    with _lock:
        data = _load(state_path)
        session = data.setdefault("sessions", {}).get(session_id)
        if not session:
            raise ValueError("会话不存在")
        if session["memory_mode"] != "full":
            raise PermissionError("只读模式不接收完整对话")
        archive = _archive_path(session_id, archive_root)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        existing = next((row for row in _read_archive(archive) if row["message_id"] == message_id), None)
        if existing:
            if existing["content_sha256"] != digest or existing["role"] != role:
                raise ValueError("同一message_id的原文冲突")
            result = {"saved": False, "duplicate": True, "session": session, "record": existing}
            if should_materialize:
                from .session_archive import materialize
                materialize(session_id)
            return result
        record = {
            "session_id": session_id, "message_id": message_id, "role": role,
            "content": content, "original_text": content, "timestamp": timestamp, "attachment_refs": attachment_refs or [],
            "token_usage": token_usage, "provenance": provenance or {}, "content_sha256": digest,
            "message_order": session["user_message_count"] + session["assistant_message_count"] + session.get("system_message_count",0) + 1,
            "ingest_timestamp": utcnow(),
        }
        _append_archive(archive, record)
        session[f"{role}_message_count"] = session.get(f"{role}_message_count",0) + 1
        session["turn_count"] = min(session["user_message_count"], session["assistant_message_count"])
        session["estimated_tokens"] += estimate_tokens(content)
        if token_usage is not None:
            session["actual_tokens"] = int(session["actual_tokens"] or 0) + token_usage
        session["last_activity_at"] = timestamp
        session["raw_archive_status"] = "saved"
        _save(data, state_path)
        result = {"saved": True, "duplicate": False, "session": session, "record": record}
    if should_materialize:
        from .session_archive import materialize
        result["l0_archive"] = materialize(session_id)["archive_path"]
    return result


def session_status(session_id: str, *, state_path: Path = SESSIONS_PATH) -> dict:
    session = _load(state_path).get("sessions", {}).get(session_id)
    if not session:
        raise ValueError("会话不存在")
    full = session["memory_mode"] == "full"
    token_kind = "actual" if session["actual_tokens"] is not None else "estimated" if full else "unavailable"
    token_value = session["actual_tokens"] if token_kind == "actual" else session["estimated_tokens"] if token_kind == "estimated" else None
    return session | {
        "turn_count_available": full,
        "display_turn_count": session["turn_count"] if full else None,
        "token_kind": token_kind, "display_tokens": token_value,
        "raw_archive_available": full,
    }


def list_sessions(client_id: str | None = None, *, state_path: Path = SESSIONS_PATH) -> list[dict]:
    rows = list(_load(state_path).get("sessions", {}).values())
    if client_id:
        rows = [row for row in rows if row["client_id"] == client_id]
    return sorted(rows, key=lambda row: row["last_activity_at"], reverse=True)


def session_end(session_id: str, *, state_path: Path = SESSIONS_PATH) -> dict:
    with _lock:
        data = _load(state_path); session = data.get("sessions", {}).get(session_id)
        if not session: raise ValueError("会话不存在")
        session["ended_at"] = utcnow(); session["raw_archive_status"] = "closed" if session["memory_mode"] == "full" else "unavailable"
        _save(data, state_path); return session_status(session_id, state_path=state_path)


def record_bootstrap(session_id: str, version: str|None=None, mode:str="manual", *, state_path: Path = SESSIONS_PATH) -> dict:
    with _lock:
        data = _load(state_path); session = data.get("sessions", {}).get(session_id)
        if not session: raise ValueError("会话不存在")
        session["last_bootstrap_at"] = utcnow()
        session.update({"core_status":"read_manual" if mode=="manual" else "read_automatic","core_required":False,"core_version":version,"core_read_at":session["last_bootstrap_at"],"core_read_mode":mode})
        if session.get("refresh_status") == "refresh_required":
            session["refresh_status"] = "boosted"; session["last_weight_boost_at"] = session["last_bootstrap_at"]
            session["last_weight_boost_turn"] = session["turn_count"]
        _save(data, state_path); return session_status(session_id, state_path=state_path)


def refresh_memory_context(session_id: str, supports_push: bool = False, *, state_path: Path = SESSIONS_PATH) -> dict:
    with _lock:
        data = _load(state_path); session = data.get("sessions", {}).get(session_id)
        if not session: raise ValueError("会话不存在")
        now = utcnow()
        if supports_push:
            session.update({"refresh_status": "boosted", "last_weight_boost_at": now, "last_weight_boost_turn": session["turn_count"]})
        else:
            session["refresh_status"] = "refresh_required"
        _save(data, state_path)
        return {"status": session["refresh_status"], "executed": supports_push, "session": session_status(session_id, state_path=state_path)}
