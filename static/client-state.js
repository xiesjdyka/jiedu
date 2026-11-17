/* 界渡 · AI 客户端连接状态的唯一来源（第 39 轮）
 * 首页双盘、设置页胶囊标签、「连接 AI」页客户端卡片都从这里取状态，判定规则只写这一份。
 * 数据：/api/client-access/status（「连接 AI」页同一个接口）+ /api/local-sources/status（只用来区分「本机已发现」）。
 * 任何地方读到这两个接口（「连接 AI」页 5 秒轮询、首页刷新、这里自己的轮询），都会在同一刻广播 jiedu:ai-state，
 * 订阅方立即重绘，所以两边不会出现一边新一边旧。 */
(function(){
  'use strict';
  const NAMES={codex:'Codex',hermes:'Hermes',dsh:'DSH',claude:'Claude',cursor:'Cursor',workbuddy:'WorkBuddy',opencode:'OpenCode',zcode:'ZCode',kimi:'Kimi Work'};
  const ORDER=Object.keys(NAMES);
  const BAD_CONFIG=['missing','mismatch','invalid','unreadable','disabled'];
  /* 与「连接 AI」页完全相同的「最近读取成功」判定；app.js 的 clientRecentRead 也改为调用这里 */
  function recentRead(c){return !!c&&c.status!=='revoked'&&c.read_verification_current===true&&c.configuration_current!==false&&c.configuration_present!==false&&c.installation_state!=='absent'&&!BAD_CONFIG.includes(c.configuration_state)}
  function needsRepair(c){return c.installation_state==='absent'||c.configuration_present===false||c.configuration_current===false||BAD_CONFIG.includes(c.configuration_state)||c.status==='configuration_failed'}
  function derive(status,sources){
    const hidden=new Set(status?.hidden_client_types||[]);
    const reg=(status?.registered||[]).filter(r=>r.status!=='revoked');
    const legacy=(status?.legacy||[]).filter(r=>r.configured);
    const src=new Map((sources?.sources||[]).map(x=>[x.id||x.source_id,x]));
    return ORDER.filter(k=>!hidden.has(k)).map((key,i)=>{
      const client=reg.find(r=>r.client_type===key),leg=legacy.find(r=>r.claimed_id===key||r.client_type===key),found=!!src.get(key)?.detected;
      let state,hint;
      if(client&&recentRead(client)){state='ok';hint='已经能读你的知识库'}
      else if(client&&needsRepair(client)){state='warn';hint='连接需要处理，重新连接一下就好'}
      else if(client){state='idle';hint='已配置，等待第一次读取验证'}
      else if(leg){state='warn';hint='发现旧连接，需要重新接入'}
      else{state=found?'idle':'off';hint=found?'电脑上有这个 AI，还没连接':'还没在这台电脑上发现它'}
      return {key,label:NAMES[key],state,hint,order:i};
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
  /* r40：请求按发出顺序编号，只采用比已采用的更新的结果；慢回来的旧结果直接丢弃，不会把新状态冲回去 */
  let reqSeq=0;const applied={};
  function accept(pt,seq,d){if(seq<=(applied[pt]||0))return false;applied[pt]=seq;if(pt==='/api/client-access/status')status=d;else sources=d;return true}
  /* r41：本机服务状态也从这里出——右上角「界渡已运行」和故障排查页的本机服务卡用同一份 /api/health 结果、同一句文案 */
  let health=null,healthAt=0,healthSeq=0;
  function serviceState(h){if(!h)return {state:'warn',text:'正在连接',note:'正在确认本机服务'};
    if(h.status==='down')return {state:'down',text:'连接中断',note:'连续两次连不上本机服务'};
    if(h.status==='ready')return {state:'ok',text:'界渡已运行',note:h.workspace_ready===false?'服务在运行，工作区还没加载':'本机服务和工作区都正常'};
    return {state:'warn',text:'需要检查',note:h.message||'本机服务有部分功能没有就绪'}}
  let healthFails=0;
  /* 与右上角同一条规则：连续失败 2 次才算中断，一次偶发失败保持原状态 */
  function setHealth(seq,d,err){if(seq<healthSeq)return;healthSeq=seq;if(err){if(++healthFails<2)return;health={status:'down'}}else{healthFails=0;health=d}healthAt=Date.now();window.dispatchEvent(new CustomEvent('jiedu:health',{detail:{health,at:healthAt}}))}
  function publish(){
    if(!status)return;
    const next=derive(status,sources),s=JSON.stringify(next.map(a=>[a.key,a.state]));
    list=next;stamp=Date.now();window.dispatchEvent(new CustomEvent('jiedu:ai-data',{detail:{at:stamp}}));
    if(s!==sig){sig=s;window.dispatchEvent(new CustomEvent('jiedu:ai-state',{detail:{list:next,at:stamp}}))}
  }
  const path=u=>{try{return new URL(u,location.href).pathname}catch(e){return ''}};
  const raw=window.fetch.bind(window);
  window.fetch=function(input,init){
    const p=raw(input,init),url=typeof input==='string'?input:input instanceof URL?input.href:input?.url||'',pt=path(url),m=String(init?.method||(input instanceof Request?input.method:'GET')).toUpperCase();
    if(m==='GET'&&pt==='/api/health'){const seq=++reqSeq;p.then(r=>{if(!r.ok)return setHealth(seq,null,true);r.clone().json().then(d=>setHealth(seq,d)).catch(()=>{})}).catch(e=>{if(e?.name!=='AbortError')setHealth(seq,null,true)})}
    if(m==='GET'&&(pt==='/api/client-access/status'||pt==='/api/local-sources/status')){const seq=++reqSeq;
      p.then(r=>{if(!r.ok)return;r.clone().json().then(d=>{if(accept(pt,seq,d))publish()}).catch(()=>{})}).catch(()=>{})}
    return p;
  };
  /* r46：所有请求都带超时（modern-web-guidance · js：AbortSignal.timeout），挂住的请求到点自动放弃，
     不会让 inflight 永远卡住、状态从此停更。 */
  const T=ms=>({cache:'no-store',signal:AbortSignal.timeout(ms)});
  function checkHealth(){return fetch('/api/health?t='+Date.now(),T(8000)).then(r=>r.ok?r.json():null).catch(()=>null)}
  function refresh(){
    if(inflight)return inflight;
    const t=Date.now();
    /* 结果经上面的 fetch 包装按顺序采用；这里只等它们回来再统一广播 */
    inflight=Promise.all([fetch('/api/client-access/status?t='+t,T(10000)).then(r=>r.ok?r.json():null),fetch('/api/local-sources/status?t='+t,T(10000)).then(r=>r.ok?r.json():null)])
      .then(()=>new Promise(r=>setTimeout(r,0))).then(()=>{publish();return list}).catch(()=>list).finally(()=>{inflight=null});
    return inflight;
  }
  /* 首页 / 设置页可见时每 4 秒核对一次；「连接 AI」页可见时由它自己的 5 秒轮询带动（同一个接口，经上面的 fetch 广播） */
  setInterval(()=>{if(document.hidden)return;const a=document.querySelector('.page.active')?.id;if(a==='overview'||a==='protection'||a==='troubleshooting')refresh()},4000);
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh()});
  /* 故障排查「重新检查」：服务健康 + 连接状态 + 本机来源一起拉一遍 */
  function refreshAll(){return Promise.all([checkHealth(),refresh()]).then(()=>new Promise(r=>setTimeout(r,0))).then(()=>list)}
  window.jieduAiState={names:NAMES,order:ORDER,recentRead,derive,refresh,refreshAll,checkHealth,serviceState,get health(){return health},get healthAt(){return healthAt},get status(){return status},get sources(){return sources},homePrefs,homeSelection,setHomePrefs,get list(){return list},get ready(){return !!status},get at(){return stamp},of:k=>list.find(a=>a.key===k)};
})();
