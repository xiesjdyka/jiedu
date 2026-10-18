"""Compile versioned Core Bootstrap exclusively from formal confirmed sources."""
from __future__ import annotations
import hashlib,json,os
from datetime import datetime,timezone
from pathlib import Path
from .core import ROOT,VAULT
from .workspace import active_workspace
from .knowledge_base import load as load_knowledge_base
STORE=active_workspace().cache/"core-bootstrap"; CURRENT=STORE/"current.json"
SOURCES=None
def sources():
 if SOURCES is not None:return list(SOURCES)
 config=load_knowledge_base();base=Path(config["vault_path"])
 if base.resolve()!=VAULT.resolve():raise ValueError("知识库位置已更新，请重启界渡后再读取核心背景")
 return [base/item for item in config["core_files"]]
def _now():return datetime.now(timezone.utc).isoformat()
def _sha(raw):return hashlib.sha256(raw).hexdigest()
def compile_core():
 parts=[];source_rows=[]
 source_paths=sources()
 if not source_paths:raise ValueError("尚未配置核心背景文件")
 for path in source_paths:
  if not path.exists():raise ValueError(f"正式Core来源不存在：{path.name}")
  raw=path.read_bytes();text=raw.decode("utf-8-sig");source_rows.append({"path":path.relative_to(VAULT).as_posix(),"sha256":_sha(raw),"modified_at":datetime.fromtimestamp(path.stat().st_mtime,timezone.utc).isoformat()});parts.append(f"## {path.stem}\n\n{text}")
 content="# Core Bootstrap\n\n> 由用户配置的核心背景文件机械编译；不包含项目状态、L0、TEST、草稿或自动推断。\n\n"+"\n\n".join(parts)+"\n"
 if any(not part.split("\n",2)[-1].strip() for part in parts):raise ValueError("正式Core来源不能为空")
 content_hash=_sha(content.encode("utf-8"));STORE.mkdir(parents=True,exist_ok=True)
 try:previous=json.loads(CURRENT.read_text("utf-8"))
 except (OSError,ValueError):previous=None
 if previous and previous.get("content_hash")==content_hash:return previous
 version=f"core-{datetime.now().strftime('%Y%m%d-%H%M%S')}";item={"version":version,"compiled_at":_now(),"source_files":source_rows,"source_hash":_sha(json.dumps(source_rows,sort_keys=True).encode()),"content_hash":content_hash,"characters":len(content),"estimated_tokens":round(len(content)*.85),"supersedes":previous.get("version") if previous else None,"status":"正式Core已就绪","gate":{"formal_sources_only":True,"source_count":len(source_rows),"project_state":False,"l0":False,"test":False,"draft":False,"candidate":False,"unconfirmed_inference":False},"content":content}
 (STORE/f"{version}.json").write_text(json.dumps(item,ensure_ascii=False,indent=2)+"\n","utf-8");tmp=CURRENT.with_suffix(".tmp");tmp.write_text(json.dumps(item,ensure_ascii=False,indent=2)+"\n","utf-8");os.replace(tmp,CURRENT);return item
def current():return compile_core()
def versions():return sorted([json.loads(p.read_text("utf-8")) for p in STORE.glob("core-*.json")],key=lambda x:x["compiled_at"],reverse=True)
def rollback(version):
 path=STORE/f"{version}.json"
 if not path.exists():raise ValueError("Core Bootstrap版本不存在")
 item=json.loads(path.read_text("utf-8"));tmp=CURRENT.with_suffix(".tmp");tmp.write_text(json.dumps(item,ensure_ascii=False,indent=2)+"\n","utf-8");os.replace(tmp,CURRENT);return item
