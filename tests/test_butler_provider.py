import hashlib
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from knowledge_bridge.butler_provider import ButlerProviders, CompatibleTransport, ProviderError, endpoint


class Secrets:
    def __init__(self): self.values = {}
    def put(self, target, value): self.values[target] = value
    def get(self, target): return self.values.get(target, "")
    def delete(self, target): self.values.pop(target, None)


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.context = SimpleNamespace(state=Path(self.temp.name), workspace_id="synthetic-workspace")
        self.secrets = Secrets(); self.provider = ButlerProviders(self.context, secrets=self.secrets, isolated=True)
        self.requests = []; self.fragments = []; self.status = 200; self.received = threading.Event();self.headers_gate=None
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append(request); owner.received.set()
                if owner.headers_gate:owner.headers_gate.wait(5)
                self.send_response(owner.status); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
                if owner.status != 200: return
                if callable(owner.fragments): pieces = owner.fragments(request)
                else: pieces = owner.fragments
                try:
                    for delta, finish in pieces:
                        value = {"choices": [{"delta": delta, "finish_reason": finish}]}
                        self.wfile.write(("data: " + json.dumps(value) + "\n\n").encode()); self.wfile.flush()
                    self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush()
                except (OSError, BrokenPipeError): pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.addCleanup(self.server.server_close); self.addCleanup(self.server.shutdown)
        self.url = "http://127.0.0.1:" + str(self.server.server_port) + "/v1"

    def configure(self, **overrides):
        args = dict(selected="external", base_url=self.url, model="fixture-current-model", dialect="deepseek",
                    api_key="synthetic-key", tools=True, thinking=True)
        args.update(overrides); return self.provider.configure(**args)

    def transport(self):
        return CompatibleTransport(self.provider, self.provider.snapshot(), cancel=threading.Event())

    def test_cancel_during_dns_returns_without_waiting_or_sending_http(self):
        provider=ButlerProviders(self.context,secrets=self.secrets,isolated=False)
        with patch('knowledge_bridge.butler_provider.socket.getaddrinfo',return_value=[(2,1,6,'',('8.8.8.8',443))]):
            provider.configure(selected='external',base_url='https://fixture-public.example/v1',model='fixture-model',api_key='synthetic-key')
        started=threading.Event();release=threading.Event();cancel=threading.Event();errors=[]
        def slow_dns(*args,**kwargs):
            started.set();release.wait(3)
            return [(2,1,6,'',('8.8.8.8',443))]
        transport=CompatibleTransport(provider,provider.snapshot(),cancel=cancel)
        def execute():
            try:transport.complete([{'role':'user','content':'public fixture greeting'}])
            except Exception as error:errors.append(error)
        with patch('knowledge_bridge.butler_provider.socket.getaddrinfo',side_effect=slow_dns),patch('httpx.AsyncClient.stream') as outgoing:
            worker=threading.Thread(target=execute);worker.start();self.assertTrue(started.wait(1))
            begin=time.monotonic();cancel.set();transport.close();worker.join(1)
            self.assertFalse(worker.is_alive());self.assertLess(time.monotonic()-begin,1)
            self.assertEqual(1,len(errors));outgoing.assert_not_called()
            release.set();time.sleep(.05);outgoing.assert_not_called()

    def test_no_ambient_key_and_no_request_without_configuration(self):
        self.assertEqual("local", self.provider.snapshot()["selected"])
        with self.assertRaises(ProviderError): self.provider.connection(self.provider.snapshot())
        with self.assertRaises(ProviderError): self.configure(api_key=None)
        self.assertEqual([], self.requests); self.assertEqual({}, self.secrets.values)

    def test_credential_is_separate_hidden_and_endpoint_bound(self):
        public = self.configure()
        text = self.provider.path.read_text("utf-8")
        self.assertNotIn("synthetic-key", text); self.assertNotIn("credential_target", json.dumps(public))
        self.assertTrue(all(k.startswith("Jiedu.Butler.Provider.synthetic-workspace.") for k in self.secrets.values))
        with self.assertRaises(ProviderError): self.configure(base_url=self.url + "/new", api_key=None)
        old = self.provider.snapshot(); self.provider.clear_external()
        with self.assertRaises(ProviderError): self.provider.connection(old)
        self.assertEqual({}, self.secrets.values)

    def test_real_http_tool_roundtrip_preserves_supplier_reasoning(self):
        self.configure()
        def fragments(request):
            if len(request["messages"]) == 1:
                return [({"reasoning_content": "synthetic thought"}, None),
                        ({"tool_calls": [{"index": 0, "id": "call-fixture", "function": {"name": "lookup", "arguments": '{"q":'}}]}, None),
                        ({"tool_calls": [{"index": 0, "function": {"arguments": '"fixture"}'}}]}, "tool_calls")]
            return [({"content": "已读取合成资料"}, "stop")]
        self.fragments = fragments; receipts = []
        def dispatch(name, raw):
            self.assertEqual("lookup", name); self.assertEqual({"q": "fixture"}, json.loads(raw))
            return {"content": "synthetic evidence", "source": "fixture://document"}
        result, finish, messages = self.transport().run([{"role": "user", "content": "fixture question"}],
            dispatch=dispatch, tools=[{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}],
            thinking=True, receipt=receipts.append)
        self.assertEqual("stop", finish); self.assertEqual("已读取合成资料", result["content"])
        replay = self.requests[1]["messages"][1]
        self.assertEqual("synthetic thought", replay["reasoning_content"])
        self.assertEqual("", replay["content"]); self.assertEqual("call-fixture", self.requests[1]["messages"][2]["tool_call_id"])
        self.assertEqual(1, len(receipts)); self.assertNotIn("temperature", self.requests[0])
        self.assertEqual("fixture-current-model", self.requests[0]["model"])

    def test_probe_has_no_workspace_context_and_real_noop_roundtrip(self):
        self.configure()
        def fragments(request):
            if request["messages"][-1]["role"] != "tool":
                return [({"tool_calls": [{"index": 0, "id": "probe-1", "function": {"name": "probe_echo", "arguments": "{}"}}]}, "tool_calls")]
            return [({"content": "你好"}, "stop")]
        self.fragments = fragments
        self.assertEqual("observed", self.provider.probe(check_tools=True)["tools"])
        wire = json.dumps(self.requests, ensure_ascii=False)
        self.assertNotIn(str(self.context.state), wire); self.assertNotIn("synthetic-key", wire)
        self.assertNotIn("portrait", wire); self.assertEqual(2, len(self.requests))

    def test_unsupported_and_revoke_between_rounds_stop_requests(self):
        self.configure(tools=False, thinking=False)
        with self.assertRaises(ProviderError): self.transport().complete([], tools=[{}])
        with self.assertRaises(ProviderError): self.transport().complete([], thinking=True)
        old = self.transport(); self.provider.clear_external()
        with self.assertRaises(ProviderError): old.complete([])
        self.assertEqual([], self.requests)

    def test_cancel_before_send_and_http_error_sanitize(self):
        self.configure(); transport = self.transport(); transport.cancel.set()
        with self.assertRaises(ProviderError): transport.complete([])
        self.assertEqual([], self.requests)
        self.status = 401
        with self.assertRaises(ProviderError) as error: self.transport().complete([])
        self.assertNotIn("synthetic-key", str(error.exception)); self.assertIn("401", str(error.exception))

    def test_truncated_valid_tool_json_never_dispatches(self):
        self.configure();self.fragments=[({'tool_calls':[{'index':0,'id':'truncated','function':{'name':'write','arguments':'{}'}}]},'length')]
        executed=[]
        with self.assertRaises(ProviderError):self.transport().run([{'role':'user','content':'fixture'}],
            dispatch=lambda *args:executed.append(args),tools=[{'type':'function','function':{'name':'write','parameters':{'type':'object'}}}])
        self.assertEqual([],executed)

    def test_cancel_interrupts_wait_for_headers_immediately(self):
        self.configure();self.headers_gate=threading.Event();self.addCleanup(self.headers_gate.set)
        transport=self.transport();errors=[]
        def execute():
            try:transport.complete([{'role':'user','content':'fixture'}])
            except BaseException as exc:errors.append(exc)
        worker=threading.Thread(target=execute,daemon=True);worker.start()
        self.assertTrue(self.received.wait(2));started=time.monotonic()
        transport.cancel.set();transport.close();worker.join(1)
        self.assertFalse(worker.is_alive());self.assertLess(time.monotonic()-started,1)
        self.assertTrue(any(isinstance(error,InterruptedError) for error in errors));self.headers_gate.set()

    def test_each_send_rechecks_budget_and_permission_callback(self):
        self.configure();self.fragments=[({'content':'fixture'},'stop')];checks=[]
        def check(messages):
            checks.append(messages)
            if len(checks)>1:raise ProviderError('scope revoked')
        transport=CompatibleTransport(self.provider,self.provider.snapshot(),cancel=threading.Event(),before_send=check)
        transport.complete([{'role':'user','content':'fixture'}])
        with self.assertRaises(ProviderError):transport.complete([{'role':'user','content':'fixture'}])
        self.assertEqual(1,len(self.requests))
        with self.assertRaises(ProviderError):self.transport().complete([{'role':'user','content':'x'*9000}])
        self.assertEqual(1,len(self.requests))

    def test_queue_keeps_history_and_portrait_off_outbound_wire(self):
        from knowledge_bridge.workspace import initialize_workspace
        from knowledge_bridge.local_butler import LocalButler
        from unittest.mock import Mock
        context=initialize_workspace(Path(self.temp.name)/'workspace')
        providers=ButlerProviders(context,secrets=self.secrets,isolated=True)
        providers.configure(selected='external',base_url=self.url,model='fixture-model',api_key='synthetic-key')
        runtime=Mock();runtime.config.return_value={};runtime.status.return_value={'state':'stopped'}
        service=LocalButler(context,runtime=runtime,providers=providers,start_worker=False);self.addCleanup(service.close)
        sid=service.create_session()['id']
        with service.db() as db:
            db.execute("INSERT INTO messages(session,role,content,sources,created,job) VALUES(?,?,?,?,?,?)",(sid,'assistant','PRIVATE_OLD_CHAT','[]','fixture','old'))
        (context.system/'我的长期画像.md').write_text('PRIVATE_OWNER_PORTRAIT','utf-8')
        jid=service.submit(sid,'你好')['id'];service.cancel_event=threading.Event();service._update(jid,state='running')
        with service.db() as db:job=dict(db.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone())
        self.fragments=[({'content':'你好'},'stop')]
        service._execute(job)
        wire=json.dumps(self.requests,ensure_ascii=False)
        self.assertNotIn('PRIVATE_OLD_CHAT',wire);self.assertNotIn('PRIVATE_OWNER_PORTRAIT',wire)
        runtime.start.assert_not_called();runtime.close.assert_not_called()
        self.assertEqual('你好',service.conversation(sid)['messages'][-1]['content'])

    def test_endpoint_rejects_hostname_prefix_tricks_and_credentials(self):
        for url in ("http://localhost.evil.invalid", "http://127.0.0.1.evil.invalid", "https://u:p@example.org",
                    "https://example.org/?key=secret", "http://192.168.1.3"):
            with self.subTest(url=url), self.assertRaises(ProviderError): endpoint(url, isolated=True)
        with self.assertRaises(ProviderError): endpoint("https://127.0.0.1")


class CoreEvidenceTests(unittest.TestCase):
    def test_missing_core_returns_unknown_and_verified_core_returns_fixture_only(self):
        from knowledge_bridge import memory_control, core_bootstrap
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); path = root / "我的长期画像.md"; path.write_text("# 画像\n合成用户喜欢绘画。", "utf-8")
            row = {"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            with patch.object(memory_control, "VAULT", root), patch.object(core_bootstrap, "current", return_value={"source_files": []}):
                for intent in ("identity", "collaboration", "core_lesson"):
                    answer, sources = memory_control._core_answer(intent, "fixture")
                    self.assertIn("不知道", answer); self.assertEqual([], sources)
            with patch.object(memory_control, "VAULT", root), patch.object(core_bootstrap, "current", return_value={"source_files": [row]}):
                answer, sources = memory_control._core_answer("identity", "fixture")
                self.assertIn("合成用户喜欢绘画", answer); self.assertEqual([path.name], sources)
                path.write_text("changed", "utf-8")
                self.assertEqual([], memory_control._core_answer("identity", "fixture")[1])


if __name__ == "__main__": unittest.main()
