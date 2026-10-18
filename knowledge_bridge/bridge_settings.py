from __future__ import annotations

import json
import os
import socket
from pathlib import Path

from .core import ROOT
from .workspace import local_path


SETTINGS_PATH = local_path("bridge-settings.json")
DEFAULTS = {
    "console": {"host": "127.0.0.1", "port": 8765},
    "mcp": {"mode": "stdio", "host": "127.0.0.1", "port": 8766},
    "weight_boost": {"mode": "manual", "automatic": False, "formal_core_enabled": False, "reminders": True, "turn_threshold": 30,
                     "project_switch_reminder": True, "memory_update_reminder": True},
    "clients": {"codex": {"memory_mode": "readonly","knowledge_access_mode":"normal"}, "hermes": {"memory_mode": "readonly","knowledge_access_mode":"normal"}, "other": {"memory_mode": "readonly","knowledge_access_mode":"normal"}},
    "security": {"knowledge_local_only": True, "public_access": False, "remote_mcp": False,
                 "cloud_sync": False, "public_bind_allowed": False},
    "governance": {
        "extraction_sources": {"chatgpt": True, "hermes": False, "codex": False},
        "owner_trust_mode": False,
    },
}


def _merge(base: dict, update: dict) -> dict:
    result = {key: (value.copy() if isinstance(value, dict) else value) for key, value in base.items()}
    for key, value in update.items():
        result[key] = _merge(result.get(key, {}), value) if isinstance(value, dict) and isinstance(result.get(key), dict) else value
    return result


def load_settings(path: Path = SETTINGS_PATH) -> dict:
    try: return _merge(DEFAULTS, json.loads(path.read_text("utf-8")))
    except (OSError, ValueError): return _merge(DEFAULTS, {})


def validate_settings(data: dict, check_port: bool = False) -> None:
    if data["console"]["host"] != "127.0.0.1" or data["mcp"]["host"] != "127.0.0.1":
        raise ValueError("安全策略拒绝非localhost监听")
    if data["mcp"]["mode"] not in {"stdio", "localhost_http"}: raise ValueError("MCP连接方式无效")
    if not isinstance(data["mcp"]["port"], int) or not 1024 <= data["mcp"]["port"] <= 65535: raise ValueError("MCP端口必须在1024到65535之间")
    port = data["console"]["port"]
    if not isinstance(port, int) or not 1024 <= port <= 65535: raise ValueError("控制台端口必须在1024到65535之间")
    threshold = data["weight_boost"]["turn_threshold"]
    if not isinstance(threshold, int) or not 5 <= threshold <= 999: raise ValueError("提醒轮数必须在5到999之间")
    if not isinstance(data["weight_boost"]["automatic"], bool): raise ValueError("自动权重提升配置无效")
    governance = data.get("governance", {})
    if not isinstance(governance.get("owner_trust_mode"), bool): raise ValueError("Owner信任模式配置无效")
    sources = governance.get("extraction_sources", {})
    if any(not isinstance(sources.get(key), bool) for key in ("chatgpt", "hermes", "codex")):
        raise ValueError("提炼来源配置无效")
    for client in ("codex","hermes","other"):
        if data["clients"][client].get("knowledge_access_mode","normal") not in {"normal","bridge_only"}: raise ValueError("知识访问模式无效")
    if any((not data["security"][key]) for key in ("knowledge_local_only",)) or any(data["security"][key] for key in ("public_access", "remote_mcp", "cloud_sync", "public_bind_allowed")):
        raise ValueError("安全锁定项不可关闭")
    if check_port:
        with socket.socket() as sock:
            try: sock.bind(("127.0.0.1", port))
            except OSError as exc: raise ValueError(f"端口 {port} 已被占用") from exc


def save_settings(update: dict, path: Path = SETTINGS_PATH, check_port: bool = False, owner_confirmed: bool = False) -> dict:
    old = load_settings(path); candidate = _merge(old, update); validate_settings(candidate, check_port)
    path.parent.mkdir(parents=True, exist_ok=True); tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(candidate, ensure_ascii=False, indent=2) + "\n", "utf-8"); os.replace(tmp, path)
    except Exception:
        if tmp.exists(): tmp.unlink()
        raise
    return candidate


def reminder_state(session: dict, settings: dict | None = None) -> dict:
    cfg = (settings or load_settings())["weight_boost"]
    if session.get("memory_mode") != "full": return {"status": "unavailable", "due": False, "turns_since": None}
    last = session.get("last_weight_boost_turn") or 0; turns_since = max(0, session.get("turn_count", 0) - last)
    if session.get("refresh_status") == "refresh_required": status = "waiting"
    elif session.get("refresh_status") == "boosted": status = "boosted"
    elif cfg["reminders"] and turns_since >= cfg["turn_threshold"]: status = "suggested"
    else: status = "normal"
    return {"status": status, "due": status == "suggested", "turns_since": turns_since, "threshold": cfg["turn_threshold"]}
