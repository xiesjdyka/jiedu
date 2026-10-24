import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch
from knowledge_bridge import encoder_service as service

class FakeBackend:
    def __init__(self):self.loaded=False;self.entered=threading.Event();self.release=threading.Event()
    def status(self):return {'state':'loaded' if self.loaded else 'available_unloaded'}
    def warmup(self):self.loaded=True
    def encode(self,payload):
        self.loaded=True
        if payload['texts']==['block']:
            self.entered.set();self.release.wait(4)
        return [[float(len(text)),1.] for text in payload['texts']]

class EncoderServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.backend=FakeBackend()
        self.ready=threading.Event();self.thread=threading.Thread(target=service.serve,args=(self.root,self.backend,2,self.ready),daemon=True)
        self.thread.start();self.assertTrue(self.ready.wait(3))
    def tearDown(self):
        self.backend.release.set();service.stop(self.root);self.thread.join(4);self.temp.cleanup()
    def test_authenticated_encoding_and_status(self):
        result=service.call(self.root,'encode',{'texts':['中文','abc']})
        self.assertEqual(result['vectors'],[[2.,1.],[3.,1.]])
        self.assertEqual(service.status(self.root)['state'],'loaded')
    def test_no_auth_cannot_invoke(self):
        item=service.descriptor(self.root)
        req=urllib.request.Request(f"http://127.0.0.1:{item['port']}/encode",data=b'{}')
        with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req)
        self.assertEqual(error.exception.code,403);self.assertFalse(self.backend.loaded)
    def test_idle_status_polling_does_not_keep_model_alive(self):
        service.call(self.root,'warmup')
        for _ in range(14):service.status(self.root);time.sleep(.2)
        self.thread.join(2);self.assertFalse(self.thread.is_alive());self.assertFalse((self.root/'service.json').exists())
    def test_busy_rejected_without_second_model(self):
        t=threading.Thread(target=lambda:service.call(self.root,'encode',{'texts':['block']},timeout=5));t.start()
        self.assertTrue(self.backend.entered.wait(2));self.assertTrue(service.status(self.root)['busy'])
        with self.assertRaises(TimeoutError):service.call(self.root,'encode',{'texts':['x'],'lock_timeout':0})
        service.stop(self.root);self.assertTrue(self.thread.is_alive())
        self.backend.release.set();t.join(3);self.thread.join(3);self.assertFalse(self.thread.is_alive())
    def test_bad_input_retains_server(self):
        with self.assertRaises(RuntimeError):service.call(self.root,'encode',{'texts':[3]})
        self.assertTrue(service.status(self.root)['pid'])

    def test_ten_minute_default_and_no_status_wakeup(self):
        import inspect
        self.assertEqual(inspect.signature(service.serve).parameters['idle_seconds'].default,600)
        with tempfile.TemporaryDirectory() as empty,patch.object(service.subprocess,'Popen') as launch:
            self.assertEqual(service.status(Path(empty))['state'],'available_unloaded')
            launch.assert_not_called()

    def test_interactive_request_never_loads_cold_worker(self):
        with self.assertRaises(TimeoutError):service.call(self.root,'encode',{'texts':['query'],'require_loaded':True,'lock_timeout':.1})
        self.assertFalse(self.backend.loaded)

    def test_encode_captures_workspace_once_and_cold_race_does_not_restart(self):
        with patch.object(service,'root',side_effect=[self.root,Path('wrong-workspace')]) as root,patch.object(service,'ensure') as ensure,patch.object(service,'call',return_value={'vectors':[[1.,2.]]}) as call:
            service.encode(['query'],lock_timeout=.1)
            root.assert_called_once();ensure.assert_not_called()
            self.assertEqual(call.call_args.args[0],self.root)
            self.assertTrue(call.call_args.args[2]['require_loaded'])
        with patch.object(service,'root',side_effect=[self.root,Path('wrong-workspace')]) as root,patch.object(service,'ensure') as ensure,patch.object(service,'call',return_value={'vectors':[[1.,2.]]}) as call:
            service.encode(['background'])
            root.assert_called_once();ensure.assert_called_once_with(self.root)
            self.assertEqual(call.call_args.args[0],self.root)

    def test_failed_warmup_is_visible_then_recovers(self):
        with patch.object(self.backend,'warmup',side_effect=ImportError('fixture')):
            with self.assertRaises(RuntimeError):service.call(self.root,'warmup')
        state=service.status(self.root)
        self.assertEqual(state['state'],'load_failed');self.assertEqual(state['error'],'ImportError')
        service.call(self.root,'warmup')
        self.assertEqual(service.status(self.root)['state'],'loaded')

    def test_concurrent_warmup_requests_share_one_request(self):
        entered=threading.Event();release=threading.Event()
        def hold(*args,**kwargs):entered.set();release.wait(3)
        with patch.object(service,'root',return_value=self.root),patch.object(service,'status',return_value={'state':'available_unloaded'}),patch.object(service,'ensure',side_effect=hold) as ensure,patch.object(service,'call'):
            try:
                self.assertTrue(service.warmup());self.assertTrue(entered.wait(1))
                for _ in range(6):self.assertFalse(service.warmup())
                self.assertEqual(ensure.call_count,1)
            finally:
                release.set()
                deadline=time.monotonic()+2
                while service._warmup_pending and time.monotonic()<deadline:time.sleep(.01)
        self.assertFalse(service._warmup_pending)

if __name__=='__main__':unittest.main()
