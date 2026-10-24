from __future__ import annotations

import difflib
import hashlib
import json
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from .core import ROOT, VAULT, overview, search
from .memory_pipeline import list_candidates, build_attachment_manifest
from .workspace import active_workspace, local_path

DRAFT=active_workspace().queues/"memory-draft"
INTERFACE_STATE=local_path("memory_interfaces.json")
BACKUPS=active_workspace().versions/"project-backups"
MANIFEST=VAULT/"90_原始档案"/"原始聊天记忆"/"落地项目"/"manifest.json"
RAW_SOURCE=VAULT/"90_原始档案"/"原始聊天记忆"/"落地项目"/"00_原始文件"/"落地项目_RAW_SOURCE.txt"
PROJECT_ROOT=VAULT/"02_项目"
STATUS_ROOT=VAULT/"01_当前状态"
READ_INTERFACES=("memory_bootstrap","get_project_state","search_memory","get_raw_message")
WRITE_INTERFACES=("save_raw_conversation","create_candidate","submit_review","modify_profile","modify_project_state","modify_collaboration_rule","modify_ai_failure","delete_memory")
INTERFACES=READ_INTERFACES+WRITE_INTERFACES
FORMAL_WRITE_INTERFACES={"modify_profile","modify_project_state","modify_collaboration_rule","modify_ai_failure","delete_memory"}
CLIENT_IDS=("chatgpt","codex","hermes","other")
STATUSES=("已实现","进行中","已决定未开始","仅方向/想法","暂停","否决","被替代","待确认")
_pending_diffs={}


def now(): return datetime.now(timezone.utc).isoformat()


def _default_interface_state():
    safe_tools={name:name not in FORMAL_WRITE_INTERFACES for name in INTERFACES}
    clients={client:{"enabled":client=="chatgpt","tools":dict(safe_tools)} for client in CLIENT_IDS}
    return {"mcp_enabled":False,"memory_writer_enabled":False,"clients":clients,"interfaces":{
      name:{"enabled":name in READ_INTERFACES or name in {"save_raw_conversation","create_candidate","submit_review"},"permission":"只读" if name in READ_INTERFACES else "受控写入","last_called":None,"call_count":0,"last_error":None} for name in INTERFACES}}


def interface_state():
    if not INTERFACE_STATE.exists(): return _default_interface_state()
    try: data=json.loads(INTERFACE_STATE.read_text("utf-8"))
    except (OSError,ValueError): return _default_interface_state()
    base=_default_interface_state(); base.update({k:v for k,v in data.items() if k!="interfaces"})
    for name in INTERFACES: base["interfaces"][name].update(data.get("interfaces",{}).get(name,{}))
    for client in CLIENT_IDS:
        saved=data.get("clients",{}).get(client,{})
        base["clients"].setdefault(client,{"enabled":False,"tools":{name:name not in FORMAL_WRITE_INTERFACES for name in INTERFACES}})
        base["clients"][client]["enabled"]=bool(saved.get("enabled",base["clients"][client]["enabled"]))
        base["clients"][client]["tools"].update(saved.get("tools",{}))
    for client,saved in data.get("clients",{}).items():
        if client in base["clients"]: continue
        base["clients"][client]={"enabled":bool(saved.get("enabled",True)),"tools":{name:name not in FORMAL_WRITE_INTERFACES for name in INTERFACES}}
        base["clients"][client]["tools"].update(saved.get("tools",{}))
    base["memory_writer_enabled"]=bool(data.get("memory_writer_enabled",False))
    return base


def save_interface_state(data):
    INTERFACE_STATE.parent.mkdir(parents=True,exist_ok=True)
    INTERFACE_STATE.write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\n","utf-8")


def update_interface(name:str,enabled:bool,owner_confirmed:bool=False):
    data=interface_state()
    if name=="mcp": data["mcp_enabled"]=bool(enabled)
    elif name=="memory_writer":
        if enabled and not owner_confirmed:raise PermissionError("正式记忆自动应用需要Owner二次确认")
        data["memory_writer_enabled"]=bool(enabled)
    elif name in INTERFACES:
        if name in FORMAL_WRITE_INTERFACES and enabled and not owner_confirmed:raise PermissionError("高风险正式写入能力需要Owner二次确认")
        data["interfaces"][name]["enabled"]=bool(enabled)
    else: raise ValueError("未知记忆接口")
    save_interface_state(data); return data

def owner_capabilities():
    data=interface_state()
    from .provider_config import status as provider_status
    provider=provider_status()
    return {"raw_archive":{"enabled":data["interfaces"]["save_raw_conversation"]["enabled"],"available":True,"production_ready":True},"candidate_extraction":{"enabled":data["interfaces"]["create_candidate"]["enabled"],"available":True,"provider":provider,"status":"权限已开放 · "+("尚未配置生产提炼器" if provider["state"]=="未配置" else provider["state"])},"manual_writer":{"available":True,"requires_review":True,"requires_diff":True,"requires_confirmation":True,"versioned":True,"undo":True},"automatic_writer":{"enabled":data["memory_writer_enabled"],"available":True,"provider_ready":provider["state"]=="已配置","status":"已授权 · 等待生产提炼器" if data["memory_writer_enabled"] and provider["state"]!="已配置" else "关闭" if not data["memory_writer_enabled"] else "已就绪","requires_confirmation":True},"weight_boost":{"available":True,"formal_core":False,"test_mode":True,"status":"权限已开放 · 正式核心背景待确认"}}


def update_client_permission(client_id:str,enabled:bool,tool:str|None=None):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,63}",client_id): raise ValueError("客户端ID格式无效")
    data=interface_state()
    data["clients"].setdefault(client_id,{"enabled":False,"tools":{name:True for name in INTERFACES}})
    if tool is None: data["clients"][client_id]["enabled"]=bool(enabled)
    elif tool in INTERFACES: data["clients"][client_id]["tools"][tool]=bool(enabled)
    else: raise ValueError("未知记忆接口")
    save_interface_state(data); return data


def record_call(name:str,error:str|None=None):
    data=interface_state(); item=data["interfaces"].get(name)
    if item:
        item["last_called"]=now(); item["call_count"]=int(item.get("call_count",0))+1; item["last_error"]=error
        save_interface_state(data)


def require_interface(name:str,require_mcp:bool=False,client_id:str|None=None):
    data=interface_state()
    if require_mcp and data["mcp_enabled"]:
        client_id=client_id or "other"; client=data["clients"].get(client_id)
        if not client or not client.get("enabled"):
            record_call(name,f"客户端 {client_id} 访问关闭"); raise PermissionError(f"客户端 {client_id} 访问关闭")
        if not client.get("tools",{}).get(name,False):
            record_call(name,"该能力未授权"); raise PermissionError("该能力未授权")
    if require_mcp and not data["mcp_enabled"]:
        record_call(name,"MCP记忆接口总开关未启用"); raise PermissionError("MCP记忆接口总开关未启用")
    if not data["interfaces"].get(name,{}).get("enabled"):
        record_call(name,f"接口 {name} 已停用"); raise PermissionError(f"接口 {name} 已停用")


def _file_info(path:Path):
    if not path.exists(): return {"status":"未就绪","characters":0,"estimated_tokens":0,"updated_at":None,"source":str(path),"content":""}
    text=path.read_text("utf-8")
    return {"status":"草稿","characters":len(text),"estimated_tokens":round(len(text)*.85),"updated_at":datetime.fromtimestamp(path.stat().st_mtime,timezone.utc).isoformat(),"source":str(path),"content":text}


def bootstrap_documents():
    from .core_bootstrap import current
    try:
        core=current();formal={"status":core["status"],"characters":core["characters"],"estimated_tokens":core["estimated_tokens"],"updated_at":core["compiled_at"],"source":"、".join(x["path"] for x in core["source_files"]),"content":core["content"],"version":core["version"]}
    except ValueError:
        formal={"status":"未初始化","characters":0,"estimated_tokens":0,"updated_at":None,"source":"尚未配置核心背景文件","content":"","version":None}
    return {"stable_core":formal,"live_context":{"status":"不属于Core","characters":0,"estimated_tokens":0,"updated_at":None,"source":"项目状态按需读取","content":""},"bootstrap":formal}


def projects():
    result=[]
    if not PROJECT_ROOT.exists(): return result
    for state_path in sorted(PROJECT_ROOT.glob("*/当前状态.md")):
        if _frontmatter(state_path.read_text('utf-8')).get('document_role') == '旧路径兼容指引':
            continue
        detail=project_detail(state_path.parent.name)
        result.append({k:detail[k] for k in ("project","status","summary","updated_at","source")})
    return result


def _field(text:str,name:str):
    match=re.search(rf"(?m)^-\s*{re.escape(name)}[：:]\s*(.*?)\s*$",text)
    return match.group(1).strip() if match else "暂无数据"


def _frontmatter(text:str):
    result={}; match=re.match(r"\A---\s*\n(.*?)\n---\s*\n",text,re.S)
    if match:
        for line in match.group(1).splitlines():
            if ":" in line:
                key,value=line.split(":",1); result[key.strip()]=value.strip().strip("\"'")
    return result


def project_detail(project:str):
    from .knowledge_entities import KnowledgeEntities
    from .workspace import active_workspace
    entity=KnowledgeEntities(active_workspace()).detail(project)
    if entity:return entity
    folder=(PROJECT_ROOT/project).resolve()
    if folder.parent!=PROJECT_ROOT.resolve() or not folder.exists(): raise ValueError("项目不存在")
    overview_path=folder/"项目总览.md"; state_path=folder/"当前状态.md"
    overview_text=overview_path.read_text("utf-8") if overview_path.exists() else ""
    state_text=state_path.read_text("utf-8") if state_path.exists() else ""
    fm=_frontmatter(state_text); status=fm.get("status",_field(state_text,"状态")); summary=fm.get("summary",_field(state_text,"当前状态"))
    return {"project":project,"status":status,"summary":summary,"overview":overview_text,"current_state":state_text,
      "implemented":_field(state_text,"当前已实现"),"unfinished":_field(state_text,"当前未完成"),"blocked":_field(state_text,"当前阻塞"),"next_step":_field(state_text,"下一步"),
      "updated_at":fm.get("updated") or (datetime.fromtimestamp(state_path.stat().st_mtime,timezone.utc).isoformat() if state_path.exists() else None),
      "source":state_path.relative_to(VAULT).as_posix(),"overview_source":overview_path.relative_to(VAULT).as_posix()}


def preview_project_update(project:str,new_text:str):
    detail=project_detail(project)
    if detail.get('entity_projection'):raise ValueError('这是统一事项的元数据视图；请通过原知识页的审核写入修改历史正文，不覆盖事项映射')
    path=VAULT/detail["source"]
    old=path.read_text("utf-8"); new_text=new_text.replace("\r\n","\n").rstrip()+"\n"
    token=hashlib.sha256((project+old+new_text+now()).encode()).hexdigest()[:24]
    diff="\n".join(difflib.unified_diff(old.splitlines(),new_text.splitlines(),fromfile="当前版本",tofile="拟保存版本",lineterm=""))
    _pending_diffs[token]={"project":project,"path":path,"old_hash":hashlib.sha256(old.encode()).hexdigest(),"new_text":new_text}
    return {"token":token,"changed":old!=new_text,"diff":diff}


def confirm_project_update(token:str):
    item=_pending_diffs.pop(token,None)
    if not item: raise ValueError("Diff确认已失效，请重新预览")
    current=item["path"].read_text("utf-8")
    if hashlib.sha256(current.encode()).hexdigest()!=item["old_hash"]: raise ValueError("文件已发生变化，请重新预览")
    BACKUPS.mkdir(parents=True,exist_ok=True); stamp=datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup=BACKUPS/f"{item['project']}-{stamp}-当前状态.md"; shutil.copy2(item["path"],backup)
    item["path"].write_text(item["new_text"],"utf-8")
    return {"saved":True,"backup":str(backup),"source":item["path"].relative_to(VAULT).as_posix()}


def raw_message(message_id:str,session_id:str|None=None):
    message_id=str(message_id).strip()
    from .session_archive import read_message
    try:return read_message(message_id,session_id)
    except ValueError as error:
        if session_id or str(error)!="MESSAGE不存在":raise
    if not RAW_SOURCE.exists(): raise ValueError("MESSAGE不存在")
    # Six-digit numbering belongs only to the legacy flat export. Source IDs
    # in the canonical archive (including Hermes numeric IDs) are exact.
    message_id=message_id.zfill(6) if message_id.isdigit() else message_id
    text=RAW_SOURCE.read_text("utf-8-sig")
    match=re.search(rf"(?ms)^\[MESSAGE {re.escape(message_id)}\]\s*\n\[(USER|ASSISTANT)\]\s*\n(.*?)(?=^\[MESSAGE \d{{6}}\]|\Z)",text)
    if not match: raise ValueError("MESSAGE不存在")
    images=re.findall(r"\[IMAGE:\s*(IMG_\d{6})\]",match.group(2))
    content=match.group(2).rstrip("\r\n");version=hashlib.sha256(content.encode('utf-8')).hexdigest()
    return {"message_id":message_id,"role":match.group(1),"content":content,"image_refs":images,"source":RAW_SOURCE.relative_to(VAULT).as_posix(),"version":version,"source_version":version,"verification":"raw_evidence"}


def memory_status():
    docs=bootstrap_documents(); ov=overview(); manifest=json.loads(MANIFEST.read_text("utf-8")) if MANIFEST.exists() else {}
    project_rows=projects(); interfaces=interface_state()
    return {"layers":{
      "l0":{"status":"就绪" if manifest.get("validation",{}).get("source_sha256_unchanged") else "未就绪","message_count":manifest.get("total_messages"),"source":manifest.get("source_file"),"updated_at":datetime.fromtimestamp(MANIFEST.stat().st_mtime,timezone.utc).isoformat() if MANIFEST.exists() else None},
      "stable_core":{"status":docs["stable_core"]["status"],"updated_at":docs["stable_core"]["updated_at"]},
      "live_context":{"status":docs["live_context"]["status"],"updated_at":docs["live_context"]["updated_at"]},
      "bootstrap":{"status":docs["bootstrap"]["status"],"updated_at":docs["bootstrap"]["updated_at"]},
      "mcp":{"status":"已启用" if interfaces["mcp_enabled"] else "未启用"},
      "memory_writer":{"status":"未启用"}},
      "metrics":{"message_count":manifest.get("total_messages"),"chunk_count":ov["chunk_count"],"vector_count":ov["vector_count"],"project_count":len(project_rows),"bootstrap_characters":docs["bootstrap"]["characters"],"bootstrap_estimated_tokens":docs["bootstrap"]["estimated_tokens"],"updated_at":docs["bootstrap"]["updated_at"]},
      "interfaces":interfaces,"pending_memory":{"count":len(list_candidates("pending")),"status":"自动记忆写入尚未启用"},"attachments":build_attachment_manifest()}


def _markdown_section(text:str, heading:str):
    match=re.search(rf"(?ms)^##+\s+{re.escape(heading)}\s*$\n(.*?)(?=^##+\s+|\Z)",text)
    return match.group(1).strip() if match else ""


def _meaningful_lines(text:str,limit:int=12):
    lines=[]
    for raw in text.splitlines():
        line=re.sub(r"^\s*(?:[-*]|\d+[.)])\s*","",raw).strip()
        if line and not line.startswith(("#","---",">")) and line not in lines:
            lines.append(line)
        if len(lines)>=limit: break
    return lines


def memory_awareness():
    """Return only facts that already exist in the current memory sources."""
    docs=bootstrap_documents()
    from .core_bootstrap import current
    core=current();source_docs={Path(x["path"]).stem:(VAULT/x["path"]).read_text("utf-8") for x in core["source_files"]}

    def first_section(document:str,names:tuple[str,...],limit:int):
        for name in names:
            section=_markdown_section(document,name)
            if section: return _meaningful_lines(section,limit)
        return []

    known={"user_profile":_meaningful_lines(source_docs["我的长期画像"],60),"collaboration_rules":_meaningful_lines(source_docs["AI协作规则"],80),"ai_lessons":_meaningful_lines(source_docs["AI错误与教训"],100),"knowledge_principles":_meaningful_lines(source_docs["知识库规则"],80),"sources":{"user_profile":"00_系统/我的长期画像.md","collaboration_rules":"00_系统/AI协作规则.md","ai_lessons":"00_系统/AI错误与教训.md","knowledge_principles":"00_系统/知识库规则.md","bootstrap":docs["bootstrap"]["source"]}}

    gaps=[]
    project_rows=projects();pending=[p for p in project_rows if p["status"]=="待确认"]
    if pending: gaps.append({"type":"待确认状态","detail":"、".join(p["project"] for p in pending),"count":len(pending)})
    project_dirs=[p for p in PROJECT_ROOT.iterdir() if p.is_dir()] if PROJECT_ROOT.exists() else []
    missing=[p.name for p in project_dirs if not (p/"当前状态.md").exists()]
    if missing: gaps.append({"type":"缺少项目当前状态","detail":"、".join(missing),"count":len(missing)})
    manifest=json.loads(MANIFEST.read_text("utf-8")) if MANIFEST.exists() else {}
    refs=manifest.get("image_refs") or manifest.get("image_numbers") or []
    ref_count=manifest.get("image_reference_count") or manifest.get("total_image_refs") or len(refs)
    attachment_dir=MANIFEST.parent/"01_图片附件"
    attachment_count=sum(1 for p in attachment_dir.rglob("*") if p.is_file()) if attachment_dir.exists() else 0
    if ref_count:
        gaps.append({"type":"图片引用尚无视觉理解","detail":f"原始聊天记录了 {ref_count} 个图片引用；附件目录现有 {attachment_count} 个文件，当前系统未执行图片视觉理解。","count":ref_count})
    gaps.append({"type":"自动记忆写入尚未开放","detail":"新记忆不会自动写回知识财富，仍需人工确认流程。","count":0})
    return {"known":known,"unknown":gaps,"bootstrap":{"status":docs["bootstrap"]["status"],"characters":docs["bootstrap"]["characters"],"source":docs["bootstrap"]["source"],"has_content":bool(core["content"].strip())}}


def _timed_call(calls,name,fn):
    started=time.perf_counter(); value=fn(); elapsed=round((time.perf_counter()-started)*1000,1)
    calls.append({"interface":name,"status":"成功","duration_ms":elapsed}); return value


def _route_memory_intent(query:str,project_names:list[str]):
    q=query.lower(); project=next((name for name in project_names if name.lower() in q),None)
    if project:return "project",project
    if any(x in q for x in ("我是谁","我的身份","了解我","长期画像","用户画像")):return "identity",None
    if any(x in q for x in ("怎么和ai","如何和ai","怎样和ai","协作方式","合作方式","ai合作")):return "collaboration",None
    if any(x in q for x in ("死磕","失败路线","连续失败","ai错误","教训")):return "core_lesson",None
    if any(x in q for x in ("以前","当时","后来","过去","为什么决定","为什么放弃","历史")):return "history",None
    return "knowledge",None


def _core_answer(intent:str,query:str):
    """Quote only current configured Core evidence; never ship an owner's biography."""
    from .core_bootstrap import current
    import hashlib
    subjects={"identity":("我的长期画像","用户画像"),
              "collaboration":("AI协作规则","协作规则"),
              "core_lesson":("AI错误与教训","协作教训")}
    wanted,label=subjects.get(intent,("","相关资料"))
    unknown=f"当前已授权的核心资料中没有可确认的{label}，我还不知道。"
    try:
        rows=current().get("source_files",[])
    except (OSError,ValueError):
        return unknown,[]
    root=VAULT.resolve()
    for item in rows:
        relative=item.get("path","")
        if Path(relative).stem!=wanted:continue
        try:
            path=(root/relative).resolve()
            path.relative_to(root)
            raw=path.read_bytes()
            if hashlib.sha256(raw).hexdigest()!=item.get("sha256"):continue
            text=raw.decode("utf-8-sig")
        except (OSError,ValueError,UnicodeError):continue
        lines=_meaningful_lines(text,12)
        if lines:return f"根据当前已授权的{label}："+"；".join(lines),[relative]
    return unknown,[]


def memory_chain(query:str):
    started=time.perf_counter(); calls=[]; timings={}; results=[]; raw=None; detail=None; sources=[]
    try:
        t=time.perf_counter(); project_names=[x["project"] for x in projects()]; intent,project_name=_route_memory_intent(query,project_names);timings["intent/router"]=round((time.perf_counter()-t)*1000,1)
        calls.append({"interface":"intent/router","status":"成功","hit":intent,"duration_ms":timings["intent/router"]})
        t=time.perf_counter();require_interface("memory_bootstrap");docs=bootstrap_documents();record_call("memory_bootstrap");timings["memory_bootstrap"]=round((time.perf_counter()-t)*1000,1)
        calls.append({"interface":"memory_bootstrap","status":"成功","hit":"正式Core","duration_ms":timings["memory_bootstrap"]})
        if intent in {"identity","collaboration","core_lesson"}:
            t=time.perf_counter();answer,sources=_core_answer(intent,query);timings["final_answer"]=round((time.perf_counter()-t)*1000,1)
            calls.extend([{"interface":"get_project_state","status":"短路","hit":"Core已足够"},{"interface":"search_memory","status":"短路","hit":"Core已足够"},{"interface":"get_raw_message","status":"短路","hit":"未进入L0"}])
        elif intent=="project":
            t=time.perf_counter();require_interface("get_project_state");detail=project_detail(project_name);record_call("get_project_state");timings["get_project_state"]=round((time.perf_counter()-t)*1000,1)
            calls.append({"interface":"get_project_state","status":"成功","hit":project_name,"source":detail["source"],"duration_ms":timings["get_project_state"]})
            calls.extend([{"interface":"search_memory","status":"短路","hit":"正式项目状态已足够"},{"interface":"get_raw_message","status":"短路","hit":"未进入L0"}]);sources=[detail["source"]]
            t=time.perf_counter();answer=f"{project_name}当前状态是“{detail['status']}”。{detail['summary'] or detail['current_state'][:240]}";timings["final_answer"]=round((time.perf_counter()-t)*1000,1)
        else:
            calls.append({"interface":"get_project_state","status":"跳过","hit":"未识别明确项目"})
            t=time.perf_counter();require_interface("search_memory");results=search(query,"hybrid",8);record_call("search_memory");timings["search_memory"]=round((time.perf_counter()-t)*1000,1)
            calls.append({"interface":"search_memory","status":"成功","hit_count":len(results),"top_hit":results[0]["source_path"] if results else None,"duration_ms":timings["search_memory"]})
            raw_hit=next((x for x in results if x.get("message_id")),None)
            if intent=="history" and raw_hit:
                t=time.perf_counter();require_interface("get_raw_message");raw=raw_message(raw_hit["message_id"]);record_call("get_raw_message");timings["get_raw_message"]=round((time.perf_counter()-t)*1000,1)
                calls.append({"interface":"get_raw_message","status":"成功","hit":raw["message_id"],"role":raw["role"],"source":raw["source"],"duration_ms":timings["get_raw_message"]})
            else:calls.append({"interface":"get_raw_message","status":"短路","hit":"当前证据已足够"})
            sources=list(dict.fromkeys(x.get("source_path") for x in results[:3] if x.get("source_path")))
            t=time.perf_counter();answer=("根据现有记忆，"+"；".join(re.sub(r"\s+"," ",x.get("content","")).strip()[:180] for x in results[:2])+"。") if results else "当前正式记忆和检索索引中没有找到足够证据回答这个问题。";timings["final_answer"]=round((time.perf_counter()-t)*1000,1)
        timings["total"]=round((time.perf_counter()-started)*1000,1)
        return {"query":query,"intent":intent,"answer":answer,"sources":sources,"bootstrap":{"status":docs["bootstrap"]["status"],"characters":docs["bootstrap"]["characters"]},"project":detail,"results":results,"raw_message":raw,"calls":calls,"timings":timings}
    except Exception as exc:
        if calls:calls.append({"interface":"链路","status":"失败","error":str(exc)})
        raise
