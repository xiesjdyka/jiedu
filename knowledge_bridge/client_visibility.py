"""Workspace-scoped owner preference for hiding a removed client entry."""
from __future__ import annotations

import json
from pathlib import Path

from .workspace import WorkspaceContext, _atomic_json


PREFERENCE_NAME = "client-visibility.json"
HIDEABLE_TYPES = frozenset({"kimi"})


def _path(context: WorkspaceContext) -> Path:
    return context.state / PREFERENCE_NAME


def hidden_client_types(context: WorkspaceContext) -> frozenset[str]:
    path = _path(context)
    if not path.exists():
        return frozenset()
    if path.is_symlink() or not path.is_file():
        raise ValueError("客户端隐藏设置无效")
    data = json.loads(path.read_text("utf-8-sig"))
    if (not isinstance(data, dict) or data.get("schema") != 1 or
            data.get("workspace_id") != context.workspace_id or
            not isinstance(data.get("hidden_client_types"), list) or
            any(kind not in HIDEABLE_TYPES for kind in data["hidden_client_types"])):
        raise ValueError("客户端隐藏设置与当前工作区不匹配")
    return frozenset(data["hidden_client_types"])


def set_client_hidden(context: WorkspaceContext, client_type: str, hidden: bool) -> dict:
    if client_type not in HIDEABLE_TYPES or type(hidden) is not bool:
        raise ValueError("只支持设置 Kimi 入口是否隐藏")
    current = set(hidden_client_types(context))
    if hidden:
        current.add(client_type)
    else:
        current.discard(client_type)
    result = {"schema": 1, "workspace_id": context.workspace_id,
              "hidden_client_types": sorted(current)}
    _atomic_json(_path(context), result)
    return result
