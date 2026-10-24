import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch,MagicMock
import numpy as np
from knowledge_bridge import core,evidence_index
from knowledge_bridge.retrieval import RetrievalService
from knowledge_bridge import retrieval
from knowledge_bridge.workspace import initialize_workspace


class RetrievalContentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.context=initialize_workspace(Path(self.tmp.name)/'workspace')
        (self.context.projects/'fixture.md').write_text('# 项目\n\n归档 Hermes 当前状态','utf-8')
        (self.context.raw_archive/'fixture.md').write_text('# 档案\n\n归档 Hermes 原文','utf-8')
        self.entered=threading.Event();self.release=threading.Event()
        def background():
            with core._model_lock:
                self.entered.set();self.release.wait(10)
        self.worker=threading.Thread(target=background);self.worker.start()
        self.assertTrue(self.entered.wait(1))
        self.addCleanup(self.stop_worker)

    def stop_worker(self):
        self.release.set();self.worker.join(2)

    def test_encoder_lock_wait_is_bounded_and_recovers(self):
        started=time.monotonic()
        with patch.object(core,'model') as model:
            with self.assertRaises(core.ModelBusyError):core.embed(['query'],local_only=True,lock_timeout=.1)
            self.assertLess(time.monotonic()-started,.75);model.assert_not_called()
        self.stop_worker()
        encoder=MagicMock();encoder.encode.return_value=np.ones((1,3))
        with patch.object(core,'model',return_value=encoder):
            self.assertEqual((1,3),core.embed(['query'],local_only=True,lock_timeout=.1).shape)

    def test_warmup_returns_without_waiting_or_starting_another_model(self):
        with patch.object(core,'_model',None),patch.object(core,'_model_loading',False),patch.object(core.threading,'Timer') as timer:
            started=time.monotonic();self.assertFalse(core.start_model_warmup())
            self.assertLess(time.monotonic()-started,.25);timer.assert_not_called()
            self.assertFalse(core._model_loading)

    def test_busy_hybrid_returns_real_passage_without_freshness_or_encoder(self):
        with patch.object(core,'VAULT',self.context.root),patch.object(core,'local_model_status',return_value={'state':'loaded'}),patch.object(core,'embed') as encode,patch.object(evidence_index,'freshness') as freshness:
            started=time.monotonic()
            result=RetrievalService(self.context).search('Hermes',scope='evidence',mode='hybrid',limit=3)
            self.assertLess(time.monotonic()-started,1)
            self.assertEqual('keyword',result['effective_mode']);self.assertFalse(result['embedding_used'])
            self.assertIn('占用共享向量模型',result['fallback_reason']);self.assertTrue(result['results'])
            encode.assert_not_called();freshness.assert_not_called()

    def test_lock_race_after_busy_check_also_falls_back(self):
        with patch.object(core,'VAULT',self.context.root),patch.object(core,'local_model_status',return_value={'state':'loaded'}),patch.object(core,'model_busy',return_value=False),patch.object(core,'index_freshness',return_value={'fresh':True}),patch.object(core,'table',return_value=object()),patch.object(core,'_vector_rows') as vectors:
            started=time.monotonic();result=RetrievalService(self.context).search('Hermes',mode='hybrid',limit=3)
            self.assertLess(time.monotonic()-started,1)
            self.assertEqual('keyword',result['effective_mode']);self.assertIn('100毫秒',result['fallback_reason'])
            self.assertTrue(result['results']);vectors.assert_not_called()

    def test_cold_query_uses_vector_on_same_request_when_ready(self):
        self.stop_worker()
        with patch.object(core,'VAULT',self.context.root),patch.object(core,'local_model_status',side_effect=[{'state':'available_unloaded'},{'state':'loaded'},{'state':'loaded'}]),patch.object(core,'start_model_warmup') as wake,patch.object(core,'model_busy',return_value=False),patch.object(core,'index_freshness',return_value={'fresh':True}),patch.object(core,'table',return_value=object()),patch.object(core,'embed',return_value=np.ones((1,3))) as encode,patch.object(core,'_vector_rows',return_value=[]):
            result=RetrievalService(self.context).search('Hermes',mode='hybrid')
            self.assertTrue(result['embedding_used']);wake.assert_called_once();encode.assert_called_once()

    def test_cold_timeout_is_bounded_and_honest(self):
        with patch.object(core,'VAULT',self.context.root),patch.object(core,'local_model_status',return_value={'state':'loading'}),patch.object(core,'start_model_warmup') as wake,patch.object(core,'embed') as encode,patch.object(retrieval,'MODEL_READY_WAIT_SECONDS',.05):
            started=time.monotonic();result=RetrievalService(self.context).search('Hermes',mode='hybrid')
            self.assertLess(time.monotonic()-started,1)
            self.assertFalse(result['embedding_used']);self.assertEqual(result['effective_mode'],'keyword')
            self.assertIn('仍在加载',result['fallback_reason']);self.assertTrue(result['results'])
            wake.assert_called_once();encode.assert_not_called()

    def test_missing_model_and_keyword_do_not_wake_or_download(self):
        with patch.object(core,'VAULT',self.context.root),patch.object(core,'local_model_status',return_value={'state':'unavailable'}),patch.object(core,'start_model_warmup') as wake:
            result=RetrievalService(self.context).search('Hermes',mode='hybrid')
            self.assertIn('没有完整',result['fallback_reason'])
            RetrievalService(self.context).search('Hermes',mode='keyword')
            wake.assert_not_called()


if __name__=='__main__':unittest.main()
