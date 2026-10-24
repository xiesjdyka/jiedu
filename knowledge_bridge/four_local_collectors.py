"""Opt-in, read-only local collectors for WorkBuddy, OpenCode, ZCode and Kimi Work.

Only a source-proven completed user/final pair enters the standard archive.
Reasoning, tools, system records and unfinished turns never enter that archive.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock

from .archive_store import ArchiveStore, NormalizedAttachment, NormalizedMessage
from .durable_state import StateCorruptError, read_object, write_object
from .workspace import WorkspaceContext, require_active_workspace


SOURCES = {"workbuddy": "WorkBuddy", "opencode": "OpenCode",
           "zcode": "ZCode", "kimi": "Kimi Work"}
PARSER_VERSION = "four-local-completed-v1"
MAX_TEXT = 512 * 1024
MAX_LINE = 8 * 1024 * 1024
MAX_IMAGE_BYTES = 25 * 1024 * 1024
ATTACHMENT_SCAN_VERSION = 1
_stop = threading.Event()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _text(value):
    return value if isinstance(value, str) and value.strip() and len(value.encode("utf-8")) <= MAX_TEXT else None


def _stamp(value):
    if isinstance(value, str):
        return value[:80]
    if isinstance(value, (int, float)) and value > 0:
        return datetime.fromtimestamp(value / (1000 if value > 10_000_000_000 else 1), timezone.utc).isoformat()
    return None


def _db(path):
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
    conn.execute("pragma query_only=ON")
    conn.execute("begin")
    return conn


def _json(value):
    row = json.loads(value)
    if not isinstance(row, dict):
        raise ValueError("本机聊天记录结构无效")
    return row


def _parts(parts, kind):
    if not isinstance(parts, list):
        return []
    return [(index, item["text"]) for index, item in enumerate(parts)
            if isinstance(item, dict) and item.get("type") == kind and _text(item.get("text"))]


def _user_query(value):
    value = _text(value)
    if not value:
        return None
    match = re.search(r"<user_query>\s*(.*?)\s*</user_query>", value, re.DOTALL)
    if match:
        return _text(match.group(1))
    # A wrapper with no explicit user span may contain injected instructions.
    if "<system_reminder" in value or "<system-reminder" in value or "<user_query" in value:
        return None
    return value


def _turn(user_id, user_text, user_time, final_id, final_text, final_time, source_order, progress=(), attachments=()):
    if not all((_text(user_id), _text(final_id), _text(final_text))) or not (_text(user_text) or attachments):
        return None
    if not isinstance(source_order, int) or source_order < 0:
        return None
    return {"user_id": user_id, "user_text": user_text or "", "user_time": _stamp(user_time),
            "final_id": final_id, "final_text": final_text, "final_time": _stamp(final_time),
            "source_order": source_order, "progress": tuple(progress),
            "attachments": tuple(attachments)}


def _workbuddy_images(parts, source_root):
    """Resolve only source-owned, hash-verified user image blobs."""
    result = []
    allowed = source_root.resolve()
    for part in parts if isinstance(parts, list) else []:
        if not isinstance(part, dict) or part.get("type") != "input_image" or part.get("synthetic") or \
                part.get("visibility") == "hidden" or \
                (part.get("semantics") or {}).get("transcriptVisibility") == "hidden":
            continue
        image = part.get("image")
        if not isinstance(image, dict) or not str(image.get("mime") or "").startswith("image/"):
            continue
        blob_id = image.get("blob_id")
        if not isinstance(blob_id, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", blob_id):
            continue
        filename = image.get("original_filename") or part.get("original_filename") or "图片.png"
        name = Path(str(filename)).name[:180] or "图片.png"
        candidate = Path(str(image.get("blob_path") or ""))
        size = image.get("size")
        local = None
        try:
            resolved = candidate.resolve()
            if (candidate.is_absolute() and resolved.is_relative_to(allowed) and
                    not candidate.is_symlink() and not any(parent.is_symlink() for parent in candidate.parents if parent == allowed or allowed in parent.parents) and
                    candidate.is_file() and type(size) is int and 0 < size <= MAX_IMAGE_BYTES and
                    candidate.stat().st_size == size):
                with candidate.open("rb") as stream:
                    digest = hashlib.sha256()
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(block)
                if digest.hexdigest().casefold() == blob_id.casefold():
                    local = candidate
        except (OSError, ValueError):
            local = None
        result.append(NormalizedAttachment(reference="workbuddy:" + blob_id.casefold(),
                                           original_name=name, local_path=local,
                                           mime_type=image["mime"], media_type="image",
                                           expected_sha256=blob_id.casefold() if local else None,
                                           expected_size=size if local else None))
    return tuple(result)


def _zcode_images(parts):
    """Keep unresolved provider artifact references visible without guessing bytes."""
    result = []
    for part in parts:
        if not isinstance(part, dict) or part.get("type") != "file":
            continue
        mime = part.get("mime")
        url = part.get("url")
        if not isinstance(mime, str) or not mime.startswith("image/") or \
                not isinstance(url, str) or not url.startswith("zcode-artifact:"):
            continue
        extension = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
                     "image/gif": ".gif"}.get(mime, "")
        result.append(NormalizedAttachment(reference="zcode-artifact:" + hashlib.sha256(url.encode()).hexdigest(),
                                           original_name="ZCode 图片引用" + extension,
                                           mime_type=mime, media_type="image",
                                           original_name_source="source_reference"))
    return tuple(result)


def _jsonl(path):
    pending_tail = False
    with path.open("rb") as stream:
        for number, raw in enumerate(stream):
            if len(raw) > MAX_LINE:
                raise ValueError("来源转录行超过安全上限")
            if not raw.endswith(b"\n"):
                pending_tail = True
                break
            try:
                yield number, _json(raw)
            except (UnicodeError, ValueError) as exc:
                raise ValueError("来源已完成转录行无法解析") from exc
    if pending_tail:
        yield -1, {"type": "__pending_tail__"}


def _workbuddy(path, sid):
    turns, current, pending, unverified, extra_progress = [], None, 0, 0, []
    for line, row in _jsonl(path):
        if row.get("type") == "__pending_tail__":
            pending += 1
            break
        if row.get("type") != "message" or row.get("sessionId") != sid:
            continue
        if row.get("role") == "user":
            if current:
                pending += 1
                extra_progress.extend((current["order"], *entry) for entry in current["progress"])
            items = _parts(row.get("content"), "input_text")
            text = _user_query("\n".join(part for _, part in items)) if items else None
            current = {"id": row.get("id"), "text": text, "time": row.get("timestamp"), "order": line,
                       "progress": [], "attachments": _workbuddy_images(row.get("content"), path.parents[2])}
        elif row.get("role") == "assistant" and current:
            if row.get("status") != "completed":
                current["progress"].extend(
                    (f"{row.get('id')}:{i}", text, row.get("timestamp"))
                    for i, text in _parts(row.get("content"), "output_text"))
                if row.get("status") in {"failed", "cancelled", "aborted"}:
                    unverified += 1
                    extra_progress.extend((current["order"], *entry) for entry in current["progress"])
                    current = None
                continue
            texts = _parts(row.get("content"), "output_text")
            final = "".join(text for _, text in texts) if texts else None
            turn = _turn(current["id"], current["text"], current["time"],
                         row.get("id"), final, row.get("timestamp"), current["order"], current["progress"],
                         current["attachments"])
            if turn:
                turns.append(turn)
            else:
                unverified += 1
            current = None
    if current:
        pending += 1
        extra_progress.extend((current["order"], *entry) for entry in current["progress"])
    return turns, pending, unverified, extra_progress


def _opencode(db, sid):
    turns, current, pending, unverified, extra_progress = [], None, 0, 0, []
    for mid, kind, seq, raw in db.execute(
            "select id,type,seq,data from session_message where session_id=? order by seq,id", (sid,)):
        data = _json(raw)
        if kind == "user":
            if current:
                pending += 1
                extra_progress.extend((current["order"], *entry) for entry in current["progress"])
            current = {"id": mid, "text": _text(data.get("text")),
                       "time": (data.get("time") or {}).get("created"), "order": seq,
                       "assistant": None, "progress": []}
        elif kind == "assistant" and current:
            content = _parts(data.get("content"), "text")
            complete = data.get("finish") == "stop" and bool((data.get("time") or {}).get("completed"))
            if complete and content:
                current["assistant"] = (mid, "".join(text for _, text in content),
                                        data["time"]["completed"])
            elif not complete:
                current["progress"].extend((f"{mid}:{i}", text, (data.get("time") or {}).get("created"))
                                           for i, text in content)
        elif kind == "idle" and current:
            if data.get("outcome") == "succeeded" and current["assistant"]:
                aid, final, at = current["assistant"]
                turn = _turn(current["id"], current["text"], current["time"],
                             aid, final, at, current["order"], current["progress"])
                if turn:
                    turns.append(turn)
                else:
                    unverified += 1
            elif data.get("outcome") == "succeeded":
                unverified += 1
            else:
                unverified += 1
            if data.get("outcome") != "succeeded" or not current["assistant"]:
                extra_progress.extend((current["order"], *entry) for entry in current["progress"])
            current = None
    if current:
        pending += 1
        extra_progress.extend((current["order"], *entry) for entry in current["progress"])
    return turns, pending, unverified, extra_progress


def _zcode(db, sid):
    turns, current, pending, unverified, extra_progress = [], None, 0, 0, []
    for mid, raw, seq in db.execute("select id,data,sequence from message where session_id=? order by sequence,id", (sid,)):
        data = _json(raw)
        role = data.get("role")
        semantics = data.get("semantics") or {}
        visible = semantics.get("uiVisibility") == "visible" and semantics.get("transcriptVisibility") == "visible"
        if role == "user" and not data.get("synthetic") and semantics.get("origin") == "real_user" and visible:
            if current:
                pending += 1
                extra_progress.extend((current["order"], *entry) for entry in current["progress"])
            parts = _zcode_parts(db, mid)
            texts = _parts(parts, "text")
            current = {"id": mid, "text": "".join(text for _, text in texts) if texts else None,
                       "time": (data.get("time") or {}).get("created"), "order": seq,
                       "progress": [], "attachments": _zcode_images(parts)}
        elif role == "assistant" and current and visible:
            if data.get("finish") != "stop" or not (data.get("time") or {}).get("completed"):
                current["progress"].extend((f"{mid}:{i}", text, (data.get("time") or {}).get("created"))
                                           for i, text in _parts(_zcode_parts(db, mid), "text"))
                continue
            parts = _zcode_parts(db, mid)
            texts = _parts(parts, "text")
            final = "".join(text for _, text in texts) if texts else None
            turn = _turn(current["id"], current["text"], current["time"],
                         mid, final, data["time"]["completed"], current["order"], current["progress"],
                         current["attachments"])
            if turn:
                turns.append(turn)
            else:
                unverified += 1
            current = None
    if current:
        pending += 1
        extra_progress.extend((current["order"], *entry) for entry in current["progress"])
    return turns, pending, unverified, extra_progress


def _zcode_parts(db, mid):
    rows = []
    for (raw,) in db.execute("select data from part where message_id=? order by sequence,id", (mid,)):
        part = _json(raw)
        if (part.get("synthetic") or part.get("visibility") == "hidden" or
                (part.get("semantics") or {}).get("transcriptVisibility") == "hidden" or
                (part.get("type") == "text" and part.get("metadata"))):
            continue
        rows.append(part)
    return rows


def _kimi(path):
    turns, current, pending, unverified, extra_progress = [], None, 0, 0, []
    for line, row in _jsonl(path):
        kind = row.get("type")
        if kind == "__pending_tail__":
            pending += 1
            break
        if kind == "turn.prompt":
            if current:
                pending += 1
                extra_progress.extend((current["order"], *entry) for entry in current["progress"])
            origin = row.get("origin") or {}
            if not isinstance(origin, dict) or origin.get("kind") != "user":
                current = None
                continue
            parts = _parts(row.get("input"), "text")
            raw = "\n".join(text for _, text in parts)
            raw = re.sub(r"^\s*<meta\s+awareness\b[^>]*?/?>\s*", "", raw, flags=re.I)
            current = {"id": f"prompt:{line}", "text": _text(raw), "time": row.get("time"),
                       "order": line,
                       "progress": [], "active_step": None}
        elif kind == "context.append_loop_event" and current:
            event = row.get("event") or {}
            if not isinstance(event, dict):
                continue
            if event.get("type") == "step.begin":
                if current["active_step"] is not None:
                    unverified += 1
                    extra_progress.extend((current["order"], *entry) for entry in current["progress"])
                    current = None
                    continue
                if not isinstance(event.get("uuid"), str) or not isinstance(event.get("turnId"), str):
                    unverified += 1
                    extra_progress.extend((current["order"], *entry) for entry in current["progress"])
                    current = None
                    continue
                current["active_step"] = {"uuid": event["uuid"], "turn_id": event["turnId"],
                                           "texts": []}
            elif event.get("type") == "content.part":
                step = current["active_step"]
                if not step or event.get("stepUuid") != step["uuid"] or event.get("turnId") != step["turn_id"]:
                    unverified += 1
                    extra_progress.extend((current["order"], *entry) for entry in current["progress"])
                    current = None
                    continue
                part = event.get("part") or {}
                if isinstance(part, dict) and part.get("type") == "text" and _text(part.get("text")):
                    step["texts"].append(part["text"])
            elif event.get("type") == "step.end":
                step = current["active_step"]
                if not step or event.get("uuid") != step["uuid"] or event.get("turnId") != step["turn_id"]:
                    unverified += 1
                    extra_progress.extend((current["order"], *entry) for entry in current["progress"])
                    current = None
                    continue
                text = "".join(step["texts"])
                if event.get("finishReason") == "end_turn":
                    turn = _turn(current["id"], current["text"], current["time"],
                                 event.get("messageId"), text, row.get("time"),
                                 current["order"], current["progress"])
                    if turn:
                        turns.append(turn)
                    else:
                        unverified += 1
                    current = None
                elif event.get("finishReason") in {"cancelled", "error", "failed"}:
                    unverified += 1
                    extra_progress.extend((current["order"], *entry) for entry in current["progress"])
                    current = None
                else:
                    if _text(text):
                        current["progress"].append((step["uuid"], text, row.get("time")))
                    current["active_step"] = None
    if current:
        pending += 1
        extra_progress.extend((current["order"], *entry) for entry in current["progress"])
    return turns, pending, unverified, extra_progress


class LocalCompletedCollector:
    def __init__(self, source: str, context: WorkspaceContext | None = None, *, source_root: Path | None = None):
        if source not in SOURCES:
            raise ValueError("未知本机聊天来源")
        self.source = source
        self.context = context or require_active_workspace()
        default = Path(os.getenv("APPDATA", Path.home() / "AppData/Roaming")) / "kimi-desktop" if source == "kimi" else Path.home()
        self.root = Path(source_root or default).expanduser().resolve()
        self.state_path = self.context.sync / f"{source}-source-adapter.json"
        self.lock = FileLock(str(self.context.sync / f"{source}-source-adapter.lock"))
        self.progress_dir = self.context.sync / f"{source}-visible-progress"

    def _load(self):
        row = read_object(self.state_path)
        if self.state_path.exists() and (row.get("policy") not in {"paused", "all", "selected"} or
                                         not isinstance(row.get("sessions"), dict)):
            raise StateCorruptError("采集状态损坏；原游标保留")
        return {"policy": row.get("policy", "paused"), "resume_policy": row.get("resume_policy", "selected"),
                "selected_session_ids": row.get("selected_session_ids", []),
                "selected_sources": row.get("selected_sources", {}),
                "sessions": row.get("sessions", {}), "last_sync_at": row.get("last_sync_at"),
                "last_error": row.get("last_error")}

    def _paths(self):
        home = self.root
        if self.source == "workbuddy":
            return home / ".workbuddy/workbuddy.db", home / ".workbuddy/projects"
        if self.source == "opencode":
            return home / ".local/share/opencode/opencode.db", None
        if self.source == "zcode":
            return home / ".zcode/cli/db/db.sqlite", home / ".zcode/v2/tasks-index.sqlite"
        return home / "daimon-share/daimon/agents/main/sessions/hosted-logical/conversations.sqlite", home / "daimon-share/daimon/runtime"

    def discover(self):
        database, auxiliary = self._paths()
        if not database.is_file():
            return []
        items = []
        with closing(_db(database)) as db:
            if self.source == "workbuddy":
                files = {}
                if auxiliary and auxiliary.is_dir():
                    for path in auxiliary.glob("*/*.jsonl"):
                        if path.is_file() and not path.is_symlink():
                            if path.stem in files:
                                raise ValueError("WorkBuddy 会话身份重复")
                            files[path.stem] = path
                for sid, title, updated in db.execute("select id,coalesce(custom_title,title),updated_at from sessions where deleted_at is null"):
                    path = files.get(sid)
                    if path:
                        items.append({"session_id": sid, "path": str(path.resolve()), "title": title or "WorkBuddy",
                                      "updated": updated, "source_bytes": path.stat().st_size})
            elif self.source == "opencode":
                for sid, title, updated in db.execute("select id,title,time_updated from session_v2"):
                    count = db.execute("select count(*) from session_message where session_id=?", (sid,)).fetchone()[0]
                    items.append({"session_id": sid, "path": str(database.resolve()),
                                  "title": title or "OpenCode", "updated": updated,
                                  "available_messages": count})
            elif self.source == "zcode":
                for sid, title, updated in db.execute("select id,title,time_updated from session where time_deleted is null"):
                    count = db.execute("select count(*) from message where session_id=?", (sid,)).fetchone()[0]
                    items.append({"session_id": sid, "path": str(database.resolve()),
                                  "title": title or "ZCode", "updated": updated,
                                  "available_messages": count})
            else:
                allowed = auxiliary.resolve() if auxiliary else None
                for sid, title, wire, updated in db.execute(
                        "select conversation_key,title,kernel_records_path,updated_at_ms from conversations"):
                    if not isinstance(wire, str):
                        continue
                    path = Path(wire)
                    resolved = path.resolve()
                    if not path.is_file() or path.is_symlink() or not allowed or not resolved.is_relative_to(allowed):
                        continue
                    items.append({"session_id": sid, "path": str(resolved), "title": title or "Kimi Work",
                                  "updated": updated, "source_bytes": path.stat().st_size})
        if len({x["session_id"] for x in items}) != len(items):
            raise ValueError("来源会话身份重复")
        return items

    def _indexed_gaps(self, found, state):
        if self.source not in {"workbuddy", "kimi"}:
            return {"count": 0, "in_scope_count": 0, "sample": []}
        database, _ = self._paths()
        if not database.is_file():
            return {"count": 0, "in_scope_count": 0, "sample": []}
        known = {item["session_id"] for item in found}
        selected = set(state["selected_session_ids"])
        count, in_scope, sample = 0, 0, []
        with closing(_db(database)) as db:
            if self.source == "workbuddy":
                rows = db.execute("select id from sessions where deleted_at is null")
            else:
                rows = db.execute("select conversation_key from conversations")
            for (sid,) in rows:
                if sid in known:
                    continue
                count += 1
                if state["policy"] == "all" or sid in selected:
                    in_scope += 1
                if len(sample) < 20:
                    sample.append({"session_id": sid, "verification": "索引存在但转录不可读，暂未入档"})
        return {"count": count, "in_scope_count": in_scope, "sample": sample}

    def status(self):
        state = self._load()
        found = self.discover()
        gaps = self._indexed_gaps(found, state)
        rows = state["sessions"]
        attachment_available = sum(int(r.get("attachment_available") or 0) for r in rows.values())
        attachment_missing = sum(int(r.get("attachment_missing") or 0) for r in rows.values())
        return {"adapter": self.source, "display_name": SOURCES[self.source],
                "enabled": state["policy"] != "paused", "ever_configured": self.state_path.exists(),
                "policy": state["policy"], "resume_policy": state["resume_policy"],
                "selected_session_ids": state["selected_session_ids"],
                "source_detected": bool(found or gaps["count"]), "source_readable": bool(found),
                "discovered_sessions": found,
                "indexed_without_transcript": gaps,
                "history": {"known_sessions": len(found), "checked_sessions": len(rows),
                            "unverified_indexed_sessions": gaps["in_scope_count"],
                            "archived_messages": sum(int(r.get("saved_messages") or 0) for r in rows.values()),
                            "pending_turns": sum(int(r.get("pending_turns") or 0) for r in rows.values()),
                            "unverified_turns": sum(int(r.get("unverified_turns") or 0) for r in rows.values()),
                            "total_is_dynamic": True},
                "attachments": {"available": attachment_available, "missing": attachment_missing,
                                "sample_verified": self.source == "workbuddy" and attachment_available > 0},
                "sessions": {k: {"saved_messages": v.get("saved_messages", 0),
                                  "checked_turns": v.get("checked_turns", 0),
                                  "pending_turns": v.get("pending_turns", 0),
                                  "unverified_turns": v.get("unverified_turns", 0),
                                  "attachment_available": v.get("attachment_available", 0),
                                  "attachment_missing": v.get("attachment_missing", 0),
                                  "last_error": v.get("last_error")} for k, v in rows.items()},
                "last_sync_at": state["last_sync_at"], "last_error": state["last_error"],
                "history_coverage": "仅本机可读且获准的已完成用户/最终答复；思考、工具和系统注入排除。"
                                    + (f"范围内另有 {gaps['in_scope_count']} 个索引会话缺少可读转录，未入档。"
                                       if gaps["in_scope_count"] else "")
                                    + ("WorkBuddy 用户图片按源文件大小及 SHA-256 核验后关联；缺失原件保留引用。"
                                       if self.source == "workbuddy" else
                                       "ZCode 图片仅见来源引用，原件路径未核实，暂不宣称已保存。"
                                       if self.source == "zcode" else "图片附件尚无真实已发送样本可核验。")}

    def configure(self, policy, selected_session_ids=(), *, owner_confirmed=False, only_if_absent=False):
        if not owner_confirmed:
            raise PermissionError("采集范围需由 Owner 确认")
        if policy not in {"paused", "all", "selected"}:
            raise ValueError("采集范围无效")
        selected = sorted(set(selected_session_ids))
        found = {r["session_id"]: r["path"] for r in self.discover()}
        if policy != "paused":
            from .client_access import ClientIdentityRegistry
            connected = any(row.get("client_type") == self.source and row.get("status") != "revoked" and
                            row.get("last_read_at") for row in ClientIdentityRegistry(self.context).list_clients())
            if not connected:
                raise PermissionError("请先在客户端完成真实连接与读取验证")
            if policy == "selected" and not selected:
                raise ValueError("请选择至少一个会话")
            if any(sid not in found for sid in selected):
                raise ValueError("选定会话不可读")
        with self.lock:
            if only_if_absent and self.state_path.exists():
                return self.status()
            state = self._load()
            if policy == "paused":
                if state["policy"] != "paused":
                    state["resume_policy"] = state["policy"]
            else:
                state["resume_policy"] = policy
                state["selected_session_ids"] = selected
                state["selected_sources"] = {sid: found[sid] for sid in selected}
            state["policy"] = policy
            write_object(self.state_path, state, backup=True)
        return self.status()

    def _parse(self, item, db):
        sid = item["session_id"]
        if self.source == "workbuddy":
            return _workbuddy(Path(item["path"]), sid)
        if self.source == "opencode":
            return _opencode(db, sid)
        if self.source == "zcode":
            return _zcode(db, sid)
        return _kimi(Path(item["path"]))

    def _save_progress(self, sid, turn, source_order, entries):
        for index, (pid, content, timestamp) in enumerate(entries):
            identity = hashlib.sha256(f"{self.source}:{sid}:{pid}".encode()).hexdigest()
            target = self.progress_dir / (identity + ".json")
            self.progress_dir.mkdir(parents=True, exist_ok=True)
            value = {"source": self.source, "session_id": sid, "turn": turn,
                                  "bubble_id": pid, "role": "assistant_progress", "text": content,
                                  "source_offset": source_order * 2 + 0.5 + index * 0.000001,
                                  "timestamp": _stamp(timestamp)}
            if target.exists() and read_object(target) == value:
                continue
            write_object(target, value, backup=False)

    def sync(self):
        with self.lock:
            state = self._load()
            if state["policy"] == "paused":
                return {**self.status(), "saved_this_sync": 0}
            database, _ = self._paths()
            if not database.is_file():
                return {**self.status(), "saved_this_sync": 0}
            found = self.discover()
            found_ids = {x["session_id"] for x in found}
            allowed = found_ids if state["policy"] == "all" else set(state["selected_session_ids"])
            if allowed - found_ids:
                state["last_error"] = "选定会话当前缺失；原档案和游标保留"
            saved = 0
            store = ArchiveStore(self.context, recover=False)
            with closing(_db(database)) as db:
                for item in found:
                    sid = item["session_id"]
                    if sid not in allowed:
                        continue
                    row = state["sessions"].setdefault(sid, {"saved_messages": 0, "checked_turns": 0})
                    try:
                        if state["policy"] == "selected" and state["selected_sources"].get(sid) != item["path"]:
                            raise ValueError("选定会话路径已改变；原游标保留")
                        if row.get("source_path") and row["source_path"] != item["path"]:
                            raise ValueError("来源路径已改变；原游标保留")
                        row["source_path"] = item["path"]
                        source = Path(item["path"])
                        stat = source.stat()
                        if self.source in {"workbuddy", "kimi"}:
                            if row.get("source_inode") is not None and row["source_inode"] != stat.st_ino:
                                raise ValueError("来源文件已轮换；原游标保留")
                            if int(row.get("source_size") or 0) > stat.st_size:
                                raise ValueError("来源文件已截短；原游标保留")
                            prefix_length = int(row.get("prefix_length") or min(stat.st_size, 4096))
                            with source.open("rb") as stream:
                                prefix_sha256 = hashlib.sha256(stream.read(prefix_length)).hexdigest()
                            if row.get("prefix_sha256") and row["prefix_sha256"] != prefix_sha256:
                                raise ValueError("来源转录已重写；原游标保留")
                        else:
                            prefix_length, prefix_sha256 = None, None
                        if self.source in {"workbuddy", "kimi"} and \
                                row.get("source_size") == stat.st_size and row.get("source_mtime_ns") == stat.st_mtime_ns and \
                                row.get("source_updated") == item.get("updated") and \
                                not row.get("last_error") and not row.get("pending_turns") and \
                                (self.source != "workbuddy" or
                                 (row.get("attachment_scan_version") == ATTACHMENT_SCAN_VERSION and
                                  not row.get("attachment_missing"))):
                            continue
                        turns, pending, unverified, extra_progress = self._parse(item, db)
                        hashes = dict(row.get("turn_hashes") or {})
                        session_key = None
                        seen_ids = set()
                        attachment_available = 0
                        attachment_missing = 0
                        for index, turn in enumerate(turns):
                            identity = turn["user_id"]
                            if identity in seen_ids:
                                raise ValueError("来源用户回合身份重复；原档案保留")
                            seen_ids.add(identity)
                            digest = hashlib.sha256(json.dumps(
                                [turn["user_id"], turn["user_text"], turn["final_id"], turn["final_text"]],
                                ensure_ascii=False).encode()).hexdigest()
                            prior = hashes.get(identity)
                            if prior and prior != digest:
                                raise ValueError("已确认回合内容变化；原档案保留")
                            attachments = turn.get("attachments") or ()
                            if prior == digest and not attachments:
                                continue
                            if session_key is None:
                                source_instance = self.source + ":" + hashlib.sha256(str(self.root).encode()).hexdigest()[:16]
                                session = store.ensure_session(source_key=self.source, source_session_id=sid,
                                    source_instance=source_instance, title=str(item.get("title") or SOURCES[self.source])[:200],
                                    acquisition_method=self.source + "_local_readonly")
                                session_key = session["session_key"]
                            if prior == digest:
                                linked = store.reconcile_attachments(session_key, "user:" + turn["user_id"],
                                                                      list(attachments))
                                attachment_available += sum(not x.get("missing") for x in linked)
                                attachment_missing += sum(bool(x.get("missing")) for x in linked)
                                continue
                            for role, mid, content, timestamp in (("user", turn["user_id"], turn["user_text"], turn["user_time"]),
                                                                 ("assistant", turn["final_id"], turn["final_text"], turn["final_time"])):
                                source_offset = turn["source_order"] * 2 + (1 if role == "assistant" else 0)
                                message = NormalizedMessage(message_id=role + ":" + mid, role=role,
                                    content=content, timestamp=timestamp,
                                    attachments=attachments if role == "user" else (),
                                    provenance={"adapter": PARSER_VERSION, "turn": turn["source_order"],
                                                "source_message_id": mid, "position": source_offset},
                                    sync_position={"turn": turn["source_order"],
                                                   "byte_offset": source_offset})
                                result = store.append_normalized_message(session_key, message)
                                saved += int(result["saved"])
                                if role == "user":
                                    attachment_available += sum(not x.get("missing") for x in result["record"].get("attachments", ()))
                                    attachment_missing += sum(bool(x.get("missing")) for x in result["record"].get("attachments", ()))
                                row["saved_messages"] = max(int(row["saved_messages"]),
                                                            int(result["session"]["message_count"]))
                            hashes[identity] = digest
                            self._save_progress(sid, turn["source_order"], turn["source_order"], turn["progress"])
                        for order, pid, content, timestamp in extra_progress:
                            self._save_progress(sid, order, order, ((pid, content, timestamp),))
                        if set(hashes) - seen_ids:
                            raise ValueError("已确认回合从来源消失；原档案和游标保留")
                        row.update(checked_turns=max(int(row.get("checked_turns") or 0),len(turns)), pending_turns=pending,
                                   unverified_turns=unverified, turn_hashes=hashes,
                                   attachment_scan_version=ATTACHMENT_SCAN_VERSION,
                                   attachment_available=attachment_available,
                                   attachment_missing=attachment_missing,
                                   source_inode=stat.st_ino if prefix_length is not None else None,
                                   source_size=stat.st_size, source_mtime_ns=stat.st_mtime_ns,
                                   source_updated=item.get("updated"), prefix_length=prefix_length,
                                   prefix_sha256=prefix_sha256,
                                   last_error="有已结束回合未通过核验" if unverified else None,
                                   last_checked_at=_now())
                    except (OSError, ValueError, sqlite3.Error) as exc:
                        row["last_error"] = type(exc).__name__ + ": " + str(exc)[:200]
                    write_object(self.state_path, state, backup=True)
            state["last_sync_at"] = _now()
            if not (allowed - found_ids) and not any(row.get("last_error") for row in state["sessions"].values()):
                state["last_error"] = None
            elif any(row.get("last_error") for row in state["sessions"].values()):
                state["last_error"] = "有本机聊天回合未通过核验；原游标保留"
            write_object(self.state_path, state, backup=True)
            return {**self.status(), "saved_this_sync": saved}


def run(interval=10.0, stop_event=None):
    stop = stop_event or _stop
    while not stop.is_set():
        for source in SOURCES:
            if stop.is_set():
                break
            try:
                adapter = LocalCompletedCollector(source)
                if adapter._load()["policy"] != "paused":
                    adapter.sync()
            except Exception:
                pass
        stop.wait(interval)
