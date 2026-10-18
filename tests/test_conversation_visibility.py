import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from knowledge_bridge import conversation_visibility as cv, session_archive as sa
from knowledge_bridge import hermes_session_adapter as ha, manual_extraction as me
from knowledge_bridge.workspace import initialize_workspace
from knowledge_bridge.search_scopes import files_for_scope, search_scope


class ConversationVisibilityTests(unittest.TestCase):
    def test_codex_explicit_metadata_only(self):
        for field in ('source', 'thread_source'):
            for source in ({'subagent': {'thread_spawn': {}}}, {'subagent': {'other': 'guardian'}}):
                self.assertEqual('codex_subagent', cv.codex_exclusion({field: source, 'parent_thread_id': 'p'}))
        for row in ({'source': 'exec'}, {'source': 'vscode', 'parent_thread_id': 'fork'}, {'title': 'guardian test 未命名'}):
            self.assertIsNone(cv.codex_exclusion(row))

    def test_hermes_real_lineage_fields_preserve_branch_reset_and_compression(self):
        parent = {'id': 'parent', 'source': 'desktop', 'model_config': '{}', 'end_reason': 'ws_orphan_reap', 'ended_at': 2, 'session_key': 'routing'}
        child = {'id': 'child', 'source': 'desktop', 'parent_session_id': 'parent', 'model_config': '{}', 'started_at': 3, 'session_key': 'routing'}
        self.assertEqual({'child'}, set(cv.hermes_exclusions([parent, child])))
        for marker in ('_branched_from', '_reset_from'):
            self.assertFalse(cv.hermes_exclusions([parent, child | {'model_config': json.dumps({marker: 'parent'})}]))
        for reason in ('branched', 'compression', 'session_reset', 'session_switch', 'idle', 'daily', 'suspended', 'resume_pending_expired'):
            self.assertFalse(cv.hermes_exclusions([parent | {'end_reason': reason}, child]))
        self.assertEqual({'child'}, set(cv.hermes_exclusions([parent | {'end_reason': 'session_reset'}, child | {'session_key': 'delegated'}])))
        self.assertFalse(cv.hermes_exclusions([child]))
        self.assertFalse(cv.hermes_exclusions([parent, child | {'model_config': 'invalid'}]))
        self.assertFalse(cv.hermes_exclusions([{'id': 'main', 'source': 'desktop', 'title': '', 'model_config': '{}'}]))

    def test_public_body_not_title_or_length_and_empty_is_dynamic(self):
        self.assertIsNone(cv.exclusion_reason({'session_title': '', 'messages': [{'role': 'user', 'content': '好'}]}))
        self.assertEqual('no_public_messages', cv.exclusion_reason({'messages': [{'role': 'tool', 'content': 'tool output'}, {'role': 'assistant', 'content': '  '}]}))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'messages.jsonl';path.write_text('', 'utf-8')
            self.assertFalse(cv.has_public_file(path))
            path.write_text(json.dumps({'role': 'user', 'content': '好'}), 'utf-8')
            self.assertTrue(cv.has_public_file(path))

    def test_lists_queue_retrieval_and_old_copies_share_classification(self):
        with tempfile.TemporaryDirectory() as tmp:
            context = initialize_workspace(Path(tmp) / 'workspace')
            def archive(name, sid, role=None, messages=None):
                folder = context.raw_archive / 'codex' / name;folder.mkdir(parents=True)
                manifest = {'source_key': 'codex', 'session_id': sid, 'source_session_id': sid, 'session_title': ''}
                if role:manifest['archive_kind'] = role
                (folder / 'manifest.json').write_text(json.dumps(manifest), 'utf-8')
                rows = messages if messages is not None else [{'message_id': '1', 'role': 'user', 'content': 'needle ' + sid}]
                (folder / 'messages.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in rows), 'utf-8')
                (folder / 'conversation.md').write_text('# Chat\n\nneedle '+sid, 'utf-8')
                return folder
            main = archive('main', 'main', messages=[{'message_id': '1', 'role': 'user', 'content': '好'}])
            internal = archive('child', 'child', 'subtask')
            archive('a-legacy-child', 'child')
            archive('guardian', 'guardian', 'guardian')
            archive('validation', 'validation', 'validation')
            archive('empty', 'empty', messages=[])
            archive('tool-only', 'tool', messages=[{'role': 'tool', 'content': 'needle'}])
            archive('ledger-child', 'ledger')
            (context.state / 'conversation-exclusions.json').write_text(json.dumps({'records': [{'client': 'codex', 'source_session_id': 'ledger', 'excluded': True}]}), 'utf-8')
            (context.raw_archive / 'loose-old-copy.md').write_bytes((internal / 'conversation.md').read_bytes())
            before = {str(p): p.read_bytes() for p in context.raw_archive.rglob('*') if p.is_file()}
            with patch.object(sa, 'CANONICAL_ROOT', context.raw_archive), patch.object(sa, 'SESSIONS_PATH', context.state/'sessions.json'), patch.object(sa, 'active_workspace', return_value=context), patch('knowledge_bridge.original_archive_import.session_records', return_value=[]), patch.object(sa, '_display_cache', return_value={}):
                self.assertEqual(['main'], [r['session_id'] for r in sa.list_archives(read_only=True, include_conversation=False)])
                self.assertEqual(['main'], [r['session_id'] for r in sa.list_archive_summaries()['items']])
                self.assertEqual({'main'}, set(me._sessions()))
                self.assertGreater(len(sa.list_archives(read_only=True, include_internal=True)), 1)
            self.assertEqual([main/'conversation.md'], files_for_scope(context, 'evidence'))
            from knowledge_bridge.organizer import Organizer
            service=Organizer(context)
            with patch.object(cv,'SourceVisibility',wraps=cv.SourceVisibility) as snapshot:
                job=service.start('fixture','evidence','90_原始档案/codex','filter')
                self.assertEqual(1,snapshot.call_count)
                self.assertTrue(all('/main/' in row['source_path'] for row in job['items']))
            legacy_job=service.load(job['job_id'],'fixture')
            source=context.relative(internal/'conversation.md')
            legacy_job['sources'].append({'source_path':source,'status':'unprocessed','readable':True})
            service.path(job['job_id']).write_text(json.dumps(legacy_job),'utf-8')
            self.assertFalse(any(row['source_path']==source for row in service.view(legacy_job)['items']))
            with self.assertRaises(ValueError):service.read('fixture',job['job_id'],source)
            from knowledge_bridge.continuous_organization import ContinuousOrganizer
            continuous=ContinuousOrganizer(context)
            policy={'source_roots':['90_原始档案']}
            @cv.visibility_operation
            def batch_checks():
                for _ in range(10):
                    self.assertFalse(continuous.source_allowed(source,policy))
                    self.assertTrue(continuous.source_allowed(context.relative(main/'messages.jsonl'),policy))
            with patch.object(cv,'SourceVisibility',wraps=cv.SourceVisibility) as snapshot:
                batch_checks();self.assertEqual(1,snapshot.call_count)
            with patch.object(Path,'read_bytes',side_effect=AssertionError('status must not read full conversation bodies')):
                with patch.object(cv,'SourceVisibility',wraps=cv.SourceVisibility) as snapshot:
                    service.view(legacy_job);self.assertEqual(1,snapshot.call_count)
            stale = [{'source_path': context.relative(internal/'conversation.md'), 'content': (internal/'conversation.md').read_text('utf-8'), '_distance': 0}]
            from knowledge_bridge.passage_search import search
            control=search('unrelated','evidence',context,[internal/'conversation.md'],vector_hits=stale,requested_mode='vector')
            self.assertGreater(control['vector_matched_passages'],0)
            results = search_scope('unrelated', 'evidence', 10, context, vector_hits=stale,requested_mode='vector')
            self.assertEqual(0,results['vector_matched_passages'])
            self.assertFalse(any('child' in str(row.get('source_path')) for row in results['results']))
            for path, content in before.items():self.assertEqual(content, Path(path).read_bytes())

    def test_hermes_excluded_batch_advances_cursor_without_copying(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);db_path=root/'state.db';state=root/'adapter.json'
            db=sqlite3.connect(db_path)
            db.executescript('CREATE TABLE sessions(id TEXT,source TEXT,started_at REAL,parent_session_id TEXT,model_config TEXT,end_reason TEXT,ended_at REAL,session_key TEXT); CREATE TABLE messages(id INTEGER,session_id TEXT,role TEXT,content TEXT,timestamp REAL,token_count INTEGER,platform_message_id TEXT,display_metadata TEXT,active INTEGER);')
            db.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?)', ('parent','desktop',1,None,'{}','ws_orphan_reap',2,None))
            db.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?)', ('child','desktop',3,'parent','{}',None,None,None))
            for i in range(1, 56):db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)',(i,'child','user','internal @image:C:/private.png',i,None,None,None,1))
            db.commit();db.close()
            state.write_text(json.dumps({'cursor':0}), 'utf-8')
            with patch.object(ha,'STATE',state), patch.object(ha,'list_sessions',return_value=[]), patch('knowledge_bridge.memory_control.interface_state',return_value={'interfaces':{'save_raw_conversation':{'enabled':True}}}), patch.object(ha,'session_start') as start, patch.object(ha,'session_append') as append, patch.object(ha,'_attachments') as attachments:
                first=ha.sync_once(db_path);second=ha.sync_once(db_path)
                self.assertEqual(50, first['cursor']);self.assertEqual(55,second['cursor'])
                self.assertEqual(0,second['synced_messages']);self.assertEqual(55,second['excluded_messages'])
                self.assertIn('child',second['excluded_sessions'])
                start.assert_not_called();append.assert_not_called();attachments.assert_not_called()

    def test_hermes_child_image_cannot_jump_over_main_message_batches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);db_path=root/'state.db';state=root/'adapter.json'
            db=sqlite3.connect(db_path)
            db.executescript('CREATE TABLE sessions(id TEXT,source TEXT,started_at REAL,parent_session_id TEXT,model_config TEXT,end_reason TEXT,ended_at REAL,session_key TEXT); CREATE TABLE messages(id INTEGER,session_id TEXT,role TEXT,content TEXT,timestamp REAL,token_count INTEGER,platform_message_id TEXT,display_metadata TEXT,active INTEGER);')
            db.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?)',('parent','desktop',1,None,'{}','ws_orphan_reap',2,None))
            db.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?)',('child','desktop',3,'parent','{}',None,None,None))
            for i in range(1,101):db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)',(i,'parent','user','好',i,None,None,None,1))
            db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)',(101,'child','assistant','internal @image:C:/private.png',101,None,None,None,1))
            db.commit();db.close();state.write_text(json.dumps({'cursor':0}),'utf-8')
            with patch.object(ha,'STATE',state),patch.object(ha,'list_sessions',return_value=[]),patch('knowledge_bridge.memory_control.interface_state',return_value={'interfaces':{'save_raw_conversation':{'enabled':True}}}),patch.object(ha,'session_start'),patch.object(ha,'session_append',return_value={'record':{},'session':{'turn_count':1}}) as append,patch.object(ha,'_attachments',return_value=[]),patch('knowledge_bridge.live_acceptance.record_message'):
                first=ha.sync_once(db_path);second=ha.sync_once(db_path);third=ha.sync_once(db_path)
                self.assertEqual([50,100,101],[first['cursor'],second['cursor'],third['cursor']])
                self.assertEqual(100,third['synced_messages']);self.assertEqual(1,third['excluded_messages'])
                self.assertEqual(100,append.call_count)
                self.assertTrue(all(call.args[0]=='hermes:parent' for call in append.call_args_list))


if __name__=='__main__':unittest.main()
