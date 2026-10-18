import hashlib,json,tempfile,threading,time,unittest,socket
from unittest.mock import patch
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from knowledge_bridge.workspace import initialize_workspace
from knowledge_bridge.butler_runtime import LocalButlerRuntime
from knowledge_bridge.butler_model_manager import ModelManager

class ModelFilesTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.ctx=initialize_workspace(Path(self.temp.name)/'workspace')
        self.data=b'GGUF'+bytes(range(256))*16384;self.requests=[];self.slow=False;self.first=threading.Event();self.release=threading.Event();self.corrupt=False;self.range_bad=False;self.cut_once=False;self.http_fail=0;self.ignore_range=False
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*a):pass
            def do_GET(self):
                owner.requests.append((self.path,self.headers.get('Range'),self.headers.get('Authorization')))
                if self.path=='/asset':self.send_response(302);self.send_header('Location','/signed?fixture=public-signature');self.end_headers();return
                if owner.http_fail:owner.http_fail-=1;self.send_response(503);self.send_header('Content-Length','0');self.end_headers();return
                offset=int((self.headers.get('Range') or 'bytes=0-').split('=')[1].split('-')[0]);data=(b'X'+owner.data[1:]) if owner.corrupt else owner.data
                if owner.ignore_range:offset=0
                self.send_response(206 if offset else 200);self.send_header('Content-Length',str(len(data)-offset))
                if offset:self.send_header('Content-Range',f"bytes {offset+(1 if owner.range_bad else 0)}-{len(data)-1}/{len(data)}")
                self.end_headers()
                try:
                    self.wfile.write(data[offset:offset+1024*1024]);self.wfile.flush();owner.first.set()
                    if owner.cut_once:owner.cut_once=False;self.connection.shutdown(socket.SHUT_RDWR);self.connection.close();return
                    if owner.slow:owner.release.wait(3)
                    self.wfile.write(data[offset+1024*1024:]);self.wfile.flush()
                except OSError:pass
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.server.daemon_threads=True;threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.model={'id':'fixture-model','revision':'a'*40,'name':'Synthetic GGUF','bytes':len(self.data),'context_tokens':8192,'max_output_tokens':4096,'thinking_mode':'required','files':[{'name':'fixture.gguf','bytes':len(self.data),'sha256':hashlib.sha256(self.data).hexdigest(),'url':'http://127.0.0.1:'+str(self.server.server_port)+'/asset'}]}
        self.runtime=LocalButlerRuntime(self.ctx);self.manager=ModelManager(self.ctx,self.runtime,catalog=[self.model],isolated=True);self.addCleanup(self.manager.close)
    def finish(self):
        self.manager.worker.join(5);self.assertFalse(self.manager.worker.is_alive());return self.manager.status()['download']
    def test_signed_redirect_complete_sha_and_no_implicit_selection(self):
        self.manager.start('fixture-model','direct');state=self.finish();self.assertEqual('completed',state['state']);self.assertIsNone(self.manager.status()['active'])
        self.assertFalse(self.runtime.config_path.exists());self.assertEqual(self.data,self.manager._file(self.model,'fixture.gguf').read_bytes());self.assertTrue(any('?fixture=' in row[0] for row in self.requests));self.assertTrue(all(row[2] is None for row in self.requests))
    def test_partial_resume_exact_range_and_restart_interruption(self):
        part=self.manager._file(self.model,'fixture.gguf').with_suffix('.gguf.part');part.write_bytes(self.data[:1048576])
        self.manager.start('fixture-model','direct');self.assertEqual('completed',self.finish()['state']);self.assertTrue(any(row[1]=='bytes=1048576-' for row in self.requests))
        target=self.manager._file(self.model,'fixture.gguf');self.assertEqual(hashlib.sha256(self.data).hexdigest(),hashlib.sha256(target.read_bytes()).hexdigest())
    def test_wrong_sha_and_wrong_range_fail_without_installing(self):
        self.corrupt=True;self.manager.start('fixture-model','direct');self.assertEqual('failed',self.finish()['state']);self.assertEqual([],self.manager.status()['installed'])
        part=self.manager._file(self.model,'fixture.gguf').with_suffix('.gguf.part');part.write_bytes(self.data[:1048576]);self.corrupt=False;self.range_bad=True
        self.manager.start('fixture-model','direct');self.assertEqual('failed',self.finish()['state']);self.assertEqual(self.data[:1048576],part.read_bytes())
    def test_pause_preserves_real_partial_then_resume(self):
        self.slow=True;self.manager.start('fixture-model','direct');self.assertTrue(self.first.wait(1))
        deadline=time.monotonic()+1
        while self.manager.status()['download']['received']<1048576 and time.monotonic()<deadline:time.sleep(.01)
        self.manager.pause();self.release.set();state=self.finish();self.assertEqual('paused',state['state'])
        part=self.manager._file(self.model,'fixture.gguf').with_suffix('.gguf.part');self.assertGreater(part.stat().st_size,0)
        self.slow=False;self.manager.start('fixture-model','direct');self.assertEqual('completed',self.finish()['state'])
    def test_failed_full_file_can_redownload_without_deleting_bad_bytes(self):
        self.corrupt=True;self.manager.start('fixture-model','direct');self.assertEqual('failed',self.finish()['state']);before=len(self.requests)
        self.corrupt=False;self.manager.start('fixture-model','direct',restart_failed=True);self.assertEqual('completed',self.finish()['state']);self.assertGreater(len(self.requests),before)
        bad=list(self.manager._file(self.model,'fixture.gguf').parent.glob('*.failed-*'));self.assertEqual(1,len(bad));self.assertEqual(b'X',bad[0].read_bytes()[:1])
    def test_existing_import_copies_pinned_bytes_and_keeps_source(self):
        source=Path(self.temp.name)/'existing.gguf';source.write_bytes(self.data)
        self.manager.import_existing(str(source));self.assertEqual('completed',self.finish()['state']);installed=self.manager.status()['installed'][0]
        self.assertEqual('unverified',installed['thinking_mode']);self.assertEqual(self.data,source.read_bytes());self.assertIsNone(self.manager.status()['active'])
        with self.assertRaises(ValueError):self.manager.activate(installed['id'])
        self.assertFalse(self.runtime.config_path.exists())

    def test_proxy_choice_only_download_and_unknown_no_worker(self):
        with self.assertRaises(ValueError):self.manager.start('unknown')
        with self.assertRaises(ValueError):self.manager.start('fixture-model','hidden-proxy')
        self.assertIsNone(self.manager.worker)

class DownloadRetryTests(unittest.TestCase):
    setUp=ModelFilesTests.setUp
    finish=ModelFilesTests.finish
    def test_mid_body_cut_resumes_from_flushed_disk_bytes(self):
        self.cut_once=True;self.manager.start('fixture-model','direct');self.assertEqual('completed',self.finish()['state']);ranges=[r[1] for r in self.requests if r[1]];self.assertIn('bytes=1048576-',ranges)
    def test_existing_partial_http503_retries_206(self):
        part=self.manager._file(self.model,'fixture.gguf').with_suffix('.gguf.part');part.write_bytes(self.data[:65536]);self.http_fail=1;self.manager.start('fixture-model','direct');self.assertEqual('completed',self.finish()['state']);self.assertGreaterEqual(len([r for r in self.requests if r[1]=='bytes=65536-']),2)
    def test_pause_during_backoff_with_rapid_status_reads(self):
        part=self.manager._file(self.model,'fixture.gguf').with_suffix('.gguf.part');part.write_bytes(self.data[:65536]);self.http_fail=10;self.manager.start('fixture-model','direct');deadline=time.monotonic()+2
        while (self.manager.status()['download'] or {}).get('state')!='retrying' and time.monotonic()<deadline:time.sleep(.001)
        self.assertEqual('retrying',self.manager.status()['download']['state']);begin=time.monotonic();self.manager.pause();self.assertEqual('paused',self.finish()['state']);self.assertLess(time.monotonic()-begin,.5);self.assertEqual(self.data[:65536],part.read_bytes());self.assertEqual(1,len([r for r in self.requests if r[0].startswith('/signed')]))
    def test_200_ignores_range_preserves_partial(self):
        part=self.manager._file(self.model,'fixture.gguf').with_suffix('.gguf.part');part.write_bytes(self.data[:65536]);self.ignore_range=True;self.manager.start('fixture-model','direct');self.assertEqual('failed',self.finish()['state']);self.assertEqual(self.data[:65536],part.read_bytes())

class DownloadCancelTests(unittest.TestCase):
    setUp=ModelFilesTests.setUp
    finish=ModelFilesTests.finish
    def test_cancel_active_preserves_partial_and_late_records_cannot_overwrite(self):
        self.slow=True;self.manager.start('fixture-model','direct');self.assertTrue(self.first.wait(1));self.manager.cancel();self.release.set();state=self.finish();self.assertEqual('cancelled',state['state']);part=self.manager._file(self.model,'fixture.gguf').with_suffix('.gguf.part');self.assertGreater(part.stat().st_size,0)
        stale={**state,'state':'completed','message':'late completion'};self.manager._save(self.manager.root/'download.json',stale);self.assertEqual('cancelled',self.manager.status()['download']['state']);self.manager._save(self.manager.root/'download.json',{**stale,'run_id':'other-run'});self.assertEqual('cancelled',self.manager.status()['download']['state']);self.assertEqual([],self.manager.status()['installed']);self.assertEqual('cancelled',ModelManager(self.ctx,self.runtime,catalog=[self.model],isolated=True).status()['download']['state'])
    def test_cancel_retry_wait_immediate_and_explicit_resume_only(self):
        part=self.manager._file(self.model,'fixture.gguf').with_suffix('.gguf.part');part.write_bytes(self.data[:65536]);self.http_fail=10;self.manager.start('fixture-model','direct');deadline=time.monotonic()+2
        while self.manager.status()['download']['state']!='retrying' and time.monotonic()<deadline:time.sleep(.005)
        self.assertEqual('retrying',self.manager.status()['download']['state']);begin=time.monotonic();self.manager.cancel();self.assertEqual('cancelled',self.finish()['state']);self.assertLess(time.monotonic()-begin,.5);before=len(self.requests);time.sleep(.1);self.assertEqual(before,len(self.requests));self.assertEqual(self.data[:65536],part.read_bytes());self.http_fail=0;self.manager.start('fixture-model','direct');self.assertEqual('completed',self.finish()['state']);self.assertTrue(any(r[1]=='bytes=65536-' for r in self.requests))
    def test_cancel_hash_verification_prevents_install(self):
        started=threading.Event()
        def held_digest(path,event):
            started.set();event.wait(2)
            if event.is_set():raise InterruptedError('stopped')
            return hashlib.sha256(path.read_bytes()).hexdigest()
        with patch('knowledge_bridge.butler_model_manager.digest',side_effect=held_digest):
            self.manager.start('fixture-model','direct');self.assertTrue(started.wait(2));self.manager.cancel();self.assertEqual('cancelled',self.finish()['state'])
        self.assertEqual([],self.manager.status()['installed']);self.assertFalse(self.manager._file(self.model,'fixture.gguf').exists());self.assertEqual(len(self.data),self.manager._file(self.model,'fixture.gguf').with_suffix('.gguf.part').stat().st_size)
    def test_cancel_existing_failed_or_paused_keeps_bytes(self):
        from knowledge_bridge.butler_model_manager import save
        part=self.manager._file(self.model,'fixture.gguf').with_suffix('.gguf.part');part.write_bytes(self.data[:65536])
        for state in ['failed','paused']:
            save(self.manager.root/'download.json',{'run_id':state,'model_id':'fixture-model','state':state,'received':65536,'total':len(self.data)})
            self.assertEqual('cancelled',self.manager.cancel()['state']);self.assertEqual('cancelled',self.manager.status()['download']['state']);self.assertEqual(self.data[:65536],part.read_bytes())

if __name__=='__main__':unittest.main()
