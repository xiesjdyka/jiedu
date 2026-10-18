"""Read-only Doubao Work IndexedDB snapshots and public message extraction.

The live Chromium LevelDB is never opened directly.  A snapshot is accepted
only when the complete file inventory and CURRENT pointer are unchanged before
and after copying.  Decoding failures are reported to the caller so it can keep
the last confirmed cursor and archive state.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen


SUPPORTED_LIST_SCHEMAS = {
    "conversation-list-data-project-v3",
    "conversation-list-data-non-project-v2",
    "conversation-list-data-device-group-v1",
    "conversation-list-data-non-device-v1",
    "conversation-list-data-overall-v2",
}
TASK_STORE_FRAGMENT = "samantha-web-super-task-store"
PUBLIC_TEXT_BLOCK_TYPE = 10000
PUBLIC_ATTACHMENT_BLOCK_TYPES = {6, 7, 10012, 10020, 10052, 10063}
SIGNED_ATTACHMENT_HOST_SUFFIXES = (".byteimg.com", ".bytedance.net")
MAX_ATTACHMENT_BYTES = 32 * 1024 * 1024
# A profile enters this allowlist only in a reviewed release after real Doubao
# UI samples validate continuous turns, final replies, restart dedupe and files.
# This gives validation a durable release gate without per-user/per-turn prompts.
CACHE_PUBLICATION_PROFILE = "doubao-work-2.30.2-indexeddb-v1"
VALIDATED_PUBLICATION_PROFILES: frozenset[str] = frozenset()


class SnapshotBusyError(RuntimeError):
    pass


class UnsupportedDoubaoSchema(RuntimeError):
    pass


class UnsafeAttachmentReference(ValueError):
    pass


@dataclass(frozen=True)
class CacheMessage:
    conversation_id: str
    message_id: str
    role: str
    content: str
    order: int
    sequence: int
    completed: bool
    task_id: str | None = None
    attachments: tuple[dict, ...] = ()
    provenance: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CacheReadResult:
    messages: tuple[CacheMessage, ...]
    titles: dict[str, str]
    max_sequence: int
    decode_errors: int
    task_records: int
    schemas: tuple[str, ...]
    current_decode_errors: int = 0
    ignored_historical_decode_errors: int = 0
    publication_ready: bool = False
    current_state_verified: bool = False


def default_cache_paths() -> tuple[Path, Path]:
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    root = local / "Doubao" / "User Data" / "Default" / "IndexedDB"
    return (root / "chrome_doubao-chat_0.indexeddb.leveldb",
            root / "chrome_doubao-chat_0.indexeddb.blob")


def _inventory(root: Path) -> tuple[tuple[str, int, int], ...]:
    if not root.is_dir():
        return ()
    rows = []
    for path in sorted((item for item in root.rglob("*") if item.is_file()),
                       key=lambda item: item.relative_to(root).as_posix()):
        if path.name == "LOCK":
            continue
        stat = path.stat()
        rows.append((path.relative_to(root).as_posix(), stat.st_size, stat.st_mtime_ns))
    return tuple(rows)


def cache_signature(leveldb: Path, blob: Path) -> tuple:
    current = (leveldb / "CURRENT").read_bytes() if (leveldb / "CURRENT").is_file() else b""
    return (_inventory(leveldb), _inventory(blob), hashlib.sha256(current).hexdigest())


def _validate_leveldb(root: Path) -> None:
    current_path = root / "CURRENT"
    if not current_path.is_file():
        raise SnapshotBusyError("IndexedDB snapshot has no CURRENT pointer")
    manifest_name = current_path.read_text("ascii", errors="strict").strip()
    if not manifest_name.startswith("MANIFEST-") or not (root / manifest_name).is_file():
        raise SnapshotBusyError("IndexedDB CURRENT does not name a copied manifest")


def consistent_snapshot(leveldb: Path, blob: Path, destination: Path, *, attempts: int = 3,
                        retry_delay: float = 0.05,
                        after_copy: Callable[[int], None] | None = None) -> dict:
    """Copy a live LevelDB without ever replacing the last accepted snapshot."""
    leveldb = leveldb.expanduser().resolve(); blob = blob.expanduser().resolve()
    if not leveldb.is_dir():
        raise FileNotFoundError(leveldb)
    destination = destination.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    managed_root = destination.parent
    last_reason = "source changed while copying"
    for attempt in range(max(1, attempts)):
        staging = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.pending")
        retry = False
        try:
            before = cache_signature(leveldb, blob)
            shutil.copytree(leveldb, staging / "leveldb", ignore=shutil.ignore_patterns("LOCK"))
            if blob.is_dir():
                shutil.copytree(blob, staging / "blob")
            else:
                (staging / "blob").mkdir(parents=True)
            if after_copy:
                after_copy(attempt)
            after = cache_signature(leveldb, blob)
            copied = cache_signature(staging / "leveldb", staging / "blob")
            _validate_leveldb(staging / "leveldb")
            if before != after or copied != before:
                last_reason = "source inventory or CURRENT changed while copying"
                retry = True
            if retry:
                pass
            else:
                previous = destination.with_name(f".{destination.name}.previous")
                _safe_remove_tree(previous, managed_root)
                if destination.exists():
                    os.replace(destination, previous)
                os.replace(staging, destination)
                _safe_remove_tree(previous, managed_root)
                return {"attempts": attempt + 1, "signature": hashlib.sha256(repr(after).encode()).hexdigest(),
                        "file_count": len(after[0]) + len(after[1])}
        except (OSError, PermissionError) as exc:
            last_reason = f"snapshot copy unavailable: {type(exc).__name__}: {exc}"
            retry = True
        finally:
            _safe_remove_tree(staging, managed_root, ignore_errors=True)
        if attempt + 1 < attempts:
            time.sleep(retry_delay * (attempt + 1))
    raise SnapshotBusyError(last_reason)


def _safe_remove_tree(path: Path, managed_root: Path, *, ignore_errors: bool = False) -> None:
    path = path.resolve(); managed_root = managed_root.resolve()
    if path.parent != managed_root:
        raise RuntimeError(f"refusing to remove unmanaged snapshot path: {path}")
    if path.exists():
        shutil.rmtree(path, ignore_errors=ignore_errors)


def _plain_key(key: Any) -> str:
    value = getattr(key, "value", None)
    return str(value if value is not None else key)


def _state(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    value = value.get("state", value)
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _repair_text(value: Any) -> str:
    text = str(value or "")
    if "\ufffd" in text or not text:
        return text
    # Some Electron snapshots are returned by older V8 readers as UTF-8 bytes
    # interpreted through a single-byte codec.  Repair only when round-tripping
    # is lossless and the result has fewer C1/control characters.
    try:
        repaired = text.encode("latin1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text
    bad = lambda item: sum(1 for char in item if 0x80 <= ord(char) <= 0x9F)
    return repaired if bad(repaired) < bad(text) else text


def _walk_conversations(data: Any) -> Iterable[dict]:
    if isinstance(data, dict):
        if data.get("conversation_id"):
            yield data
        for value in data.values():
            yield from _walk_conversations(value)
    elif isinstance(data, list):
        for value in data:
            yield from _walk_conversations(value)


def _message_list(value: Any) -> list[dict]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [item for item in value.values() if isinstance(item, dict)]
    return []


def _public_text(message: dict) -> str:
    blocks = message.get("content_blocks_v2") or message.get("contentBlocks") or []
    if isinstance(blocks, dict):
        blocks = list(blocks.values())
    result = []
    for block in blocks if isinstance(blocks, list) else []:
        if not isinstance(block, dict) or int(block.get("block_type") or block.get("blockType") or 0) != PUBLIC_TEXT_BLOCK_TYPE:
            continue
        content = block.get("content") if isinstance(block.get("content"), dict) else block
        text_block = content.get("text_block") if isinstance(content.get("text_block"), dict) else content.get("textBlock")
        text = text_block.get("text") if isinstance(text_block, dict) else None
        if isinstance(text, str) and text.strip():
            result.append(text.strip())
    return "\n\n".join(result).strip()


def _attachments(message: dict) -> tuple[dict, ...]:
    refs = []
    blocks = message.get("content_blocks_v2") or []
    if isinstance(blocks, dict):
        blocks = list(blocks.values())
    for block in blocks if isinstance(blocks, list) else []:
        if not isinstance(block, dict):
            continue
        block_type = int(block.get("block_type") or block.get("blockType") or 0)
        if block_type not in PUBLIC_ATTACHMENT_BLOCK_TYPES:
            continue
        content = block.get("content") if isinstance(block.get("content"), dict) else {}
        candidates = []
        def visit(value: Any) -> None:
            if isinstance(value, dict):
                candidates.append(value)
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
        visit(content)
        for container in candidates:
            for key in ("url", "uri", "file_url", "download_url"):
                ref = container.get(key)
                if isinstance(ref, str) and ref:
                    item = {"reference": ref, "block_type": block_type,
                            "name": container.get("name") or container.get("file_name") or
                                    content.get("name") or content.get("file_name")}
                    if item not in refs:
                        refs.append(item)
            for key in ("local_path", "path"):
                ref = container.get(key)
                if isinstance(ref, str) and ref and Path(ref).is_absolute():
                    item = {"reference": ref, "block_type": block_type,
                            "name": container.get("name") or container.get("file_name") or Path(ref).name}
                    if item not in refs:
                        refs.append(item)
    return tuple(refs)


def validate_signed_attachment_url(reference: str, *, now: int | None = None) -> str:
    parsed = urlparse(reference)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme != "https" or not any(host.endswith(suffix) for suffix in SIGNED_ATTACHMENT_HOST_SUFFIXES):
        raise UnsafeAttachmentReference("非豆包 HTTPS 附件域名")
    query = {key.casefold(): value for key, value in parse_qs(parsed.query).items()}
    signature = query.get("x-signature") or query.get("signature")
    expires = query.get("x-expires") or query.get("expires")
    if not signature or not expires:
        raise UnsafeAttachmentReference("缺少签名或过期时间")
    try:
        expiry = int(expires[0])
    except (ValueError, TypeError, IndexError) as exc:
        raise UnsafeAttachmentReference("附件过期时间无效") from exc
    if expiry <= int(time.time() if now is None else now):
        raise UnsafeAttachmentReference("附件签名已过期")
    return reference


def redacted_attachment_reference(reference: str) -> str:
    parsed = urlparse(reference)
    digest = hashlib.sha256(reference.encode("utf-8")).hexdigest()[:16]
    if parsed.scheme in {"http", "https"} and parsed.hostname:
        return f"{parsed.scheme}://{parsed.hostname}{parsed.path}?signed_reference_sha256={digest}"
    path = Path(reference)
    if path.is_absolute():
        return f"local:{path.name}:{digest}"
    return reference.split("?", 1)[0]


def fetch_signed_attachment(reference: str, *, opener: Callable = urlopen,
                            max_bytes: int = MAX_ATTACHMENT_BYTES) -> tuple[bytes, str, str]:
    validate_signed_attachment_url(reference)
    request = Request(reference, headers={"User-Agent": "Jiedu-Doubao-Archive/1.0"})
    with opener(request, timeout=15) as response:
        final_url = response.geturl()
        validate_signed_attachment_url(final_url)
        length = response.headers.get("Content-Length")
        if length and int(length) > max_bytes:
            raise UnsafeAttachmentReference("附件超过归档大小上限")
        mime = str(response.headers.get_content_type()).casefold()
        allowed = (mime.startswith("image/") or mime in {
            "application/pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        })
        if not allowed:
            raise UnsafeAttachmentReference(f"未验证的附件类型: {mime}")
        raw = response.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise UnsafeAttachmentReference("附件超过归档大小上限")
    name = Path(unquote(urlparse(final_url).path)).name or "附件"
    return raw, mime, name


def _message_terminal(message: dict) -> bool:
    extra = message.get("extra") if isinstance(message.get("extra"), dict) else {}
    ext = extra.get("ext") if isinstance(extra.get("ext"), dict) else {}
    final = extra.get("final_status") if isinstance(extra.get("final_status"), dict) else {}
    # Installed Doubao 2.30.2 defines FINALLY=4. Its ReplyFinish handler sets
    # message.extra.stage=FINALLY, while completed full messages carry
    # final_status.message="Success"; parent messages also receive is_finish=1.
    return int(extra.get("stage") or -99) == 4 and final.get("message") == "Success" and (
        ext.get("is_finish") in {None, "1", 1, True})


def extract_task_messages(state: Any, sequence: int) -> list[CacheMessage]:
    if not isinstance(state, dict):
        return []
    task_map = state.get("mainTaskDataMap")
    if not isinstance(task_map, dict):
        return []
    result = []
    for task_key, task in task_map.items():
        if not isinstance(task, dict):
            continue
        query = task.get("requestQuery") if isinstance(task.get("requestQuery"), dict) else {}
        sync_task = query.get("syncTask") if isinstance(query.get("syncTask"), dict) else {}
        groups = (task.get("sentMessages") or sync_task.get("messages"), task.get("receivedMessages"))
        for items in groups:
            for message in _message_list(items):
                extra = message.get("extra") if isinstance(message.get("extra"), dict) else {}
                conversation_id = str(message.get("conversation_id") or extra.get("conversation_id") or
                                      task.get("conversationId") or task.get("conversation_id") or "")
                message_id = str(message.get("message_id") or extra.get("message_id") or message.get("id") or "")
                user_type = message.get("user_type") or extra.get("user_type")
                role = "user" if str(user_type) == "1" else "assistant" if str(user_type) == "2" else None
                content = _public_text(message)
                attachments = _attachments(message)
                if not conversation_id or not message_id or role is None or (not content and not attachments):
                    continue
                # User input is immutable once sent. Assistant output is public
                # only after the task has an explicit terminal marker.
                message_complete = _message_terminal(message) if role == "assistant" else True
                if role == "assistant" and not message_complete:
                    continue
                order = int(message.get("index") or extra.get("index") or 0)
                result.append(CacheMessage(conversation_id, message_id, role, content, order,
                    int(sequence), message_complete, str(task_key),
                    attachments, {"cache_stage": task.get("stage"), "user_type": user_type}))
    return result


def _key_identity(record: Any) -> bytes | str:
    raw = getattr(getattr(record, "key", None), "raw_key", None)
    return raw if isinstance(raw, bytes) else _plain_key(getattr(record, "key", ""))


def _active_file_numbers(raw_leveldb: Any) -> tuple[set[int], set[int]]:
    active_tables: set[int] = set(); log_number = None; previous_log = None
    manifest = getattr(raw_leveldb, "manifest", None)
    if manifest is None:
        raise UnsupportedDoubaoSchema("LevelDB manifest unavailable")
    leveldb_root = getattr(raw_leveldb, "in_dir_path", None)
    manifest_path = getattr(manifest, "path", None)
    if leveldb_root is not None and manifest_path is not None:
        leveldb_root = Path(leveldb_root)
        current_path = leveldb_root / "CURRENT"
        try:
            current_manifest = current_path.read_text("ascii", errors="strict").strip()
        except (OSError, UnicodeError) as exc:
            raise UnsupportedDoubaoSchema("LevelDB CURRENT pointer unavailable") from exc
        manifest_path = Path(manifest_path)
        if (current_manifest != manifest_path.name or
                manifest_path.parent.resolve() != leveldb_root.resolve()):
            raise UnsupportedDoubaoSchema("LevelDB parser did not open the CURRENT manifest")
    for edit in manifest:
        for deleted in getattr(edit, "deleted_files", ()):
            active_tables.discard(int(deleted.file_no))
        for added in getattr(edit, "new_files", ()):
            active_tables.add(int(added.file_no))
        if getattr(edit, "log_number", None) is not None:
            log_number = int(edit.log_number)
        if getattr(edit, "prev_log_number", None) is not None:
            previous_log = int(edit.prev_log_number)
    logs = {value for value in (log_number, previous_log) if value}
    return active_tables, logs


def _current_raw_for_store(store: Any) -> dict[bytes, Any] | None:
    raw_db = getattr(store, "_raw_db", None)
    raw_leveldb = getattr(raw_db, "_db", None)
    if raw_db is None or raw_leveldb is None or not hasattr(raw_db, "_fetched_records"):
        return None
    active_tables, active_logs = _active_file_numbers(raw_leveldb)
    prefix = raw_db.make_prefix(store._dbid_no, store._obj_store_id, 1)
    latest: dict[bytes, Any] = {}
    for record in raw_db._fetched_records:
        origin = Path(record.origin_file)
        try:
            number = int(origin.stem, 10)
        except ValueError:
            continue
        active = (origin.suffix.casefold() in {".ldb", ".sst"} and number in active_tables) or (
            origin.suffix.casefold() == ".log" and number in active_logs)
        if not active or not record.user_key.startswith(prefix):
            continue
        prior = latest.get(record.user_key)
        if prior is None or int(record.seq) > int(prior.seq):
            latest[record.user_key] = record
    return latest


def _latest_records(store: Any) -> tuple[list[Any], int, int, bool]:
    """Return only the effective latest record for each IndexedDB raw key."""
    bad = []
    decoded = list(store.iterate_records(bad_deserializer_data_handler=lambda *_: bad.append(1)))
    raw_latest = _current_raw_for_store(store)
    if raw_latest is None:
        latest = {}
        for record in decoded:
            key = _key_identity(record)
            if key not in latest or int(record.ldb_seq_no) > int(latest[key].ldb_seq_no):
                latest[key] = record
        return list(latest.values()), len(bad), 0, False
    decoded_by_version = {(_key_identity(record), int(record.ldb_seq_no)): record for record in decoded}
    current = []; current_errors = 0
    for raw_user_key, raw_record in raw_latest.items():
        # Strip the database/store/index prefix to match IdbKey.raw_key.
        raw_key = raw_user_key[len(store._raw_db.make_prefix(store._dbid_no, store._obj_store_id, 1)):]
        if getattr(raw_record.state, "name", "").casefold() == "deleted":
            continue
        record = decoded_by_version.get((raw_key, int(raw_record.seq)))
        if record is None:
            current_errors += 1
        else:
            current.append(record)
    return current, current_errors, max(0, len(bad) - current_errors), True


def read_snapshot(snapshot: Path, *, wrapper_factory: Callable | None = None) -> CacheReadResult:
    if wrapper_factory is None:
        try:
            from ccl_chromium_reader import ccl_chromium_indexeddb
        except ImportError as exc:
            raise RuntimeError("ccl_chromium_reader is required for Doubao IndexedDB") from exc
        wrapper_factory = ccl_chromium_indexeddb.WrappedIndexDB
    db = wrapper_factory(snapshot / "leveldb", snapshot / "blob")
    titles: dict[str, tuple[int, str]] = {}
    messages: dict[tuple[str, str], CacheMessage] = {}
    schemas = set(); errors = 0; historical_errors = 0; task_records = 0; max_sequence = 0
    current_verified = True
    try:
      for meta in db.database_ids:
        database = db[meta.dbid_no]
        names = set(database.object_store_names)
        if "conversationListDataSnapshots" in names:
            records, current_bad, old_bad, verified = _latest_records(database["conversationListDataSnapshots"])
            errors += current_bad; historical_errors += old_bad; current_verified &= verified
            for record in records:
                max_sequence = max(max_sequence, int(record.ldb_seq_no))
                if not record.is_live or not isinstance(record.value, dict):
                    continue
                schema = str(record.value.get("schema") or "")
                if schema:
                    schemas.add(schema)
                    if schema not in SUPPORTED_LIST_SCHEMAS:
                        raise UnsupportedDoubaoSchema(schema)
                for row in _walk_conversations(record.value.get("data")):
                    if row.get("deleted") is True or int(row.get("conversation_status") or 1) == 0:
                        continue
                    conversation_id = str(row.get("conversation_id") or "")
                    name = _repair_text(row.get("name"))
                    version = int(float(row.get("version") or row.get("conv_version") or record.ldb_seq_no))
                    if conversation_id and name and (conversation_id not in titles or version >= titles[conversation_id][0]):
                        titles[conversation_id] = (version, name)
        if "store" in names:
            records, current_bad, old_bad, verified = _latest_records(database["store"])
            errors += current_bad; historical_errors += old_bad; current_verified &= verified
            for record in records:
                max_sequence = max(max_sequence, int(record.ldb_seq_no))
                if not record.is_live or TASK_STORE_FRAGMENT not in _plain_key(record.key):
                    continue
                task_records += 1
                for message in extract_task_messages(_state(record.value), int(record.ldb_seq_no)):
                    key = (message.conversation_id, message.message_id)
                    if key not in messages or message.sequence >= messages[key].sequence:
                        messages[key] = message
    finally:
        close = getattr(db, "close", None)
        if callable(close):
            close()
    ordered = tuple(sorted(messages.values(), key=lambda row: (row.conversation_id, row.order, row.sequence, row.message_id)))
    return CacheReadResult(ordered, {key: value[1] for key, value in titles.items()},
                           max_sequence, errors + historical_errors, task_records, tuple(sorted(schemas)),
                           current_decode_errors=errors,
                           ignored_historical_decode_errors=historical_errors,
                           publication_ready=(CACHE_PUBLICATION_PROFILE in VALIDATED_PUBLICATION_PROFILES),
                           current_state_verified=current_verified and errors == 0)
