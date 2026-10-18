"""Explicit Butler providers. No ambient credentials or private probe context."""
from __future__ import annotations

import asyncio
import ctypes
import ipaddress
import json
import os
import socket
import threading
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

MAX_REQUEST_BODY_BYTES=48*1024*1024


class ProviderError(ValueError):
    pass


def documented_capabilities(config):
    """Exact official endpoint/model documentation, never a weight-identity claim."""
    value=dict(config)
    parsed=urlsplit(str(value.get('base_url','')))
    official=(value.get('dialect')=='deepseek' and parsed.scheme=='https' and
        parsed.hostname=='api.deepseek.com' and parsed.port in {None,443} and
        parsed.path.rstrip('/') in {'','/v1'} and not parsed.username and not parsed.password and
        not parsed.query and not parsed.fragment)
    if official and value.get('model') in {'deepseek-flash','deepseek-v4-pro'}:
        # https://api-docs.deepseek.com/guides/thinking_mode/
        if value.get('thinking_mode') in {None,'unverified'}:
            value['thinking_mode']='switchable'
        if value.get('thinking_mode') in {'switchable','required'}:value['thinking']=True
    return value


def supports_images(config):
    """Only documented official Flash image input is enabled by this adapter."""
    parsed=urlsplit(str(config.get('base_url','')))
    return (config.get('dialect')=='deepseek' and config.get('model')=='deepseek-flash' and
        parsed.scheme=='https' and parsed.hostname=='api.deepseek.com' and parsed.port in {None,443} and
        parsed.path.rstrip('/') in {'','/v1'} and not parsed.username and not parsed.password and
        not parsed.query and not parsed.fragment)


def message_input_estimate(messages):
    """Text UTF-8 bytes plus Flash's documented upper bound of 1024 tokens/image.

    Base64 transport bytes are not language-model tokens. The actual serialized
    body has a separate size limit below. Only this adapter's inline images pass.
    """
    bounded=[];images=0
    for message in messages:
        item=dict(message);content=item.get('content')
        if isinstance(content,list):
            parts=[]
            for part in content:
                if part.get('type')=='image_url':
                    image=part.get('image_url') or {};url=image.get('url','')
                    if message.get('role')!='user' or not isinstance(url,str) or not any(url.startswith('data:image/'+kind+';base64,') for kind in ('png','jpeg','gif','webp')):
                        raise ProviderError('图片必须是本轮选定的受支持本机图片，不接受外部图片链接')
                    images+=1;parts.append({'type':'text','text':'[本轮图片]'})
                else:parts.append(part)
            item['content']=parts
        bounded.append(item)
    if images>4:raise ProviderError('每轮最多4张图片')
    return len(json.dumps(bounded,ensure_ascii=False,separators=(',',':')).encode('utf-8'))+128+1024*images


_dns_slots=threading.BoundedSemaphore(8)

async def resolve_addresses(host,port):
    """A cancelled DNS wait cannot block loop shutdown or later send an HTTP request."""
    if not _dns_slots.acquire(blocking=False):raise ProviderError('地址核验繁忙，请稍后重试')
    loop=asyncio.get_running_loop();future=loop.create_future()
    def publish(value,error):
        if future.done():return
        if error:future.set_exception(ProviderError('无法核验服务地址'))
        else:future.set_result(value)
    def resolve():
        try:
            try:value=socket.getaddrinfo(host,port,type=socket.SOCK_STREAM);error=False
            except OSError:value=None;error=True
            try:loop.call_soon_threadsafe(publish,value,error)
            except RuntimeError:pass
        finally:_dns_slots.release()
    threading.Thread(target=resolve,daemon=True,name='butler-dns-check').start()
    return await asyncio.wait_for(future,15)


class WindowsSecrets:
    """Only targets supplied by this module; never enumerate credential storage."""
    class Credential(ctypes.Structure):
        _fields_ = [("Flags", ctypes.c_ulong), ("Type", ctypes.c_ulong),
                    ("TargetName", ctypes.c_wchar_p), ("Comment", ctypes.c_wchar_p),
                    ("LastWritten", ctypes.c_byte * 8), ("CredentialBlobSize", ctypes.c_ulong),
                    ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", ctypes.c_ulong),
                    ("AttributeCount", ctypes.c_ulong), ("Attributes", ctypes.c_void_p),
                    ("TargetAlias", ctypes.c_wchar_p), ("UserName", ctypes.c_wchar_p)]

    def put(self, target, value):
        if os.name != "nt": raise ProviderError("当前系统没有Windows凭据管理器")
        raw = value.encode("utf-16-le"); buffer = ctypes.create_string_buffer(raw)
        credential = self.Credential(0, 1, target, "界渡管家独立凭据", (ctypes.c_byte*8)(),
            len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)), 2, 0, None, None, "界渡管家")
        if not ctypes.windll.advapi32.CredWriteW(ctypes.byref(credential), 0):
            raise ProviderError("无法保存管家凭据")

    def get(self, target):
        if os.name != "nt": return ""
        pointer = ctypes.POINTER(self.Credential)()
        if not ctypes.windll.advapi32.CredReadW(target, 1, 0, ctypes.byref(pointer)): return ""
        try:
            c = pointer.contents
            return ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize).decode("utf-16-le")
        finally: ctypes.windll.advapi32.CredFree(pointer)

    def delete(self, target):
        if os.name == "nt": ctypes.windll.advapi32.CredDeleteW(target, 1, 0)


def endpoint(value, *, isolated=False, resolve_dns=True):
    """Require a clean public HTTPS endpoint; isolated tests may use loopback HTTP."""
    value = str(value).strip().rstrip("/")
    try:
        parsed = urlsplit(value); port = parsed.port
    except ValueError as exc: raise ProviderError("模型服务地址无效") from exc
    host = parsed.hostname or ""
    if (not host or parsed.username or parsed.password or parsed.query or parsed.fragment
            or "\\" in value or any(ord(c) < 33 for c in value)):
        raise ProviderError("模型服务地址无效")
    loopback = host in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (isolated and loopback and parsed.scheme == "http"):
        raise ProviderError("外部模型服务必须使用HTTPS")
    if not (isolated and loopback) and resolve_dns:
        try:
            addresses = [ipaddress.ip_address(x[4][0]) for x in socket.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)]
        except (OSError, ValueError) as exc: raise ProviderError("无法核验模型服务地址") from exc
        if not addresses or any(not addr.is_global for addr in addresses):
            raise ProviderError("外部模型服务不能指向本机或私有网络")
    return value


class ButlerProviders:
    def __init__(self, context, *, secrets=None, isolated=False):
        self.path = context.state / "local-butler" / "providers.json"
        self.target_prefix = "Jiedu.Butler.Provider." + str(context.workspace_id) + "."
        self.secrets = secrets or WindowsSecrets(); self.isolated = isolated
        self.lock = threading.RLock();self.on_change=[]

    def _load(self):
        if not self.path.exists(): return {"selected": "local", "revision": "initial", "external": None}
        try:
            value = json.loads(self.path.read_text("utf-8"))
            if value["selected"] not in {"local", "external", "hermes"}: raise ValueError()
            external=value.get('external')
            if external:
                if not isinstance(external,dict) or set(external)-{'base_url','model','dialect','context_tokens','max_output_tokens','thinking_mode','tools','thinking','native_search','data_scopes','credential_target','probe'}:raise ValueError()
                target=external.get('credential_target','')
                if not isinstance(target,str) or not target.startswith(self.target_prefix) or len(target)!=len(self.target_prefix)+32:raise ValueError()
            if value['selected']=='external' and not external:raise ValueError()
            return value
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ProviderError("管家模型配置损坏，请在设置中重新保存") from exc

    def status(self):
        with self.lock:
            value = self._load(); external = value.get("external")
            public = {k: v for k, v in (external or {}).items() if k != "credential_target"}
            if external:
                public=documented_capabilities(public)
                public['vision_supported']=supports_images(public)
                public['vision_source']='https://api-docs.deepseek.com/guides/vision/' if public['vision_supported'] else ''
            public["credential_present"] = bool(external and self.secrets.get(external["credential_target"]))
            from .hermes_client import status as hermes_status
            hermes = hermes_status()
            if hermes.get('ready') and value.get('hermes_revision') and value['hermes_revision'] != hermes.get('revision'):
                hermes = {**hermes, 'ready':False, 'error':'Hermes 配置已变化，请重新选择以确认使用新配置'}
            return {"selected": value["selected"], "revision": value["revision"], "external": public, "hermes":hermes}

    def select_hermes(self):
        from .hermes_client import status as hermes_status
        hermes = hermes_status()
        if not hermes['ready']:raise ProviderError(hermes['error'])
        with self.lock:
            self._save({**self._load(), 'selected':'hermes', 'hermes_revision':hermes['revision'], 'revision':uuid.uuid4().hex})
            public=self.status()
        for callback in self.on_change:callback()
        return public

    def _save(self, value):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name("." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2); handle.flush(); os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally: temporary.unlink(missing_ok=True)

    def configure(self, *, selected, base_url="", model="", dialect="generic", api_key=None,
                  context_tokens=8192, max_output_tokens=8192, thinking_mode=None, tools=False, thinking=False, native_search=False, data_scopes=()):
        if selected not in {"local", "external"}: raise ProviderError("请选择本机或外部模型")
        if dialect not in {"generic", "deepseek","qwen_template","dashscope"}: raise ProviderError("不支持的模型协议")
        if type(tools) is not bool or type(thinking) is not bool: raise ProviderError("能力开关必须是布尔值")
        if type(native_search) is not bool or native_search and dialect!='dashscope':raise ProviderError('当前接入方式未提供服务商自带搜索')
        scopes = sorted(set(data_scopes))
        if not set(scopes) <= {"formal", "evidence", "inbox", "selected_folders"}:
            raise ProviderError("不支持的外发资料范围")
        if type(context_tokens) is not int or not 1024 <= context_tokens <= 8388608:
            raise ProviderError("模型上下文容量须在1024至8388608词元之间")
        if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= min(context_tokens,1048576):raise ProviderError('单次输出上限须在1至模型上下文容量之间，最高1048576词元')
        thinking_mode=thinking_mode or ('switchable' if thinking else 'unverified')
        if thinking_mode not in {'switchable','required','unsupported','unverified'}:raise ProviderError('思考能力类型无效')
        if thinking_mode=='required':thinking=True
        if thinking_mode=='unsupported':thinking=False
        if api_key is not None and (not isinstance(api_key, str) or not api_key.strip() or
                len(api_key) > 4096 or any(ord(c) < 33 for c in api_key)):
            raise ProviderError("密钥格式无效")
        with self.lock:
            old = self._load(); revision = uuid.uuid4().hex; external = old.get("external")
            fresh_target = None
            try:
                if selected == "external":
                    url = endpoint(base_url, isolated=self.isolated)
                    if not isinstance(model, str) or not model.strip() or len(model) > 200:
                        raise ProviderError("请填写供应商当前模型标识")
                    if external and external.get("base_url") != url and api_key is None:
                        raise ProviderError("模型端点已变更，请为新端点重新填写密钥")
                    target = (external or {}).get("credential_target")
                    if api_key is not None:
                        target = fresh_target = self.target_prefix + revision
                        self.secrets.put(target, api_key)
                    if not target or not self.secrets.get(target): raise ProviderError("请单独配置管家模型密钥")
                    external = {"base_url": url, "model": model.strip(), "dialect": dialect,
                        "context_tokens": context_tokens, "max_output_tokens":max_output_tokens,"thinking_mode":thinking_mode,"tools": tools, "thinking": thinking,
                        "native_search":native_search,"data_scopes": scopes, "credential_target": target,
                        "probe": {"state": "untested"}}
                    external=documented_capabilities(external)
                self._save({"selected": selected, "revision": revision, "external": external})
            except Exception:
                if fresh_target: self.secrets.delete(fresh_target)
                raise
            if fresh_target and old.get("external") and old["external"]["credential_target"] != fresh_target:
                self.secrets.delete(old["external"]["credential_target"])
            public=self.status()
        for callback in self.on_change:callback()
        return public

    def select_existing_external(self):
        with self.lock:
            old=self._load();external=old.get('external')
            if not external or not self.secrets.get(external['credential_target']):raise ProviderError('请先添加并保存外部服务')
            self._save({**old,'selected':'external','revision':uuid.uuid4().hex});public=self.status()
        for callback in self.on_change:callback()
        return public

    def set_native_search(self,enabled):
        if type(enabled) is not bool:raise ProviderError('搜索开关无效')
        with self.lock:
            old=self._load();external=old.get('external')
            if old['selected']!='external' or not external or external.get('dialect')!='dashscope' or not self.secrets.get(external['credential_target']):raise ProviderError('当前已保存的接入方式未提供服务商自带搜索')
            self._save({**old,'external':{**external,'native_search':enabled},'revision':uuid.uuid4().hex});public=self.status()
        for callback in self.on_change:callback()
        return public

    def clear_external(self):
        with self.lock:
            old = self._load(); self._save({"selected": "local", "revision": uuid.uuid4().hex, "external": None})
            if old.get("external"): self.secrets.delete(old["external"]["credential_target"])
            public=self.status()
        for callback in self.on_change:callback()
        return public

    def snapshot(self):
        value = self.status()
        if value['selected']=='hermes' and not value['hermes']['ready']:
            raise ProviderError(value['hermes'].get('error','Hermes 未就绪'))
        if value["selected"] == "external" and not value["external"]["credential_present"]:
            raise ProviderError("管家外部模型凭据不可用")
        return value

    def connection(self, snapshot):
        with self.lock:
            current = self._load()
            if snapshot["revision"] != current["revision"] or current["selected"] != "external":
                raise ProviderError("模型配置已变更或撤销，请重新发起请求")
            external = current["external"]
            endpoint(external["base_url"], isolated=self.isolated,resolve_dns=False)
            key = self.secrets.get(external["credential_target"])
            if not key: raise ProviderError("管家模型凭据不可用")
            return external["base_url"] + "/chat/completions", key

    def list_models(self, *, base_url, api_key=None):
        base=endpoint(base_url,isolated=self.isolated,resolve_dns=False)
        if api_key is not None and (not isinstance(api_key,str) or not api_key.strip() or len(api_key)>4096 or any(ord(c)<33 for c in api_key)):raise ProviderError('模型密钥格式无效')
        with self.lock:
            external=self._load().get('external') or {}
            key=api_key or (self.secrets.get(external['credential_target']) if external.get('base_url')==base else '')
        if not key:raise ProviderError('请先填写该服务的密钥，再读取模型列表')
        async def fetch():
            parsed=urlsplit(base);url=base+'/models';headers={'Authorization':'Bearer '+key};extensions={}
            if not (self.isolated and parsed.hostname in {'localhost','127.0.0.1','::1'}):
                addresses=[ipaddress.ip_address(x[4][0]) for x in await resolve_addresses(parsed.hostname,parsed.port or 443)]
                if not addresses or any(not a.is_global for a in addresses):raise ProviderError('模型服务不能指向本机或私有网络')
                address=str(addresses[0]);authority='['+address+']' if ':' in address else address
                if parsed.port:authority+=':'+str(parsed.port)
                headers['Host']=parsed.netloc;extensions['sni_hostname']=parsed.hostname
                url=urlunsplit((parsed.scheme,authority,parsed.path.rstrip('/')+'/models','',''))
            async with httpx.AsyncClient(timeout=httpx.Timeout(15,connect=5),follow_redirects=False,trust_env=False) as client:
                async with client.stream('GET',url,headers=headers,extensions=extensions) as response:
                    if response.status_code in {404,405}:raise ProviderError('该服务不提供模型列表，请使用“自定义型号”填写服务商给出的名称')
                    if response.status_code in {401,403}:raise ProviderError('密钥未通过验证，请核对密钥和服务区域')
                    if response.status_code!=200:raise ProviderError('读取模型列表返回HTTP '+str(response.status_code)+'，可稍后重试或自定义型号')
                    body=bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body)+len(chunk)>256*1024:raise ProviderError('模型列表过大，请使用自定义型号')
                        body.extend(chunk)
            try:rows=json.loads(body).get('data')
            except (ValueError,AttributeError):raise ProviderError('服务返回的模型列表格式不兼容，请使用自定义型号')
            if not isinstance(rows,list):raise ProviderError('服务返回的模型列表格式不兼容，请使用自定义型号')
            metadata=[]
            for row in rows:
                if not isinstance(row,dict) or not isinstance(row.get('id'),str) or not 0<len(row['id'])<=200 or any(ord(c)<32 for c in row['id']):continue
                item={'id':row['id'],'name':str(row.get('name') or row['id'])[:200]}
                for field in ['context_window','max_output_tokens']:
                    if type(row.get(field)) is int and row[field]>0:item[field]=row[field]
                metadata.append(item)
            models=sorted({r['id'] for r in metadata})[:200]
            if not models:raise ProviderError('该密钥没有可选模型，请核对服务开通情况或使用自定义型号')
            return {'models':models,'metadata':metadata[:200]}
        try:return asyncio.run(asyncio.wait_for(fetch(),20))
        except ProviderError:raise
        except (httpx.HTTPError,OSError,TimeoutError):raise ProviderError('模型列表连接未完成，请核对网络、服务地址或区域；密钥未保存') from None

    def probe(self, *, check_tools=False, check_thinking=False):
        """Owner-triggered test sends fixed public text and a no-op schema only."""
        snapshot = self.snapshot()
        if snapshot["selected"] != "external": raise ProviderError("请先配置并选择外部模型")
        snapshot["external"]["tools"] = check_tools
        snapshot["external"]["thinking"] = check_thinking
        snapshot["external"]["native_search"] = False
        transport = CompatibleTransport(self, snapshot, cancel=threading.Event())
        schema = [{"type": "function", "function": {"name": "probe_echo", "description": "返回固定问候，无任何业务操作",
                  "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}]
        messages = [{"role": "user", "content": "你好。这是公开的连接测试，请简短回复你好。" +
                     ("请先调用probe_echo，然后简短回复。" if check_tools else "")}]
        outcome = {"state": "failed", "chat": False, "tools": "not_tested", "thinking": "not_tested"}
        try:
            answer, finish = transport.complete(messages, max_tokens=128, thinking=check_thinking,
                                                 tools=schema if check_tools else None)
            outcome["chat"] = bool(answer.get("content") or answer.get("tool_calls"))
            if check_thinking: outcome["thinking"] = "observed" if answer.get("reasoning_content") else "not_observed"
            if check_tools:
                calls = answer.get("tool_calls", [])
                if len(calls) != 1 or calls[0]["function"]["name"] != "probe_echo" or json.loads(calls[0]["function"]["arguments"]) != {}:
                    outcome["tools"] = "not_observed"
                else:
                    messages.extend([answer, {"role": "tool", "tool_call_id": calls[0]["id"], "content": '{"greeting":"你好"}'}])
                    final, _ = transport.complete(messages, max_tokens=128, thinking=check_thinking, tools=schema)
                    outcome["tools"] = "observed" if final.get("content") and not final.get("tool_calls") else "incomplete"
            outcome["state"] = "completed"
        except (ProviderError, ValueError):
            outcome["error"] = "探测未完成；请核对端点、模型和供应商协议。没有发送工作区资料。"
        finally: transport.close()
        with self.lock:
            current = self._load()
            if current["revision"] != snapshot["revision"]: raise ProviderError("探测期间模型配置已变更，结果未保存")
            current["external"]["probe"] = outcome; self._save(current)
        return outcome


class CompatibleTransport:
    """One cancellable HTTP stream and bounded, lossless assistant tool replay."""
    def __init__(self, providers, snapshot, *, cancel, emit=lambda channel, text: None, before_send=lambda messages:None):
        self.providers = providers; self.snapshot = snapshot; self.cancel = cancel; self.emit = emit
        self.lock = threading.Lock(); self.response = None; self.client = None
        self.before_send=before_send;self.loop=None;self.task=None

    def close(self):
        with self.lock:
            if self.loop and self.task:
                try:self.loop.call_soon_threadsafe(self.task.cancel)
                except RuntimeError:pass # Completion already closed its loop.
                return
            if self.response: self.response.close()
            if self.client: self.client.close()

    def complete(self, messages, *, max_tokens=512, thinking=False, tools=None):
        config = self.snapshot["external"]
        if tools and not config["tools"]: raise ProviderError("该模型尚未声明工具调用能力，请先探测并配置")
        mode=config.get('thinking_mode')
        if mode=='required':thinking=True
        if mode=='unsupported' and thinking:raise ProviderError('该模型不支持思考')
        if thinking and not config["thinking"]: raise ProviderError("该模型尚未声明思考能力")
        if thinking and mode!='required' and config["dialect"] not in {"deepseek","llama_cpp","qwen_template","dashscope"}:
            raise ProviderError("通用兼容协议未定义思考参数，请关闭思考或选择供应商协议")
        if self.cancel.is_set(): raise ProviderError("请求已取消")
        count=self.before_send(messages)
        has_images=any(isinstance(message.get('content'),list) and any(part.get('type')=='image_url' for part in message['content']) for message in messages)
        if has_images and not supports_images(config):raise ProviderError('当前接入未支持图片，未发送；不会自动改用OCR')
        request_bytes=message_input_estimate(messages)
        if tools:request_bytes+=len(json.dumps(tools,ensure_ascii=False).encode('utf-8'))
        input_tokens=count if type(count) is int else request_bytes
        if type(max_tokens) is not int or max_tokens<1 or input_tokens>=config['context_tokens']:
            raise ProviderError("本轮请求和工具记录超出已配置的模型上下文额度")
        max_tokens=min(max_tokens,config['context_tokens']-input_tokens,config.get('max_output_tokens',8192))
        self.last_budget={'input_tokens':input_tokens,'context_tokens':config['context_tokens'],'max_output_tokens':max_tokens,
            'count_kind':'local_tokenizer' if type(count) is int else 'conservative_utf8_bytes_plus_1024_per_image' if has_images else 'conservative_utf8_byte_estimate'}
        url, key = self.providers.connection(self.snapshot)
        parsed = urlsplit(url)
        host = parsed.hostname
        request_headers = {"Authorization": "Bearer " + key}
        extensions = {}
        payload = {"model": config["model"], "messages": messages, "max_tokens": max_tokens, "stream": True}
        if config["dialect"] == "deepseek": payload["thinking"] = {"type": "enabled" if thinking else "disabled"}
        if config['dialect']=='llama_cpp':
            payload['chat_template_kwargs']={'enable_thinking':thinking};payload['reasoning_format']='deepseek'
        if config['dialect']=='qwen_template':payload['chat_template_kwargs']={'enable_thinking':thinking}
        if config['dialect']=='dashscope':payload['enable_thinking']=thinking
        if config.get('native_search'):
            if config['dialect']!='dashscope':raise ProviderError('当前接入方式未提供服务商自带搜索')
            payload['enable_search']=True
        if not thinking: payload["temperature"] = 0.2
        if tools: payload["tools"] = tools
        if len(json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode('utf-8'))>MAX_REQUEST_BODY_BYTES:
            raise ProviderError('请求正文超过48MiB，未发送，请减少图片或文字')
        result = {"role": "assistant", "content": ""}; calls = {}; reasoning = ""; finish = None
        async def request():
            nonlocal reasoning,finish,url
            with self.lock:
                self.loop=asyncio.get_running_loop();self.task=asyncio.current_task()
                if self.cancel.is_set():raise asyncio.CancelledError()
            # Resolve in the cancellable request task and pin the verified address.
            if not (self.providers.isolated and host in {"localhost", "127.0.0.1", "::1"}):
                try:
                    resolved=await resolve_addresses(host,parsed.port or 443)
                    addresses=[ipaddress.ip_address(x[4][0]) for x in resolved]
                except (OSError,ValueError,TimeoutError) as exc:raise ProviderError('无法核验模型服务地址') from exc
                if not addresses or any(not address.is_global for address in addresses):raise ProviderError('模型服务地址已指向私有网络，连接已阻止')
                address=str(addresses[0]);authority='['+address+']' if ':' in address else address
                if parsed.port:authority+=':'+str(parsed.port)
                request_headers['Host']=parsed.netloc;extensions['sni_hostname']=host
                url=urlunsplit((parsed.scheme,authority,parsed.path,'',''))
            async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=15), follow_redirects=False, trust_env=False) as client:
                with self.lock: self.client = client
                async with client.stream("POST", url, headers=request_headers, json=payload, extensions=extensions) as response:
                    with self.lock: self.response = response
                    if not 200 <= response.status_code < 300:
                        raise ProviderError(f"模型服务返回HTTP {response.status_code}；请核对模型、能力和配置")
                    if "text/event-stream" not in response.headers.get("content-type", ""):
                        raise ProviderError("模型未返回兼容的流式响应")
                    total = 0; saw_done = False
                    async for line in response.aiter_lines():
                        if self.cancel.is_set(): raise ProviderError("请求已取消")
                        total += len(line.encode("utf-8"))
                        if total > 4 * 1024 * 1024: raise ProviderError("模型响应超出安全额度")
                        if not line.startswith("data:"): continue
                        data = line[5:].strip()
                        if data == "[DONE]": saw_done = True; break
                        try: event = json.loads(data)
                        except ValueError as exc: raise ProviderError("模型流式响应格式无效") from exc
                        if "error" in event: raise ProviderError("模型服务报告请求失败")
                        choices = event.get("choices", [])
                        if not choices: continue
                        choice = choices[0]; delta = choice.get("delta") or {}
                        for source in [event.get('search_info'),choice.get('search_info'),delta.get('search_info'),delta.get('annotations'),delta.get('citations')]:
                            if source is not None and len(json.dumps(source,ensure_ascii=False).encode())<=8192:
                                result.setdefault('provider_search_sources',[])
                                if len(result['provider_search_sources'])<16:result['provider_search_sources'].append(source)
                        finish = choice.get("finish_reason") or finish
                        for channel in ("content", "reasoning_content"):
                            value = delta.get(channel) or ""
                            if not isinstance(value, str): raise ProviderError("模型输出格式无效")
                            if channel == "content": result["content"] += value
                            else: reasoning += value
                            if value: self.emit("answer" if channel == "content" else "reasoning", value)
                        for fragment in delta.get("tool_calls", []):
                            index = fragment.get("index")
                            if type(index) is not int or not 0 <= index < 16: raise ProviderError("工具调用序号无效")
                            call = calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                            if fragment.get("type", "function") != "function": raise ProviderError("工具调用类型不支持")
                            for name in ("id",): call[name] += fragment.get(name) or ""
                            for name in ("name", "arguments"):
                                call["function"][name] += (fragment.get("function") or {}).get(name) or ""
                    if not saw_done and not finish: raise ProviderError("模型响应中断，未收到完成记录")
        try:
            async def bounded_request():await asyncio.wait_for(request(),timeout=180)
            asyncio.run(bounded_request())
            if calls:
                result["tool_calls"] = [calls[i] for i in sorted(calls)]
                ids = [c["id"] for c in result["tool_calls"]]
                if any(not c["id"] or not c["function"]["name"] for c in result["tool_calls"]) or len(ids) != len(set(ids)):
                    raise ProviderError("工具调用身份无效")
            # DeepSeek needs this field on every assistant turn when tools are present.
            if reasoning or (tools and config["dialect"] == "deepseek"): result["reasoning_content"] = reasoning
            return result, finish
        except asyncio.CancelledError as exc:
            raise InterruptedError('请求已取消或模型配置已撤销') from exc
        except TimeoutError as exc:
            raise ProviderError('模型本轮请求超过180秒，已停止连接，可调整请求后重试') from exc
        except (httpx.HTTPError, OSError) as exc:
            raise ProviderError("模型连接中断或超时，请检查服务后重试") from exc
        finally:
            with self.lock: self.response = None; self.client = None;self.loop=None;self.task=None

    def run(self, messages, *, dispatch, tools, max_tokens=512, thinking=False, rounds=8, receipt=lambda item: None):
        messages = [dict(message) for message in messages]
        search_sources=[]
        for round_index in range(rounds):
            assistant, finish = self.complete(messages, max_tokens=max_tokens, thinking=thinking, tools=tools)
            search_sources.extend(assistant.pop('provider_search_sources',[])[:16-len(search_sources)])
            messages.append(assistant)
            if not assistant.get("tool_calls"):
                result=dict(assistant)
                if search_sources:result['provider_search_sources']=search_sources
                return result, finish, messages
            if finish!='tool_calls':raise ProviderError('模型工具回合未完整结束，未执行任何工具')
            for call in assistant["tool_calls"]:
                if self.cancel.is_set(): raise ProviderError("请求已取消")
                # dispatch owns schema validation and authorization; model arguments grant nothing.
                output = dispatch(call["function"]["name"], call["function"]["arguments"])
                text = json.dumps(output, ensure_ascii=False, separators=(",", ":"))
                if len(text.encode()) > 128 * 1024: raise ProviderError("工具结果超出本轮额度")
                item = {"role": "tool", "tool_call_id": call["id"], "content": text}
                messages.append(item); receipt({"round": round_index, "tool": call["function"]["name"], "result": output})
            if message_input_estimate(messages) > 3 * 1024 * 1024:
                raise ProviderError("工具回合上下文超出本轮额度")
        raise ProviderError("已达到工具回合上限，请检查活动记录后继续")


class RuntimeConnection:
    """Owned local llama.cpp endpoint through the same tool-round transport."""
    isolated=True
    def __init__(self,runtime,context_tokens):
        self.runtime=runtime
        self.snapshot={'selected':'local','external':{'model':'jiedu-local-butler','dialect':'llama_cpp',
            'context_tokens':context_tokens,'max_output_tokens':runtime.config().get('output_capacity',context_tokens),'tools':True,'thinking':True}}
    def connection(self,snapshot):
        if self.runtime.status().get('state')!='ready':raise ProviderError('本机模型尚未就绪')
        authorization=self.runtime.headers().get('Authorization','')
        if not authorization.startswith('Bearer '):raise ProviderError('本机模型私有凭据不可用')
        return self.runtime.endpoint()+'/v1/chat/completions',authorization[7:]
