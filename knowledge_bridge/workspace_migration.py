"""Conservative migration helpers: copy, verify, record; never delete legacy data."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .workspace import PROGRAM_ROOT, WorkspaceContext, active_workspace


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def legacy_runtime_preview(context: WorkspaceContext | None = None) -> dict:
    context = context or active_workspace()
    legacy = PROGRAM_ROOT / "data"
    mappings = [
        (legacy / "sessions" / "sessions.json", context.sessions_state, "portable_session_ledger"),
        (legacy / "sessions" / "raw-archives", context.raw_message_store, "append_only_messages"),
        (legacy / "sessions" / "attachments", context.state / "attachments", "attachment_entities"),
        (legacy / "original-archive-imports.json", context.state / "original-archive-imports.json", "archive_registry"),
        (legacy / "manual-chatgpt-extraction", context.queues / "manual-chatgpt-extraction", "review_queue"),
    ]
    operations = []
    for source, target, kind in mappings:
        exists = source.exists()
        count = len(list(source.rglob("*"))) if source.is_dir() else int(source.is_file())
        operations.append({"kind": kind, "source": str(source), "target": context.relative(target),
                           "source_exists": exists, "source_items": count, "target_exists": target.exists(),
                           "action": "skip_existing" if target.exists() else "copy" if exists else "none"})
    return {"workspace_id": context.workspace_id, "preview": True, "operations": operations,
            "deletes": [], "legacy_archive_reorganization": "preview_only"}


def migrate_legacy_runtime(context: WorkspaceContext | None = None) -> dict:
    context = context or active_workspace()
    preview = legacy_runtime_preview(context)
    completed = []
    for operation in preview["operations"]:
        if operation["action"] != "copy":
            completed.append(operation | {"result": operation["action"]})
            continue
        source = Path(operation["source"])
        target = context.root / operation["target"]
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, copy_function=shutil.copy2)
            source_files = {x.relative_to(source).as_posix(): _digest(x) for x in source.rglob("*") if x.is_file()}
            target_files = {x.relative_to(target).as_posix(): _digest(x) for x in target.rglob("*") if x.is_file()}
            verified = source_files == target_files
        else:
            shutil.copy2(source, target)
            verified = _digest(source) == _digest(target)
        if not verified:
            raise OSError(f"迁移校验失败：{operation['kind']}")
        completed.append(operation | {"result": "copied_verified", "verified": True})
    record = {"migration": "legacy-program-data-to-workspace-v1", "workspace_id": context.workspace_id,
              "completed_at": _now(), "operations": completed, "source_preserved": True, "deletes": []}
    _atomic_json(context.audit / "migration-legacy-runtime-v1.json", record)
    return record


def legacy_archive_preview(context: WorkspaceContext | None = None) -> dict:
    context = context or active_workspace()
    rows = []
    if context.raw_archive.exists():
        for manifest_path in context.raw_archive.glob("*/**/manifest.json"):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            source = str(manifest.get("client") or manifest.get("source") or manifest_path.parts[-3]).casefold()
            family = "ChatGPT" if source in {"chatgpt", "chatgpt_web", "codex"} else "Hermes" if source == "hermes" else None
            rows.append({"manifest": context.relative(manifest_path), "session_id": manifest.get("session_id"),
                         "current_path": context.relative(manifest_path.parent), "proposed_family": family,
                         "proposed_title": manifest.get("session_title") or manifest.get("title"),
                         "action": "needs_source_confirmation" if family is None else "copy_then_verify",
                         "executed": False})
    result = {"workspace_id": context.workspace_id, "generated_at": _now(), "preview_only": True,
              "sessions": rows, "delete_legacy": False, "merge_without_identity": False}
    _atomic_json(context.audit / "legacy-archive-layout-preview.json", result)
    return result


def backup_legacy_index(context: WorkspaceContext | None = None) -> dict:
    """Copy the old derived index before rebuilding; source remains untouched."""
    context = context or active_workspace()
    source_root = PROGRAM_ROOT / "data"
    candidates = [source_root / "lancedb", source_root / "state.json", source_root / "vector_map.json",
                  source_root / "context-windows.json", source_root / "context-windows.npy"]
    fingerprint = hashlib.sha256("\n".join(f"{x.name}:{x.stat().st_mtime_ns}:{x.stat().st_size if x.is_file() else 0}" for x in candidates if x.exists()).encode()).hexdigest()[:16]
    target = context.versions / "index-backups" / fingerprint
    records = []
    for source in candidates:
        if not source.exists():
            continue
        destination = target / source.name
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, destination, copy_function=shutil.copy2)
            else:
                shutil.copy2(source, destination)
        file_count = sum(1 for x in destination.rglob("*") if x.is_file()) if destination.is_dir() else 1
        size = sum(x.stat().st_size for x in destination.rglob("*") if x.is_file()) if destination.is_dir() else destination.stat().st_size
        records.append({"source_name": source.name, "backup_path": context.relative(destination),
                        "file_count": file_count, "bytes": size})
    result = {"backup_id": fingerprint, "created_at": _now(), "workspace_id": context.workspace_id,
              "source_preserved": True, "records": records}
    _atomic_json(target / "backup-manifest.json", result)
    return result
