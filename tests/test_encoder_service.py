import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
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
        with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(req)
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

if __name__=='__main__':unittest.main()
