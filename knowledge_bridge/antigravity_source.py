"""Read the visible fields of local Antigravity trajectories, never tool payloads.

Field numbers follow the embedded trajectory.proto/cortex.proto used by the
Windows SQLite format (steps.step_format=0). No client executable or RPC runs.
"""
import hashlib
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .archive_store import NormalizedAttachment, NormalizedMessage


def _stat(p):
    s = p.stat()
    return s.st_ino, s.st_size, s.st_mtime_ns


@contextmanager
def snapshot(path):
    # immutable=1 avoids even creating shared-memory files in the source.
    def checked():
        if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-journal")):
            raise ValueError("来源正在写入，等待稳定快照")
        return _stat(path)
    before = checked()
    db = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 32 * 1024 * 1024)
        yield db
        if checked() != before:
            raise ValueError("读取期间来源发生变化，等待重试")
    finally:
        db.close()


def discover(home):
    base = home / ".gemini/antigravity"
    index = base / "conversation_summaries.db"
    if not index.is_file():
        return []
    if not index.resolve().is_relative_to(home):
        raise ValueError("索引不在来源目录")
    with snapshot(index) as db:
        rows = db.execute("select conversation_id,title,last_modified_time from conversation_summaries").fetchall()
    items = []
    indexed = {sid:(title,updated) for sid,title,updated in rows}
    folder = base / "conversations"
    for sid in sorted(set(indexed) | {p.stem for p in folder.glob("*.db")}):
        if str(uuid.UUID(sid)) != sid:
            raise ValueError("未知会话编号")
        p = folder / (sid + ".db")
        if not p.resolve().is_relative_to(base.resolve()) or not base.resolve().is_relative_to(home):
            raise ValueError("会话文件不在来源目录")
        title, updated = indexed.get(sid, ("Antigravity", None))
        items.append(dict(session_id=sid, title=title or "Antigravity", updated=updated,
                          path=str(p), source_bytes=p.stat().st_size if p.is_file() else 0))
    return items


def _varint(data, pos):
    n = 0
    for shift in range(0, 70, 7):
        if pos >= len(data):
            break
        b = data[pos]
        pos += 1
        n |= (b & 127) << shift
        if b < 128:
            return n, pos
    raise ValueError("来源 protobuf 数据不完整")


def _fields(data, wanted):
    result = {}
    pos = 0
    while pos < len(data):
        tag, pos = _varint(data, pos)
        key, wire = tag >> 3, tag & 7
        if not key:
            raise ValueError("来源 protobuf 字段无效")
        if wire == 0:
            val, pos = _varint(data, pos)
        elif wire in (1, 2, 5):
            if wire == 2:
                size, pos = _varint(data, pos)
            else:
                size = 8 if wire == 1 else 4
            if size > len(data) - pos:
                raise ValueError("来源 protobuf 长度无效")
            val = data[pos:pos+size] if key in wanted else None
            pos += size
        else:
            raise ValueError("来源 protobuf 格式未支持")
        if key in wanted:
            if wire != wanted[key]:
                raise ValueError("来源 protobuf 字段类型已变化")
            result.setdefault(key, []).append(val)
    return result


def _one(d, k, default=b""):
    vals = d.get(k, [])
    if len(vals) > 1:
        raise ValueError("来源字段重复")
    return vals[0] if vals else default


def _media(raw, folder, index):
    m = _fields(raw, {1:2, 2:2, 5:2, 9:2})
    uri = _one(m, 5).decode()
    u = urlsplit(uri)
    p = Path(unquote(u.path).lstrip("/")) if u.scheme == "file" and not u.netloc else (Path(uri) if uri and not uri.startswith(("\\\\", "//")) and not u.netloc else None)
    inline = _one(m, 2)
    local = p if p and p.is_absolute() and p.resolve().is_relative_to(folder) and p.is_file() else None
    ref = hashlib.sha256((str(index) + ":" + uri).encode() + inline).hexdigest()
    name = Path(_one(m, 9).decode()).name or (p.name if p else "image")
    return NormalizedAttachment(reference="antigravity-media:" + ref, original_name=name,
                                mime_type=_one(m, 1).decode() or None, media_type="image",
                                local_path=local, inline_data=inline or None)


def messages(item):
    path = Path(item["path"])
    sid = item["session_id"]
    media_root = path.parent.parent / "brain" / sid
    if not media_root.resolve().is_relative_to(path.parent.parent.resolve()):
        raise ValueError("附件目录超出来源范围")
    result, ids, pending = [], set(), 0
    with snapshot(path) as db:
        if db.execute("select cascade_id from trajectory_meta").fetchall() != [(sid,)]:
            raise ValueError("来源会话身份不匹配")
        for idx, kind, status, fmt, payload, render in db.execute(
            "select idx,step_type,status,step_format,step_payload,render_info from steps "
            "where step_type in (14,15) order by idx"):
            if fmt != 0:
                raise ValueError("未识别的轨迹版本")
            if status != 3:
                pending += 1
                continue
            if _one(_fields(render or b"", {4:0}), 4, 0):
                continue
            s = _fields(payload, {1:0,4:0,5:2,19:2,20:2})
            if _one(s,1) != kind or _one(s,4) != status:
                raise ValueError("来源轨迹字段不一致")
            md = _fields(_one(s,5), {3:0,1:2,35:2})
            is_user = kind == 14
            if _one(md,3) != (4 if is_user else 2):
                continue
            if is_user:
                row = _fields(_one(s,19), {2:2,9:2,19:2})
                role, id_field, content = "user", 19, _one(row,2)
                attachments = tuple(_media(m,media_root.resolve(),n) for n,m in enumerate(row.get(9,[])))
            else:
                row = _fields(_one(s,20), {1:2,8:2,6:2})
                role, id_field, content = "assistant", 6, _one(row,8) or _one(row,1)
                attachments = ()
            if not content.strip() and not attachments:
                continue
            native = _one(row,id_field) or _one(md,35)
            if not native:
                raise ValueError("可见消息缺少稳定编号")
            mid = role + ":" + hashlib.sha256(native).hexdigest()
            if mid in ids:
                raise ValueError("可见消息编号重复")
            ids.add(mid)
            t = _fields(_one(md,1), {1:0,2:0})
            timestamp = datetime.fromtimestamp(_one(t,1,0),timezone.utc).replace(
                microsecond=_one(t,2,0)//1000).isoformat() if t else None
            result.append(NormalizedMessage(mid, role, content.decode(), timestamp, attachments,
                provenance={"adapter":"antigravity_local","position":idx}, sync_position={"byte_offset":idx}))
    return result, pending
