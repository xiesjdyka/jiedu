"""No user length quotas; synthetic requests captured before any model service."""
import copy
import json
import unittest
import httpx
from . import test_butler_request_context as fixture
from knowledge_bridge.butler_generation import effective_settings, requested_settings, settings_for
from unittest.mock import patch


class DefaultBudgetTests(unittest.TestCase):
    setUp=fixture.ButlerRequestContextTests.setUp
    service=fixture.ButlerRequestContextTests.service

    def respond(self,request):
        if not getattr(self,'limited_response',False) and not getattr(self,'long_response',False):
            return fixture.ButlerRequestContextTests.respond(self,request)
        payload=json.loads(request.content);self.captured.append(payload)
        answer=('合成的完整长回答。'*6000) if getattr(self,'long_response',False) else getattr(self,'answer_text','   ')
        event={'choices':[{'delta':{'reasoning_content':'合成思考原文。','content':answer},'finish_reason':'stop' if getattr(self,'long_response',False) else 'length'}]}
        return httpx.Response(200,headers={'content-type':'text/event-stream'},content=('data: '+json.dumps(event,ensure_ascii=False)+'\n\ndata: [DONE]\n\n').encode())

    def value(self,**changes):
        value=fixture.provider();value['external'].update(context_tokens=1048576,max_output_tokens=512)
        value['external'].update(changes);return value

    def turn(self,service,*,thinking=False,output_limit='auto',sid=None,text='本轮合成问题。',legacy=False):
        sid=sid or service.create_session()['id']
        jid=service.submit(sid,text,thinking=thinking,output_limit=output_limit)['id'];service._update(jid,state='running')
        with service.db() as db:
            if legacy:
                requested=json.loads(db.execute('SELECT requested FROM jobs WHERE id=?',(jid,)).fetchone()[0])
                requested.update(output_limit=output_limit,default_output_target=8192 if thinking else 4096,output_limit_source='owner_explicit')
                db.execute('UPDATE jobs SET requested=? WHERE id=?',(json.dumps(requested),jid))
            job=dict(db.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone())
        service._execute_external(job,service.providers.snapshot())
        with service.db() as db:finished=dict(db.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone())
        return sid,finished,service.conversation(sid)

    def test_deepseek_auto_and_old_short_choices_use_actual_capability_in_both_modes(self):
        for thinking in (False,True):
            for limit in ('auto',128,512,'max'):
                with self.subTest(thinking=thinking,limit=limit):
                    value=self.value();before=copy.deepcopy(value);service=self.service(value)
                    _,job,_=self.turn(service,thinking=thinking,output_limit=limit,legacy=True)
                    payload=self.captured[-1];effective=json.loads(job['effective'])
                    self.assertEqual(payload['max_tokens'],393216)
                    self.assertEqual(payload['thinking'],{'type':'enabled' if thinking else 'disabled'})
                    self.assertEqual(effective['output_limit_source'],'model_capacity')
                    self.assertEqual(service.providers.value,before)

    def test_required_thinking_is_normalized_before_budget(self):
        service=self.service(self.value(thinking_mode='required'))
        _,job,_=self.turn(service)
        self.assertTrue(json.loads(job['requested'])['thinking'])
        self.assertEqual(self.captured[-1]['max_tokens'],393216)
        self.assertEqual(self.captured[-1]['thinking'],{'type':'enabled'})

    def test_deepseek_remaining_context_clamps_both_thinking_modes(self):
        for thinking in (False,True):
            with self.subTest(thinking=thinking):
                service=self.service(self.value());_,job,_=self.turn(service,thinking=thinking,text='x'*750000)
                effective=json.loads(job['effective'])
                self.assertEqual(self.captured[-1]['max_tokens'],1048576-effective['input_tokens'])
                self.assertGreater(self.captured[-1]['max_tokens'],8192)
                self.assertLess(self.captured[-1]['max_tokens'],393216)
                self.assertIn('context_limit',effective['output_limit_reasons'])

    def test_known_generic_capability_is_separate_from_context(self):
        service=self.service(self.value(model='synthetic-known',dialect='generic',thinking=False,
            max_output_tokens=6000,output_capacity_source='service_metadata'))
        _,job,_=self.turn(service,output_limit=128)
        self.assertEqual(self.captured[-1]['max_tokens'],6000)
        self.assertEqual(json.loads(job['effective'])['output_target'],6000)

    def test_unknown_generic_ignores_old_config_quota_and_defers_to_service(self):
        for old_cap in (1,512,8192,None):
            with self.subTest(old_cap=old_cap):
                value=self.value(model='synthetic-unknown',dialect='generic',thinking=False,max_output_tokens=old_cap)
                service=self.service(value);_,job,_=self.turn(service,output_limit=128,legacy=True)
                self.assertNotIn('max_tokens',self.captured[-1])
                effective=json.loads(job['effective'])
                self.assertIsNone(effective['max_output_tokens'])
                self.assertEqual(effective['output_limit_source'],'service_default')
                self.assertGreater(effective['available_output_tokens'],900000)

    def test_unknown_length_notice_does_not_claim_maximum_capability(self):
        self.limited_response=True
        service=self.service(self.value(model='unknown',dialect='generic',thinking=False))
        _,job,_=self.turn(service)
        self.assertIn('输出长度由服务决定',job['output_notice'])
        self.assertNotIn('提高',job['output_notice'])

    def test_reasoning_only_length_preserves_recovery_and_actual_notice(self):
        self.limited_response=True;service=self.service(self.value())
        _,job,conversation=self.turn(service,thinking=True)
        answer=conversation['messages'][-1]
        self.assertEqual(answer['content'],'   ');self.assertEqual(answer['reasoning'],'合成思考原文。')
        self.assertEqual(json.loads(job['effective'])['answer_status'],'no_answer')
        self.assertIn('尚未形成回答',job['output_notice']);self.assertIn('模型的单次输出能力',job['output_notice'])
        self.assertNotIn('提高',job['output_notice']);self.assertNotEqual(job['stage'],'已完成')

    def test_complete_long_answer_is_not_cut_by_old_limit(self):
        self.long_response=True;service=self.service(self.value())
        _,job,conversation=self.turn(service,output_limit=128)
        answer=conversation['messages'][-1]['content']
        self.assertEqual(answer,'合成的完整长回答。'*6000)
        self.assertEqual(self.captured[-1]['max_tokens'],393216)
        self.assertEqual(job['finish_reason'],'stop');self.assertIsNone(job['output_notice'])

    def test_maximum_output_does_not_reserve_away_normal_history(self):
        service=self.service(self.value(context_tokens=25000));sid=service.create_session()['id']
        self.turn(service,sid=sid,text='OLD-LONG-INPUT '+('x'*16000),output_limit=512)
        _,job,_=self.turn(service,sid=sid,thinking=True)
        self.assertIn('OLD-LONG-INPUT',json.dumps(self.captured[-1]))
        budget=json.loads(job['context'])['budget']
        self.assertEqual(budget['history_messages'],2);self.assertFalse(budget['history_omitted'])
        self.assertEqual(self.captured[-1]['max_tokens'],25000-budget['input_tokens'])
        self.assertGreater(self.captured[-1]['max_tokens'],512)

    def test_direct_recovery_drops_legacy_choice_and_keeps_provider(self):
        service=self.service(self.value());sid=service.create_session()['id']
        parent=service.submit(sid,'合成恢复问题。',thinking=True,output_limit=128)['id']
        with service.db() as db:
            request=json.loads(db.execute('SELECT requested FROM jobs WHERE id=?',(parent,)).fetchone()[0])
            request.update(output_limit=128,default_output_target=8192)
            db.execute("UPDATE jobs SET state='failed',requested=?,finish_reason='length',reasoning='synthetic' WHERE id=?",(json.dumps(request),parent))
        child=service.answer_directly(parent)['id']
        with service.db() as db:requested=json.loads(db.execute('SELECT requested FROM jobs WHERE id=?',(child,)).fetchone()[0])
        self.assertNotIn('output_limit',requested);self.assertNotIn('default_output_target',requested)
        self.assertFalse(requested['thinking']);self.assertEqual(requested['max_output_tokens'],393216)
        self.assertEqual(requested['provider_snapshot'],request['provider_snapshot'])

    def test_local_capability_and_context_never_use_old_short_choice(self):
        for limit in ('auto',128,512,'max'):
            request=requested_settings(True,limit,65536,32768)
            self.assertEqual(effective_settings(request,2000)['max_output_tokens'],32768)
            self.assertEqual(effective_settings(request,64000)['max_output_tokens'],1536)
            self.assertEqual(effective_settings(requested_settings(False,limit,8192),2000)['max_output_tokens'],6192)

    def test_service_close_releases_attachment_store_and_is_repeatable(self):
        service=self.service(self.value());actual=service.attachments.close
        with patch.object(service.attachments,'close',wraps=actual) as close:service.close();close.assert_called_once()
        service.close()


if __name__=='__main__':unittest.main()
