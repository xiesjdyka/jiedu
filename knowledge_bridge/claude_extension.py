"""Prepare a private MCPB for Claude Desktop after explicit Owner action.

The bundle is only a launch shim for the installed Jiedu MCP stdio server.
Creating it does not install it into Claude or establish a connection.
"""
from __future__ import annotations

import json
from io import BytesIO
import os
import stat
import uuid
import zipfile
import zlib
from pathlib import Path

from .client_config import _command
from .workspace import local_path


BUNDLE = local_path("client-config") / "jiedu-claude.mcpb"
VERSION = "1.1.9"
ENTRY_POINT = "server/index.js"
MAX_BUNDLE_BYTES = 512 * 1024
_SHIM = r'''"use strict";
const { spawn } = require("node:child_process");
function fail(message) {
  process.stderr.write("Jiedu MCP: " + message + "\n");
  process.exitCode = 1;
}
function validString(value) {
  return typeof value === "string" && !value.includes("\0");
}
function readJson(value, name) {
  try { return JSON.parse(value); }
  catch { throw new Error(name + " must contain valid JSON."); }
}
function launch() {
  const { JIEDU_COMMAND: command, JIEDU_ARGS_JSON: argsJson,
    JIEDU_ENV_JSON: envJson, JIEDU_CWD: cwd, ...parentEnv } = process.env;
  let args, extra;
  try {
    if (!validString(command) || !command.trim()) throw new Error("JIEDU_COMMAND is missing or invalid.");
    if (cwd !== undefined && !validString(cwd)) throw new Error("JIEDU_CWD is invalid.");
    args = readJson(argsJson, "JIEDU_ARGS_JSON");
    if (!Array.isArray(args) || !args.every(validString)) throw new Error("JIEDU_ARGS_JSON must be an array of strings.");
    extra = readJson(envJson, "JIEDU_ENV_JSON");
    if (!extra || typeof extra !== "object" || Array.isArray(extra) ||
        !Object.entries(extra).every(([key, value]) => key && !key.includes("=") && validString(key) && validString(value))) {
      throw new Error("JIEDU_ENV_JSON must map environment names to strings.");
    }
  } catch (error) {
    fail(error.message);
    return;
  }
  function launchError(error) {
    const code = typeof error.code === "string" && /^[A-Z0-9_]+$/.test(error.code) ? error.code : "UNKNOWN";
    const reason = code === "ENOENT" ? "the executable or working directory is missing" :
      ["EACCES", "EPERM"].includes(code) ? "Windows denied process creation" : "process creation failed";
    fail("could not start because " + reason + " (" + code + ").");
  }
  let child;
  try {
    child = spawn(command, args, {
      cwd: cwd || undefined,
      env: { ...parentEnv, ...extra },
      stdio: "inherit",
      shell: false,
      windowsHide: true,
    });
  } catch (error) {
    launchError(error);
    return;
  }
  child.once("error", launchError);
  child.once("exit", (code, signal) => {
    if (signal) fail("the child was terminated by " + signal + ".");
    else if (code !== 0) {
      fail("the child exited with code " + code + ".");
      if (Number.isInteger(code) && code > 0) process.exitCode = code;
    }
  });
}
launch();
'''


def _manifest(client_id: str, secret: str) -> dict:
    launch = _command(client_id, secret)
    env = {"JIEDU_COMMAND": launch["command"],
           "JIEDU_ARGS_JSON": json.dumps(launch["args"]),
           "JIEDU_ENV_JSON": json.dumps(launch["env"]),
           "JIEDU_CWD": launch.get("cwd") or ""}
    return {"manifest_version": "0.3", "name": "jiedu-knowledge-bridge",
            "display_name": "界渡知识库", "version": VERSION,
            "description": "在本机通过 MCP 读取已获准的界渡知识。",
            "author": {"name": "界渡"},
            "server": {"type": "node", "entry_point": ENTRY_POINT,
                       "mcp_config": {"command": "node",
                                      "args": ["${__dirname}/" + ENTRY_POINT],
                                      "env": env}},
            "compatibility": {"platforms": ["win32"]}}


def read_bundle(path: Path | None = None) -> bytes | None:
    try:
        with (path or BUNDLE).open("rb") as handle:
            data = handle.read(MAX_BUNDLE_BYTES + 1)
    except FileNotFoundError:
        return None
    if len(data) > MAX_BUNDLE_BYTES:
        raise ValueError("Claude 扩展包超出大小限制")
    return data


def inspect_bundle(path: Path | None = None, *, data: bytes | None = None,
                   identity_only: bool = False) -> dict | None:
    """Verify the launchable package, or read only its old identity for repair."""
    path = path or BUNDLE
    try:
        if data is None:
            data = read_bundle(path)
        if data is None or len(data) > MAX_BUNDLE_BYTES:
            return None
        with zipfile.ZipFile(BytesIO(data)) as archive:
            names = archive.namelist()
            if names.count("manifest.json") != 1 or archive.getinfo("manifest.json").file_size > MAX_BUNDLE_BYTES:
                return None
            manifest = json.loads(archive.read("manifest.json"))
            if manifest.get("name") != "jiedu-knowledge-bridge" or manifest.get("version") not in {"1.1.8", VERSION}:
                return None
            env = manifest["server"]["mcp_config"]["env"]
            launch_env = json.loads(env["JIEDU_ENV_JSON"])
            client_id = launch_env.get("KNOWLEDGE_BRIDGE_CLIENT_ID")
            secret = launch_env.get("KNOWLEDGE_BRIDGE_CLIENT_SECRET")
            if not all(isinstance(value, str) and value for value in (client_id, secret)):
                return None
            if not identity_only:
                if sorted(names) != sorted(("manifest.json", ENTRY_POINT)) or manifest != _manifest(client_id, secret):
                    return None
                script = _SHIM.encode("utf-8")
                entry = archive.getinfo(manifest["server"]["entry_point"])
                mode = stat.S_IFMT(entry.external_attr >> 16)
                if mode not in (0, stat.S_IFREG) or entry.file_size != len(script) or archive.read(entry) != script:
                    return None
            return {"client_id": client_id, "secret": secret, "path": str(path)}
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError, zipfile.BadZipFile, zlib.error):
        return None


def build_bundle(client_id: str, secret: str) -> bytes:
    if not client_id or not secret:
        raise ValueError("Claude 扩展缺少配对身份")
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(_manifest(client_id, secret), ensure_ascii=False, indent=2))
        archive.writestr(ENTRY_POINT, _SHIM)
    data = stream.getvalue()
    if not inspect_bundle(data=data):
        raise ValueError("Claude 扩展包入口校验失败")
    return data


def replace_bundle(data: bytes | None, previous: bytes | None, path: Path | None = None) -> Path:
    """Publish verified bytes atomically. Previous bytes also permit rollback."""
    path = path or BUNDLE
    if read_bundle(path) != previous:
        raise ValueError("Claude 扩展包已变化，请重新预览或准备")
    if data is None:
        path.unlink(missing_ok=True)
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def prepare_bundle(client_id: str, secret: str, path: Path | None = None) -> Path:
    return replace_bundle(build_bundle(client_id, secret), None, path)
