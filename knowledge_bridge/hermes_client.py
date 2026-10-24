"""Installed Hermes CLI bridge. Separate request state; no original-session resume."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import tempfile
import threading
import time

import yaml

KEYS = {'deepseek': 'DEEPSEEK_API_KEY', 'openai': 'OPENAI_API_KEY',
        'anthropic': 'ANTHROPIC_API_KEY', 'openrouter': 'OPENROUTER_API_KEY'}


def discover(*, with_key=False):
    roots = [Path(x) for x in (os.environ.get('HERMES_HOME'),
             str(Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'hermes'),
             str(Path.home()/'.hermes')) if x]
    for home in dict.fromkeys(roots):
        source = home/'hermes-agent'
        if not (source/'hermes_cli/main.py').is_file() or not (home/'config.yaml').is_file():
            continue
        try:
            raw = (home/'config.yaml').read_bytes()
            config = (yaml.safe_load(raw) or {}).get('model', {})
            provider, model = config.get('provider'), config.get('default')
            if config.get('base_url'):
                raise ValueError('此 Hermes 自定义地址暂不支持，请使用外部模型服务接入')
            if provider not in KEYS or not isinstance(model, str) or not model.strip():
                raise ValueError('此 Hermes 服务配置暂不支持，请使用外部模型服务接入')
            source = source.resolve()
            install_key = hashlib.sha256(str(source).encode()).hexdigest()[:16]
            facts = home/'installs'/install_key/'facts.json'
            if facts.is_file():
                environment = Path(json.loads(facts.read_text('utf-8'))['packages']['venv']['environment'])
            else:
                environment = source/'venv'
            python = environment/('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
            if not python.is_file() or not (environment/'pyvenv.cfg').is_file():
                raise ValueError('Hermes 运行环境未就绪，请先在 Hermes 中完成安装或修复')
            # Read only this selected provider's credential; never expose it in status or receipts.
            key = ''
            if with_key:
                from dotenv import dotenv_values
                key = dotenv_values(home/'.env').get(KEYS[provider]) or os.environ.get(KEYS[provider], '')
                if not key:
                    raise ValueError('Hermes 尚未配置可用凭据，请先在 Hermes 中完成模型设置')
            credential_stamp = str((home/'.env').stat().st_mtime_ns) if (home/'.env').exists() else 'environment'
            revision = hashlib.sha256(raw + str(python).encode() + credential_stamp.encode()).hexdigest()
            return dict(ready=True, model=model, provider=provider, revision=revision,
                        source=str(source), python=str(python), key_name=KEYS[provider], key=key,
                        base_url=config.get('base_url') or '')
        except (OSError, ValueError, KeyError, TypeError, AttributeError, yaml.YAMLError) as exc:
            if isinstance(exc, ValueError) and str(exc).startswith(('此 Hermes', 'Hermes ')):
                raise
            raise ValueError('Hermes 安装信息无法读取，请先打开 Hermes 检查安装') from None
    raise ValueError('未找到本机 Hermes；请先安装并在 Hermes 中配置模型')


def status():
    try:
        value = discover()
        return {k: value[k] for k in ('ready', 'model', 'provider', 'revision')}
    except ValueError as exc:
        return dict(ready=False, error=str(exc))


class HermesTransport:
    def __init__(self, state, expected, *, cancel, emit, before_send=lambda: None, runner=None, activity=lambda item:None):
        self.state = Path(state)/'hermes-requests'
        self.expected = expected
        self.cancel = cancel
        self.emit = emit
        self.before_send = before_send
        self.runner = runner
        self.activity = activity
        self.process = None
        self.last_result_error = ''
        self.last_exit_code = None
        self.last_diagnostics = []
        self.last_tool_name = ''
        self.model_send_checks = 0
        self.model_send_rejections = 0
        self.lock = threading.Lock()

    def close(self):
        with self.lock:
            process = self.process
            if process is None or process.poll() is not None:
                return
            if os.name == 'nt':
                subprocess.run(['taskkill.exe', '/PID', str(process.pid), '/T', '/F'],
                               capture_output=True, timeout=10, creationflags=0x08000000)
            else:
                process.kill()

    def complete(self, messages):
        connection = discover(with_key=True)
        if connection['revision'] != self.expected['revision']:
            raise ValueError('Hermes 模型配置已变化，请重新选择 Hermes 后再发送')
        if self.cancel.is_set():
            raise InterruptedError('已取消')
        self.before_send()
        events = queue.Queue(maxsize=128)
        stop_reader = threading.Event()
        prompt = json.dumps(messages, ensure_ascii=False)
        if len(prompt.encode('utf-8')) > 48000:
            raise ValueError('Hermes 本轮上下文过长，请缩短问题或新建对话')
        self.state.mkdir(parents=True, exist_ok=True)
        home = Path(tempfile.mkdtemp(prefix='request-', dir=self.state))
        bridge = None
        (home/'query.txt').write_text(prompt, 'utf-8')
        # Keep operating-system/network settings, but remove unrelated AI keys and app context.
        env = {k: v for k, v in os.environ.items() if not any(s in k.upper() for s in ('API_KEY','TOKEN','SECRET','HERMES','PYTHONPATH','PYTHONHOME','VIRTUAL_ENV'))}
        env.update(HERMES_HOME=str(home), HERMES_DISABLE_LAZY_INSTALLS='1', HERMES_SAFE_MODE='1',
                   HERMES_IGNORE_RULES='1', HERMES_IGNORE_USER_CONFIG='1', PYTHONDONTWRITEBYTECODE='1',
                   TEMP=str(home), TMP=str(home), UV_CACHE_DIR=str(home/'cache'), HF_HOME=str(home/'cache'))
        env[connection['key_name']] = connection['key']
        env['JIEDU_HERMES_KEY_NAME'] = connection['key_name']
        env['JIEDU_HERMES_PROVIDER'] = connection['provider']
        env['JIEDU_HERMES_MODEL'] = connection['model']
        if connection['base_url']:
            env['JIEDU_HERMES_BASE_URL'] = connection['base_url']
        from .hermes_tool_bridge import HermesToolServer
        try:
            bridge = HermesToolServer(self.runner,send_gate=self.before_send).start()
        except Exception:
            shutil.rmtree(home,ignore_errors=True)
            raise
        env['JIEDU_TOOL_PORT'] = str(bridge.port)
        env['JIEDU_TOOL_TOKEN'] = bridge.token
        env['JIEDU_TOOLS_ENABLED'] = '1' if self.runner else '0'
        setup = ('import sys;sys.path.insert(0,'+repr(str(Path(__file__).resolve().parent.parent))+');'
                 'sys.path.insert(0,'+repr(connection['source'])+');'
                 'from knowledge_bridge.hermes_direct_runner import main;sys.exit(main())')
        command = [connection['python'], '-I', '-B', '-X', 'utf8', '-c',
            setup, str(home/'query.txt')]
        process = None
        try:
            with self.lock:
                if self.cancel.is_set():
                    raise InterruptedError('已取消')
                process = self.process = subprocess.Popen(command, cwd=home, env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    creationflags=0x08000000 if os.name=='nt' else 0)
            def read():
                while not stop_reader.is_set():
                    line = process.stdout.readline(65537)
                    while not stop_reader.is_set():
                        try:
                            events.put(line, timeout=.2)
                            break
                        except queue.Full:
                            pass
                    if not line:
                        break
            reader = threading.Thread(target=read, daemon=True, name='hermes-output')
            reader.start()
            started = time.monotonic()
            total = 0
            result = None
            while True:
                if self.cancel.is_set():
                    raise InterruptedError('已取消')
                self.before_send()
                if time.monotonic()-started > 150:
                    raise ValueError('Hermes 超时，已停止本轮请求；可重试或检查 Hermes 网络')
                try:
                    line = events.get(timeout=.2)
                except queue.Empty:
                    continue
                if not line:
                    break
                total += len(line)
                if total > 4*1024*1024 or len(line)>65536:
                    raise ValueError('Hermes 返回内容超过本轮限制，已停止')
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    if len(self.last_diagnostics)<12:
                        self.last_diagnostics.append(line.decode('utf-8','replace')[:300].replace(connection['key'],'[已隐藏凭据]'))
                    continue
                kind = event.get('type')
                if kind in {'tool_use','tool_result'}:
                    self.last_tool_name = str(event.get('name') or '')[:80]
                    if not self.runner or event.get('name') not in {'product_operation','tool_search','tool_describe','tool_call'}:
                        raise ValueError('Hermes 请求了未开放的工具，本轮已停止')
                    if kind == 'tool_use' and event.get('name') == 'product_operation':
                        arguments = event.get('input') or {}
                        operation = arguments.get('operation') if isinstance(arguments,dict) else None
                        self.activity({'operation':operation if isinstance(operation,str) else 'product_operation',
                                       'title':'Hermes 请求界渡工具','state':'running'})
                    continue
                if kind == 'text' and isinstance(event.get('text'),str):
                    self.emit(event['text'].replace(connection['key'], '[已隐藏凭据]'))
                if kind == 'result':
                    result = event
            code = process.wait(timeout=5)
            self.last_exit_code = code
            self.last_result_error = str((result or {}).get('error') or '')[:500].replace(connection['key'], '[已隐藏凭据]')
            if bridge and bridge.rejected_send_count:
                raise InterruptedError('界渡授权已变化，本轮后续模型请求未发送')
            if code or not result or result.get('exit_code') != 0 or not str(result.get('text','')).strip():
                raise ValueError('Hermes 没有完成回答，请先检查 Hermes 是否能正常对话后重试')
            return result['text'].replace(connection['key'], '[已隐藏凭据]')
        finally:
            stop_reader.set()
            try:
                self.close()
            finally:
                try:
                    if process:
                        try:process.wait(timeout=10)
                        finally:process.stdout.close()
                finally:
                    with self.lock:self.process = None
                    try:
                        if bridge:
                            self.model_send_checks = bridge.authorize_count
                            self.model_send_rejections = bridge.rejected_send_count
                            bridge.close()
                    finally:shutil.rmtree(home, ignore_errors=True)
