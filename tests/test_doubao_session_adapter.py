import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from knowledge_bridge.doubao_session_adapter import DoubaoSessionAdapter
from knowledge_bridge.doubao_indexeddb_source import CacheMessage, CacheReadResult
from knowledge_bridge.passage_search import search as passage_search
from knowledge_bridge.client_access import ClientIdentityRegistry, ClientPermissionService
from knowledge_bridge.source_connections import SourceConnections
from knowledge_bridge.workspace import initialize_workspace


def _line(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


class DoubaoSessionAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.workspace = initialize_workspace(self.base / "workspace")
        self.root = self.base / ".sessions"
        SourceConnections(self.workspace).bind("doubao-client", {
            "source_key": "doubao_work", "product_family": "豆包",
            "session_source": "豆包工作版", "client_type": "desktop_agent",
        })

    def tearDown(self):
        self.temporary.cleanup()

    def _source(self, session="12345", agent="agent-a"):
        folder = self.root / session / "agents" / agent / "system"
        folder.mkdir(parents=True)
        (self.root / session / "board.md").write_text("board", "utf-8")
        path = folder / "trajectory.jsonl"
        path.write_bytes(b"".join([
            _line({"role": "user", "content": "公开问题", "image_link_list": ["missing/image.png"]}),
            _line({"role": "assistant", "content": "内部过程", "tool_calls": [{"name": "tool"}]}),
            _line({"role": "tool", "content": "工具秘密"}),
            _line({"role": "assistant", "content": "公开回答"}),
        ]))
        return path

    def _calibrate(self, session="12345", agent="agent-a", title="真实标题"):
        user=hashlib.sha256("公开问题".encode()).hexdigest()
        assistant=hashlib.sha256("公开回答".encode()).hexdigest()
        (self.workspace.state / "doubao-work-calibration.json").write_text(json.dumps({
            "format_version":1,"title_overrides":{session:title},
            "visible_messages":{f"{session}/{agent}":[
                {"line":1,"role":"user","content_sha256":user,"evidence_kind":"fixture_ui"},
                {"line":4,"role":"assistant","content_sha256":assistant,"evidence_kind":"fixture_ui"},
            ]}},ensure_ascii=False),"utf-8")

    def _cache_source(self):
        level=self.base/'leveldb';blob=self.base/'blob';level.mkdir(exist_ok=True);blob.mkdir(exist_ok=True)
        (level/'CURRENT').write_text('MANIFEST-000001\n','ascii')
        (level/'MANIFEST-000001').write_bytes(b'm');(level/'000001.log').write_bytes(b'data')
        return level,blob

    @staticmethod
    def _decoded(messages=()):
        return CacheReadResult(tuple(messages),{'c1':'验证对话'},9,0,1,(),
            current_decode_errors=0,ignored_historical_decode_errors=0,
            publication_ready=False,current_state_verified=True)

    def test_conservative_incremental_restart_partial_and_truncation(self):
        path = self._source()
        self._calibrate()
        adapter = DoubaoSessionAdapter(self.workspace, source_root=self.root,
                                       source_instance="doubao-client")
        first = adapter.sync_once()
        self.assertEqual(2, first["saved_messages"])
        session = next(iter(adapter.store._registry()["sessions"].values()))
        folder = self.workspace.root / session["relative_path"]
        conversation = (folder / "01_对话" / "conversation.md").read_text("utf-8")
        self.assertIn("公开问题", conversation); self.assertIn("公开回答", conversation)
        self.assertNotIn("内部过程", conversation); self.assertNotIn("工具秘密", conversation)
        self.assertEqual("partial_visible_history", session["history_coverage"])
        self.assertEqual(1, len(adapter.store.attachment_status(session["session_key"])))
        self.assertTrue(adapter.store.attachment_status(session["session_key"])[0]["missing"])

        restarted = DoubaoSessionAdapter(self.workspace, source_root=self.root,
                                         source_instance="doubao-client")
        manifest=folder/"03_元数据"/"manifest.json"; conversation_path=folder/"01_对话"/"conversation.md"
        stable=(manifest.read_bytes(),manifest.stat().st_mtime_ns,conversation_path.read_bytes(),conversation_path.stat().st_mtime_ns)
        self.assertEqual(0, restarted.sync_once()["saved_messages"])
        self.assertEqual(stable,(manifest.read_bytes(),manifest.stat().st_mtime_ns,conversation_path.read_bytes(),conversation_path.stat().st_mtime_ns))
        partial = json.dumps({"role": "user", "content": "稍后完整"}, ensure_ascii=False).encode("utf-8")
        with path.open("ab") as handle:
            handle.write(partial[:len(partial)//2])
        self.assertEqual("partial_line_pending", restarted.sync_once()["streams"][0]["status"])
        with path.open("ab") as handle:
            handle.write(partial[len(partial)//2:] + b"\n")
        self.assertEqual(0, restarted.sync_once()["saved_messages"])
        self.assertNotIn("稍后完整", conversation_path.read_text("utf-8"))
        self.assertIn("稍后完整", (folder/"03_元数据"/"visibility-review.jsonl").read_text("utf-8"))
        count_before = restarted.store.session(session["session_key"])["message_count"]
        path.write_bytes(_line({"role": "user", "content": "被改写"}))
        changed = restarted.sync_once()["streams"][0]
        self.assertEqual("source_changed_requires_review", changed["status"])
        self.assertEqual(count_before, restarted.store.session(session["session_key"])["message_count"])

    def test_stable_identity_renames_without_duplicate_session(self):
        self._source(session="98765")
        self._calibrate(session="98765",title="旧标题")
        first = DoubaoSessionAdapter(self.workspace, source_root=self.root,
                                     source_instance="doubao-client")
        first.sync_once()
        before = first.store._registry()["sessions"]
        key = next(iter(before))
        self._calibrate(session="98765",title="新标题")
        second = DoubaoSessionAdapter(self.workspace, source_root=self.root,
                                      source_instance="doubao-client")
        second.sync_once()
        after = second.store._registry()["sessions"]
        self.assertEqual({key}, set(after))
        self.assertEqual("新标题", after[key]["title"])
        self.assertEqual(2, len(after[key]["title_history"]))

    def test_unverified_user_and_assistant_stay_out_of_public_conversation(self):
        self._source(session="55555")
        adapter=DoubaoSessionAdapter(self.workspace,source_root=self.root,source_instance="doubao-client")
        result=adapter.sync_once();self.assertEqual(0,result["saved_messages"])
        session=next(iter(adapter.store._registry()["sessions"].values()))
        folder=self.workspace.root/session["relative_path"]
        conversation=(folder/"01_对话"/"conversation.md").read_text("utf-8")
        self.assertNotIn("公开问题",conversation);self.assertNotIn("公开回答",conversation)
        review=(folder/"03_元数据"/"visibility-review.jsonl").read_text("utf-8")
        self.assertIn("公开问题",review);self.assertIn("公开回答",review)
        self.assertEqual(0,session.get("message_count",0))
        digest=hashlib.sha256("公开回答".encode()).hexdigest()
        (self.workspace.state/"doubao-work-calibration.json").write_text(json.dumps({
            "format_version":1,"identity_matches":{"55555/agent-a":[{
                "line":4,"role":"assistant","content_sha256":digest,
                "message_identity_matched":True,"full_body_verified":False}]}}),"utf-8")
        DoubaoSessionAdapter(self.workspace,source_root=self.root,source_instance="doubao-client").sync_once()
        rows=[json.loads(line) for line in (folder/"03_元数据"/"visibility-review.jsonl").read_text("utf-8").splitlines()]
        matched=next(row for row in rows if row["source_line"]==4)
        self.assertTrue(matched["identity_match_evidence"]["message_identity_matched"])
        self.assertFalse(matched["identity_match_evidence"]["full_body_verified"])

    def test_binding_requires_one_active_client_with_archive_allow(self):
        identity_root=self.base/"identities";registry=ClientIdentityRegistry(self.workspace,identity_root)
        permissions=ClientPermissionService(registry)
        def add(claimed):
            row=registry.commit_prepared(registry.prepare(claimed,claimed,"doubao_work","stdio"))
            SourceConnections(self.workspace).bind(row["internal_id"],{
                "source_key":"doubao_work","product_family":"豆包","session_source":"豆包工作版","client_type":"desktop_agent"})
            permissions.set_policy(row["internal_id"],"evidence","raw_archive","allow")
            return row
        first=add("doubao-one")
        adapter=DoubaoSessionAdapter(self.workspace,source_root=self.root,identity_root=identity_root)
        self.assertEqual(first["internal_id"],adapter._bound_instance()[0])
        add("doubao-two")
        instance,matches,reason=adapter._bound_instance()
        self.assertIsNone(instance);self.assertEqual(2,len(matches))
        self.assertEqual("multiple_active_doubao_bindings_require_selection",reason)

    def test_current_cache_decode_failure_preserves_previous_snapshot_and_cursor(self):
        level=self.base/'leveldb';blob=self.base/'blob';level.mkdir();blob.mkdir()
        (level/'CURRENT').write_text('MANIFEST-000001\n','ascii')
        (level/'MANIFEST-000001').write_bytes(b'm');(level/'000001.log').write_bytes(b'data')
        adapter=DoubaoSessionAdapter(self.workspace,source_root=self.root,source_instance='doubao-client',
                                     cache_leveldb=level,cache_blob=blob)
        adapter.cache_snapshot.mkdir(parents=True);(adapter.cache_snapshot/'last-good').write_text('keep')
        state={'cache':{'max_sequence':7,'signature':'old','status':'structured_cache_detected_awaiting_body_validation'}}
        decoded=SimpleNamespace(current_decode_errors=1,ignored_historical_decode_errors=4)
        with patch('knowledge_bridge.doubao_session_adapter.read_snapshot',return_value=decoded):
            result=adapter._sync_cache('doubao-client',state)
        self.assertEqual('cache_current_value_decode_failed',result['status'])
        self.assertEqual(7,state['cache']['max_sequence'])
        self.assertEqual('keep',(adapter.cache_snapshot/'last-good').read_text())

    def test_unverified_cache_sample_survives_source_clear_restart_and_deduplicates(self):
        level,blob=self._cache_source()
        message=CacheMessage('c1','m1','user','只用于真实界面对照',1,7,True,'task-1',({
            'reference':'https://p11-flow-imagex-sign.byteimg.com/a.png?x-signature=secret&x-expires=9999999999',
            'block_type':10012,'name':'a.png'},),{'user_type':1,'cache_stage':4})
        adapter=DoubaoSessionAdapter(self.workspace,source_root=self.root,source_instance='doubao-client',
                                     cache_leveldb=level,cache_blob=blob)
        state={'cache':{}}
        with patch('knowledge_bridge.doubao_session_adapter.cache_signature',return_value=('first',)), \
             patch('knowledge_bridge.doubao_session_adapter.consistent_snapshot',return_value={'signature':'snap-1','attempts':1}), \
             patch('knowledge_bridge.doubao_session_adapter.read_snapshot',return_value=self._decoded([message])), \
             patch.object(adapter,'_promote_cache_snapshot') as promote:
            first=adapter._sync_cache('doubao-client',state)
        self.assertEqual('structured_cache_detected_awaiting_body_validation',first['status'])
        self.assertEqual('validation_samples_preserved',first['validation_status'])
        self.assertEqual(1,first['validation_sample_count']);self.assertEqual(1,first['new_validation_samples'])
        self.assertTrue(first['validation_samples_preserved']);promote.assert_called_once()
        rows=[json.loads(line) for line in adapter.cache_validation_messages.read_text('utf-8').splitlines()]
        attachments=adapter.cache_validation_attachments.read_text('utf-8')
        self.assertEqual([('c1','m1',7)],[(r['conversation_id'],r['message_id'],r['source_sequence']) for r in rows])
        self.assertNotIn('x-signature',attachments);self.assertNotIn('secret',attachments)

        restarted=DoubaoSessionAdapter(self.workspace,source_root=self.root,source_instance='doubao-client',
                                       cache_leveldb=level,cache_blob=blob)
        with patch('knowledge_bridge.doubao_session_adapter.cache_signature',return_value=('second',)), \
             patch('knowledge_bridge.doubao_session_adapter.consistent_snapshot',return_value={'signature':'snap-2','attempts':1}), \
             patch('knowledge_bridge.doubao_session_adapter.read_snapshot',return_value=self._decoded([message])), \
             patch.object(restarted,'_promote_cache_snapshot'):
            duplicate=restarted._sync_cache('doubao-client',{'cache':{}})
        self.assertEqual(1,duplicate['validation_sample_count']);self.assertEqual(0,duplicate['new_validation_samples'])
        with patch('knowledge_bridge.doubao_session_adapter.cache_signature',return_value=('cleared',)), \
             patch('knowledge_bridge.doubao_session_adapter.consistent_snapshot',return_value={'signature':'snap-3','attempts':1}), \
             patch('knowledge_bridge.doubao_session_adapter.read_snapshot',return_value=self._decoded()), \
             patch.object(restarted,'_promote_cache_snapshot'):
            cleared=restarted._sync_cache('doubao-client',{'cache':{}})
        self.assertEqual(1,cleared['validation_sample_count']);self.assertEqual(0,cleared['new_validation_samples'])
        self.assertEqual([],list(self.workspace.raw_message_store.glob('*.jsonl')))
        self.assertEqual({},restarted.store._registry().get('sessions',{}))
        found=passage_search('真实界面对照','evidence',self.workspace,[restarted.cache_validation_messages])
        self.assertEqual(0,found['returned'])

    def test_validation_capture_rejects_unknown_incomplete_and_empty_messages(self):
        level,blob=self._cache_source()
        rows=[CacheMessage('c','unknown','tool','工具内容',1,1,True),
              CacheMessage('c','partial','assistant','未完成助手',2,2,False),
              CacheMessage('c','empty','user','',3,3,True)]
        adapter=DoubaoSessionAdapter(self.workspace,source_root=self.root,source_instance='doubao-client',
                                     cache_leveldb=level,cache_blob=blob)
        with patch('knowledge_bridge.doubao_session_adapter.cache_signature',return_value=('filtered',)), \
             patch('knowledge_bridge.doubao_session_adapter.consistent_snapshot',return_value={'signature':'filtered','attempts':1}), \
             patch('knowledge_bridge.doubao_session_adapter.read_snapshot',return_value=self._decoded(rows)), \
             patch.object(adapter,'_promote_cache_snapshot'):
            result=adapter._sync_cache('doubao-client',{'cache':{}})
        self.assertEqual(0,result['validation_sample_count'])
        self.assertEqual('',adapter.cache_validation_messages.read_text('utf-8'))

    def test_validation_write_failure_and_capacity_stop_snapshot_promotion(self):
        level,blob=self._cache_source()
        message=CacheMessage('c1','m1','user','sample',1,7,True)
        adapter=DoubaoSessionAdapter(self.workspace,source_root=self.root,source_instance='doubao-client',
                                     cache_leveldb=level,cache_blob=blob)
        adapter.cache_snapshot.mkdir(parents=True);(adapter.cache_snapshot/'last-good').write_text('keep')
        state={'cache':{'max_sequence':6,'status':'structured_cache_detected_awaiting_body_validation'}}
        common=(patch('knowledge_bridge.doubao_session_adapter.cache_signature',return_value=('write',)),
                patch('knowledge_bridge.doubao_session_adapter.consistent_snapshot',return_value={'signature':'write','attempts':1}),
                patch('knowledge_bridge.doubao_session_adapter.read_snapshot',return_value=self._decoded([message])))
        with common[0],common[1],common[2], \
             patch('knowledge_bridge.doubao_session_adapter._atomic_jsonl',side_effect=OSError('disk full')), \
             patch.object(adapter,'_promote_cache_snapshot') as promote:
            failed=adapter._sync_cache('doubao-client',state)
        self.assertEqual('validation_capture_failed',failed['status'])
        self.assertFalse(failed['validation_samples_preserved']);promote.assert_not_called()
        self.assertEqual('keep',(adapter.cache_snapshot/'last-good').read_text())
        self.assertEqual(6,state['cache']['max_sequence'])

        with patch('knowledge_bridge.doubao_session_adapter.cache_signature',return_value=('full',)), \
             patch('knowledge_bridge.doubao_session_adapter.consistent_snapshot',return_value={'signature':'full','attempts':1}), \
             patch('knowledge_bridge.doubao_session_adapter.read_snapshot',return_value=self._decoded([message])), \
             patch('knowledge_bridge.doubao_session_adapter.VALIDATION_SAMPLE_LIMIT',0), \
             patch.object(adapter,'_promote_cache_snapshot') as promote:
            full=adapter._sync_cache('doubao-client',state)
        self.assertEqual('validation_capture_full',full['status']);promote.assert_not_called()


if __name__ == "__main__":
    unittest.main()
