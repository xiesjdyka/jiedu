import base64
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import knowledge_bridge.client_config as cc
from knowledge_bridge.client_access import ClientIdentityRegistry, ClientPermissionService
from knowledge_bridge.client_pairing import PairingTransactionManager
from knowledge_bridge.cursor_source_adapter import CursorSourceAdapter, _completed_turns
from knowledge_bridge.visible_conversations import conversation, list_conversations
from knowledge_bridge.workspace import initialize_workspace


SID = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'


def _bubble(bid, kind, text='', capability=None, **extra):
    return dict(bubbleId=bid, type=kind, text=text,
                createdAt='2026-10-23T00:00:0' + str(len(bid)) + 'Z',
                **({'capabilityType': capability} if capability else {}), **extra)


class CursorLocalCollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = initialize_workspace(self.root / 'workspace')
        self.db = self.root / 'state.vscdb'
        self.projects = self.root / 'projects'
        self.path = self.projects / 'example' / 'agent-transcripts' / SID / (SID + '.jsonl')
        self.path.parent.mkdir(parents=True)
        self.conn = sqlite3.connect(self.db)
        self.conn.execute('create table cursorDiskKV (key text primary key, value text)')

    def tearDown(self):
        if self.conn is not None:
            self.conn.close()
        self.temp.cleanup()

    def _source(self, *, second='success'):
        turns = [
            {'role':'user','message':{'content':[{'type':'text','text':'<timestamp>synthetic</timestamp>\n<user_query>Question one</user_query>'}]}},
            {'role':'assistant','message':{'content':[{'type':'text','text':'Working…'}, {'type':'tool_use','name':'read'}]}},
            {'role':'assistant','message':{'content':[{'type':'text','text':'Final one'}]}},
            {'type':'turn_ended','status':'success'},
            {'role':'user','message':{'content':[{'type':'text','text':'<user_query>Question two</user_query>'}]}},
            {'role':'assistant','message':{'content':[{'type':'text','text':'Final two'}]}},
            {'type':'turn_ended','status':second},
        ]
        self.path.write_text(''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in turns),encoding='utf-8')
        bubbles = [_bubble('user-one',1,'Question one'), _bubble('progress-one',2,'Working…'),
                   _bubble('thinking-one',2,'HIDDEN_THINKING',30,thinking='HIDDEN_THINKING'),
                   _bubble('tool-one',2,'HIDDEN_TOOL',15,toolFormerData={'x':'HIDDEN_TOOL'}),
                   _bubble('final-one',2,'Final one'), _bubble('user-two',1,'Question two'),
                   _bubble('final-two',2,'Final two')]
        for index, bubble in enumerate(bubbles):
            bubble['createdAt'] = f'2026-10-23T00:00:0{index}Z'
        composer = {'fullConversationHeadersOnly':[{'bubbleId':x['bubbleId'],'type':x['type']} for x in bubbles],
                    'lastUpdatedAt':1,'name':'Synthetic Cursor'}
        self.conn.execute('insert or replace into cursorDiskKV values (?,?)',('composerData:'+SID,json.dumps(composer)))
        for x in bubbles:
            self.conn.execute('insert or replace into cursorDiskKV values (?,?)',
                              ('bubbleId:'+SID+':'+x['bubbleId'],json.dumps(x)))
        self.conn.commit()
        self.conn.close()
        self.conn = None

    def test_handoff_read_enables_collection_and_progress_is_view_only(self):
        self._source(second='failed')
        registry = ClientIdentityRegistry(self.workspace)
        service = PairingTransactionManager(registry, ClientPermissionService(registry))
        config = cc.CursorAdapter()
        config.path = self.root / '.cursor' / 'mcp.json'
        config.path.parent.mkdir()
        with patch.dict(cc.ADAPTERS, {'cursor':config}), \
             patch.object(cc,'DATA',self.root/'config-state'), \
             patch.object(cc,'BACKUPS',self.root/'config-state'/'backups'), \
             patch.object(cc,'AUDIT',self.root/'config-state'/'audit.jsonl'), \
             patch('knowledge_bridge.cursor_source_adapter.default_paths',return_value=(self.db,self.projects)):
            paired = service.quick_connect('cursor')
            self.assertEqual('paired', paired['outcome'])
            self.assertFalse((self.workspace.sync/'cursor-source-adapter.json').exists())
            self.assertEqual('cursor',json.loads(config.path.read_text())['mcpServers']['knowledge_bridge']['env']['KNOWLEDGE_BRIDGE_CLIENT_ID'])
            # This exercises the same post-read registry callback used by MCP.
            row = registry.list_clients()[0]
            from types import SimpleNamespace
            authenticated = SimpleNamespace(internal_id=row['internal_id'], authenticated=True)
            with patch.object(registry,'identity_current',return_value=True):
                registry.mark_read_success(authenticated,'formal','knowledge_search')
            state = self.workspace.sync/'cursor-source-adapter.json'
            self.assertTrue(state.exists())
            adapter = CursorSourceAdapter(self.workspace)
            self.assertEqual('all',adapter.status()['policy'])
            self.assertEqual(3,adapter.sync()['saved_this_sync'])
            self.assertEqual(0,adapter.sync()['saved_this_sync'])
            listing = list_conversations('cursor',self.workspace)
            self.assertEqual(2,listing['sessions'][0]['visible_progress'])
            shown = conversation('cursor',SID,self.workspace)
            self.assertEqual(3,shown['saved_messages'])
            self.assertEqual(2,shown['visible_progress'])
            self.assertEqual(['user','assistant_progress','assistant','user','assistant_progress'],
                             [x['role'] for x in shown['messages']])
            archived = b''.join(x.read_bytes() for x in self.workspace.raw_archive.rglob('*') if x.is_file())
            self.assertNotIn(b'Working',archived)
            self.assertNotIn(b'HIDDEN_THINKING',archived)
            self.assertNotIn(b'HIDDEN_TOOL',archived)
            adapter.configure('paused',owner_confirmed=True)
            cursor_before=adapter._load()['sessions'][SID]['checked_turns']
            with patch.object(registry,'identity_current',return_value=True):
                registry.mark_read_success(authenticated,'formal','knowledge_search')
            self.assertEqual('paused',adapter.status()['policy'])
            self.assertEqual(cursor_before,adapter._load()['sessions'][SID]['checked_turns'])
            from knowledge_bridge import web
            from unittest.mock import Mock
            allowed=Mock();allowed._policy.return_value=web.PolicyDecision.ALLOW
            prior=registry.list_clients()[0]
            self.assertTrue(web._client_read_verification_current(prior,[],allowed,
                web._client_configuration_current(prior,registry),web._cursor_configuration_detail(prior)['modified_at']))
            config.path.unlink()
            self.assertFalse(web._client_configuration_current(prior,registry))
            self.assertFalse(web._cursor_configuration_detail(prior)['present'])
            self.assertFalse(web._client_read_verification_current(prior,[],allowed,False))
            repaired=service.quick_connect('cursor')
            self.assertEqual('repaired',repaired['outcome'])
            current=registry.list_clients()[0]
            self.assertEqual(prior['internal_id'],current['internal_id'])
            self.assertTrue(web._client_configuration_current(current,registry))
            self.assertFalse(web._client_read_verification_current(current,[],allowed,True))
            with patch.object(registry,'identity_current',return_value=True):
                registry.mark_read_success(authenticated,'formal','knowledge_search')
            current=registry.list_clients()[0]
            self.assertTrue(web._client_read_verification_current(current,[],allowed,True,
                web._cursor_configuration_detail(current)['modified_at']))
            self.assertEqual('paused',adapter.status()['policy'])
            self.assertEqual(cursor_before,adapter._load()['sessions'][SID]['checked_turns'])

    def test_failed_turn_keeps_alignment_and_partial_tail_is_pending(self):
        self._source(second='failed')
        with self.path.open('a',encoding='utf-8') as stream:
            stream.write(json.dumps({'role':'user','message':{'content':[{'type':'text','text':'unfinished'}]}})+'\n')
        turns,pending=_completed_turns(self.path)
        self.assertEqual(['success','failed','pending'],[x[0] for x in turns])
        self.assertTrue(pending)

    def _enable_without_client(self):
        state = self.workspace.sync/'cursor-source-adapter.json'
        state.write_text(json.dumps({'policy':'all','resume_policy':'all',
                                     'selected_session_ids':[],'selected_sources':{},
                                     'sessions':{},'last_sync_at':None,'last_error':None}),encoding='utf-8')
        return CursorSourceAdapter(self.workspace,database=self.db,projects=self.projects)

    def test_pending_reply_is_not_a_collector_error(self):
        self._source()
        lines = self.path.read_text('utf-8').splitlines(keepends=True)
        self.path.write_text(''.join(lines[:-1]),encoding='utf-8')
        adapter = self._enable_without_client()
        result = adapter.sync()
        self.assertTrue(result['sessions'][SID]['pending_turn'])
        self.assertEqual(1,result['history']['pending_turns'])
        self.assertIsNone(result['sessions'][SID]['last_error'])
        self.assertIsNone(result['last_error'])
        self.assertEqual(3,result['history']['archived_messages'])

    def test_db_only_nonempty_history_is_reported_and_empty_drafts_ignored(self):
        self._source()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('insert into cursorDiskKV values (?,?)',
                         ('composerData:db-only',json.dumps({'fullConversationHeadersOnly':
                           [{'bubbleId':'orphan-user','type':1}]})))
            conn.execute('insert into cursorDiskKV values (?,?)',
                         ('composerData:empty-draft',json.dumps({'fullConversationHeadersOnly':[]})))
        adapter = self._enable_without_client()
        state = adapter.sync()
        self.assertEqual(1,state['indexed_without_transcript']['count'])
        self.assertEqual('db-only',state['indexed_without_transcript']['sample'][0]['session_id'])
        self.assertEqual(4,state['history']['archived_messages'])
        with patch('knowledge_bridge.cursor_source_adapter._completed_turns',
                   side_effect=AssertionError('unchanged session reparsed')):
            self.assertEqual(0,adapter.sync()['saved_this_sync'])
        selected=adapter._load()
        selected['policy']='selected'
        selected['selected_session_ids']=[SID]
        selected['selected_sources']={SID:str(self.path)}
        adapter.state_path.write_text(json.dumps(selected),encoding='utf-8')
        self.assertEqual(0,adapter.status()['indexed_without_transcript']['in_scope_count'])

    def test_composer_draft_image_is_not_archived(self):
        self._source()
        image_path = self.root/'cursor-image.png'
        image_path.write_bytes(base64.b64decode(
            'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/RZkAAAAASUVORK5CYII='))
        draft = {'uuid':'draft-only','path':str(image_path)}
        with closing(sqlite3.connect(self.db)) as conn, conn:
            key='composerData:'+SID
            composer=json.loads(conn.execute('select value from cursorDiskKV where key=?',(key,)).fetchone()[0])
            composer['context']={'selectedImages':[draft]}
            conn.execute('update cursorDiskKV set value=? where key=?',(json.dumps(composer),key))
        adapter=self._enable_without_client()
        self.assertEqual(4,adapter.sync()['history']['archived_messages'])
        self.assertFalse(list(self.workspace.raw_archive.rglob('attachments.jsonl')))


if __name__=='__main__':unittest.main()
