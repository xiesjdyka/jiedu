"""Synthetic channel boundaries; no provider calls or real conversation stores.

The runner must select isolated workspace/control/TEMP roots before importing
knowledge_bridge, as for the other workspace tests in this suite.
"""
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from knowledge_bridge.codex_source_adapter import CodexSourceAdapter, PARSER_VERSION, VISIBILITY_POLICY_VERSION
from knowledge_bridge.conversation_visibility import knowledge_message
from knowledge_bridge import hermes_session_adapter as hermes, session_manager
from knowledge_bridge.archive_store import ArchiveStore
from knowledge_bridge.continuous_organization import ContinuousOrganizer
from knowledge_bridge.organizer import evidence_checked
from knowledge_bridge.workspace import initialize_workspace
from knowledge_bridge import manual_extraction as manual
from knowledge_bridge.inbox_capture import capture


VISIBLE='可见正文，讨论 <think> 标签。\n\n```python\nprint("assistant analysis is plain code")\n```'


def codex_event(mid='visible', **fields):
    return {'type':'response_item','payload':{'type':'message','id':mid,'role':'assistant',
        'phase':'final_answer','content':[{'type':'output_text','text':VISIBLE}],**fields}}


class CodexChannelTests(unittest.TestCase):
    def setUp(self):
        self.adapter=CodexSourceAdapter.__new__(CodexSourceAdapter)

    def normalize(self, **fields):
        return self.adapter._normalize_event(codex_event(**fields),'synthetic-session')

    def test_public_channels_preserve_exact_prose_code_and_think_discussion(self):
        for phase in ('commentary','final','final_answer'):
            for channel in (None,'commentary','final','final_answer'):
                for recipient in (None,'all','user'):
                    with self.subTest(phase=phase,channel=channel,recipient=recipient):
                        result=self.normalize(phase=phase,channel=channel,recipient=recipient)
                        self.assertEqual('message',result['status'])
                        self.assertEqual(VISIBLE,result['message'].content)
                        self.assertTrue(knowledge_message({'role':'assistant','provenance':result['message'].provenance}))

    def test_explicit_internal_channels_override_visible_phase(self):
        for channel in ('analysis','reasoning'):
            with self.subTest(channel=channel):
                result=self.normalize(channel=channel)
                self.assertEqual(('ignored','internal_message_channel'),(result['status'],result['category']))
                self.assertNotIn('message',result)

    def test_tool_and_agent_routes_are_internal_unknown_routes_need_review(self):
        for recipient in ('functions.exec','tools.exec','mcp__fixture__tool','/root/helper'):
            with self.subTest(recipient=recipient):
                result=self.normalize(recipient=recipient)
                self.assertEqual(('ignored','internal_message_recipient'),(result['status'],result['category']))
        for fields,reason in (({'recipient':'future_display_recipient'},'unknown_message_recipient'),
                              ({'channel':'future_display_channel'},'unknown_message_channel')):
            result=self.normalize(**fields)
            self.assertEqual(('needs_review',reason),(result['status'],result['reason']))

    def test_legacy_envelope_receives_same_explicit_structure_checks(self):
        fields={'phase':None,'internal_chat_message_metadata_passthrough':{'turn_id':'synthetic-turn'}}
        self.assertEqual('message',self.normalize(**fields)['status'])
        for extra in ({'channel':'analysis'},{'channel':'reasoning'},{'recipient':'functions.exec'},{'recipient':'/root/helper'}):
            with self.subTest(extra=extra):self.assertEqual('ignored',self.normalize(**fields,**extra)['status'])
        self.assertEqual('needs_review',self.normalize(**fields,channel='future_display_channel')['status'])

    def test_calls_outputs_and_uncertain_phase_do_not_become_body_text(self):
        for kind in ('reasoning','function_call','function_call_output','custom_tool_call','custom_tool_call_output'):
            result=self.adapter._normalize_event({'type':'response_item','payload':{'type':kind,'content':'synthetic runtime output'}},'synthetic-session')
            self.assertEqual('ignored',result['status'])
        self.assertEqual('needs_review',self.normalize(phase='analysis')['status'])

    def test_question_card_does_not_override_explicit_internal_structure(self):
        def card(**fields):
            return {'type':'event_msg','payload':{'type':'item_completed','thread_id':'synthetic-session','turn_id':'synthetic-turn',
                'item':{'type':'AgentMessage','id':'call_synthetic','phase':'final_answer','delivery':'async',
                    'questions':[{'title':'Synthetic question','options':['one','two']}],
                    'content':[{'type':'Text','text':VISIBLE}],**fields}}}
        self.assertEqual(VISIBLE,self.adapter._normalize_event(card(),'synthetic-session')['message'].content)
        for fields in ({'channel':'analysis'},{'recipient':'functions.exec'}):
            with self.subTest(fields=fields):self.assertEqual('ignored',self.adapter._normalize_event(card(**fields),'synthetic-session')['status'])
        self.assertEqual('needs_review',self.adapter._normalize_event(card(channel='future_display_channel'),'synthetic-session')['status'])

    def test_filter_only_change_keeps_source_ids_byte_offsets_and_existing_cursor(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);home=base/'codex';sessions=home/'sessions';sessions.mkdir(parents=True)
            context=initialize_workspace(base/'workspace');machine=base/'control'
            line=lambda value:(json.dumps(value,ensure_ascii=False,separators=(',',':'))+'\n').encode()
            first=codex_event('source-id-before')
            path=sessions/'rollout-synthetic.jsonl'
            path.write_bytes(line({'type':'session_meta','payload':{'id':'synthetic','cli_version':'fixture'}})+line(first))
            adapter=CodexSourceAdapter(context,codex_home=home,machine_data=machine)
            adapter.configure(enabled=True,authorize=['synthetic'],owner_confirmed=True)
            self.assertEqual(1,adapter.sync(manual=True)['saved_messages'])
            before=adapter.status()['current_cursors']['synthetic'];initial_offset=before['confirmed_offset']
            raw=next(context.raw_message_store.glob('*.jsonl'));raw_before=raw.read_bytes()
            internal=codex_event('must-not-renumber',channel='analysis')
            tail=codex_event('source-id-after');tail['payload']['id']=None
            tail_id=hashlib.sha256(json.dumps(tail['payload'],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            with path.open('ab') as handle:handle.write(line(internal)+line(tail))
            self.assertEqual(1,adapter.sync(manual=True)['saved_messages'])
            rows=[json.loads(value) for value in raw.read_text('utf-8').splitlines()]
            self.assertEqual(['source-id-before',tail_id],[row['message_id'] for row in rows])
            self.assertTrue(raw.read_bytes().startswith(raw_before))
            self.assertEqual(initial_offset+len(line(internal)),rows[-1]['source_position']['byte_offset'])
            after=adapter.status()['current_cursors']['synthetic']
            self.assertEqual(path.stat().st_size,after['confirmed_offset'])
            self.assertEqual(('codex-jsonl-v2.2',2),(PARSER_VERSION,VISIBILITY_POLICY_VERSION))
            self.assertEqual((before['parser_version'],before['visibility_policy_version']),(after['parser_version'],after['visibility_policy_version']))
            restarted=CodexSourceAdapter(context,codex_home=home,machine_data=machine)
            self.assertEqual(0,restarted.sync(manual=True)['saved_messages'])
            self.assertEqual(2,len(raw.read_text('utf-8').splitlines()))


class HermesChannelTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name);self.db=self.root/'synthetic.db'
        self.sessions=self.root/'sessions.json';self.archives=self.root/'raw'
        db=sqlite3.connect(self.db)
        db.executescript('create table sessions(id text primary key,source text,started_at real,title text);'
            'create table messages(id integer primary key,session_id text,role text,content text,timestamp real,token_count integer,platform_message_id text,display_metadata text,active integer,display_kind text,channel text,recipient text,tool_calls text,reasoning_content text);'
            'insert into sessions values("fixture","desktop",1,"Synthetic");')
        fixtures=[('assistant',VISIBLE,None,'commentary',None),
                  ('assistant','visible future kind','future_visible_panel',None,None),
                  ('user','steer user text','steer',None,None),
                  ('assistant','synthetic hidden','hidden',None,None),
                  ('assistant','synthetic notice','model_switch',None,None),
                  ('assistant','synthetic analysis',None,'analysis',None),
                  ('assistant','synthetic routed',None,None,'functions.exec'),
                  ('tool','synthetic raw tool',None,None,None)]
        for i,(role,content,kind,channel,recipient) in enumerate(fixtures,1):
            db.execute('insert into messages values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (i,'fixture',role,content,10+i,None,f'm{i}','{}',1,kind,channel,recipient,'[{"id":"synthetic-call"}]','synthetic reasoning column'))
        db.commit();db.close()
        patches=[patch.object(hermes,'STATE',self.root/'adapter.json'),patch.object(hermes,'list_sessions',return_value=[]),
            patch.object(hermes,'session_start',side_effect=lambda *a,**k:session_manager.session_start(*a,**k,state_path=self.sessions,archive_root=self.archives)),
            patch.object(hermes,'session_append',side_effect=lambda *a,**k:session_manager.session_append(*a,**k,state_path=self.sessions,archive_root=self.archives)),
            patch.object(hermes,'update_session_metadata',side_effect=lambda sid,**k:session_manager.update_session_metadata(sid,state_path=self.sessions,**k)),
            patch.object(hermes,'_flush_projection',side_effect=lambda state:state.get('pending_materializations',{}).clear()),
            patch('knowledge_bridge.live_acceptance.record_message'),
            patch('knowledge_bridge.memory_control.interface_state',return_value={'interfaces':{'save_raw_conversation':{'enabled':True}}})]
        for item in patches:item.start();self.addCleanup(item.stop)
        hermes.initialize(self.db)

    def test_optional_columns_exclude_internal_but_keep_tool_call_assistant_prose(self):
        hermes.history_control('start',self.db,owner_confirmed=True)
        for _ in range(5):
            hermes.sync_once(self.db)
            if hermes._load()['history']['status']=='complete':break
        path=session_manager._archive_path('hermes:fixture',self.archives)
        rows=[json.loads(line) for line in path.read_text('utf-8').splitlines()]
        by_id={row['message_id']:row for row in rows}
        self.assertEqual({'m1','m2','m3','m5'},set(by_id))
        self.assertEqual(VISIBLE,by_id['m1']['original_text'])
        self.assertEqual('commentary',by_id['m1']['provenance']['source_channel'])
        self.assertTrue(knowledge_message(by_id['m1']))
        self.assertEqual('system',by_id['m5']['role']);self.assertFalse(knowledge_message(by_id['m5']))
        self.assertNotIn('synthetic reasoning column',path.read_text('utf-8'))
        self.assertNotIn('synthetic-call',path.read_text('utf-8'))
        self.assertEqual('internal_message_channel',hermes._load()['observations']['6']['display_decision'])

    def test_unknown_display_kind_and_code_are_not_keyword_filtered(self):
        for kind in (None,'steer','future_visible_panel'):
            row={'role':'assistant','content':VISIBLE,'display_kind':kind,'display_metadata':'{}','tool_calls':'[{}]'}
            self.assertEqual(('assistant','authored',kind),hermes._display_semantics(row))
        self.assertTrue(knowledge_message({'role':'assistant','content':VISIBLE,'provenance':{'source_display_kind':'future_visible_panel'}}))


class OrganizationChannelTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.context=initialize_workspace(Path(self.temporary.name)/'workspace')
        self.service=ContinuousOrganizer(self.context)
        self.target=self.context.root/'02_项目/合成/当前状态.md';self.target.parent.mkdir(parents=True,exist_ok=True)
        self.target.write_text('# 合成\n\n## 当前进度\n待确认。\n','utf-8')
        self.service.configure({'enabled':True,'automatic':False,'client_id':'fixture','source_roots':['90_原始档案/连续整理'],
            'targets':[{'path':'02_项目/合成/当前状态.md','sections':['当前进度']}]})
        self.path=self.context.raw_archive/'连续整理/合成/03_元数据/messages.jsonl';self.path.parent.mkdir(parents=True)
        self.relative=self.context.relative(self.path)

    @staticmethod
    def row(mid,role='assistant',content=VISIBLE,provenance=None):
        return {'session_key':'synthetic','message_id':mid,'role':role,'original_text':content,
            'content_sha256':hashlib.sha256(content.encode()).hexdigest(),'provenance':provenance or {}}

    def write(self,rows):
        self.path.write_bytes(b''.join((json.dumps(row,ensure_ascii=False,separators=(',',':'))+'\n').encode() for row in rows))

    def evidence(self,index):
        lines=self.path.read_bytes().splitlines(keepends=True);raw=lines[index];row=json.loads(raw);start=sum(map(len,lines[:index]))
        return {'source_path':self.relative,'version':row['content_sha256'],'byte_start':start,'byte_end':start+len(raw),
            'quote':raw.decode(),'archive_line_end':start+len(raw),'archive_message_id':row['message_id'],'archive_session_id':'synthetic'}

    def test_archive_intake_and_model_batch_keep_provenance_filter_all_nonchat(self):
        self.write([self.row('user','user','synthetic user'),self.row('code'),
            self.row('not-eligible',content='excluded flag',provenance={'knowledge_eligible':False}),
            self.row('notice','system','excluded notice',{'display_semantics':'system_notice','knowledge_eligible':False}),
            self.row('tool','tool','excluded tool'),self.row('analysis',content='excluded channel',provenance={'source_channel':'analysis'}),
            self.row('reasoning',content='excluded phase',provenance={'assistant_phase':'reasoning'}),
            self.row('unknown-kind',content='visible future kind',provenance={'source_display_kind':'future_visible_panel'})])
        before=self.path.read_bytes()
        result=self.service.consume_project_archive('合成','fixture',self.relative,'synthetic')
        self.assertEqual(3,len(result['changed']))
        ledger=self.service.load_project('合成')['messages']
        self.assertEqual({'user','code','unknown-kind'},{row['message_id'] for row in ledger.values()})
        self.assertEqual('future_visible_panel',next(row for row in ledger.values() if row['message_id']=='unknown-kind')['provenance']['source_display_kind'])
        batch=self.service.project_batch('合成','fixture',self.relative,'synthetic','当前进度')
        self.assertEqual({'user','code','unknown-kind'},{row['message_id'] for row in batch['messages']})
        self.assertEqual(VISIBLE,next(row for row in batch['messages'] if row['message_id']=='code')['content'])
        self.assertEqual(before,self.path.read_bytes())

    def test_delta_rejects_nonchat_before_any_evidence_read(self):
        rows=[{'role':'tool'},{'role':'system'},{'role':'assistant','provenance':{'knowledge_eligible':False}},
              {'role':'assistant','provenance':{'source_recipient':'functions.exec'}}]
        with patch('knowledge_bridge.continuous_organization.evidence_checked',side_effect=AssertionError('must not read excluded rows')):
            self.assertEqual([],self.service.consume_project_delta('合成','synthetic','v1',rows,'fixture')['changed'])

    def test_archived_evidence_cannot_launder_internal_role_or_flag(self):
        self.write([self.row('public','user'),self.row('tool','tool'),self.row('flag',provenance={'knowledge_eligible':False}),
                    self.row('channel',provenance={'source_channel':'reasoning'})])
        evidence_checked(self.context,[self.evidence(0)])
        for index in (1,2,3):
            with self.subTest(index=index),self.assertRaisesRegex(ValueError,'不是可提炼'):
                evidence_checked(self.context,[self.evidence(index)])
        with self.assertRaisesRegex(ValueError,'不能混用文件快照'):
            evidence_checked(self.context,[self.evidence(1)|{'snapshot_id':'synthetic','snapshot_job_id':'synthetic'}])

    def test_local_recheck_and_delivery_reject_same_body_role_change(self):
        self.write([self.row('public','user'),self.row('changed','user')])
        self.service.consume_project_archive('合成','fixture',self.relative,'synthetic')
        raw=self.path.read_bytes();self.path.write_bytes(raw.replace(b'"message_id":"changed","role":"user"',b'"message_id":"changed","role":"tool"'))
        self.assertEqual(len(raw),self.path.stat().st_size)
        with self.assertRaisesRegex(ValueError,'不是可提炼'):
            self.service.recheck_project_message('合成','fixture','synthetic','changed')
        # Simulate an edit after the earlier evidence verification. The delivery
        # read must independently reject the role even with unchanged body hash.
        with patch.object(self.service,'consume_project_archive',return_value={'source_read_bytes':0}),patch('knowledge_bridge.continuous_organization.evidence_checked',side_effect=lambda context,rows:rows),self.assertRaisesRegex(ValueError,'聊天提炼资格'):
            self.service.project_batch('合成','fixture',self.relative,'synthetic','当前进度')

    def test_excluded_only_pages_are_bounded_and_cursor_reaches_public_tail(self):
        self.write([self.row(f'tool-{i}','tool','synthetic output') for i in range(501)]+[self.row('public','user')])
        first=self.service.consume_project_archive('合成','fixture',self.relative,'synthetic')
        self.assertEqual([],first['changed']);self.assertIsNotNone(first['next_offset'])
        self.assertLess(first['confirmed_offset'],self.path.stat().st_size)
        second=self.service.consume_project_archive('合成','fixture',self.relative,'synthetic')
        self.assertEqual(1,len(second['changed']));self.assertIsNone(second['next_offset'])

    def test_raw_archive_preserves_tool_evidence_while_organization_excludes_it(self):
        store=ArchiveStore(self.context,recover=False)
        store.sources.register('fixture_archive','连续整理','fixture','stdio')
        session=store.ensure_session(source_key='fixture_archive',source_session_id='synthetic',title='原始证据')
        for mid,role,body,provenance in [('user','user','synthetic user',{}),('code','assistant',VISIBLE,{}),
            ('tool','tool','synthetic tool evidence',{}),('notice','system','synthetic notice',{'knowledge_eligible':False})]:
            store.append_message(session['session_key'],message_id=mid,role=role,content=body,provenance=provenance,materialize=False)
        item=store._registry()['sessions'][session['session_key']]
        raw=self.context.raw_message_store/(session['session_key']+'.jsonl');store._materialize(item,raw)
        folder=self.context.root/item['relative_path'];messages=folder/'03_元数据/messages.jsonl';conversation=folder/'01_对话/conversation.md'
        before={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in (raw,messages,conversation)}
        result=self.service.consume_project_archive('合成','fixture',self.context.relative(messages),'synthetic')
        self.assertEqual(2,len(result['changed']))
        self.assertIn('synthetic tool evidence',conversation.read_text('utf-8'))
        self.assertIn(VISIBLE,conversation.read_text('utf-8'))
        self.assertEqual(before,{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in (raw,messages,conversation)})


class ManualInboxChannelTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)

    def raw_rows(self,root,rows):
        path=session_manager._archive_path('synthetic',root);path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(''.join(json.dumps(row,ensure_ascii=False)+'\n' for row in rows),'utf-8')
        return path

    def test_default_projection_failure_is_visible_retryable_and_never_reads_raw(self):
        canonical=self.root/'canonical'
        raw=self.raw_rows(canonical,[OrganizationChannelTests.row('stale',content='synthetic old eligible version')])
        before=raw.read_bytes();packages=self.root/'packages'
        for error in (OSError('synthetic projection fault'),ValueError('synthetic invalid projection')):
            with self.subTest(error=type(error).__name__),patch.object(manual,'ARCHIVE_ROOT',canonical),patch('knowledge_bridge.session_archive.materialize',side_effect=error),patch.object(manual,'_archive_path',side_effect=AssertionError('raw fallback must not occur')),patch.object(manual,'_sessions',return_value={'synthetic':{'client_id':'chatgpt'}}):
                with self.assertRaisesRegex(ValueError,'可见性投影不可用.*重试'):
                    manual._session_rows('synthetic',{},canonical)
                with self.assertRaisesRegex(ValueError,'可见性投影不可用.*重试'):
                    manual.export_package(state_path=self.root/'manual.json',state_sessions=self.root/'sessions.json',archive_root=canonical,packages_dir=packages,session_ids=['synthetic'])
            self.assertEqual(before,raw.read_bytes());self.assertFalse(packages.exists());self.assertFalse((self.root/'manual.json').exists())
        fresh=OrganizationChannelTests.row('fresh')
        with patch.object(manual,'ARCHIVE_ROOT',canonical),patch('knowledge_bridge.session_archive.materialize',return_value={'messages':[fresh]}):
            self.assertEqual([fresh],manual._session_rows('synthetic',{},canonical))
        self.assertEqual(before,raw.read_bytes())

    def test_explicit_custom_raw_root_keeps_compatibility_and_structural_filter(self):
        canonical=self.root/'canonical';custom=self.root/'explicit-raw'
        rows=[OrganizationChannelTests.row('public'),OrganizationChannelTests.row('tool','tool'),
              OrganizationChannelTests.row('channel',provenance={'source_channel':'analysis'}),
              OrganizationChannelTests.row('route',provenance={'source_recipient':'functions.exec'}),
              OrganizationChannelTests.row('flag',provenance={'knowledge_eligible':False}),
              OrganizationChannelTests.row('old'),OrganizationChannelTests.row('new',provenance={'supersedes_message_id':'old'})]
        raw=self.raw_rows(custom,rows);before=raw.read_bytes()
        with patch.object(manual,'ARCHIVE_ROOT',canonical),patch('knowledge_bridge.session_archive.materialize',side_effect=AssertionError('explicit raw root does not use canonical materialize')):
            selected=manual._session_rows('synthetic',{},custom)
        self.assertEqual(['public','new'],[row['message_id'] for row in selected])
        self.assertEqual(VISIBLE,selected[0]['original_text']);self.assertEqual(before,raw.read_bytes())

    def test_inbox_capture_reuses_structural_qualification_preserving_raw_evidence(self):
        context=initialize_workspace(self.root/'workspace');store=ArchiveStore(context,recover=False)
        store.sources.register('fixture_inbox','合成收件箱','fixture','stdio')
        session=store.ensure_session(source_key='fixture_inbox',source_session_id='synthetic',title='合成消息')
        rows=[OrganizationChannelTests.row('public'),OrganizationChannelTests.row('tool','tool'),
              OrganizationChannelTests.row('channel',provenance={'source_channel':'reasoning'}),
              OrganizationChannelTests.row('route',provenance={'source_recipient':'functions.exec'}),
              OrganizationChannelTests.row('flag',provenance={'knowledge_eligible':False}),
              OrganizationChannelTests.row('notice','system',provenance={'knowledge_eligible':False})]
        for row in rows:
            store.append_message(session['session_key'],message_id=row['message_id'],role=row['role'],content=row['original_text'],provenance=row['provenance'],materialize=False)
        store.materialize_session(session['session_key'])
        raw=context.raw_message_store/(session['session_key']+'.jsonl');before=raw.read_bytes()
        for row in rows[1:]:
            with self.subTest(mid=row['message_id']),self.assertRaisesRegex(ValueError,'系统通知'):
                capture(context,session['session_key'],row['message_id'])
        self.assertFalse((context.state/'inbox-captures.json').exists())
        first=capture(context,session['session_key'],'public');second=capture(context,session['session_key'],'public')
        self.assertEqual(first['capture_id'],second['capture_id']);self.assertTrue(second['duplicate'])
        self.assertNotIn(VISIBLE,(context.root/first['path']).read_text('utf-8'))
        self.assertEqual(before,raw.read_bytes())


if __name__=='__main__':unittest.main()
