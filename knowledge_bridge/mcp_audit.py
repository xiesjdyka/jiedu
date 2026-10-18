from __future__ import annotations
import json,threading
from contextlib import contextmanager
from datetime import datetime,timezone
from pathlib import Path
from .core import ROOT
from .workspace import active_workspace
from .redaction import redact,redact_text,sensitive_key

AUDIT_PATH=active_workspace().audit/"mcp-call-audit.jsonl";_lock=threading.Lock()
def _now():return datetime.now(timezone.utc).isoformat()
def _summary(params):
    safe={}
    for key,value in (params or {}).items():
        if value is None:continue
        if sensitive_key(key):continue
        text=redact_text(redact(value));safe[key]=text[:180]+("…" if len(text)>180 else "")
    return safe
def record(client_id,tool,params,outcome,error=None,authenticated_identity=None,identity_status=None):
    row={"at":_now(),"client_id":client_id,"claimed_identity":client_id,
         "authenticated_identity":authenticated_identity,"identity_status":identity_status,
         "tool":tool,"outcome":outcome,"parameters":_summary(params),"error":redact_text(error)[:300] if error else None}
    AUDIT_PATH.parent.mkdir(parents=True,exist_ok=True)
    with _lock,AUDIT_PATH.open("a",encoding="utf-8") as f:f.write(json.dumps(row,ensure_ascii=False)+"\n")
    return row
@contextmanager
def audited(client_id,tool,params=None,authenticated_identity=None,identity_status=None):
    try:yield
    except Exception as exc:record(client_id,tool,params,"rejected" if isinstance(exc,PermissionError) else "failed",exc,authenticated_identity,identity_status);raise
    else:record(client_id,tool,params,"success",authenticated_identity=authenticated_identity,identity_status=identity_status)
def client_summary(client_id):
    rows=[]
    if AUDIT_PATH.exists():
        for line in AUDIT_PATH.read_text("utf-8").splitlines():
            try:
                row=json.loads(line)
                if row.get("client_id")==client_id:rows.append(row)
            except ValueError:pass
    last=rows[-1] if rows else None
    return {"mcp_call_count":len(rows),"last_mcp_tool":last.get("tool") if last else None,"last_mcp_call":last.get("at") if last else None,"last_mcp_outcome":last.get("outcome") if last else None,"last_mcp_parameters":last.get("parameters") if last else None,"last_mcp_error":last.get("error") if last else None,"recent_mcp_calls":rows[-20:]}
