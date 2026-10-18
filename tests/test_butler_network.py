import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import test_butler_provider as provider_fixture
from knowledge_bridge.butler_network import WebSettings,WebReader
from knowledge_bridge.butler_provider import ProviderError


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        context=SimpleNamespace(state=Path(self.temp.name),workspace_id='fixture-web')
        self.secrets=provider_fixture.Secrets();self.settings=WebSettings(context,secrets=self.secrets,isolated=True)
        self.requests=[];self.body=b'<article>Fixture web text<script>hidden script</script></article>'
        self.media='text/html';self.redirect=None;self.search_redirect=False;self.gate=None;self.received=threading.Event()
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                owner.requests.append({'path':self.path,'key':self.headers.get('X-Subscription-Token')});owner.received.set()
                if owner.gate:owner.gate.wait(5)
                redirect=owner.redirect or ('/other' if owner.search_redirect and self.path.startswith('/search') else None)
                self.send_response(302 if redirect else 200)
                if redirect:self.send_header('Location',redirect)
                self.send_header('Content-Type','application/json' if self.path.startswith('/search') else owner.media);self.end_headers()
                body=json.dumps({'web':{'results':[{'title':'Fixture result','url':'https://example.org/fixture','description':'Fixture snippet'}]}}).encode() if self.path.startswith('/search') else owner.body
                try:self.wfile.write(body)
                except OSError:pass
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.server.daemon_threads=True
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.url='http://127.0.0.1:'+str(self.server.server_port)

    def reader(self):return WebReader(self.settings,cancel=threading.Event())

    def test_disabled_and_unconfigured_search_make_no_request(self):
        with self.assertRaises(PermissionError):self.reader().read(self.url+'/page')
        self.settings.configure(enabled=True)
        with self.assertRaises(ProviderError):self.reader().search('fixture')
        self.assertEqual([],self.requests)

    def test_real_web_receipt_filters_scripts_and_has_url_time_hash(self):
        self.settings.configure(enabled=True);result=self.reader().read(self.url+'/page')
        self.assertIn('Fixture web text',result['content']);self.assertNotIn('hidden script',result['content'])
        self.assertEqual(self.url+'/page',result['url']);self.assertEqual(64,len(result['version']))
        self.assertTrue(result['retrieved_at']);self.assertEqual(1,len(self.requests))

    def test_real_search_uses_dedicated_header_and_redirect_never_carries_key(self):
        self.settings.configure(enabled=True,search_endpoint=self.url+'/search',api_key='fixture-web-key')
        result=self.reader().search('fixture query',2)
        self.assertEqual('https://example.org/fixture',result['results'][0]['url'])
        self.assertIn('q=fixture+query',self.requests[0]['path']);self.assertEqual('fixture-web-key',self.requests[0]['key'])
        self.assertNotIn('fixture-web-key',self.settings.path.read_text('utf-8'))
        self.search_redirect=True
        with self.assertRaises(ProviderError):self.reader().search('fixture next')
        self.assertEqual(2,len(self.requests));self.assertFalse(any(row['path']=='/other' for row in self.requests))
        with self.assertRaises(ProviderError):self.settings.configure(enabled=True,search_endpoint=self.url+'/new')

    def test_private_redirect_binary_and_size_limit_are_blocked(self):
        self.settings.configure(enabled=True);self.redirect='http://192.168.1.1/private'
        with self.assertRaises(ProviderError):self.reader().read(self.url+'/page')
        self.assertEqual(1,len(self.requests));self.redirect=None;self.media='image/png'
        with self.assertRaises(ProviderError):self.reader().read(self.url+'/media')
        self.media='text/plain';self.body=b'x'*(512*1024+1)
        with self.assertRaises(ProviderError):self.reader().read(self.url+'/large')

    def test_revoke_while_waiting_headers_cancels_within_one_second(self):
        self.settings.configure(enabled=True);self.gate=threading.Event();self.addCleanup(self.gate.set)
        reader=self.reader();self.settings.on_change.append(reader.close);errors=[]
        def read():
            try:reader.read(self.url+'/page')
            except BaseException as error:errors.append(error)
        worker=threading.Thread(target=read,daemon=True);worker.start();self.assertTrue(self.received.wait(2))
        started=time.monotonic();self.settings.configure(enabled=False);worker.join(1)
        self.assertFalse(worker.is_alive());self.assertLess(time.monotonic()-started,1)
        self.assertTrue(any(isinstance(error,InterruptedError) for error in errors));self.gate.set()


if __name__=='__main__':unittest.main()
