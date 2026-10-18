"""Product auto budget differs from explicit Owner length; all traffic is mocked."""
import copy
import json
import unittest
import httpx
from . import test_butler_request_context as fixture
from unittest.mock import patch


class DefaultBudgetTests(unittest.TestCase):
    setUp=fixture.ButlerRequestContextTests.setUp
    service=fixture.ButlerRequestContextTests.service
    def respond(self,request):
        if not getattr(self,'limited_response',False):return fixture.ButlerRequestContextTests.respond(self,request)
        payload=json.loads(request.content);self.captured.append(payload)
        event={'choices':[{'delta':{'reasoning_content':'合成思考原文。','content':getattr(self,'answer_text','   ')},'finish_reason':'length'}]}
        return httpx.Response(200,headers={'content-type':'text/event-stream'},content=('data: '+json.dumps(event,ensure_ascii=False)+'\n\ndata: [DONE]\n\n').encode())
    def value(self,**changes):
        value=fixture.provider();value['external']['max_output_tokens']=20000;value['external'].update(changes);return value
    def turn(self,service,*,thinking=False,output_limit='auto',sid=None,text='本轮合成问题。'):
        sid=sid or service.create_session()['id']
        jid=service.submit(sid,text,thinking=thinking,output_limit=output_limit)['id'];service._update(jid,state='running')
        with service.db() as db:job=dict(db.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone())
        service._execute_external(job,service.providers.snapshot())
        with service.db() as db:finished=dict(db.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone())
        return sid,finished,service.conversation(sid)
    def test_new_external_auto_uses_4096_without_changing_service_cap(self):
        value=self.value();before=copy.deepcopy(value);service=self.service(value)
        _,job,_=self.turn(service);requested=json.loads(job['requested'])
        self.assertEqual(self.captured[-1]['max_tokens'],4096)
        self.assertEqual(requested['output_limit'],'auto');self.assertEqual(requested['default_output_target'],4096)
        self.assertEqual(requested['output_limit_source'],'product_default');self.assertEqual(service.providers.value,before)
    def test_thinking_auto_gets_8192_and_required_mode_is_normalized_first(self):
        for mode in ('switchable','required'):
            with self.subTest(mode=mode):
                service=self.service(self.value(thinking_mode=mode))
                _,job,_=self.turn(service,thinking=mode!='required');requested=json.loads(job['requested'])
                self.assertTrue(requested['thinking']);self.assertEqual(requested['default_output_target'],8192)
                self.assertEqual(self.captured[-1]['max_tokens'],8192);self.assertEqual(self.captured[-1]['thinking'],{'type':'enabled'})
    def test_explicit_512_and_max_are_preserved_as_owner_choices(self):
        for limit,expected in ((512,512),('max',20000)):
            with self.subTest(limit=limit):
                service=self.service(self.value());_,job,_=self.turn(service,thinking=True,output_limit=limit)
                requested=json.loads(job['requested']);self.assertEqual(requested['output_limit'],limit)
                self.assertEqual(requested['output_limit_source'],'owner_explicit');self.assertEqual(self.captured[-1]['max_tokens'],expected)
    def test_auto_is_clamped_by_owner_service_cap_and_actual_context(self):
        for changes,reason in (({'max_output_tokens':1024},'service_limit'),({'context_tokens':10000},'context_limit')):
            with self.subTest(reason=reason):
                value=self.value(**changes);before=copy.deepcopy(value);service=self.service(value)
                _,job,_=self.turn(service,thinking=True);effective=json.loads(job['effective'])
                self.assertGreater(effective['max_output_tokens'],0);self.assertLess(effective['max_output_tokens'],8192)
                self.assertIn(reason,effective['output_limit_reasons']);self.assertEqual(service.providers.value,before)
                self.assertLessEqual(effective['max_output_tokens'],effective['available_output_tokens'])
    def test_reasoning_only_length_keeps_no_answer_and_real_budget_notice(self):
        self.limited_response=True;service=self.service(self.value(max_output_tokens=1024))
        _,job,conversation=self.turn(service,thinking=True)
        answer=conversation['messages'][-1]
        self.assertEqual(answer['content'],'   ');self.assertEqual(answer['reasoning'],'合成思考原文。')
        self.assertEqual(job['finish_reason'],'length');self.assertIn('尚未形成回答',job['stage'])
        self.assertEqual(json.loads(job['effective'])['answer_status'],'no_answer')
        self.assertIn('尚未形成回答',job['output_notice']);self.assertIn('模型设置中的输出上限',job['output_notice'])
        self.assertNotEqual(job['stage'],'已完成')
    def test_partial_length_is_incomplete_and_not_labeled_finished_answer(self):
        self.limited_response=True;self.answer_text='合成回答的未完成部分';service=self.service(self.value())
        _,job,_=self.turn(service,thinking=True)
        self.assertEqual(json.loads(job['effective'])['answer_status'],'incomplete')
        self.assertEqual(job['stage'],'回答达到额度上限');self.assertIn('达到本轮生成上限',job['output_notice'])
    def test_auto_reserves_thinking_budget_by_omitting_old_pair_visibly(self):
        service=self.service(self.value(context_tokens=25000));sid=service.create_session()['id']
        self.turn(service,sid=sid,text='OLD-LONG-INPUT '+('x'*16000),output_limit=512)
        _,job,_=self.turn(service,sid=sid,thinking=True)
        self.assertEqual(self.captured[-1]['max_tokens'],8192)
        self.assertFalse('OLD-LONG-INPUT' in json.dumps(self.captured[-1]));self.assertIn('较早问答',job['output_notice'])
        self.assertEqual(json.loads(job['context'])['budget']['history_messages'],0)
    def test_auto_choice_and_target_remain_distinct_during_direct_recovery(self):
        service=self.service(self.value());sid=service.create_session()['id']
        parent=service.submit(sid,'合成恢复问题。',thinking=True)['id']
        with service.db() as db:db.execute("UPDATE jobs SET state='failed',finish_reason='length',reasoning='synthetic' WHERE id=?",(parent,))
        child=service.answer_directly(parent)['id']
        with service.db() as db:requested=json.loads(db.execute('SELECT requested FROM jobs WHERE id=?',(child,)).fetchone()[0])
        self.assertEqual(requested['output_limit'],'auto');self.assertEqual(requested['output_limit_source'],'product_default')
        self.assertFalse(requested['thinking']);self.assertEqual(requested['default_output_target'],4096)
    def test_service_close_releases_attachment_store_and_is_repeatable(self):
        service=self.service(self.value());actual=service.attachments.close
        with patch.object(service.attachments,'close',wraps=actual) as close:
            service.close();close.assert_called_once()
        service.close()


if __name__=='__main__':unittest.main()
