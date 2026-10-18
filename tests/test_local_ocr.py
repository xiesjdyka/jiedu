import json,os,subprocess,tempfile,threading,time,unittest
from pathlib import Path
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from knowledge_bridge.local_ocr import recognize_document,runtime_root,runtime_python
from knowledge_bridge.workspace import initialize_workspace
from knowledge_bridge.protected_storage import EncryptedProtectedStorageProvider
from knowledge_bridge.knowledge_objects import KnowledgeObjectService
from knowledge_bridge.storage import StorageCancelledError
from fixture_documents import pdf


class LocalOCRTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        python=runtime_python(runtime_root())
        if not python.is_file():raise unittest.SkipTest('Prepared local OCR runtime required')
        cls.clear=subprocess.check_output([str(python),'-I','-c',"from PIL import Image,ImageDraw,ImageFont;import sys;im=Image.new('RGB',(900,180),'white');ImageDraw.Draw(im).text((30,40),'Offline fixture 895',font=ImageFont.load_default(size=42),fill='black');im.save(sys.stdout.buffer,format='PNG')"])
    def test_shapes_are_review_candidates_and_native_pdf_not_ocr(self):
        shapes=recognize_document(pdf(scan=True),'.pdf')
        self.assertEqual('partial',shapes.status)
        self.assertIn('symbol_only_candidate',shapes.markdown)
        self.assertFalse(shapes.metadata['coverage_complete'])
        self.assertIn('0.91202',shapes.markdown)
        native=recognize_document(pdf(),'.pdf')
        self.assertEqual('converted',native.status);self.assertFalse(native.metadata['automatic_ocr'])
        self.assertFalse(native.requires_ocr);self.assertIn('未执行 OCR',native.markdown)
    def test_timeout_and_cancellation_release_worker(self):
        data=self.clear
        with patch('knowledge_bridge.local_ocr.MAX_SECONDS',.05):
            r=recognize_document(data,'.png')
            self.assertEqual('time_limit',r.metadata['failure'])
        event=threading.Event()
        def progress(row):
            if row.get('stage')=='ocr_loading':event.set()
        with self.assertRaises(StorageCancelledError):recognize_document(data,'.png',progress=progress,cancel=event.is_set)
        self.assertEqual('converted',recognize_document(data,'.png').status)
    def test_missing_model_and_python_offline_guards(self):
        with tempfile.TemporaryDirectory() as root,patch.dict(os.environ,{'KNOWLEDGE_BRIDGE_OCR_RUNTIME':root}):
            self.assertEqual('ocr_unavailable',recognize_document(b'image','.png').status)
        from knowledge_bridge.ocr_worker import offline_memory_only
        for event,args in [('socket.connect',()),('socket.getaddrinfo',()),('open',('test','wb',os.O_WRONLY))]:
            with self.assertRaises(PermissionError):offline_memory_only(event,args)
    def test_lock_rolls_back_new_ciphertext_and_allows_retry(self):
        with tempfile.TemporaryDirectory(prefix='jiedu-ocr-lock-') as root:
            context=initialize_workspace(Path(root)/'workspace');provider=EncryptedProtectedStorageProvider(context)
            provider.initialize('fictional-ocr-password-895')
            service=KnowledgeObjectService(context,provider)
            external=Path(root)/'external';external.mkdir();source=external/'original.png';source.write_bytes(self.clear)
            preview=service.preview_import(str(external),'protected');started=threading.Event()
            def progress(row):
                if row.get('stage')=='ocr_loading':started.set()
            with ThreadPoolExecutor(max_workers=1) as pool:
                job=pool.submit(service.execute_import,preview['preview_id'],owner_confirmed=True,progress=progress)
                self.assertTrue(started.wait(20));begin=time.monotonic();provider.lock()
                with self.assertRaises(StorageCancelledError):job.result(timeout=5)
                self.assertLess(time.monotonic()-begin,2)
            self.assertEqual([],list(provider.objects_root.rglob('manifest.bin')))
            self.assertIsNone(service._registry()['catalog_head'])
            self.assertEqual(source.read_bytes(),self.clear)
    def test_partial_retry_is_idempotent_for_both_policies(self):
        for policy in ['protected','open']:
            with self.subTest(policy=policy),tempfile.TemporaryDirectory(prefix='jiedu-ocr-retry-') as root:
                context=initialize_workspace(Path(root)/'workspace');provider=EncryptedProtectedStorageProvider(context)
                provider.initialize('fictional-ocr-password-895');service=KnowledgeObjectService(context,provider)
                external=Path(root)/'external';external.mkdir();path=external/'shape.pdf';path.write_bytes(pdf(scan=True))
                for i in range(2):
                    result=service.execute_import(service.preview_import(str(external),policy)['preview_id'],owner_confirmed=True)
                    self.assertEqual(1,result['conversion']['counts']['partial'])
                    rows=service._catalog() if policy=='protected' else service._open_items()
                    relevant=[r for r in rows if 'shape.pdf' in r['title']]
                    self.assertEqual(2,len(relevant))
                    if policy=='open':self.assertEqual('open_derived',next(r for r in relevant if r['title'].endswith('.md'))['source_kind'])
                provider.lock()


if __name__=='__main__':unittest.main()
