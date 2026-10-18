import hashlib
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from knowledge_bridge.document_converter import ConversionResult
from knowledge_bridge.knowledge_objects import KnowledgeObjectService, KnowledgeObjectJobManager
from knowledge_bridge.protected_storage import EncryptedProtectedStorageProvider
from knowledge_bridge.retrieval import RetrievalService
from knowledge_bridge.workspace import initialize_workspace


class ProductChainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.context = initialize_workspace(self.root / 'workspace')
        self.service = KnowledgeObjectService(self.context, EncryptedProtectedStorageProvider(self.context))
        self.source = self.root / 'samples'
        self.source.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_repeat_import_links_and_scope_preserve_originals(self):
        source = self.source / 'note.txt'
        source.write_text('AuroraTest fictional information', 'utf-8')
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        result = self.service.execute_import(self.service.preview_import(str(self.source), 'open')['preview_id'], owner_confirmed=True)
        first = result['conversion']['items'][0]
        self.assertTrue((self.context.root / first['original_path']).is_file())
        self.assertTrue((self.context.root / first['derived_path']).is_file())
        before = sorted(str(p) for p in self.context.inbox.rglob('*') if p.is_file())
        repeat = self.service.execute_import(self.service.preview_import(str(self.source), 'open')['preview_id'], owner_confirmed=True)
        second = repeat['conversion']['items'][0]
        self.assertEqual('already_imported', second['status'])
        self.assertEqual(first['original_path'], second['original_path'])
        self.assertEqual(before, sorted(str(p) for p in self.context.inbox.rglob('*') if p.is_file()))
        self.assertEqual(digest, hashlib.sha256(source.read_bytes()).hexdigest())
        retrieval = RetrievalService(self.context, vector_backend=False)
        self.assertEqual([], retrieval.search('AuroraTest', scope='formal')['results'])
        inbox = retrieval.search('AuroraTest', scope='inbox')['results']
        self.assertEqual(1, len(inbox))
        self.assertNotIn('source_sha256', inbox[0]['content'])
        source.write_text('different content', 'utf-8')
        self.assertEqual(1, self.service.preview_import(str(self.source), 'open')['conflicts'])

    def test_listing_does_not_wait_for_converter_or_hash_files(self):
        (self.source / 'note.txt').write_text('fictional', 'utf-8')
        preview = self.service.preview_import(str(self.source), 'open')
        entered, release = threading.Event(), threading.Event()
        def convert(path):
            entered.set()
            release.wait(3)
            return ConversionResult('converted', 'txt', 'fixture', markdown='fictional')
        with patch.object(self.service.converter, 'convert', side_effect=convert):
            jobs = KnowledgeObjectJobManager(self.service)
            job = jobs.start('import', preview['preview_id'], owner_confirmed=True)
            self.assertTrue(entered.wait(2))
            try:
                start = time.monotonic()
                with patch('knowledge_bridge.knowledge_objects._sha_file', side_effect=AssertionError('list must not hash')):
                    self.assertTrue(self.service.list_objects()['items'])
                self.assertLess(time.monotonic() - start, .5)
                jobs.cancel(job['job_id'])
            finally:
                release.set()
            deadline = time.monotonic() + 3
            while jobs.get(job['job_id'])['status'] in ('queued', 'running') and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertEqual('cancelled', jobs.get(job['job_id'])['status'])
            self.assertEqual([], list(self.context.inbox.rglob('note.txt')))
            self.assertEqual('cancelled', KnowledgeObjectJobManager(self.service).recent()[0]['status'])

    def test_cancel_after_commit_preserves_terminal_result(self):
        jobs = KnowledgeObjectJobManager(self.service)
        job_id = 'a' * 32
        jobs.root.mkdir(parents=True, exist_ok=True)
        completed = {'job_id': job_id, 'status': 'completed', 'message': '任务完成',
                     'result': {'source_preserved': True}}
        jobs._save(completed)
        # The worker has committed but has not yet removed its cancellation event.
        event = threading.Event()
        jobs._cancel[job_id] = event
        self.assertEqual(completed, jobs.cancel(job_id))
        self.assertEqual(completed, jobs.get(job_id))
        self.assertFalse(event.is_set())

    def test_original_endpoint_requires_owner_and_rejects_protected_paths(self):
        from fastapi import HTTPException
        from knowledge_bridge import web
        original = self.context.inbox / 'original.bin'
        original.write_bytes(b'fictional original')
        request = Mock()
        with patch.object(web, 'active_workspace', return_value=self.context):
            with patch.object(web, '_owner_read', side_effect=HTTPException(403, 'required')):
                with self.assertRaises(HTTPException) as error:
                    web.document_original(request, '99_收件箱/original.bin')
                self.assertEqual(403, error.exception.status_code)
            with patch.object(web, '_owner_read') as gate:
                response = web.document_original(request, '99_收件箱/original.bin')
                gate.assert_called_once_with(request)
                self.assertEqual(original, response.path)
                self.assertEqual('no-store', response.headers['cache-control'])
            with self.assertRaises(HTTPException):
                web.document_original(request, '.jiedu/workspace.json')


if __name__ == '__main__':
    unittest.main()
