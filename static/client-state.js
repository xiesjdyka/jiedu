/* 界渡 · AI 客户端连接状态的唯一来源（第 39 轮）
 * 首页双盘、设置页胶囊标签、「连接 AI」页客户端卡片都从这里取状态，判定规则只写这一份。
 * 数据：/api/client-access/status（「连接 AI」页同一个接口）+ /api/local-sources/status（只用来区分「本机已发现」）。
 * 任何地方读到这两个接口（「连接 AI」页 5 秒轮询、首页刷新、这里自己的轮询），都会在同一刻广播 jiedu:ai-state，
 * 订阅方立即重绘，所以两边不会出现一边新一边旧。 */
(function(){
  'use strict';
  const NAMES={antigravity:'Antigravity',codex:'Codex',hermes:'Hermes',dsh:'DSH',claude:'Claude',cursor:'Cursor',workbuddy:'WorkBuddy',opencode:'OpenCode',zcode:'ZCode',kimi:'Kimi Work'};
  const ORDER=Object.keys(NAMES);
  const CONFIG_LABEL={bundle_prepared:'扩展包已准备',disabled:'配置已停用',unreadable:'配置无法核对',invalid:'配置格式需检查',missing:'配置未检出',mismatch:'配置不匹配'};
  const REPAIR_STATES=new Set(['disabled','unreadable','invalid','mismatch']);
  function configurationLabel(client){
    return CONFIG_LABEL[client.configuration_state]||(client.configuration_present===false?'配置未检出':
      client.configuration_current===true?'配置有效':client.configuration_current===false?'配置需检查':'配置待核对');
  }
  function connection(client,legacy,source){
    const result=(state,label,hint)=>({state,label,hint});
    if(client?.status==='revoked')return result('off','已撤销','该连接已撤销');
    if(client?.installation_state==='absent')return result('off','未接入','本机没有找到程序');
    if(!client)return legacy?.configured?result('warn','需处理','发现旧连接，需要重新接入'):
      result('off','未接入',source?.detected?'本机已发现，还没有配置':'本机没有发现');
    const config=configurationLabel(client);
    if(client.client_type==='antigravity'&&client.configuration_current===true&&client.connection_read_verified===true)return result('ok','已连接','已通过当前配置读取验证');
    if(client.configuration_state==='bundle_prepared')return result('idle',config,'在客户端安装并启用扩展后，请实际读取一次');
    if(client.configuration_present===false||client.configuration_state==='missing')return result('off','未接入',config);
    if(REPAIR_STATES.has(client.configuration_state)||client.status==='configuration_failed'||
        (client.configuration_current===false&&client.configuration_present===true))return result('warn','需处理',config);
    if(client.read_verification_current===true&&client.configuration_current!==false)return result('ok','最近读取成功','当前权限下已验证实际读取');
    return result('idle',config,client.configuration_current===true?'已配置，等待第一次读取验证':'等待核对连接配置');
  }
  function recentRead(client){return client?.read_verification_current===true&&connection(client).state==='ok'}
  function derive(status,sources){
    const hidden=new Set(status?.hidden_client_types||[]);
    const reg=(status?.registered||[]).filter(r=>r.status!=='revoked');
    const legacy=(status?.legacy||[]).filter(r=>r.configured);
    const src=new Map((sources?.sources||[]).map(x=>[x.id||x.source_id,x]));
    return ORDER.filter(k=>!hidden.has(k)).map((key,i)=>{
      const client=reg.find(r=>r.client_type===key),leg=legacy.find(r=>r.claimed_id===key||r.client_type===key),found=!!src.get(key)?.detected;
      const {state,label:statusLabel,hint}=connection(client,leg,{detected:found});
      return {key,label:NAMES[key],state,statusLabel,hint,order:i};
    });
  }
  /* 首页外部智能域展示规则（首页与设置页共用）：上限 3–8 / 全部（默认 5）；可指定具体客户端，未指定=自动 */
  /* r40：去掉数量上限。自动模式按状态取前 5 个；手动模式就显示用户点选的那几个，其余全部合并 */
  const RANK={ok:0,warn:1,idle:2,off:3},AUTO_COUNT=5,PICK_KEY='jiedu.home.aiPick';
  const ls=k=>{try{return localStorage.getItem(k)}catch(e){return null}};
  function homePrefs(){let pick=null;try{const p=JSON.parse(ls(PICK_KEY)||'null');if(Array.isArray(p)&&p.length)pick=p.filter(k=>NAMES[k])}catch(e){}return {pick:pick&&pick.length?pick:null,auto:AUTO_COUNT}}
  function rankSort(l){return [...l].sort((a,b)=>RANK[a.state]-RANK[b.state]||a.order-b.order)}
  function homeSelection(l){const {pick}=homePrefs(),s=rankSort(l||list),shown=pick?s.filter(a=>pick.includes(a.key)):s.slice(0,AUTO_COUNT);return {shown,rest:s.filter(a=>!shown.includes(a)),pick}}
  function setHomePrefs({pick}={}){try{if(pick!==undefined){if(pick===null)localStorage.removeItem(PICK_KEY);else localStorage.setItem(PICK_KEY,JSON.stringify(pick))}}catch(e){}window.dispatchEvent(new CustomEvent('jiedu:home-ai-show'))}
  let status=null,sources=null,list=[],sig='',stamp=0,inflight=null;
  function signature(value){
    return JSON.stringify(value,(_,item)=>{
      if(!item||typeof item!=='object'||Array.isArray(item))return item;
      return Object.fromEntries(Object.keys(item).sort().map(key=>[key,item[key]]));
    });
  }
  /* r40：请求按发出顺序编号，只采用比已采用的更新的结果；慢回来的旧结果直接丢弃，不会把新状态冲回去 */
  let reqSeq=0;const applied={};
  function accept(pt,seq,d){
    if(seq<=(applied[pt]||0))return;
    applied[pt]=seq;stamp=Date.now();
    if(pt==='/api/client-access/status')status=d;else sources=d;
    publish();
  }
  /* r41：本机服务状态也从这里出——右上角「界渡已运行」和故障排查页的本机服务卡用同一份 /api/health 结果、同一句文案 */
  let health=null,healthAt=0,healthSeq=0,healthSignature='';
  function serviceState(h){if(!h)return {state:'warn',text:'正在连接',note:'正在确认本机服务'};
    if(h.status==='down')return {state:'down',text:'连接中断',note:'连续两次连不上本机服务'};
    if(h.status==='ready')return {state:'ok',text:'界渡已运行',note:h.workspace_ready===false?'服务在运行，工作区还没加载':'本机服务和工作区都正常'};
    return {state:'warn',text:'需要检查',note:h.message||'本机服务有部分功能没有就绪'}}
  let healthFails=0;
  /* 与右上角同一条规则：连续失败 2 次才算中断，一次偶发失败保持原状态 */
  function setHealth(seq,d,err){
    if(seq<healthSeq)return;
    healthSeq=seq;
    if(err){if(++healthFails<2)return;health={status:'down'}}else{healthFails=0;health=d}
    healthAt=Date.now();
    const next=signature([serviceState(health),health?.workspace_root,health?.mcp,
      health?.vector_model?.actual_vector_recall_ready,health?.vector_model?.installation_state]);
    if(next!==healthSignature){
      healthSignature=next;
      window.dispatchEvent(new CustomEvent('jiedu:health',{detail:{health,at:healthAt}}));
    }
  }
  function publish(){
    if(!status)return;
    const next=derive(status,sources),s=signature(next);
    const detail=signature([
      (status.registered||[]).map(row=>[row.internal_id,row.client_type,row.status,row.last_read_at,row.configuration_state,row.configuration_current,row.installation_state]),
      (sources?.sources||[]).map(row=>[row.id||row.source_id,row.detected,row.readable,row.last_error,
        row.live?.status,row.live?.saved_messages,row.live?.last_message_at,row.history?.status,row.source_issues?.length])
    ]);
    if(s!==sig){sig=s;list=next;window.dispatchEvent(new CustomEvent('jiedu:ai-state',{detail:{list,at:stamp}}))}
    if(detail!==detailSignature){detailSignature=detail;window.dispatchEvent(new CustomEvent('jiedu:ai-data',{detail:{at:stamp}}))}
  }
  let detailSignature='';
  const path=u=>{try{return new URL(u,location.href).pathname}catch(e){return ''}};
  const raw=window.fetch.bind(window);
  window.fetch=function(input,init){
    const p=raw(input,init),url=typeof input==='string'?input:input instanceof URL?input.href:input?.url||'',pt=path(url),m=String(init?.method||(input instanceof Request?input.method:'GET')).toUpperCase();
    const isHealth=pt==='/api/health';
    if(m!=='GET'||(!isHealth&&pt!=='/api/client-access/status'&&pt!=='/api/local-sources/status'))return p;
    const seq=++reqSeq;
    return p.then(async response=>{
      if(response.ok){
        const data=await response.clone().json().catch(()=>null);
        if(data){if(isHealth)setHealth(seq,data);else accept(pt,seq,data)}
      }else if(isHealth)setHealth(seq,null,true);
      return response;
    },error=>{
      if(isHealth&&error?.name!=='AbortError')setHealth(seq,null,true);
      throw error;
    });
  };
  /* r46：所有请求都带超时（modern-web-guidance · js：AbortSignal.timeout），挂住的请求到点自动放弃，
     不会让 inflight 永远卡住、状态从此停更。 */
  const T=ms=>({cache:'no-store',signal:AbortSignal.timeout(ms)});
  function checkHealth(){return fetch('/api/health?t='+Date.now(),T(8000)).then(r=>r.ok?r.json():null).catch(()=>null)}
  function refresh(){
    if(inflight)return inflight;
    const t=Date.now();
    /* 请求完成时，上面的 fetch 包装已采用最新状态。 */
    inflight=Promise.allSettled([fetch('/api/client-access/status?t='+t,T(10000)),fetch('/api/local-sources/status?t='+t,T(10000))])
      .then(()=>list).finally(()=>{inflight=null});
    return inflight;
  }
  /* 首页 / 设置页可见时每 4 秒核对一次；「连接 AI」页可见时由它自己的 5 秒轮询带动（同一个接口，经上面的 fetch 广播） */
  setInterval(()=>{if(document.hidden)return;const a=document.querySelector('.page.active')?.id;if(a==='overview'||a==='protection'||a==='troubleshooting')refresh()},4000);
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh()});
  /* 故障排查「重新检查」：服务健康 + 连接状态 + 本机来源一起拉一遍 */
  function refreshAll(){return Promise.all([checkHealth(),refresh()]).then(()=>list)}
  window.jieduAiState={names:NAMES,order:ORDER,connection,configurationLabel,signature,recentRead,derive,refresh,refreshAll,checkHealth,serviceState,get health(){return health},get healthAt(){return healthAt},get status(){return status},get sources(){return sources},homePrefs,homeSelection,setHomePrefs,get list(){return list},get ready(){return !!status},get at(){return stamp},of:k=>list.find(a=>a.key===k)};
})();
