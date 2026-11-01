const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),path=require('node:path');
const ROOT=path.resolve(__dirname,'..'),source=fs.readFileSync(path.join(ROOT,'static/app.js'),'utf8');
// All rows are invented fixtures. No local status snapshot or previous checkout is read.
const sample={registered:Object.entries({codex:[true,'valid','present'],hermes:[true,'valid','present'],
  opencode:[null,'unknown','present'],dsh:[false,'unknown','present'],cursor:[false,'missing','present'],
  workbuddy:[false,'unknown','absent'],zcode:[false,'unknown','absent'],claude:[null,'bundle_prepared','unknown']})
  .map(([client_type,[configuration_current,configuration_state,installation_state]],i)=>({
    internal_id:'synthetic-'+i,client_type,configuration_current,configuration_state,installation_state,
    configuration_present:client_type==='dsh'?true:configuration_current,read_verification_current:false,status:'read_verified',
    last_read_at:'2026-01-01T10:00:00Z'}))};
const segment=(from,to)=>source.slice(source.indexOf(from),source.indexOf(to,source.indexOf(from)));
const labels=segment('function clientRecentRead(','function clientLegacyCard(');
const daily=segment('function collectorSavedText(','function configuredLegacyClients(');
const order=source.split('\n').find(line=>line.startsWith('function orderedBrandedKinds('));
const card=segment('function brandedClientCard(','function brandedConnectPanel(');
const polling=segment('let clientAccessPollTimer=','function resolveSelectedClient(');
const loader=segment('function loadClientAccessControl(','\n$(\'#pairClient\')');
const post=segment('async function clientAccessPost(','function clientPolicyValue(');
const navigation=segment('function switchPage(','\n$$(\'.sidebar nav button[data-page]\')');
const sent=source.split('\n').find(line=>line.includes("$$('[data-sent-branded]').forEach"));
const clone=x=>JSON.parse(JSON.stringify(x)),flush=async()=>{for(let i=0;i<25;i++)await Promise.resolve()};
function deferred(){let resolve,reject;const promise=new Promise((r,j)=>{resolve=r;reject=j});return {promise,resolve,reject}}
function environment(){
  let nextTimer=0;const timers=new Map(),listeners=new Map(),nodes=new Map(),calls=[],renders=[],aborts=[];
  function node(id){if(!nodes.has(id)){const attrs={},n={id,value:'',hidden:false,open:false,innerHTML:'',textContent:'',dataset:{},className:'',disabled:false,
    classList:{toggle(){},add(){},remove(){}},setAttribute:(k,v)=>attrs[k]=v,getAttribute:k=>attrs[k],addEventListener(){},removeEventListener(){},
    querySelector:()=>null,querySelectorAll:()=>[],contains:other=>!!other?.inRoot,before(){},matches:s=>s==='dialog'&&id==='modal',closest:()=>null,focus(){}};nodes.set(id,n)}return nodes.get(id)}
  const root=node('clientAccessControl'),detail=node('detail');detail.className='permission-group';detail.open=true;
  root.querySelectorAll=s=>s==='details'?[detail]:[];
  root.querySelector=s=>s.includes('data-sent')&&context.sentBusy?node('sent'):s.includes('data-copy')&&context.copyBusy?node('copy'):null;
  const document={hidden:false,activeElement:null,body:node('body'),querySelector:s=>s==='dialog[open]'?(context.modalOpen?node('modal'):null):s==='.page.active'?node('access'):null,
    querySelectorAll:()=>[],addEventListener:(name,fn)=>{if(!listeners.has(name))listeners.set(name,[]);listeners.get(name).push(fn)},removeEventListener(){},createElement:()=>node('clientAccessRefreshStatus')};
  const prot={owner_session_active:false,storage_enabled:false,csrf_token:'synthetic-csrf'};
  const store=new Map(),context={console,Promise,Map,Set,Date,JSON,Number,Boolean,String,Math,Error,performance:{now:()=>10},activePage:'access',pageNavigationSequence:0,clientAccessLoadSequence:0,
    clientAccessState:null,clientAccessSelected:'preset:codex',protectionStatus:clone(prot),protectionCsrf:'',modalOpen:false,sentBusy:false,copyBusy:false,
    brandedClientNames:{codex:'Codex',hermes:'Hermes',dsh:'DSH',claude:'Claude',cursor:'Cursor',workbuddy:'WorkBuddy',opencode:'OpenCode',zcode:'ZCode'},
    pairingPolicyLabels:{},sessionStorage:{getItem:k=>store.get(k),setItem:(k,v)=>store.set(k,v),removeItem:k=>store.delete(k)},
    esc:x=>String(x??''),formatTime:x=>String(x??''),protectionMessage:(e,f)=>e?.message||f,isRequestCancelled:e=>e?.name==='AbortError',
    $:s=>node(s.replace(/^#/,'')),$$:()=>[],document,
    setTimeout:(fn,ms)=>{const id=++nextTimer;timers.set(id,{fn,ms});return id},clearTimeout:id=>timers.delete(id),
    api:async(url,options)=>{calls.push({url,options});return context.respond(url)},
    respond:url=>url.includes('/api/protection/')?clone(prot):url.includes('/confirmations')?{pending:[],confirmed:[]}:url.includes('/audit')?{events:[]}:clone(context.status),
    readProtectionSession:async()=>clone(context.protection),protection:clone(prot),status:{registered:[]},
    abortPageRequests:scope=>aborts.push(scope),
    renderClientAccess:()=>{renders.push(clone(context.clientAccessState));node('clientScopeKind').value='all';node('clientScopeValue').value='';detail.open=false},
    clientAccessErrorMessage:()=>'',fetch:async()=>({ok:true,json:async()=>({synthetic:true})}),
    workspaceRuntime:null,navigationCleanup(){},pageScrollState:new Map(),detailsState:new Map(),clearPageFeedback(){},pages:{overview:['a','b','c']},
    withPageFeedback:(name,text,fn)=>fn(),loadOverview:()=>Promise.resolve(),window:{scrollY:0,scrollTo(){},closeWorkspaceNavigation(){}},requestAnimationFrame:fn=>fn(),showToast(){}};
  node('access').id='access';vm.createContext(context);vm.runInContext(labels+daily+order+'\n'+card+polling+loader+post+navigation,context);
  function event(type,target){for(const fn of listeners.get(type)||[])fn({target})}
  function timer(ms=5000){const row=[...timers.entries()].find(([,r])=>r.ms===ms);assert.ok(row,'scheduled timer missing: '+ms);timers.delete(row[0]);return row[1].fn()}
  function get(name){return vm.runInContext(name,context)}
  return {c:context,node,calls,renders,timers,aborts,event,timer,get,prot};
}
const tests=[];function test(name,fn){tests.push([name,fn])}
test('合成客户端记录区分当前配置与历史读取，不声称在线',()=>{
 const c=environment().c;
 const expected={codex:'配置有效',hermes:'配置有效',opencode:'配置待核对',dsh:'配置需检查',cursor:'配置未检出',workbuddy:'程序未检出',zcode:'程序未检出'};
 for(const client of sample.registered){const label=c.clientReadLabel(client);assert.notEqual(label,'已接入');assert.notEqual(label,'已连接');
   if(expected[client.client_type])assert.equal(label,expected[client.client_type]);
   assert.match(c.clientReadEvidence(client),/历史成功读取/);}
});
test('近期读取不冒充实时在线；配置破损、不可读、停用、移除和不匹配优先显示',()=>{
 const c=environment().c,base={client_type:'codex',status:'read_verified',configuration_current:true,configuration_present:true,read_verification_current:true,last_read_at:'2026-11-01T10:00:00Z',installation_state:'present'};
 assert.equal(c.clientReadLabel(base),'最近读取成功');assert.match(c.clientReadEvidence(base),/不代表实时在线/);
 for(const [state,label] of Object.entries({invalid:'配置格式需检查',unreadable:'配置无法核对',disabled:'配置已停用',missing:'配置未检出',mismatch:'配置不匹配'})){
   const client={...base,configuration_state:state,configuration_current:state==='unreadable'?null:false};assert.equal(c.clientReadLabel(client),label);assert.equal(c.clientRecentRead(client),false);assert.match(c.clientReadEvidence(client),/历史成功读取/);
 }
 assert.equal(c.clientReadLabel({...base,read_verification_current:false}),'配置有效');
 assert.equal(c.clientReadLabel({...base,installation_state:'absent'}),'程序未检出');
 assert.equal(c.clientReadLabel({...base,status:'revoked'}),'已撤销');
});
test('排序只依据当前配置/近期验证，不把旧读取排为已接入',()=>{
 const c=environment().c;c.clientAccessState={};const rows=[{client_type:'opencode',last_read_at:'2026-10-01',configuration_current:null,installation_state:'present'},
 {client_type:'zcode',configuration_current:true,installation_state:'present'}];const order=c.orderedBrandedKinds(rows);assert.equal(order[0],'zcode');
 const html=c.brandedClientCard('opencode',rows[0]);assert.match(html,/配置待核对/);assert.doesNotMatch(html,/已接入|已连接/);
 const daily=c.clientDailyStatus(rows[0]);assert.match(daily,/历史成功读取/);assert.match(daily,/连接状态与聊天采集分开/);
});
test('所有客户端安装/配置/权限/读取/增删均在同一5秒刷新中更新',async()=>{
 const e=environment(),c=e.c;c.status={registered:clone(sample.registered)};await c.loadClientAccessControl();const count=e.renders.length;
 c.status.registered[0].installation_state='absent';c.status.registered[1].policies=[{scope:'formal',operation:'read',decision:'deny'}];
 c.status.registered.push({internal_id:'new',client_type:'synthetic',configuration_current:null});await e.timer();
 assert.equal(e.renders.length,count+1);assert.equal(c.clientAccessState.registered[0].installation_state,'absent');assert.equal(c.clientAccessState.registered[1].policies[0].decision,'deny');
 c.status.registered.splice(0,1);await e.timer();assert.equal(c.clientAccessState.registered.length,sample.registered.length);
 assert.ok(e.calls.filter(r=>r.url.includes('/client-access/status')).length===3);assert.ok([...e.timers.values()].some(r=>r.ms===5000));
});
test('背景刷新只读且无变化不重建界面',async()=>{
 const e=environment(),c=e.c;let renewals=0;c.readProtectionSession=async()=>{renewals++;return clone(e.prot)};
 await c.loadClientAccessControl();await e.timer();await e.timer();assert.equal(e.renders.length,1);assert.equal(renewals,1);
 assert.ok(e.calls.some(r=>r.url.startsWith('/api/protection/status')));assert.ok(e.calls.every(r=>r.options.scope==='client-access-refresh'));
});
test('慢请求期间不重叠；显式更新排队并阻止旧响应覆盖',async()=>{
 const e=environment(),c=e.c,old=deferred();let count=0;
 c.respond=url=>url.includes('/client-access/status')?(++count===1?old.promise:{registered:[{internal_id:'new'}]}):clone(e.prot);
 const first=c.loadClientAccessControl();const background=c.loadClientAccessControl({automatic:true});assert.equal(first,background);
 const explicit=c.loadClientAccessControl({quiet:true});assert.equal(count,1);old.resolve({registered:[{internal_id:'old'}]});await explicit;
 assert.equal(count,2);assert.equal(e.renders.length,1);assert.equal(c.clientAccessState.registered[0].internal_id,'new');
});
test('离开页面停止刷新并忽略迟到响应；切回重新更新',async()=>{
 const e=environment(),c=e.c,d=deferred();c.respond=url=>url.includes('/client-access/status')?d.promise:clone(e.prot);
 const loading=c.loadClientAccessControl();c.switchPage('overview');d.resolve({registered:[{internal_id:'late'}]});await loading;
 assert.equal(e.renders.length,0);assert.ok(e.aborts.includes('client-access-refresh'));assert.equal([...e.timers.values()].filter(r=>r.ms===5000).length,0);
 c.activePage='access';c.pageNavigationSequence++;c.respond=url=>url.includes('/client-access/status')?{registered:[{internal_id:'fresh'}]}:clone(e.prot);
 e.event('visibilitychange');await flush();assert.equal(c.clientAccessState.registered[0].internal_id,'fresh');
});
test('隐藏页暂停，显示时立即读取；旧失败不会污染切回后的状态',async()=>{
 const e=environment(),c=e.c,d=deferred();c.respond=url=>url.includes('/client-access/status')?d.promise:clone(e.prot);
 const loading=c.loadClientAccessControl();c.document.hidden=true;e.event('visibilitychange');d.reject(Error('old failure'));await loading;
 assert.equal(e.node('clientAccessRefreshStatus').hidden,false); // untouched blank fixture is not a generated error notice
 assert.equal(e.node('clientAccessRefreshStatus').innerHTML,'');
 c.document.hidden=false;c.respond=url=>url.includes('/client-access/status')?{registered:[]}:clone(e.prot);e.event('visibilitychange');await flush();
 assert.equal(e.node('clientAccessRefreshStatus').hidden,true);
});
test('编辑和弹窗期间不请求/不替换；请求途中开始编辑也不覆盖',async()=>{
 const e=environment(),c=e.c;await c.loadClientAccessControl();const editor=e.node('clientScopeValue');editor.inRoot=true;editor.matches=()=>true;editor.value='pending selector';c.document.activeElement=editor;
 const before=e.calls.length;await c.loadClientAccessControl({automatic:true});assert.equal(e.calls.length,before);
 c.document.activeElement=null;c.modalOpen=true;await c.loadClientAccessControl({automatic:true});assert.equal(e.calls.length,before);
 c.modalOpen=false;const d=deferred();c.respond=url=>url.includes('/client-access/status')?d.promise:clone(e.prot);const request=c.loadClientAccessControl({automatic:true});
 e.event('input',editor);d.resolve({registered:[{internal_id:'during-edit'}]});await request;assert.equal(e.renders.length,1);assert.equal(editor.value,'pending selector');
 c.respond=url=>url.includes('/client-access/status')?{registered:[{internal_id:'after-edit'}]}:clone(e.prot);e.event('focusout',editor);await e.timer(0);await flush();assert.equal(c.clientAccessState.registered[0].internal_id,'after-edit');
});
test('刷新保留范围草稿和展开面板；弹窗关闭后刷新',async()=>{
 const e=environment(),c=e.c;await c.loadClientAccessControl();e.node('clientScopeKind').value='project';e.node('clientScopeValue').value='draft selector';e.node('detail').open=true;
 c.status={registered:[{internal_id:'changed'}]};await e.timer();assert.equal(e.node('clientScopeKind').value,'project');assert.equal(e.node('clientScopeValue').value,'draft selector');assert.equal(e.node('detail').open,true);
 c.modalOpen=true;c.status={registered:[{internal_id:'after-modal'}]};await e.timer();assert.equal(c.clientAccessState.registered[0].internal_id,'changed');
 c.modalOpen=false;e.event('close',e.node('modal'));await e.timer(0);await flush();assert.equal(c.clientAccessState.registered[0].internal_id,'after-modal');
});
test('失联保留旧记录并显示重试，恢复清除；权限/确认请求失败不静默清空',async()=>{
 const e=environment(),c=e.c;c.status={registered:[{internal_id:'kept'}]};await c.loadClientAccessControl();const stable=e.renders.length;
 c.respond=()=>Promise.reject(Error('synthetic unavailable'));await e.timer();assert.equal(e.renders.length,stable);assert.equal(c.clientAccessState.registered[0].internal_id,'kept');assert.match(e.node('clientAccessRefreshStatus').innerHTML,/状态未更新/);
 c.respond=url=>url.includes('/client-access/status')?clone(c.status):clone(e.prot);await e.node('retryClientAccess').onclick();assert.equal(e.node('clientAccessRefreshStatus').hidden,true);
 c.protection.owner_session_active=true;c.respond=url=>url.includes('/confirmations')?Promise.reject(Error('synthetic confirmations failure')):url.includes('/client-access/status')?clone(c.status):clone(c.protection);
 await c.loadClientAccessControl();assert.match(e.node('clientAccessRefreshStatus').innerHTML,/状态未更新/);assert.equal(c.clientAccessState.registered[0].internal_id,'kept');
});
test('用户操作期间旧快照失效，写入完成后读取新状态',async()=>{
 const e=environment(),c=e.c,d=deferred();let count=0;c.respond=url=>url.includes('/client-access/status')?(++count===1?d.promise:{registered:[{internal_id:'after-save'}]}):clone(e.prot);
 const reading=c.loadClientAccessControl({automatic:true});await c.clientAccessPost('/synthetic-fixture',{});const fresh=c.loadClientAccessControl();d.resolve({registered:[{internal_id:'before-save'}]});await fresh;
 assert.equal(e.renders.length,1);assert.equal(c.clientAccessState.registered[0].internal_id,'after-save');
});
test('等待接入按钮使用共享刷新结果，即使按钮忙碌也不误读旧状态',async()=>{
 const e=environment(),c=e.c,b=e.node('sent');b.dataset={sentBranded:'codex'};b.closest=()=>({querySelector:()=>e.node('feedback')});c.$$=()=>[b];
 c.status={registered:[{client_type:'codex',read_verification_current:true,configuration_current:true,last_read_at:'synthetic-time'}]};c.clientAccessState={registered:[]};c.sentBusy=true;
 vm.runInContext(sent,c);await b.onclick();assert.equal(c.sessionStorage.getItem('jiedu.connect.sent.codex'),undefined);assert.equal(b.disabled,false);
});
test('编辑期间显式更新只读证据，不重建控件或丢掉已保存权限',async()=>{
 const e=environment(),c=e.c;c.status={registered:[{internal_id:'client',policies:[{decision:'deny'}]}]};await c.loadClientAccessControl();
 const editor=e.node('clientScopeValue');editor.inRoot=true;editor.matches=()=>true;editor.value='draft';c.document.activeElement=editor;
 c.status={registered:[{internal_id:'client',policies:[{decision:'allow'}]}]};await c.loadClientAccessControl({quiet:true});
 assert.equal(e.renders.length,1);assert.equal(editor.value,'draft');assert.equal(c.clientAccessState.registered[0].policies[0].decision,'allow');
 const a={className:'same',querySelector:()=>({textContent:'配置与验证'})},b={className:'same',querySelector:()=>({textContent:'使用说明'})};assert.notEqual(c.clientAccessViewKey(a),c.clientAccessViewKey(b));
});
(async()=>{const results=[];for(const [name,fn] of tests){try{await fn();results.push({name,status:'PASS'});console.log('PASS '+name)}catch(error){console.error('FAIL '+name);throw error}}console.log(`Passed ${results.length} behavioral frontend tests`);})().catch(error=>{console.error(error);process.exitCode=1});
