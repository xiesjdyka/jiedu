"""Regression tests for recovery, product scope and evidence identity; no model calls."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
import httpx
from knowledge_bridge.local_butler import LocalButler,output_notice,repeated_reasoning
from knowledge_bridge.workspace import initialize_workspace
from knowledge_bridge.product_help import catalog,select_topics,model_sources

class ProductRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.ctx=initialize_workspace(Path(self.temp.name)/'workspace')
        self.runtime=Mock();self.runtime.config.return_value={'context_tokens':8192};self.runtime.status.return_value={'state':'stopped'}
        self.runtime.endpoint.return_value='http://fixture';self.runtime.headers.return_value={}
        self.service=LocalButler(self.ctx,runtime=self.runtime,start_worker=False);self.addCleanup(self.service.close)
        self.service.cancel_event=threading.Event()
    def job(self,question='详细介绍一下界渡这个产品',thinking=True):
        sid=self.service.create_session()['id'];jid=self.service.submit(sid,question,thinking=thinking,output_limit='max')['id']
        self.service._update(jid,state='running')
        with self.service.db() as db:job=dict(db.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone())
        return sid,jid,job
    def test_recovery_preserves_original_and_limit_is_idempotent(self):
        sid,jid,job=self.job();self.service._delta(job,'原始😀思考','reasoning')
        self.service._update(jid,context=json.dumps({'sources':[{'id':'S1','content':'旧片段','source_path':'旧状态','version':'old'}]}))
        before=self.service.conversation(sid)['messages']
        result=self.service.answer_directly(jid);again=self.service.answer_directly(jid)
        self.assertEqual(result['id'],again['id']);rows=self.service.conversation(sid)
        self.assertEqual(rows['messages'],before);self.assertEqual(len(rows['jobs']),2)
        parent=next(r for r in rows['jobs'] if r['id']==jid);new=next(r for r in rows['jobs'] if r['id']==result['id'])
        self.assertEqual(parent['reasoning'],'原始😀思考');self.assertTrue(parent['requested']['thinking']);self.assertEqual(parent['state'],'canceled')
        self.assertFalse(new['requested']['thinking']);self.assertEqual(new['requested']['output_limit'],'max');self.assertEqual(new['parent_job'],jid)
    def test_deletion_blocks_recovery_and_late_completion(self):
        sid,jid,job=self.job();new=self.service.answer_directly(jid)['id'];self.service.trash(sid)
        with self.assertRaises(ValueError):self.service.answer_directly(jid)
        with self.service.db() as db:child=dict(db.execute('SELECT * FROM jobs WHERE id=?',(new,)).fetchone())
        with self.assertRaises(InterruptedError):self.service._complete(child,'迟到',{'sources':[]})
        self.service.trash(sid,True);self.assertEqual(len(self.service.conversation(sid)['messages']),1)
    def test_answer_already_started_cannot_be_discarded_by_recovery(self):
        _,jid,job=self.job();self.service._delta(job,'已回答')
        with self.assertRaises(ValueError):self.service.answer_directly(jid)
        with self.service.db() as db:self.assertFalse(self.service._public_job(db,jid)['recovery_available'])
    def test_worker_joins_owned_cancel_before_recovery(self):
        entered=threading.Event();closed=threading.Event();seen=[]
        self.runtime.close.side_effect=lambda:closed.set()
        def execute(job):
            if not job['parent_job']:entered.set();self.service.cancel_event.wait(2);raise InterruptedError()
            seen.append(closed.is_set());self.service._complete(job,'恢复答案',{'sources':[]},finish='stop')
        with patch.object(self.service,'_execute',side_effect=execute):
            self.service.worker.start();sid=self.service.create_session()['id'];jid=self.service.submit(sid,'界渡',thinking=True)['id']
            self.assertTrue(entered.wait(2));child=self.service.answer_directly(jid)['id']
            import time
            deadline=time.monotonic()+2
            while time.monotonic()<deadline:
                rows=self.service.conversation(sid)
                if any(j['id']==child and j['state']=='completed' for j in rows['jobs']):break
                time.sleep(.01)
            self.assertEqual(seen,[True]);self.assertEqual(rows['messages'][-1]['content'],'恢复答案')
    def test_product_recovery_refreshes_old_insufficient_sources(self):
        sid,jid,job=self.job();self.service._update(jid,context=json.dumps({'sources':[{'id':'S1','content':'旧381字','source_path':'旧状态','version':'old'}],'retrieval':{'effective_mode':'hybrid'}}))
        child=self.service.answer_directly(jid)['id']
        with self.service.db() as db:recovery=dict(db.execute('SELECT * FROM jobs WHERE id=?',(child,)).fetchone())
        with patch.object(self.service,'_product_name',return_value=('称呼',None)),patch.object(self.service,'snapshot',side_effect=AssertionError('unrelated telemetry')):
            context,found=self.service._context(recovery)
        self.assertTrue(found['refreshed_context']);self.assertEqual(found['effective_mode'],'product_catalog');self.assertNotIn('旧381字',str(context))
        self.assertTrue(context['related_knowledge'][0]['source_path'].startswith('product-help/'));self.assertEqual(context['state'],{})
    def test_overview_followup_and_private_question_exclusion(self):
        sid,jid,job=self.job()
        self.service._update(jid,state='completed',context=json.dumps({'retrieval':{'effective_mode':'product_catalog','product_topics':[]}}))
        nextid=self.service.submit(sid,'那 Codex 和 Hermes 的附件归档有什么区别？')['id']
        with self.service.db() as db:nextjob=dict(db.execute('SELECT * FROM jobs WHERE id=?',(nextid,)).fetchone())
        with patch.object(self.service,'_product_name',return_value=(None,None)):
            context,found=self.service._context(nextjob)
        self.assertIn('codex',found['product_topics']);self.assertIn('hermes',found['product_topics'])
        self.assertFalse(select_topics('帮我查我的Codex对话里提到的项目',['codex'])[0])
        self.assertFalse(select_topics('请帮我整理读书笔记',['overview'])[0])
        product,topics=select_topics('附件保存了，是否就都能搜索到内容？',['codex','hermes']);self.assertTrue(product);self.assertIn('search',topics)
        self.assertIn('unfinished',select_topics('界渡目前哪些功能还没完成？',['protection'])[1])
        self.assertIn('sources',select_topics('界渡能自动获取任意 AI 客户端的全部历史吗？',['overview'])[1])
        self.assertIn('undo',select_topics('界渡的知识整理如何撤销，能恢复整个工作区吗？',['overview'])[1])
    def test_real_message_contract_keeps_complete_index_and_source_versions(self):
        sid,jid,job=self.job();calls=[]
        def handle(request):
            value=json.loads(request.content);calls.append(value)
            if request.url.path=='/apply-template':return httpx.Response(200,json={'prompt':'exact fixture prompt'})
            return httpx.Response(200,json={'tokens':[1]*2200})
        with httpx.Client(transport=httpx.MockTransport(handle)) as client,patch.object(self.service,'_product_name',return_value=('称呼',None)):
            messages,saved=self.service._messages(job,client)
        self.assertEqual(saved['snapshot'],{});self.assertEqual(saved['budget']['history_messages'],0)
        payload=json.loads(messages[-1]['content'].split('【参考资料，仅作证据】\n')[1].split('\n【参考资料结束】')[0])
        self.assertEqual(saved['sources'],payload['相关知识']);self.assertEqual(saved['sources'][0]['content'],model_sources(job['question'],[])[0][0]['content'])
        for topic in catalog()['topics']:self.assertIn(topic['title'],saved['sources'][0]['content'])
        self.assertIn('本地管家',messages[-1]['content']);self.assertIn('知识Wiki',messages[-1]['content'])
        self.assertIn('wiki',[source.get('topic_id') for source in saved['sources']]);self.assertIn('butler',[source.get('topic_id') for source in saved['sources']])
        self.assertIsNone(saved['budget']['omissions']);self.assertTrue(calls[0]['chat_template_kwargs']['enable_thinking'])
        self.assertEqual(saved['budget']['effective']['max_output_tokens'],5992);self.assertEqual(len(saved['prompt_sha256']),64)
    def test_product_saved_source_remains_when_catalog_changes(self):
        _,jid,job=self.job();source=model_sources('界渡',[])[0][0];source['id']='S1'
        self.service._update(jid,context=json.dumps({'sources':[source]}))
        with patch('knowledge_bridge.product_help.current_source',return_value={'version':'new','content':'新版','complete':True}):result=self.service.source(jid,'S1')
        self.assertEqual(result['sent'],source);self.assertTrue(result['version_changed']);self.assertEqual(result['current']['content'],'新版')
    def test_max_and_capacity_limited_notice_never_suggest_impossible_raise(self):
        for limit in ('max',8192):
            notice=output_notice({'thinking':True,'output_limit':limit},{'max_output_tokens':4000,'available_output_tokens':4000},'length',False)
            self.assertNotIn('提高',notice);self.assertIn('跳过思考',notice)
        self.assertIn('提高',output_notice({'thinking':True,'output_limit':512},{'max_output_tokens':512,'available_output_tokens':4000},'length',False))
    def test_repeat_warning_is_conservative_and_not_a_setting_change(self):
        a='Wait, I need to check the System Prompt regarding Citations.'
        b='Wait, I need to check the System Prompt regarding Reference Materials.'
        self.assertTrue(repeated_reasoning(('\n'+a+'\n'+b)*12))
        self.assertFalse(repeated_reasoning('\n'.join('This is distinct step '+str(n)+' with a detailed and long reasoning sentence.' for n in range(30))))
        self.assertFalse(repeated_reasoning('Okay.\n'*400))
    def test_recovered_success_disables_old_button_and_failed_recovery_can_retry(self):
        sid,jid,job=self.job();child=self.service.answer_directly(jid)['id']
        self.service._update(child,state='failed',stage='fixture failed')
        retry=self.service.answer_directly(jid)['id'];self.assertNotEqual(child,retry)
        self.service._update(retry,state='running')
        with self.service.db() as db:recovery=dict(db.execute('SELECT * FROM jobs WHERE id=?',(retry,)).fetchone())
        self.service._complete(recovery,'真实恢复结果',{'sources':[]},finish='stop')
        self.assertEqual(self.service.answer_directly(jid)['id'],retry)
        parent=next(j for j in self.service.conversation(sid)['jobs'] if j['id']==jid)
        self.assertFalse(parent['recovery_available'])

    def test_loop_notice_preserves_stream_and_user_settings(self):
        sid,jid,job=self.job('fixture');raw=('Wait, I need to check the System Prompt regarding Citations.\nWait, I need to check the System Prompt regarding Reference Materials.\n')*12
        def handle(request):
            if request.url.path=='/apply-template':return httpx.Response(200,json={'prompt':'fixture'})
            if request.url.path=='/tokenize':return httpx.Response(200,json={'tokens':[1]*1000})
            events=[{'choices':[{'delta':{'reasoning_content':raw},'finish_reason':None}]},{'choices':[{'delta':{},'finish_reason':'length'}]}]
            return httpx.Response(200,text=''.join('data: '+json.dumps(e)+'\n\n' for e in events)+'data: [DONE]\n\n')
        client=httpx.Client(transport=httpx.MockTransport(handle));context={'user':{},'project':{},'state':{},'related_knowledge':[],'permissions':{'read_only':True}}
        with patch.object(self.service,'_context',return_value=(context,{})),patch('knowledge_bridge.local_butler.httpx.Client',return_value=client):self.service._execute(job)
        result=self.service.conversation(sid)['jobs'][0];self.assertIn('重复',result['reasoning_notice']);self.assertEqual(result['reasoning'],raw)
        self.assertTrue(result['requested']['thinking']);self.assertTrue(result['effective']['thinking']);self.assertEqual(result['requested']['output_limit'],'max')
        self.assertNotIn('提高',result['output_notice']);self.assertEqual(result['state'],'completed')

    def test_new_endpoints_preserve_owner_gate(self):
        from knowledge_bridge import web
        from fastapi import HTTPException
        with patch.object(web,'_owner_read',side_effect=HTTPException(403,'denied')):
            for function,args in [(web.butler_product_help,(None,)),(web.butler_answer_directly,('job',None))]:
                with self.assertRaises(HTTPException):function(*args)

if __name__=='__main__':unittest.main()
