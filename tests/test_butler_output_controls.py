"""Execute real UI handlers with removed quota nodes absent, without a browser."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT=Path(__file__).resolve().parents[1]


class OutputControlTests(unittest.TestCase):
    def run_js(self,bindings,harness):
        node=shutil.which('node')
        if not node:self.skipTest('Node.js required for isolated UI handlers')
        script='\n'.join('const '+name+'='+json.dumps(value)+';' for name,value in bindings.items())+'\n'+harness
        result=subprocess.run([node,'-e',script],capture_output=True,text=True,encoding='utf-8',timeout=15,
            creationflags=0x08000000 if os.name=='nt' else 0)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_old_local_storage_does_not_resurrect_length_or_enter_ask_payload(self):
        source=(ROOT/'static/butler.js').read_text('utf-8')
        snippets=[next(line for line in source.splitlines() if marker in line) for marker in
            ('function saveChoices(){','function choiceLabels(){','function loadChoices(){')]
        ask="$('#butlerForm').onsubmit="+source.split("$('#butlerForm').onsubmit=",1)[1].split("\n  const input=$('#butlerQuestion')",1)[0]
        self.run_js({'SNIPPETS':snippets,'ASK':ask},r'''
const assert=require('node:assert/strict'),vm=require('node:vm'),nodes=new Map(),saved={},sent=[];
function $(id){if(/butler(?:Length|Limit)/.test(id))return null;if(!nodes.has(id)){const attrs={};nodes.set(id,{value:'',setAttribute:(k,v)=>attrs[k]=v,getAttribute:k=>attrs[k],querySelector:()=>({textContent:''}),focus(){}})}return nodes.get(id)}
const context={$ ,settingsLoaded:false,window:{ButlerControls:{thinkingMode:()=>'switchable',runtime(){},canChat:()=>true,toolsEnabled:()=>false}},
localStorage:{getItem:()=>JSON.stringify({thinking:false,output_limit:'512'}),setItem:(k,v)=>saved[k]=v},memoryKey:()=>'synthetic',
resizeInput(){},busy(){},feedback(){},newSessionPending:()=>false,imageRestoring:()=>false,attachmentRows:()=>[],visionSupported:()=>true,
submitting:false,composing:false,job:null,terminal:new Set(),selected:'synthetic-session',draftKey:()=>'draft',inputDraft:()=>({}),prefs:{username:'我'},
apiPost:async(url,body)=>{sent.push({url,body});return{}},clearSentImages(){},clearSubmittedDraft(){},refreshConversation:async()=>{},scrollLatest(){},errorText:e=>{throw e}};
vm.createContext(context);vm.runInContext(SNIPPETS.join('\n')+'\nloadChoices();saveChoices();',context);
assert.equal(context.window.ButlerThinkingPreference,false);assert.deepEqual(JSON.parse(saved['synthetic.generation']),{thinking:false});
$('#butlerQuestion').value='合成问题';$('#butlerScope').value='formal';vm.runInContext(ASK,context);
(async()=>{await $('#butlerForm').onsubmit({preventDefault(){}});assert.equal(sent.length,1);assert.equal(sent[0].body.question,'合成问题');assert.equal(sent[0].body.thinking,false);assert.equal(Object.hasOwn(sent[0].body,'output_limit'),false)})().catch(e=>{console.error(e);process.exitCode=1});
''')

    def test_provider_save_keeps_capability_and_does_not_read_removed_input(self):
        source=(ROOT/'static/butler-controls.js').read_text('utf-8')
        helper=next(line for line in source.splitlines() if 'function providerOutputCapacity(){' in line)
        save=next(line for line in source.splitlines() if "bindForm('butlerProviderForm'," in line)
        self.run_js({'HELPER':helper,'SAVE':save},r'''
const assert=require('node:assert/strict'),vm=require('node:vm'),nodes=new Map(),sent=[];
function q(id){if(id==='butlerProviderOutput')return null;if(!nodes.has(id))nodes.set(id,{value:'',checked:false});return nodes.get(id)}
for(const [id,value] of Object.entries({butlerProviderSelected:'external',butlerProviderURL:'https://synthetic.invalid/v1',butlerProviderModel:'known-model',butlerProviderDialect:'generic',butlerProviderKey:'',butlerProviderCapacity:'65536',butlerProviderThinkingMode:'unsupported'}))q(id).value=value;
const external={model:'known-model',base_url:'https://synthetic.invalid/v1',dialect:'generic',max_output_tokens:16000,output_capacity_source:'service_metadata'};
const context={q,provider:{external},remoteMetadata:[],remoteMetadataURL:'',providerEpoch:0,dirty:new Set(),panel:{querySelectorAll:()=>[]},
bindForm:(id,fn)=>context.save=fn,apiPost:async(url,body)=>{sent.push(body);return{external}},providerView(){}};
vm.createContext(context);vm.runInContext(HELPER+'\n'+SAVE,context);
(async()=>{await context.save();assert.equal(sent[0].max_output_tokens,16000);assert.equal(sent[0].output_capacity_source,'service_metadata');assert.equal(sent[0].api_key,null);
q('butlerProviderModel').value='new-unknown';await context.save();assert.equal(sent[1].max_output_tokens,null);assert.equal(sent[1].output_capacity_source,null);
context.remoteMetadata=[{id:'new-unknown',max_output_tokens:32000}];context.remoteMetadataURL=external.base_url;await context.save();assert.equal(sent[2].max_output_tokens,32000);
q('butlerProviderURL').value='https://other.invalid/v1';await context.save();assert.equal(sent[3].max_output_tokens,null);
})().catch(e=>{console.error(e);process.exitCode=1});
''')

    def test_all_provider_runtime_branches_and_run_info_work_without_length_nodes(self):
        controls=(ROOT/'static/butler-controls.js').read_text('utf-8')
        source=(ROOT/'static/butler.js').read_text('utf-8')
        runtime='function thinkingPresentation('+controls.split('  function thinkingPresentation(',1)[1].split('\n  function runtime(',1)[0]
        runtime+='\nfunction runtime(value){'+controls.split('  function runtime(value){',1)[1].split('\n  bindForm(',1)[0]
        info=next(line for line in source.splitlines() if 'function generationInfo(value){' in line)
        self.run_js({'RUNTIME':runtime,'INFO':info},r'''
const assert=require('node:assert/strict'),vm=require('node:vm'),nodes=new Map();
function q(id){if(/butler(?:Length|Limit)/.test(id)||id==='butlerProviderOutput')return null;if(!nodes.has(id)){const attrs={};nodes.set(id,{textContent:'',dataset:{},setAttribute:(k,v)=>attrs[k]=v,getAttribute:k=>attrs[k],querySelector:()=>({textContent:''})})}return nodes.get(id)}
const context={q,provider:null,lastRuntime:null,window:{ButlerThinkingPreference:true},scopes:{},toolCapability(){},CustomEvent:class{},document:{dispatchEvent(){},createElement:()=>({textContent:'',children:[],append(...nodes){this.children.push(...nodes)}})}};
vm.createContext(context);vm.runInContext(RUNTIME+'\n'+INFO,context);
for(const selected of ['local','external','hermes']){context.provider={selected,external:{model:'synthetic',thinking_mode:'switchable',credential_present:true,data_scopes:[]},hermes:{model:'synthetic'}};context.runtime({enabled:true,model:'synthetic',thinking_mode:'switchable'});assert.ok(q('butlerModel').textContent)}
const unknown=context.generationInfo({requested:{thinking:false,output_limit:128},effective:{model_used:true,output_limit_source:'service_default'}});
assert.match(unknown.children[1].textContent,/输出长度由服务决定/);assert.doesNotMatch(unknown.children[1].textContent,/选择上限|128/);
const known=context.generationInfo({requested:{thinking:true},effective:{max_output_tokens:393216}});assert.match(known.children[1].textContent,/393216/);
''')
