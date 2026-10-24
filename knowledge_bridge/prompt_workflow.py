"""Truthful mother-document gate and workspace-local startup prompt storage."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import uuid

from .workspace import WorkspaceContext

FORMAT = "jiedu-startup-prompt-v1"
MAX_PROMPT = 12000
_SECRET = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\bsk-[A-Za-z0-9_-]{20,}\b", re.I)


def distillation_gate(local_sources: dict) -> dict:
    """Require explicit completed history for every collection participant."""
    sources = {row.get("id"): row for row in local_sources.get("sources", []) if isinstance(row, dict)}
    waiting: list[str] = []
    participants: list[str] = []
    for source_id, name in (("codex", "Codex"), ("hermes", "Hermes")):
        source = sources.get(source_id) or {}
        history = source.get("history") or {}
        rows = history.get("by_source") if source_id == "codex" else None
        progress = history.get("status") not in (None, "not_started", "unknown") or bool(history.get("checked") or history.get("archived"))
        if rows:
            progress = progress or any(row.get("status") not in (None, "not_started", "unknown") or row.get("checked") or row.get("archived") for row in rows)
        participating = bool(source.get("participating") or source.get("configured") or progress)
        if not participating:
            continue
        participants.append(name)
        if not source.get("detected") or not source.get("readable"):
            waiting.append(f"{name} 历史来源当前未检出或不可读")
        if source.get("paused") or (source.get("live") or {}).get("status") == "paused":
            waiting.append(f"{name} 采集已暂停")
        if source.get("last_error") or source.get("source_issues"):
            waiting.append(f"{name} 来源有错误或待核对项")
        if source_id == "codex":
            detected_kinds = {item.get("kind") for item in source.get("locations", []) if item.get("detected")}
            active_rows = [row for row in rows or [] if
                           (row.get("source") == "jsonl" and "jsonl" in detected_kinds) or
                           (row.get("source") != "jsonl" and "sqlite" in detected_kinds)]
            if not active_rows:
                waiting.append("Codex 历史尚无可核验的采集来源")
            for row in active_rows:
                label = "会话日志" if row.get("source") == "jsonl" else f"SQLite {row.get('source')}"
                if row.get("status") not in ("completed", "complete"):
                    waiting.append(f"Codex {label}历史尚未完成（{row.get('status') or '未知'}）")
                if row.get("failed_missing") is None or row.get("failed_missing") or row.get("source_error_count") or row.get("last_error") or row.get("needs_review"):
                    waiting.append(f"Codex {label}有失败、缺失或待核对项")
            if history.get("status") not in ("completed", "complete") and "jsonl" in detected_kinds:
                waiting.append(f"Codex 历史总状态未完成（{history.get('status') or '未知'}）")
        else:
            if history.get("status") != "complete":
                waiting.append(f"Hermes 历史尚未完成（{history.get('status') or '未知'}）")
            if history.get("failed_missing") is None or history.get("failed_missing") or history.get("needs_review"):
                waiting.append("Hermes 历史有缺失或待核对项")
    for source_id, name in (("dsh", "DSH"), ("claude", "Claude")):
        source = sources.get(source_id) or {}
        # Merely detecting a local application is not an Owner opt-in.
        if not source.get("participating"):
            continue
        participants.append(name)
        history = source.get("history") or {}
        if not source.get("detected") or not source.get("readable"):
            waiting.append(f"{name} 本机历史来源当前不可读")
        if source.get("paused") or (source.get("live") or {}).get("status") == "paused":
            waiting.append(f"{name} 采集已暂停")
        if source.get("last_error") or history.get("failed_missing"):
            waiting.append(f"{name} 当前可读历史有错误或缺失")
        if history.get("status") != "complete":
            waiting.append(f"{name} 本机历史检查尚未完成（{history.get('status') or '未知'}）")
    if not participants:
        waiting.append("尚未接入可核验的聊天历史采集，请先归档")
    return {"ready": not waiting, "sources": participants, "reasons": list(dict.fromkeys(waiting))}


def _path(context: WorkspaceContext) -> Path:
    if not context.initialized:
        raise ValueError("请先加载界渡工作区")
    return context.state / "startup-prompt.json"


def _read(context: WorkspaceContext) -> dict:
    try:
        data = json.loads(_path(context).read_text("utf-8"))
        return data if data.get("format") == FORMAT and data.get("workspace_id") == context.workspace_id else {}
    except (OSError, ValueError, AttributeError):
        return {}


def startup_status(context: WorkspaceContext, default_instruction: str) -> dict:
    state = _read(context)
    mode = state.get("mode") if state.get("mode") in {"generated", "custom"} else "default"
    return {"workspace_id": context.workspace_id, "mode": mode,
            "prompt": state.get("prompt") if mode != "default" else default_instruction,
            "source_files": state.get("source_files", []) if mode != "default" else [],
            "skipped_files": state.get("skipped_files", []) if mode != "default" else [],
            "saved_at": state.get("saved_at"), "default_prompt": default_instruction,
            "locally_saved": mode != "default"}


def save_startup(context: WorkspaceContext, prompt: str, *, mode: str,
                 source_files: list[str] | None = None,
                 skipped_files: list[dict] | None = None) -> dict:
    if mode not in {"generated", "custom"}:
        raise ValueError("提示词模式无效")
    value = prompt.strip() if isinstance(prompt, str) else ""
    if not value or len(value) > MAX_PROMPT or _SECRET.search(value):
        raise ValueError("提示词为空、过长或包含疑似密钥；请删去敏感内容后再保存")
    path = _path(context)
    path.parent.mkdir(parents=True, exist_ok=True)
    state = {"format": FORMAT, "workspace_id": context.workspace_id, "mode": mode,
             "prompt": value, "source_files": list(source_files or []) if mode == "generated" else [],
             "skipped_files": list(skipped_files or []) if mode == "generated" else [],
             "saved_at": datetime.now(timezone.utc).isoformat()}
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", "utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return state


def restore_default(context: WorkspaceContext) -> None:
    _path(context).unlink(missing_ok=True)
