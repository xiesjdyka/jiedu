import json
import hashlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from knowledge_bridge.four_local_collectors import (
    LocalCompletedCollector, _kimi, _opencode, _user_query, _workbuddy, _zcode)
from knowledge_bridge.attachment_integrity import store_verified
from knowledge_bridge.client_access import ClientIdentityRegistry, ClientPermissionService
from knowledge_bridge.client_pairing import PairingTransactionManager
from knowledge_bridge.visible_conversations import conversation
from knowledge_bridge.workspace import initialize_workspace


class FourLocalCollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.workspace=initialize_workspace(self.root/'workspace')

    def tearDown(self):
        self.temp.cleanup()

    def _workbuddy_fixture(self):
        folder=self.root/'.workbuddy/projects/example'
        folder.mkdir(parents=True)
        path=folder/'wb-session.jsonl'
        rows=[
            {'type':'message','role':'user','id':'u1','sessionId':'wb-session','timestamp':1,
             'content':[{'type':'input_text','text':'<system-reminder>private</system-reminder>\n<user_query>First user</user_query>'}]},
            {'type':'reasoning','id':'secret','sessionId':'wb-session','rawContent':[{'type':'reasoning_text','text':'PRIVATE_THOUGHT'}]},
            {'type':'message','role':'assistant','id':'progress1','sessionId':'wb-session','timestamp':1.5,
             'status':'streaming','content':[{'type':'output_text','text':'Working'}]},
            {'type':'message','role':'assistant','id':'a1','sessionId':'wb-session','timestamp':2,
             'status':'completed','content':[{'type':'output_text','text':'Final '},{'type':'output_text','text':'one'}]}]
        path.write_text(''.join(json.dumps(row)+'\n' for row in rows),encoding='utf-8')
        db=self.root/'.workbuddy/workbuddy.db'
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute('create table sessions (id text,title text,custom_title text,updated_at integer,deleted_at integer)')
            conn.execute('insert into sessions values (?,?,?,?,?)',('wb-session','WorkBuddy',None,1,None))
        return path

    def test_workbuddy_default_is_paused_and_sync_resumes_without_duplicates(self):
        path=self._workbuddy_fixture()
        adapter=LocalCompletedCollector('workbuddy',self.workspace,source_root=self.root)
        self.assertFalse(adapter.state_path.exists())
        self.assertEqual(1,len(adapter.discover()))
        self.assertFalse(adapter.state_path.exists())
        self.assertEqual(0,adapter.sync()['saved_this_sync'])
        self.assertFalse(adapter.state_path.exists())
        with patch('knowledge_bridge.client_access.ClientIdentityRegistry.list_clients',
                   return_value=[{'client_type':'workbuddy','status':'read_verified','last_read_at':'now'}]):
            adapter.configure('all',owner_confirmed=True)
        self.assertEqual(2,adapter.sync()['saved_this_sync'])
        self.assertEqual(0,adapter.sync()['saved_this_sync'])
        self.assertEqual(['user','assistant_progress','assistant'],
                         [row['role'] for row in conversation('workbuddy','wb-session',self.workspace)['messages']])
        raw=b''.join(p.read_bytes() for p in self.workspace.raw_message_store.glob('*.jsonl'))
        self.assertNotIn(b'PRIVATE_THOUGHT',raw)
        self.assertNotIn(b'private</system-reminder>',raw)
        with path.open('a',encoding='utf-8') as stream:
            stream.write(json.dumps({'type':'message','role':'user','id':'u-pending',
                'sessionId':'wb-session','timestamp':3,'content':[{'type':'input_text','text':'Pending user'}]})+'\n')
        pending=adapter.sync()
        self.assertEqual(0,pending['saved_this_sync'])
        self.assertEqual(1,pending['history']['pending_turns'])
        self.assertIsNone(pending['last_error'])
        with path.open('a',encoding='utf-8') as stream:
            stream.write(json.dumps({'type':'message','role':'user','id':'u2',
                'sessionId':'wb-session','timestamp':4,'content':[{'type':'input_text','text':'Second user'}]})+'\n')
            stream.write(json.dumps({'type':'message','role':'assistant','id':'a2',
                'sessionId':'wb-session','timestamp':5,'status':'completed',
                'content':[{'type':'output_text','text':'Final two'}]})+'\n')
        final=adapter.sync()
        self.assertEqual(2,final['saved_this_sync'])
        self.assertEqual(0,adapter.sync()['saved_this_sync'])
        self.assertEqual(4,adapter.status()['history']['archived_messages'])

    def test_workbuddy_wrapper_without_user_query_fails_closed(self):
        self.assertIsNone(_user_query('<system-reminder>hidden</system-reminder>'))
        self.assertIsNone(_user_query('<system_reminder>hidden</system_reminder>'))

    def test_workbuddy_image_backfills_existing_user_without_rewriting_messages(self):
        path=self._workbuddy_fixture()
        blob=self.root/'.workbuddy/blobs/user-image.png'
        blob.parent.mkdir(parents=True)
        image=b'\x89PNG\r\n\x1a\nfixture-image-bytes'
        blob.write_bytes(image)
        source_rows=[json.loads(line) for line in path.read_text('utf-8').splitlines()]
        source_rows[0]['content'].append({'type':'input_image','image':{
            'type':'image/png','blob_id':hashlib.sha256(image).hexdigest(),
            'blob_path':str(blob),'mime':'image/png','size':len(image),
            'original_filename':'user-image.png'}})
        path.write_text(''.join(json.dumps(row)+'\n' for row in source_rows),encoding='utf-8')
        original_source=hashlib.sha256(path.read_bytes()).hexdigest()
        adapter=LocalCompletedCollector('workbuddy',self.workspace,source_root=self.root)
        with patch('knowledge_bridge.client_access.ClientIdentityRegistry.list_clients',
                   return_value=[{'client_type':'workbuddy','status':'read_verified','last_read_at':'now'}]):
            adapter.configure('all',owner_confirmed=True)
        with patch('knowledge_bridge.four_local_collectors._workbuddy_images',return_value=()):
            self.assertEqual(2,adapter.sync()['saved_this_sync'])
        raw_path=next(self.workspace.raw_message_store.glob('*.jsonl'))
        original_archive=raw_path.read_bytes()
        state=json.loads(adapter.state_path.read_text('utf-8'))
        state['sessions']['wb-session'].pop('attachment_scan_version')
        adapter.state_path.write_text(json.dumps(state),encoding='utf-8')
        result=adapter.sync()
        self.assertEqual(0,result['saved_this_sync'])
        self.assertEqual(original_archive,raw_path.read_bytes())
        self.assertEqual(original_source,hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(1,result['attachments']['available'])
        self.assertEqual(0,result['attachments']['missing'])
        files=[p for p in self.workspace.raw_archive.rglob('*.png') if p.is_file()]
        self.assertEqual(1,len(files))
        self.assertEqual(hashlib.sha256(image).hexdigest(),hashlib.sha256(files[0].read_bytes()).hexdigest())
        self.assertEqual(0,adapter.sync()['saved_this_sync'])

    def test_workbuddy_image_outside_source_root_is_only_missing_reference(self):
        path=self._workbuddy_fixture()
        outside=self.root/'outside.png'
        outside.write_bytes(b'outside-image')
        rows=[json.loads(line) for line in path.read_text('utf-8').splitlines()]
        rows[0]['content'].append({'type':'input_image','image':{
            'blob_id':hashlib.sha256(outside.read_bytes()).hexdigest(),
            'blob_path':str(outside),'mime':'image/png','size':outside.stat().st_size,
            'original_filename':'outside.png'}})
        path.write_text(''.join(json.dumps(row)+'\n' for row in rows),encoding='utf-8')
        turns,_,_,_=_workbuddy(path,'wb-session')
        self.assertEqual(1,len(turns))
        self.assertIsNone(turns[0]['attachments'][0].local_path)

    def test_workbuddy_pure_image_turn_is_archived_with_empty_user_text(self):
        path=self._workbuddy_fixture()
        blob=self.root/'.workbuddy/blobs/pure.png'
        blob.parent.mkdir(parents=True)
        image=b'pure-image-bytes';blob.write_bytes(image)
        rows=[json.loads(line) for line in path.read_text('utf-8').splitlines()]
        rows[0]['content']=[{'type':'input_image','image':{'blob_id':hashlib.sha256(image).hexdigest(),
            'blob_path':str(blob),'mime':'image/png','size':len(image),'original_filename':'pure.png'}}]
        path.write_text(''.join(json.dumps(row)+'\n' for row in rows),encoding='utf-8')
        adapter=LocalCompletedCollector('workbuddy',self.workspace,source_root=self.root)
        adapter.state_path.write_text(json.dumps({'policy':'all','resume_policy':'all',
            'selected_session_ids':[],'selected_sources':{},'sessions':{},'last_sync_at':None,'last_error':None}),
            encoding='utf-8')
        result=adapter.sync()
        self.assertEqual(2,result['saved_this_sync'])
        self.assertEqual(1,result['attachments']['available'])
        raw=next(self.workspace.raw_message_store.glob('*.jsonl')).read_text('utf-8').splitlines()
        self.assertEqual('',json.loads(raw[0])['original_text'])

    def test_workbuddy_changed_image_is_rejected_before_publish(self):
        path=self.root/'source.png';path.write_bytes(b'original')
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        path.write_bytes(b'replaced')  # same size, different content
        folder=self.root/'archived'
        with self.assertRaisesRegex(OSError,'核验后发生变化'):
            store_verified(folder,lambda sha:sha+'.png',source=path,
                           expected_sha256=digest,expected_size=8)
        self.assertEqual([],list(folder.glob('*.png')))

    def test_missing_indexed_workbuddy_history_is_visible_and_scope_aware(self):
        self._workbuddy_fixture()
        db=self.root/'.workbuddy/workbuddy.db'
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute('insert into sessions values (?,?,?,?,?)',('missing','Missing',None,2,None))
        adapter=LocalCompletedCollector('workbuddy',self.workspace,source_root=self.root)
        adapter.state_path.write_text(json.dumps({'policy':'all','resume_policy':'all',
            'selected_session_ids':[],'selected_sources':{},'sessions':{},'last_sync_at':None,'last_error':None}),
            encoding='utf-8')
        state=adapter.status()
        self.assertEqual(1,state['indexed_without_transcript']['in_scope_count'])
        self.assertIn('缺少可读转录',state['history_coverage'])
        selected=adapter._load()
        selected['policy']='selected'
        selected['selected_session_ids']=['wb-session']
        selected['selected_sources']={'wb-session':str(next((self.root/'.workbuddy/projects').glob('*/*.jsonl')).resolve())}
        adapter.state_path.write_text(json.dumps(selected),encoding='utf-8')
        self.assertEqual(0,adapter.status()['indexed_without_transcript']['in_scope_count'])

    def test_manual_branded_pairing_waits_for_real_read_and_reuses_identity(self):
        registry=ClientIdentityRegistry(self.workspace,self.root/'machine')
        manager=PairingTransactionManager(registry,ClientPermissionService(registry))
        with patch('knowledge_bridge.client_pairing.source_adapter_for_client',
                   return_value={'adapter_id':'workbuddy_local'}):
            first=manager.quick_connect('workbuddy')
            self.assertEqual('paired',first['outcome'])
            self.assertTrue(first['secret_once'])
            self.assertEqual('waiting_for_verified_read',first['auto_archive'])
            self.assertFalse((self.workspace.sync/'workbuddy-source-adapter.json').exists())
            second=manager.quick_connect('workbuddy')
            self.assertEqual('reused',second['outcome'])
            self.assertFalse(second['secret_available'])
            self.assertEqual(1,len(registry.list_clients()))

    def test_opencode_requires_idle_success_and_excludes_system_reasoning(self):
        dbpath=self.root/'opencode.sqlite'
        with closing(sqlite3.connect(dbpath)) as db, db:
            db.execute('create table session_message (id text,session_id text,type text,seq integer,data text)')
            rows=[('s1','system',0,{'text':'PRIVATE_SYSTEM'}),
                  ('u1','user',1,{'text':'User','time':{'created':1}}),
                  ('a1','assistant',2,{'finish':'stop','time':{'created':2,'completed':3},
                    'content':[{'type':'reasoning','text':'PRIVATE_THOUGHT'},
                               {'type':'text','text':'Final '},{'type':'text','text':'answer'}]}),
                  ('idle','idle',3,{'outcome':'succeeded'})]
            for mid,typ,seq,data in rows:
                db.execute('insert into session_message values (?,?,?,?,?)',(mid,'session',typ,seq,json.dumps(data)))
            turns,pending,unverified,progress=_opencode(db,'session')
            self.assertEqual((1,0,0,0),(len(turns),pending,unverified,len(progress)))
            self.assertEqual('Final answer',turns[0]['final_text'])
            db.execute("delete from session_message where type='idle'")
            turns,pending,_,_=_opencode(db,'session')
            self.assertEqual((0,1),(len(turns),pending))

    def test_opencode_wal_append_is_seen_with_stable_main_db_stat(self):
        path=self.root/'.local/share/opencode/opencode.db'
        path.parent.mkdir(parents=True)
        writer=sqlite3.connect(path)
        try:
            writer.execute('pragma journal_mode=WAL')
            writer.execute('create table session_v2 (id text,title text,time_updated integer)')
            writer.execute('create table session_message (id text,session_id text,type text,seq integer,data text)')
            writer.execute('insert into session_v2 values (?,?,?)',('session','OpenCode',1))
            def append(number):
                base=number*10
                rows=[(f'u{number}','user',base,{'text':f'User {number}','time':{'created':base}}),
                      (f'a{number}','assistant',base+1,{'finish':'stop','time':{'created':base+1,'completed':base+2},
                        'content':[{'type':'text','text':f'Final {number}'}]}),
                      (f'idle{number}','idle',base+2,{'outcome':'succeeded'})]
                for mid,kind,seq,data in rows:
                    writer.execute('insert into session_message values (?,?,?,?,?)',
                                   (mid,'session',kind,seq,json.dumps(data)))
                writer.commit()
            append(1)
            adapter=LocalCompletedCollector('opencode',self.workspace,source_root=self.root)
            adapter.state_path.write_text(json.dumps({'policy':'all','resume_policy':'all',
                'selected_session_ids':[],'selected_sources':{},'sessions':{},'last_sync_at':None,'last_error':None}),
                encoding='utf-8')
            self.assertEqual(2,adapter.sync()['saved_this_sync'])
            before=(path.stat().st_size,path.stat().st_mtime_ns)
            append(2)
            self.assertEqual(before,(path.stat().st_size,path.stat().st_mtime_ns))
            self.assertEqual(2,adapter.sync()['saved_this_sync'])
            self.assertEqual(0,adapter.sync()['saved_this_sync'])
        finally:
            writer.close()

    def test_late_completed_earlier_turn_sorts_before_later_archived_turn(self):
        path=self.root/'.local/share/opencode/opencode.db'
        path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('create table session_v2 (id text,title text,time_updated integer)')
            db.execute('create table session_message (id text,session_id text,type text,seq integer,data text)')
            db.execute('insert into session_v2 values (?,?,?)',('session','OpenCode',1))
            def add(mid,kind,seq,data):
                db.execute('insert into session_message values (?,?,?,?,?)',
                           (mid,'session',kind,seq,json.dumps(data)))
            add('u1','user',10,{'text':'First user','time':{'created':10}})
            add('u2','user',20,{'text':'Second user','time':{'created':20}})
            add('a2','assistant',21,{'finish':'stop','time':{'created':21,'completed':22},
                'content':[{'type':'text','text':'Second final'}]})
            add('idle2','idle',22,{'outcome':'succeeded'})
        adapter=LocalCompletedCollector('opencode',self.workspace,source_root=self.root)
        adapter.state_path.write_text(json.dumps({'policy':'all','resume_policy':'all',
            'selected_session_ids':[],'selected_sources':{},'sessions':{},'last_sync_at':None,'last_error':None}),
            encoding='utf-8')
        self.assertEqual(2,adapter.sync()['saved_this_sync'])
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('insert into session_message values (?,?,?,?,?)',('a1','session','assistant',11,
                json.dumps({'finish':'stop','time':{'created':11,'completed':12},
                            'content':[{'type':'text','text':'First final'}]})))
            db.execute('insert into session_message values (?,?,?,?,?)',('idle1','session','idle',12,
                json.dumps({'outcome':'succeeded'})))
        self.assertEqual(2,adapter.sync()['saved_this_sync'])
        shown=conversation('opencode','session',self.workspace)
        self.assertEqual(['First user','First final','Second user','Second final'],
                         [row['text'] for row in shown['messages']])
        registry=json.loads(self.workspace.archive_registry.read_text('utf-8'))
        item=next(iter(registry['sessions'].values()))
        materialized=(self.workspace.root/item['relative_path']/'03_元数据/messages.jsonl').read_text('utf-8')
        self.assertLess(materialized.index('First user'),materialized.index('Second user'))

    def test_zcode_ignores_synthetic_user_and_joins_final_parts(self):
        path=self.root/'zcode.sqlite'
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('create table message (id text,session_id text,data text,sequence integer)')
            db.execute('create table part (id text,message_id text,session_id text,data text,sequence integer)')
            visible={'origin':'real_user','uiVisibility':'visible','transcriptVisibility':'visible'}
            hidden={'origin':'agent_runtime','uiVisibility':'hidden','transcriptVisibility':'hidden'}
            rows=[('u1',0,{'role':'user','semantics':visible,'time':{'created':1}}),
                  ('fake',1,{'role':'user','synthetic':True,'semantics':hidden}),
                  ('a1',2,{'role':'assistant','semantics':visible,'finish':'stop',
                           'time':{'created':2,'completed':3}})]
            for mid,seq,data in rows:
                db.execute('insert into message values (?,?,?,?)',(mid,'session',json.dumps(data),seq))
            for mid,pid,seq,part in [('u1','pu',0,{'type':'text','text':'User'}),
                                     ('u1','ps',1,{'type':'text','text':'PRIVATE_SYSTEM','synthetic':True}),
                                     ('fake','pf',0,{'type':'text','text':'PRIVATE_SYSTEM'}),
                                     ('a1','pr',0,{'type':'reasoning','text':'PRIVATE_THOUGHT'}),
                                     ('a1','pt1',1,{'type':'text','text':'Final '}),
                                     ('a1','pt2',2,{'type':'text','text':'answer'})]:
                db.execute('insert into part values (?,?,?,?,?)',(pid,mid,'session',json.dumps(part),seq))
            turns,pending,unverified,progress=_zcode(db,'session')
            self.assertEqual((1,0,0,0),(len(turns),pending,unverified,len(progress)))
            self.assertEqual('User',turns[0]['user_text'])
            self.assertEqual('Final answer',turns[0]['final_text'])
            db.execute('insert into part values (?,?,?,?,?)',('pfile','u1','session',json.dumps({
                'type':'file','mime':'image/png','url':'zcode-artifact://opaque-image',
                'metadata':{'storageKind':'artifact','recoverability':'provider_ready'}}),2))
            turns,_,_,_=_zcode(db,'session')
            self.assertEqual(1,len(turns[0]['attachments']))
            db.execute("delete from part where message_id='u1' and json_extract(data,'$.type')='text'")
            turns,_,_,_=_zcode(db,'session')
            self.assertEqual('',turns[0]['user_text'])
            self.assertEqual(1,len(turns[0]['attachments']))
            self.assertIsNone(turns[0]['attachments'][0].local_path)
            self.assertTrue(turns[0]['attachments'][0].reference.startswith('zcode-artifact:'))
            for seq,part in enumerate(({'synthetic':True},{'visibility':'hidden'},
                                       {'semantics':{'transcriptVisibility':'hidden'}}),start=3):
                db.execute('insert into part values (?,?,?,?,?)',(f'hidden{seq}','u1','session',json.dumps({
                    'type':'file','mime':'image/png','url':f'zcode-artifact://hidden{seq}',**part}),seq))
            turns,_,_,_=_zcode(db,'session')
            self.assertEqual(1,len(turns[0]['attachments']))

    def test_kimi_requires_matching_step_and_ignores_duplicate_user(self):
        path=self.root/'wire.jsonl'
        rows=[{'type':'turn.prompt','origin':{'kind':'system_trigger'},'input':[{'type':'text','text':'PRIVATE_SYSTEM'}]},
              {'type':'turn.prompt','origin':{'kind':'user'},'time':1,
               'input':[{'type':'text','text':'<meta awareness timestamp="x"/>User'}]},
              {'type':'context.append_message','message':{'role':'user','content':[{'type':'text','text':'DUPLICATE_USER'}]}},
              {'type':'context.append_loop_event','event':{'type':'step.begin','uuid':'step1','turnId':'0'}},
              {'type':'context.append_loop_event','event':{'type':'content.part','uuid':'part-think','stepUuid':'step1',
                'turnId':'0','part':{'type':'think','think':'PRIVATE_THOUGHT'}}},
              {'type':'context.append_loop_event','event':{'type':'content.part','uuid':'part1','stepUuid':'step1',
                'turnId':'0','part':{'type':'text','text':'Final '}}},
              {'type':'context.append_loop_event','event':{'type':'content.part','uuid':'part2','stepUuid':'step1',
                'turnId':'0','part':{'type':'text','text':'answer'}}},
              {'type':'context.append_loop_event','event':{'type':'step.end','uuid':'step1','turnId':'0',
                'messageId':'a1','finishReason':'end_turn'}}]
        path.write_text(''.join(json.dumps(row)+'\n' for row in rows),encoding='utf-8')
        turns,pending,unverified,progress=_kimi(path)
        self.assertEqual((1,0,0,0),(len(turns),pending,unverified,len(progress)))
        self.assertEqual('User',turns[0]['user_text'])
        self.assertEqual('Final answer',turns[0]['final_text'])
        rows[-1]['event']['uuid']='wrong-step'
        path.write_text(''.join(json.dumps(row)+'\n' for row in rows),encoding='utf-8')
        turns,pending,unverified,_=_kimi(path)
        self.assertEqual(0,len(turns))
        self.assertGreater(unverified,0)


if __name__=='__main__':unittest.main()
