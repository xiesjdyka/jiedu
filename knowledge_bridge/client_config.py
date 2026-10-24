from __future__ import annotations

import difflib, hashlib, json, os, re, shutil, threading, uuid
from datetime import datetime, timezone
from pathlib import Path

import tomllib
import yaml

from .core import ROOT, VAULT
from .workspace import local_path
from .package_layout import python_executable, component, package_root, connected_workspace
from .redaction import redact_text

DATA = local_path("client-config")
BACKUPS = DATA / "backups"
AUDIT = DATA / "audit.jsonl"
PENDING = DATA / "pending.json"
NAME = "knowledge_bridge"
_EPHEMERAL_PENDING: dict[str, dict] = {}
_EPHEMERAL_LOCK = threading.RLock()

def _now(): return datetime.now(timezone.utc).isoformat()
def _sha(raw: bytes): return hashlib.sha256(raw).hexdigest()
def _python():
    path = python_executable(ROOT)
    if path.exists(): return path
    return Path(os.sys.executable)
def _command(client_id="other", secret: str | None = None):
    packaged = component("control", ROOT) is not None
    if packaged and component("workspace", ROOT) is not None:
        from .portable_entry import locator_configuration
        return locator_configuration(client_id, secret)
    env={"KNOWLEDGE_BRIDGE_CLIENT_ID":client_id,
         "PYTHONIOENCODING":"utf-8",
         "KNOWLEDGE_BRIDGE_VAULT":str(VAULT.resolve()),
         "KNOWLEDGE_BRIDGE_LOCAL_DATA":str(local_path().resolve())}
    if secret:env["KNOWLEDGE_BRIDGE_CLIENT_SECRET"]=secret
    python = python_executable(ROOT) if packaged else _python()
    args = (["-I", "-X", "utf8"] if packaged else []) + ["-m", "knowledge_bridge.cli", "mcp", "--transport", "stdio"]
    return {"command": str(python), "args": args, "cwd": str(ROOT), "env": env}

def generic_stdio_config(client_id,workspace,local_data):
    """Safe display fields only. The secret is delivered separately in memory."""
    if component("control", ROOT) is not None and component("workspace", ROOT) is not None:
        entry = _command(client_id, "<配对秘密>")
        return {"transport":"stdio", **entry, "cwd_required":False, "instructions":"先用 .jiedu 内的入口登记活动位置，再复制此配置；同父目录改名可自动定位，跨目录移动后需重新定位。"}
    python=python_executable(ROOT)
    if not python.exists():python=Path(os.sys.executable).resolve()
    entry=ROOT/'scripts/mcp_stdio.py'
    if not entry.is_file():raise ValueError('通用 STDIO 启动入口缺失，请修复界渡程序')
    return {'transport':'stdio','command':str(python),'args':[str(entry.resolve())],
            'env':{'KNOWLEDGE_BRIDGE_CLIENT_ID':client_id,'KNOWLEDGE_BRIDGE_CLIENT_SECRET':'<配对秘密>',
                   'KNOWLEDGE_BRIDGE_VAULT':str(workspace.resolve()),'KNOWLEDGE_BRIDGE_LOCAL_DATA':str(local_data.resolve())},
            'cwd_required':False,'instructions':'选择 STDIO。命令、每一项参数和每一项环境变量分别填写，不添加额外引号。保存并启用服务后，执行一次限定范围读取；界渡显示已验证身份才表示收到有效请求。接入不包含历史自动归档。'}

class Adapter:
    id = "other"; label = "自定义"
    def detect(self): return {"installed": None, "config_path": None, "config_exists": False, "entry": None}
    def render(self, old: str, client_id: str | None = None,
               secret: str | None = None):
        raise ValueError("该客户端尚未提供可靠的配置适配器")

class CodexAdapter(Adapter):
    id="codex"; label="Codex"
    def __init__(self): self.path=Path(os.getenv("CODEX_HOME", Path.home()/".codex"))/"config.toml"
    def detect(self):
        old=self.path.read_text("utf-8") if self.path.exists() else ""
        entry=None
        try: entry=tomllib.loads(old).get("mcp_servers",{}).get(NAME)
        except (tomllib.TOMLDecodeError,OSError): pass
        return {"installed": self.path.parent.exists(), "config_path":str(self.path), "config_exists":self.path.exists(), "entry":entry}
    def render(self,old,client_id: str | None = None,secret: str | None = None):
        identity=client_id or "codex";c=_command(identity,secret)
        environment=''.join(f'{key} = {json.dumps(value)}\n' for key,value in c['env'].items())
        working_directory=f'cwd = {json.dumps(c["cwd"])}\n' if c.get('cwd') else ''
        block=f'[mcp_servers.{NAME}]\ncommand = {json.dumps(c["command"])}\nargs = {json.dumps(c["args"])}\n{working_directory}\n[mcp_servers.{NAME}.env]\n{environment}'
        pattern=re.compile(rf'(?ms)^\[mcp_servers\.{re.escape(NAME)}\]\s*\n.*?(?=^\[(?!mcp_servers\.{re.escape(NAME)}\.)|\Z)')
        return pattern.sub(lambda _:block+"\n",old).rstrip()+"\n" if pattern.search(old) else old.rstrip()+"\n\n"+block
    def remove(self,old): return re.sub(rf'(?ms)^\[mcp_servers\.{re.escape(NAME)}\]\s*\n.*?(?=^\[(?!mcp_servers\.{re.escape(NAME)}\.)|\Z)','',old).rstrip()+"\n"
    def validate(self,text): tomllib.loads(text)

def hermes_config_path():
    """Follow Hermes desktop home selection, never config modification time."""
    explicit=os.environ.get('HERMES_HOME','').strip()
    desktop_data=os.environ.get('HERMES_DESKTOP_USER_DATA_DIR','').strip()
    if not explicit and desktop_data:return Path(desktop_data).expanduser().resolve()/'hermes-home/config.yaml'
    if not explicit and os.name=='nt':
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,'Environment') as key:
                explicit=str(winreg.QueryValueEx(key,'HERMES_HOME')[0]).strip()
        except OSError:pass
    if explicit:return Path(os.path.expandvars(explicit)).expanduser()/'config.yaml'
    legacy=Path.home()/'.hermes'
    if os.name!='nt':return legacy/'config.yaml'
    local=Path(os.getenv('LOCALAPPDATA',Path.home()/'AppData'/'Local'))/'hermes'
    return (local if local.exists() or not legacy.exists() else legacy)/'config.yaml'


class HermesAdapter(Adapter):
    id="hermes"; label="Hermes Agent"
    def __init__(self):
        local=Path(os.getenv("LOCALAPPDATA",Path.home()/"AppData"/"Local"))/"hermes"/"config.yaml"
        legacy=Path.home()/".hermes"/"config.yaml"
        self.path=hermes_config_path()
        self.install_hints=[local.parent,legacy.parent]
    def detect(self):
        data={}
        if self.path.exists():
            try:data=yaml.safe_load(self.path.read_text("utf-8")) or {}
            except (yaml.YAMLError,OSError):pass
        return {"installed":self.path.is_file() or self.path.parent.is_dir() or any(p.exists() for p in self.install_hints),"config_path":str(self.path),"config_exists":self.path.exists(),"entry":data.get("mcp_servers",{}).get(NAME) if isinstance(data,dict) else None}
    def render(self,old,client_id: str | None = None,secret: str | None = None):
        data=yaml.safe_load(old) if old.strip() else {}; data=data or {}
        if not isinstance(data,dict): raise ValueError("Hermes配置根节点必须是映射")
        servers=data.setdefault("mcp_servers",{});identity=client_id or "hermes";c=_command(identity,secret)
        servers[NAME]={**c,"enabled":True}
        return yaml.safe_dump(data,allow_unicode=True,sort_keys=False)
    def remove(self,old):
        data=yaml.safe_load(old) if old.strip() else {}; data=data or {}; servers=data.get("mcp_servers",{})
        if isinstance(servers,dict): servers.pop(NAME,None)
        return yaml.safe_dump(data,allow_unicode=True,sort_keys=False)
    def validate(self,text):
        data=yaml.safe_load(text) or {}
        if not isinstance(data,dict): raise ValueError("无效YAML配置")

_DSH_BEGIN = "# BEGIN JIEDU MCP CONNECTION"
_DSH_END = "# END JIEDU MCP CONNECTION"

class _DSHPatchLoader(yaml.SafeLoader):
    pass

_DSHPatchLoader.add_constructor('tag:yaml.org,2002:js',
    lambda loader,node: loader.construct_scalar(node))

def _dsh_patch_rows(text: str) -> list:
    if not text.strip():return []
    try:value=yaml.load(text,Loader=_DSHPatchLoader)
    except yaml.YAMLError as exc:raise ValueError('DSH 补丁配置无法解析，原文件保持不变') from exc
    if not isinstance(value,list) or not all(isinstance(row,dict) for row in value):
        raise ValueError('DSH 补丁配置必须是条目列表，原文件保持不变')
    return value

def _dsh_conflicts(value) -> bool:
    if isinstance(value,dict):
        if value.get('serverName')=='knowledge_bridge' or value.get('id')=='jiedu-knowledge-bridge':
            return True
        return any(_dsh_conflicts(child) for child in value.values())
    return any(_dsh_conflicts(child) for child in value) if isinstance(value,list) else False


class DSHAdapter(Adapter):
    id="dsh"; label="DSH"
    def __init__(self, path: Path | None = None):
        home=Path(os.getenv("DSH_HOME", Path.home()/".dsh"))
        self.path=path or home/"profiles"/"desktop"/"cordis.patch.yml"
    def _block(self, old: str):
        if old.count(_DSH_BEGIN)!=old.count(_DSH_END) or old.count(_DSH_BEGIN)>1:
            raise ValueError('DSH 界渡补丁标记不完整或重复，原文件保持不变')
        if not old.count(_DSH_BEGIN):return None
        start=old.find(_DSH_BEGIN);end=old.find(_DSH_END)
        if end<start:raise ValueError('DSH 界渡补丁标记顺序错误，原文件保持不变')
        return old[start+len(_DSH_BEGIN):end].strip()
    def detect(self):
        old=self.path.read_text("utf-8") if self.path.is_file() else ""
        entry=None
        try:
            self.validate(old)
            rows=_dsh_patch_rows(old)
            block=self._block(old)
            if block:
                parsed=_dsh_patch_rows(block)
                items=parsed[0].get('insert',[]) if len(parsed)==1 else []
                if items and isinstance(items[0],dict):entry=items[0].get('config')
            elif _dsh_conflicts(rows):entry={"unmanaged_existing":True}
        except ValueError:
            entry={"invalid_existing":True}
        return {"installed":self.path.parent.is_dir(),"config_path":str(self.path),
                "config_exists":self.path.is_file(),"entry":entry}
    def render(self,old,client_id: str | None = None,secret: str | None = None):
        _dsh_patch_rows(old)
        old=self.remove(old)
        if _dsh_conflicts(_dsh_patch_rows(old)):
            raise ValueError("DSH 已有非界渡管理的同名 MCP 服务，请先核对")
        c=_command(client_id or "dsh",secret)
        config={"serverName":"knowledge_bridge","transport":"stdio",
                "command":c["command"],"args":c["args"],"env":c["env"]}
        if c.get("cwd"):config["cwd"]=c["cwd"]
        row={"insert":[{"id":"jiedu-knowledge-bridge",
                        "name":"@deepseek-ai/dsh-mcp-client","config":config}]}
        block=_DSH_BEGIN+"\n"+yaml.safe_dump([row],allow_unicode=True,sort_keys=False)+_DSH_END
        return old.rstrip()+"\n\n"+block+"\n"
    def remove(self,old):
        if self._block(old) is None:return old
        pattern=re.compile(r"(?ms)^"+re.escape(_DSH_BEGIN)+r"\n.*?^"+re.escape(_DSH_END)+r"\n?")
        if not pattern.search(old):raise ValueError('DSH 界渡补丁边界无法核验，原文件保持不变')
        return pattern.sub("",old)
    def validate(self,text):
        rows=_dsh_patch_rows(text)
        block=self._block(text)
        if block is None:return
        parsed=_dsh_patch_rows(block)
        if len(parsed)!=1:
            raise ValueError("DSH MCP 补丁结构无效")
        rows=parsed[0].get("insert")
        if not isinstance(rows,list) or len(rows)!=1 or rows[0].get("name")!="@deepseek-ai/dsh-mcp-client":
            raise ValueError("DSH MCP 服务条目无效")
        config=rows[0].get("config") or {}
        if config.get("transport")!="stdio" or config.get("serverName")!="knowledge_bridge":
            raise ValueError("DSH MCP 连接方式无效")
        if _dsh_conflicts(_dsh_patch_rows(self.remove(text))):
            raise ValueError('DSH 其他补丁含同名服务或条目标识')


class ClaudeAdapter(Adapter):
    id="claude"; label="Claude"
    def __init__(self, path: Path | None = None):
        local=Path(os.getenv("LOCALAPPDATA",Path.home()/"AppData"/"Local"))
        msix=local/"Packages"/"Claude_pzs8sxrjxfjjc"/"LocalCache"/"Roaming"/"Claude"
        roaming=Path(os.getenv("APPDATA",Path.home()/"AppData"/"Roaming"))/"Claude"
        self.ambiguous=path is None and msix.is_dir() and roaming.is_dir()
        self.path=path or (msix if msix.is_dir() else roaming)/"claude_desktop_config.json"
    def detect(self):
        if self.ambiguous:
            return {"installed":True,"config_path":None,"config_exists":None,
                    "entry":{"invalid_existing":True,"ambiguous_profiles":True}}
        data={}
        if self.path.is_file():
            try:data=json.loads(self.path.read_text("utf-8"))
            except (ValueError,OSError):pass
        return {"installed":self.path.parent.is_dir(),"config_path":str(self.path),
                "config_exists":self.path.is_file(),"entry":(data.get("mcpServers") or {}).get(NAME) if isinstance(data,dict) else None}
    def render(self,old,client_id: str | None = None,secret: str | None = None):
        if self.ambiguous:raise ValueError("检测到多个 Claude 配置目录；请先明确选择目标")
        data=json.loads(old) if old.strip() else {}
        if not isinstance(data,dict):raise ValueError("Claude 桌面配置根节点必须是对象")
        servers=data.setdefault("mcpServers",{})
        if not isinstance(servers,dict):raise ValueError("Claude MCP 服务配置无效")
        c=_command(client_id or "claude",secret)
        servers[NAME]={key:c[key] for key in ("command","args","env")}
        return json.dumps(data,ensure_ascii=False,indent=2)+"\n"
    def remove(self,old):
        data=json.loads(old) if old.strip() else {}
        servers=data.get("mcpServers")
        if isinstance(servers,dict):servers.pop(NAME,None)
        return json.dumps(data,ensure_ascii=False,indent=2)+"\n"
    def validate(self,text):
        if not isinstance(json.loads(text),dict):raise ValueError("Claude 桌面配置根节点必须是对象")


class CursorAdapter(Adapter):
    id="cursor"; label="Cursor"
    def __init__(self): self.path=Path.home()/".cursor"/"mcp.json"
    def detect(self):
        try:data=json.loads(self.path.read_text("utf-8")) if self.path.is_file() else {}
        except (OSError,ValueError):data={}
        servers=data.get("mcpServers") if isinstance(data,dict) else None
        return {"installed":self.path.parent.is_dir(),"config_path":str(self.path),
                "config_exists":self.path.is_file(),"entry":servers.get(NAME) if isinstance(servers,dict) else None}
    def render(self,old,client_id=None,secret=None):
        data=json.loads(old) if old.strip() else {}
        if not isinstance(data,dict):raise ValueError("Cursor MCP 根节点必须是对象")
        servers=data.setdefault("mcpServers",{})
        if not isinstance(servers,dict):raise ValueError("Cursor MCP 服务配置无效")
        c=_command(client_id or "cursor",secret)
        servers[NAME]={key:c[key] for key in ("command","args","env")}
        return json.dumps(data,ensure_ascii=False,indent=2)+"\n"
    def remove(self,old):
        data=json.loads(old) if old.strip() else {}
        servers=data.get("mcpServers")
        if isinstance(servers,dict):servers.pop(NAME,None)
        return json.dumps(data,ensure_ascii=False,indent=2)+"\n"
    def validate(self,text):
        if not isinstance(json.loads(text),dict):raise ValueError("Cursor MCP 根节点必须是对象")


ADAPTERS={"cursor":CursorAdapter(),"codex":CodexAdapter(),"hermes":HermesAdapter(),
          "dsh":DSHAdapter(),"claude":ClaudeAdapter(),"other":Adapter()}
def _entry_identity(entry):
    return entry.get("env",{}).get("KNOWLEDGE_BRIDGE_CLIENT_ID") if isinstance(entry,dict) and isinstance(entry.get("env",{}),dict) else None
def _valid_entry(entry,client_id=None):
    if not isinstance(entry,dict): return False
    command=str(entry.get("command",entry.get("url","")))
    if command.startswith("http://"): return command.startswith("http://127.0.0.1")
    if command.startswith("https://") or "0.0.0.0" in command: return False
    args=" ".join(map(str,entry.get("args",[])))
    control=package_root(ROOT)
    try:
        locator_allowed=control is not None and (component("workspace", ROOT) is not None or
                                                 connected_workspace(control) is not None)
    except (OSError,ValueError):
        locator_allowed=False
    if locator_allowed:
        from .portable_entry import locator_configuration
        expected=locator_configuration(client_id or 'other')
        if command==expected['command'] and entry.get('args')==expected['args']:
            return Path(command).is_file() and Path(expected['args'][6]).is_file()
    if Path(command).name.casefold() in {'pwsh.exe','powershell.exe'}:
        return False
    executable=Path(command).exists() and ("knowledge_bridge.cli" in args or (Path(command).name.casefold()=="kb.exe" and "mcp" in args))
    return executable
def statuses():
    rows=[]
    for key,a in ADAPTERS.items():
        d=a.detect(); entry=d["entry"]; valid=_valid_entry(entry,key); identity=_entry_identity(entry)
        state="待确认" if d["installed"] is None else "未安装" if not d["installed"] else "未配置" if not entry else "已配置" if valid else "配置失效"
        credential_present=bool(isinstance(entry,dict) and isinstance(entry.get("env"),dict) and entry["env"].get("KNOWLEDGE_BRIDGE_CLIENT_SECRET"))
        rows.append({"id":key,"client":a.label,"installed":d["installed"],"config_path":d["config_path"],
            "config_exists":d["config_exists"],"configured":bool(entry),"config_valid":valid,
            "declared_identity":identity,"credential_present":credential_present,
            "identity_configured":identity==key,"identity_upgrade_available":bool(entry and valid and identity!=key),
            "config_state":state,"bridge_path":str(ROOT),"python_path":str(_python()),
            "vault_path":str(VAULT),"transport":"stdio","restart_required":key in {"codex","claude"}})
    return rows
def _masked(value: str, secret: str | None) -> str:
    return value.replace(secret,"<配对秘密：确认时安全写入>") if secret else value

def preview(client,action="install",*,client_id: str | None = None,
            secret: str | None = None,ephemeral: bool = False):
    a=ADAPTERS.get(client); 
    if not a or client=="other": raise ValueError("该客户端仅预留适配器，暂不支持自动配置")
    d=a.detect(); path=Path(d["config_path"]); old=path.read_bytes().decode("utf-8") if path.exists() else ""
    if secret is None and isinstance(d.get("entry"),dict):
        existing_env=d["entry"].get("env")
        if isinstance(existing_env,dict) and existing_env.get("KNOWLEDGE_BRIDGE_CLIENT_SECRET"):
            raise ValueError("已有配对凭据；请在客户端访问中使用“修复连接”，保留原身份和权限")
    render_base=old.replace("\r\n","\n")
    new=a.remove(render_base) if action=="remove" else a.render(render_base,client_id,secret); a.validate(new)
    if action!="remove":
        if client=="codex":
            parsed=tomllib.loads(new);bridge=(parsed.get("mcp_servers") or {}).get(NAME)
        elif client=="hermes":
            parsed=yaml.safe_load(new) or {};bridge=(parsed.get("mcp_servers") or {}).get(NAME)
        elif client=="dsh":
            parsed=yaml.safe_load(a._block(new)) or []
            bridge=parsed[0]["insert"][0]["config"]
        else:
            parsed=json.loads(new);bridge=(parsed.get("mcpServers") or {}).get(NAME)
        if not isinstance(bridge,dict):raise ValueError("界渡本机连接配置缺失")
        controlled=json.dumps(bridge,ensure_ascii=False)
        if "0.0.0.0" in controlled or "http://" in controlled or "https://" in controlled:
            raise ValueError("安全策略拒绝非本机配置")
    token=uuid.uuid4().hex; DATA.mkdir(parents=True,exist_ok=True)
    pending={"token":token,"client":client,"action":action,"path":str(path),"old":old,"new":new,"created_at":_now(),"ephemeral":ephemeral}
    with _EPHEMERAL_LOCK:_EPHEMERAL_PENDING[token]=pending
    diff="\n".join(difflib.unified_diff(old.splitlines(),new.splitlines(),fromfile=str(path)+"（当前）",tofile=str(path)+"（预览）",lineterm=""))
    return {"token":token,"client":client,"action":action,"target_path":str(path),
            "diff":redact_text(_masked(diff or "没有变化",secret)),
            "config":redact_text(_masked(new,secret)),
            "changed":old!=new,"requires_confirmation":True,
            "backup_directory":str(BACKUPS),"rollback":"应用失败时自动恢复修改前配置；应用后可使用备份文件恢复。",
            "restart_required":client in {"codex","claude"}}

def cancel(token: str) -> bool:
    with _EPHEMERAL_LOCK:
        if _EPHEMERAL_PENDING.pop(token,None) is not None:return True
    try:
        pending=json.loads(PENDING.read_text("utf-8"))
        if pending.get("token")==token:PENDING.unlink(missing_ok=True);return True
    except (OSError,ValueError):pass
    return False

def _take_pending(token: str) -> dict:
    with _EPHEMERAL_LOCK:
        pending=_EPHEMERAL_PENDING.pop(token,None)
    if pending is not None:return pending
    pending=json.loads(PENDING.read_text("utf-8"))
    if pending.get("token")!=token:raise ValueError("预览已失效，请重新生成")
    return pending
def apply(token):
    p=_take_pending(token)
    a=ADAPTERS[p["client"]]; path=Path(p["path"]);before=path.read_bytes() if path.exists() else b"";current=before.decode("utf-8")
    if current!=p["old"]: raise ValueError("客户端配置已被其他程序修改，请重新预览")
    BACKUPS.mkdir(parents=True,exist_ok=True); stamp=datetime.now().strftime("%Y%m%d-%H%M%S"); backup=BACKUPS/f'{p["client"]}-{stamp}-{uuid.uuid4().hex[:6]}.bak'
    backup.write_bytes(before); path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(p["new"],"utf-8");a.validate(temporary.read_text("utf-8"));os.replace(temporary,path)
    except Exception:
        temporary.unlink(missing_ok=True)
        if before:path.write_bytes(before)
        elif path.exists():path.unlink()
        raise
    after=path.read_bytes(); record={"at":_now(),"client":p["client"],"action":p["action"],"path":str(path),"backup":str(backup),"before_sha256":_sha(before),"after_sha256":_sha(after)}
    with AUDIT.open("a",encoding="utf-8") as f:f.write(json.dumps(record,ensure_ascii=False)+"\n")
    if not p.get("ephemeral"):PENDING.unlink(missing_ok=True)
    return {"applied":True,"backup":str(backup),**record}
def rollback(result):
    path=Path(result["path"]); backup=Path(result["backup"]); raw=backup.read_bytes()
    if raw:path.write_bytes(raw)
    elif path.exists():path.unlink()
    with AUDIT.open("a",encoding="utf-8") as f:f.write(json.dumps({"at":_now(),"client":result["client"],"action":"automatic_rollback","path":str(path),"restored_sha256":_sha(raw)},ensure_ascii=False)+"\n")
    return {"rolled_back":True,"restored_sha256":_sha(raw)}


def remove_identity_configuration(registry, internal_id):
    """Remove only entries verified as this exact identity, never a same-name successor."""
    removed=[];skipped=[]
    for key,adapter in ADAPTERS.items():
        if key=='other':continue
        detected=adapter.detect();entry=detected.get('entry') or {}
        if not isinstance(entry,dict):continue
        env=entry.get('env',{})
        if not isinstance(env,dict):continue
        if not registry.credential_matches(internal_id,env.get('KNOWLEDGE_BRIDGE_CLIENT_ID'),env.get('KNOWLEDGE_BRIDGE_CLIENT_SECRET','')):
            continue
        pending=preview(key,'remove',secret=env.get('KNOWLEDGE_BRIDGE_CLIENT_SECRET'),ephemeral=True)
        # preview may have raced with a client rewrite: verify its exact snapshot.
        with _EPHEMERAL_LOCK:
            snapshot=_EPHEMERAL_PENDING[pending['token']]['old']
        if key=='codex':snapshot_env=tomllib.loads(snapshot).get('mcp_servers',{}).get(NAME,{}).get('env',{})
        elif key=='hermes':snapshot_env=(yaml.safe_load(snapshot) or {}).get('mcp_servers',{}).get(NAME,{}).get('env',{})
        elif key=='dsh':snapshot_env=(yaml.safe_load(adapter._block(snapshot)) or [{}])[0].get('insert',[{}])[0].get('config',{}).get('env',{})
        else:snapshot_env=(json.loads(snapshot).get('mcpServers') or {}).get(NAME,{}).get('env',{})
        if not registry.credential_matches(internal_id,snapshot_env.get('KNOWLEDGE_BRIDGE_CLIENT_ID'),snapshot_env.get('KNOWLEDGE_BRIDGE_CLIENT_SECRET','')):
            cancel(pending['token']);skipped.append(key);continue
        apply(pending['token']);removed.append(key)
    return {'removed':removed,'skipped':skipped}
