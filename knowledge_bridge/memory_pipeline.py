from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import threading
import uuid
from contextlib import contextmanager
from functools import wraps
from filelock import FileLock
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from .core import ROOT, VAULT
from .workspace import active_workspace

RUNTIME_ROOT=active_workspace().state/"memory-control"
RAW_STORE=active_workspace().raw_message_store
CANDIDATE_EVENTS=RUNTIME_ROOT/"candidate-events.jsonl"
STAGING_EVENTS=RUNTIME_ROOT/"approved-staging.jsonl"
ATTACHMENT_MANIFEST=RUNTIME_ROOT/"attachment-manifest.json"
AUDIT_EVENTS=active_workspace().audit/"memory-control.jsonl"
ALLOWED_ROLES={"user","assistant","system","tool"}
MEMORY_TYPES={"user_profile","collaboration_rule","project","project_state","decision","ai_failure","preference","priority","knowledge"}
_lock=threading.RLock()
_file_locks={}

from contextvars import ContextVar
_candidate_workspace=ContextVar('jiedu_candidate_workspace',default=None)

@contextmanager
def in_workspace(context):
    """Bind existing candidate storage without mutating shared module globals."""
    token=_candidate_workspace.set(context)
    try:yield
    finally:_candidate_workspace.reset(token)

def _workspace():return _candidate_workspace.get() or active_workspace()
def _workspace_root():
    context=_candidate_workspace.get()
    return context.root if context is not None else VAULT

def _workspace_path(name):
    context=_candidate_workspace.get()
    if context is None:return globals()[name]
    paths={'CANDIDATE_EVENTS':context.state/'memory-control'/'candidate-events.jsonl',
           'STAGING_EVENTS':context.state/'memory-control'/'approved-staging.jsonl',
           'AUDIT_EVENTS':context.audit/'memory-control.jsonl',
           'RAW_STORE':context.raw_message_store,
           'ATTACHMENT_MANIFEST':context.state/'memory-control'/'attachment-manifest.json'}
    return paths[name]

@contextmanager
def candidate_transaction():
    with _lock:
        _workspace_path('CANDIDATE_EVENTS').parent.mkdir(parents=True,exist_ok=True)
        key=str(_workspace_path('CANDIDATE_EVENTS'))+'.lock'
        lock=_file_locks.setdefault(key,FileLock(key))
        with lock:yield

def serialized_candidate(function):
    @wraps(function)
    def wrapped(*args,**kwargs):
        with candidate_transaction():return function(*args,**kwargs)
    return wrapped

def utcnow(): return datetime.now(timezone.utc).isoformat()
def _digest(value:str): return hashlib.sha256(value.encode("utf-8")).hexdigest()
def _append_jsonl(path:Path,payload:dict):
    path.parent.mkdir(parents=True,exist_ok=True)
    line=json.dumps(payload,ensure_ascii=False,separators=(",",":"))+"\n"
    with path.open("a",encoding="utf-8",newline="\n") as handle:
        handle.write(line); handle.flush(); os.fsync(handle.fileno())
def _read_jsonl(path:Path):
    if not path.exists(): return []
    rows=[]
    for line in path.read_text("utf-8").splitlines():
        if line.strip(): rows.append(json.loads(line))
    return rows
def _audit(action:str,subject_id:str,detail:dict|None=None):
    _append_jsonl(_workspace_path('AUDIT_EVENTS'),{"at":utcnow(),"action":action,"subject_id":subject_id,"detail":detail or {}})

def save_raw_conversation(payload:dict,store_root:Path|None=None):
    """Append one message without touching the legacy RAW_SOURCE."""
    root=store_root or _workspace_path('RAW_STORE')
    conversation_id=str(payload.get("conversation_id") or "").strip()
    message_id=str(payload.get("message_id") or "").strip()
    role=str(payload.get("role") or "").strip().lower()
    content=payload.get("content")
    if not conversation_id or not message_id: raise ValueError("conversation_id和message_id不能为空")
    if role not in ALLOWED_ROLES: raise ValueError("role无效")
    if not isinstance(content,str) or not content: raise ValueError("content不能为空")
    attachments=payload.get("attachment_refs") or []
    if not isinstance(attachments,list) or not all(isinstance(x,str) for x in attachments): raise ValueError("attachment_refs必须为字符串数组")
    timestamp=str(payload.get("timestamp") or utcnow()); source=str(payload.get("source") or "unknown")
    provenance=payload.get("provenance") or {}
    if not isinstance(provenance,dict): raise ValueError("provenance必须为对象")
    record={"conversation_id":conversation_id,"message_id":message_id,"role":role,"content":content,"timestamp":timestamp,"source":source,"attachment_refs":attachments,"provenance":provenance}
    record["content_sha256"]=_digest(content)
    conversations=root/"conversations.jsonl"; messages=root/"messages.jsonl"
    with _lock:
        existing=[x for x in _read_jsonl(messages) if x["conversation_id"]==conversation_id and x["message_id"]==message_id]
        if existing:
            if existing[-1]["content_sha256"]!=record["content_sha256"] or existing[-1]["role"]!=role: raise ValueError("同一消息ID内容冲突")
            return {"saved":False,"duplicate":True,"record":existing[-1]}
        if not any(x["conversation_id"]==conversation_id for x in _read_jsonl(conversations)):
            _append_jsonl(conversations,{"conversation_id":conversation_id,"created_at":timestamp,"source":source,"provenance":provenance})
        _append_jsonl(messages,record)
        if store_root is None: _audit("raw_message_saved",message_id,{"conversation_id":conversation_id,"source":source})
    return {"saved":True,"duplicate":False,"record":record}

class MemoryExtractorProvider(Protocol):
    name:str
    def extract(self,messages:list[dict])->list[dict]: ...

@dataclass
class DeterministicTestProvider:
    name:str="deterministic-test"
    def extract(self,messages:list[dict])->list[dict]:
        candidates=[]
        for message in messages:
            for marker,memory_type in (("[RULE]","collaboration_rule"),("[LESSON]","ai_failure"),("[PREFERENCE]","preference"),("[PROJECT]","project"),("[PRIORITY]","priority")):
                if marker in message.get("content",""):
                    content=message["content"].split(marker,1)[1].strip()
                    if content: candidates.append({"type":memory_type,"title":content[:60],"content":content,"evidence":[{"conversation_id":message["conversation_id"],"message_id":message["message_id"]}]})
        return candidates

def _candidate_snapshot(events:list[dict]|None=None):
    result={}
    for event in events if events is not None else _read_jsonl(_workspace_path('CANDIDATE_EVENTS')):
        cid=event["candidate_id"]
        if event["event"]=="created": result[cid]=dict(event["candidate"])
        elif cid in result:
            if event["event"]=="edited":
                changes=event["changes"]
                previous=result[cid]
                provenance=previous.get('provenance') or {}
                continuity=provenance.get('continuity') or {}
                # Provider merges carry their own provenance. A normal review edit
                # changes the proposal text but must not rewrite the model's hash.
                # Older UI edits lack review_origin; their restricted event shape
                # is still an append-only record of the same review action.
                review_content=(continuity.get('mode')=='project_issue' and 'content' in changes
                                and 'provenance' not in changes and
                                set(changes)<={'title','content','type','project','evidence','supersedes','conflicts'})
                if review_content:
                    original_hash=continuity['content_hash']
                    review={'origin':event.get('review_origin') or 'legacy_candidate_edit_event',
                            'at':event['at'],'original_model_content_hash':original_hash,
                            'previous_content_hash':_digest(previous['content'].strip()),
                            'content_hash':_digest(str(changes['content']).strip())}
                    result[cid]['provenance']=provenance|{'continuity':continuity|{'review_edit':review}}
                result[cid].update(changes)
            if event["event"] in {"accepted","rejected","less_often"}: result[cid]["status"]={"accepted":"approved_staging","rejected":"rejected","less_often":"less_often"}[event["event"]]
            if event["event"]=="written":
                result[cid]["status"]="written"
                result[cid].pop("write_reverted_at",None)
                result[cid].pop("reverted_writer_version",None)
                if event.get("writer_version"): result[cid]["writer_version"]=event["writer_version"]
            if event["event"]=="reverted":
                result[cid]["status"]="approved_staging"
                result[cid]["write_reverted_at"]=event["at"]
                result[cid]["reverted_writer_version"]=result[cid].get("writer_version")
                result[cid].pop("writer_version",None)
            if event["event"]=="reorganized":
                result[cid]["status"]="reorganized"
                result[cid]["reorganization"]=event["resolution"]
            result[cid]["updated_at"]=event["at"]
    return result

def list_candidates(status:str|None=None):
    rows=list(_candidate_snapshot().values())
    if status: rows=[x for x in rows if x["status"]==status]
    return sorted(rows,key=lambda x:x["created_at"],reverse=True)

def create_candidate(data:dict,provider:str="manual"):
    with candidate_transaction():
        return _create_candidate(data,provider)

def _create_candidate(data:dict,provider:str="manual"):
    from .organizer import reject_protected
    reject_protected(data)
    memory_type=str(data.get("type") or "")
    if memory_type not in MEMORY_TYPES: raise ValueError("记忆类型无效")
    title=str(data.get("title") or "").strip(); content=str(data.get("content") or "").strip(); evidence=data.get("evidence") or []
    if not title or not content: raise ValueError("标题和内容不能为空")
    if not isinstance(evidence,list) or not evidence: raise ValueError("候选必须包含证据")
    provenance=data.get('provenance') or {}
    identity={"type":memory_type,"content":content,"evidence":evidence,"target":provenance.get('target_path'),"action":provenance.get('proposed_action'),"expected_target_hash":provenance.get('expected_target_hash')}
    if provenance.get('continuity'):identity['continuity']=provenance['continuity']
    if provenance.get('reorganization'):identity['reorganization']=provenance['reorganization']
    canonical=_digest(json.dumps(identity,ensure_ascii=False,sort_keys=True))
    for existing in list_candidates():
        old=existing.get('provenance') or {}
        if provenance.get('request_id') and old.get('client_id')==provenance.get('client_id') and old.get('request_id')==provenance['request_id']:
            if old.get('request_digest')!=provenance.get('request_digest'):raise ValueError('请求标识已用于不同内容')
            if old.get('authorization')==provenance.get('authorization'):return {'created':False,'duplicate_of':existing}
        if provenance.get('client_id')!=old.get('client_id'):continue
        # Content identity is stable, but a revoked authorization is not a
        # reusable review receipt. Keep that record and its evidence immutable.
        if old.get('authorization')!=provenance.get('authorization'):continue
        if existing["dedup_key"]==canonical and existing["status"]!="rejected": return {"created":False,"duplicate_of":existing}
    candidate={"candidate_id":"mem_"+uuid.uuid4().hex[:16],"type":memory_type,"title":title,"content":content,"status":"pending","project":data.get("project"),"evidence":evidence,"provenance":data.get("provenance") or {},"supersedes":data.get("supersedes") or [],"conflicts":data.get("conflicts") or [],"provider":provider,"dedup_key":canonical,"created_at":utcnow(),"updated_at":utcnow()}
    with _lock: _append_jsonl(_workspace_path('CANDIDATE_EVENTS'),{"event":"created","at":candidate["created_at"],"candidate_id":candidate["candidate_id"],"candidate":candidate}); _audit("candidate_created",candidate["candidate_id"],{"provider":provider})
    return {"created":True,"candidate":candidate}

def extract_candidates(messages:list[dict],provider:MemoryExtractorProvider):
    output=[]
    for item in provider.extract(messages): output.append(create_candidate(item,provider.name))
    return output

@serialized_candidate
def edit_candidate(candidate_id:str,changes:dict):
    from .organizer import reject_protected
    reject_protected(changes)
    current=_candidate_snapshot().get(candidate_id)
    if not current or current["status"]!="pending": raise ValueError("候选不存在或不可修改")
    allowed={k:v for k,v in changes.items() if k in {"title","content","type","project","evidence","supersedes","conflicts"}}
    merged=current|allowed
    if 'evidence' in allowed:
        from .organizer import evidence_checked
        allowed['evidence']=evidence_checked(_workspace(),allowed['evidence'])
        if (current.get('provenance') or {}).get('interface')=='external_organizer' and allowed['evidence']!=current['evidence']:raise ValueError('外部候选不能改换已授权来源；请重新读取并提交新候选')
    if merged["type"] not in MEMORY_TYPES or not str(merged["title"]).strip() or not str(merged["content"]).strip() or not merged["evidence"]: raise ValueError("候选修改后不符合Schema")
    continuity=(current.get('provenance') or {}).get('continuity') or {}
    if continuity.get('mode')=='project_issue':
        from .continuous_organization import ContinuousOrganizer
        organizer=ContinuousOrganizer(_workspace())
        organizer.validate_candidate(current)
        if 'content' in allowed:organizer.validate_project_review_content(current,str(allowed['content']))
    at=utcnow()
    with _lock:
        _append_jsonl(_workspace_path('CANDIDATE_EVENTS'),{"event":"edited","at":at,"candidate_id":candidate_id,"changes":allowed,
                                        "review_origin":"candidate_review_edit"})
        _audit("candidate_edited",candidate_id,{"fields":list(allowed),"review_origin":"candidate_review_edit",
                                                 "model_content_hash":continuity.get('content_hash'),
                                                 "edited_content_hash":_digest(str(allowed['content']).strip()) if 'content' in allowed else None})
    return _candidate_snapshot()[candidate_id]

@serialized_candidate
def merge_continuity_candidate(context,candidate_id:str,content:str,evidence:list[dict],source_roles:list[dict],content_hash:str,conflicts:list,uncertainties:list):
    """Extend one pending project-continuity candidate without changing its identity.

    This is deliberately narrower than ``edit_candidate``: only an explicitly
    keyed continuity item may gain source evidence, and the target/baseline/client
    receipt stay immutable.  The caller has already decided semantic sameness;
    this function never guesses it from text similarity.
    """
    from .organizer import evidence_checked,reject_protected
    reject_protected({'content':content,'evidence':evidence,'source_roles':source_roles})
    current=_candidate_snapshot().get(candidate_id)
    if not current or current['status']!='pending':raise ValueError('连续事项候选不存在或已离开待审核状态')
    provenance=current.get('provenance') or {};continuity=provenance.get('continuity') or {}
    if provenance.get('interface')!='external_organizer' or continuity.get('mode')!='project_issue' or not continuity.get('merge_key'):
        raise PermissionError('只有显式事项标识的待审核连续候选可以续并')
    checked=evidence_checked(context,evidence)
    def evkey(row):return (row.get('source_path'),row.get('version'),row.get('byte_start'),row.get('byte_end'),row.get('quote_sha256'))
    if not {evkey(x) for x in current['evidence']}<={evkey(x) for x in checked}:raise ValueError('续并不得删除或替换既有来源')
    old_roles={(x.get('session_id'),x.get('message_id'),x.get('version'),x.get('kind')) for x in continuity.get('source_roles',[])}
    new_roles={(x.get('session_id'),x.get('message_id'),x.get('version'),x.get('kind')) for x in source_roles}
    if not old_roles<=new_roles:raise ValueError('续并不得删除或改写既有消息角色')
    updated_continuity=continuity|{'source_roles':source_roles,'content_hash':content_hash}
    updated_provenance=provenance|{'continuity':updated_continuity}
    identity={'type':current['type'],'content':content,'evidence':checked,'target':provenance.get('target_path'),'action':provenance.get('proposed_action'),'expected_target_hash':provenance.get('expected_target_hash'),'continuity':updated_continuity}
    updated_provenance['uncertainties']=uncertainties
    updated_provenance['conflict_status']='possible' if conflicts else 'none'
    changes={'content':content,'evidence':checked,'conflicts':conflicts,'provenance':updated_provenance,'dedup_key':_digest(json.dumps(identity,ensure_ascii=False,sort_keys=True))}
    with _lock:_append_jsonl(_workspace_path('CANDIDATE_EVENTS'),{'event':'edited','at':utcnow(),'candidate_id':candidate_id,'changes':changes});_audit('continuity_candidate_merged',candidate_id,{'source_count':len(source_roles)})
    return _candidate_snapshot()[candidate_id]

@serialized_candidate
def review_candidate(candidate_id:str,decision:str):
    current=_candidate_snapshot().get(candidate_id)
    if not current or current["status"]!="pending": raise ValueError("候选不存在或已审核")
    if decision not in {"accept","reject","less_often"}: raise ValueError("审核决定无效")
    event={"accept":"accepted","reject":"rejected","less_often":"less_often"}[decision]; at=utcnow()
    with _lock:
        _append_jsonl(_workspace_path('CANDIDATE_EVENTS'),{"event":event,"at":at,"candidate_id":candidate_id})
        if event=="accepted": _append_jsonl(_workspace_path('STAGING_EVENTS'),{"event":"staged","at":at,"candidate_id":candidate_id,"candidate":current|{"status":"approved_staging"}})
        _audit("candidate_"+event,candidate_id)
    return _candidate_snapshot()[candidate_id]

@serialized_candidate
def mark_candidate_written(candidate_id:str,version:str):
    current=_candidate_snapshot().get(candidate_id)
    if current and current['status']=='written' and current.get('writer_version')==version:return current
    if not current or current["status"]!="approved_staging": raise ValueError("候选不存在或未处于已接受待写入状态")
    with _lock:
        _append_jsonl(_workspace_path('CANDIDATE_EVENTS'),{"event":"written","at":utcnow(),"candidate_id":candidate_id,"writer_version":version})
        _audit("candidate_written",candidate_id,{"version":version})
    return _candidate_snapshot()[candidate_id]

@serialized_candidate
def revert_candidate_written(candidate_id:str,version:str|None=None):
    current=_candidate_snapshot().get(candidate_id)
    if not current or current["status"]!="written": raise ValueError("候选不存在或未处于已写入状态")
    with _lock:
        _append_jsonl(_workspace_path('CANDIDATE_EVENTS'),{"event":"reverted","at":utcnow(),"candidate_id":candidate_id})
        _audit("candidate_written_reverted",candidate_id,{"version":version})
    return _candidate_snapshot()[candidate_id]

@serialized_candidate
def mark_candidate_reorganized(candidate_id,resolution,expected_fingerprint):
    current=_candidate_snapshot().get(candidate_id)
    if current and current.get('status')=='reorganized' and (current.get('reorganization') or {}).get('job_id')==resolution['job_id']:return current
    if not current or current['status'] not in {'pending','approved_staging'} or _digest(json.dumps(current,sort_keys=True))!=expected_fingerprint:raise ValueError('旧建议领取版本已变化，保留用户后续决定')
    with _lock:
        _append_jsonl(_workspace_path('CANDIDATE_EVENTS'),{'event':'reorganized','at':utcnow(),'candidate_id':candidate_id,'resolution':resolution})
        _audit('candidate_reorganized',candidate_id,resolution)
    return _candidate_snapshot()[candidate_id]

def candidate_source(candidate_id:str):
    candidate=_candidate_snapshot().get(candidate_id)
    if not candidate: raise ValueError("候选不存在")
    return candidate

def memory_apply_dry_run(candidate_id:str):
    candidate=candidate_source(candidate_id)
    if candidate["status"]!="approved_staging": raise ValueError("只有approved staging可预演")
    p=candidate.get('provenance') or {}
    if p.get('target_path'):
        from .organizer import evidence_checked
        from .production_writer import preview
        evidence_checked(_workspace(),candidate['evidence'])
        if p.get('expected_target_hash') is None:raise ValueError('请重新读取目标版本并提交候选，再生成修改预览')
        return preview(p['target_path'],candidate['title'],candidate['content'],candidate['evidence'],candidate.get('supersedes'),mode='production',action=p.get('proposed_action','create'),expected_hash=p['expected_target_hash'])|{'dry_run':True,'candidate_id':candidate_id}
    type_to_target={"user_profile":_workspace_root()/"00_系统"/"我的长期画像.md","collaboration_rule":_workspace_root()/"00_系统"/"AI协作规则.md","ai_failure":_workspace_root()/"00_系统"/"AI错误与教训.md","preference":_workspace_root()/"00_系统"/"我的长期画像.md","priority":_workspace_root()/"01_当前状态"/"当前优先事项.md","project":_workspace_root()/"00_系统"/"项目总索引.md","knowledge":_workspace_root()/"03_知识"/"待归档知识.md","decision":_workspace_root()/"01_当前状态"/"最近关键决策.md"}
    if candidate["type"]=="project_state":
        if not candidate.get("project"): raise ValueError("项目状态候选缺少project")
        target=_workspace_root()/"02_项目"/candidate["project"]/"当前状态.md"
    else: target=type_to_target[candidate["type"]]
    old=target.read_text("utf-8") if target.exists() else ""
    addition=f"\n\n## {candidate['title']}\n\n{candidate['content']}\n"
    new=old.rstrip()+addition
    diff="\n".join(difflib.unified_diff(old.splitlines(),new.splitlines(),fromfile=str(target),tofile=str(target)+"（预演）",lineterm=""))
    return {"dry_run":True,"candidate_id":candidate_id,"target":str(target),"original":old,"proposed":new,"diff":diff,"status_change":{"from":"approved_staging","to":"would_apply"},"evidence":candidate["evidence"],"supersedes":candidate["supersedes"]}

def build_attachment_manifest(output:Path|None=None):
    source_manifest=_workspace_root()/"90_原始档案"/"原始聊天记忆"/"落地项目"/"manifest.json"
    if not source_manifest.exists():
        return {"generated_at":None,"reference_count":0,"unique_reference_count":0,"attachment_file_count":0,"mapped_reference_count":0,"missing_reference_count":0,"duplicate_reference_count":0,"orphan_file_count":0,"visual_understood_count":0,"missing_refs":[],"duplicates":{},"orphan_files":[],"files":[]}
    data=json.loads(source_manifest.read_text("utf-8")); refs=[]
    for message in data.get("messages",[]): refs.extend(message.get("image_refs") or [])
    attachment_dir=source_manifest.parent/"01_图片附件"; files=[p for p in attachment_dir.rglob("*") if p.is_file()] if attachment_dir.exists() else []
    by_id={}
    for path in files:
        match=re.search(r"IMG_\d{6}",path.stem,re.I)
        if match: by_id.setdefault(match.group(0).upper(),[]).append(path)
    unique_refs=sorted(set(refs)); mapped={key:paths for key,paths in by_id.items() if key in unique_refs}; missing=[x for x in unique_refs if x not in mapped]
    orphan=[p for key,paths in by_id.items() if key not in unique_refs for p in paths]; duplicates={key:paths for key,paths in mapped.items() if len(paths)>1}
    file_rows=[{"path":p.relative_to(attachment_dir).as_posix(),"image_ref":next(iter(re.findall(r"IMG_\d{6}",p.stem,re.I)),None),"extension":p.suffix.lower(),"sha256":hashlib.sha256(p.read_bytes()).hexdigest(),"bytes":p.stat().st_size} for p in files]
    result={"generated_at":utcnow(),"reference_count":len(refs),"unique_reference_count":len(unique_refs),"attachment_file_count":len(files),"mapped_reference_count":len(mapped),"missing_reference_count":len(missing),"duplicate_reference_count":len(duplicates),"orphan_file_count":len(orphan),"visual_understood_count":0,"missing_refs":missing,"duplicates":{k:[p.relative_to(attachment_dir).as_posix() for p in v] for k,v in duplicates.items()},"orphan_files":[p.relative_to(attachment_dir).as_posix() for p in orphan],"files":file_rows}
    target=output or _workspace_path('ATTACHMENT_MANIFEST'); target.parent.mkdir(parents=True,exist_ok=True); target.write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n","utf-8")
    return result
