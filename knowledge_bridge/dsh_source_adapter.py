"""Owner-selected, read-only DSH conversation collector.

The source is an append-only sequence of independent zstd frames.  Only
source.kind=user messages and the last text answer of a completed turn enter
ArchiveStore.  Reasoning, tool calls/results and synthetic user contexts are
never copied to the archive or the checkpoint.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

import zstd
from filelock import FileLock

from .archive_store import ArchiveStore, NormalizedAttachment, NormalizedMessage
from .durable_state import read_object, write_object, StateCorruptError
from .workspace import WorkspaceContext, require_active_workspace


PARSER_VERSION = "dsh-final-visible-v1"
MAGIC = b"\x28\xb5\x2f\xfd"
SESSION_NAME = re.compile(r"session-([0-9a-f-]{36})$")
SHA_ATTACHMENT = re.compile(r"sha256:([0-9a-f]{64})$")
MAX_FRAME_BYTES = 16 * 1024 * 1024
MAX_TEXT_BYTES = 512 * 1024
READ_CHUNK = 64 * 1024
FRAME_BUDGET = 96
BYTE_BUDGET = 8 * 1024 * 1024
_stop = threading.Event()


class SourceFormatError(ValueError):
    pass


def default_root() -> Path:
    return Path(os.getenv("DSH_HOME", Path.home() / ".dsh"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _time(value):
    try:
        return datetime.fromtimestamp(float(value) / 1000, timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def _frame_end(data: bytes, start: int) -> int | None:
    """Return one complete zstd frame end, or None for an unfinished tail."""
    size = len(data)
    if size - start < 4:
        return None
    if data[start:start + 4] != MAGIC:
        raise SourceFormatError(f"DSH 压缩帧标记无效，位置 {start}")
    position = start + 4
    if position == size:
        return None
    descriptor = data[position]
    position += 1
    if descriptor & 0x18:
        raise SourceFormatError("DSH 压缩帧含保留标志")
    single = bool(descriptor & 0x20)
    header_bytes = (0 if single else 1) + (0, 1, 2, 4)[descriptor & 3]
    header_bytes += ((1, 2, 4, 8) if single else (0, 2, 4, 8))[(descriptor >> 6) & 3]
    if position + header_bytes > size:
        return None
    position += header_bytes
    while True:
        if position + 3 > size:
            return None
        block = int.from_bytes(data[position:position + 3], "little")
        position += 3
        kind = (block >> 1) & 3
        if kind == 3:
            raise SourceFormatError("DSH 压缩块类型无效")
        position += 1 if kind == 1 else block >> 3
        if position - start > MAX_FRAME_BYTES:
            raise SourceFormatError("DSH 单帧超过安全上限")
        if position > size:
            return None
        if block & 1:
            break
    position += 4 if descriptor & 4 else 0
    return position if position <= size else None


def complete_frames(data: bytes, offset: int = 0):
    while offset < len(data):
        end = _frame_end(data, offset)
        if end is None:
            break
        try:
            raw = zstd.decompress(data[offset:end])
            if len(raw) > MAX_FRAME_BYTES:
                raise SourceFormatError("DSH 解压帧超过安全上限")
            events = [json.loads(line) for line in raw.splitlines() if line.strip()]
        except (zstd.Error, UnicodeError, json.JSONDecodeError) as exc:
            raise SourceFormatError(f"DSH 会话帧不可解码，位置 {offset}") from exc
        if not all(isinstance(row, dict) and isinstance(row.get("type"), str) for row in events):
            raise SourceFormatError(f"DSH 会话事件结构无效，位置 {offset}")
        yield end, events
        offset = end


def stream_frames(path: Path, offset: int, *, frame_budget=FRAME_BUDGET,
                  byte_budget=BYTE_BUDGET):
    """Read bounded frames from one checkpoint without loading the full session."""
    with path.open("rb") as source:
        source.seek(offset)
        buffered = bytearray()
        absolute = offset
        frames = 0
        compressed = 0
        while frames < frame_budget and (compressed < byte_budget or frames == 0):
            end = _frame_end(buffered, 0)
            if end is None:
                chunk = source.read(READ_CHUNK)
                if not chunk:
                    return
                buffered.extend(chunk)
                if len(buffered) > MAX_FRAME_BYTES:
                    raise SourceFormatError("DSH 单帧超过安全上限")
                continue
            frame = bytes(buffered[:end])
            try:
                raw = zstd.decompress(frame)
                if len(raw) > MAX_FRAME_BYTES:
                    raise SourceFormatError("DSH 解压帧超过安全上限")
                events = [json.loads(line) for line in raw.splitlines() if line.strip()]
            except (zstd.Error, UnicodeError, json.JSONDecodeError) as exc:
                raise SourceFormatError(f"DSH 会话帧不可解码，位置 {absolute}") from exc
            if not all(isinstance(row, dict) and isinstance(row.get("type"), str) for row in events):
                raise SourceFormatError(f"DSH 会话事件结构无效，位置 {absolute}")
            yield absolute, absolute + end, events, hashlib.sha256(frame).hexdigest()
            del buffered[:end]
            absolute += end
            compressed += end
            frames += 1


def _read_candidate(path: Path, locator: dict):
    position = locator.get("frame_offset")
    if type(position) is not int or position < 0:
        raise SourceFormatError("DSH 最终答复定位无效")
    for start, _, events, frame_hash in stream_frames(path, position, frame_budget=1):
        if start != position:
            break
        if frame_hash != locator.get("frame_sha256"):
            raise SourceFormatError("DSH 最终答复来源帧已变化；保留原游标")
        for event in events:
            if event.get("seq") == locator.get("seq"):
                candidate = _candidate(event)
                if candidate and candidate["turn"] == locator.get("turn"):
                    return candidate
    raise SourceFormatError("DSH 最终答复来源事件已变化；保留原游标")


def _text_blocks(blocks, *, after_tools=False) -> str:
    if not isinstance(blocks, list):
        return ""
    start = 0
    if after_tools:
        for index, part in enumerate(blocks):
            if isinstance(part, dict) and part.get("type") == "tool-call":
                start = index + 1
    pieces = [part.get("text") for part in blocks[start:] if isinstance(part, dict)
              and part.get("type") == "text" and isinstance(part.get("text"), str)]
    text = "\n".join(piece for piece in pieces if piece)
    if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        raise SourceFormatError("DSH 可见答复超过单条消息上限")
    return text


def _attachment_specs(blocks, root: Path) -> tuple[NormalizedAttachment, ...]:
    result = []
    for part in blocks if isinstance(blocks, list) else []:
        if not isinstance(part, dict) or part.get("type") != "image":
            continue
        entry = part.get("attachment")
        if not isinstance(entry, dict):
            continue
        match = SHA_ATTACHMENT.fullmatch(str(entry.get("attachmentId") or ""))
        if not match:
            continue
        digest = match.group(1)
        original = root / "attachments" / "v1" / "objects" / digest[:2] / digest
        local = None
        if original.is_file() and not original.is_symlink() and hashlib.sha256(original.read_bytes()).hexdigest() == digest:
            local = original
        result.append(NormalizedAttachment(reference="sha256:" + digest,
                                           original_name=str(entry.get("name") or "图片")[:200],
                                           local_path=local, mime_type=str(entry.get("mediaType") or "image/png")))
    return tuple(result)


def _human(event: dict, root: Path):
    if event.get("type") != "user/message":
        return None
    data = event.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("source"), dict):
        raise SourceFormatError("DSH 用户事件来源结构无效")
    if data["source"].get("kind") != "user":
        return None
    if not isinstance(data.get("content"), list):
        raise SourceFormatError("DSH 真人用户消息正文结构无效")
    text = _text_blocks(data.get("content"))
    attachments = _attachment_specs(data.get("content"), root)
    if not text and not attachments:
        return None
    identity = str(data.get("id") or f"seq:{event.get('seq')}")
    return NormalizedMessage(message_id="user:" + identity, role="user", content=text,
                             timestamp=_time(event.get("time")), attachments=attachments,
                             provenance={"adapter": PARSER_VERSION, "source_seq": event.get("seq"),
                                         "event_type": "user/message"},
                             sync_position={"event_seq": event.get("seq")})


def _candidate(event: dict):
    if event.get("type") != "assistant/message":
        return None
    data = event.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("turn"), int):
        raise SourceFormatError("DSH 助手事件缺少回合身份")
    message = data.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise SourceFormatError("DSH 助手事件结构无效")
    if not isinstance(message.get("content"), list):
        raise SourceFormatError("DSH 助手事件正文结构无效")
    text = _text_blocks(message.get("content"), after_tools=True)
    return {"turn": data["turn"], "seq": event.get("seq"), "message_id": str(message.get("id") or ""),
            "text": text, "time": _time(event.get("time"))}


class DSHSourceAdapter:
    def __init__(self, context: WorkspaceContext | None = None, *, source_root: Path | None = None):
        self.context = context or require_active_workspace()
        self.root = (source_root or default_root()).expanduser().resolve()
        self.state_path = self.context.sync / "dsh-source-adapter.json"
        self.lock = FileLock(str(self.context.sync / "dsh-source-adapter.lock"))
        self.progress_dir = self.context.sync / "dsh-visible-progress"
        self.frame_budget = FRAME_BUDGET

    def _load(self):
        value = read_object(self.state_path)
        if self.state_path.exists() and (value.get("policy") not in {"paused", "all", "selected"}
                                         or not isinstance(value.get("sessions"), dict)):
            raise StateCorruptError("DSH 采集状态损坏；没有重置游标")
        return {"policy": value.get("policy", "paused"),
                "resume_policy": value.get("resume_policy", "selected"),
                "selected_session_ids": value.get("selected_session_ids", []),
                "selected_sources": value.get("selected_sources", {}),
                "sessions": value.get("sessions", {}), "last_sync_at": value.get("last_sync_at"),
                "last_error": value.get("last_error")}

    def discover(self):
        result = []
        root = self.root / "sessions"
        if not root.is_dir():
            return result
        for path in sorted(root.glob("*/session-*/session.v4.jsonl.zstd")):
            match = SESSION_NAME.fullmatch(path.parent.name)
            if not match or path.is_symlink() or not path.is_file():
                continue
            result.append({"session_id": match.group(1), "path": str(path),
                           "source_bytes": path.stat().st_size})
        if len({row["session_id"] for row in result}) != len(result):
            raise SourceFormatError("DSH 发现重复会话身份；需先核对来源目录")
        return result

    def status(self):
        state = self._load()
        sessions = self.discover()
        rows = state["sessions"]
        return {"adapter": "dsh", "display_name": "DSH", "enabled": state["policy"] != "paused",
                "ever_configured": self.state_path.exists(),
                "policy": state["policy"], "resume_policy": state["resume_policy"],
                "selected_session_ids": state["selected_session_ids"],
                "source_detected": bool(sessions), "discovered_sessions": sessions,
                "history": {"known_sessions": len(sessions),
                            "checked_sessions": sum(bool(row.get("confirmed_offset")) for row in rows.values()),
                            "archived_messages": sum(int(row.get("saved_messages") or 0) for row in rows.values()),
                            "complete": sum(row.get("pending_bytes") == 0 and
                                            not row.get("pending_turn") and
                                            not row.get("last_error") for row in rows.values()),
                            "total_is_dynamic": True},
                "sessions": {key: {**{field: row.get(field) for field in
                                   ("confirmed_offset", "pending_bytes", "saved_messages", "last_error")},
                                   "has_pending_turn": bool(row.get("pending_turn"))}
                             for key, row in rows.items()},
                "last_sync_at": state["last_sync_at"], "last_error": state["last_error"],
                "history_coverage": "仅获准且本机现存的会话帧；源端其他历史总量未知"}

    def configure(self, policy: str, selected_session_ids=(), *, owner_confirmed=False,
                  validate_before_write=None):
        if not owner_confirmed:
            raise PermissionError("DSH 采集范围需由 Owner 确认")
        if policy not in {"paused", "all", "selected"}:
            raise ValueError("DSH 范围必须为 paused、all 或 selected")
        discovered = self.discover()
        found = {row["session_id"]: row["path"] for row in discovered}
        selected = sorted(set(selected_session_ids))
        if any(item not in found for item in selected):
            raise ValueError("选定的 DSH 会话已不存在")
        if policy == "selected" and not selected:
            raise ValueError("请选择至少一个 DSH 会话")
        with self.lock:
            state = self._load()
            if validate_before_write is not None:validate_before_write()
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

    def sync(self):
        with self.lock:
            state = self._load()
            if state["policy"] == "paused":
                return self.status()
            found = self.discover()
            allowed = {row["session_id"] for row in found} if state["policy"] == "all" else set(state["selected_session_ids"])
            missing_selected = state["policy"] == "selected" and bool(
                allowed - {row["session_id"] for row in found})
            if missing_selected:
                state["last_error"] = "已选择的 DSH 会话源文件当前缺失；原档案保留"
            store = ArchiveStore(self.context, recover=False)
            saved = 0
            for item in found:
                sid = item["session_id"]
                if sid not in allowed:
                    continue
                row = state["sessions"].setdefault(sid, {"confirmed_offset": 0, "saved_messages": 0})
                try:
                    expected_path = state["selected_sources"].get(sid) if state["policy"] == "selected" else row.get("source_path")
                    if expected_path and expected_path != item["path"]:
                        raise SourceFormatError("DSH 会话来源路径改变；原游标保留等待核对")
                    row["source_path"] = item["path"]
                    path = Path(item["path"])
                    size = path.stat().st_size
                    prefix_length = int(row.get("prefix_length") or min(size, 4096))
                    with path.open("rb") as handle:
                        prefix = hashlib.sha256(handle.read(prefix_length)).hexdigest()
                    if row.get("confirmed_offset", 0) > size or (
                            row.get("prefix_sha256") and row["prefix_sha256"] != prefix):
                        raise SourceFormatError("DSH 会话源截短或已改变；原游标保留等待核对")
                    if row.get("last_frame_sha256") and row.get("last_frame_start") is not None:
                        last_start = int(row["last_frame_start"])
                        last_end = int(row.get("confirmed_offset") or 0)
                        if last_start < 0 or last_end - last_start > MAX_FRAME_BYTES:
                            raise SourceFormatError("DSH 已确认帧定位无效")
                        with path.open("rb") as handle:
                            handle.seek(last_start)
                            last_frame = handle.read(last_end - last_start)
                        if hashlib.sha256(last_frame).hexdigest() != row["last_frame_sha256"]:
                            raise SourceFormatError("DSH 已确认帧发生变化；原游标保留")
                    row["prefix_length"] = prefix_length
                    row["prefix_sha256"] = prefix
                    session_key = None
                    def archive_key():
                        nonlocal session_key
                        if session_key is None:
                            session = store.ensure_session(source_key="dsh", source_session_id=sid,
                                source_instance="dsh:" + hashlib.sha256(str(self.root).encode()).hexdigest()[:16],
                                title="DSH " + sid[:8], acquisition_method="dsh_local_zstd_readonly")
                            session_key = session["session_key"]
                        return session_key
                    offset = int(row.get("confirmed_offset") or 0)
                    pending = row.get("pending_turn")
                    for start, end, events, frame_hash in stream_frames(
                            path, offset, frame_budget=self.frame_budget):
                        for event in events:
                            human = _human(event, self.root)
                            if human:
                                result = store.append_normalized_message(archive_key(), human)
                                saved += int(result["saved"])
                                row["saved_messages"] = max(int(row.get("saved_messages") or 0),
                                                            int(result["session"]["message_count"]))
                                missing = row.setdefault("missing_attachments", {})
                                for attachment in human.attachments:
                                    if attachment.local_path is None:
                                        missing[human.message_id + "|" + attachment.reference] = {
                                            "message_id": human.message_id,
                                            "reference": attachment.reference,
                                            "original_name": attachment.original_name,
                                            "mime_type": attachment.mime_type}
                            candidate = _candidate(event)
                            if candidate:
                                # A candidate is confirmed progress only when a later
                                # candidate supersedes it in the same turn. Keep the
                                # possible final out of the sidecar until turn/end.
                                if pending and pending["turn"] == candidate["turn"]:
                                    earlier = _read_candidate(path, pending)
                                    visible = earlier["text"]
                                else:
                                    earlier = None
                                    visible = None
                                if visible and earlier:
                                    identity = hashlib.sha256((sid + ":" + str(earlier["seq"])).encode()).hexdigest()
                                    self.progress_dir.mkdir(parents=True, exist_ok=True)
                                    target = self.progress_dir / (identity + ".json")
                                    if not target.exists():
                                        write_object(target, {"source": "dsh", "session_id": sid,
                                            "turn": earlier["turn"], "source_seq": earlier["seq"],
                                            "role": "assistant_visible_fragment", "text": visible,
                                            "timestamp": earlier["time"]}, backup=False)
                                pending = {"turn": candidate["turn"], "seq": candidate["seq"],
                                           "frame_offset": start, "frame_sha256": frame_hash}
                            if event.get("type") == "turn/end":
                                if not isinstance(event.get("data"), dict):
                                    raise SourceFormatError("DSH 回合结束事件结构无效")
                                turn = event["data"].get("turn")
                                reason = event["data"].get("reason")
                                if not isinstance(reason, dict):
                                    raise SourceFormatError("DSH 回合结束原因结构无效")
                                completed = reason.get("kind") == "completed"
                                if pending and pending["turn"] == turn:
                                    answer_candidate = _read_candidate(path, pending) if completed else None
                                    if completed and not answer_candidate["text"]:
                                        raise SourceFormatError("DSH 已完成回合缺少可确认的最终答复")
                                    if completed:
                                        answer = NormalizedMessage(
                                            message_id="answer:" + str(turn) + ":" + answer_candidate["message_id"],
                                            role="assistant", content=answer_candidate["text"],
                                            timestamp=answer_candidate["time"],
                                            provenance={"adapter": PARSER_VERSION, "turn": turn,
                                                        "source_seq": answer_candidate["seq"],
                                                        "event_type": "turn/end"},
                                            sync_position={"event_seq": answer_candidate["seq"]})
                                        result = store.append_normalized_message(archive_key(), answer)
                                        saved += int(result["saved"])
                                        row["saved_messages"] = max(int(row.get("saved_messages") or 0),
                                                                    int(result["session"]["message_count"]))
                                    pending = None
                                elif completed:
                                    raise SourceFormatError("DSH 已完成回合缺少可确认的最终答复")
                        row["confirmed_offset"] = end
                        row["last_frame_start"] = start
                        row["last_frame_sha256"] = frame_hash
                        row["pending_turn"] = pending
                        row["pending_bytes"] = max(0, path.stat().st_size - end)
                        row["last_error"] = None
                        write_object(self.state_path, state, backup=True)
                    row["pending_bytes"] = max(0, path.stat().st_size -
                                               int(row.get("confirmed_offset") or 0))
                    for key, reference in list(row.get("missing_attachments", {}).items()):
                        match = SHA_ATTACHMENT.fullmatch(reference["reference"])
                        if not match:
                            continue
                        digest = match.group(1)
                        original = self.root / "attachments" / "v1" / "objects" / digest[:2] / digest
                        if not original.is_file() or original.is_symlink():
                            continue
                        if hashlib.sha256(original.read_bytes()).hexdigest() != digest:
                            continue
                        spec = NormalizedAttachment(reference=reference["reference"],
                            original_name=reference["original_name"], local_path=original,
                            mime_type=reference["mime_type"])
                        store.reconcile_attachments(archive_key(), reference["message_id"], [spec])
                        del row["missing_attachments"][key]
                    row["last_error"] = None
                except (OSError, ValueError, StateCorruptError) as exc:
                    row["last_error"] = type(exc).__name__ + ": " + str(exc)[:240]
                    state["last_error"] = "有 DSH 会话未完成解析；游标保留"
                write_object(self.state_path, state, backup=True)
            state["last_sync_at"] = _now()
            if not missing_selected and not any(row.get("last_error") for sid, row in
                                                state["sessions"].items() if sid in allowed):
                state["last_error"] = None
            write_object(self.state_path, state, backup=True)
            return {**self.status(), "saved_this_sync": saved}


def run(interval=5.0, stop_event=None):
    stop = stop_event or _stop
    while not stop.is_set():
        try:
            adapter = DSHSourceAdapter()
            if adapter._load()["policy"] != "paused":
                adapter.sync()
        except Exception as exc:
            try:
                if adapter._load()["policy"] != "paused":
                    with adapter.lock:
                        state = adapter._load()
                        state["last_error"] = "DSH 采集检查未完成：" + type(exc).__name__
                        write_object(adapter.state_path, state, backup=True)
            except Exception:
                pass
        stop.wait(interval)
