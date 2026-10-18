from __future__ import annotations

import ctypes
import json
import os
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .manual_extraction import export_package, import_result, record_import_failure
from .provider_config import load as load_provider, record_call
from .redaction import redact_text
from .manual_extraction import session_state


def _now():
    return datetime.now(timezone.utc).isoformat()


def _secret(reference: str) -> str:
    if reference.startswith("env:"):
        value = os.environ.get(reference[4:], "")
        if not value:
            raise ValueError("环境变量中的API Key不存在")
        return value
    if reference.startswith("wincred:"):
        class CREDENTIALW(ctypes.Structure):
            _fields_ = [("Flags", ctypes.c_ulong), ("Type", ctypes.c_ulong), ("TargetName", ctypes.c_wchar_p), ("Comment", ctypes.c_wchar_p), ("LastWritten", ctypes.c_byte * 8), ("CredentialBlobSize", ctypes.c_ulong), ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", ctypes.c_ulong), ("AttributeCount", ctypes.c_ulong), ("Attributes", ctypes.c_void_p), ("TargetAlias", ctypes.c_wchar_p), ("UserName", ctypes.c_wchar_p)]
        pointer = ctypes.POINTER(CREDENTIALW)()
        if not ctypes.windll.advapi32.CredReadW(reference[8:], 1, 0, ctypes.byref(pointer)):
            raise ValueError("Windows凭据管理器中找不到API Key")
        try:
            size = pointer.contents.CredentialBlobSize
            return ctypes.string_at(pointer.contents.CredentialBlob, size).decode("utf-16-le" if size % 2 == 0 else "utf-8").rstrip("\x00")
        finally:
            ctypes.windll.advapi32.CredFree(pointer)
    raise ValueError("API Key必须通过环境变量或Windows凭据管理器引用")


def _endpoint(base: str) -> str:
    value = base.rstrip("/")
    if value.endswith("/chat/completions"):
        return value
    if value.endswith("/v1"):
        return value + "/chat/completions"
    return value + "/v1/chat/completions"


def _json_content(text: str) -> dict:
    clean = text.strip()
    match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", clean, re.S | re.I)
    if match:
        clean = match.group(1)
    value = json.loads(clean)
    if not isinstance(value, dict):
        raise ValueError("模型返回内容不是JSON对象")
    return value


def extract(session_ids: list[str], force: bool = False) -> dict:
    if not session_ids:
        raise ValueError("请先选择要提炼的会话")
    config = load_provider()
    if config["provider_type"] != "openai-compatible" or not config["enabled"]:
        raise ValueError("AI自动提炼尚未启用或配置不完整")
    package = export_package(force=force, session_ids=session_ids)
    content = Path(package["path"]).read_text(encoding="utf-8")
    if len(content) > config["max_input"]:
        raise ValueError("所选会话超过当前最大输入限制，请缩小范围或调整配置")
    payload = {"model": config["model"], "messages": [{"role": "system", "content": config["extraction_prompt"]}, {"role": "user", "content": content}], "temperature": 0, "response_format": {"type": "json_object"}}
    request = urllib.request.Request(_endpoint(config["base_url"]), data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers={"Content-Type": "application/json", "Authorization": "Bearer " + _secret(config["api_key_secret_ref"])}, method="POST")
    started = _now()
    try:
        with urllib.request.urlopen(request, timeout=config["timeout"]) as response:
            raw = json.loads(response.read().decode("utf-8"))
        result_payload = _json_content(raw["choices"][0]["message"]["content"])
        result = import_result(result_payload, provider="openai-compatible", provider_model=config["model"])
        record_call({"at": started, "provider":"openai-compatible", "model":config["model"], "package_id":package["package_id"], "status":"成功", "candidate_count":result["imported"]})
        return {"package": package, "import": result, "provider":"openai-compatible", "model":config["model"]}
    except Exception as exc:
        record_import_failure({"package_id": package["package_id"]}, exc)
        record_call({"at": started, "provider":"openai-compatible", "model":config["model"], "package_id":package["package_id"], "status":"失败", "error":redact_text(exc)[:300]})
        raise
