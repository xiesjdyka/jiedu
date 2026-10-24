import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
import httpx
from knowledge_bridge.butler_generation import requested_settings,effective_settings,input_limit
from knowledge_bridge.local_butler import LocalButler
from knowledge_bridge.workspace import initialize_workspace

class GenerationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.ctx=initialize_workspace(Path(self.temp.name)/'workspace')
        self.runtime=Mock();self.runtime.config.return_value={'context_tokens':8192};self.runtime.status.return_value={'state':'stopped'}
        self.runtime.endpoint.return_value='http://fixture';self.runtime.headers.return_value={}
        self.service=LocalButler(self.ctx,runtime=self.runtime,start_worker=False);self.addCleanup(self.service.close)
        self.service.cancel_event=threading.Event()

    def job(self,thinking=True,limit='max'):
        sid=self.service.create_session()['id'];jid=self.service.submit(sid,'fixture',thinking=thinking,output_limit=limit)['id']
        self.service._update(jid,state='running')
        with self.service.db() as db:job=dict(db.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone())
        return sid,jid,job

    def test_capacity_preserves_input_and_validates_before_any_write(self):
        request=requested_settings(True,'max');self.assertEqual(input_limit(request),7680)
        self.assertEqual(effective_settings(request,2000)['max_output_tokens'],6192)
        self.assertEqual(effective_settings(requested_settings(False,8192),8000)['max_output_tokens'],192)
        sid=self.service.create_session()['id']
        for value in [True,0,-1,8193,'512','unlimited',1.5]:
            with self.assertRaises(ValueError):self.service.submit(sid,'invalid',output_limit=value)
        self.assertEqual(self.service.conversation(sid)['messages'],[])

    def test_real_transport_contract_and_reasoning_only_limit(self):
        for thinking in (False,True):
            with self.subTest(thinking=thinking):
                sid,jid,job=self.job(thinking);calls=[]
                def handle(request):
                    payload=json.loads(request.content);calls.append((request.url.path,payload))
                    if request.url.path=='/apply-template':return httpx.Response(200,json={'prompt':'fixture'})
                    if request.url.path=='/tokenize':return httpx.Response(200,json={'tokens':[1]*2000})
                    delta={'reasoning_content':'思考😀'} if thinking else {'content':'答案'}
                    events=[{'choices':[{'delta':delta,'finish_reason':None}]},{'choices':[{'delta':{},'finish_reason':'length' if thinking else 'stop'}]}]
                    return httpx.Response(200,text=''.join('data: '+json.dumps(e)+'\n\n' for e in events)+'data: [DONE]\n\n')
                context={'user':{},'project':{},'state':{'captured_at':'fixture'},'related_knowledge':[],'permissions':{'read_only':True}}
                client=httpx.Client(transport=httpx.MockTransport(handle))
                with patch.object(self.service,'_context',return_value=(context,{})),patch('knowledge_bridge.local_butler.httpx.Client',return_value=client):self.service._execute(job)
                template=calls[0][1];completion=calls[-1][1]
                self.assertEqual(template['chat_template_kwargs'],completion['chat_template_kwargs'])
                self.assertEqual(completion['chat_template_kwargs']['enable_thinking'],thinking)
                self.assertEqual(completion['max_tokens'],6192);self.assertEqual(completion['reasoning_format'],'deepseek')
                conversation=self.service.conversation(sid);answer=conversation['messages'][-1]
                self.assertEqual(conversation['jobs'][0]['state'],'completed')
                self.assertEqual(answer['requested']['output_limit'],'max')
                if thinking:
                    self.assertEqual(answer['content'],'');self.assertEqual(answer['reasoning'],'思考😀');self.assertIn('尚未形成回答',answer['output_notice'])
                else:self.assertEqual(answer['content'],'答案');self.assertEqual(answer['reasoning'],'')
                reopened=LocalButler(self.ctx,runtime=self.runtime,start_worker=False)
                self.assertEqual(reopened.conversation(sid)['messages'],conversation['messages']);reopened.close()

    def test_canceled_channels_survive_restore_and_reject_late_output(self):
        sid,jid,job=self.job();self.service._delta(job,'推理😀','reasoning');self.service._delta(job,'半句答案')
        self.service.trash(sid)
        with self.assertRaises(InterruptedError):self.service._delta(job,'推理😀迟到','reasoning')
        self.service.trash(sid,restore=True)
        seq,snapshot=self.service.initial_stream(sid);saved=snapshot['conversation']['jobs'][0]
        self.assertEqual(saved['reasoning'],'推理😀');self.assertEqual(saved['preview'],'半句答案');self.assertEqual(saved['state'],'canceled')
        self.assertEqual(len(snapshot['conversation']['messages']),1)
        self.assertEqual(self.service.stream_events(sid,seq,0),[])

    def test_reasoning_clock_excludes_retrieval_and_stops_at_first_answer(self):
        sid,jid,job=self.job()
        self.assertIsNone(self.service.conversation(sid)['jobs'][0]['reasoning_started_at'])
        with patch('knowledge_bridge.local_butler.now',return_value='2026-09-30T01:00:10+00:00'):
            self.service._delta(job,'思考','reasoning')
        with patch('knowledge_bridge.local_butler.now',return_value='2026-09-30T01:00:12+00:00'):
            self.service._delta(job,'答案')
        with patch('knowledge_bridge.local_butler.now',return_value='2026-09-30T01:00:25+00:00'):
            self.service.cancel(jid)
        saved=self.service.conversation(sid)['jobs'][0]
        self.assertEqual(saved['reasoning_started_at'],'2026-09-30T01:00:10+00:00')
        self.assertEqual(saved['reasoning_finished_at'],'2026-09-30T01:00:12+00:00')
        sid,jid,job=self.job()
        self.service._delta(job,'尚在思考','reasoning')
        self.service.cancel(jid)
        self.assertIsNotNone(self.service.conversation(sid)['jobs'][0]['reasoning_finished_at'])

    def test_translation_is_separate_bounded_cached_and_cancel_safe(self):
        sid,jid,job=self.job();raw='Thinking Process: source only. '*120
        self.service._delta(job,raw,'reasoning');self.service._complete(job,'original',{'sources':[]},finish='length')
        original=self.service.conversation(sid)['messages']
        tid=self.service.translate_reasoning(jid)['id'];self.assertEqual(self.service.translate_reasoning(jid)['id'],tid)
        self.service._update(tid,state='running')
        with self.service.db() as db:tjob=dict(db.execute('SELECT * FROM jobs WHERE id=?',(tid,)).fetchone())
        calls=[]
        def handle(request):
            body=json.loads(request.content);calls.append((request.url.path,body))
            if request.url.path=='/apply-template':return httpx.Response(200,json={'prompt':'fixture'})
            if request.url.path=='/tokenize':return httpx.Response(200,json={'tokens':[1]*1200})
            return httpx.Response(200,text='data: '+json.dumps({'choices':[{'delta':{'content':'思考过程：来源原文。'},'finish_reason':'stop'}]})+'\n\ndata: [DONE]\n\n')
        client=httpx.Client(transport=httpx.MockTransport(handle))
        with patch('knowledge_bridge.local_butler.httpx.Client',return_value=client):self.service._execute(tjob)
        result=self.service.conversation(sid);translated=next(x for x in result['jobs'] if x['id']==tid)
        self.assertEqual(result['messages'],original);self.assertEqual(translated['preview'],'思考过程：来源原文。')
        self.assertEqual(translated['finish_reason'],'stop');self.assertTrue(translated['details']['budget']['excerpt']);self.assertTrue(translated['details']['budget']['original_incomplete'])
        self.assertEqual(translated['details']['budget']['translated_characters'],3000)
        self.assertFalse(calls[-1][1]['chat_template_kwargs']['enable_thinking']);self.assertEqual(calls[-1][1]['max_tokens'],384);self.assertNotIn('stop',calls[-1][1]);self.assertEqual(translated['requested']['translation_format_version'],3)
        self.assertEqual(self.service.translate_reasoning(jid)['id'],tid)
        with self.service.db() as db:db.execute("UPDATE jobs SET requested=json_set(requested,'$.translation_format_version',2) WHERE id=?",(tid,))
        old_retry=self.service.translate_reasoning(jid)['id'];self.assertNotEqual(old_retry,tid);self.service.cancel(old_retry)
        with self.service.db() as db:db.execute("UPDATE jobs SET requested=json_set(requested,'$.translation_format_version',3),finish_reason='length' WHERE id=?",(tid,))
        retry=self.service.translate_reasoning(jid)['id'];self.assertNotEqual(retry,tid)
        self.service._update(retry,state='running')
        with self.service.db() as db:retryjob=dict(db.execute('SELECT * FROM jobs WHERE id=?',(retry,)).fetchone())
        self.service.trash(sid)
        with self.assertRaises(InterruptedError):self.service._complete(retryjob,'迟到译文',{'sources':[]},finish='stop')
        self.service.trash(sid,restore=True);self.assertEqual(self.service.conversation(sid)['messages'],original)
        with self.assertRaises(ValueError):self.service.translate_reasoning(tid)

class ButlerRuntimeUiTests(unittest.TestCase):
    def test_thinking_toggle_survives_capability_refresh_and_is_saved(self):
        import shutil
        import subprocess
        node=shutil.which('node')
        if not node:self.skipTest('Node.js is needed for this isolated UI logic check')
        root=Path(__file__).resolve().parents[1]
        controls=(root/'static/butler-controls.js').read_text('utf-8')
        source=(root/'static/butler.js').read_text('utf-8')
        runtime='function runtime(value){'+controls.split('  function runtime(value){',1)[1].split('\n  bindForm(',1)[0]
        snippets=[next(line for line in source.splitlines() if marker in line) for marker in
                  ['function saveChoices(){','function choiceLabels(){',"$('#butlerThinking').onclick="]]
        harness=r'''
const assert=require('node:assert/strict'),vm=require('node:vm'),nodes=new Map(),saved={};
function $(id){if(!nodes.has(id)){const attrs={},label={textContent:''};nodes.set(id,{value:'512',setAttribute:(k,v)=>attrs[k]=v,getAttribute:k=>attrs[k]||'false',querySelector:()=>label,querySelectorAll:()=>[]})}return nodes.get(id)}
const context={$ ,q:id=>$('#'+id),provider:{selected:'external',external:{thinking_mode:'switchable',model:'synthetic-model',data_scopes:[]}},lastRuntime:null,
window:{ButlerThinkingPreference:false},localStorage:{setItem:(key,value)=>saved[key]=value},memoryKey:()=>'fixture',lengthLabels:{512:'标准'},
toolCapability(){},document:{dispatchEvent(){}},CustomEvent:class{}};
vm.createContext(context);vm.runInContext(RUNTIME,context);
context.window.ButlerControls={thinkingMode:()=>'switchable',runtime:()=>context.runtime()};
vm.runInContext(SNIPPETS.join('\n'),context);context.runtime();
assert.equal($('#butlerThinking').getAttribute('aria-pressed'),'false');
$('#butlerThinking').onclick();assert.equal($('#butlerThinking').getAttribute('aria-pressed'),'true');assert.equal(context.window.ButlerThinkingPreference,true);assert.equal(JSON.parse(saved['fixture.generation']).thinking,true);
context.runtime();assert.equal($('#butlerThinking').getAttribute('aria-pressed'),'true');
$('#butlerThinking').onclick();assert.equal($('#butlerThinking').getAttribute('aria-pressed'),'false');assert.equal(JSON.parse(saved['fixture.generation']).thinking,false);
context.window.ButlerControls.thinkingMode=()=>'unsupported';$('#butlerThinking').onclick();assert.equal($('#butlerThinking').getAttribute('aria-pressed'),'false');
'''
        result=subprocess.run([node,'-e','const RUNTIME='+json.dumps(runtime)+';const SNIPPETS='+json.dumps(snippets)+';\n'+harness],
                              capture_output=True,text=True,encoding='utf-8',timeout=15,
                              creationflags=0x08000000 if __import__('os').name=='nt' else 0)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_unconfigured_note_and_configured_provider_branches(self):
        """Run the actual render function with a minimal DOM; no server or model."""
        import shutil
        import subprocess
        node=shutil.which('node')
        if not node:self.skipTest('Node.js is needed for this isolated UI logic check')
        source=(Path(__file__).resolve().parents[1]/'static/butler-controls.js').read_text('utf-8')
        function=source.split('  function runtime(value){',1)[1].split('\n  bindForm(',1)[0]
        function='function runtime(value){'+function
        harness=r'''
const assert=require('node:assert/strict'),vm=require('node:vm');
function render(provider,value){
  const nodes=new Map();
  function q(id){if(!nodes.has(id)){const attributes={};const label={textContent:''};nodes.set(id,{textContent:'',title:'',value:'512',setAttribute:(k,v)=>attributes[k]=v,getAttribute:k=>attributes[k],querySelector:()=>label})}return nodes.get(id)}
  const context={provider,lastRuntime:null,q,window:{ButlerThinkingPreference:false},toolCapability(){},document:{dispatchEvent(){}},CustomEvent:class{}};
  vm.runInNewContext(FUNCTION,context);context.runtime(value);
  return {note:q('butlerOutgoingScope').textContent,model:q('butlerModel').textContent};
}
const empty=render({selected:'local'},{enabled:false,state:'not_configured'});
assert.match(empty.note,/尚未选择模型/);assert.doesNotMatch(empty.note,/当前使用本机模型/);assert.equal(empty.model,'选择模型');
const local=render({selected:'local'},{enabled:true,state:'stopped',model:'synthetic-local'});
assert.equal(local.note,'当前使用本机模型。联网须另行开启；工具须逐项授权。');assert.equal(local.model,'synthetic-local');
const external=render({selected:'external',external:{model:'synthetic-api',credential_present:true,data_scopes:[]}},{enabled:false});
assert.match(external.note,/外发范围：无工作区资料/);assert.equal(external.model,'synthetic-api');
assert.match(external.note,/当前接入设置下、本对话中已发送的消息和图片/);
assert.match(external.note,/超过本轮容量时省略较早内容/);
assert.match(external.note,/旧本地对话、其他接入设置、全库和画像不自动外发/);assert.doesNotMatch(external.note,/单轮/);
const hermes=render({selected:'hermes',hermes:{model:'synthetic-hermes'}},{enabled:false});
assert.match(hermes.note,/Hermes：发送本轮问题/);assert.equal(hermes.model,'Hermes · synthetic-hermes');
'''
        result=subprocess.run([node,'-e','const FUNCTION='+json.dumps(function)+';\n'+harness],
                              capture_output=True,text=True,encoding='utf-8',timeout=15,
                              creationflags=0x08000000 if __import__('os').name=='nt' else 0)
        self.assertEqual(result.returncode,0,result.stderr)

if __name__=='__main__':unittest.main()
