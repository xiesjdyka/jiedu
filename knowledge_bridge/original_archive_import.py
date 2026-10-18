"""Discover owner-supplied Markdown conversation masters in 90_原始档案.

The source Markdown is never rewritten. This module only keeps a small local
registry so the UI and the existing extraction pipeline can address each file
as a stable Session.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from .core import ROOT, VAULT
from .workspace import active_workspace

ARCHIVE_ROOT = active_workspace().raw_archive
STATE_PATH = active_workspace().state / "original-archive-imports.json"
SOURCE_PATTERN = re.compile(r"^(ChatGPT|Hermes|Codex)[_-](.+)\.md$", re.I)
SOURCE_FOLDERS = {"chatgpt": "chatgpt", "hermes": "hermes", "codex": "codex"}
ROLE_HEADING = re.compile(
    r"(?mi)^(?:#{1,6}\s*)?(?:\[?MESSAGE\s+([^\]\s｜|]+)\]?\s*[｜|:-]?\s*)?\[?(USER|ASSISTANT|SYSTEM)\]?\s*$"
)
VALID_STATES = {"未处理", "待提炼", "已提炼", "已归档"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_state(path: Path = STATE_PATH) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"records": {}}
    except (OSError, ValueError):
        return {"records": {}}


def _save_state(data: dict, path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _source_and_title(path: Path, text: str) -> tuple[str, str] | None:
    match = SOURCE_PATTERN.match(path.name)
    folder_source = next(
        (SOURCE_FOLDERS[part.casefold()] for part in reversed(path.parts[:-1])
         if part.casefold() in SOURCE_FOLDERS),
        None,
    )
    if match:
        source = match.group(1).lower()
        fallback = re.sub(r"[_-]+", " ", match.group(2)).strip()
    elif folder_source:
        source = folder_source
        fallback = re.sub(r"[_-]+", " ", path.stem).strip()
    else:
        return None
    heading = next(
        (line.lstrip("# ").strip() for line in text.splitlines()
         if line.startswith("# ") and not ROLE_HEADING.match(line)),
        "",
    )
    return source, heading or fallback or path.stem


def parse_messages(text: str, session_id: str) -> list[dict]:
    matches = list(ROLE_HEADING.finditer(text))
    rows: list[dict] = []
    for order, match in enumerate(matches, 1):
        body = text[match.end(): matches[order].start() if order < len(matches) else len(text)]
        body = body.lstrip("\r\n").rstrip("\r\n")
        if not body:
            continue
        message_id = str(match.group(1) or order)
        rows.append({
            "client_id": session_id.split(":", 1)[0],
            "session_id": session_id,
            "message_id": message_id,
            "role": match.group(2).lower(),
            "timestamp": None,
            "original_text": body,
            "message_order": order,
            "attachment_refs": re.findall(r"(?:@image:([^\r\n]+)|\[IMAGE:\s*([^\]]+)\])", body),
            "attachments": [],
            "hash": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "provenance": {"kind": "owner_markdown_master"},
        })
    for row in rows:
        row["attachment_refs"] = [a or b for a, b in row["attachment_refs"]]
    return rows


def scan(root: Path = ARCHIVE_ROOT, state_path: Path = STATE_PATH) -> dict:
    root = Path(root)
    data = _read_state(state_path)
    records = data.setdefault("records", {})
    seen: set[str] = set()
    try:
        extraction_state = json.loads((active_workspace().queues / "manual-chatgpt-extraction" / "state.json").read_text(encoding="utf-8"))
        processed = set(extraction_state.get("processed_messages", []))
    except (OSError, ValueError):
        processed = set()
    if root.exists():
        for path in sorted(root.rglob("*.md")):
            if path.name.casefold() == "conversation.md":
                continue
            relative_parts = [part.casefold() for part in path.resolve().relative_to(root.resolve()).parts]
            if "attachments" in relative_parts:
                continue
            try:
                text = path.read_text(encoding="utf-8-sig")
            except OSError:
                continue
            identified = _source_and_title(path, text)
            if not identified:
                continue
            source, title = identified
            relative = path.resolve().relative_to(root.resolve()).as_posix()
            session_id = f"{source}:md:{hashlib.sha256(relative.casefold().encode()).hexdigest()[:16]}"
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            messages = parse_messages(text, session_id)
            previous = records.get(session_id, {})
            derived_status = previous.get("status", "未处理")
            if messages and all(f"{session_id}\0{x['message_id']}" in processed for x in messages):
                derived_status = "已提炼"
            records[session_id] = {
                **previous,
                "session_id": session_id,
                "source": source,
                "title": title,
                "source_path": str(path.resolve()),
                "relative_path": relative,
                "source_sha256": digest,
                "source_bytes": path.stat().st_size,
                "updated_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
                "discovered_at": previous.get("discovered_at") or _now(),
                "message_count": len(messages),
                "status": derived_status,
                "messages": messages,
            }
            seen.add(session_id)
    for sid, record in records.items():
        record["present"] = sid in seen
    data["last_scan_at"] = _now()
    _save_state(data, state_path)
    return status(root, state_path, rescan=False)


def status(root: Path = ARCHIVE_ROOT, state_path: Path = STATE_PATH, *, rescan: bool = True) -> dict:
    if rescan:
        return scan(root, state_path)
    data = _read_state(state_path)
    rows = sorted(
        (x for x in data.get("records", {}).values() if x.get("present", True)),
        key=lambda x: str(x.get("updated_at") or ""),
        reverse=True,
    )
    return {
        "records": [{k: row.get(k) for k in (
            "session_id", "source", "title", "source_path", "relative_path",
            "source_sha256", "source_bytes", "updated_at", "discovered_at",
            "message_count", "status",
        )} for row in rows],
        "count": len(rows),
        "last_scan_at": data.get("last_scan_at"),
        "source_root": str(Path(root)),
    }


def set_status(session_id: str, value: str, state_path: Path = STATE_PATH, *, sync_extraction: bool = True) -> dict:
    if value not in VALID_STATES:
        raise ValueError("原始档案状态无效")
    data = _read_state(state_path)
    record = data.get("records", {}).get(session_id)
    if not record:
        raise ValueError("原始档案不存在")
    record["status"] = value
    record["status_updated_at"] = _now()
    _save_state(data, state_path)
    if sync_extraction and value == "待提炼":
        from .manual_extraction import set_session_state
        set_session_state(session_id, "pending")
    elif sync_extraction and value in {"未处理", "已归档"}:
        from .manual_extraction import set_session_state
        set_session_state(session_id, "deferred")
    return {"session_id": session_id, "status": value, "source_unchanged": True}


def session_records(state_path: Path = STATE_PATH, *, include_conversation=True, read_fresh=False) -> list[dict]:
    rows = []
    for record in _read_state(state_path).get("records", {}).values():
        if not record.get("present", True):
            continue
        source_path = Path(record["source_path"])
        # A stale scan record must not make every valid archive disappear.
        # The source registry remains untouched; a later rescan can restore it.
        if not source_path.is_file():
            continue
        messages = record.get("messages", [])
        text = source_path.read_text(encoding="utf-8-sig") if include_conversation and not read_fresh else ""
        source_hash = record["source_sha256"]
        updated_at = record.get("updated_at")
        if read_fresh:
            # Source records are a scan cache, not an immutable message version.
            # Reparse the current text for status without rewriting the registry.
            raw = source_path.read_bytes()
            text = raw.decode("utf-8-sig")
            source_hash = hashlib.sha256(raw).hexdigest()
            updated_at = datetime.fromtimestamp(source_path.stat().st_mtime, timezone.utc).isoformat()
            messages = parse_messages(text, record["session_id"])
        user_count = sum(x.get("role") == "user" for x in messages)
        assistant_count = sum(x.get("role") == "assistant" for x in messages)
        rows.append({
            "client": record["source"],
            "source": record["source"],
            "session_id": record["session_id"],
            "session_title": record["title"],
            "started_at": record.get("updated_at"),
            "updated_at": updated_at,
            "message_count": len(messages),
            "user_count": user_count,
            "assistant_count": assistant_count,
            "turn_count": min(user_count, assistant_count),
            "archive_path": record["source_path"],
            "conversation": text if include_conversation else "",
            "messages": messages,
            "hash": source_hash,
            "attachment_count": 0,
            "missing_attachment_count": sum(len(x.get("attachment_refs", [])) for x in messages),
            "attachments": [],
            "sync_status": "owner_master",
            "completeness": "原文母档",
            "manual_archive_status": record.get("status", "未处理"),
        })
    return rows


def get_session(session_id: str, state_path: Path = STATE_PATH) -> dict | None:
    return next((x for x in session_records(state_path) if x["session_id"] == session_id), None)
