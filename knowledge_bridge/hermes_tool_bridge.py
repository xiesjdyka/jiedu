"""One request's bounded Hermes tool bridge. The Hermes child has no workspace access."""
from __future__ import annotations

import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import secrets
import threading

TOOLSET = 'jiedu-request-tools'
TOOL = 'product_operation'
ALLOWED = frozenset({
    'product_operations', 'product_capabilities', 'product_status', 'source_archive_progress',
    'source_archive_resume', 'knowledge_search', 'source_read',
    'candidate_submit', 'web_search', 'web_read', 'folder_grants', 'folder_list', 'folder_read',
})
MAX_REQUEST = 150_000
MAX_RESULT = 300_000


def install_hermes_tool():
    """Called inside the isolated Hermes CLI before its main entry point."""
    from tools.registry import registry
    import toolsets

    def invoke(args, **_kwargs):
        try:
            request = json.dumps(args, ensure_ascii=False).encode('utf-8')
            if len(request) > MAX_REQUEST:
                return json.dumps({'ok': False, 'state': 'invalid_arguments', 'message': '工具参数过长，未执行'}, ensure_ascii=False)
            connection = http.client.HTTPConnection('127.0.0.1', int(os.environ['JIEDU_TOOL_PORT']), timeout=135)
            try:
                connection.request('POST', '/operation', body=request, headers={
                    'Content-Type': 'application/json', 'Authorization': 'Bearer ' + os.environ['JIEDU_TOOL_TOKEN']})
                response = connection.getresponse()
                body = response.read(MAX_RESULT + 1)
                if response.status != 200 or len(body) > MAX_RESULT:
                    raise ValueError('bridge response unavailable')
                return body.decode('utf-8')
            finally:
                connection.close()
        except Exception:
            return json.dumps({'ok': False, 'state': 'failed', 'message': '界渡工具连接未完成；可在界渡活动记录检查'}, ensure_ascii=False)

    schema = {'name': TOOL, 'description': '调用界渡已授权操作。先用 product_operations 查询操作参数；仅工具返回 ok=true 时可声称完成。',
              'parameters': {'type': 'object', 'properties': {
                  'operation': {'type': 'string', 'enum': sorted(ALLOWED)},
                  'arguments': {'type': 'string', 'description': '操作参数的 JSON 对象字符串'}},
                  'required': ['operation', 'arguments'], 'additionalProperties': False}}
    registry.register(name=TOOL, toolset=TOOLSET, schema=schema, handler=invoke,
                      description=schema['description'], execution_timeout=140)
    toolsets.TOOLSETS[TOOLSET] = {'tools': [TOOL], 'includes': []}
    if toolsets.resolve_toolset(TOOLSET) != [TOOL]:
        raise RuntimeError('Hermes 工具隔离未就绪')


def authorize_model_send():
    """Synchronous parent gate immediately before every Hermes model attempt."""
    connection = http.client.HTTPConnection('127.0.0.1', int(os.environ['JIEDU_TOOL_PORT']), timeout=10)
    try:
        connection.request('POST','/authorize-send',body=b'{}',headers={
            'Content-Type':'application/json','Authorization':'Bearer '+os.environ['JIEDU_TOOL_TOKEN']})
        response=connection.getresponse()
        response.read(1024)
        if response.status!=200:
            raise InterruptedError('界渡授权已变化，模型请求未发送')
    finally:
        connection.close()


class HermesToolServer:
    def __init__(self, runner=None, *, send_gate=lambda:None):
        self.runner = runner
        self.send_gate = send_gate
        self.authorize_count = 0
        self.rejected_send_count = 0
        self.token = secrets.token_urlsafe(32)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                if self.path not in {'/operation','/authorize-send'} or self.headers.get('Authorization') != 'Bearer ' + owner.token:
                    self.send_error(403)
                    return
                if self.path=='/authorize-send':
                    try:
                        owner.authorize_count += 1
                        owner.send_gate()
                        self.send_response(200)
                    except Exception:
                        owner.rejected_send_count += 1
                        self.send_response(403)
                    self.send_header('Content-Length','0')
                    self.end_headers()
                    return
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= MAX_REQUEST:
                        raise ValueError()
                    args = json.loads(self.rfile.read(size))
                    if not isinstance(args, dict) or set(args) != {'operation', 'arguments'} or not all(isinstance(v, str) for v in args.values()):
                        raise ValueError()
                    operation = args['operation']
                    if owner.runner is None or operation not in ALLOWED:
                        result = {'ok': False, 'state': 'unsupported', 'message': '此 Hermes 操作未开放，未执行'}
                    elif operation == 'product_operations':
                        result = owner.runner.dispatch_model(TOOL, json.dumps(args, ensure_ascii=False))
                        if result.get('ok') and isinstance(result.get('result'), dict):
                            catalog = result['result']
                            parameters = json.loads(args['arguments'])
                            query = (parameters.get('query') or '').casefold().strip()
                            limit = parameters.get('limit', 6)
                            available = [item for item in owner.runner.registry.tools.values()
                                         if item.name in ALLOWED and (not query or query in (item.name+' '+item.title+' '+item.area).casefold())]
                            catalog['operations'] = [{'name':item.name,'title':item.title,'mutation':item.mutation,
                                                      'parameters':item.arguments.model_json_schema()}
                                                     for item in available[:limit]]
                            catalog['more'] = len(available)>limit
                    else:
                        result = owner.runner.dispatch_model(TOOL, json.dumps(args, ensure_ascii=False))
                except (ValueError, TypeError, UnicodeError):
                    result = {'ok': False, 'state': 'invalid_arguments', 'message': '工具参数格式无效，未执行'}
                except InterruptedError:
                    result = {'ok': False, 'state': 'cancelled', 'message': '请求已取消，未执行'}
                except Exception:
                    result = {'ok': False, 'state': 'failed', 'message': '界渡工具未完成；请查看活动记录'}
                body = json.dumps(result, ensure_ascii=False, default=str).encode('utf-8')
                if len(body) > MAX_RESULT:
                    body = json.dumps({'ok': False, 'state': 'result_withheld', 'message': '工具结果超过本轮容量，正文未发送'}, ensure_ascii=False).encode('utf-8')
                try:
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json; charset=utf-8')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.server.block_on_close = False
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True, name='hermes-jiedu-tool')

    @property
    def port(self):
        return self.server.server_address[1]

    def start(self):
        self.thread.start()
        return self

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
