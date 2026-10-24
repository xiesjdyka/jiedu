"""Owner-only view of standard chat messages plus private visible progress."""
from __future__ import annotations

import json
from pathlib import Path

from .archive_store import ArchiveStore
from .workspace import WorkspaceContext, require_active_workspace


def _records(context: WorkspaceContext, source: str):
    if source not in {"dsh", "cursor", "workbuddy", "opencode", "zcode", "kimi"}:
        raise ValueError("该来源没有可查看的进度原文")
    store = ArchiveStore(context, recover=False)
    sessions = [item for item in store._registry().get("sessions", {}).values()
                if item.get("source_key") == source]
    sessions.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    return store, sessions[:100], len(sessions) > 100


def _progress_index(context, source):
    folder = context.sync / (source + "-visible-progress")
    if not folder.is_dir():
        return {}, set(), False
    result, more = {}, set()
    scanned = 0
    global_more = False
    for path in folder.glob("*.json"):
        scanned += 1
        if scanned > 10000:
            global_more = True
            break
        if path.is_symlink():
            continue
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        sid = item.get("session_id")
        if item.get("source") == source and isinstance(sid, str) and isinstance(item.get("text"), str):
            bucket = result.setdefault(sid, [])
            if len(bucket) >= 2000:
                more.add(sid)
            else:
                bucket.append(item)
    return result, more, global_more


def list_conversations(source, context: WorkspaceContext | None = None):
    context = context or require_active_workspace()
    store, sessions, session_more = _records(context, source)
    progress, progress_more, global_more = _progress_index(context, source)
    return {"source": source, "sessions": [{"session_id": item.get("source_session_id"),
             "title": item.get("title"), "saved_messages": item.get("message_count", 0),
             "visible_progress": _progress_count(context, source, item, store,
                                                   progress.get(item.get("source_session_id"), [])),
             "progress_has_more": item.get("source_session_id") in progress_more or global_more}
             for item in sessions], "has_more_sessions": session_more,
             "progress_scan_truncated": global_more,
             "note": "进度原文不进入标准档案、普通检索或历史提炼"}


def _progress_count(context, source, item, store, progress):
    rows = store._message_rows(context.raw_message_store / (item["session_key"] + ".jsonl"))
    final_ids = {(r.get("provenance") or {}).get("source_seq") for r in rows
                 if r.get("role") == "assistant" and (r.get("provenance") or {}).get("source_seq") is not None}
    final_bubbles = {(r.get("provenance") or {}).get("bubble_id") for r in rows
                     if r.get("role") == "assistant" and (r.get("provenance") or {}).get("bubble_id") is not None}
    return sum(1 for x in progress
               if (x.get("source_seq") is None or x["source_seq"] not in final_ids) and
                  (x.get("bubble_id") is None or x["bubble_id"] not in final_bubbles))


def conversation(source, sid, context: WorkspaceContext | None = None):
    context = context or require_active_workspace()
    store, sessions, session_more = _records(context, source)
    item = next((x for x in sessions if x.get("source_session_id") == sid), None)
    if item is None:
        raise ValueError("会话不存在或尚未获准归档")
    key = item["session_key"]
    rows = store._message_rows(context.raw_message_store / (key + ".jsonl"))
    final_ids = {(row.get("provenance") or {}).get("source_seq") for row in rows
                 if row.get("role") == "assistant" and (row.get("provenance") or {}).get("source_seq") is not None}
    final_bubbles = {(row.get("provenance") or {}).get("bubble_id") for row in rows
                     if row.get("role") == "assistant" and (row.get("provenance") or {}).get("bubble_id") is not None}
    timeline = [{"role": row.get("role"), "text": row.get("original_text"),
                 "timestamp": row.get("timestamp"), "turn": (row.get("provenance") or {}).get("turn"),
                 "seq": (row.get("provenance") or {}).get("source_seq"),
                 "position": (row.get("provenance") or {}).get("position"),
                 "source_offset": (row.get("source_position") or {}).get("byte_offset"),
                 "message_id": row.get("message_id")}
                for row in rows if row.get("role") in {"user", "assistant"}]
    indexed, more, global_more = _progress_index(context, source)
    progress = [x for x in indexed.get(sid, [])
                if (x.get("source_seq") is None or x["source_seq"] not in final_ids) and
                   (x.get("bubble_id") is None or x["bubble_id"] not in final_bubbles)]
    timeline.extend({"role": "assistant_progress", "text": x["text"],
                     "timestamp": x.get("timestamp"), "turn": x.get("turn"),
                     "seq": x.get("source_seq"), "position": x.get("position"),
                     "source_offset": x.get("source_offset"),
                     "message_id": None} for x in progress)
    if source == "cursor":
        timeline.sort(key=lambda x: (int(x.get("turn") or 0), int(x.get("position") or 0)))
    elif source in {"workbuddy", "opencode", "zcode", "kimi"}:
        timeline.sort(key=lambda x: (float(x.get("source_offset")) if x.get("source_offset") is not None else float("inf"),
                                     str(x.get("timestamp") or "")))
    else:
        timeline.sort(key=lambda x: (int(x.get("seq") or 0), str(x.get("timestamp") or "")))
    return {"source": source, "session_id": sid, "title": item.get("title"),
            "saved_messages": len(rows), "visible_progress": len(progress),
            "progress_has_more": sid in more or global_more,
            "messages": timeline, "note": "进度原文仅供查看，不参与普通检索或历史提炼"}
