from __future__ import annotations
import ctypes,json,os
from pathlib import Path
from .core import ROOT
from .workspace import local_path
PATH=local_path("provider-config.json");AUDIT=local_path("provider-calls.jsonl")
DEFAULT_PROMPT="""你是界渡的知识提炼节点。只根据输入包中的逐字原文和必要背景提出可追溯的长期知识候选，不得猜测事实，不得直接修改正式知识。严格返回 KB_MEMORY_CANDIDATES_V1 JSON；每条候选必须包含有效证据、目标层、目标相对路径、理由、置信度与冲突状态。没有长期价值的内容使用 ignore。"""
DEFAULT={"provider_type":"disabled","base_url":"","model":"","api_key_secret_ref":"","timeout":60,"max_input":32000,"enabled":False,"extraction_prompt":DEFAULT_PROMPT}
AI_CHAT_CREDENTIAL="Jiedu.AI.Provider"
def load(path=PATH):
 try:return DEFAULT|json.loads(path.read_text("utf-8"))
 except (OSError,ValueError):return dict(DEFAULT)
def validate(data):
 p=data["provider_type"]
 if p not in {"disabled","manual-chatgpt","deterministic-test","openai-compatible"}:raise ValueError("Provider类型无效")
 if not isinstance(data["timeout"],int) or not 1<=data["timeout"]<=600:raise ValueError("timeout必须为1到600秒")
 if not isinstance(data["max_input"],int) or not 100<=data["max_input"]<=1000000:raise ValueError("max input无效")
 if p=="openai-compatible" and data["enabled"]:
  if not data["base_url"].startswith(("http://127.0.0.1","http://localhost","https://")):raise ValueError("base_url无效")
  if not data["model"]:raise ValueError("model不能为空")
  if not str(data["api_key_secret_ref"]).startswith(("env:","wincred:")):raise ValueError("API Key只能保存secret reference")
def save(update,path=PATH):
 data=load(path)|update
 if any(k in update for k in ("api_key","token","secret")):raise ValueError("禁止保存明文密钥")
 validate(data);path.parent.mkdir(parents=True,exist_ok=True);tmp=path.with_suffix(".tmp");tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2),"utf-8");os.replace(tmp,path);return status(path)
def _store_windows_secret(value,target=AI_CHAT_CREDENTIAL):
 if os.name!="nt":raise ValueError("当前系统不支持Windows凭据管理器")
 class CREDENTIALW(ctypes.Structure):
  _fields_=[("Flags",ctypes.c_ulong),("Type",ctypes.c_ulong),("TargetName",ctypes.c_wchar_p),("Comment",ctypes.c_wchar_p),("LastWritten",ctypes.c_byte*8),("CredentialBlobSize",ctypes.c_ulong),("CredentialBlob",ctypes.POINTER(ctypes.c_ubyte)),("Persist",ctypes.c_ulong),("AttributeCount",ctypes.c_ulong),("Attributes",ctypes.c_void_p),("TargetAlias",ctypes.c_wchar_p),("UserName",ctypes.c_wchar_p)]
 blob=value.encode("utf-16-le");buffer=ctypes.create_string_buffer(blob)
 credential=CREDENTIALW(0,1,target,"界渡AI Provider密钥",(ctypes.c_byte*8)(),len(blob),ctypes.cast(buffer,ctypes.POINTER(ctypes.c_ubyte)),2,0,None,None,"界渡")
 if not ctypes.windll.advapi32.CredWriteW(ctypes.byref(credential),0):raise ctypes.WinError()
 return "wincred:"+target
def save_ai_chat(provider,model,endpoint,api_key="",path=PATH):
 current=load(path);reference=current.get("api_key_secret_ref","")
 if api_key:reference=_store_windows_secret(api_key)
 if not reference:raise ValueError("请输入API Key")
 return save({"provider_type":"openai-compatible","base_url":endpoint.strip(),"model":model.strip(),"api_key_secret_ref":reference,"enabled":True},path)
def status(path=PATH):
 data=load(path);configured=data["provider_type"]!="disabled"
 error=None
 try:validate(data)
 except ValueError as e:error=str(e)
 public={k:v for k,v in data.items() if k!="api_key_secret_ref"}|{"api_key_secret_ref":None,"credential_present":bool(data["api_key_secret_ref"])}
 calls=[]
 try:calls=[json.loads(x) for x in AUDIT.read_text("utf-8").splitlines() if x.strip()]
 except (OSError,ValueError):pass
 return {"state":"配置错误" if error else "已配置" if configured else "未配置","error":error,"config":public,"network_calls":len(calls),"last_call":calls[-1] if calls else None,"default_prompt":DEFAULT_PROMPT}
def record_call(item):
 AUDIT.parent.mkdir(parents=True,exist_ok=True)
 with AUDIT.open("a",encoding="utf-8") as handle:handle.write(json.dumps(item,ensure_ascii=False)+"\n")
