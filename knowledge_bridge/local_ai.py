from __future__ import annotations
import asyncio, json, os, re
from datetime import datetime, timezone
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from .core import CFG, ROOT, VAULT
from .workspace import local_path
from .memory_control import interface_state
from .bridge_settings import load_settings
from .session_manager import list_sessions, session_status
from .client_config import statuses as config_statuses, preview as config_preview, apply as config_apply, rollback as config_rollback, _command
from .mcp_audit import client_summary
from .hermes_session_adapter import status as hermes_session_status
from .chatgpt_adapter import status as chatgpt_adapter_status, TOOLS as CHATGPT_ADAPTER_TOOLS
from .workspace import active_workspace
from .redaction import redact,redact_text

STATE_PATH=local_path("local_ai_clients.json")
def _now(): return datetime.now(timezone.utc).isoformat()
def _state():
    try:return json.loads(STATE_PATH.read_text("utf-8"))
    except (OSError,ValueError):return {"clients":{}}
def _save(data): STATE_PATH.parent.mkdir(parents=True,exist_ok=True); STATE_PATH.write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\n","utf-8")
def client_status():
    permissions=interface_state(); gate=permissions["mcp_enabled"]; runtime=_state().get("clients",{}); settings=load_settings(); configs={x["id"]:x for x in config_statuses()}; rows=[]
    try:
        from .client_access import ClientIdentityRegistry
        paired={x["claimed_id"]:x for x in ClientIdentityRegistry(active_workspace()).list_clients()}
    except Exception:paired={}
    definitions=[("chatgpt","ChatGPT"),("hermes","Hermes Agent"),("codex","Codex"),("other","自定义")]+[(key,value.get("name",key)) for key,value in runtime.items() if value.get("custom")]
    for client,label in definitions:
        cfg=configs.get(client) or {"id":client,"installed":True,"config_state":"本地适配可用" if client=="chatgpt" else "待配置","config_path":None,"configured":False,"credential_present":False}; saved=runtime.get(client,{}); configured=bool(cfg.get("configured")) or bool(saved.get("custom")); connected=configured and bool(saved.get("last_connected")); access=permissions["clients"].get(client,{"enabled":False,"tools":{}})
        if client=="chatgpt":configured=False;connected=False
        if cfg["config_state"] in {"未安装","配置失效","待确认"}: status=cfg["config_state"]
        elif connected and not gate: status="被总开关阻止"
        elif connected and not access["enabled"]: status="已连接 · 当前客户端访问关闭"
        elif connected: status="已连接 · 可以访问"
        elif configured: status="已配置未连接"
        else: status="未配置"
        client_cfg=settings["clients"].get(client,{});mode=client_cfg.get("memory_mode","readonly"); sessions=list_sessions(client); current=session_status(sessions[0]["session_id"]) if sessions else None;audit=client_summary(client)
        adapter=hermes_session_status() if client=="hermes" else None
        if adapter and adapter.get("memory_mode")=="full":mode="full";current=adapter.get("current_session") or current
        full=mode=="full"
        adapter_state=chatgpt_adapter_status(settings["console"]["port"]) if client=="chatgpt" else None
        # Template availability is not a legacy connection. Do not synthesize
        # unused ChatGPT/custom identities into the registered connection list.
        if client in {'chatgpt','other'} and not configured and not saved and not sessions and not (adapter_state or {}).get('chatgpt_handshake'):
            continue
        chatgpt_diag={"mcp_server_status":"已就绪","client_handshake_status":"已握手" if adapter_state.get("chatgpt_handshake") else "未发生","tool_discovery_status":"已发现" if adapter_state.get("tool_discovery") else "本地适配可用","last_real_tool_call":adapter_state.get("last_call") if adapter_state.get("chatgpt_handshake") else None,"bound_session":adapter_state.get("bound_session"),"restart_required":False,"restart_note":"本地适配器无需重启；ChatGPT当前不能直接访问localhost","desktop_support":"当前不支持本地localhost MCP直连","web_support":"本轮不配置网页版或远程入口","probe_at":saved.get("last_probe") or saved.get("last_connected"),"adapter_endpoint":adapter_state.get("endpoint"),"last_result":adapter_state.get("last_result")} if client=="chatgpt" else None
        identity=paired.get(client);identity_status=identity.get("status") if identity else "legacy_unverified"
        if connected and identity_status=="legacy_unverified":status="已连接 · 身份未验证（仅保留现有公开能力）"
        rows.append({**cfg,**({} if client=="chatgpt" else audit),"id":client,"client":label,"status":"ChatGPT已连接" if client=="chatgpt" and adapter_state.get("chatgpt_handshake") else "本地适配可用" if client=="chatgpt" else status,"connection":"localhost HTTP适配" if client=="chatgpt" else settings["mcp"]["mode"],"configured":configured,"connected":bool(adapter_state.get("chatgpt_handshake")) if client=="chatgpt" else connected,"client_access":access["enabled"],"tool_permissions":access["tools"],"knowledge_access_mode":client_cfg.get("knowledge_access_mode","normal"),"identity_status":identity_status,"authenticated_client_id":identity.get("internal_id") if identity else None,"protected_access":"deny" if identity_status!="authenticated" else "policy_controlled","knowledge_access_note":"公开文件权限只约束经界渡的访问，不能阻止同权限程序直接读取普通文件。","isolation_level":"tool_only" if client=="hermes" and client_cfg.get("knowledge_access_mode")=="bridge_only" else "none","filesystem_bypass_possible":client=="hermes","last_connected":adapter_state.get("chatgpt_handshake") if client=="chatgpt" else saved.get("last_connected"),"last_called":adapter_state.get("last_call") if client=="chatgpt" else saved.get("last_called"),"tool_count":len(__import__('knowledge_bridge.chatgpt_adapter',fromlist=['TOOLS']).TOOLS) if client=="chatgpt" else saved.get("tool_count"),"permission":"总开关关闭" if not gate else "客户端访问关闭" if not access["enabled"] else "按工具权限","last_error":adapter_state.get("last_error") if client=="chatgpt" else saved.get("last_error") or (adapter or {}).get("last_error"),"memory_mode":mode,"conversation_sync":full,"raw_archive":full,"turn_statistics":"available" if current and full else "unavailable","token_statistics":current["token_kind"] if current and full else "unavailable","current_session":current,"session_adapter":adapter,"chatgpt_diagnostics":chatgpt_diag})
        if client=="chatgpt":
            rows[-1]["status"]="界渡本地可用 · ChatGPT桥接等待连接"
            rows[-1]["connected"]=False
            rows[-1]["chatgpt_diagnostics"].update({"bridge_last_call":adapter_state.get("bridge_last_call"),"bridge_last_source":adapter_state.get("bridge_last_source"),"bridge_last_tool":adapter_state.get("bridge_last_tool"),"bridge_last_result":adapter_state.get("bridge_last_result"),"bridge_request_count":adapter_state.get("bridge_request_count",0)})
    configured_host=settings["console"]["host"]
    return redact({"master_enabled":gate,"clients":rows,"security":{"knowledge_data":"仅本机","mcp_listen":settings["mcp"]["mode"],"console_listen":f"{configured_host}:{settings['console']['port']}","public_access":"关闭","remote_mcp":"禁用","cloud_sync":"未配置","verified_localhost":configured_host=="127.0.0.1","policy_locked":True}})
async def _probe(client="other",exercise_gate=True):
    entry=_command(client)
    params=StdioServerParameters(command=entry['command'],args=entry['args'],cwd=entry.get('cwd'),env={**os.environ,**entry['env']})
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize(); tools=await session.list_tools(); blocked=False
            if exercise_gate:
                try: blocked=bool((await session.call_tool("memory_bootstrap",{})).is_error)
                except Exception: blocked=True
            return len(tools.tools),blocked
def test_client(client):
    data=_state(); item=data.setdefault("clients",{}).setdefault(client,{})
    try:
        probe_only=client=="chatgpt";count,blocked=asyncio.run(_probe(client,not probe_only));stamp=_now();item.update({"last_probe":stamp,"tool_count":count,"last_error":None})
        if not probe_only:item.update({"last_connected":stamp,"last_called":stamp})
        result={"connected":not probe_only,"probe_ok":True,"probe_only":probe_only,"tool_count":count,"gate_blocked":blocked,"message":"界渡本地探针成功，但这不代表ChatGPT客户端已连接；ChatGPT不能直接加载本机stdio配置。" if probe_only else "本地stdio连接成功；权限链已真实阻断记忆读取。" if blocked else "本地stdio连接与记忆读取均成功。"}
    except Exception as exc:item.update({"last_called":_now(),"last_error":redact_text(exc)});result={"connected":False,"tool_count":0,"gate_blocked":False,"message":redact_text(exc)}
    _save(data);return result
def disconnect_client(client):
    data=_state(); data.setdefault("clients",{}).setdefault(client,{}).update({"last_connected":None,"last_error":None}); _save(data); return {"disconnected":True}
def generated_config(client): return config_preview(client,"install")

def create_custom_client(name:str,client_id:str):
    client_id=client_id.strip().lower();name=name.strip()
    if not name:raise ValueError("请输入客户端名称")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,63}",client_id):raise ValueError("client_id只能使用小写字母、数字、下划线或连字符")
    if client_id in {"codex","hermes","other"}:raise ValueError("该client_id已被系统客户端使用")
    data=_state();clients=data.setdefault("clients",{})
    if client_id in clients:raise ValueError("该client_id已经存在")
    clients[client_id]={"custom":True,"name":name,"created_at":_now(),"last_connected":None,"last_called":None,"tool_count":None,"last_error":None};_save(data)
    from .memory_control import update_client_permission
    update_client_permission(client_id,False)
    return custom_client_config(client_id)

def custom_client_config(client_id:str):
    saved=_state().get("clients",{}).get(client_id)
    if client_id=="chatgpt":
        settings=load_settings();endpoint=f"http://127.0.0.1:{settings['console']['port']}/api/chatgpt-adapter"
        return {"client_id":"chatgpt","name":"ChatGPT","configured":False,"config_path":None,"access_method":"localhost HTTP适配器","adapter_endpoint":endpoint,"discovery_endpoint":endpoint+"/discover","call_endpoint":endpoint+"/call","local_stdio_supported":False,"chatgpt_direct_local_support":False,"remote_gateway":"not_configured","reason":"本地适配器可运行，但当前ChatGPT产品不能直接访问localhost；只有真实握手后才显示已连接。","tools":["knowledge_search","context_state","request_knowledge_change"],"owner_action_required":True}
    elif saved and saved.get("custom"):name=saved["name"]
    else:raise ValueError("自定义客户端不存在")
    return {"client_id":client_id,"name":name,"transport":"stdio",**_command(client_id),"local_only":True,"tools":["knowledge_search","context_state","request_knowledge_change","save_raw_conversation"],"permission_note":"读取和原始归档按权限开放；正式知识修改只生成待Owner确认的变更请求。"}
