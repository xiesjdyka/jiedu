import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from knowledge_bridge.index_gate import IndexBusyError, IndexWriteGate
from knowledge_bridge.workspace import initialize_workspace


HOLDER = r'''
import sys,time
from pathlib import Path
from knowledge_bridge.index_gate import IndexWriteGate
from knowledge_bridge.workspace import load_workspace
gate=IndexWriteGate(load_workspace(Path(sys.argv[1])))
with gate.hold(sys.argv[2],5):
    print("LOCKED",flush=True)
    time.sleep(float(sys.argv[3]))
'''


class IndexGateProcessTests(unittest.TestCase):
    def _python(self):
        candidate=Path(__file__).resolve().parents[1]/".venv312"/"Scripts"/"python.exe"
        return str(candidate if candidate.exists() else Path(sys.executable))

    def test_two_real_processes_allow_only_one_writer(self):
        with tempfile.TemporaryDirectory() as temporary:
            context=initialize_workspace(Path(temporary)/"workspace")
            first=subprocess.Popen([self._python(),"-c",HOLDER,str(context.root),"first", "1.5"],
                cwd=Path(__file__).resolve().parents[1],stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                text=True,encoding="utf-8",env=os.environ.copy())
            self.assertEqual("LOCKED",first.stdout.readline().strip())
            gate=IndexWriteGate(context);started=time.monotonic()
            with self.assertRaises(IndexBusyError) as caught:
                with gate.hold("second",timeout=.25):pass
            self.assertLess(time.monotonic()-started,1.2)
            self.assertEqual("first",caught.exception.owner["operation"])
            self.assertEqual(0,first.wait(timeout=5),first.stderr.read())
            first.stdout.close();first.stderr.close()
            with gate.hold("after",timeout=.5):pass

    def test_abnormal_exit_recovers_only_after_os_lock_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            context=initialize_workspace(Path(temporary)/"workspace")
            child=subprocess.Popen([self._python(),"-c",HOLDER,str(context.root),"crash", "30"],
                cwd=Path(__file__).resolve().parents[1],stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                text=True,encoding="utf-8",env=os.environ.copy())
            self.assertEqual("LOCKED",child.stdout.readline().strip())
            gate=IndexWriteGate(context)
            self.assertFalse(gate.recover_if_abandoned())
            child.kill();child.wait(timeout=5)
            child.stdout.close();child.stderr.close()
            self.assertTrue(gate.recover_if_abandoned())
            audit=gate.audit_path.read_text("utf-8")
            self.assertIn('"previous_operation":"crash"',audit)


class IndexGenerationTests(unittest.TestCase):
    def test_empty_evidence_rebuild_reports_throttle_without_loading_model(self):
        import knowledge_bridge.evidence_index as evidence_index
        with tempfile.TemporaryDirectory() as temporary:
            context=initialize_workspace(Path(temporary)/'workspace')
            with patch.dict(os.environ,{'KNOWLEDGE_BRIDGE_EMBEDDING_DUTY_CYCLE':'.5'},clear=False), \
                 patch('knowledge_bridge.core.local_model_status',return_value={'state':'available_unloaded'}):
                result=evidence_index.rebuild(context)
            self.assertEqual('rebuilt',result['status']);self.assertEqual(0,result['embedded_chunks'])
            self.assertEqual(.5,result['throttle_duty_cycle']);self.assertEqual(0,result['throttle_wait_seconds'])
            self.assertTrue((context.index_root('evidence')/'active-vector.json').is_file())

    def test_failed_fp16_setup_does_not_publish_partial_model(self):
        import knowledge_bridge.core as core
        marker=object()
        with patch.dict(os.environ,{'KNOWLEDGE_BRIDGE_MODEL_PRECISION':'fp16'},clear=False), \
             patch.object(core,'_model',None),patch.object(core,'_model_error',None), \
             patch('sentence_transformers.SentenceTransformer',return_value=marker), \
             patch('torch.cuda.is_available',return_value=False):
            with self.assertRaisesRegex(RuntimeError,'CUDA'):
                core.model(local_only=True)
            self.assertIsNone(core._model)
            self.assertEqual('load_failed',core.local_model_status()['state'])
            self.assertEqual(4,core.configured_embedding_batch_size())

    def test_evidence_build_can_cancel_without_switching_pointer(self):
        import knowledge_bridge.evidence_index as evidence_index
        with tempfile.TemporaryDirectory() as temporary:
            context=initialize_workspace(Path(temporary)/'workspace')
            evidence=context.raw_archive/'fixture.md';evidence.write_text('# 证据\n\n取消前不应切换。','utf-8')
            cancelled=threading.Event();cancelled.set()
            result=evidence_index.rebuild(context,operation='fixture_cancel',cancel_event=cancelled)
            self.assertEqual('cancelled',result['status']);self.assertFalse(result['pointer_switched'])
            self.assertFalse((context.index_root('evidence')/'active-vector.json').exists())

    def test_throttled_evidence_build_wait_is_cancel_responsive(self):
        import knowledge_bridge.evidence_index as evidence_index
        class CancelDuringWait:
            def __init__(self):self.waits=[]
            def is_set(self):return False
            def wait(self,seconds):self.waits.append(seconds);return True
        with tempfile.TemporaryDirectory() as temporary:
            context=initialize_workspace(Path(temporary)/'workspace')
            evidence=context.raw_archive/'fixture.md';evidence.write_text('# A\n\n第一块。\n\n# B\n\n第二块。','utf-8')
            cancel=CancelDuringWait()
            def fake_embed(texts,**_):time.sleep(.01);return np.ones((len(texts),4),dtype='float32')
            with patch.dict(os.environ,{'KNOWLEDGE_BRIDGE_EMBEDDING_DUTY_CYCLE':'.5','KNOWLEDGE_BRIDGE_EMBEDDING_BATCH_SIZE':'1'}), \
                 patch('knowledge_bridge.core.model',return_value=object()),patch('knowledge_bridge.core.embed',side_effect=fake_embed):
                result=evidence_index.rebuild(context,cancel_event=cancel)
            self.assertEqual('cancelled',result['status']);self.assertEqual(1,result['embedded_chunks'])
            self.assertTrue(cancel.waits and cancel.waits[0]>=.009)
            self.assertEqual(.5,result['throttle_duty_cycle']);self.assertFalse(result['pointer_switched'])

    def test_evidence_generation_isolated_atomic_and_incremental(self):
        import knowledge_bridge.evidence_index as evidence_index
        from knowledge_bridge.archive_store import ArchiveStore
        with tempfile.TemporaryDirectory() as temporary:
            context=initialize_workspace(Path(temporary)/"workspace");store=ArchiveStore(context)
            session=store.ensure_session(source_key='codex',source_session_id='fixture-session',title='虚构证据')
            store.append_message(session['session_key'],message_id='m1',role='user',content='最初选择甲方案。')
            store.append_message(session['session_key'],message_id='m1b',role='assistant',content='收到甲方案。')
            def fake_embed(texts,**_):return np.asarray([[float(i+1),0.0,0.0,1.0] for i,_ in enumerate(texts)],dtype='float32')
            with patch('knowledge_bridge.core.embed',side_effect=fake_embed),patch('knowledge_bridge.core.local_model_status',return_value={'state':'loaded'}):
                built=evidence_index.rebuild(context,operation='fixture_evidence')
                self.assertEqual('rebuilt',built['status']);self.assertGreater(built['embedded_chunks'],0)
                formal=context.index_root('formal')/'active.json';self.assertFalse(formal.exists())
                pointer=(context.index_root('evidence')/'active-vector.json').read_bytes()
                store.append_message(session['session_key'],message_id='m2',role='user',content='后来纠正为乙方案。')
                conversation=Path(store.session(session['session_key'])['archive_path'])/'01_对话'/'conversation.md'
                changed=evidence_index.sync([context.relative(conversation)],context,operation='fixture_incremental',lock_timeout=1)
                self.assertEqual('incremental_synced',changed['status']);self.assertGreater(changed['embedded_chunks'],0)
                self.assertLessEqual(changed['embedded_chunks'],2);self.assertGreaterEqual(changed['reused_vectors'],1)
                self.assertNotEqual(pointer,(context.index_root('evidence')/'active-vector.json').read_bytes())
                rows=evidence_index.table(context).to_arrow().to_pylist()
                self.assertEqual(len(rows),len({row['chunk_id'] for row in rows}))
                self.assertTrue(any(row['message_id']=='m2' and row['source_version'] for row in rows))
                stable=(context.index_root('evidence')/'active-vector.json').read_bytes()
                store.append_message(session['session_key'],message_id='m3',role='user',content='第三条消息。')
                with patch('knowledge_bridge.core.embed',side_effect=RuntimeError('fictional evidence build failure')):
                    with self.assertRaisesRegex(RuntimeError,'fictional'):
                        evidence_index.sync([context.relative(conversation)],context,operation='fixture_failure',lock_timeout=1)
                self.assertEqual(stable,(context.index_root('evidence')/'active-vector.json').read_bytes())

    def test_attachment_message_headings_do_not_collide_with_canonical_conversation(self):
        import knowledge_bridge.evidence_index as evidence_index
        from knowledge_bridge.archive_store import ArchiveStore
        with tempfile.TemporaryDirectory() as temporary:
            context=initialize_workspace(Path(temporary)/'workspace');store=ArchiveStore(context)
            session=store.ensure_session(source_key='codex',source_session_id='same-message',title='块身份')
            store.append_message(session['session_key'],message_id='m1',role='user',content='标准正文。')
            folder=Path(store.session(session['session_key'])['archive_path'])
            conversation=folder/'01_对话'/'conversation.md'
            attachment=folder/'02_附件'/'带消息标题.md';attachment.write_text('## MESSAGE m1｜USER\n\n附件派生正文。','utf-8')
            canonical=evidence_index.rows_for_file(conversation,context)[1]
            derived=evidence_index.rows_for_file(attachment,context)[1]
            self.assertFalse({row['chunk_id'] for row in canonical}&{row['chunk_id'] for row in derived})
            self.assertTrue(all(row['source_type']=='raw_chat' for row in canonical))
            self.assertTrue(all(row['source_type']=='derived_document' for row in derived))

    def test_rebuild_switches_only_valid_unique_generation_and_failure_keeps_old(self):
        import knowledge_bridge.core as core
        with tempfile.TemporaryDirectory() as temporary:
            context=initialize_workspace(Path(temporary)/"workspace")
            (context.knowledge/"a.md").write_text("# A\n\nalpha unique body",encoding="utf-8")
            (context.projects/"demo").mkdir()
            (context.projects/"demo"/"b.md").write_text("# B\n\nbeta unique body",encoding="utf-8")
            formal=context.index_root("formal")
            replacements={"VAULT":context.root,"_WORKSPACE":context,"DB_PATH":formal/"lancedb",
                          "STATE_PATH":formal/"state.json","ACTIVE_INDEX_PATH":formal/"active.json",
                          "GENERATION_ROOT":formal/"generations"}
            def fake_embed(texts,**_):return np.asarray([[float(i+1),0.0,0.0,1.0] for i,_ in enumerate(texts)],dtype="float32")
            with patch.multiple(core,**replacements),patch.object(core,"active_workspace",return_value=context),patch.object(core,"embed",side_effect=fake_embed):
                result=core.rebuild_index(operation="test_rebuild")
                self.assertEqual("rebuilt",result["status"])
                rows=core.all_rows();self.assertEqual(len(rows),len({row["chunk_id"] for row in rows}))
                self.assertEqual(set(core.state()["files"]),{"02_\u9879\u76ee/demo/b.md","03_\u77e5\u8bc6/a.md"})
                pointer_before=(formal/"active.json").read_bytes();rows_before=rows
                with patch.object(core,"embed",side_effect=RuntimeError("fictional build failure")):
                    with self.assertRaisesRegex(RuntimeError,"fictional"):
                        core.rebuild_index(operation="failed_rebuild")
                self.assertEqual(pointer_before,(formal/"active.json").read_bytes())
                self.assertEqual([row["chunk_id"] for row in rows_before],[row["chunk_id"] for row in core.all_rows()])

    def test_incremental_sync_reports_generation_source_versions_changes_and_deletions(self):
        import knowledge_bridge.core as core
        with tempfile.TemporaryDirectory() as temporary:
            context=initialize_workspace(Path(temporary)/"workspace");formal=context.index_root("formal")
            a=context.knowledge/"a.md";a.write_text("# A\n\nalpha",encoding="utf-8")
            replacements={"VAULT":context.root,"_WORKSPACE":context,"DB_PATH":formal/"lancedb",
                          "STATE_PATH":formal/"state.json","ACTIVE_INDEX_PATH":formal/"active.json",
                          "GENERATION_ROOT":formal/"generations"}
            def fake_embed(texts,**_):return np.asarray([[float(i+1),0.0,0.0,1.0] for i,_ in enumerate(texts)],dtype="float32")
            with patch.multiple(core,**replacements),patch.object(core,"active_workspace",return_value=context),patch.object(core,"embed",side_effect=fake_embed):
                built=core.rebuild_index(operation="fixture_build");generation=built["generation"]
                before=core.state()["source_version"];a.write_text("# A\n\nalpha changed",encoding="utf-8")
                b=context.knowledge/"b.md";b.write_text("# B\n\nbeta",encoding="utf-8")
                changed=core.sync(paths=["03_知识/a.md","03_知识/b.md"],operation="fixture_incremental")
                self.assertEqual("incremental_synced",changed["status"]);self.assertEqual(generation,changed["generation"])
                self.assertEqual(before,changed["source_version_before"]);self.assertNotEqual(before,changed["source_version_after"])
                self.assertEqual(["03_知识/a.md","03_知识/b.md"],changed["changed_paths"]);self.assertGreater(changed["embedded_chunks"],0)
                b.unlink();deleted=core.sync(paths=["03_知识/b.md"],operation="fixture_delete")
                self.assertEqual(["03_知识/b.md"],deleted["deleted_paths"]);self.assertEqual(generation,deleted["generation"])
                audit=(context.audit/"index-incremental.jsonl").read_text("utf-8")
                self.assertIn('"operation":"fixture_incremental"',audit);self.assertIn('"operation":"fixture_delete"',audit)

    def test_watcher_retains_paths_while_rebuild_is_busy(self):
        import knowledge_bridge.watcher as watcher
        handler=watcher.Handler();handler._pending={"03_\u77e5\u8bc6/a.md"};handler._last_change=0
        with patch.object(watcher,"sync",side_effect=[{"status":"index_busy"},{"added_or_modified":1}]):
            handler.flush(force=True);self.assertEqual({"03_\u77e5\u8bc6/a.md"},handler._pending)
            handler.flush(force=True);self.assertFalse(handler._pending)

    def test_background_evidence_sync_failure_keeps_active_generation(self):
        from knowledge_bridge import evidence_index
        with tempfile.TemporaryDirectory() as temporary:
            context=initialize_workspace(Path(temporary)/'workspace');pointer=context.index_root('evidence')/'active-vector.json'
            pointer.parent.mkdir(parents=True,exist_ok=True);pointer.write_bytes(b'{"generation":"stable"}\n');before=pointer.read_bytes()
            evidence_index._SYNC_THREAD=None
            with patch.object(evidence_index,'sync',return_value={'status':'failed'}):
                self.assertEqual('started',evidence_index.start_sync(context)['status']);evidence_index._SYNC_THREAD.join(5)
            self.assertEqual(before,pointer.read_bytes())

    @unittest.skipUnless(os.name=='nt','Windows extended path syntax')
    def test_watcher_normalizes_windows_extended_length_event_paths(self):
        import knowledge_bridge.watcher as watcher
        with tempfile.TemporaryDirectory() as temporary:
            context=initialize_workspace(Path(temporary)/'workspace')
            target=context.knowledge/'a.md';target.write_text('x','utf-8')
            extended=Path('\\\\?\\'+str(target.resolve()))
            with patch.object(watcher,'VAULT',context.root):
                self.assertEqual('03_知识/a.md',watcher._relative(extended).as_posix())


if __name__=="__main__":unittest.main()
