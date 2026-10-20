from __future__ import annotations

import hashlib
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .core import ROOT, VAULT
from . import workspace as workspace_module
from .package_layout import component, connected_workspace, package_root
from .workspace import active_workspace, configured_workspace_root, initialize_workspace, is_workspace, load_workspace, local_path, resolve_workspace_root

CONFIG_PATH = local_path("knowledge-base.json")
AUDIT_PATH = active_workspace().audit / "knowledge-base.json"
_DEFAULT_AUDIT_PATH = AUDIT_PATH
DEFAULT_FOLDERS = ["00_系统", "01_当前状态", "02_项目", "03_知识", "90_原始档案", "99_收件箱"]
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_base(path: str | Path) -> Path:
    target = Path(path).expanduser().resolve()
    if not target.exists() or not target.is_dir():
        raise ValueError("请选择真实存在的本地文件夹")
    if target == Path(target.anchor):
        raise ValueError("不能把磁盘根目录直接作为知识库")
    return target


def load() -> dict:
    try:
        saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    # The runtime override identifies the workspace currently loaded by this
    # process. A newly saved selection must be read without that override so
    # the API can report its identity and controlled-restart destination.
    vault = (resolve_workspace_root(workspace_module.PROGRAM_ROOT, CONFIG_PATH.parent)
             if isinstance(saved.get("vault_path"), str) and saved["vault_path"].strip()
             else configured_workspace_root())
    core_files = saved.get("core_files") if isinstance(saved.get("core_files"), list) else []
    return {
        "vault_path": str(vault),
        "workspace_id": saved.get("workspace_id"),
        "core_files": core_files,
        "initialized": bool(saved.get("initialized", False)),
        "updated_at": saved.get("updated_at"),
        "scan": saved.get("scan") if isinstance(saved.get("scan"), dict) else None,
    }


def _write(data: dict) -> dict:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    stored = dict(data)
    package = package_root(ROOT)
    connected = connected_workspace(package) if package is not None else None
    if component("workspace", ROOT) is not None or (connected is not None and
            Path(stored.get("vault_path") or "").resolve() == connected):
        stored["vault_path"] = "@package-workspace"
    tmp.write_text(json.dumps(stored, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, CONFIG_PATH)
    return data


def markdown_files(path: str | Path | None = None) -> list[str]:
    base = _safe_base(path or load()["vault_path"])
    ignored = {".knowledge-bridge", ".jiedu", ".obsidian", ".git", "node_modules", ".venv", ".venv312"}
    rows: list[str] = []
    for folder, dirs, files in os.walk(base, topdown=True, followlinks=False):
        dirs[:] = [name for name in dirs if name not in ignored and
                   not name.startswith("__pycache__") and not (Path(folder) / name).is_symlink()]
        for name in files:
            if name.lower().endswith(".md"):
                item = Path(folder) / name
                if not item.is_symlink():
                    rows.append(item.relative_to(base).as_posix())
    return sorted(rows)


def _scan_snapshot(base: Path) -> dict:
    files = markdown_files(base)
    structure = []
    for item in sorted(base.iterdir(), key=lambda value: value.name.casefold()):
        if item.name.startswith(".") or item.name in {"node_modules", ".venv", ".venv312"}:
            continue
        if item.is_dir():
            prefix = item.name + "/"
            structure.append({"name": item.name, "kind": "folder", "markdown_count": sum(1 for relative in files if relative.startswith(prefix))})
        elif item.suffix.lower() == ".md":
            structure.append({"name": item.name, "kind": "file", "markdown_count": 1})
    return {"markdown_files": files, "markdown_count": len(files), "structure": structure, "scanned_at": _now()}


def diagnose_import(path: str) -> dict:
    """Read-only inventory for a user-selected import source."""
    base = _safe_base(path)
    files = [item for item in base.rglob("*") if item.is_file() and not any(part.startswith(".") for part in item.relative_to(base).parts)]
    types = Counter((item.suffix.lower() or "无扩展名") for item in files)
    hashes: dict[str, list[str]] = defaultdict(list)
    unknown = []
    supported = {".md", ".txt", ".json", ".csv", ".pdf", ".docx", ".html", ".htm"}
    for item in files:
        digest = hashlib.sha256(item.read_bytes()).hexdigest()
        hashes[digest].append(item.relative_to(base).as_posix())
        if item.suffix.lower() not in supported:
            unknown.append(item.relative_to(base).as_posix())
    duplicates = [{"sha256": digest, "files": names} for digest, names in hashes.items() if len(names) > 1]
    return {
        "path": str(base), "file_count": len(files), "types": dict(sorted(types.items())),
        "duplicates": duplicates, "duplicate_group_count": len(duplicates), "unknown_files": unknown,
        "classification": {"available": False, "status": "尚未配置AI分类服务", "suggestions": [], "requires_owner_confirmation": True},
        "read_only": True, "scanned_at": _now(),
    }


def refresh_scan() -> dict:
    data = load()
    base = _safe_base(data["vault_path"])
    data["scan"] = _scan_snapshot(base)
    data["updated_at"] = _now()
    _write(data)
    audit(base)
    return status()


def audit(path: str | Path | None = None) -> dict:
    base = _safe_base(path or load()["vault_path"])
    audit_path = AUDIT_PATH if AUDIT_PATH != _DEFAULT_AUDIT_PATH else load_workspace(base).audit / "knowledge-base.json"
    files = []
    for relative in markdown_files(base):
        item = base / relative
        raw = item.read_bytes()
        files.append({
            "path": relative,
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "modified_at": datetime.fromtimestamp(item.stat().st_mtime, timezone.utc).isoformat(),
        })
    result = {
        "vault_path": str(base),
        "created_at": _now(),
        "algorithm": "SHA-256",
        "purpose": "完整性与变化检测，不提供进程级访问隔离",
        "files": files,
    }
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = audit_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, audit_path)
    return {"file_count": len(files), "audit_path": str(audit_path), "created_at": result["created_at"]}


def _audit_summary() -> dict | None:
    try:
        audit_path = AUDIT_PATH if AUDIT_PATH != _DEFAULT_AUDIT_PATH else load_workspace(load()["vault_path"]).audit / "knowledge-base.json"
    except ValueError:
        return None
    if not audit_path.exists():
        return None
    try:
        value = json.loads(audit_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return {key: item for key, item in value.items() if key != "files"} | {"file_count": len(value.get("files", []))}


def status() -> dict:
    data = load()
    base = Path(data["vault_path"])
    selected = CONFIG_PATH.exists()
    configured = selected and base.exists() and is_workspace(base)
    context = load_workspace(base) if configured else None
    # Workspace inventory must reflect files changed outside the app too. This
    # is a read-only filename scan, not an index rebuild or a model operation.
    snapshot = _scan_snapshot(base) if configured else {"markdown_files": [], "markdown_count": 0, "structure": [], "scanned_at": None}
    files = snapshot.get("markdown_files", [])
    content_files = [
        item for item in files
        if not (Path(item).name.casefold() == "readme.md" and Path(item).parts and Path(item).parts[0] in DEFAULT_FOLDERS)
    ]
    if configured:
        try:
            from .core import overview
            index = overview()
        except Exception:
            index = {"file_count": 0, "chunk_count": 0, "vector_count": 0, "index_status": "未建立"}
    else:
        index = {"file_count": 0, "chunk_count": 0, "vector_count": 0, "index_status": "未建立"}
    runtime_match = base.resolve() == VAULT.resolve()
    indexed = runtime_match and int(index.get("vector_count") or 0) > 0
    if not configured:
        connection_state, state_label = "uninitialized", "尚未选择或初始化工作区"
    elif not runtime_match:
        connection_state, state_label = "restart_required", "已选择知识库 · 等待重启切换"
    elif not indexed:
        connection_state, state_label = "connected_unindexed", "知识库已连接，但索引未建立"
    else:
        connection_state, state_label = "ready", "知识库已就绪"
    return data | {
        "scan": snapshot,
        "workspace_state": "loaded" if configured and runtime_match else "uninitialized" if not configured else "selected",
        "workspace_ready": bool(configured and runtime_match),
        "workspace_id": context.workspace_id if context else None,
        "program_state": "running",
        "program_label": "界渡程序正常",
        "name": base.name,
        "exists": base.exists(),
        "active_runtime_path": str(VAULT.resolve()),
        "current_runtime_path": str(VAULT.resolve()),
        "selected_workspace_path": str(base.resolve()) if configured else None,
        "restart_target_path": str(base.resolve()) if configured and not runtime_match else None,
        "loaded_workspace_id": active_workspace().workspace_id if active_workspace().initialized else None,
        "configured": configured,
        "connected": configured and runtime_match,
        "restart_required": configured and not runtime_match,
        "connection_state": connection_state,
        "state_label": state_label,
        "indexed": indexed,
        "index_state": "ready" if indexed else "not_built",
        "index_label": "索引已就绪" if indexed else "索引未建立",
        "vector_state": "available" if int(index.get("vector_count") or 0) > 0 else "not_built",
        "vector_label": f"{int(index.get('vector_count') or 0)} 条向量" if int(index.get("vector_count") or 0) > 0 else "未建立语义向量",
        "index": index,
        "markdown_count": int(snapshot.get("markdown_count") or len(files)),
        "content_markdown_count": len(content_files),
        "structure": snapshot.get("structure", []),
        "scan_state": "completed" if configured and snapshot.get("scanned_at") else "not_started",
        "scan_label": f"当前 {int(snapshot.get('markdown_count') or len(files))} 个Markdown文件" if configured and snapshot.get("scanned_at") else "尚未扫描",
        "available_core_files": [item for item in files
                                 if not item.startswith(("90_原始档案/", "99_收件箱/"))] if configured else [],
        "core_ready": bool(data["core_files"]) and all((base / item).is_file() for item in data["core_files"]),
        "recommended_structure": DEFAULT_FOLDERS,
        "audit": _audit_summary(),
    }


def build_index(owner_confirmed: bool = False) -> dict:
    """Build the configured Vault index only after an explicit local Owner action."""
    if not owner_confirmed:
        raise PermissionError("建立知识索引需要用户确认")
    current = status()
    if not current["configured"]:
        raise ValueError("请先选择并连接本地知识库")
    if current["restart_required"]:
        raise ValueError("知识库路径已保存，请先重启界渡再建立索引")
    from .core import rebuild_index
    result = rebuild_index(operation="owner_api_rebuild")
    if result.get("status")=="index_busy":
        return {"status":"index_busy","sync":result,"knowledge_base":status()}
    return {"sync": result, "knowledge_base": status()}


def connect(path: str, core_files: list[str] | None = None) -> dict:
    base = _safe_base(path)
    if component("workspace", ROOT) is not None and base != component("workspace", ROOT):
        raise ValueError("独立目录模式请从重新定位入口连接完整实例；导入资料请使用知识页。")
    context = load_workspace(base) if is_workspace(base) else initialize_workspace(base)
    previous = load() if CONFIG_PATH.exists() else {}
    same_workspace = previous.get("workspace_id") == context.workspace_id
    chosen = core_files if core_files is not None else list(previous.get("core_files") or []) if same_workspace else []
    validate_core_files(base, chosen)
    _write({"vault_path": str(base), "workspace_id": context.workspace_id, "core_files": chosen, "initialized": True, "updated_at": _now(), "scan": _scan_snapshot(base)})
    audit(base)
    return status()


def initialize(path: str, core_files: list[str] | None = None, owner_confirmed: bool = False) -> dict:
    if not owner_confirmed:
        raise PermissionError("初始化目录结构需要用户确认")
    base = Path(path).expanduser().resolve()
    if base == Path(base.anchor):
        raise ValueError("不能把磁盘根目录直接作为知识库")
    if component("workspace", ROOT) is not None and base != component("workspace", ROOT):
        raise ValueError("请初始化包含当前 .jiedu 的外层目录；已有资料会保留。")
    context = initialize_workspace(base)
    for name in DEFAULT_FOLDERS:
        (base / name).mkdir(exist_ok=True)
    chosen = core_files or []
    validate_core_files(base, chosen)
    _write({"vault_path": str(base), "workspace_id": context.workspace_id, "core_files": chosen, "initialized": True, "updated_at": _now(), "scan": _scan_snapshot(base)})
    audit(base)
    return status()


def ensure_first_run() -> dict:
    """Report first-run state; initialization is always an explicit Owner action."""
    return status()


def validate_core_files(base: Path, items: list[str]) -> None:
    for relative in items:
        candidate = (base / relative).resolve()
        try:
            candidate.relative_to(base)
        except ValueError as exc:
            raise ValueError("核心背景文件必须位于知识库内") from exc
        if candidate.suffix.lower() != ".md" or not candidate.is_file():
            raise ValueError(f"核心背景文件不存在或不是Markdown：{relative}")


def save_core_files(items: list[str]) -> dict:
    data = load()
    base = _safe_base(data["vault_path"])
    validate_core_files(base, items)
    data["core_files"] = list(dict.fromkeys(items))
    data["updated_at"] = _now()
    _write(data)
    audit(base)
    return status()


def startup_instruction() -> dict:
    data = load()
    names = [Path(item).stem for item in data["core_files"]]
    configured = "、".join(names)
    text = ("当前没有设置优先读取资料。请按本次问题检索相关文件，并保留来源；不要假定已有用户画像或协作规则。"
            if not names else
        f"通过界渡读取我当前配置的全部核心背景（{configured}），确认我的长期信息、协作规则和重要原则。"
        "先只建立长期背景，不主动加载全部项目或历史；我提到具体项目后再读取项目状态，证据不足时再继续检索。"
        "任何核心背景读取失败必须明确说明，不得猜测补全。"
    )
    return {"instruction": text, "core_files": data["core_files"], "generated_at": _now()}


def choose_folder() -> dict:
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askdirectory(title="选择界渡本地知识库文件夹", mustexist=True)
        root.destroy()
    except Exception as exc:
        raise RuntimeError(f"无法打开本机文件夹选择器：{exc}") from exc
    return {"selected": selected or None}
