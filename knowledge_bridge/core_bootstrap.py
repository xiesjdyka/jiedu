"""Compile versioned Core Bootstrap exclusively from formal confirmed sources."""
from __future__ import annotations
import hashlib,json,os
from datetime import datetime,timezone
from pathlib import Path
from filelock import FileLock
from .core import VAULT
from .workspace import active_workspace
from .knowledge_base import load as load_knowledge_base
STORE=active_workspace().cache/"core-bootstrap"; CURRENT=STORE/"current.json"
SOURCES=None


class CoreUnavailable(ValueError):
 """An expected condition in the configured source files, not a compiler failure."""
 def __init__(self, reason, message):
  super().__init__(message)
  self.reason=reason


def sources():
 if SOURCES is not None:return list(SOURCES)
 config=load_knowledge_base();base=Path(config["vault_path"])
 if base.resolve()!=VAULT.resolve():raise CoreUnavailable("workspace_changed","知识库位置已更新，请重启界渡后再读取核心背景")
 if any(not isinstance(item,str) or not item.strip() for item in config["core_files"]):
  raise CoreUnavailable("source_invalid","核心背景文件配置无效，请在工作区重新选择")
 return [base/item for item in config["core_files"]]
def _now():return datetime.now(timezone.utc).isoformat()
def _sha(raw):return hashlib.sha256(raw).hexdigest()
def _compile():
 parts=[];source_rows=[];documents={}
 source_paths=sources()
 if not source_paths:raise CoreUnavailable("not_configured","尚未配置核心背景文件")
 base=VAULT.resolve()
 for path in source_paths:
  try:
   relative=path.relative_to(VAULT).as_posix()
   resolved=path.resolve()
   resolved.relative_to(base)
  except (ValueError,RuntimeError) as exc:
   raise CoreUnavailable("source_invalid","核心背景文件必须位于当前知识库内") from exc
  except OSError as exc:
   raise CoreUnavailable("source_unreadable",f"无法访问核心背景文件：{path.name}") from exc
  if path.suffix.lower()!=".md":
   raise CoreUnavailable("source_invalid",f"核心背景来源不是 Markdown：{path.name}")
  try:
   with resolved.open("rb") as source:
    raw=source.read()
    mtime=os.fstat(source.fileno()).st_mtime
  except (FileNotFoundError,NotADirectoryError) as exc:
   raise CoreUnavailable("source_missing",f"核心背景文件已找不到：{path.name}") from exc
  except OSError as exc:
   raise CoreUnavailable("source_unreadable",f"无法读取核心背景文件：{path.name}") from exc
  try:
   text=raw.decode("utf-8-sig")
  except UnicodeDecodeError as exc:
   raise CoreUnavailable("source_encoding",f"核心背景文件不是 UTF-8：{path.name}") from exc
  if not text.strip():raise CoreUnavailable("source_empty",f"核心背景文件为空：{path.name}")
  documents[relative]=text
  source_rows.append({"path":relative,"sha256":_sha(raw),"modified_at":datetime.fromtimestamp(mtime,timezone.utc).isoformat()})
  parts.append(f"## {path.stem}\n\n{text}")
 content="# Core Bootstrap\n\n> 由用户配置的核心背景文件机械编译；不包含项目状态、L0、TEST、草稿或自动推断。\n\n"+"\n\n".join(parts)+"\n"
 return _save_compilation(content,source_rows),documents


def _save_compilation(content,source_rows):
 content_hash=_sha(content.encode("utf-8"))
 STORE.mkdir(parents=True,exist_ok=True)
 # The UI and MCP can compile at the same time. Serialize cache updates so
 # one caller cannot replace the temporary file while the other is writing.
 with FileLock(str(CURRENT.with_suffix(".lock")),timeout=10):
  try:previous=json.loads(CURRENT.read_text("utf-8"))
  except (FileNotFoundError,ValueError):previous=None
  if isinstance(previous,dict) and previous.get("content_hash")==content_hash and previous.get("source_files")==source_rows:
   return previous
  version=f"core-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}"
  item={"version":version,"compiled_at":_now(),"source_files":source_rows,
        "source_hash":_sha(json.dumps(source_rows,sort_keys=True).encode()),"content_hash":content_hash,
        "characters":len(content),"estimated_tokens":round(len(content)*.85),
        "supersedes":previous.get("version") if isinstance(previous,dict) else None,
        "status":"正式Core已就绪","gate":_gate(len(source_rows)),"content":content}
  (STORE/f"{version}.json").write_text(json.dumps(item,ensure_ascii=False,indent=2)+"\n","utf-8")
  _set_current(item)
  return item


def _set_current(item):
 tmp=CURRENT.with_suffix(".tmp")
 tmp.write_text(json.dumps(item,ensure_ascii=False,indent=2)+"\n","utf-8")
 os.replace(tmp,CURRENT)


def _gate(count):
 return {"formal_sources_only":True,"source_count":count,"project_state":False,"l0":False,
         "test":False,"draft":False,"candidate":False,"unconfirmed_inference":False}


def compile_core():return _compile()[0]
def current():return compile_core()


def read_status():
 """Return a stable UI payload and the exact source text used for that result."""
 try:
  core,documents=_compile()
 except CoreUnavailable as exc:
  return ({
   "ready":False,"reason":exc.reason,"message":str(exc),
   "status":"尚未配置核心背景" if exc.reason=="not_configured" else "核心背景暂不可用",
   "version":None,"compiled_at":None,"source_files":[],"source_hash":None,
   "content_hash":None,"characters":0,"estimated_tokens":0,"supersedes":None,
   "gate":_gate(0),
   "content":"",
  },{})
 return core|{"ready":True,"reason":None,"message":None},documents


def versions():return sorted([json.loads(p.read_text("utf-8")) for p in STORE.glob("core-*.json")],key=lambda x:x["compiled_at"],reverse=True)
def rollback(version):
 path=STORE/f"{version}.json"
 if not path.exists():raise ValueError("Core Bootstrap版本不存在")
 with FileLock(str(CURRENT.with_suffix(".lock")),timeout=10):
  item=json.loads(path.read_text("utf-8"))
  _set_current(item)
  return item
