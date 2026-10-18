from __future__ import annotations
import hashlib, json, math, os, re, threading, time, uuid
from datetime import datetime, timezone
from pathlib import Path
import lancedb
import numpy as np
import pyarrow as pa
from .workspace import FORMAL_LAYERS, PROGRAM_ROOT, active_workspace, configured_workspace_root
from .index_gate import IndexBusyError, IndexWriteGate

ROOT = PROGRAM_ROOT
CFG = json.loads((ROOT / ("config.json" if (ROOT / "config.json").exists() else "config.example.json")).read_text("utf-8"))
from .package_layout import component
if component('models', ROOT) is not None:
    CFG['model_cache'] = str(component('models', ROOT))
VAULT = configured_workspace_root()
_WORKSPACE = active_workspace()
DB_PATH = _WORKSPACE.index_root("formal") / "lancedb"
STATE_PATH = _WORKSPACE.index_root("formal") / "state.json"
ACTIVE_INDEX_PATH = _WORKSPACE.index_root("formal") / "active.json"
GENERATION_ROOT = _WORKSPACE.index_root("formal") / "generations"
MAP_PATH = _WORKSPACE.cache / "vector_map.json"
EXCLUDED = {".jiedu", ".obsidian", ".knowledge-bridge", ".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build"}
DEDICATED_MEMORY_SOURCES={"00_系统/我的长期画像.md","00_系统/AI协作规则.md","00_系统/AI错误与教训.md"}
SECRET_PATTERNS = [
    re.compile(r"(?i)(authorization\s*:\s*(?:bearer|basic)\s+)[^\s]+"),
    re.compile(r"(?i)\b(api[_ -]?key|password|passwd|token|secret)\b\s*[:=]\s*[^\s,;]+"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
]
_model = None
_model_error = None
_model_load_ms = None
_model_loading = False
_model_last_batch_size = None
_model_oom_reductions = 0
_lock = threading.RLock()
_model_lock = threading.RLock()
_freshness_lock = threading.Lock()
_freshness_cache = None

def configured_model_precision():
    value=os.environ.get('KNOWLEDGE_BRIDGE_MODEL_PRECISION','fp32').strip().lower()
    if value not in {'fp32','fp16'}:raise ValueError('KNOWLEDGE_BRIDGE_MODEL_PRECISION 只支持 fp32 或 fp16')
    return value

def configured_embedding_batch_size():
    default=4 if configured_model_precision()=='fp16' else 32
    raw=os.environ.get('KNOWLEDGE_BRIDGE_EMBEDDING_BATCH_SIZE',str(default)).strip()
    try:value=int(raw)
    except ValueError:raise ValueError('KNOWLEDGE_BRIDGE_EMBEDDING_BATCH_SIZE 必须是正整数')
    if value<1 or value>128:raise ValueError('KNOWLEDGE_BRIDGE_EMBEDDING_BATCH_SIZE 必须在 1 到 128 之间')
    return value

def configured_embedding_duty_cycle():
    raw=os.environ.get('KNOWLEDGE_BRIDGE_EMBEDDING_DUTY_CYCLE','1.0').strip()
    try:value=float(raw)
    except ValueError:raise ValueError('KNOWLEDGE_BRIDGE_EMBEDDING_DUTY_CYCLE 必须是小数')
    if value<.1 or value>1:raise ValueError('KNOWLEDGE_BRIDGE_EMBEDDING_DUTY_CYCLE 必须在 0.1 到 1.0 之间')
    return value

def utcnow(): return datetime.now(timezone.utc).isoformat()
def rel(path: Path): return path.resolve().relative_to(VAULT).as_posix()
def is_allowed(path: Path):
    try: parts = path.resolve().relative_to(VAULT).parts
    except ValueError: return False
    source=Path(*parts).as_posix()
    return bool(parts) and parts[0] in FORMAL_LAYERS and path.suffix.lower() == ".md" and source not in DEDICATED_MEMORY_SOURCES and not any(p in EXCLUDED or p.startswith(".") for p in parts[:-1])
def is_formal_document(path: Path):
    """Direct-read policy for formal Markdown, independent from index ingestion."""
    try: parts=path.resolve().relative_to(VAULT.resolve()).parts
    except ValueError:return False
    return bool(parts) and parts[0] in FORMAL_LAYERS and path.suffix.lower()==".md" and not any(p in EXCLUDED or p.startswith(".") for p in parts[:-1])
def scan_files(): return sorted(p for name in FORMAL_LAYERS for p in (VAULT/name).rglob("*.md") if is_allowed(p))
def redact(text: str):
    for p in SECRET_PATTERNS: text = p.sub(lambda m: (m.group(1) if m.lastindex else "") + "[REDACTED]", text)
    return text
def sha(text: str): return hashlib.sha256(text.encode("utf-8")).hexdigest()
def layer_project(source: str):
    parts = Path(source).parts; layer = parts[0] if parts else "根目录"
    project = parts[1] if layer == "02_项目" and len(parts) > 1 else ""
    return layer, project
def _chat_context(source: str):
    """Load manifest-backed context for a normalized raw-chat Markdown file."""
    path=(VAULT/source).resolve()
    if not path.parent.name.startswith("02_"): return None
    manifest_path=path.parent.parent/"manifest.json"
    if not manifest_path.exists(): return None
    try: manifest=json.loads(manifest_path.read_text("utf-8"))
    except (OSError, ValueError): return None
    if path.name not in {Path(item["file"]).name for item in manifest.get("normalized_files",[])}: return None
    return {"project":path.parent.parent.name,"raw_source":manifest.get("source_file",""),"standardized_file":source,
      "messages":{m["message_id"]:m for m in manifest.get("messages",[])}}

def split_long(text: str, limit: int, overlap: int):
    paras = re.split(r"\n\s*\n", text); out=[]; buf=""
    for para in paras:
        if len(buf)+len(para)+2 <= limit: buf += ("\n\n" if buf else "")+para
        else:
            if buf: out.append(buf.strip())
            if len(para) <= limit: buf=para
            else:
                start=0
                while start < len(para):
                    end=min(len(para), start+limit)
                    cut=end if end==len(para) else para.rfind("。", start, end)
                    if cut <= start+limit//2: cut=end
                    out.append(para[start:cut].strip())
                    if cut == len(para): break
                    start=max(cut-overlap, start+1)
                buf=""
    if buf.strip(): out.append(buf.strip())
    return out
def parse_markdown(path: Path):
    raw=path.read_text("utf-8-sig", errors="replace"); source=rel(path)
    title=path.stem; current="正文"; sections=[]; buf=[]; chat=_chat_context(source); current_message=None
    def flush():
        if any(x.strip() for x in buf): sections.append((current, "\n".join(buf).strip(), current_message))
        buf.clear()
    for line in raw.splitlines():
        m=re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if m:
            flush(); current=m.group(2).strip()
            message_match=re.fullmatch(r"MESSAGE\s+(\d{6})[｜|](USER|ASSISTANT)",current)
            if message_match: current_message=(message_match.group(1),message_match.group(2))
            if len(m.group(1)) == 1 and title == path.stem: title=current
        else: buf.append(line)
    flush(); layer, project=layer_project(source); rows=[]; idx=0; message_chunk_indices={}
    for section, body, message_ctx in sections:
        for piece in split_long(redact(body), CFG["max_chunk_chars"], CFG["overlap_chars"]):
            if not piece: continue
            metadata={"source_type":"vault_document","raw_source":"","standardized_file":"","message_id":"","role":"","image_refs":[],"message_order":-1,"chunk_index_in_message":-1}
            row_project=project
            if chat:
                row_project=chat["project"]
                metadata.update({"source_type":"raw_chat_metadata","raw_source":chat["raw_source"],"standardized_file":source})
                if message_ctx:
                    message_id,role=message_ctx; info=chat["messages"].get(message_id,{})
                    chunk_in_message=message_chunk_indices.get(message_id,0); message_chunk_indices[message_id]=chunk_in_message+1
                    metadata.update({"source_type":"raw_chat","message_id":message_id,"role":role,"image_refs":info.get("image_refs",[]),
                      "message_order":info.get("order",int(message_id)),"chunk_index_in_message":chunk_in_message})
            h=sha(piece); rows.append({"source_path":source,"document_title":title,"section_title":section,
              "chunk_id":f"{sha(source)[:12]}-{idx:04d}","content":piece,"modified_at":datetime.fromtimestamp(path.stat().st_mtime,timezone.utc).isoformat(),
              "content_hash":h,"knowledge_layer":layer,"project":row_project,**metadata}); idx+=1
    return raw, rows
def local_model_status():
    cache=Path(CFG["model_cache"]);model_dir=cache/("models--"+CFG["model_name"].replace("/","--"))
    snapshots=model_dir/"snapshots";complete=[]
    if snapshots.is_dir():
        for item in snapshots.iterdir():
            if item.is_dir() and (item/"config.json").is_file() and ((item/"model.safetensors").is_file() or (item/"pytorch_model.bin").is_file()):
                complete.append(item.name)
    state="loaded" if _model is not None else "loading" if _model_loading else "load_failed" if _model_error else "available_unloaded" if complete else "unavailable"
    runtime={"loaded":"loaded","loading":"loading","load_failed":"failed","available_unloaded":"unloaded","unavailable":"unavailable"}[state]
    precision=configured_model_precision();device=None;actual_precision=None
    if _model is not None:
        try:
            parameter=next(_model.parameters());device=str(parameter.device);actual_precision=str(parameter.dtype).replace('torch.','')
        except (StopIteration,AttributeError):pass
    result={"name":CFG["model_name"],"state":state,"installation_state":"installed" if complete else "missing",
            "runtime_state":runtime,"actual_vector_recall_ready":state=="loaded","local_only":True,"cached_snapshots":len(complete),
            "configured_precision":precision,"actual_precision":actual_precision,"device":device,
            "configured_batch_size":configured_embedding_batch_size(),"last_batch_size":_model_last_batch_size,
            "configured_duty_cycle":configured_embedding_duty_cycle(),
            "oom_batch_reductions":_model_oom_reductions,
            "load_ms":_model_load_ms,"error":_model_error}
    from . import encoder_service
    if encoder_service.enabled():
        worker=encoder_service.status()
        result.update(worker)
        result['actual_vector_recall_ready']=result['state']=='loaded'
        if not complete and not worker.get('pid'):result.update(state='unavailable',runtime_state='unavailable')
    return result
def model(*,local_only=False):
    global _model,_model_error,_model_load_ms,_model_loading
    with _model_lock:
        if _model is None:
            from sentence_transformers import SentenceTransformer
            started=time.perf_counter();os.environ["HF_HOME"] = CFG["model_cache"];_model_loading=True
            try:
                precision=configured_model_precision()
                candidate=SentenceTransformer(CFG["model_name"],cache_folder=CFG["model_cache"],device=None,
                                              local_files_only=bool(local_only))
                if precision=='fp16':
                    import torch
                    if not torch.cuda.is_available():raise RuntimeError('FP16 向量模型需要可用的 CUDA 设备')
                    candidate.to('cuda');candidate.half()
                _model=candidate
                _model_error=None;_model_load_ms=round((time.perf_counter()-started)*1000,1)
            except Exception as exc:
                _model=None;_model_error=type(exc).__name__;raise
            finally:_model_loading=False
    return _model
class ModelBusyError(TimeoutError):
    """The shared encoder is occupied; interactive callers should use passages."""

def model_busy():
    from . import encoder_service
    if encoder_service.enabled():return bool(encoder_service.status().get('busy'))
    if not _model_lock.acquire(blocking=False):return True
    _model_lock.release();return False

def embed(texts,*,local_only=False,batch_size=None,lock_timeout=None):
    global _model_last_batch_size,_model_oom_reductions
    from . import encoder_service
    if encoder_service.enabled():
        try:return encoder_service.encode(texts,local_only=local_only,batch_size=batch_size,lock_timeout=lock_timeout)
        except TimeoutError as exc:raise ModelBusyError(str(exc)) from exc
    selected=max(1,int(batch_size or configured_embedding_batch_size()))
    acquired=_model_lock.acquire() if lock_timeout is None else _model_lock.acquire(timeout=max(0,float(lock_timeout)))
    if not acquired:raise ModelBusyError('共享向量模型正忙')
    try:
        encoder=model(local_only=local_only)
        while True:
            try:
                vectors=encoder.encode(texts,normalize_embeddings=True,show_progress_bar=len(texts)>10,
                                       batch_size=selected).astype('float32')
                _model_last_batch_size=selected
                return vectors
            except Exception as exc:
                try:
                    import torch
                    oom=isinstance(exc,torch.cuda.OutOfMemoryError)
                except ImportError:oom=False
                if not oom or selected<=1:raise
                selected=max(1,selected//2);_model_oom_reductions+=1;torch.cuda.empty_cache()
    finally:_model_lock.release()
def start_model_warmup():
    """Load the cached model in the background so the first query never hangs."""
    from . import encoder_service
    if encoder_service.enabled():return encoder_service.warmup()
    global _model_loading
    if not _model_lock.acquire(blocking=False):return False
    try:
        if _model is not None or _model_loading:return False
        _model_loading=True
    finally:_model_lock.release()
    def load():
        global _model_loading,_model_error
        try:model(local_only=True)
        except Exception as exc:_model_error=type(exc).__name__
        finally:_model_loading=False
    timer=threading.Timer(.5,load);timer.name="jiedu-model-warmup";timer.daemon=True;timer.start();return True

def index_freshness(*,max_age_seconds=15.0):
    global _freshness_cache
    now=time.monotonic()
    with _freshness_lock:
        if _freshness_cache and now-_freshness_cache['at']<=max(0,float(max_age_seconds)):
            return _freshness_cache['value']
    indexed=state().get("files",{});current={};unreadable=[]
    for path in scan_files():
        try:
            raw,_=parse_markdown(path);current[rel(path)]=sha(raw)
        except (OSError,UnicodeError,ValueError):unreadable.append(rel(path))
    stale=sorted(path for path,digest in current.items() if path not in indexed or indexed[path].get("hash")!=digest)
    missing=sorted(path for path in indexed if path not in current)
    value={"fresh":not stale and not missing and not unreadable,"actual_files":len(current),"indexed_files":len(indexed),
           "stale_or_unindexed":stale,"indexed_but_missing":missing,"unreadable":unreadable,"checked_at":utcnow(),"cache_ttl_seconds":max_age_seconds}
    with _freshness_lock:_freshness_cache={'at':now,'value':value}
    return value
def invalidate_index_freshness():
    global _freshness_cache
    with _freshness_lock:_freshness_cache=None
def _atomic_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary=path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2)+"\n","utf-8")
    os.replace(temporary,path)
def _active_descriptor():
    try:
        value=json.loads(ACTIVE_INDEX_PATH.read_text("utf-8"))
        if isinstance(value,dict) and value.get("version")==1:return value
    except (OSError,ValueError):pass
    return {"version":0,"table":"chunks","state_file":STATE_PATH.name}
def _active_state_path():
    descriptor=_active_descriptor();relative=Path(str(descriptor.get("state_file") or STATE_PATH.name))
    if relative.is_absolute() or ".." in relative.parts:return STATE_PATH
    return _WORKSPACE.index_root("formal")/relative
def state():
    if not active_workspace().initialized: return {"files":{},"last_sync":None,"last_result":{"status":"workspace_required"},"model":CFG["model_name"]}
    state_path=_active_state_path()
    if state_path.exists(): return json.loads(state_path.read_text("utf-8"))
    return {"files":{},"last_sync":None,"last_result":{},"model":CFG["model_name"]}
def save_state(s,path:Path|None=None):
    _atomic_json(path or _active_state_path(),s);invalidate_index_freshness()
def db():
    if not active_workspace().initialized: raise RuntimeError("尚未选择或初始化工作区")
    DB_PATH.mkdir(parents=True,exist_ok=True); return lancedb.connect(DB_PATH)
def table():
    if not active_workspace().initialized:return None
    d=db();name=_active_descriptor().get("table")
    if not name:return None
    # table_names() is paginated (ten by default); an active generation can be
    # outside its first page. Open the exact atomically published name instead.
    try:return d.open_table(name)
    except ValueError as exc:
        if 'was not found' in str(exc):return None
        raise
def _fts(t):
    t.create_fts_index("content",replace=True,base_tokenizer="ngram",ngram_min_length=2,ngram_max_length=4,stem=False,remove_stop_words=False,ascii_folding=False,with_position=True)
def _source_version(files):
    compact={path:{'hash':item.get('hash'),'chunks':item.get('chunks')} for path,item in sorted(files.items())}
    return hashlib.sha256(json.dumps(compact,ensure_ascii=False,separators=(',',':')).encode('utf-8')).hexdigest()
def _sync_locked(paths=None,operation='incremental'):
    if not active_workspace().initialized:
        return {"status":"workspace_required","error":"workspace_not_initialized","message":"尚未选择或初始化工作区","added_or_modified":0,"deleted":0,"unchanged":0,"embedded_chunks":0,"changed_paths":[]}
    with _lock:
        started=time.perf_counter();s=state();before_version=_source_version(s.get('files',{}));descriptor=_active_descriptor();files=scan_files(); selected=set(rel(Path(p)) if Path(p).is_absolute() else str(p).replace("\\","/") for p in paths) if paths else None
        current={rel(p):p for p in files}; prior=set(s["files"]); changed=[]; unchanged=[]
        for source,p in current.items():
            if selected is not None and source not in selected: continue
            raw,_=parse_markdown(p); h=sha(raw)
            if source not in s["files"] or s["files"][source]["hash"] != h: changed.append((source,p,h))
            else: unchanged.append(source)
        deleted=[x for x in prior-set(current) if selected is None or x in selected]
        t=table()
        for source in deleted:
            if t: t.delete(f"source_path = '{source.replace(chr(39), chr(39)*2)}'")
            s["files"].pop(source,None)
        added_rows=[]
        for source,p,h in changed:
            raw,rows=parse_markdown(p)
            if t: t.delete(f"source_path = '{source.replace(chr(39), chr(39)*2)}'")
            if rows:
                vectors=embed([r["content"] for r in rows],local_only=True)
                for r,v in zip(rows,vectors): r["vector"]=v.tolist()
                if t is None:
                    table_name=_active_descriptor().get("table") or "chunks"
                    t=db().create_table(table_name,data=rows);_fts(t)
                else: t.add(rows)
                added_rows.extend(rows)
            s["files"][source]={"hash":h,"mtime":p.stat().st_mtime,"chunks":len(rows)}
        if t and (changed or deleted):
            try:_fts(t)
            except Exception: pass
        result={"status":"incremental_synced","operation":operation,"generation":descriptor.get('generation'),
                "added_or_modified":len(changed),"deleted":len(deleted),"unchanged":len(unchanged),"embedded_chunks":len(added_rows),
                "changed_paths":[x[0] for x in changed],"deleted_paths":deleted,"source_version_before":before_version,
                "source_version_after":_source_version(s.get('files',{})),"model_process":{"pid":os.getpid(),**local_model_status()},
                "elapsed_ms":round((time.perf_counter()-started)*1000,3)}
        s["last_sync"]=utcnow(); s["source_version"]=result['source_version_after'];s["last_result"]=result; save_state(s)
        audit=_WORKSPACE.audit/'index-incremental.jsonl';audit.parent.mkdir(parents=True,exist_ok=True)
        with audit.open('a',encoding='utf-8',newline='\n') as handle:
            handle.write(json.dumps({'at':s['last_sync'],**result},ensure_ascii=False,separators=(',',':'))+'\n')
        # The semantic map is a separately frozen projection. Ordinary Markdown
        # indexing must not trigger an expensive full UMAP recomputation.
        return result
def rebuild_index(*,lock_timeout:float=8.0,operation:str="manual_rebuild"):
    """Build and validate a complete generation before an atomic pointer switch."""
    if not active_workspace().initialized:
        return {"status":"workspace_required","error":"workspace_not_initialized","message":"尚未选择或初始化工作区"}
    gate=IndexWriteGate(active_workspace())
    try:
        with gate.hold(operation,lock_timeout):
            files=scan_files();rows=[];file_state={}
            for path in files:
                raw,parsed=parse_markdown(path);source=rel(path);digest=sha(raw)
                if parsed:
                    vectors=embed([row["content"] for row in parsed],local_only=True)
                    for row,vector in zip(parsed,vectors):row["vector"]=vector.tolist()
                    rows.extend(parsed)
                file_state[source]={"hash":digest,"mtime":path.stat().st_mtime,"chunks":len(parsed)}
            chunk_ids=[row["chunk_id"] for row in rows]
            if len(chunk_ids)!=len(set(chunk_ids)):
                raise RuntimeError("索引重建校验失败：知识块身份重复")
            expected=sum(item["chunks"] for item in file_state.values())
            if expected!=len(rows):raise RuntimeError("索引重建校验失败：知识块数量不一致")
            generation=uuid.uuid4().hex
            table_name=f"chunks_{generation}"
            state_relative=f"generations/state-{generation}.json"
            state_path=_WORKSPACE.index_root("formal")/state_relative
            if rows:
                created=db().create_table(table_name,data=rows);_fts(created)
                written=created.to_arrow().to_pylist()
                if len(written)!=len(rows) or len({row["chunk_id"] for row in written})!=len(rows):
                    db().drop_table(table_name)
                    raise RuntimeError("索引重建校验失败：落盘数据重复或缺失")
            source_version=_source_version(file_state)
            rebuilt={"files":file_state,"source_version":source_version,"last_sync":utcnow(),"last_result":{
                "status":"rebuilt","added_or_modified":len(files),"deleted":0,"unchanged":0,
                "embedded_chunks":len(rows),"changed_paths":sorted(file_state),"generation":generation,"source_version_after":source_version,
            },"model":CFG["model_name"]}
            save_state(rebuilt,state_path)
            descriptor={"version":1,"workspace_id":active_workspace().workspace_id,
                        "generation":generation,"table":table_name if rows else None,
                        "state_file":state_relative,"activated_at":utcnow()}
            _atomic_json(ACTIVE_INDEX_PATH,descriptor)
            from .index_retention import after_publish
            after_publish(_WORKSPACE,'formal')
            return rebuilt["last_result"]|{"file_count":len(files),"unique_chunks":len(chunk_ids)}
    except IndexBusyError as exc:return exc.public_status()
def sync(force=False,paths=None,*,operation:str="incremental",lock_timeout:float=8.0):
    if force:return rebuild_index(lock_timeout=lock_timeout,operation=operation if operation!="incremental" else "forced_rebuild")
    if not active_workspace().initialized:return _sync_locked(paths,operation)
    try:
        with IndexWriteGate(active_workspace()).hold(operation,lock_timeout):return _sync_locked(paths,operation)
    except IndexBusyError as exc:return exc.public_status()
def recover_index_writer():
    if not active_workspace().initialized:return False
    return IndexWriteGate(active_workspace()).recover_if_abandoned()
def rows_from_arrow(data): return data.to_pylist()
def _format(rows, method):
    out=[]
    for i,r in enumerate(rows):
        dist=r.get("_distance"); score=r.get("_relevance_score", r.get("_score", (1/(1+dist) if dist is not None else None)))
        out.append({k:r.get(k) for k in ("source_path","document_title","section_title","chunk_id","content","knowledge_layer","project","source_type","raw_source","standardized_file","message_id","role","image_refs","message_order","chunk_index_in_message")} | {"search_method":method,"rank":i+1,"score":float(score) if score is not None else None})
    return out

_LEXICAL_STOP={"什么","为什么","怎么","哪些","时候","以前","后来","当时","开始","主要","方向","明确","一个","这个","那个","可以","需要","如何","是否","我的","我以","了吗","方向做"}
def _lexical_terms(query):
    terms=[]
    for latin in re.findall(r"[A-Za-z][A-Za-z0-9_.+-]+",query.casefold()):
        if len(latin)>=2: terms.append(latin)
    # Error/status codes such as 500 and explicit versions carry more signal than
    # surrounding Chinese function words and must remain available to keyword search.
    terms.extend(re.findall(r"(?<!\d)\d{3,}(?:\.\d+)*(?!\d)",query.casefold()))
    for run in re.findall(r"[\u3400-\u9fff]+",query):
        for size in (4,3,2):
            terms.extend(run[i:i+size] for i in range(len(run)-size+1) if run[i:i+size] not in _LEXICAL_STOP)
    if any(x in query for x in ("不满","纠正","纠错","不满意")):
        terms.extend(("不对","不是","不要","纠正","纠错","误解","擅自","幻觉","停止重试"))
    if any(x in query for x in ("跨聊天","跨对话","长期认识","记住我")):
        terms.extend(("新对话","聊天窗口","长期协作","记住","上下文","迁移","协作档案","带走"))
    return list(dict.fromkeys(terms))

def _matches_filters(row,source_type=None,folder=None,project=None):
    if source_type and row.get("source_type") != source_type: return False
    if folder and not row.get("source_path","").startswith(folder.strip("/")+"/"): return False
    if project and row.get("project") != project: return False
    return True

def _historical_intent(query): return any(x in query for x in ("以前","过去","当时","后来","什么时候","开始","曾经","聊天","对话","记得","长期认识"))
def _quality(row,query):
    content=row.get("content","").strip(); factor=1.0
    if row.get("source_type")=="raw_chat_metadata": factor*=.18
    if re.fullmatch(r"(?:\[\[[^]]+\]\]|\[[^]]+\]\([^)]+\))\s*",content): factor*=.18
    elif len(content)<45: factor*=.68
    if _historical_intent(query):
        if row.get("source_type")=="raw_chat": factor*=1.38
        elif row.get("knowledge_layer")=="00_系统": factor*=.52
        else: factor*=.8
        if query.lstrip().startswith("我") and any(x in query for x in ("为什么","不满","纠正")):
            factor*=1.18 if row.get("role")=="USER" else .94
    elif any(x in query for x in ("系统规则","知识库规则","配置规则","规范")) and row.get("knowledge_layer")=="00_系统": factor*=1.3
    if any(x in query for x in ("不满","纠正","纠错","不满意")):
        evidence=sum(content.count(x) for x in ("不对","不是","不要","纠正","纠错","误解","擅自","幻觉","停止重试"))
        factor*=1+min(.32,evidence*.045)
    if any(x in query for x in ("跨聊天","跨对话","长期认识","记住我")):
        evidence=sum(content.count(x) for x in ("新对话","聊天窗口","长期协作","记住","上下文","迁移","协作档案","带走"))
        factor*=1+min(.4,evidence*.065)
    if any(x in query for x in ("主要解决","解决什么")):
        evidence=content.count("真实需求")*4+sum(content.count(x) for x in ("完成","实现","问题","流程","交付","系统","产品设计","软件开发"))
        factor*=1+min(.5,evidence*.055)
    return factor

def _keyword_rows(query,limit,source_type=None,folder=None,project=None):
    rows=[r for r in all_rows() if _matches_filters(r,source_type,folder,project)]
    # Three formal mother documents are deliberately outside the vector table.
    # Make all four formal Core sources retrievable as exact, read-only evidence
    # without creating vectors or treating the compiled Core as a fifth source.
    if source_type in (None,"formal_core") and project is None:
        for path in (VAULT/"00_系统").glob("*.md"):
            source=rel(path)
            if source not in {"00_系统/我的长期画像.md","00_系统/AI协作规则.md","00_系统/AI错误与教训.md","00_系统/知识库规则.md"}: continue
            if folder and not source.startswith(folder.rstrip("/")+"/"): continue
            text=path.read_text("utf-8-sig")
            rows.append({"chunk_id":"formal-core-"+hashlib.sha256(source.encode()).hexdigest()[:12],"source_path":source,"document_title":path.stem,"section_title":"正式母档全文","content":text,"knowledge_layer":"00_系统","project":"","source_type":"formal_core","raw_source":"","standardized_file":"","message_id":"","role":"","image_refs":"[]","message_order":-1,"chunk_index_in_message":0})
    terms=_lexical_terms(query)
    if not terms: return []
    folded=[r.get("content","").casefold() for r in rows]
    dfs={term:sum(term in text for text in folded) for term in terms}
    terms=sorted((term for term in terms if dfs[term]),key=lambda term:(math.log((len(rows)+1)/(dfs[term]+1))+1)*(len(term)**1.35),reverse=True)[:12]
    scored=[]; total=max(1,len(rows))
    for row,text in zip(rows,folded):
        score=0.0
        for term in terms:
            hits=text.count(term)
            if hits: score+=(1+math.log(hits))*(math.log((total+1)/(dfs[term]+1))+1)*(len(term)**1.35)
        # Metadata remains searchable, but can never outweigh substantive body matches.
        meta=(row.get("document_title","")+" "+row.get("section_title","")).casefold()
        score+=sum((math.log((total+1)/(dfs[t]+1))+1)*len(t)*.04 for t in terms if t in meta)
        if score:
            item=dict(row); item["_score"]=score*_quality(row,query); scored.append(item)
    scored.sort(key=lambda r:r["_score"],reverse=True)
    return scored[:limit]

def _result_key(row): return ("message",row["message_id"]) if row.get("message_id") else ("chunk",row["chunk_id"])
def _diversify(rows,limit):
    selected=[]; seen=set(); recent_orders=[]
    for row in rows:
        key=_result_key(row)
        if key in seen: continue
        order=row.get("message_order",-1)
        if order is not None and order>=0 and sum(abs(order-other)<=3 for other in recent_orders)>=2: continue
        seen.add(key); selected.append(row)
        if order is not None and order>=0: recent_orders.append(order)
        if len(selected)>=limit: break
    return selected

def _vector_rows(query,query_vector,limit,source_type=None,folder=None,project=None):
    t=table(); candidates=rows_from_arrow(t.search(query_vector).limit(max(100,limit*20)).to_arrow()); ranked=[]
    terms=_lexical_terms(query)
    for r in candidates:
        if not _matches_filters(r,source_type,folder,project): continue
        semantic=1/(1+float(r.get("_distance",1)))
        overlap=sum(len(term) for term in terms if term in r.get("content","").casefold())
        item=dict(r); item["_score"]=(semantic+.006*overlap)*_quality(r,query); ranked.append(item)
    ranked.sort(key=lambda r:r["_score"],reverse=True)
    return _format(ranked[:limit],"vector")

def search_detailed(query,limit=10,source_type=None,folder=None,project=None,include_context=True):
    t=table()
    if not t or not query.strip(): return {"query":query,"results":[],"observability":{"keyword_candidates":0,"vector_candidates":0,"context_candidates":0,"hybrid_candidates":0,"deduplicated_candidates":0,"returned":0,"requested_limit":limit}}
    candidate_limit=max(50,limit*5); query_vector=embed([query])[0]
    keyword=search(query,"keyword",candidate_limit,source_type,folder,project); vector=_vector_rows(query,query_vector,candidate_limit,source_type,folder,project)
    context=[]
    if include_context and (source_type in (None,"raw_chat")):
        from .context_retrieval import context_search
        context=context_search(query_vector,candidate_limit)
    merged={}
    vector_max=max((r.get("score") or 0 for r in vector),default=1) or 1
    for weight,group in ((.75,keyword),(1.15,vector)):
        for r in group:
            item=merged.setdefault(_result_key(r),dict(r)|{"_fusion":0.0,"retrieval_channels":[]})
            item["_fusion"]+=weight/(20+r["rank"])
            if group is vector: item["_fusion"]+=.035*((r.get("score") or 0)/vector_max)
            item["retrieval_channels"].append("keyword" if group is keyword else "vector")
    rows_by_message={r.get("message_id"):r for r in all_rows() if r.get("message_id")}
    for window in context:
        for position,message_id in enumerate(window["member_message_ids"]):
            row=rows_by_message.get(message_id)
            if not row or not _matches_filters(row,source_type,folder,project): continue
            key=_result_key(row); item=merged.setdefault(key,dict(row)|{"search_method":"hybrid","rank":0,"score":None,"_fusion":0.0,"retrieval_channels":[]})
            center_distance=abs(position-(len(window["member_message_ids"])-1)/2); item["_fusion"]+=.92/(20+window["rank"])/(1+.08*center_distance)
            item["retrieval_channels"].append("context"); item.setdefault("context_window_ids",[]).append(window["window_id"])
    ranked=list(merged.values())
    for item in ranked: item["_score"]=item.pop("_fusion"); item["retrieval_channels"]=list(dict.fromkeys(item["retrieval_channels"]))
    ranked.sort(key=lambda r:r["_score"],reverse=True); diversified=_diversify(ranked,limit)
    results=_format(diversified,"hybrid")
    for formatted,raw in zip(results,diversified): formatted["retrieval_channels"]=raw["retrieval_channels"]; formatted["context_window_ids"]=raw.get("context_window_ids",[])
    channel_contributions=len(keyword)+len(vector)+sum(len(w["member_message_ids"]) for w in context)
    return {"query":query,"results":results,"observability":{"keyword_candidates":len(keyword),"vector_candidates":len(vector),"context_candidates":len(context),"hybrid_candidates":channel_contributions,"deduplicated_candidates":len(ranked),"returned":len(results),"requested_limit":limit}}

def search(query, method="hybrid", limit=8, source_type=None, folder=None, project=None):
    t=table()
    if not t or not query.strip(): return []
    if method == "keyword":
        return _format(_keyword_rows(query,limit,source_type,folder,project),method)
    elif method == "vector":
        return _vector_rows(query,embed([query])[0],limit,source_type,folder,project)
    else:
        return search_detailed(query,limit,source_type,folder,project)["results"]

def message_context(message_id:str,before=2,after=2):
    rows=[r for r in all_rows() if r.get("source_type")=="raw_chat" and r.get("message_id")]
    by_message={}
    for row in rows: by_message.setdefault(row["message_id"],row)
    target=by_message.get(str(message_id).zfill(6))
    if not target: raise ValueError("MESSAGE不存在")
    ordered=sorted((r for r in by_message.values() if r.get("raw_source")==target.get("raw_source")),key=lambda r:r.get("message_order",0)); index=next(i for i,r in enumerate(ordered) if r["message_id"]==target["message_id"])
    selected=ordered[max(0,index-before):index+after+1]
    return {"core_message_id":target["message_id"],"messages":[{k:r.get(k) for k in ("message_id","role","content","source_path","raw_source","standardized_file","image_refs")}|{"is_core":r["message_id"]==target["message_id"]} for r in selected]}
def get_document(source, chunk_id=None, context=1):
    p=(VAULT/source).resolve()
    if not is_formal_document(p) or not p.is_file(): raise ValueError("无效或不存在的知识文件")
    raw, chunks=parse_markdown(p); index=next((i for i,x in enumerate(chunks) if x["chunk_id"]==chunk_id),None)
    selected=chunks if index is None else chunks[max(0,index-context):index+context+1]
    return {"source_path":source,"content":redact(raw),"chunks":selected}
def overview():
    if not active_workspace().initialized:
        return {"status":"workspace_required","workspace_state":"uninitialized","workspace_id":None,"file_count":0,"chunk_count":0,"vector_count":0,"project_count":0,"embedding_model":CFG["model_name"],"index_status":"尚未选择工作区","last_sync":None,"last_result":{}}
    s=state(); t=table(); count=t.count_rows() if t else 0; projects={x.split("/")[1] for x in s["files"] if x.startswith("02_项目/") and len(x.split("/"))>2}
    freshness=index_freshness();model_state=local_model_status()
    descriptor=_active_descriptor()
    index_ready=bool(t and freshness['fresh']);loaded=model_state['state']=='loaded';installed=model_state['installation_state']=='installed'
    return {"file_count":len(s["files"]),"chunk_count":count,"vector_count":count,"project_count":len(projects),"embedding_model":CFG["model_name"],"index_status":"就绪" if index_ready else "待同步" if t else "未建立","last_sync":s["last_sync"],"last_result":s["last_result"],"index_freshness":freshness,"active_generation":descriptor.get('generation'),"source_version":s.get('source_version') or _source_version(s.get('files',{})),"model_process":{"pid":os.getpid(),**model_state},"vector_model":model_state,"vector_index_ready":index_ready,"vector_query_available":bool(index_ready and installed),"vector_query_requires_model_load":bool(index_ready and installed and not loaded),"vector_query_ready":bool(index_ready and loaded),"vector_status_explanation":('本进程模型已加载，可立即执行向量召回' if loaded and index_ready else '向量索引可用；模型已安装但本进程尚未加载，keyword 查询不会触发向量召回' if index_ready and installed else '向量索引或模型尚未就绪')}
def all_rows():
    t=table(); return [] if not t else t.to_arrow().to_pylist()
def build_map():
    rows=all_rows()
    if not rows: MAP_PATH.parent.mkdir(parents=True,exist_ok=True); MAP_PATH.write_text("[]","utf-8"); return []
    vec=np.array([r["vector"] for r in rows],dtype="float32")
    if len(rows)>=4:
        import umap; xy=umap.UMAP(n_components=2,n_neighbors=min(15,len(rows)-1),min_dist=.15,metric="cosine",random_state=42).fit_transform(vec)
    elif len(rows)>=2:
        from sklearn.decomposition import PCA; xy=PCA(n_components=2).fit_transform(vec)
    else: xy=np.array([[0.,0.]])
    data=[{k:r.get(k) for k in ("chunk_id","source_path","document_title","section_title","knowledge_layer","project","content")} | {"x":float(x),"y":float(y)} for r,(x,y) in zip(rows,xy)]
    MAP_PATH.parent.mkdir(parents=True,exist_ok=True); MAP_PATH.write_text(json.dumps(data,ensure_ascii=False),"utf-8"); return data
def graph():
    nodes={}; edges=[]; files=scan_files()
    by_stem={}
    for candidate in files: by_stem.setdefault(candidate.stem,rel(candidate))
    for p in files:
        source=rel(p); nodes[source]={"id":source,"label":p.stem,"layer":layer_project(source)[0]}; text=p.read_text("utf-8-sig",errors="replace")
        parent="dir:"+str(Path(source).parent).replace("\\","/"); nodes.setdefault(parent,{"id":parent,"label":str(Path(source).parent),"layer":"目录"}); edges.append({"source":parent,"target":source,"type":"目录"})
        for target in re.findall(r"\[\[([^\]|#]+)",text):
            match=by_stem.get(Path(target).stem)
            if match: edges.append({"source":source,"target":match,"type":"Wikilink"})
        for target in re.findall(r"\[[^]]+\]\(([^)]+\.md)(?:#[^)]+)?\)",text):
            q=(p.parent/target).resolve()
            if is_allowed(q) and q.exists(): edges.append({"source":source,"target":rel(q),"type":"Markdown链接"})
    return {"nodes":list(nodes.values()),"edges":edges}
