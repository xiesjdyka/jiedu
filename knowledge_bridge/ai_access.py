"""Minimal AI access layer for a pure, unindexed Knowledge Bridge.

Reads operate directly on Owner-controlled Markdown.  They never build an
index, create candidates, or modify formal knowledge.  Proposed changes are
stored only as pending requests for later Owner review.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .core import ROOT
from .retrieval import RetrievalService
from .workspace import active_workspace
from .knowledge_base import load as load_knowledge_base, markdown_files


REQUESTS = active_workspace().queues / "change-requests.jsonl"
FORMAL_PREFIXES = ("00_系统/", "01_当前状态/", "02_项目/", "03_知识/")
RAW_PREFIX = "90_原始档案/"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _console_search(context, query: str, *, mode: str, scope: str, limit: int,
                    source_type, folder, project, candidate_budget: int, excerpt_budget: int) -> dict:
    """Ask the persistent localhost Console to run semantic retrieval.

    The Console authenticates and authorizes the client again. Credentials are
    sent only to the configured loopback listener and never placed in the URL.
    """
    from .bridge_settings import load_settings
    client_id=os.environ.get('KNOWLEDGE_BRIDGE_CLIENT_ID') or ''
    secret=os.environ.get('KNOWLEDGE_BRIDGE_CLIENT_SECRET') or ''
    if not client_id or not secret:raise PermissionError('Console 语义检索需要已认证客户端')
    settings=load_settings();host=settings['console']['host'];port=int(settings['console']['port'])
    if host!='127.0.0.1':raise ConnectionError('Console 不是本机回环地址')
    body=json.dumps({'query':query,'mode':mode,'scope':scope,'limit':limit,'source_type':source_type,'folder':folder,
                     'project':project,'candidate_budget':candidate_budget,
                     'excerpt_budget':excerpt_budget},ensure_ascii=False).encode('utf-8')
    request=urllib.request.Request(f'http://127.0.0.1:{port}/api/internal/mcp-search',data=body,
        method='POST',headers={'Content-Type':'application/json','Authorization':f'JieduClient {client_id}:{secret}',
                               'X-Jiedu-Workspace-ID':context.workspace_id})
    try:
        # Evidence freshness may require one bounded incremental generation
        # before the query can run.  At the production 50% duty cycle this can
        # legitimately exceed the old 20-second transport timeout.
        with urllib.request.urlopen(request,timeout=60) as response:
            return json.loads(response.read(65537).decode('utf-8'))
    except urllib.error.HTTPError as exc:
        if exc.code in {401,403}:raise PermissionError('Console 拒绝客户端身份或检索权限') from None
        if exc.code==409:raise ValueError('Console 工作区不匹配或尚未就绪') from None
        raise ConnectionError(f'Console 检索返回 HTTP {exc.code}') from None
    except (OSError,ValueError,json.JSONDecodeError) as exc:
        raise ConnectionError('Console 单实例当前不可用') from exc


def _excerpt(text: str, positions: list[int], radius: int = 180) -> str:
    point = min(positions) if positions else 0
    start = max(0, point - radius)
    end = min(len(text), point + radius * 2)
    value = text[start:end].replace("\x00", "").strip()
    return ("…" if start else "") + value + ("…" if end < len(text) else "")


def knowledge_search(query: str, limit: int = 10, include_raw: bool = False, scope: str = "formal", *, mode='hybrid',source_type=None,folder=None,project=None,candidate_budget=80,excerpt_budget=6000) -> dict:
    """Permission callers choose the scope; retrieval semantics stay centralized."""
    if include_raw:
        scope = "evidence"
    base = Path(load_knowledge_base()["vault_path"]).resolve()
    context=active_workspace()
    is_mcp=os.environ.get('KNOWLEDGE_BRIDGE_PROCESS_ROLE')=='mcp'
    same_workspace=context.initialized and context.root.resolve()==base
    if is_mcp and not same_workspace:
        raise PermissionError('MCP 工作区与界渡当前知识库不匹配')
    service=(RetrievalService(context,vector_backend=not is_mcp,
                              vector_backend_reason='当前 MCP 进程不常驻向量模型，且 Console 转交尚未成功；本次使用有界段落检索。')
             if same_workspace else RetrievalService.for_root(base))
    if is_mcp and same_workspace and mode!='keyword' and scope in {'formal','evidence','workspace'}:
        try:
            result=_console_search(context,query,mode=mode,scope=scope,limit=limit,source_type=source_type,folder=folder,
                                   project=project,candidate_budget=candidate_budget,excerpt_budget=excerpt_budget)
            result['retrieval_process']='console_single_instance'
        except ConnectionError as exc:
            service.vector_backend_reason=f'{exc}；本次使用有界段落检索。'
            result=service.search(query,mode=mode,scope=scope,limit=limit,include_context=False,
                                  source_type=source_type,folder=folder,project=project,candidate_budget=candidate_budget,excerpt_budget=excerpt_budget)
            result['retrieval_process']='mcp_keyword_fallback'
    else:
        result = service.search(query,mode=mode,scope=scope,limit=limit,include_context=False,
                                source_type=source_type,folder=folder,project=project,candidate_budget=candidate_budget,excerpt_budget=excerpt_budget)
        result['retrieval_process']='mcp_keyword' if is_mcp else 'local_process'
    result["raw_included"] = bool(include_raw)
    return result


def context_state(project: str | None = None) -> dict:
    """Describe current configured context without compiling Core or indexing."""
    config = load_knowledge_base()
    base = Path(config["vault_path"]).resolve()
    from .knowledge_entities import KnowledgeEntities
    entities=KnowledgeEntities(active_workspace());entity=entities.resolve(name=project) if project else None

    def metadata(relative: str) -> dict:
        path = (base / relative).resolve()
        try:
            path.relative_to(base)
        except ValueError:
            return {"path": relative, "available": False, "error": "路径越界"}
        if not path.is_file():
            return {"path": relative, "available": False}
        raw = path.read_bytes()
        return {
            "path": relative,
            "available": True,
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "modified_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
        }

    core = [metadata(item) for item in config.get("core_files", [])]
    current = [metadata(item) for item in markdown_files(base) if item.startswith("01_当前状态/")]
    project_rows = []
    if project:
        safe = project.strip()
        roots=entity.get('paths',[]) if entity else [f"02_项目/{safe}"]
        for root in roots:
            for name in ("当前状态.md", "项目总览.md"):
                project_rows.append(metadata(root+'/'+name))
    return {
        "knowledge_root": str(base),
        "core_files": core,
        "current_state_files": current,
        "project": entity['name'] if entity else project,
        "entity_context": entities.public(entity) if entity else None,
        "project_files": project_rows,
        "read_only": True,
        "index_used": False,
        "generated_at": _now(),
    }


def request_change(client_id: str, target_path: str, proposed_content: str, reason: str,
                   evidence: list[dict] | None = None, **options) -> dict:
    """Submit to the existing candidate queue; never apply formal changes."""
    from .organizer import submit
    return submit(active_workspace(),client_id,target_path,proposed_content,reason,evidence,**options)
