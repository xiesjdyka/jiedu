"""Read-only governance view over L0 archives and Owner-controlled settings."""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .bridge_settings import load_settings, save_settings
from .core import VAULT
from .session_archive import list_archives
from .workspace import active_workspace


_REFRESH_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cache_path() -> Path:
    return active_workspace().state / "knowledge-governance-snapshot.json"


def _read_cache() -> dict:
    try:
        value = json.loads(_cache_path().read_text("utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_cache(value: dict) -> None:
    path = _cache_path(); path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", "utf-8")
    os.replace(temporary, path)


def _project_names() -> list[str]:
    root = VAULT / "02_项目"
    if not root.is_dir():
        return []
    return sorted({item.name for item in root.iterdir() if item.is_dir() and not item.name.startswith(".")})


def _project_intro(detail: dict) -> str:
    """A short literal excerpt from a saved overview, never an inferred status."""
    text=re.sub(r'\A---\s*\n.*?\n---\s*\n','',detail.get('overview') or '',flags=re.S)
    for paragraph in re.split(r'\n\s*\n',text):
        lines=[line.strip() for line in paragraph.splitlines() if line.strip()]
        if not lines or lines[0].startswith(('#','>','```','|','---','- ','* ')):continue
        excerpt=' '.join(lines)
        return excerpt[:240]+('…' if len(excerpt)>240 else '')
    return ''


def project_change_suggestions(archives=None) -> list[dict]:
    """Separate verified change candidates from mere project-name references."""
    projects = _project_names()
    if not projects:
        return []
    grouped = {project: [] for project in projects}
    for archive in archives if archives is not None else list_archives():
        title = str(archive.get("session_title") or "")
        messages = archive.get("messages") or []
        searchable = (title + "\n" + "\n".join(str(row.get("original_text") or row.get("content") or "") for row in messages)).casefold()
        for project in projects:
            if project and project.casefold() in searchable:
                evidence = next((row for row in messages if project.casefold() in str(row.get("original_text") or row.get("content") or "").casefold()), None)
                grouped[project].append({
                    "client": archive.get("client"),
                    "session_id": archive.get("session_id"),
                    "session_title": title,
                    "message_id": (evidence or {}).get("message_id"),
                    "updated_at": archive.get("updated_at"),
                })
    from .memory_control import project_detail
    from .memory_pipeline import list_candidates
    candidates=[row for row in list_candidates() if row.get('type')=='project_state' and row.get('status') in {'pending','approved_staging'}]
    result=[]
    for project in projects:
        unique={str(row.get('session_id')):row for row in grouped[project] if row.get('session_id')}
        related=sorted(unique.values(),key=lambda row:str(row.get('updated_at') or ''),reverse=True)
        changes=[row for row in candidates if row.get('project')==project]
        if not related and not changes:continue
        current=project_detail(project)
        result.append({
            'project':project,
            'kind':'verified_change' if changes else 'related_sources',
            'status':'有明确状态变更候选' if changes else '仅发现相关来源',
            'reason':('已有经过来源约束的项目状态候选，请核对实际差异' if changes else
                      '这些会话提到了项目，但没有检测到可验证的状态差异，不作为待办'),
            'current_status':current.get('summary') or current.get('status'),
            'current_state_path':current.get('source'),
            'project_intro':_project_intro(current),
            'project_intro_source':current.get('overview_source'),
            'related_source_count':len(related),
            'related_sources':related[:5],
            'change_candidates':[{'candidate_id':row['candidate_id'],'title':row.get('title'),'content':row.get('content'),
                                  'target_path':(row.get('provenance') or {}).get('target_path'),
                                  'created_at':row.get('created_at')} for row in changes],
            'updated_at':max([str(row.get('updated_at') or '') for row in related]+[str(row.get('updated_at') or '') for row in changes] or ['']),
        })
    return sorted(result,key=lambda item:(item['kind']!='verified_change',str(item.get('updated_at') or '')),reverse=False)


def refresh_snapshot() -> dict:
    if not _REFRESH_LOCK.acquire(blocking=False):
        return status()
    try:
        archives = list_archives()
        counts = {"chatgpt": 0, "hermes": 0, "codex": 0, "other": 0}
        for item in archives:
            source = str(item.get("client") or item.get("source") or "other").lower()
            counts[source if source in counts else "other"] += 1
        value = {
            "archive_counts": counts,
            "project_change_suggestions": project_change_suggestions(archives),
            "snapshot_updated_at": _now(), "refreshing": False,
        }
        _save_cache(value)
        return status()
    finally:
        _REFRESH_LOCK.release()


def run(interval_seconds: float, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            from .continuous_organization import ContinuousOrganizer
            ContinuousOrganizer(active_workspace()).reconcile()
            refresh_snapshot()
        except Exception:
            pass
        stop_event.wait(max(1.0, interval_seconds))


def status() -> dict:
    settings = load_settings()["governance"]
    cached = _read_cache()
    return {
        "extraction_sources": settings["extraction_sources"],
        "owner_trust_mode": settings["owner_trust_mode"],
        "archive_counts": cached.get("archive_counts", {"chatgpt": 0, "hermes": 0, "codex": 0, "other": 0}),
        "project_change_suggestions": cached.get("project_change_suggestions", []),
        "snapshot_updated_at": cached.get("snapshot_updated_at"),
        "refreshing": not bool(cached),
        "guarantees": {
            "raw_archives_preserved": True,
            "automatic_formal_write": False,
            "automatic_candidate_creation": False,
            "automatic_indexing": False,
        },
    }


def update(update_data: dict, owner_confirmed: bool = False) -> dict:
    if not owner_confirmed:
        raise PermissionError("治理设置变更需要Owner确认")
    allowed = {"extraction_sources", "owner_trust_mode"}
    if set(update_data) - allowed:
        raise ValueError("包含不允许的治理设置")
    save_settings({"governance": update_data})
    return status()
