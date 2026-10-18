import hashlib
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from knowledge_bridge import manual_extraction as manual, session_archive as archive
from knowledge_bridge.original_archive_import import session_records
from knowledge_bridge.session_manager import _archive_path
from knowledge_bridge.workspace import initialize_workspace


class OrganizeLoadingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.context = initialize_workspace(self.base / 'vault')
        self.sessions = self.base / 'sessions.json'
        self.sessions.write_text('{"sessions":{}}', 'utf-8')
        self.state = self.base / 'manual.json'
        self.raw = self.base / 'raw'
        self.stack = ExitStack()
        self.stack.enter_context(patch.multiple(manual, SESSIONS_PATH=self.sessions, ARCHIVE_ROOT=self.raw))
        self.stack.enter_context(patch.multiple(archive, SESSIONS_PATH=self.sessions,
            ARCHIVE_ROOT=self.raw, CANONICAL_ROOT=self.context.raw_archive))
        self.stack.enter_context(patch.object(archive, 'active_workspace', return_value=self.context))
        self.stack.enter_context(patch('knowledge_bridge.original_archive_import.session_records', return_value=[]))
        self.no_materialize = self.stack.enter_context(patch.object(archive, 'materialize', side_effect=AssertionError('status must not write evidence')))
        self.no_lookup = self.stack.enter_context(patch.object(archive, '_find_canonical', side_effect=AssertionError('status must not rescan every session')))

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def add_session(self, sid, text):
        folder = self.context.raw_archive / 'ChatGPT' / sid
        folder.mkdir(parents=True)
        (folder / 'manifest.json').write_text(json.dumps({'session_id':sid,'session_title':sid,'client':'chatgpt'}),'utf-8')
        path = folder / 'messages.jsonl'
        path.write_text(json.dumps({'message_id':'m1','role':'user','original_text':text})+'\n','utf-8')
        return path

    def status(self):
        return manual.status(self.state, self.sessions, self.raw)

    def test_each_canonical_session_loaded_once_and_empty_snapshot_not_reloaded(self):
        for index in range(7):self.add_session('s'+str(index),'body'+str(index))
        empty = self.add_session('empty', 'unused');empty.write_text('', 'utf-8')
        original = archive._canonical_item
        with patch.object(archive, '_canonical_item', wraps=original) as read:
            result = self.status()
        self.assertEqual(8, read.call_count)
        self.assertEqual(7, result['pending_message_count'])
        self.no_materialize.assert_not_called()
        self.no_lookup.assert_not_called()

    def test_same_message_id_new_content_is_unprocessed_and_added_deleted_sources_refresh(self):
        path = self.add_session('s1','first version')
        self.state.write_text(json.dumps({'processed_message_versions':{
            manual._message_key('s1','m1'):hashlib.sha256(b'first version').hexdigest()}}),'utf-8')
        self.assertEqual(0,self.status()['pending_message_count'])
        path.write_text(json.dumps({'message_id':'m1','role':'user','original_text':'other version'})+'\n','utf-8')
        result=self.status()
        self.assertEqual(1,result['pending_message_count'])
        self.assertEqual('other version',result['pending_sessions'][0]['preview'])
        second=self.add_session('s2','new source')
        self.assertEqual(2,self.status()['pending_session_count'])
        second.unlink()
        self.assertEqual(1,self.status()['pending_session_count'])

    def test_registered_live_rows_override_old_mirror_without_materialization(self):
        canonical=self.add_session('live','old mirror')
        before=canonical.read_bytes()
        self.sessions.write_text(json.dumps({'sessions':{'live':{'client_id':'chatgpt','last_activity_at':'2026-09-23','turn_count':1}}}),'utf-8')
        raw=_archive_path('live',self.raw);raw.parent.mkdir(parents=True,exist_ok=True)
        raw.write_text(json.dumps({'message_id':'m1','role':'user','original_text':'current live body'})+'\n','utf-8')
        self.assertEqual('current live body',self.status()['pending_sessions'][0]['preview'])
        self.assertEqual(before,canonical.read_bytes())
        self.no_materialize.assert_not_called()

    def test_imported_message_snapshot_rechecks_changed_source_without_rescan_write(self):
        source=self.base/'source.md';source.write_text('## MESSAGE m1 | USER\n\nchanged body\n','utf-8')
        state=self.base/'imports.json'
        state.write_text(json.dumps({'records':{'s':{'session_id':'s','source':'chatgpt','title':'source',
            'source_path':str(source),'source_sha256':'stale','messages':[{'message_id':'m1','role':'user','original_text':'stale body'}]}}}),'utf-8')
        before=state.read_bytes()
        rows=session_records(state,include_conversation=False,read_fresh=True)
        self.assertEqual('changed body',rows[0]['messages'][0]['original_text'])
        self.assertEqual('',rows[0]['conversation'])
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),rows[0]['hash'])
        self.assertEqual(before,state.read_bytes())


if __name__=='__main__':unittest.main()
