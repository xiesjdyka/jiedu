"""Owner-selected collection of locally available Claude conversation trees.

Chromium's current IndexedDB records are read from a temporary consistent
snapshot.  The snapshot is deleted before returning and is never written into
the workspace.  Only human text and completed assistant final text are saved.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock

from .archive_store import ArchiveStore, NormalizedAttachment, NormalizedMessage
from .durable_state import read_object, write_object, StateCorruptError
from .workspace import WorkspaceContext, require_active_workspace


PARSER_VERSION = "claude-local-final-visible-v1"
MAX_TEXT_BYTES = 512 * 1024
_stop = threading.Event()


def profile_paths():
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    roaming = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    roots = {
        "store": ("商店版", local / "Packages" / "Claude_pzs8sxrjxfjjc" /
                  "LocalCache" / "Roaming" / "Claude" / "IndexedDB"),
        "desktop": ("桌面版", roaming / "Claude" / "IndexedDB"),
    }
    return {key: (label, root / "https_claude.ai_0.indexeddb.leveldb",
                  root / "https_claude.ai_0.indexeddb.blob")
            for key, (label, root) in roots.items()}


def default_paths():
    profiles = profile_paths()
    available = [value for value in profiles.values() if (value[1] / "CURRENT").is_file()]
    if len(available) != 1:
        raise ValueError("Claude 本机来源不存在或有多个资料目录，请明确选择")
    return available[0][1:]


def _now():
    return datetime.now(timezone.utc).isoformat()


def _safe_string(value, limit=240):
    return value[:limit] if isinstance(value, str) else ""


def _text(parts, *, assistant):
    if not isinstance(parts, list):
        return ""
    start = 0
    if assistant:
        for index, block in enumerate(parts):
            if isinstance(block, dict) and block.get("type") in {"tool_use", "tool_result"}:
                start = index + 1
    text = "\n".join(block["text"] for block in parts[start:]
                     if isinstance(block, dict) and block.get("type") == "text"
                     and isinstance(block.get("text"), str) and block["text"])
    if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        raise ValueError("Claude 可见消息超过单条上限")
    return text


def _references(row):
    """Keep explicit message references only; never read arbitrary client files."""
    result = []
    seen = set()
    for field in ("attachments", "files", "files_v2"):
        values = row.get(field)
        for item in values if isinstance(values, list) else []:
            if not isinstance(item, dict):
                continue
            token = next((_safe_string(item.get(key)) for key in
                          ("id", "uuid", "file_uuid", "attachment_id") if _safe_string(item.get(key))), "")
            if not token or token in seen:
                continue
            seen.add(token)
            name = next((_safe_string(item.get(key), 200) for key in
                         ("file_name", "filename", "name") if _safe_string(item.get(key))), "附件")
            result.append(NormalizedAttachment(reference="claude:" + token,
                                               original_name=name, local_path=None))
    return tuple(result)


def _messages(tree):
    if not isinstance(tree, dict) or not isinstance(tree.get("messages"), list):
        raise ValueError("Claude 会话树结构未知")
    messages = tree["messages"]
    if len(messages) > 100_000:
        raise ValueError("Claude 会话消息数量超过上限")
    result = []
    for index, row in enumerate(messages):
        if not isinstance(row, dict):
            raise ValueError("Claude 消息结构未知")
        sender = row.get("sender")
        if sender not in {"human", "assistant"}:
            continue
        if sender == "assistant" and row.get("stop_reason") != "end_turn":
            continue
        if not isinstance(row.get("content"), list):
            raise ValueError("Claude 可见消息正文结构未知")
        identity = _safe_string(row.get("uuid"))
        if not identity:
            raise ValueError("Claude 可见消息缺少稳定身份")
        content = _text(row.get("content"), assistant=sender == "assistant")
        attachments = _references(row) if sender == "human" else ()
        if sender == "assistant" and not content:
            raise ValueError("Claude 已完成答复缺少可确认的最终正文")
        if not content and not attachments:
            continue
        result.append(NormalizedMessage(message_id=identity,
            role="user" if sender == "human" else "assistant", content=content,
            timestamp=_safe_string(row.get("created_at")) or None,
            attachments=attachments,
            provenance={"adapter": PARSER_VERSION, "source_message_id": identity,
                        "tree_index": index, "message_kind": sender},
            sync_position={"tree_index": index}))
    return result


def _read_current_trees(leveldb: Path, blob: Path):
    """Return effective live tree records; report unavailable current blobs."""
    from ccl_chromium_reader import ccl_chromium_indexeddb
    from .doubao_indexeddb_source import _latest_records

    db = ccl_chromium_indexeddb.WrappedIndexDB(leveldb, blob)
    trees = {}
    current_errors = 0
    current_verified = True
    found_store = False
    try:
        for meta in db.database_ids:
            database = db[meta.dbid_no]
            if "trees" not in database.object_store_names:
                continue
            found_store = True
            records, bad, _, verified = _latest_records(database["trees"])
            current_errors += bad
            current_verified &= verified
            for record in records:
                if not record.is_live:
                    continue
                value = record.value
                if not isinstance(value, dict) or not isinstance(value.get("tree"), dict):
                    current_errors += 1
                    continue
                if (value.get("product") != "hub" or value.get("v") != "2.2.hub" or
                        value["tree"].get("kind") != "hub_transcript"):
                    current_errors += 1
                    continue
                conversation = _safe_string(value.get("conversationUuid"))
                if not conversation or value["tree"].get("conversationUuid") != conversation:
                    current_errors += 1
                    continue
                old = trees.get(conversation)
                if old is None or int(record.ldb_seq_no) > old[0]:
                    trees[conversation] = (int(record.ldb_seq_no), value["tree"])
    finally:
        db.close()
    if not found_store:
        raise ValueError("Claude 当前 IndexedDB 未找到已验证的会话树存储")
    return {key: value[1] for key, value in trees.items()}, current_errors, current_verified


class ClaudeSourceAdapter:
    def __init__(self, context: WorkspaceContext | None = None, *,
                 leveldb: Path | None = None, blob: Path | None = None, reader=None,
                 profile_id: str | None = None):
        self.context = context or require_active_workspace()
        self.state_path = self.context.sync / "claude-source-adapter.json"
        profiles = profile_paths()
        self.available_profiles = [
            {"id": key, "label": item[0]} for key, item in profiles.items()
            if (item[1] / "CURRENT").is_file()]
        if leveldb is not None:
            if blob is None:
                raise ValueError("自定义 Claude 测试来源需要完整 blob 路径")
            self.profile_id = "fixture"
            self.leveldb, self.blob = leveldb.expanduser().resolve(), blob.expanduser().resolve()
        else:
            saved = read_object(self.state_path)
            chosen = profile_id or saved.get("profile_id")
            if chosen is not None and chosen not in profiles:
                raise ValueError("Claude 来源资料目录标识无效")
            if chosen is None and len(self.available_profiles) == 1:
                chosen = self.available_profiles[0]["id"]
            self.profile_id = chosen
            self.leveldb = profiles[chosen][1].expanduser().resolve() if chosen else None
            self.blob = profiles[chosen][2].expanduser().resolve() if chosen else None
        self.reader = reader
        self.lock = FileLock(str(self.context.sync / "claude-source-adapter.lock"))

    def _load(self):
        value = read_object(self.state_path)
        if self.state_path.exists() and (value.get("policy") not in {"paused", "all", "selected"}
                                         or not isinstance(value.get("sessions"), dict)):
            raise StateCorruptError("Claude 采集状态损坏；没有重置进度")
        return {"policy": value.get("policy", "paused"),
                "profile_id": value.get("profile_id"),
                "resume_policy": value.get("resume_policy", "selected"),
                "selected_session_ids": value.get("selected_session_ids", []),
                "sessions": value.get("sessions", {}), "last_sync_at": value.get("last_sync_at"),
                "last_error": value.get("last_error"), "current_decode_errors": value.get("current_decode_errors", 0),
                "current_session_ids": value.get("current_session_ids", []),
                "current_state_verified": value.get("current_state_verified", False),
                "current_checked_at": value.get("current_checked_at")}

    def _trees(self):
        if self.reader is not None:
            return self.reader()
        if self.leveldb is None or self.blob is None:
            raise ValueError("Claude 检出多个本机资料目录，请先选择一个来源")
        if not (self.leveldb / "CURRENT").is_file():
            return {}, 0, False
        from .doubao_indexeddb_source import consistent_snapshot
        # No complete cache, thinking, or tool body is ever persisted in the workspace.
        with tempfile.TemporaryDirectory(prefix="jiedu-claude-read-") as temporary:
            destination = Path(temporary) / "snapshot"
            consistent_snapshot(self.leveldb, self.blob, destination)
            return _read_current_trees(destination / "leveldb", destination / "blob")

    def discover(self):
        trees, errors, verified = self._trees()
        rows = [{"session_id": identity, "available_messages": len(tree.get("messages", []))}
                for identity, tree in sorted(trees.items())]
        return {"sessions": rows, "current_decode_errors": errors,
                "selected_profile": self.profile_id, "available_profiles": self.available_profiles,
                "current_state_verified": verified,
                "history_coverage": "仅本机当前 IndexedDB 缓存；云端和已清理历史总量未知"}

    def status(self):
        state = self._load()
        return {"adapter": "claude", "display_name": "Claude",
                "enabled": state["policy"] != "paused", "policy": state["policy"],
                "ever_configured": self.state_path.exists(),
                "resume_policy": state["resume_policy"],
                "selected_session_ids": state["selected_session_ids"],
                "source_detected": bool(self.available_profiles) or
                                   bool(self.leveldb and (self.leveldb / "CURRENT").is_file()),
                "selected_profile": self.profile_id,
                "current_session_ids": state["current_session_ids"],
                "current_state_verified": state["current_state_verified"],
                "current_checked_at": state["current_checked_at"],
                "available_profiles": self.available_profiles,
                "source_ambiguous": self.profile_id is None and len(self.available_profiles) > 1,
                "history": {"known_local_sessions": len(state["sessions"]),
                            "archived_messages": sum(int(row.get("saved_messages") or 0)
                                                     for row in state["sessions"].values()),
                            "current_decode_errors": state["current_decode_errors"],
                            "total_is_dynamic": True, "cloud_total_known": False},
                "sessions": {key: {field: row.get(field) for field in
                                   ("checked_messages", "saved_messages", "last_error")}
                             for key, row in state["sessions"].items()},
                "last_sync_at": state["last_sync_at"], "last_error": state["last_error"],
                "history_coverage": "仅本机当前缓存可读范围；不能证明云端历史已全部保存"}

    def configure(self, policy: str, selected_session_ids=(), *, owner_confirmed=False,
                  validate_before_write=None):
        if not owner_confirmed:
            raise PermissionError("Claude 采集范围需由 Owner 确认")
        if policy not in {"paused", "all", "selected"}:
            raise ValueError("Claude 范围必须为 paused、all 或 selected")
        selected = sorted(set(selected_session_ids))
        if policy != "paused":
            discovered = self.discover()
            if not discovered["current_state_verified"]:
                raise ValueError("Claude 当前缓存快照尚未验证")
            if not discovered["sessions"]:
                raise ValueError("Claude 当前没有可读的本机会话")
            if any(item not in {row["session_id"] for row in discovered["sessions"]} for item in selected):
                raise ValueError("选定的 Claude 会话不在当前本机缓存")
            if policy == "selected" and not selected:
                raise ValueError("请选择至少一个 Claude 会话")
        with self.lock:
            state = self._load()
            if validate_before_write is not None:validate_before_write()
            if policy != "paused":
                state["profile_id"] = self.profile_id
            if policy == "paused":
                if state["policy"] != "paused":
                    state["resume_policy"] = state["policy"]
            else:
                state["resume_policy"] = policy
                state["selected_session_ids"] = selected
            state["policy"] = policy
            write_object(self.state_path, state, backup=True)
        return self.status()

    def sync(self):
        with self.lock:
            state = self._load()
            if state["policy"] == "paused":
                return self.status()
            try:
                trees, errors, verified = self._trees()
            except Exception as exc:
                state["last_error"] = "Claude 本机缓存快照或解码失败：" + type(exc).__name__
                write_object(self.state_path, state, backup=True)
                return self.status()
            state["current_decode_errors"] = errors
            state["current_session_ids"] = sorted(trees)
            state["current_state_verified"] = bool(verified)
            state["current_checked_at"] = _now()
            state["last_error"] = ("Claude 当前缓存有不可读记录；仅已确认可读会话被采集"
                                   if errors or not verified else None)
            selected = set(trees) if state["policy"] == "all" else set(state["selected_session_ids"])
            missing_selected = selected - set(trees)
            if missing_selected:
                state["last_error"] = "已选择的 Claude 会话当前不在可读本机缓存；原档案保留"
            store = ArchiveStore(self.context, recover=False)
            saved = 0
            for sid, tree in trees.items():
                if sid not in selected:
                    continue
                row = state["sessions"].setdefault(sid, {"saved_messages": 0})
                try:
                    normalized = _messages(tree)
                    session = store.ensure_session(source_key="claude", source_session_id=sid,
                        source_instance="claude:" + hashlib.sha256(str(self.leveldb).encode()).hexdigest()[:16],
                        title="Claude " + sid[:8], acquisition_method="claude_local_cache_readonly")
                    for message in normalized:
                        result = store.append_normalized_message(session["session_key"], message)
                        row["saved_messages"] = max(int(row.get("saved_messages") or 0),
                                                    int(result["session"]["message_count"]))
                        saved += int(result["saved"])
                    row.update(checked_messages=len(tree["messages"]), last_error=None,
                               last_checked_at=_now())
                except (OSError, ValueError) as exc:
                    row["last_error"] = type(exc).__name__ + ": " + str(exc)[:240]
                    state["last_error"] = "有 Claude 会话未完成归档；原记录保留"
                write_object(self.state_path, state, backup=True)
            state["last_sync_at"] = _now()
            write_object(self.state_path, state, backup=True)
            return {**self.status(), "saved_this_sync": saved}


def run(interval=10.0, stop_event=None):
    stop = stop_event or _stop
    while not stop.is_set():
        try:
            adapter = ClaudeSourceAdapter()
            if adapter._load()["policy"] != "paused":
                adapter.sync()
        except Exception as exc:
            try:
                if adapter._load()["policy"] != "paused":
                    with adapter.lock:
                        state = adapter._load()
                        state["last_error"] = "Claude 采集检查未完成：" + type(exc).__name__
                        write_object(adapter.state_path, state, backup=True)
            except Exception:
                pass
        stop.wait(interval)
