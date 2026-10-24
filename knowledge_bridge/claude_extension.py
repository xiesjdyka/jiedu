"""Prepare a private MCPB for Claude Desktop after explicit Owner action.

The bundle is only a launch shim for the installed Jiedu MCP stdio server.
Creating it does not install it into Claude or establish a connection.
"""
from __future__ import annotations

import json
import os
import uuid
import zipfile
from pathlib import Path

from .client_config import _command
from .workspace import local_path


BUNDLE = local_path("client-config") / "jiedu-claude.mcpb"
_SHIM = r'''"use strict";
const { spawn } = require("node:child_process");
let args, extra;
try {
  args = JSON.parse(process.env.JIEDU_ARGS_JSON || "[]");
  extra = JSON.parse(process.env.JIEDU_ENV_JSON || "{}");
  if (!Array.isArray(args) || !extra || typeof extra !== "object") throw Error("invalid launch data");
} catch (_) {
  process.stderr.write("Jiedu MCP launch data is invalid.\n");
  process.exit(1);
}
const child = spawn(process.env.JIEDU_COMMAND, args, {
  cwd: process.env.JIEDU_CWD || undefined,
  env: { ...process.env, ...extra },
  stdio: "inherit",
  windowsHide: true,
});
child.on("error", () => { process.stderr.write("Jiedu MCP could not start.\n"); process.exit(1); });
child.on("exit", (code) => process.exit(code === null ? 1 : code));
'''


def _manifest(client_id: str, secret: str) -> dict:
    launch = _command(client_id, secret)
    env = {"JIEDU_COMMAND": launch["command"],
           "JIEDU_ARGS_JSON": json.dumps(launch["args"]),
           "JIEDU_ENV_JSON": json.dumps(launch["env"]),
           "JIEDU_CWD": launch.get("cwd") or ""}
    return {"manifest_version": "0.3", "name": "jiedu-knowledge-bridge",
            "display_name": "界渡知识库", "version": "1.1.8",
            "description": "在本机通过 MCP 读取已获准的界渡知识。",
            "author": {"name": "界渡"},
            "server": {"type": "node", "entry_point": "server/index.js",
                       "mcp_config": {"command": "node",
                                      "args": ["${__dirname}/server/index.js"],
                                      "env": env}},
            "compatibility": {"platforms": ["win32"]}}


def inspect_bundle(path: Path | None = None) -> dict | None:
    path=path or BUNDLE
    if not path.is_file():
        return None
    try:
        with zipfile.ZipFile(path) as archive:
            if set(archive.namelist()) != {"manifest.json", "server/index.js"}:
                return None
            manifest = json.loads(archive.read("manifest.json"))
            if manifest.get("name") != "jiedu-knowledge-bridge":
                return None
            env = manifest["server"]["mcp_config"]["env"]
            launch_env = json.loads(env["JIEDU_ENV_JSON"])
            return {"client_id": launch_env.get("KNOWLEDGE_BRIDGE_CLIENT_ID"),
                    "secret": launch_env.get("KNOWLEDGE_BRIDGE_CLIENT_SECRET"),
                    "path": str(path)}
    except (OSError, ValueError, KeyError, zipfile.BadZipFile):
        return None


def prepare_bundle(client_id: str, secret: str, path: Path | None = None) -> Path:
    path=path or BUNDLE
    if not client_id or not secret:
        raise ValueError("Claude 扩展缺少配对身份")
    if path.exists():
        raise ValueError("界渡 Claude 扩展包已存在；保留原包等待核对")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(_manifest(client_id, secret), ensure_ascii=False, indent=2))
            archive.writestr("server/index.js", _SHIM)
        if not inspect_bundle(temporary):
            raise ValueError("Claude 扩展包校验失败")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path
