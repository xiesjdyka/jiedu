import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, nullcontext
from pathlib import Path
from unittest.mock import patch, MagicMock

from knowledge_bridge.workspace import initialize_workspace
from knowledge_bridge.archive_store import ArchiveStore, NormalizedMessage, NormalizedAttachment
from knowledge_bridge import ai_chat_bridge as chat
from knowledge_bridge import evidence_index as evidence
from knowledge_bridge import evidence_updates as queue
from knowledge_bridge import session_archive as archive
from knowledge_bridge import session_manager as manager
from knowledge_bridge import hermes_session_adapter as hermes
from knowledge_bridge.codex_source_adapter import CodexSourceAdapter


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.context=initialize_workspace(self.root/'workspace')
        self.store=ArchiveStore(self.context,recover=False)

    def test_identical_conversation_render_keeps_source_version(self):
        from knowledge_bridge.passage_search import file_version
        session=self.store.ensure_session(source_key='codex',source_session_id='stable-render',title='fixture')
        self.store.append_normalized_message(session['session_key'],NormalizedMessage('m1','user','same body'))
        self.store.materialize_session(session['session_key'])
        target=self.context.root/session['relative_path']/'01_对话/conversation.md'
        # Recreate an already installed export with the former write_text API.
        target.write_text(target.read_text('utf-8'),encoding='utf-8')
        self.assertIn(b'\r\n',target.read_bytes())
        before=(target.read_bytes(),file_version(target.stat()))
        self.store.materialize_session(session['session_key'])
        self.assertEqual(before,(target.read_bytes(),file_version(target.stat())))
        self.store.append_normalized_message(session['session_key'],NormalizedMessage('m2','assistant','new body'))
        self.store.materialize_session(session['session_key'])
        self.assertNotEqual(before[1],file_version(target.stat()))
        self.assertIn(b'new body',target.read_bytes())

    def test_corrupt_reused_entity_is_refused_without_overwrite(self):
        session=self.store.ensure_session(source_key='codex',source_session_id='s',title='fixture')
        self.store.append_normalized_message(session['session_key'],NormalizedMessage('m','assistant','file'))
        source=self.root/'附件.txt';source.write_bytes(b'original')
        spec=NormalizedAttachment('a',source.name,source)
        row=self.store.reconcile_attachments(session['session_key'],'m',[spec])[0]
        target=self.context.root/row['stored_path'];target.write_bytes(b'corrupt')
        with self.assertRaisesRegex(OSError,'SHA256'):
            self.store.reconcile_attachments(session['session_key'],'m',[spec])
        self.assertEqual(b'corrupt',target.read_bytes());self.assertEqual(b'original',source.read_bytes())
        self.assertFalse(list(target.parent.glob('.attachment-*')))

    def test_latest_attachments_single_and_page_agree(self):
        session=self.store.ensure_session(source_key='hermes',source_session_id='hermes:s',title='fixture')
        self.store.append_normalized_message(session['session_key'],NormalizedMessage('12','user','原文'))
        source=self.root/'late.txt'
        spec=NormalizedAttachment('late','late.txt',source)
        self.store.reconcile_attachments(session['session_key'],'12',[spec])
        source.write_text('late','utf-8');self.store.reconcile_attachments(session['session_key'],'12',[spec])
        with patch.object(archive,'active_workspace',return_value=self.context),patch.object(archive,'CANONICAL_ROOT',self.context.raw_archive):
            single=archive.read_message('12','hermes:s')
            page=archive.materialize_page(session['session_key'],include_attachments=True)
        self.assertEqual(single['attachments'],page['messages'][0]['attachments'])
        self.assertEqual('available',single['attachments'][0]['status'])

    def test_real_mcp_gate_preserves_numeric_and_revision_id_and_approval_scope(self):
        from knowledge_bridge import mcp_server
        from knowledge_bridge.client_access import ClientIdentityRegistry,ClientPermissionService,ClientAccessGate,normalize_message_selector
        registry=ClientIdentityRegistry(self.context,self.root/'identities')
        created=registry.create('fixture_agent','Fixture','test','stdio')
        identity=registry.authenticate('fixture_agent',created['secret'])
        permissions=ClientPermissionService(registry)
        permissions.set_policy(identity.internal_id,'evidence','read','ask')
        gate=ClientAccessGate(permissions,identity)
        session=self.store.ensure_session(source_key='hermes',source_session_id='hermes:test',title='fixture')
        revision='12~revision-'+'a'*20
        for mid in ('12','000012',revision):
            self.store.append_normalized_message(session['session_key'],NormalizedMessage(mid,'user','body:'+mid))
        sid=session['session_key']
        with patch.object(archive,'active_workspace',return_value=self.context),patch.object(archive,'CANONICAL_ROOT',self.context.raw_archive),patch.multiple(mcp_server,_IDENTITY=identity,_PERMISSIONS=permissions,_GATE=gate,
                audited=lambda *a,**k:nullcontext(),record_call=MagicMock(),_mark_read=MagicMock()):
            self.assertEqual('owner_confirmation_required',mcp_server.get_raw_message('12',sid)['status'])
            permissions.confirm(permissions.pending()[-1]['approval_id'])
            self.assertEqual('owner_confirmation_required',mcp_server.get_raw_message('000012',sid)['status'])
            self.assertEqual('owner_confirmation_required',mcp_server.get_raw_message(revision,sid)['status'])
            self.assertEqual('body:12',mcp_server.get_raw_message('12',sid)['content'])
            permissions.set_policy(identity.internal_id,'evidence','read','allow')
            self.assertEqual('body:'+revision,mcp_server.get_raw_message(revision,sid)['content'])
            self.assertEqual('body:000012',mcp_server.get_raw_message('000012',sid)['content'])
        with self.assertRaises(ValueError):normalize_message_selector('１２')
        with self.assertRaises(ValueError):normalize_message_selector('../12')

    def test_hermes_intermediate_cache_corruption_is_refused(self):
        source=self.root/'source.png';source.write_bytes(b'original-image')
        with patch.object(hermes,'ROOT',self.root):
            first=hermes._attachments('hermes:s','1',1,[str(source)])
            cached=Path(first[0]['stored_path']);cached.write_bytes(b'corrupt')
            with self.assertRaisesRegex(OSError,'SHA256'):
                hermes._attachments('hermes:s','1',1,[str(source)])
        self.assertEqual(b'original-image',source.read_bytes());self.assertEqual(b'corrupt',cached.read_bytes())
        manifest=cached.parent/'manifest.jsonl'
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),json.loads(manifest.read_text('utf-8'))['sha256'])

    def test_attachment_interrupted_publish_leaves_no_partial_entity(self):
        from knowledge_bridge.attachment_integrity import store_verified
        folder=self.root/'entities'
        with patch('knowledge_bridge.attachment_integrity.os.link',side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):store_verified(folder,lambda sha:sha+'.txt',data=b'complete')
        self.assertEqual([],list(folder.iterdir()))

    def test_context_preserves_metadata_and_actual_sent_sources(self):
        context={'user':{'documents':[{'path':'u','sha256':'v','content':'甲'*5000}]},
                 'project':{'path':'p','source_version':'v2','content':'乙'*5000},'state':{},
                 'related_knowledge':{'results':[{'source_path':'k','source_version':'v3','excerpt':'丙'*5000}]},
                 'permissions':{'formal_write':False,'rules':['owner_only']}}
        original=json.dumps(context,ensure_ascii=False)
        encoded=chat._model_context(context,1000);sent=json.loads(encoded)
        self.assertLessEqual(len(encoded),1000);self.assertEqual(context['permissions'],sent['权限状态'])
        self.assertEqual('v3',sent['相关知识']['results'][0]['source_version'])
        self.assertGreater(sent['上下文省略']['omitted_characters'],0)
        self.assertEqual(original,json.dumps(context,ensure_ascii=False))
        with self.assertRaises(ValueError):chat._model_context(context,50)
        response=MagicMock();response.__enter__.return_value=response
        response.read.return_value=b'{"choices":[{"message":{"content":"ok"}}]}'
        config={'provider_type':'openai-compatible','enabled':True,'base_url':'http://fixture.invalid',
                'model':'fixture','api_key_secret_ref':'fixture','timeout':1,'max_input':1000}
        with patch.object(chat,'load_provider',return_value=config),patch.object(chat,'context_request',return_value={'context':context,'audit':{}}),patch.object(chat,'_secret',return_value='test'),patch.object(chat,'record_call'),patch.object(chat.urllib.request,'urlopen',return_value=response) as call:
            answer=chat.ask('test')
        submitted=json.loads(call.call_args.args[0].data)['messages'][0]['content'].split('界渡上下文：\n',1)[1]
        actual=json.loads(submitted)['相关知识']['results'][0]
        self.assertEqual(actual['excerpt'],answer['sources'][0]['excerpt'])
        self.assertTrue(answer['sources'][0]['truncated'])

    def test_explicit_delivery_links_and_bounded_old_message_review(self):
        adapter=CodexSourceAdapter(self.context,codex_home=self.root/'codex',machine_data=self.root/'machine')
        source=self.root/'中文 文件.xlsx';source.write_bytes(b'fixture')
        text=f'下载 [文件](<{source}>)'
        links=adapter._delivery_attachments(text,'assistant','final')
        self.assertEqual([source],[row.local_path for row in links])
        for extension in ('.png','.mp4','.mp3'):
            media=source.with_suffix(extension)
            self.assertEqual([media],[row.local_path for row in adapter._delivery_attachments(f'![预览](<{media}>)','assistant','final')])
        self.assertEqual([],adapter._delivery_attachments('![图](https://example.org/a.png)','assistant','final'))
        for negative in [str(source),f'`{text}`',f'```\n{text}\n```',f'> {text}']:
            self.assertEqual([],adapter._delivery_attachments(negative,'assistant','final'))
        self.assertEqual([],adapter._delivery_attachments(text,'assistant','commentary'))
        session=self.store.ensure_session(source_key='codex',source_session_id='s',title='fixture')
        self.store.append_normalized_message(session['session_key'],NormalizedMessage('m','assistant',text,provenance={'assistant_phase':'final'}))
        raw=self.context.raw_message_store/f"{session['session_key']}.jsonl";before=raw.read_bytes()
        adapter._review_file_deliveries({'s'})
        self.assertEqual('available',self.store.attachment_status(session['session_key'])[0]['status'])
        self.assertEqual(before,raw.read_bytes())
        adapter._review_file_deliveries({'s'})
        self.assertEqual(1,len(self.store.attachment_status(session['session_key'])))

    def test_hermes_changes_late_empty_attachment_and_tool_cursor(self):
        db=self.root/'hermes.db';state=self.root/'hermes.json';sessions=self.root/'sessions.json';raw=self.root/'raw'
        con=sqlite3.connect(db)
        self.addCleanup(con.close)
        con.executescript('create table sessions(id text primary key,source text,started_at real); create table messages(id integer primary key,session_id text,role text,content text,timestamp real,token_count integer,platform_message_id text,display_metadata text,active integer); insert into sessions values("s","desktop",1);')
        late=self.root/'late.png'
        def insert(i,role,content,refs=()):
            con.execute('insert into messages values(?,?,?,?,?,?,?,?,?)',(i,'s',role,content,10+i,None,None,json.dumps({'attachments':list(refs)}),1));con.commit()
        with ExitStack() as stack:
            for module in (hermes,archive):stack.enter_context(patch.object(module,'active_workspace',return_value=self.context))
            stack.enter_context(patch.object(hermes,'STATE',state));stack.enter_context(patch.object(hermes,'ARCHIVE_ROOT',raw))
            stack.enter_context(patch.object(archive,'SESSIONS_PATH',sessions));stack.enter_context(patch.object(archive,'ARCHIVE_ROOT',raw));stack.enter_context(patch.object(archive,'CANONICAL_ROOT',self.context.raw_archive))
            # Make the canonical materializer use the real isolated session stores.
            stack.enter_context(patch.object(type(self.context),'sessions_state',property(lambda _:sessions)))
            stack.enter_context(patch.object(archive,'ATTACHMENT_MANIFEST',self.context.state/'attachments'/'manifest.jsonl'))
            stack.enter_context(patch('knowledge_bridge.original_archive_import.get_session',return_value=None))
            stack.enter_context(patch('knowledge_bridge.memory_control.interface_state',return_value={'interfaces':{'save_raw_conversation':{'enabled':True}}}))
            stack.enter_context(patch('knowledge_bridge.live_acceptance.record_message'))
            stack.enter_context(patch.object(hermes,'list_sessions',side_effect=lambda client:manager.list_sessions(client,state_path=sessions)))
            stack.enter_context(patch.object(hermes,'session_start',side_effect=lambda *a,**k:manager.session_start(*a,**k,state_path=sessions,archive_root=raw)))
            def append(*a,**k):
                return manager.session_append(*a,**k,state_path=sessions,archive_root=raw)
            stack.enter_context(patch.object(hermes,'session_append',side_effect=append))
            stack.enter_context(patch.object(hermes,'update_session_metadata',side_effect=lambda *a,**k:manager.update_session_metadata(*a,**k,state_path=sessions)))
            hermes.initialize(db)
            insert(1,'user','original');insert(2,'tool','@image:'+str(late));insert(3,'assistant','',[str(late)])
            materialize=archive.materialize
            def project(*args,**kwargs):
                checkpoint=json.loads(state.read_text('utf-8'))
                self.assertEqual(3,checkpoint['cursor'])
                self.assertIn('hermes:s',checkpoint['pending_materializations'])
                return materialize(*args,**kwargs)
            with patch.object(archive,'materialize',side_effect=project) as projection:
                first=hermes.sync_once(db)
                self.assertEqual(1,projection.call_count)
            self.assertIsNone(first['last_error']);self.assertEqual(3,first['cursor'])
            path=manager._archive_path('hermes:s',raw);original=path.read_bytes()
            late.write_bytes(b'image');con.execute('update messages set content="changed" where id=1');con.commit()
            second=hermes.sync_once(db);self.assertIsNone(second['last_error']);self.assertEqual(3,second['cursor'])
            self.assertTrue(path.read_bytes().startswith(original));rows=[json.loads(x) for x in path.read_text('utf-8').splitlines()]
            self.assertEqual(3,len(rows));self.assertNotIn('tool',[x['role'] for x in rows])
            key=next(iter(self.store._registry()['sessions']))
            current=archive.materialize_page(key,include_attachments=True)['messages']
            self.assertEqual(['changed',''],[x['original_text'] for x in current])
            self.assertEqual('available',current[1]['attachments'][0]['status'])
            con.execute('update messages set active=0');con.commit();hermes.sync_once(db)
            self.assertEqual([],archive.materialize_page(key)['messages'])
        con.close()

    def test_long_unchanged_projection_uses_one_identity_pass_and_keeps_conflict_checks(self):
        sid='hermes:long';sessions=self.root/'sessions.json';raw=self.root/'session-raw'
        manager.session_start(sid,'hermes','desktop',memory_mode='full',state_path=sessions,archive_root=raw)
        session=self.store.ensure_session(source_key='hermes',source_session_id=sid,source_instance='desktop',title='fixture')
        rows=[{'message_id':str(i),'role':'user','timestamp':'2026-09-29T00:00:00Z',
               'original_text':'body '+str(i),'content_sha256':hashlib.sha256(('body '+str(i)).encode()).hexdigest(),
               'message_order':i+1} for i in range(1000)]
        text=''.join(json.dumps(row)+'\n' for row in rows)
        canonical=self.context.raw_message_store/f"{session['session_key']}.jsonl"
        canonical.parent.mkdir(parents=True,exist_ok=True);canonical.write_text(text,'utf-8')
        source=manager._archive_path(sid,raw);source.parent.mkdir(parents=True,exist_ok=True);source.write_text(text,'utf-8')
        with patch.object(archive,'active_workspace',return_value=self.context),patch.object(archive,'SESSIONS_PATH',sessions),patch.object(archive,'ARCHIVE_ROOT',raw),patch.object(archive,'CANONICAL_ROOT',self.context.raw_archive),patch.object(type(self.context),'sessions_state',property(lambda _:sessions)),patch.object(archive,'ATTACHMENT_MANIFEST',self.root/'absent.jsonl'),patch('knowledge_bridge.original_archive_import.get_session',return_value=None),patch.object(ArchiveStore,'append_normalized_message',side_effect=AssertionError('unchanged rows must not append again')):
            archive.materialize(sid)
            self.assertEqual(text,canonical.read_text('utf-8'))
            rows[500]['original_text']='conflict';source.write_text(''.join(json.dumps(row)+'\n' for row in rows),'utf-8')
            with self.assertRaisesRegex(ValueError,'冲突'):archive.materialize(sid)
        self.assertEqual(text,canonical.read_text('utf-8'))

    def test_projection_failure_keeps_durable_intent_and_rotates_session(self):
        state={'pending_materializations':{'hermes:first':{'1':{'archive_message_id':'r'}},'hermes:next':{}}}
        with patch.object(archive,'materialize',side_effect=OSError('fixture write failure')):
            with self.assertRaises(OSError):hermes._flush_projection(state)
        self.assertEqual(['hermes:next','hermes:first'],list(state['pending_materializations']))
        self.assertEqual('r',state['pending_materializations']['hermes:first']['1']['archive_message_id'])

    def test_incremental_add_modify_rename_delete_reuses_vectors_and_retries(self):
        import numpy as np
        path=self.context.raw_archive/'fixture.md';path.write_text('# fixture\n\nalpha','utf-8')
        encode=lambda texts,**kwargs:np.array([[float(len(text)),1.0,0.0] for text in texts],dtype='float32')
        with patch('knowledge_bridge.core.embed',side_effect=encode) as embed,patch('knowledge_bridge.core.local_model_status',return_value={}):
            first=evidence.sync(context=self.context);self.assertEqual('incremental_synced',first['status'])
            self.assertTrue(evidence.freshness(self.context)['fresh'])
            calls=embed.call_count;evidence.sync(context=self.context);self.assertEqual(calls,embed.call_count)
            path.write_text('# fixture\n\nbeta','utf-8');queue.enqueue([self.context.relative(path)],self.context)
            with patch.object(evidence,'sync',return_value={'status':'index_busy'}):self.assertEqual('index_busy',queue.drain(self.context,force=True)['status'])
            self.assertTrue(queue._read(self.context.queues/'evidence-updates.json')['pending'])
            self.assertEqual('incremental_synced',queue.drain(self.context,force=True)['status'])
            renamed=path.with_name('renamed.md');path.rename(renamed);queue.enqueue(context=self.context)
            result=queue.drain(self.context,force=True);self.assertEqual(1,result['deleted'])
            self.assertTrue(evidence.freshness(self.context)['fresh'])
            renamed.unlink();queue.enqueue(context=self.context);queue.drain(self.context,force=True)
            self.assertTrue(evidence.freshness(self.context)['fresh']);self.assertIsNone(evidence.table(self.context))

    def test_index_mid_embedding_change_preserves_pointer(self):
        import numpy as np
        path=self.context.raw_archive/'fixture.md';path.write_text('# fixture\n\nalpha','utf-8')
        def encode(texts,**kwargs):
            path.write_text('# fixture\n\nchanged while embedding','utf-8')
            return np.ones((len(texts),3),dtype='float32')
        with patch('knowledge_bridge.core.embed',side_effect=encode):
            result=evidence.sync(context=self.context)
        self.assertEqual('source_changed',result['status']);self.assertFalse(evidence._paths(self.context)[2].exists())

    def test_canonical_folder_rename_reuses_vectors(self):
        import numpy as np
        session=self.store.ensure_session(source_key='hermes',source_session_id='h',title='before')
        self.store.append_normalized_message(session['session_key'],NormalizedMessage('1','user','stable body'))
        with patch('knowledge_bridge.core.embed',side_effect=lambda texts,**k:np.ones((len(texts),3),dtype='float32')),patch('knowledge_bridge.core.local_model_status',return_value={}):
            evidence.sync(context=self.context)
            self.store.ensure_session(source_key='hermes',source_session_id='h',title='after')
            result=evidence.sync(context=self.context)
        self.assertEqual(0,result['embedded_chunks']);self.assertGreater(result['reused_vectors'],0)
        self.assertEqual(1,result['deleted']);self.assertTrue(evidence.freshness(self.context)['fresh'])

    def test_queue_ack_preserves_concurrent_event(self):
        queue.enqueue(['first.md'],self.context)
        def sync(*args,**kwargs):
            queue.enqueue(['first.md','second.md'],self.context);return {'status':'incremental_synced'}
        with patch.object(evidence,'_descriptor',return_value={'version':1}),patch.object(evidence,'sync',side_effect=sync):
            queue.drain(self.context,force=True)
        self.assertEqual({'first.md','second.md'},set(queue._read(self.context.queues/'evidence-updates.json')['pending']))

    def test_watcher_registers_directory_move_before_archive_exclusion(self):
        from knowledge_bridge import watcher
        from watchdog.events import DirMovedEvent
        with patch.object(watcher,'VAULT',self.context.root),patch.object(watcher,'active_workspace',return_value=self.context):
            watcher.Handler().on_any_event(DirMovedEvent(str(self.context.raw_archive/'before'),str(self.context.raw_archive/'after')))
        self.assertIn('*',queue._read(self.context.queues/'evidence-updates.json')['pending'])


if __name__=='__main__':unittest.main()
