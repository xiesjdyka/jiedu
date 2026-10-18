"""Bounded same-configuration external conversation and original-image history.

Run with isolated control/vault/TEMP. All model traffic is MockTransport and all
data is synthetic. Existing request fixtures use TemporaryDirectory throughout.
"""
import base64
import json
import unittest
from unittest.mock import patch
from . import test_butler_request_context as fixture
from knowledge_bridge import local_butler as lb
from knowledge_bridge import butler_provider as bp


class ExternalHistoryTests(unittest.TestCase):
    setUp=fixture.ButlerRequestContextTests.setUp
    respond=fixture.ButlerRequestContextTests.respond
    service=fixture.ButlerRequestContextTests.service

    def turn(self,service,sid,text,*,images=(),thinking=False,tools=False,output_limit='auto'):
        ids=[service.attachments.save(sid,'synthetic.png',data)['id'] for data in images]
        jid=service.submit(sid,text,thinking=thinking,tools_enabled=tools,attachments=ids,output_limit=output_limit)['id']
        service._update(jid,state='running')
        with service.db() as db:job=dict(db.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone())
        service._execute_external(job,service.providers.snapshot())
        with service.db() as db:context=json.loads(db.execute('SELECT context FROM jobs WHERE id=?',(jid,)).fetchone()[0])
        return jid,context

    def change_context(self,service,jid,change):
        with service.db() as db:
            value=json.loads(db.execute('SELECT context FROM jobs WHERE id=?',(jid,)).fetchone()[0]);change(value)
            db.execute('UPDATE jobs SET context=? WHERE id=?',(json.dumps(value,ensure_ascii=False),jid))

    def test_three_turns_repeat_codeword_and_first_original_image(self):
        service=self.service();sid=service.create_session()['id'];data=fixture.png()
        first,_=self.turn(service,sid,'本轮代号是 FICTIONAL-KITE-17。',images=[data])
        second,middle=self.turn(service,sid,'重复我刚才的代号。')
        third,last=self.turn(service,sid,'再查看第一轮原图，描述可见颜色。')
        self.assertEqual([m['role'] for m in self.captured[2]['messages']],['system','user','assistant','user','assistant','user'])
        wire=json.dumps(self.captured[2],ensure_ascii=False);self.assertIn('FICTIONAL-KITE-17',wire)
        old_image=next(part for message in self.captured[2]['messages'] if isinstance(message['content'],list) for part in message['content'] if part['type']=='image_url')
        self.assertEqual(base64.b64decode(old_image['image_url']['url'].split(',',1)[1]),data)
        self.assertEqual(last['history']['included_job_ids'],[first,second]);self.assertEqual(last['budget']['history_messages'],4)
        self.assertEqual(last['outgoing']['history_image_count'],1);self.assertEqual(last['outgoing']['data_scopes'],[])
        self.assertTrue(middle['outgoing']['history_sent']);self.assertFalse(service.retriever.calls)
        self.assertNotIn('data:image',json.dumps(last))

    def test_other_revision_model_and_old_local_are_excluded(self):
        for mode in ('revision','model','old_local'):
            with self.subTest(mode=mode):
                service=self.service();sid=service.create_session()['id'];first,_=self.turn(service,sid,'OMIT-OLD-'+mode)
                if mode=='revision':service.providers.value['revision']='new-synthetic-revision'
                elif mode=='model':service.providers.value['external']['model']='synthetic-new-model'
                else:
                    with service.db() as db:
                        value=json.loads(db.execute('SELECT requested FROM jobs WHERE id=?',(first,)).fetchone()[0]);value['provider_snapshot']['selected']='local'
                        db.execute('UPDATE jobs SET requested=? WHERE id=?',(json.dumps(value),first))
                _,saved=self.turn(service,sid,'仅回答本轮。')
                self.assertNotIn('OMIT-OLD-'+mode,json.dumps(self.captured[-1]))
                self.assertEqual(saved['budget']['history_messages'],0)
                self.assertEqual(saved['budget']['history_omitted'][0]['reason'],'provider_changed')

    def test_private_source_scope_free_tool_and_unverified_history_pairs_are_omitted(self):
        changes={
            'private_source':lambda value:value.update(sources=[{'kind':'tool_source','source_path':'03_知识/synthetic-private.txt','content':'PRIVATE_DERIVED'}]),
            'scope_free_tool':lambda value:value.update(activity=[{'operation':'workspace_status','data_scopes':[],'result':{'initialized':True}}]),
            'snapshot':lambda value:value.update(snapshot={'synthetic':'PRIVATE_DERIVED'}),
            'native_search':lambda value:value.update(provider_search={'sources':[]}),
            'unverified_history':lambda value:(value['outgoing'].update(history_sent=True),value.pop('history',None)),
        }
        for mode,change in changes.items():
            with self.subTest(mode=mode):
                service=self.service();sid=service.create_session()['id'];first,_=self.turn(service,sid,'PRIVATE-PAIR-'+mode)
                self.change_context(service,first,change)
                _,saved=self.turn(service,sid,'仅回答本轮。')
                self.assertNotIn('PRIVATE-PAIR-'+mode,json.dumps(self.captured[-1]));self.assertEqual(saved['budget']['history_messages'],0)
                self.assertIn(saved['budget']['history_omitted'][0]['reason'],{'private_derived','unverified_history'})

    def test_only_latest_eight_completed_pairs_are_used(self):
        service=self.service();sid=service.create_session()['id'];ids=[]
        for index in range(9):ids.append(self.turn(service,sid,'HISTORY-CODE-'+str(index))[0])
        _,saved=self.turn(service,sid,'本轮结束。')
        self.assertEqual(saved['history']['included_job_ids'],ids[-8:]);self.assertEqual(saved['budget']['history_messages'],16)
        self.assertNotIn('HISTORY-CODE-0',json.dumps(self.captured[-1]));self.assertIn('HISTORY-CODE-1',json.dumps(self.captured[-1]))
        with service.db() as db:notice=db.execute('SELECT output_notice FROM jobs ORDER BY rowid DESC LIMIT 1').fetchone()[0]
        self.assertIn('保留范围',notice)

    def test_context_budget_drops_oldest_whole_pair_and_keeps_current(self):
        service=self.service(fixture.provider(tools=True),tools=True);sid=service.create_session()['id']
        oldest,_=self.turn(service,sid,'DROP-LONG-PAIR '+('x'*12000))
        recent,_=self.turn(service,sid,'KEEP-RECENT-PAIR')
        service.providers.value['external']['context_tokens']=11000
        with patch.object(lb,'output_notice',return_value='已达到本轮生成上限，可继续追问。'):
            _,saved=self.turn(service,sid,'KEEP-CURRENT-QUESTION',tools=True,output_limit=512)
        wire=json.dumps(self.captured[-1]);self.assertNotIn('DROP-LONG-PAIR',wire)
        self.assertIn('KEEP-RECENT-PAIR',wire);self.assertIn('KEEP-CURRENT-QUESTION',wire)
        self.assertEqual(saved['history']['included_job_ids'],[recent])
        self.assertIn({'job_id':oldest,'reason':'input_budget','images_omitted':False},saved['budget']['history_omitted'])
        with service.db() as db:notice=db.execute('SELECT output_notice FROM jobs ORDER BY rowid DESC LIMIT 1').fetchone()[0]
        self.assertIn('已达到本轮生成上限',notice);self.assertIn('上下文容量不足',notice)

    def test_old_image_tamper_omits_pair_and_tells_model(self):
        service=self.service();sid=service.create_session()['id'];first,_=self.turn(service,sid,'OMIT-TAMPERED-IMAGE',images=[fixture.png()])
        with service.db() as db:
            item=json.loads(db.execute("SELECT attachments FROM messages WHERE job=? AND role='user'",(first,)).fetchone()[0])[0]
        service.attachments.original(sid,item['id']).write_bytes(b'tampered')
        _,saved=self.turn(service,sid,'第一轮图片是什么？')
        self.assertNotIn('OMIT-TAMPERED-IMAGE',json.dumps(self.captured[-1]))
        self.assertIn('旧图原件缺失、变化或绑定失效',self.captured[-1]['messages'][0]['content'])
        self.assertEqual(saved['budget']['image_count'],0);self.assertEqual(saved['budget']['history_omitted'][0]['reason'],'image_invalid')
        with service.db() as db:notice=db.execute('SELECT output_notice FROM jobs ORDER BY rowid DESC LIMIT 1').fetchone()[0]
        self.assertIn('旧图未重发，可重新选择',notice)

    def test_current_images_win_over_history_count_and_binary_budget(self):
        for mode in ('count','bytes'):
            with self.subTest(mode=mode):
                service=self.service();sid=service.create_session()['id'];data=fixture.png()
                first,_=self.turn(service,sid,'OMIT-OLD-IMAGE',images=[data])
                count=4 if mode=='count' else 1
                with patch.object(lb,'EXTERNAL_IMAGE_BYTES',16*1024*1024 if mode=='count' else len(data)+1):
                    _,saved=self.turn(service,sid,'KEEP-NEW-IMAGES',images=[data]*count)
                self.assertEqual(saved['outgoing']['explicit_image_count'],count);self.assertEqual(saved['budget']['image_count'],count)
                self.assertEqual(saved['history']['included_job_ids'],[])
                self.assertIn({'job_id':first,'reason':'image_budget','images_omitted':True},saved['budget']['history_omitted'])

    def test_session_revocation_omits_old_pairs_without_scope_widening(self):
        service=self.service(fixture.provider(tools=True),tools=True);sid=service.create_session()['id']
        first,_=self.turn(service,sid,'REVOKED-OLD-PAIR')
        service.permissions.revoke_butler_session(sid)
        _,saved=self.turn(service,sid,'本轮新输入。')
        self.assertNotIn('REVOKED-OLD-PAIR',json.dumps(self.captured[-1]))
        self.assertIn({'job_id':first,'reason':'authorization_changed','images_omitted':False},saved['budget']['history_omitted'])
        self.assertEqual(saved['outgoing']['data_scopes'],[])

    def test_inflight_history_revocation_cancels_before_send(self):
        service=self.service(fixture.provider(tools=True),tools=True);sid=service.create_session()['id'];self.turn(service,sid,'FIRST-SAFE-PAIR')
        original=bp.CompatibleTransport.complete
        def revoke_then_send(transport,messages,**kwargs):
            service.permissions.revoke_butler_session(sid)
            return original(transport,messages,**kwargs)
        before=len(self.captured)
        with patch.object(bp.CompatibleTransport,'complete',revoke_then_send):
            with self.assertRaises((InterruptedError,bp.ProviderError)):self.turn(service,sid,'本轮未发送。')
        self.assertEqual(len(self.captured),before)

    def test_current_deepseek_tools_replay_final_answer_and_safe_reasoning(self):
        service=self.service(fixture.provider(tools=True),tools=True);sid=service.create_session()['id']
        first,_=self.turn(service,sid,'SAFE-REASONING-PAIR',thinking=True,tools=True)
        _,saved=self.turn(service,sid,'本轮仍可使用工具。',tools=True)
        history_answer=next(message for message in self.captured[-1]['messages'] if message['role']=='assistant')
        self.assertEqual(history_answer['reasoning_content'],'合成思考。');self.assertNotIn('tool_calls',history_answer)
        self.assertFalse(any(message['role']=='tool' for message in self.captured[-1]['messages']))
        self.assertIn(first,saved['history']['included_job_ids']);self.assertEqual(saved['budget']['history_messages'],2)

    def test_tool_schema_is_included_in_history_trim_budget(self):
        service=self.service(fixture.provider(tools=True),tools=True);sid=service.create_session()['id']
        text='SCHEMA-BUDGET-OLD '+('x'*5000)
        self.turn(service,sid,text,output_limit=512);self.turn(service,sid,'本轮允许工具。',tools=True,output_limit=512)
        full=self.captured[-1]
        schema_size=len(json.dumps(full['tools'],ensure_ascii=False).encode('utf-8'))
        capacity=bp.message_input_estimate(full['messages'])+512+schema_size//2
        service=self.service(fixture.provider(tools=True),tools=True);service.providers.value['external']['context_tokens']=capacity
        sid=service.create_session()['id'];first,_=self.turn(service,sid,text,output_limit=512)
        _,saved=self.turn(service,sid,'本轮允许工具。',tools=True,output_limit=512)
        self.assertNotIn('SCHEMA-BUDGET-OLD',json.dumps(self.captured[-1]))
        self.assertIn({'job_id':first,'reason':'input_budget','images_omitted':False},saved['budget']['history_omitted'])

    def test_image_direct_answer_recovery_uses_verified_parent_original(self):
        service=self.service();sid=service.create_session()['id'];data=fixture.png()
        item=service.attachments.save(sid,'synthetic.png',data)
        parent=service.submit(sid,'恢复这张图。',thinking=True,attachments=[item['id']])['id']
        with service.db() as db:db.execute("UPDATE jobs SET state='failed',reasoning='synthetic reasoning',finish_reason='length' WHERE id=?",(parent,))
        child=service.answer_directly(parent)['id'];service._update(child,state='running')
        with service.db() as db:job=dict(db.execute('SELECT * FROM jobs WHERE id=?',(child,)).fetchone())
        service._execute_external(job,service.providers.snapshot())
        part=next(part for part in self.captured[-1]['messages'][-1]['content'] if part['type']=='image_url')
        self.assertEqual(base64.b64decode(part['image_url']['url'].split(',',1)[1]),data)
        with service.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM messages WHERE job=? AND role='user'",(child,)).fetchone()[0],0)


if __name__=='__main__':unittest.main()
