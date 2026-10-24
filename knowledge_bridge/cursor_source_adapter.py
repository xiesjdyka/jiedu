"""Opt-in, read-only Cursor local conversation collector.

The SQLite store supplies visible bubble bodies; completed transcript turns
provide the completion boundary. Unknown schemas and incomplete turns fail
closed. Progress text is retained outside the standard archive.
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

from .archive_store import ArchiveStore, NormalizedMessage
from .durable_state import read_object, write_object, StateCorruptError
from .workspace import WorkspaceContext, require_active_workspace


PARSER_VERSION = "cursor-local-visible-v1"
MAX_LINE = 8 * 1024 * 1024
MAX_TEXT = 512 * 1024
_stop = threading.Event()


def _now():
    return datetime.now(timezone.utc).isoformat()


def default_paths():
    home = Path.home()
    return (Path(os.getenv("APPDATA", home / "AppData/Roaming")) /
            "Cursor/User/globalStorage/state.vscdb", home / ".cursor/projects")


def _safe_text(value):
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_TEXT:
        return None
    return value if value.strip() else None


def _completed_turns(path):
    """Retain failed turn positions; only success permits final publication."""
    turns, current = [], []
    tail_pending = False
    with path.open("rb") as handle:
        for raw in handle:
            if len(raw) > MAX_LINE:
                raise ValueError("Cursor 转录行超过安全上限")
            if not raw.endswith(b"\n"):
                tail_pending = True
                break
            try:
                row = json.loads(raw)
            except (UnicodeError, ValueError):
                raise ValueError("Cursor 已完成转录行无法解析")
            if not isinstance(row, dict):
                raise ValueError("Cursor 转录事件结构无效")
            if row.get("type") == "turn_ended":
                if current:
                    turns.append((row.get("status"), current))
                current = []
            else:
                current.append(row)
    pending = bool(current) or tail_pending
    if current:
        turns.append(("pending", current))
    return turns, pending


def _bubble_turns(db, sid):
    key = "composerData:" + sid
    row = db.execute("select value from cursorDiskKV where key=?", (key,)).fetchone()
    if row is None:
        raise ValueError("Cursor 会话索引缺失")
    composer = json.loads(row[0])
    headers = composer.get("fullConversationHeadersOnly")
    if not isinstance(headers, list):
        raise ValueError("Cursor 会话消息索引结构无效")
    turns, current = [], None
    for header in headers:
        if not isinstance(header, dict) or header.get("type") not in (1, 2):
            raise ValueError("Cursor 未知消息类型")
        bid = header.get("bubbleId")
        if not isinstance(bid, str) or not bid:
            raise ValueError("Cursor 消息身份缺失")
        bubble_row = db.execute("select value from cursorDiskKV where key=?", (f"bubbleId:{sid}:{bid}",)).fetchone()
        if bubble_row is None:
            raise ValueError("Cursor 消息正文缺失")
        bubble = json.loads(bubble_row[0])
        if bubble.get("type") != header["type"] or bubble.get("bubbleId") != bid:
            raise ValueError("Cursor 消息身份不一致")
        item = {"id": bid, "type": bubble["type"], "capability": bubble.get("capabilityType"),
                "internal": bool(bubble.get("thinking") or bubble.get("toolFormerData") or bubble.get("allThinkingBlocks")),
                "text": _safe_text(bubble.get("text")), "time": bubble.get("createdAt"),
                "position": len(current) if bubble["type"] == 2 and current is not None else 0}
        if item["type"] == 1:
            if current is not None:
                turns.append(current)
            current = [item]
        elif current is not None:
            current.append(item)
    if current is not None:
        turns.append(current)
    return turns, composer


def _user_message(transcript, bubbles):
    if not transcript or transcript[0].get("role") != "user" or not isinstance(transcript[0].get("message"), dict):
        return None
    if any(row.get("role") not in {"user", "assistant"} or row.get("type") is not None for row in transcript):
        return None
    if sum(row.get("role") == "user" for row in transcript) != 1:
        return None
    if not bubbles or bubbles[0]["type"] != 1 or not bubbles[0]["text"]:
        return None
    user_parts = transcript[0]["message"].get("content")
    if not isinstance(user_parts, list) or not user_parts or any(not isinstance(p, dict) or p.get("type") != "text" for p in user_parts):
        return None
    wrapped = "\n".join(str(p.get("text") or "") for p in user_parts)
    match = re.search(r"<user_query>\s*(.*?)\s*</user_query>", wrapped, re.DOTALL)
    user_visible = match.group(1) if match else wrapped
    if user_visible.strip() != bubbles[0]["text"].strip():
        return None
    return bubbles[0]


def _messages(transcript, bubbles):
    user = _user_message(transcript, bubbles)
    if user is None:
        return None
    if transcript[-1].get("role") != "assistant":
        return None
    last = transcript[-1].get("message")
    parts = last.get("content") if isinstance(last, dict) else None
    if not isinstance(parts, list) or not parts or any(not isinstance(p, dict) or p.get("type") != "text" for p in parts):
        return None
    final = next((b for b in reversed(bubbles[1:]) if b["type"] == 2 and b["capability"] is None and
                  not b["internal"] and b["text"]), None)
    if final is None:
        return None
    # Verify the transcript's final visible text against the SQLite bubble;
    # schema drift blocks publication rather than guessing a final.
    transcript_text = "\n".join(p.get("text", "") for p in parts)
    if not transcript_text.strip() or transcript_text.strip() != final["text"].strip():
        return None
    return user, final


class CursorSourceAdapter:
    def __init__(self, context: WorkspaceContext | None = None, *, database: Path | None = None,
                 projects: Path | None = None):
        self.context = context or require_active_workspace()
        default_db, default_projects = default_paths()
        source_db = Path(database or default_db).expanduser()
        source_projects = Path(projects or default_projects).expanduser()
        for source in (source_db, source_projects):
            for part in (source, *source.parents):
                if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
                    raise ValueError("Cursor 来源链接目录需要人工核对")
        self.database = source_db.resolve()
        self.projects = source_projects.resolve()
        self.state_path = self.context.sync / "cursor-source-adapter.json"
        self.lock = FileLock(str(self.context.sync / "cursor-source-adapter.lock"))
        self.progress_dir = self.context.sync / "cursor-visible-progress"

    def _load(self):
        state = read_object(self.state_path)
        if self.state_path.exists() and (state.get("policy") not in {"paused", "all", "selected"} or
                                         not isinstance(state.get("sessions"), dict)):
            raise StateCorruptError("Cursor 采集状态损坏；原游标保留")
        return {"policy": state.get("policy", "paused"), "resume_policy": state.get("resume_policy", "selected"),
                "selected_session_ids": state.get("selected_session_ids", []),
                "selected_sources": state.get("selected_sources", {}),
                "sessions": state.get("sessions", {}), "last_sync_at": state.get("last_sync_at"),
                "last_error": state.get("last_error")}

    def discover(self):
        result = []
        if not self.projects.is_dir():
            return result
        for path in sorted(self.projects.glob("*/agent-transcripts/*/*.jsonl")):
            if path.is_symlink() or not path.is_file() or path.stem != path.parent.name:
                continue
            if any(parent.is_symlink() for parent in (path.parent, path.parent.parent, path.parent.parent.parent)):
                continue
            result.append({"session_id": path.stem, "path": str(path), "source_bytes": path.stat().st_size})
        if len({x["session_id"] for x in result}) != len(result):
            raise ValueError("Cursor 重复会话身份；需先核对来源")
        return result

    def _indexed_without_transcript(self, found, state):
        """Report nonempty local DB history that lacks a completion transcript."""
        if not self.database.is_file():
            return {"count": 0, "in_scope_count": 0, "sample": []}
        known = {row["session_id"] for row in found}
        selected = set(state["selected_session_ids"])
        count, in_scope_count, sample = 0, 0, []
        uri = self.database.as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=1)) as db:
            db.execute("pragma query_only=ON")
            for key, raw in db.execute("select key,value from cursorDiskKV where key like 'composerData:%'"):
                sid = key[len("composerData:"):]
                if not sid or sid in known:
                    continue
                try:
                    headers = json.loads(raw).get("fullConversationHeadersOnly")
                except (TypeError, ValueError, AttributeError):
                    continue
                if not isinstance(headers, list) or not headers:
                    continue
                count += 1
                if state["policy"] == "all" or sid in selected:
                    in_scope_count += 1
                if len(sample) < 20:
                    sample.append({"session_id": sid, "indexed_messages": len(headers),
                                   "verification": "缺少已结束回合转录，暂不入档"})
        return {"count": count, "in_scope_count": in_scope_count, "sample": sample}

    def status(self):
        state = self._load()
        found = self.discover()
        indexed_gap = self._indexed_without_transcript(found, state)
        rows = state["sessions"]
        return {"adapter": "cursor", "display_name": "Cursor", "enabled": state["policy"] != "paused",
                "ever_configured": self.state_path.exists(), "policy": state["policy"],
                "resume_policy": state["resume_policy"], "selected_session_ids": state["selected_session_ids"],
                "source_detected": self.database.is_file() and bool(found or indexed_gap["count"]),
                "discovered_sessions": found,
                "indexed_without_transcript": indexed_gap,
                "history": {"known_sessions": len(found), "checked_sessions": len(rows),
                            "unverified_indexed_sessions": indexed_gap["in_scope_count"],
                            "pending_turns": sum(bool(r.get("pending_turn")) for r in rows.values()),
                            "archived_messages": sum(int(r.get("saved_messages") or 0) for r in rows.values()),
                            "total_is_dynamic": True}, "sessions": {k: {"saved_messages": v.get("saved_messages", 0),
                            "checked_turns": v.get("checked_turns", 0), "unverified_turns": v.get("unverified_turns", 0),
                            "pending_turn": bool(v.get("pending_turn")), "last_error": v.get("last_error")}
                            for k, v in rows.items()}, "last_sync_at": state["last_sync_at"],
                "last_error": state["last_error"],
                "history_coverage": "仅本机可读且获准的用户正文及成功结束回合最终答复；"
                                    + (f"获准范围内另有 {indexed_gap['in_scope_count']} 个非空索引会话缺少结束转录，未入档；"
                                       if indexed_gap["in_scope_count"] else "")
                                    + "图片附件已发送链路尚未真实验证"}

    def configure(self, policy, selected_session_ids=(), *, owner_confirmed=False,
                  validate_before_write=None, only_if_absent=False):
        if not owner_confirmed:
            raise PermissionError("Cursor 采集范围需由 Owner 确认")
        if policy not in {"paused", "all", "selected"}:
            raise ValueError("Cursor 范围无效")
        selected = sorted(set(selected_session_ids))
        found = {r["session_id"]: r["path"] for r in self.discover()}
        if policy != "paused":
            from .client_access import ClientIdentityRegistry
            connected = any(c.get("client_type") == "cursor" and c.get("status") != "revoked" and
                            c.get("last_read_at") for c in ClientIdentityRegistry(self.context).list_clients())
            if not connected:
                raise PermissionError("请先在 Cursor 完成真实连接与读取验证")
            if policy == "selected" and not selected:
                raise ValueError("请选择至少一个 Cursor 会话")
            if any(s not in found for s in selected):
                raise ValueError("选定的 Cursor 会话不可读")
        with self.lock:
            if only_if_absent and self.state_path.exists():
                return self.status()
            state = self._load()
            if validate_before_write is not None:
                validate_before_write()
            if policy == "paused":
                if state["policy"] != "paused":
                    state["resume_policy"] = state["policy"]
            else:
                state["resume_policy"] = policy
                state["selected_session_ids"] = selected
                state["selected_sources"] = {s: found[s] for s in selected}
            state["policy"] = policy
            write_object(self.state_path, state, backup=True)
        return self.status()

    def _save_visible_progress(self, sid, turn, bubbles, *, final_id=None):
        for bubble in bubbles[1:]:
            if bubble["type"] != 2 or bubble["capability"] is not None or bubble["internal"] or \
                    not bubble["text"] or bubble["id"] == final_id:
                continue
            self.progress_dir.mkdir(parents=True, exist_ok=True)
            identity = hashlib.sha256((sid + ":" + bubble["id"]).encode()).hexdigest()
            target = self.progress_dir / (identity + ".json")
            if not target.exists():
                write_object(target, {"source": "cursor", "session_id": sid,
                    "turn": turn, "bubble_id": bubble["id"], "position": bubble["position"],
                    "role": "assistant_progress", "text": bubble["text"],
                    "timestamp": bubble["time"]}, backup=False)

    def sync(self):
        with self.lock:
            state = self._load()
            if state["policy"] == "paused":
                return self.status()
            if not self.database.is_file():
                return self.status()
            found = self.discover()
            allowed = {x["session_id"] for x in found} if state["policy"] == "all" else set(state["selected_session_ids"])
            if allowed - {x["session_id"] for x in found}:
                state["last_error"] = "选定的 Cursor 会话当前缺失；原档案和游标保留"
            saved = 0
            store = ArchiveStore(self.context, recover=False)
            uri = self.database.as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=1)) as db:
                db.execute("pragma query_only=ON")
                db.execute("begin")
                database_stat = self.database.stat()
                for item in found:
                    sid = item["session_id"]
                    if sid not in allowed:
                        continue
                    row = state["sessions"].setdefault(sid, {"saved_messages": 0, "checked_turns": 0})
                    try:
                        if state["policy"] == "selected" and state["selected_sources"].get(sid) != item["path"]:
                            raise ValueError("Cursor 会话路径已改变；原状态保留")
                        if row.get("source_path") and row["source_path"] != item["path"]:
                            raise ValueError("Cursor 会话来源已改变；原状态保留")
                        row["source_path"] = item["path"]
                        transcript_path = Path(item["path"])
                        stat = transcript_path.stat()
                        if row.get("source_inode") is not None and row["source_inode"] != stat.st_ino:
                            raise ValueError("Cursor 转录来源文件已轮换；原状态保留")
                        if int(row.get("source_size") or 0) > stat.st_size:
                            raise ValueError("Cursor 转录来源已截短；原状态保留")
                        prefix_len = int(row.get("prefix_length") or min(stat.st_size, 4096))
                        with transcript_path.open("rb") as handle:
                            prefix_hash = hashlib.sha256(handle.read(prefix_len)).hexdigest()
                        if row.get("prefix_sha256") and row["prefix_sha256"] != prefix_hash:
                            raise ValueError("Cursor 转录来源已重写；原状态保留")
                        composer_row = db.execute("select value from cursorDiskKV where key=?",
                            ("composerData:" + sid,)).fetchone()
                        if composer_row is None:
                            raise ValueError("Cursor 会话索引缺失")
                        composer_meta = json.loads(composer_row[0])
                        composer_updated = composer_meta.get("lastUpdatedAt")
                        composer_sha256 = hashlib.sha256(
                            composer_row[0].encode("utf-8") if isinstance(composer_row[0], str)
                            else composer_row[0]).hexdigest()
                        if row.get("source_size") == stat.st_size and row.get("source_mtime_ns") == stat.st_mtime_ns and \
                                row.get("composer_updated") == composer_updated and \
                                row.get("composer_sha256") == composer_sha256 and \
                                not row.get("last_error") and \
                                not row.get("pending_turn"):
                            continue
                        transcript, pending_turn = _completed_turns(transcript_path)
                        bubbles, composer = _bubble_turns(db, sid)
                        if len(transcript) > len(bubbles):
                            raise ValueError("Cursor 转录与本机消息索引不一致")
                        source_instance = "cursor:" + hashlib.sha256(str(self.projects).encode()).hexdigest()[:16]
                        session_key = None
                        skipped = 0
                        turn_hashes = dict(row.get("turn_hashes") or {})
                        for index, (turn_status, events) in enumerate(transcript):
                            user = _user_message(events, bubbles[index])
                            if user is None:
                                skipped += 1
                                continue
                            if session_key is None:
                                session = store.ensure_session(source_key="cursor", source_session_id=sid,
                                    source_instance=source_instance, title=str(composer.get("name") or "Cursor " + sid[:8])[:200],
                                    acquisition_method="cursor_local_readonly")
                                session_key = session["session_key"]
                            message = NormalizedMessage(message_id="user:" + user["id"], role="user",
                                content=user["text"], timestamp=user["time"],
                                provenance={"adapter": PARSER_VERSION, "turn": index + 1,
                                            "bubble_id": user["id"], "position": user["position"]},
                                sync_position={"turn": index + 1})
                            result = store.append_normalized_message(session_key, message)
                            saved += int(result["saved"])
                            row["saved_messages"] = max(int(row["saved_messages"]),
                                                        int(result["session"]["message_count"]))
                            if turn_status != "success":
                                self._save_visible_progress(sid, index + 1, bubbles[index])
                                if turn_status != "pending":
                                    skipped += 1
                                continue
                            pair = _messages(events, bubbles[index])
                            if pair is None:
                                self._save_visible_progress(sid, index + 1, bubbles[index])
                                skipped += 1
                                continue
                            user, final = pair
                            turn_digest = hashlib.sha256(json.dumps(
                                [(b["id"], b["text"]) for b in (user, final)],
                                ensure_ascii=False).encode("utf-8")).hexdigest()
                            prior_digest = turn_hashes.get(str(index))
                            if prior_digest and prior_digest != turn_digest:
                                raise ValueError("Cursor 已确认回合内容已变化；原档案保留")
                            message = NormalizedMessage(message_id="assistant:" + final["id"], role="assistant",
                                content=final["text"], timestamp=final["time"],
                                provenance={"adapter": PARSER_VERSION, "turn": index + 1,
                                            "bubble_id": final["id"], "position": final["position"]},
                                sync_position={"turn": index + 1})
                            result = store.append_normalized_message(session_key, message)
                            saved += int(result["saved"])
                            row["saved_messages"] = max(int(row["saved_messages"]),
                                                        int(result["session"]["message_count"]))
                            turn_hashes[str(index)] = turn_digest
                            # Visible progress is private source evidence. Standard archive
                            # and normal knowledge retrieval never index this directory.
                            self._save_visible_progress(sid, index + 1, bubbles[index], final_id=final["id"])
                        row.update(checked_turns=len(transcript) - int(pending_turn and bool(transcript) and transcript[-1][0] == "pending"),
                                   unverified_turns=skipped,
                                   turn_hashes=turn_hashes, source_inode=stat.st_ino,
                                   source_size=stat.st_size, source_mtime_ns=stat.st_mtime_ns,
                                   composer_updated=composer_updated,
                                   composer_sha256=composer_sha256,
                                   database_mtime_ns=database_stat.st_mtime_ns,
                                   pending_turn=pending_turn, prefix_length=prefix_len,
                                   prefix_sha256=prefix_hash,
                                   # An unfinished answer is normal source state. Keep it
                                   # visible as pending_turn without a collector error.
                                   last_error="有已结束回合未通过核验" if skipped else None,
                                   last_checked_at=_now())
                    except (OSError, ValueError, sqlite3.Error) as exc:
                        row["last_error"] = type(exc).__name__ + ": " + str(exc)[:200]
                        state["last_error"] = "有 Cursor 会话未完成分类；原游标保留"
                    write_object(self.state_path, state, backup=True)
            state["last_sync_at"] = _now()
            if not (allowed - {x["session_id"] for x in found}) and not any(r.get("last_error") for r in state["sessions"].values()):
                state["last_error"] = None
            elif any(r.get("last_error") for r in state["sessions"].values()):
                state["last_error"] = "有 Cursor 已结束回合未通过核验；已保存内容保留"
            write_object(self.state_path, state, backup=True)
            return {**self.status(), "saved_this_sync": saved}


def run(interval=10.0, stop_event=None):
    stop = stop_event or _stop
    while not stop.is_set():
        try:
            adapter = CursorSourceAdapter()
            if adapter._load()["policy"] != "paused":
                adapter.sync()
        except Exception:
            pass
        stop.wait(interval)
