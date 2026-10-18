"""Owner-triggered AI chat over permission-checked Knowledge Bridge context."""
from __future__ import annotations

import json
import copy
import urllib.request
from datetime import datetime, timezone

from .automatic_extraction import _endpoint, _secret
from .chatgpt_adapter import context_request
from .provider_config import load as load_provider, record_call
from .redaction import redact_text


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def status() -> dict:
    config = load_provider()
    ready = config.get("provider_type") == "openai-compatible" and bool(config.get("enabled"))
    return {
        "ready": ready,
        "state": "已就绪" if ready else "尚未配置可用AI Provider",
        "provider": "OpenAI-compatible" if config.get("provider_type") == "openai-compatible" else None,
        "model": config.get("model") or None,
        "endpoint_configured": bool(config.get("base_url")),
        "secret_configured": bool(config.get("api_key_secret_ref")),
    }


def test_connection() -> dict:
    config = load_provider()
    if config.get("provider_type") != "openai-compatible" or not config.get("enabled"):
        raise ValueError("请先保存可用的AI配置")
    payload = {"model": config["model"], "messages": [{"role": "user", "content": "只回复：连接成功"}], "temperature": 0, "max_tokens": 12}
    request = urllib.request.Request(_endpoint(config["base_url"]), data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers={"Content-Type": "application/json", "Authorization": "Bearer " + _secret(config["api_key_secret_ref"])}, method="POST")
    started = _now()
    try:
        with urllib.request.urlopen(request, timeout=config["timeout"]) as response:
            raw = json.loads(response.read().decode("utf-8"))
        if not str(raw.get("choices", [{}])[0].get("message", {}).get("content", "")).strip():
            raise ValueError("Provider未返回有效响应")
        record_call({"purpose": "ai_chat_connection_test", "started_at": started, "finished_at": _now(), "provider": "openai-compatible", "model": config["model"], "success": True, "usage": raw.get("usage") or {}})
        return {"ok": True, "state": "连接成功", "provider": "OpenAI-compatible", "model": config["model"], "usage": raw.get("usage") or {}}
    except Exception as exc:
        record_call({"purpose": "ai_chat_connection_test", "started_at": started, "finished_at": _now(), "provider": "openai-compatible", "model": config.get("model"), "success": False, "error": redact_text(exc)[:300]})
        raise


def _model_context(context: dict, max_chars: int) -> str:
    payload = {
        "用户基础上下文": context["user"],
        "当前项目状态": context["project"],
        "当前知识状态": context["state"],
        "相关知识": context["related_knowledge"],
        "权限状态": context["permissions"],
    }
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(text) <= max_chars:
        return text
    reduced = copy.deepcopy(payload)
    fields = []
    def collect(node):
        if isinstance(node, list):
            for item in node: collect(item)
        elif isinstance(node, dict):
            for key, value in list(node.items()):
                if key in {'content', 'original_text', 'excerpt', 'text', 'body'} and isinstance(value, str):
                    fields.append((node, key, value))
                else:
                    collect(value)
    # Source identities, versions and all permissions remain intact. Only text bodies
    # have a budget; an impossible metadata budget fails before sending a request.
    for key in payload:
        if key != '权限状态': collect(reduced[key])
    original_flags={id(node):(node.get('truncated'),copy.deepcopy(node.get('omitted_fields',{}))) for node,_,_ in fields}
    reduced['上下文省略'] = {'reason': '字符预算', 'text_fields': len(fields),
                            'omitted_characters': 0, 'metadata_preserved': True}
    def render(limit):
        omitted = 0
        for node,_,_ in fields:
            flag,previous_omissions=original_flags[id(node)]
            if flag is None:node.pop('truncated',None)
            else:node['truncated']=flag
            if previous_omissions:node['omitted_fields']=copy.deepcopy(previous_omissions)
            else:node.pop('omitted_fields',None)
        for node, key, original in fields:
            node[key] = original[:limit]
            if len(original) > limit:
                node['truncated'] = True
                node.setdefault('omitted_fields', {})[key] = len(original) - limit
            else:
                node.get('omitted_fields', {}).pop(key, None)
            omitted += max(0, len(original) - limit)
        reduced['上下文省略']['omitted_characters'] = omitted
        return json.dumps(reduced, ensure_ascii=False, separators=(',', ':'))
    minimum = render(0)
    if len(minimum) > max_chars:
        raise ValueError('上下文预算不足以保留来源、版本和权限；请增加max_input或缩小检索范围')
    low, high = 0, max((len(original) for _, _, original in fields), default=0)
    best = minimum
    while low <= high:
        middle = (low + high) // 2
        candidate = render(middle)
        if len(candidate) <= max_chars:
            best = candidate; low = middle + 1
        else:
            high = middle - 1
    return best


def ask(question: str, project: str | None = None, session_id: str | None = None) -> dict:
    question = question.strip()
    if not question:
        raise ValueError("请输入问题")
    config = load_provider()
    if config.get("provider_type") != "openai-compatible" or not config.get("enabled"):
        raise ValueError("尚未配置可用AI Provider")
    bridge = context_request(question, project, session_id, "jiedu-ai-chat", "chatgpt")
    model_context = _model_context(bridge["context"], int(config.get("max_input", 32000)))
    system = "你是界渡AI对话桥。只能依据界渡提供的上下文回答。区分事实、推断与未知；证据不足时明确说明。不得声称已经修改知识，也不得绕过Owner确认。"
    payload = {"model": config["model"], "messages": [{"role": "system", "content": system + "\n\n界渡上下文：\n" + model_context}, {"role": "user", "content": question}], "temperature": 0.2}
    request = urllib.request.Request(_endpoint(config["base_url"]), data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers={"Content-Type": "application/json", "Authorization": "Bearer " + _secret(config["api_key_secret_ref"])}, method="POST")
    started = _now()
    try:
        with urllib.request.urlopen(request, timeout=config["timeout"]) as response:
            raw = json.loads(response.read().decode("utf-8"))
        answer = str(raw.get("choices", [{}])[0].get("message", {}).get("content", "")).strip()
        if not answer:
            raise ValueError("AI Provider未返回有效回答")
        usage = raw.get("usage") or {}
        record_call({"purpose": "ai_chat_bridge", "started_at": started, "finished_at": _now(), "provider": "openai-compatible", "model": config["model"], "success": True, "usage": usage})
    except Exception as exc:
        record_call({"purpose": "ai_chat_bridge", "started_at": started, "finished_at": _now(), "provider": "openai-compatible", "model": config.get("model"), "success": False, "error": redact_text(exc)})
        raise
    sent_context = json.loads(model_context)
    sources = [dict(row, path=row.get('source_path') or row.get('path'))
               for row in sent_context['相关知识'].get('results', [])]
    return {"answer": answer, "project": bridge["context"]["project"], "sources": sources, "knowledge_count": len(sources), "permissions": bridge["context"]["permissions"], "usage": usage, "provider": "OpenAI-compatible", "model": config["model"], "bridge_audit": bridge["audit"], "formal_write_performed": False}
