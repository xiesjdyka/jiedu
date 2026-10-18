"""Loopback-only protocol adapter for a future ChatGPT Remote MCP gateway."""
from __future__ import annotations

import json
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

from .ai_access import context_state, knowledge_search, request_change
from .core import ROOT
from .workspace import active_workspace
from .mcp_audit import audited, client_summary
from .memory_control import interface_state,project_detail,record_call,require_interface
from .knowledge_base import load as load_knowledge_base, markdown_files

CLIENT_ID = "chatgpt"
STATE_PATH = active_workspace().sync / "chatgpt-adapter.json"
TOOLS = {
    "knowledge_search": {"description": "只读搜索当前知识库；不修改知识。", "permission": "search_memory", "input_schema": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}, "include_raw": {"type": "boolean", "default": False}}, "required": ["query"], "additionalProperties": False}},
    "context_state": {"description": "读取知识库、当前状态与指定项目上下文。", "permission": "get_project_state", "input_schema": {"type": "object", "properties": {"project": {"type": ["string", "null"], "default": None}}, "additionalProperties": False}},
    "request_knowledge_change": {"description": "只创建待Owner确认的知识变更请求，不直接写入Markdown。", "permission": "create_candidate", "input_schema": {"type": "object", "properties": {"target_path": {"type": "string"}, "proposed_content": {"type": "string"}, "reason": {"type": "string"}, "evidence": {"type": "array", "items": {"type": "object"}, "default": []}}, "required": ["target_path", "proposed_content", "reason"], "additionalProperties": False}},
}

def _now(): return datetime.now(timezone.utc).isoformat()
def _load():
    try:return json.loads(STATE_PATH.read_text("utf-8"))
    except (OSError,ValueError):return {}
def _save(update):
    state={**_load(),**update};STATE_PATH.parent.mkdir(parents=True,exist_ok=True);temp=STATE_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(state,ensure_ascii=False,indent=2)+"\n","utf-8");temp.replace(STATE_PATH);return state

def status(console_port=8765):
    state=_load();audit=client_summary(CLIENT_ID);bridge_audit=client_summary("chatgpt-bridge")
    connected=bool(state.get("chatgpt_handshake"))
    return {"adapter_status":"available","transport":"localhost_http","endpoint":f"http://127.0.0.1:{console_port}/api/chatgpt-adapter","chatgpt_handshake":state.get("chatgpt_handshake"),"tool_discovery":state.get("chatgpt_tool_discovery"),"bound_session":state.get("bound_session"),"last_call":audit.get("last_mcp_call") if connected else None,"last_tool":audit.get("last_mcp_tool") if connected else None,"last_result":audit.get("last_mcp_outcome") if connected else None,"last_error":audit.get("last_mcp_error") if connected else None,"bridge_last_call":state.get("last_bridge_request") or bridge_audit.get("last_mcp_call"),"bridge_last_source":state.get("last_bridge_source"),"bridge_last_client":state.get("last_bridge_client"),"bridge_last_tool":bridge_audit.get("last_mcp_tool"),"bridge_last_result":bridge_audit.get("last_mcp_outcome"),"bridge_request_count":state.get("bridge_request_count",0),"remote_gateway":"not_configured","chatgpt_direct_local_support":False}

def discover(client_product="local-adapter-test",session_id=None):
    _save({"last_probe":_now(),"last_probe_product":client_product})
    return {"server":"界渡","transport":"localhost_http","tools":[{"name":name,**spec} for name,spec in TOOLS.items()]}

def call(tool,arguments,client_product="local-adapter-test",session_id=None,access_client_id=CLIENT_ID):
    if tool not in TOOLS:raise ValueError("未知工具")
    if tool=='request_knowledge_change':raise PermissionError('知识候选请通过已配对 MCP 的 request_knowledge_change 提交；旧兼容桥不接受写请求')
    permission=TOOLS[tool]["permission"];product=client_product.casefold();audit_client=CLIENT_ID if product=="chatgpt" else "chatgpt-bridge" if product=="chatgpt-bridge" else "chatgpt-adapter-test"
    with audited(audit_client,tool,arguments):
        require_interface(permission,True,access_client_id);record_call(permission)
        if tool=="knowledge_search":
            if arguments.get("include_raw",False):
                raise PermissionError("原始证据需要已认证客户端和一次性 Owner 确认")
            query=arguments.get("query","");result=knowledge_search(query,arguments.get("limit",10),False)
            if "项目" in query:
                base=Path(load_knowledge_base()["vault_path"]);result["projects"]=sorted({relative.split("/")[1] for relative in markdown_files(base) if relative.startswith("02_项目/") and len(relative.split("/"))>2})
        elif tool=="context_state":result=context_state(arguments.get("project"))
        else:result=request_change(CLIENT_ID,arguments.get("target_path",""),arguments.get("proposed_content",""),arguments.get("reason",""),arguments.get("evidence"))
    _save({"last_probe":_now(),"last_probe_product":client_product});return {"ok":True,"tool":tool,"result":result}

def _projects() -> list[str]:
    base=Path(load_knowledge_base()["vault_path"])
    return sorted({relative.split("/")[1] for relative in markdown_files(base)
                   if relative.startswith("02_项目/") and len(relative.split("/"))>2})

def _detect_project(query:str,requested:str|None=None) -> str|None:
    if requested and requested.strip():return requested.strip()
    matches=[name for name in _projects() if name.casefold() in query.casefold()]
    return max(matches,key=len) if matches else None

def _user_context(client_id:str) -> dict:
    with audited("chatgpt-bridge","memory_bootstrap",{}):
        require_interface("memory_bootstrap",True,client_id);record_call("memory_bootstrap")
        config=load_knowledge_base();base=Path(config["vault_path"]).resolve();rows=[]
        for relative in config.get("core_files",[]):
            path=(base/relative).resolve()
            try:path.relative_to(base)
            except ValueError:continue
            if not path.is_file():continue
            raw=path.read_bytes();rows.append({"path":relative,"sha256":hashlib.sha256(raw).hexdigest(),"content":raw.decode("utf-8-sig")})
    return {"configured_files":len(config.get("core_files",[])),"available_files":len(rows),"documents":rows,"read_only":True}

def _permission_status(client_id:str) -> dict:
    state=interface_state();client=state.get("clients",{}).get(client_id,{"enabled":False,"tools":{}});tools=client.get("tools",{})
    def effective(name):return bool(state.get("mcp_enabled") and client.get("enabled") and tools.get(name) and state.get("interfaces",{}).get(name,{}).get("enabled"))
    return {"client_id":client_id,"global_access":bool(state.get("mcp_enabled")),"client_access":bool(client.get("enabled")),"capabilities":{"user_context":effective("memory_bootstrap"),"project_state":effective("get_project_state"),"knowledge_search":effective("search_memory"),"raw_archive":effective("get_raw_message"),"change_request":effective("create_candidate")},"formal_write":False,"change_policy":"只能提交待Owner确认的变更请求"}

def context_request(query:str,project:str|None=None,session_id:str|None=None,source:str="external-local-client",client_id:str=CLIENT_ID) -> dict:
    """Build one permission-checked, audited context response for an AI turn."""
    query=query.strip();client_id=client_id.strip().lower()
    if not query:raise ValueError("上下文请求不能为空")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,63}",client_id):raise ValueError("client_id格式无效")
    selected_project=_detect_project(query,project)
    user_context=_user_context(client_id)
    found=call("knowledge_search",{"query":selected_project or query,"limit":10},"chatgpt-bridge",session_id,access_client_id=client_id)["result"]
    current=call("context_state",{"project":selected_project},"chatgpt-bridge",session_id,access_client_id=client_id)["result"]
    project_state=None
    if selected_project:
        project_state=project_detail(selected_project)
    state=_load();count=int(state.get("bridge_request_count",0))+1;called_at=_now()
    tools=["memory_bootstrap","context_state","knowledge_search"]+(["get_project_state"] if selected_project else [])
    _save({"bridge_request_count":count,"last_bridge_request":called_at,"last_bridge_source":source,"last_bridge_client":client_id,"last_bridge_session":session_id,"last_bridge_query":query,"last_bridge_result_count":found.get("returned",0)})
    return {"bridge_status":"ready","request_id":f"bridge-{count}","session_id":session_id,"query":query,"context":{"user":user_context,"project":project_state,"state":current,"related_knowledge":found,"permissions":_permission_status(client_id)},"audit":{"source":source,"client_id":client_id,"requested_at":called_at,"tools":tools,"request_count":count},"instructions":"将context作为本轮知识依据；不得把未命中内容猜成事实。正式知识变化只能提交变更请求并等待Owner确认。","formal_write_performed":False}

def bridge_context(client_id:str,query:str,session_id:str|None=None) -> dict:
    """Canonical read-only Bridge response for any permissioned AI client."""
    result=context_request(query,None,session_id,"bridge-context",client_id)
    context=result["context"];search=context["related_knowledge"];project=context.get("project")
    project_names=[]
    if project and project.get("project"):project_names=[project["project"]]
    elif project and project.get("name"):project_names=[project["name"]]
    elif "项目" in query:project_names=_projects()
    else:
        for row in search.get("results",[]):
            parts=str(row.get("source_path","")).split("/")
            if len(parts)>2 and parts[0]=="02_项目" and parts[1] not in project_names:project_names.append(parts[1])
    projects=[]
    for name in project_names:
        try:projects.append(project_detail(name))
        except (OSError,ValueError):projects.append({"project":name,"status":"待确认"})
    sources=[];seen=set()
    for row in search.get("results",[]):
        path=row.get("source_path")
        if path and path not in seen:sources.append({"path":path,"title":row.get("title"),"sha256":row.get("sha256")});seen.add(path)
    for row in context.get("user",{}).get("documents",[]):
        path=row.get("path")
        if path and path not in seen:sources.append({"path":path,"title":Path(path).stem,"sha256":row.get("sha256")});seen.add(path)
    return {"context_state":context["state"],"knowledge":search,"projects":projects,"permissions":context["permissions"],"sources":sources,"audit":result["audit"],"request_id":result["request_id"],"session_id":session_id,"read_only":True,"formal_write_performed":False}

def conversation_request(messages: list[dict], session_id: str | None = None, source: str = "external-local-client") -> dict:
    """Resolve the latest user turn into machine-consumable knowledge context."""
    latest=next((str(item.get("content","")).strip() for item in reversed(messages) if item.get("role")=="user" and str(item.get("content","")).strip()),"")
    if not latest:raise ValueError("对话请求缺少USER消息")
    result=context_request(latest,None,session_id,source,CLIENT_ID)
    # Compatibility for existing local callers while the canonical response is context.
    result["knowledge"]={"search":result["context"]["related_knowledge"],"context_state":result["context"]["state"]}
    return result
