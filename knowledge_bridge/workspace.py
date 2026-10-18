"""Portable workspace identity and authoritative path contract for Jiedu."""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


from .package_layout import component

PROGRAM_ROOT = Path(__file__).resolve().parents[1]
LOCAL_DATA_ROOT = Path(os.environ.get("KNOWLEDGE_BRIDGE_LOCAL_DATA") or (component("control", PROGRAM_ROOT) or PROGRAM_ROOT / "data")).expanduser().resolve()
STANDARD_DIRS = ("00_系统", "01_当前状态", "02_项目", "03_知识", "90_原始档案", "99_收件箱")
FORMAL_LAYERS = STANDARD_DIRS[:4]
FORMAT_VERSION = 1
MANIFEST_NAME = "workspace.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


@dataclass(frozen=True)
class WorkspaceContext:
    """All portable paths derived from one root; the path is never its identity."""

    root: Path
    workspace_id: str | None
    format_version: int | None
    created_at: str | None

    @property
    def initialized(self) -> bool:
        return bool(self.workspace_id and self.format_version and self.created_at and self.manifest_path.is_file())

    @property
    def control(self) -> Path:
        return self.root / ".jiedu"

    @property
    def manifest_path(self) -> Path:
        return self.control / MANIFEST_NAME

    @property
    def system(self) -> Path:
        return self.root / "00_系统"

    @property
    def current_state(self) -> Path:
        return self.root / "01_当前状态"

    @property
    def projects(self) -> Path:
        return self.root / "02_项目"

    @property
    def knowledge(self) -> Path:
        return self.root / "03_知识"

    @property
    def formal_roots(self) -> tuple[Path, ...]:
        return (self.system, self.current_state, self.projects, self.knowledge)

    @property
    def raw_archive(self) -> Path:
        return self.root / "90_原始档案"

    @property
    def inbox(self) -> Path:
        return self.root / "99_收件箱"

    @property
    def state(self) -> Path:
        return self.control / "state"

    @property
    def sync(self) -> Path:
        return self.state / "sync"

    @property
    def indices(self) -> Path:
        return self.control / "index"

    def index_root(self, scope: str) -> Path:
        if scope not in {"formal", "evidence", "inbox"}:
            raise ValueError(f"未知检索范围：{scope}")
        return self.indices / scope

    @property
    def queues(self) -> Path:
        return self.control / "queue"

    @property
    def audit(self) -> Path:
        return self.control / "audit"

    @property
    def versions(self) -> Path:
        return self.control / "versions"

    @property
    def cache(self) -> Path:
        return self.control / "cache"

    @property
    def protected_root(self) -> Path:
        """Reserved future provider location; merely resolving it never creates it."""
        return self.control / "protected"

    @property
    def sessions_state(self) -> Path:
        return self.state / "sessions.json"

    @property
    def raw_message_store(self) -> Path:
        return self.state / "raw-messages"

    @property
    def archive_registry(self) -> Path:
        return self.state / "archive-sessions.json"

    def relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix()

    def portable_manifest(self) -> dict:
        if not self.initialized:
            raise ValueError("尚未选择或初始化界渡工作区")
        return {
            "workspace_id": self.workspace_id,
            "format_version": self.format_version,
            "created_at": self.created_at,
            "compatibility": {"minimum_reader_version": 1, "layout": "jiedu-workspace-v1"},
        }


def initialize_workspace(path: str | Path) -> WorkspaceContext:
    """Initialize without replacing any existing user content."""
    root = Path(path).expanduser().resolve()
    if root == Path(root.anchor):
        raise ValueError("不能把磁盘根目录初始化为界渡工作区")
    root.mkdir(parents=True, exist_ok=True)
    for name in STANDARD_DIRS:
        (root / name).mkdir(exist_ok=True)
    control = root / ".jiedu"
    for relative in (
        "state/sync", "state/raw-messages", "index/formal", "index/evidence", "index/inbox",
        "queue", "audit", "versions", "cache",
    ):
        (control / relative).mkdir(parents=True, exist_ok=True)
    manifest_path = control / MANIFEST_NAME
    if not manifest_path.exists():
        _atomic_json(manifest_path, {
            "workspace_id": str(uuid.uuid4()),
            "format_version": FORMAT_VERSION,
            "created_at": _now(),
            "compatibility": {"minimum_reader_version": 1, "layout": "jiedu-workspace-v1"},
        })
    return load_workspace(root)


def load_workspace(path: str | Path) -> WorkspaceContext:
    root = Path(path).expanduser().resolve()
    manifest_path = root / ".jiedu" / MANIFEST_NAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError("所选文件夹不是已初始化的界渡工作区") from exc
    except (OSError, ValueError) as exc:
        raise ValueError("工作区清单损坏或无法读取") from exc
    required = ("workspace_id", "format_version", "created_at")
    if any(not manifest.get(key) for key in required):
        raise ValueError("工作区清单缺少必要身份字段")
    version = int(manifest["format_version"])
    if version > FORMAT_VERSION:
        raise ValueError(f"工作区格式 {version} 高于当前支持版本 {FORMAT_VERSION}")
    return WorkspaceContext(root, str(manifest["workspace_id"]), version, str(manifest["created_at"]))


def is_workspace(path: str | Path) -> bool:
    try:
        load_workspace(path)
        return True
    except ValueError:
        return False


def local_path(*parts: str) -> Path:
    """Machine-local state only (secrets, executables, recent workspace pointer)."""
    return LOCAL_DATA_ROOT.joinpath(*parts)


def resolve_workspace_root(program_root: Path, local_data: Path, *, override: str | None = None) -> Path:
    """An application location never implicitly selects its parent as a vault."""
    program_root = Path(program_root).resolve()
    portable = component('workspace', program_root)
    if portable is not None and not override:
        return portable
    selected = override
    if not selected:
        try:
            selected = json.loads((Path(local_data) / 'knowledge-base.json').read_text('utf-8')).get('vault_path')
        except (OSError, ValueError, AttributeError):
            pass
    if not selected:
        try:
            selected = json.loads((program_root / 'config.json').read_text('utf-8')).get('vault_root')
        except (OSError, ValueError, AttributeError):
            pass
    if not isinstance(selected, str) or not selected.strip():
        return program_root / 'knowledge-space'
    candidate = Path(selected).expanduser()
    if not candidate.is_absolute() and '..' in candidate.parts:
        raise ValueError('请明确选择知识库目录，不能从程序的父目录推断知识库')
    candidate = (program_root / candidate).resolve()
    if candidate == Path(candidate.anchor):
        raise ValueError('不能把磁盘根目录作为知识库，请选择已有知识库文件夹')
    return candidate


def configured_workspace_root() -> Path:
    return resolve_workspace_root(PROGRAM_ROOT, LOCAL_DATA_ROOT,
                                  override=os.environ.get('KNOWLEDGE_BRIDGE_VAULT'))


def active_workspace(*, initialize: bool = False, required: bool = False) -> WorkspaceContext:
    """Return the two-phase runtime context without creating anything implicitly."""
    root = configured_workspace_root()
    if initialize:
        return initialize_workspace(root)
    try:
        return load_workspace(root)
    except ValueError:
        if required:
            raise
        return WorkspaceContext(root, None, None, None)


def workspace_runtime_status() -> dict:
    context = active_workspace()
    if context.initialized:
        return {
            "workspace_state": "loaded", "workspace_ready": True,
            "workspace_id": context.workspace_id, "workspace_format_version": context.format_version,
            "workspace_root": str(context.root), "message": "工作区已加载",
        }
    return {
        "workspace_state": "uninitialized", "workspace_ready": False,
        "workspace_id": None, "workspace_format_version": None,
        "workspace_root": None, "candidate_path": str(context.root),
        "message": "尚未选择或初始化工作区",
    }


def require_active_workspace() -> WorkspaceContext:
    context = active_workspace()
    if not context.initialized:
        raise ValueError("尚未选择或初始化工作区")
    return context
