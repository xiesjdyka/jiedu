import json,threading,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
import httpx
from knowledge_bridge.workspace import initialize_workspace
from knowledge_bridge.local_butler import LocalButler
from knowledge_bridge.butler_provider import ButlerProviders
from tests.test_butler_provider import ProviderTests
from knowledge_bridge.product_help import select_topics

class ProductIdentityWireTests(unittest.TestCase):
    setUp=ProviderTests.setUp
    def service(self,allowed):
        context=initialize_workspace(Path(self.temp.name)/'vault')
        provider=ButlerProviders(context,secrets=self.secrets,isolated=True)
        provider.configure(selected='external',base_url=self.url,model='synthetic-deepseek',dialect='deepseek',api_key='synthetic-key',data_scopes=allowed)
        runtime=Mock();runtime.config.return_value={};runtime.status.return_value={'state':'stopped'}
        retriever=Mock();retriever.search.return_value={'effective_mode':'keyword','results':[{'source_path':'99_收件箱/合成识别.md','version':'synthetic-version','content':'白鹭图书馆6284，下一步是核对会议纪要。','verification':'ocr_unverified'}]}
        service=LocalButler(context,runtime=runtime,providers=provider,retriever=retriever,start_worker=False);self.addCleanup(service.close)
        service.cancel_event=threading.Event();self.fragments=[({'content':'合成回答[S1]'},'stop')]
        sid=service.create_session()['id'];jid=service.submit(sid,'概述这份资料的下一步',scope='inbox')['id'];service._update(jid,state='running')
        with service.db() as db:job=dict(db.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone())
        return service,job,retriever
    def test_unapproved_inbox_never_retrieved_or_sent_but_identity_is_present(self):
        service,job,retriever=self.service([]);service._execute(job);retriever.search.assert_not_called()
        wire=json.dumps(self.requests,ensure_ascii=False)
        self.assertIn('界渡身份与能力导航',wire);self.assertIn('本轮工具关闭',wire);self.assertNotIn('白鹭图书馆6284',wire)
        self.assertNotIn(str(service.context.root),wire)
    def test_explicit_inbox_retrieved_on_wire_and_ocr_verification_preserved(self):
        service,job,retriever=self.service(['inbox']);service._execute(job)
        self.assertEqual('inbox',retriever.search.call_args.kwargs['scope'])
        wire=json.dumps(self.requests,ensure_ascii=False);self.assertIn('白鹭图书馆6284',wire);self.assertIn('ocr_unverified',wire);self.assertIn('界渡身份与能力导航',wire)
        with service.db() as db:result=json.loads(db.execute('SELECT context FROM jobs WHERE id=?',(job['id'],)).fetchone()[0])
        self.assertEqual('ocr_unverified',result['sources'][0]['verification'])
        evidence=Path(__file__).resolve().parents[2]/'evidence'
        # Only write to an explicitly isolated audit parent, never a source repo.
        if evidence.is_dir() and 'audit' in str(evidence):
            (evidence/'identity-external-mock-wire-r7.json').write_text(json.dumps({'passed':True,'requests':self.requests,'contains_only_synthetic_sources':True,'headers_or_secrets_recorded':False},ensure_ascii=False,indent=2),'utf-8')

class LocalIdentityTests(unittest.TestCase):
    def test_product_help_and_user_file_intents_are_distinct(self):
        self.assertFalse(select_topics('在界渡里查白鹭图书馆6284截图的下一步')[0])
        self.assertFalse(select_topics('在界渡找出刚导入文件中的日期')[0])
        self.assertTrue(select_topics('介绍界渡的功能与使用说明')[0])
        self.assertTrue(select_topics('界渡OCR怎么用')[0])
    def test_ordinary_local_request_template_includes_identity_and_tool_truth(self):
        with tempfile.TemporaryDirectory() as tmp:
            context=initialize_workspace(Path(tmp)/'vault');runtime=Mock();runtime.config.return_value={'context_tokens':8192};runtime.endpoint.return_value='http://synthetic';runtime.headers.return_value={}
            service=LocalButler(context,runtime=runtime,start_worker=False);self.addCleanup(service.close)
            service.cancel_event=threading.Event()
            sid=service.create_session()['id'];jid=service.submit(sid,'概述这份资料')['id'];service._update(jid,state='running')
            with service.db() as db:job=dict(db.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone())
            calls=[]
            def handle(request):
                data=json.loads(request.content);calls.append({'path':request.url.path,'body':data})
                return httpx.Response(200,json={'prompt':'synthetic local prompt'} if request.url.path=='/apply-template' else {'tokens':[1]*500})
            local_context={'user':{},'project':{},'state':{},'related_knowledge':[],'permissions':{'read_only':True,'tools':[]}}
            with patch.object(service,'_context',return_value=(local_context,{'effective_mode':'keyword'})),httpx.Client(transport=httpx.MockTransport(handle)) as client:
                messages,saved=service._messages(job,client)
            self.assertIn('界渡身份与能力导航',calls[0]['body']['messages'][0]['content']);self.assertIn('本轮工具关闭',messages[0]['content']);self.assertNotIn(str(context.root),messages[0]['content'])
            service.tool_registry=Mock();service.tool_registry.tools={'knowledge_search':object(),'source_read':object()}
            enabled=service._request_system({'tools_enabled':True});self.assertIn('knowledge_search、source_read',enabled);self.assertIn('注册不等于已授权',enabled)
