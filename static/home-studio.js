/* 界渡首页 · 浅色工作台（纸墨 / 银白 / 晨雾）
 * 交互内核：精密刻度表盘。左半环＝你的资料（按多少切分），右半环＝电脑上的 AI（按连接状态上色）。
 * 鼠标移到分段或右侧列表 → 中心数字和指针联动；点中心或 Ctrl+K → 搜索；点分段 → 直达对应分类。
 * 只读真实接口，不改后端逻辑。 */
(()=>{
'use strict';
/* ---------- 外观：浅色（晨雾）/ 深色 / 定时深色 ---------- */
const MODE_KEY='jiedu.themeMode',SCHED_KEY='jiedu.darkSchedule';
const root=document.documentElement;
const ls={get(k){try{return localStorage.getItem(k)}catch(_){return null}},set(k,v){try{localStorage.setItem(k,v)}catch(_){}}};
const toMin=t=>{const [h,m]=String(t).split(':').map(Number);return h*60+m};
function readMode(){const m=ls.get(MODE_KEY);return ['light','dark','schedule'].includes(m)?m:'light'}
function readSched(){try{const s=JSON.parse(ls.get(SCHED_KEY)||'null');if(s&&/^\d\d:\d\d$/.test(s.from)&&/^\d\d:\d\d$/.test(s.to))return s}catch(_){}return {from:'19:00',to:'07:00'}}
function inWindow(s,d=new Date()){const now=d.getHours()*60+d.getMinutes(),f=toMin(s.from),t=toMin(s.to);if(f===t)return false;return f<t?now>=f&&now<t:now>=f||now<t}
function resolveTheme(){const m=readMode();return m==='dark'?'dark':m==='schedule'&&inWindow(readSched())?'dark':'mist'}
/* r25：主题切换用整屏快照过渡——侧栏、背景、卡片、圆盘在同一帧里一起变，从点击处以圆形柔光晕开 */
let themePt=null;addEventListener('pointerdown',e=>{themePt=[e.clientX,e.clientY]},true);
function applyTheme(animate){const next=resolveTheme();if(root.dataset.theme===next)return;
  const reduce=matchMedia('(prefers-reduced-motion: reduce)').matches;
  if(animate&&document.startViewTransition&&!reduce){
    const [x,y]=themePt||[innerWidth-120,32],r=Math.hypot(Math.max(x,innerWidth-x),Math.max(y,innerHeight-y));
    root.classList.add('theme-vt');
    let applied=false;const vt=document.startViewTransition(()=>{applied=true;root.dataset.theme=next});
    setTimeout(()=>{if(!applied){try{vt.skipTransition()}catch(e){}root.dataset.theme=next;void root.offsetWidth;requestAnimationFrame(()=>requestAnimationFrame(()=>root.classList.remove('theme-vt')))}},400); /* r34：先在暂停过渡的状态下把新颜色算完，再撤掉暂停，侧栏不再慢半拍 */
    vt.ready.then(()=>{root.animate({clipPath:[`circle(0px at ${x}px ${y}px)`,`circle(${r}px at ${x}px ${y}px)`]},{duration:620,easing:'cubic-bezier(.65,0,.25,1)',pseudoElement:'::view-transition-new(root)'})}).catch(()=>{});
    vt.finished.finally(()=>root.classList.remove('theme-vt'));return}
  if(animate){root.classList.add('theme-switching');setTimeout(()=>root.classList.remove('theme-switching'),500)}root.dataset.theme=next}
applyTheme(false);
const reduceMotion=matchMedia('(prefers-reduced-motion: reduce)');
const $=(s,r=document)=>r.querySelector(s),$$=(s,r=document)=>[...r.querySelectorAll(s)];
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt=n=>n!=null&&Number.isFinite(Number(n))?Number(n).toLocaleString('zh-CN'):'—';
const fullDate=v=>{const d=v?new Date(v):null;if(!d||isNaN(d))return '';return `${d.getFullYear()}年${d.getMonth()+1}月${d.getDate()}日 ${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`};

/* ---------- 人话名称 ---------- */
const FOLDER_NAMES={
  '00_系统':['核心背景','你的长期画像和协作规则'],
  '01_当前状态':['近况','现在在忙什么、先做什么'],
  '02_项目':['项目','每个项目的目标和进度'],
  '03_知识':['已确认的知识','确认过、能长期用的结论'],
  '90_原始档案':['聊天原文','和 AI 聊过的原话与附件'],
  '99_收件箱':['待你确认','刚收进来、还没整理的资料']
};
const AI_NAMES={codex:'Codex',hermes:'Hermes',dsh:'DSH',claude:'Claude',cursor:'Cursor',workbuddy:'WorkBuddy',opencode:'OpenCode',zcode:'ZCode',kimi:'Kimi Work',doubao:'豆包'};
/* r46：客户端名单、展示规则统一在 client-state.js；这里只留首页「指定展示」的存储键，并清掉已废弃的数量上限键 */
const AI_PICK_KEY='jiedu.home.aiPick';
try{localStorage.removeItem('jiedu.home.aiShow')}catch(e){}
function moreNode(rest){const mix={ok:0,warn:0,un:0};rest.forEach(r=>{mix[r.state==='ok'?'ok':r.state==='warn'?'warn':'un']++});
  const tone=mix.warn?'warn':mix.ok?'ok':rest.some(r=>r.state==='idle')?'idle':'off';
  const parts=[mix.ok&&`${mix.ok} 已连接`,mix.warn&&`${mix.warn} 需重连`,mix.un&&`${mix.un} 未连接`].filter(Boolean);
  const group=k=>rest.filter(r=>(r.state==='ok'?'ok':r.state==='warn'?'warn':'un')===k).map(r=>r.label).join('、');
  const hint=[mix.ok&&`已连接：${group('ok')}`,mix.warn&&`需重连：${group('warn')}`,mix.un&&`未连接：${group('un')}`].filter(Boolean).join('；');
  return {key:'__more',label:`其余 ${rest.length} 个`,hint,state:'more',tone,mix,sub:mix.ok||mix.warn?parts.join(' · '):'都还没连接',kind:'ai',count:null,more:true}}
const aiStateText=s=>s==='ok'?'已连接':s==='warn'?'需要重新连接':s==='more'?'等待接入':s==='off'?'等待接入':'还没连接';

/* ---------- 数据 ---------- */
const getJSON=url=>fetch(url+(url.includes('?')?'&':'?')+'t='+Date.now(),{cache:'no-store',signal:AbortSignal.timeout(15000)}).then(r=>r.ok?r.json():null).catch(()=>null);
let model=null,lastSig='';
let lastRaw=null;
async function loadModel(){
  const [kb,ov,objects,,recent]=await Promise.all([getJSON('/api/knowledge-base'),getJSON('/api/overview'),getJSON('/api/knowledge-objects'),window.jieduAiState?.refresh(),getJSON('/api/knowledge-base/recent?limit=4&days=56')]);
  lastRaw={kb,ov,objects,recent};return buildModel(lastRaw);
}
/* r39：AI 状态只从 client-state.js 取（与「连接 AI」页同一接口、同一判定） */
function buildModel({kb,ov,objects,recent}){
  const idByPath=new Map((objects?.items||[]).map(x=>[x.logical_path,x.entry_id]));
  const cats=(kb?.structure||[]).filter(x=>x.kind==='folder'&&!/^[._]/.test(x.name)).map(x=>{const [label,hint]=FOLDER_NAMES[x.name]||[x.name.replace(/^\d+_/,''),'这个文件夹里的资料'];return {key:x.name,label,hint,count:Number(x.markdown_count)||0,kind:'cat'}});
  const allAis=(window.jieduAiState?.list||[]).map(a=>({key:a.key,label:AI_NAMES[a.key]||a.label,hint:a.hint,state:a.state,order:a.order,kind:'ai',count:null}));
  /* r39：展示哪些、几个——与设置页共用 client-state.js 的同一规则 */
  const sel=window.jieduAiState?.homeSelection(allAis)||{shown:allAis,rest:[]},shown=sel.shown,rest=sel.rest;
  /* r40：「其余 N 个」不再一律算未接入——按里面的真实状态统计，外圈细弧按比例分色，状态点取最需要注意的那一种 */
  const ais=rest.length?[...shown,moreNode(rest)]:shown;
  const detectedAi=allAis.filter(a=>a.state!=='off').length;
  const indexed=Boolean(kb?.indexed),chunks=Number(ov?.chunk_count)||0,files=Number(kb?.markdown_count)||0,vectorReady=Boolean(ov?.vector_query_ready||ov?.vector_index_ready);
  const archive=cats.find(c=>c.key==='90_原始档案'),okAi=allAis.filter(a=>a.state==='ok').length;
  return {
    connected:Boolean(kb?.connected),indexed,ais,cats,lastSync:ov?.last_sync||null,totalFiles:files,
    recent:recent&&recent.available?{items:(recent.items||[]).map(x=>({...x,id:idByPath.get(x.path)||null})),daily:recent.daily||[],start:recent.start}:null,
    core:indexed&&chunks?{value:chunks,unit:'段知识随时能搜到'}:{value:files,unit:'份资料已经收进来'},
    note:!kb?.connected?'还没连接知识库':!vectorReady?'语义搜索还没准备好，现在按关键词查找':'',
    metrics:[
      {k:'资料',v:Number(objects?.counts?.open??files)||0,u:'份',tip:'在「知识」里可以直接打开',go:()=>switchTo('files')},
      {k:'聊天原文',v:archive?archive.count:0,u:'段',tip:'和 AI 的原始对话，一字不改',go:()=>switchTo('archives')},
      {k:'已连接的 AI',v:okAi,u:`/ ${allAis.length} 个`,tip:`「连接 AI」里共 ${allAis.length} 个，这台电脑上发现了 ${detectedAi} 个`,go:()=>switchTo('access'),warn:detectedAi>0&&okAi===0},
      {k:'搜索',text:vectorReady?'语义 + 关键词':'关键词',tip:vectorReady?'能按意思搜，也能按字面搜':'语义搜索还没准备好',go:()=>switchTo('workspace')},
      ...(ov?.last_sync?[{k:'最近更新',text:fullDate(ov.last_sync),tip:'知识库最近一次整理入库的时间',go:()=>switchTo('workspace')}]:[])
    ],
    next:[
      ...(!kb?.connected?[{text:'先选一个文件夹当知识库',go:()=>switchTo('workspace')}]:[]),
      ...(kb?.connected&&!vectorReady?[{text:'准备语义搜索，搜得更懂意思',go:()=>switchTo('workspace')}]:[]),
      ...(detectedAi&&!okAi?[{text:allAis.some(a=>a.state==='warn')?'有 AI 连接失效了，重新连接':'连接一个 AI',go:()=>switchTo('access')}]:[])
    ]
  };
}

/* ---------- 导航 ---------- */
function switchTo(page){if(typeof window.switchPage==='function')window.switchPage(page)}
function waitFor(test,timeout=3000){return new Promise(resolve=>{const t0=Date.now();(function tick(){const v=test();if(v||Date.now()-t0>timeout)return resolve(v);setTimeout(tick,120)})()})}
async function gotoFolder(key){
  if(key==='90_原始档案')return switchTo('archives');
  switchTo('files');
  const short=key.replace(/^\d+_/,'');
  const title=await waitFor(()=>$$('#fileList .file-group-title b').find(b=>b.textContent.includes(key)||b.textContent.includes(short)));
  const group=title?.closest('.file-group');if(!group)return;
  group.scrollIntoView({block:'start',behavior:reduceMotion.matches?'auto':'smooth'});
  group.classList.add('studio-flash');setTimeout(()=>group.classList.remove('studio-flash'),1800);
}
function runSearch(q){
  if(typeof window.openKnowledgeSearch==='function')window.openKnowledgeSearch('formal');else switchTo('search');
  setTimeout(()=>{const input=$('#sharedQueryInput');if(!input)return;input.value=q;input.dispatchEvent(new Event('input',{bubbles:true}));if(q&&typeof window.doSearch==='function')window.doSearch()},80);
}
const activate=n=>!n?null:n.kind==='cat'?gotoFolder(n.key):switchTo('access');

/* ---------- 表盘几何 ---------- */
const W=1000,H=600,CX=500,CY=300,R0=150,R1=194,GAP=1.6;
const rad=d=>d*Math.PI/180,f=v=>v.toFixed(1);
const polar=(r,deg)=>[CX+r*Math.cos(rad(deg)),CY+r*Math.sin(rad(deg))];
function arc(r0,r1,a0,a1){
  const [x0,y0]=polar(r1,a0),[x1,y1]=polar(r1,a1),[x2,y2]=polar(r0,a1),[x3,y3]=polar(r0,a0),large=a1-a0>180?1:0;
  return `M${f(x0)} ${f(y0)}A${r1} ${r1} 0 ${large} 1 ${f(x1)} ${f(y1)}L${f(x2)} ${f(y2)}A${r0} ${r0} 0 ${large} 0 ${f(x3)} ${f(y3)}Z`;
}
const nodeAttrs=(n,i)=>`class="hub-node ${n.kind} ${n.state||''} s${i%6}" data-kind="${n.kind}" data-index="${i}" tabindex="0" role="button" aria-label="${esc(n.label)}${n.count!=null?'，'+n.count+' 份':''}。${esc(n.hint)}"`;
let geometry=new Map();
const tintOf=(n,i)=>n.kind==='cat'?`var(--tint-${i%6})`:n.more?'var(--ai-more)':(n.tone||n.state)==='ok'?'var(--ai-ok)':(n.tone||n.state)==='warn'?'var(--ai-warn)':'var(--ai-silver)';
function arcLine(r,a0,a1){const [x0,y0]=polar(r,a0),[x1,y1]=polar(r,a1);return `M${f(x0)} ${f(y0)}A${r} ${r} 0 ${a1-a0>180?1:0} 1 ${f(x1)} ${f(y1)}`}
/* r46：mixArcs（「其余」三色分段弧）在改为专属紫色后已不用，删除 */
function dial(m){
  geometry=new Map();
  const total=m.cats.reduce((s,c)=>s+Math.max(c.count,0),0)||1,span=156,start=102;
  let shares=m.cats.map(c=>Math.max(span*.075,span*c.count/total));const k=span/(shares.reduce((a,b)=>a+b,0)||1);shares=shares.map(s=>s*k);
  let a=start;const L=m.cats.map((n,i)=>{const a0=a+GAP/2,a1=a+shares[i]-GAP/2;a+=shares[i];return {n,i,a0,a1,mid:(a0+a1)/2}});
  const aSpan=Math.min(116,Math.max(36,m.ais.length*30)),each=aSpan/Math.max(1,m.ais.length);
  const Rr=m.ais.map((n,i)=>{const a0=-aSpan/2+i*each+GAP/2,a1=-aSpan/2+(i+1)*each-GAP/2;return {n,i,a0,a1,mid:(a0+a1)/2}});
  const column=(items,side)=>{const s=[...items].sort((p,q)=>Math.sin(rad(p.mid))-Math.sin(rad(q.mid))),step=s.length>1?Math.min(62,330/(s.length-1)):0,y0=CY-step*(s.length-1)/2;s.forEach((it,j)=>{it.ly=y0+j*step;it.step=step||62;it.lx=side<0?232:768;it.side=side})};
  column(L,-1);column(Rr,1);
  const seg=(it,ai)=>{const {n,i,a0,a1,mid,lx,ly,side}=it,[ex,ey]=polar(R1+8,mid),[cx,cy]=polar(R0-8,mid),bx=side<0?lx+44:lx-44,sx=lx+(side<0?14:-14);geometry.set(n,mid);
    const route=`M${f(sx)} ${f(ly)} L${f(bx)} ${f(ly)} L${f(ex)} ${f(ey)} L${f(cx)} ${f(cy)}`,inRoute=`M${f(lx+(side<0?14:-14))} ${f(ly)} L${f(bx)} ${f(ly)} L${f(ex)} ${f(ey)} L${f(cx)} ${f(cy)}`;
    return `<g ${nodeAttrs(n,i)} style="--tint:${tintOf(n,i)}"><path class="seg${ai?' ai':''}" d="${arc(R0,R1,a0,a1)}"/>${ai?`<path class="ai-dash" d="${arcLine((R0+R1)/2,a0+1.2,a1-1.2)}"/><path class="ai-rim" d="${arcLine(R1-.6,a0,a1)}"/>${n.more?`<path class="ai-energy more-arc" d="${arcLine(R1+6,a0+.6,a1-.6)}"/><path class="ai-flow more-arc" d="${arcLine(R1+6,a0+.6,a1-.6)}" pathLength="100"/>`:`<path class="ai-energy" d="${arcLine(R1+6,a0+.6,a1-.6)}"/><path class="ai-flow" d="${arcLine(R1+6,a0+.6,a1-.6)}" pathLength="100"/>`}`:''}<path class="pulse-path" d="${route}" pathLength="100"/><polyline class="leader" points="${f(ex)},${f(ey)} ${f(bx)},${f(ly)} ${f(lx+(side<0?14:-14))},${f(ly)}"/><polyline class="leader-hit" points="${f(ex)},${f(ey)} ${f(bx)},${f(ly)} ${f(lx+(side<0?14:-14))},${f(ly)}"/><path class="leader-glint" pathLength="100" d="${inRoute}"/>${ai?`<path class="ai-lead" pathLength="100" d="M${f(lx+(side<0?14:-14))} ${f(ly)} L${f(bx)} ${f(ly)} L${f(ex)} ${f(ey)}"/>`:''}<circle class="leader-dot" cx="${f(ex)}" cy="${f(ey)}" r="2.2"/><path class="hit" d="${arc(R0-2,R1+8,a0-GAP/2,a1+GAP/2)}"/>${(()=>{const sub=ai?(n.more?n.sub:aiStateText(n.state)):fmt(n.count)+' 份',w=Math.max(String(n.label).length*16.5,String(sub).length*13)+14;return `<rect class="lbl-hit" x="${f(side<0?lx-w+6:lx-6)}" y="${f(ly-20)}" width="${f(w)}" height="${f(Math.min(42,it.step-2))}" rx="8"/>`})()}<text class="lbl" x="${f(lx)}" y="${f(ly-3)}" text-anchor="${side<0?'end':'start'}">${esc(n.label)}</text><text class="num" x="${f(lx)}" y="${f(ly+16)}" text-anchor="${side<0?'end':'start'}">${ai?esc(n.more?n.sub:aiStateText(n.state)):fmt(n.count)+' 份'}</text></g>`};
  const ticks=Array.from({length:180},(_,i)=>{const d=i*2,long=i%15===0,mid=i%5===0,[x0,y0]=polar(long?206:mid?210:213,d),[x1,y1]=polar(218,d);return `<line class="tick${long?' long':mid?' mid':''}" x1="${f(x0)}" y1="${f(y0)}" x2="${f(x1)}" y2="${f(y1)}"/>`}).join('');
  const [mlx,mly]=polar(176,268),[mrx,mry]=polar(176,292);
  return `<svg viewBox="120 40 760 520" class="hub-svg" role="group" aria-label="知识表盘">
    <defs>
      <linearGradient id="aiOkGrad" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="var(--ai-ok)"/><stop offset="1" stop-color="var(--ai-ok-2)"/></linearGradient><linearGradient id="aiMoreGrad" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="var(--ai-more)"/><stop offset="1" stop-color="var(--ai-more-2)"/></linearGradient><radialGradient id="haloGrad"><stop offset=".55" stop-color="var(--glow)" stop-opacity="0"/><stop offset=".8" stop-color="var(--glow)" stop-opacity=".16"/><stop offset="1" stop-color="var(--glow)" stop-opacity="0"/></radialGradient></defs>
    <circle class="halo h1" cx="${CX}" cy="${CY}" r="250" fill="url(#haloGrad)"/>
    <circle class="halo h2" cx="${CX}" cy="${CY}" r="250" fill="url(#haloGrad)"/>
    <g class="spin ticks">${ticks}</g>
    <circle class="ring inner" cx="${CX}" cy="${CY}" r="${R0-10}"/>
    <g class="spin inner-dash"><circle class="ring dash" cx="${CX}" cy="${CY}" r="${R0-18}"/></g>
    <text class="dial-mark" x="${f(mlx)}" y="${f(mly+4)}" text-anchor="middle">私人</text><text class="dial-mark" x="${f(mrx)}" y="${f(mry+4)}" text-anchor="middle">外部</text>
    ${L.map(it=>seg(it,false)).join('')}${Rr.map(it=>seg(it,true)).join('')}
    <g class="needle" id="studioNeedle"><line x1="${CX+200}" y1="${CY}" x2="${CX+226}" y2="${CY}"/><path d="M${CX+228} ${CY-6} L${CX+218} ${CY} L${CX+228} ${CY+6}Z"/></g>
  </svg>`;
}

/* ---------- 图标（线性，自绘） ---------- */
const ICON={
  search:'<circle class="du" cx="10.5" cy="10.5" r="6"/><circle cx="10.5" cy="10.5" r="6"/><path d="M15 15l5 5"/>',
  import:'<path class="du" d="M4 14.5h4.2l1.3 2.2h5l1.3-2.2H20V18a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z"/><path d="M12 3.8v9.4M8.3 9.6 12 13.3l3.7-3.7"/><path d="M4 14.5h4.2l1.3 2.2h5l1.3-2.2H20V18a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z"/>',
  chat:'<path class="du" d="M5.5 5h13A2.5 2.5 0 0 1 21 7.5v6.5a2.5 2.5 0 0 1-2.5 2.5h-6l-4.5 3.5v-3.5h-2.5A2.5 2.5 0 0 1 3 14V7.5A2.5 2.5 0 0 1 5.5 5z"/><path d="M5.5 5h13A2.5 2.5 0 0 1 21 7.5v6.5a2.5 2.5 0 0 1-2.5 2.5h-6l-4.5 3.5v-3.5h-2.5A2.5 2.5 0 0 1 3 14V7.5A2.5 2.5 0 0 1 5.5 5z"/><path d="M8 10.8h8"/>',
  organize:'<path class="du" d="M12 3.5l8.5 4.5-8.5 4.5L3.5 8z"/><path d="M12 3.5l8.5 4.5-8.5 4.5L3.5 8z"/><path d="M3.5 12.2 12 16.7l8.5-4.5M3.5 16.3 12 20.8l8.5-4.5"/>',
  link:'<path d="M10 14a4 4 0 0 0 5.66 0l3-3a4 4 0 0 0-5.66-5.66l-1 1"/><path d="M14 10a4 4 0 0 0-5.66 0l-3 3a4 4 0 0 0 5.66 5.66l1-1"/>',
  doc:'<path class="du" d="M7 3.5h7l4.5 4.5v11.5A1.5 1.5 0 0 1 17 21H7a1.5 1.5 0 0 1-1.5-1.5v-14A1.5 1.5 0 0 1 7 3.5z"/><path d="M7 3.5h7l4.5 4.5v11.5A1.5 1.5 0 0 1 17 21H7a1.5 1.5 0 0 1-1.5-1.5v-14A1.5 1.5 0 0 1 7 3.5z"/><path d="M14 3.5V8h4.5M9 12.5h6M9 16h4"/>',
  help:'<circle class="du" cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="9"/><path d="M9.5 9.5a2.6 2.6 0 1 1 3.6 2.4c-.7.3-1.1.9-1.1 1.6v.4"/><circle cx="12" cy="17" r=".9" fill="currentColor" stroke="none"/>'
};
const icon=n=>`<svg class="ic" viewBox="0 0 24 24" aria-hidden="true">${ICON[n]}</svg>`;

/* ---------- 首页骨架：主副双盘控制台 ---------- */
let host,stage;
/* r43：首页问候——大字按时段问好，后面跟一句随机小话；每次进入首页重新抽，偶尔抽到隐藏彩蛋 */
const slotOf=h=>h<5||h>=23?'night':h<11?'morning':h<13?'noon':h<17?'afternoon':h<19?'dusk':'evening';
const SLOT_HELLO={night:'夜深了',morning:'早上好',noon:'中午好',afternoon:'下午好',dusk:'傍晚好',evening:'晚上好'};
const QUIPS={
  night:['星星替你看着资料，早点休息','灯还亮着，界渡也陪着你','夜里冒出来的点子，先存进来再睡','世界安静下来了，正好理一理思路','别熬太晚，明天的你会感谢现在的你','夜色正好，资料都已经安顿妥当','困了就去睡，想法明天还在这里','月亮也还没睡，陪你一会儿','深夜的思考特别清醒，记得存档','再看一份就睡，好吗','键盘声和星光，都很安静','今天的最后一份资料，交给我保管'],
  morning:['新的一天，从一份清单开始','咖啡还热着，资料已经就位','早上的灵感最值钱，记下来','今天想先整理哪一块？','阳光正好，知识库也醒了','昨晚收进来的资料，都在这里等你','早安，今天也慢慢来','先喝口水，再打开第一份资料','一日之计在于晨，一库之计在于整理','今天的待办，从最小的一件开始','早晨的脑子最清爽，适合啃难题','新的一天，知识库又多了一点可能'],
  noon:['吃饱了再继续，资料不会跑','午休前，翻翻上午的收获','上午辛苦了，歇口气吧','半天过去，进度又往前挪了一格','午饭吃了吗？别光顾着看资料','中午的阳光，适合晒晒灵感','歇一歇，下午的你会更有劲','趁午休，把上午的想法存下来','别跳过午饭，大脑也要补给'],
  afternoon:['来杯茶，慢慢把思路渡过去','下午的专注时间，交给你','困了就站起来走两步','卡住的时候，搜一搜以前的自己','进度不错，继续保持','下午三点，正是灵感打盹的时候','喝口水，眼睛离屏幕远一点','阳光斜斜的，思路也慢慢理顺','难的留给现在，简单的留给傍晚','小小进展，也值得记一笔','换个角度看看，答案也许就在旧资料里'],
  dusk:['夕阳正好，给今天收个尾','今天收进来的，明天都还在','下班前，把零散的想法存一存','一天快结束了，辛苦啦','晚霞来了，今天过得怎么样','收工前，给明天留一条线索','天色渐暗，资料都已归位','把今天的收获，轻轻放进知识库','回家路上的想法，也可以先记下来'],
  evening:['今天辛苦了，慢慢来','晚上的时间，留给想深一点的事','把今天的碎片，拼成明天的线索','夜色温柔，适合回顾一下','聊过的、想过的，都替你记着','吃过晚饭了吗？慢慢翻翻','晚上适合整理，不适合着急','灯光柔和，思路也会变柔和','今天学到的，明天就能用上','闲下来的时候，翻翻以前的自己','忙碌过去了，现在是你的时间']};
const WEEK_QUIPS={1:['新的一周，慢慢进入状态','周一不着急，先把清单理顺'],2:['周二了，节奏已经找回来了吧'],3:['周三，一周过半，给自己点个赞'],4:['周四了，离周末又近了一步'],5:['周五了，再坚持一下下','周五的收尾，留给下周一个好开头'],6:['周末也来看看？那就轻松翻翻','周六的阳光，适合慢慢整理'],0:['周日的时光，适合慢慢整理','明天又是新的一周，今天先放松']};
const EGG_QUIPS=['你发现了一句隐藏问候：今天也是被知识偏爱的一天','界渡小声说：你整理资料的样子很认真','据说连开七天界渡，资料会自己变整齐（并不会）','此岸是问题，彼岸是答案，界渡在中间','每一份资料，都是写给未来自己的信','今日运气值：满格',
  '恭喜抽中隐藏款问候，今天适合做一件小事','界渡悄悄告诉你：资料越整理，越像你自己','偶遇彩蛋：你的知识库正在发光','这句话出现的概率很低，所以你今天很幸运','渡口今日风平浪静，适合出发','好奇心是最好的索引','知识不会走丢，因为你把它们都带回了家','今天的灵感已到账，请注意查收','你已解锁隐藏台词：慢慢来，比较快','彩蛋达成：被界渡夸了一句','记性不好没关系，界渡替你记着','今天适合整理，也适合偷懒一下下','一份资料，一段旅程，一次渡河','今日份好运，已存入知识库'];
const EGG_RATE=.08;
let quip=null;
function pickQuip(){const d=new Date(),slot=slotOf(d.getHours()),wd=WEEK_QUIPS[d.getDay()];
  const pool=[...QUIPS[slot],...(wd&&slot!=='night'?wd:[])],egg=Math.random()<EGG_RATE,src=egg?EGG_QUIPS:pool;
  let last='';try{last=sessionStorage.getItem('jiedu.home.quip')||''}catch(e){}
  const opts=src.filter(t=>t!==last),text=opts[Math.floor(Math.random()*opts.length)]||src[0];
  try{sessionStorage.setItem('jiedu.home.quip',text)}catch(e){}
  quip={slot,text,egg}}
function greeting(){return SLOT_HELLO[slotOf(new Date().getHours())]}
function today(){const d=new Date(),w='日一二三四五六'[d.getDay()];return `${d.getFullYear()}年${d.getMonth()+1}月${d.getDate()}日 星期${w} ${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`}
function build(){
  const overview=$('#overview');if(!overview||$('.studio-home',overview))return;
  overview.classList.add('studio-on');
  host=document.createElement('section');host.className='studio-home twin enter';setTimeout(()=>host.classList.remove('enter'),1600);host.setAttribute('aria-label','首页');
  host.innerHTML=`
    <div class="sh-ambient" aria-hidden="true"><i class="amb-grid"></i><i class="amb-glow a"></i><i class="amb-glow b"></i></div>
    <header class="sh-top">
      <div class="sh-hello"><p class="sh-date" id="studioDate"></p><h2 id="studioGreeting"></h2><div class="sh-status" id="studioStatus"></div></div>
      <button type="button" class="sh-search" id="studioSearchTrigger">${icon('search')}<span class="txt">搜索资料，或跳转到某个页面</span><span class="keys"><kbd>Ctrl</kbd><kbd>K</kbd></span></button>
    </header>
    <div class="sh-console">
      <svg class="sh-links" id="studioLinks" aria-hidden="true"></svg><i class="sh-ruler" aria-hidden="true"></i>
      <div class="sh-dial" id="studioStage">
        <div class="dial-cap l"><i></i><b>私人知识域</b><small>你的资料 · 按多少切分</small></div>
        <div class="dial-cap r"><b>外部智能域</b><small>你在用的 AI · 按连接状态</small><i></i></div>
        <div class="studio-hub" id="studioHub"></div>
        <button type="button" class="studio-core" id="studioCore" aria-label="打开全部资料">
          <span class="core-plate" aria-hidden="true"></span>
          <span class="core-kicker" id="coreKicker">界渡</span>
          <span class="core-value" id="coreValue">—</span>
          <span class="core-unit" id="coreUnit"></span>
          <span class="core-hint" id="coreHint"></span>
        </button>
      </div>
      <div class="sh-bridge" aria-hidden="true"><i></i></div>
      <div class="sh-sub" id="studioSub" aria-label="知识动态">
        <div class="sub-cap"><span class="sub-live"></span><b>知识动态</b><small id="subSum"></small></div>
        <div class="sub-stage" id="subStage">
          <div class="sub-hub" id="subHub"></div>
          <div class="sub-core" id="subCore" role="button" tabindex="0" aria-live="polite" aria-label="打开当前这份资料"><span class="core-plate" aria-hidden="true"></span><span class="sub-kicker" id="subKicker"></span><span class="sub-title" id="subTitle"></span><span class="sub-meta" id="subMeta"></span></div>
        </div>
        <p class="sub-snippet" id="subSnippet"></p>
        <ol class="sub-recent" id="subRecent" aria-label="最近收录"></ol>
      </div>
    </div>
    <div class="sh-pulse" id="studioPulse" aria-label="界渡实时脉搏">
      <div class="pulse-live"><span class="beat" aria-hidden="true"><i></i><i></i></span><span class="pl-txt"><b id="pulseState">界渡运行中</b><small id="pulseSub">正在读取…</small></span></div>
      <div class="pulse-wave" id="pulseWave" role="img"><div class="pw-stage" id="pulseStage"><svg class="pw-svg" id="pulseSvg" viewBox="0 0 1000 60" preserveAspectRatio="none" aria-hidden="true"></svg><i class="pw-cursor" id="pulseCursor" aria-hidden="true"></i><i class="pw-dot" id="pulseDot" aria-hidden="true"></i><span class="pw-tip" id="pulseTip" aria-hidden="true"></span></div><div class="pw-axis"><span id="pulseFrom"></span><span id="pulseRead"></span><span>今天</span></div></div>
      <div class="pulse-actions">
        <button type="button" class="pusher primary" data-studio-go="import">${icon('import')}<span>导入资料</span></button>
        <button type="button" class="pusher" data-studio-go="guide" title="使用指南" aria-label="使用指南">${icon('help')}<span>使用指南</span></button>
      </div>
    </div>
    <section class="sh-legacy" id="studioLegacy" hidden></section>`;
  overview.prepend(host);
  const legacy=$('#studioLegacy'),next=$('#homeNextState'),onboarding=$('#homeOnboarding');if(next){next.hidden=true;legacy.append(next)}if(onboarding)legacy.append(onboarding);
  stage=$('#studioStage');
  $('#studioCore').addEventListener('click',()=>{if(focused)return activate(focused);switchTo('files')});
  $('#studioSearchTrigger').addEventListener('click',()=>openPalette());
  host.addEventListener('click',e=>{const b=e.target.closest('[data-studio-go]');if(!b)return;const g=b.dataset.studioGo;
    if(g==='import'){switchTo('files');return setTimeout(()=>$('#openKnowledgeImport')?.click(),0)}
    if(g==='guide'){switchTo('butler');return setTimeout(()=>$('#butlerProductHelp')?.click(),0)}
    switchTo(g)});
  // 主盘：分类 / AI
  const hub=$('#studioHub'),pick=t=>t.closest('[data-kind][data-index]');
  /* r38：只认静态热区（.hit 扇形 + 文字框），色块缩放、指针、光晕都不参与命中，悬停不再来回闪 */
  const live=t=>t.closest('.hit,.lbl-hit')?pick(t):null;
  hub.addEventListener('click',e=>{const g=live(e.target);if(g)activate(nodeOf(g))});
  hub.addEventListener('keydown',e=>{if(e.key!=='Enter'&&e.key!==' ')return;const g=pick(e.target);if(g){e.preventDefault();activate(nodeOf(g))}});
  hub.addEventListener('pointerover',e=>{const g=live(e.target);focusNode(g?nodeOf(g):null);
    const lh=e.target.closest('.leader-hit');if(lh&&!g&&!reduceMotion.matches){const node=lh.closest('.hub-node');node.classList.remove('glint');void node.getBoundingClientRect();node.classList.add('glint');clearTimeout(node._gt);node._gt=setTimeout(()=>node.classList.remove('glint'),900)}});
  hub.addEventListener('focusin',e=>{const g=pick(e.target);if(g)focusNode(nodeOf(g))});
  hub.addEventListener('pointerleave',()=>focusNode(null));
  hub.addEventListener('focusout',()=>focusNode(null));
  // 副盘：最近收录 / 每天沉淀
  const wave=$('#pulseWave');
  const stageEl=$('#pulseStage');
  stageEl.addEventListener('pointermove',e=>{if(!pulseDays.length)return;const r=stageEl.getBoundingClientRect(),x=Math.min(1,Math.max(0,(e.clientX-r.left)/r.width));pulseHover(Math.round(x*(pulseDays.length-1)),x)});
  stageEl.addEventListener('pointerleave',pulseLeave);
  stageEl.addEventListener('click',()=>{if(pulseDays.length)switchTo('files')});
  const rec=$('#subRecent');
  rec.addEventListener('pointerenter',()=>{listLock=true});
  rec.addEventListener('pointermove',e=>{const r=e.target.closest('[data-rrow]');if(r)subFocus(Number(r.dataset.rrow),true)});
  rec.addEventListener('pointerleave',()=>{listLock=false;subRelease()});
  rec.addEventListener('focusin',e=>{const r=e.target.closest('[data-rrow]');if(r)subFocus(Number(r.dataset.rrow),true)});
  rec.addEventListener('focusout',subRelease);
  rec.addEventListener('click',e=>{const r=e.target.closest('[data-rrow]');if(r)openRecent(model?.recent?.items?.[Number(r.dataset.rrow)])});
  const sub=$('#subStage');
  sub.addEventListener('pointermove',e=>{if(listLock)return;const mk=e.target.closest('[data-recent]'),day=e.target.closest('[data-day]');if(mk)subFocus(Number(mk.dataset.recent),true);else if(day)subDay(Number(day.dataset.day))});
  sub.addEventListener('pointerleave',subRelease);
  sub.addEventListener('click',e=>{const mk=e.target.closest('[data-recent]');if(mk)return openRecent(model?.recent?.items?.[Number(mk.dataset.recent)]);if(e.target.closest('#subCore')){const it=model?.recent?.items?.[subIndex];return it?openRecent(it):switchTo('files')}if(e.target.closest('[data-day]'))switchTo('files')});
  sub.addEventListener('keydown',e=>{if((e.key==='Enter'||e.key===' ')&&e.target.closest('#subCore')){e.preventDefault();const it=model?.recent?.items?.[subIndex];it?openRecent(it):switchTo('files')}});
  const con=$('.sh-console',host);if(window.ResizeObserver)new ResizeObserver(()=>drawLinks()).observe(con);
  tickClock();setInterval(tickClock,30000);
  setInterval(()=>{if(!subHold&&!listLock&&!document.hidden&&!reduceMotion.matches&&$('#overview')?.classList.contains('active')&&model?.recent?.items?.length>1)subFocus((subIndex+1)%model.recent.items.length,false)},6000);
}
const nodeOf=g=>model?(g.dataset.kind==='cat'?model.cats:model.ais)[Number(g.dataset.index)]:null;
function tickClock(){const d=$('#studioDate');if(d)d.textContent=today();const name=($('#displayName')?.textContent||'').trim(),g=$('#studioGreeting');if(!g)return;
  if(!quip||quip.slot!==slotOf(new Date().getHours()))pickQuip();
  const html=`<span class="sh-hi">${esc(greeting()+(name&&name!=='我'?'，'+name:''))}</span><span class="sh-quip${quip.egg?' egg':''}">${esc(quip.text)}</span>`;
  if(g.dataset.html!==html){g.dataset.html=html;g.innerHTML=html}}
function setCore(c){$('#coreKicker').textContent=c.kicker;$('#coreValue').textContent=c.text??fmt(c.value);$('#coreValue').classList.toggle('is-text',c.text!=null);$('#coreUnit').textContent=c.unit||'';$('#coreHint').textContent=c.hint||''}
const coreDefault=()=>({kicker:'界渡',value:model.core.value,unit:model.core.unit,hint:'打开全部资料'});
let focused=null;
function focusNode(n,quiet=false){
  if(!model||n===focused)return;focused=n;
  $$('[data-kind][data-index]',host).forEach(g=>g.classList.toggle('is-focus',!!n&&nodeOf(g)===n));
  host.classList.toggle('has-focus',!!n);
  const needle=$('#studioNeedle');
  if(needle){if(n&&geometry.has(n)){if(quiet){needle.style.transition='none';needle.style.transform=`rotate(${geometry.get(n)}deg)`;needle.getBoundingClientRect();needle.style.transition=''}else needle.style.transform=`rotate(${geometry.get(n)}deg)`;needle.classList.add('on')}else needle.classList.remove('on')}
  const core=$('#studioCore');
  if(!n){core.style.removeProperty('--core-tint');return setCore(coreDefault())}
  const g=$$('.hub-node',host).find(x=>nodeOf(x)===n);
  core.style.setProperty('--core-tint',g?getComputedStyle(g).getPropertyValue('--tint').trim()||'var(--core-default)':'var(--core-default)');
  if(g&&!quiet&&!reduceMotion.matches){g.classList.remove('pulse');void g.getBoundingClientRect();g.classList.add('pulse');
    clearTimeout(focusNode.t);focusNode.t=setTimeout(()=>{core.classList.remove('ripple');void core.offsetWidth;core.classList.add('ripple')},520)}
  if(n.kind==='cat')setCore({kicker:n.label,value:n.count,unit:n.hint,hint:'点一下打开'});
  else if(n.more)setCore({kicker:n.label,text:n.tone==='warn'?'有需重连':n.tone==='ok'?'含已连接':'未连接',unit:n.sub==='都还没连接'?n.hint:n.sub,hint:'点一下去「连接 AI」'});
  else setCore({kicker:n.label,text:n.state==='ok'?'已连接':n.state==='warn'?'需重连':'未连接',unit:n.hint,hint:n.state==='ok'?'点一下查看权限':'点一下去连接'});
}
function render(){
  if(!model||!host)return;
  /* r46：重画前记住正在悬停的扇区，重画后若鼠标仍在圆盘上就原样恢复高亮（不重播脉冲、指针不重新甩一圈） */
  const hub=$('#studioHub'),keep=focused&&hub.matches(':hover')?focused.kind+':'+focused.key:null;focused=null;
  hub.innerHTML=dial(model);
  setCore(coreDefault());
  if(keep){const n=[...model.cats,...model.ais].find(x=>x.kind+':'+x.key===keep);if(n)focusNode(n,true)}
  const st=$('#studioStatus'),n=model.next[0];
  st.innerHTML=!model.connected?`<button type="button" class="next-item" id="studioNextBtn"><span>还没连接知识库，先选一个文件夹</span><i aria-hidden="true">→</i></button>`
    :n?`<button type="button" class="next-item" id="studioNextBtn"><span>${esc(n.text)}</span><i aria-hidden="true">→</i></button>`
    :`<span class="next-ok"><i></i>一切就绪，资料只存在这台电脑</span>`;
  $('#studioNextBtn')?.addEventListener('click',()=>(n?n.go:()=>switchTo('workspace'))());
  renderSub();renderPulse();
  $('#studioLegacy').hidden=!(model.connected&&model.cats.reduce((s,c)=>s+c.count,0)===0);
}
function sigOf(m){return JSON.stringify([m.recent,m.core,m.cats,m.ais.map(a=>[a.key,a.state,a.label,a.sub||"",a.tone||""]),m.next.map(n=>n.text),m.metrics.map(x=>[x.k,x.v,x.text]),m.connected])}
function apply(m,force){const sig=sigOf(m);model=m;if(sig!==lastSig||force){lastSig=sig;render()}}
/* 状态一变（任何页面读到新状态、切换模拟、改首页设置）就用已缓存的资料数据立即重算，不等下一轮轮询 */
function relive(){if(lastRaw&&host)apply(buildModel(lastRaw))}
/* r46：刷新串行化——同一时刻只跑一轮；期间再有刷新请求就记下来，等这一轮结束后再补跑一次最新的。
   旧请求不会晚到覆盖新数据，也不会同时打出多份重复请求。 */
let refreshing=null,refreshQueued=false,refreshForce=false;
function refresh(force=false){
  if(!host||(document.hidden&&!force))return Promise.resolve();
  refreshForce=refreshForce||force;
  if(refreshing){refreshQueued=true;return refreshing}
  refreshing=(async()=>{try{do{refreshQueued=false;const f=refreshForce;refreshForce=false;const m=await loadModel();apply(m,f)}while(refreshQueued)}finally{refreshing=null}})();
  return refreshing}

/* ---------- 副盘：知识动态 ---------- */
const GROUP_NAME=k=>FOLDER_NAMES[k]?.[0]||k.replace(/^\d+_/,'');
function shortDate(v){const d=new Date(v);if(isNaN(d))return '';const p=n=>String(n).padStart(2,'0');return `${d.getFullYear()}年${d.getMonth()+1}月${d.getDate()}日 ${p(d.getHours())}:${p(d.getMinutes())}`}
const S=440,SC=220,SR0=126,SR1=166,SORB=184;
const sp=(r,deg)=>[SC+r*Math.cos(rad(deg)),SC+r*Math.sin(rad(deg))];
function sarc(r0,r1,a0,a1){const [x0,y0]=sp(r1,a0),[x1,y1]=sp(r1,a1),[x2,y2]=sp(r0,a1),[x3,y3]=sp(r0,a0),l=a1-a0>180?1:0;return `M${f(x0)} ${f(y0)}A${r1} ${r1} 0 ${l} 1 ${f(x1)} ${f(y1)}L${f(x2)} ${f(y2)}A${r0} ${r0} 0 ${l} 0 ${f(x3)} ${f(y3)}Z`}
let subBars=[],subIndex=0,subHold=false,subMarks=[],listLock=false;
function catOfItem(x){const key=x.kind==='chat'?'90_原始档案':x.group;return model.cats.findIndex(c=>c.key===key)}
function computeBars(r){
  const daily=r.daily||[],start=new Date(r.start+'T00:00:00'),last14=daily.slice(-14);
  if(last14.reduce((a,b)=>a+b,0)>0){const s0=new Date(start);s0.setDate(s0.getDate()+daily.length-14);
    return {label:'近 14 天',unitDays:1,start:s0,bars:last14.map((v,i)=>{const d=new Date(s0);d.setDate(d.getDate()+i);return {v,d,t:`${d.getFullYear()}年${d.getMonth()+1}月${d.getDate()}日`,today:i===13}})}}
  const wk=Math.floor(daily.length/7),s0=new Date(start);s0.setDate(s0.getDate()+daily.length-wk*7);
  return {label:`近 ${wk} 周`,unitDays:7,start:s0,bars:Array.from({length:wk},(_,w)=>{const seg=daily.slice(daily.length-wk*7+w*7,daily.length-wk*7+w*7+7),d=new Date(s0);d.setDate(d.getDate()+w*7);return {v:seg.reduce((a,b)=>a+b,0),d,t:`${d.getFullYear()}年${d.getMonth()+1}月${d.getDate()}日起的一周`,today:w===wk-1}})};
}
function subDial(){
  const r=model.recent,ticks=Array.from({length:168},(_,i)=>{const d=i*360/168-90,long=i%12===0,[x0,y0]=sp(long?192:197,d),[x1,y1]=sp(204,d);return `<line class="tick${long?' long':''}" x1="${f(x0)}" y1="${f(y0)}" x2="${f(x1)}" y2="${f(y1)}"/>`}).join('');
  let wedges='',marks='';subMarks=[];
  if(r){
    const B=computeBars(r),n=B.bars.length,step=360/n,max=Math.max(1,...B.bars.map(b=>b.v));subBars=B.bars;
    wedges=B.bars.map((b,i)=>{const a0=-90+i*step+1.4,a1=-90+(i+1)*step-1.4,h=SR0+(SR1-SR0)*Math.max(.07,b.v/max);
      return `<g class="sub-day${b.today?' today':''}${b.v?'':' zero'}" data-day="${i}"><path class="hit" d="${sarc(SR0-6,SR1+8,a0-1.4,a1+1.4)}"/><path class="sub-track" d="${sarc(SR0,SR1,a0,a1)}"/><path class="sub-bar" d="${sarc(SR0,h,a0,a1)}"/></g>`}).join('');
    const used={};
    marks=(r.items||[]).map((x,k)=>{const t=new Date(x.modified_at).getTime();let idx=Math.floor((t-B.start.getTime())/(86400000*B.unitDays));idx=Math.max(0,Math.min(n-1,idx));const c=used[idx]=(used[idx]||0)+1;
      const ang=-90+(idx+.5)*step+(c-1)*(c%2?1:-1)*Math.min(7,step*.3),[mx,my]=sp(SORB,ang),[bx,by]=sp(SR1+6,ang),[ex,ey]=sp(96,ang),ci=catOfItem(x),tint=ci>=0?`var(--tint-${ci%6})`:'var(--accent)';subMarks.push({ang,tint});
      return `<g class="sub-mark" data-recent="${k}" style="--tint:${tint}"><path class="sub-beam" d="M${f(bx)} ${f(by)} L${f(ex)} ${f(ey)}" pathLength="100"/><circle class="hit" cx="${f(mx)}" cy="${f(my)}" r="15"/><circle class="halo-r" cx="${f(mx)}" cy="${f(my)}" r="9"/><circle class="dot" cx="${f(mx)}" cy="${f(my)}" r="4.6"/></g>`}).join('');
    $('#subSum').textContent=`${B.label}更新 ${fmt(B.bars.reduce((a,b)=>a+b.v,0))} 份`;
  }else{$('#subSum').textContent='';subBars=[]}
  return `<svg viewBox="0 0 ${S} ${S}" class="sub-svg" role="group" aria-label="知识动态表盘">
    <g class="sub-spin">${ticks}</g>
    <circle class="ring" cx="${SC}" cy="${SC}" r="${SR0-8}"/><circle class="ring orbit" cx="${SC}" cy="${SC}" r="${SORB}"/>
    ${r?'':Array.from({length:14},(_,i)=>`<path class="sub-track" d="${sarc(SR0,SR1,-90+i*360/14+1.4,-90+(i+1)*360/14-1.4)}"/>`).join('')}
    ${wedges}${marks}
  </svg>`;
}
function setSub(k,t,m,tint){$('#subKicker').textContent=k;const parts=String(t).split(/\s*[｜|]\s*/);$('#subTitle').innerHTML=parts.length>1&&parts[0]&&parts.slice(1).join('')?`<small class="t-pre">${esc(parts[0])}</small>${esc(parts.slice(1).join(' · '))}`:esc(t);$('#subTitle').title=t;$('#subMeta').textContent=m||'';const c=$('#subCore');if(tint)c.style.setProperty('--core-tint',tint);else c.style.removeProperty('--core-tint')}
function renderSub(){
  $('#subHub').innerHTML=subDial();renderSubRecent();requestAnimationFrame(drawLinks);
  const r=model.recent,sn=$('#subSnippet');
  if(!r){setSub('知识动态','—','重启一次预览服务后显示');sn.textContent='';return}
  if(!r.items.length){setSub('知识动态','还很安静','还没有新收录的资料');sn.textContent='';return}
  subIndex=Math.min(subIndex,r.items.length-1);subFocus(subIndex,false,true);
}
function subFocus(i,hold,quiet){
  const items=model?.recent?.items;if(!items?.[i])return;
  if(hold&&subHold&&i===subIndex)return;
  if(!hold&&listLock)return;
  if(hold)subHold=true;subIndex=i;const x=items[i];
  $$('.sub-mark',host).forEach(g=>g.classList.toggle('on',Number(g.dataset.recent)===i));
  $$('#subRecent [data-rrow]',host).forEach(g=>g.classList.toggle('on',Number(g.dataset.rrow)===i));
  $$('.sub-day',host).forEach(g=>g.classList.remove('is-focus'));
  const mk=$(`.sub-mark[data-recent="${i}"]`,host);
  if(mk&&!quiet&&!reduceMotion.matches){mk.classList.remove('pulse');void mk.getBoundingClientRect();mk.classList.add('pulse');const c=$('#subCore');clearTimeout(subFocus.t);subFocus.t=setTimeout(()=>{c.classList.remove('ripple');void c.offsetWidth;c.classList.add('ripple')},480)}
  const core=$('#subCore');core.classList.remove('swap');void core.offsetWidth;core.classList.add('swap');
  setSub(x.kind==='chat'?x.group:GROUP_NAME(x.group),x.title.replace(/\.md$/i,'').replace(/\s*\[[0-9a-f]{6,}\]$/i,''),`最近收录 ${i+1} / ${items.length}`,subMarks[i]?.tint);
  $('#subSnippet').innerHTML=`<time>${esc(shortDate(x.modified_at))}</time>${esc(x.snippet||'')}`;
  // 双盘联动：资料所在的分类在主盘上亮起，光脉冲涌入主核
  if(hold){const ci=catOfItem(x);host.classList.add('bridge-on');focusNode(ci>=0?model.cats[ci]:null)}
}
function subDay(i){
  const b=subBars[i];if(!b||listLock)return;subHold=true;
  $$('.sub-day',host).forEach(g=>g.classList.toggle('is-focus',Number(g.dataset.day)===i));
  $$('.sub-mark',host).forEach(g=>g.classList.remove('on'));
  setSub(b.t.includes('一周')?'这一周':'这一天',`${fmt(b.v)} 份`,b.v?'新增或改动':'没有新资料');
  $('#subSnippet').innerHTML=`<time>${esc(b.t)}</time>${b.v?'这段时间新增或改动了 '+fmt(b.v)+' 份资料':'这段时间没有新资料'}`;host.classList.remove('bridge-on');focusNode(null);
}
function subRelease(){
  if(!subHold||listLock)return;subHold=false;host.classList.remove('bridge-on');focusNode(null);
  $$('.sub-day',host).forEach(g=>g.classList.remove('is-focus'));
  if(model?.recent?.items?.length)subFocus(subIndex,false,true);else renderSub();
}
/* ---------- 律动条：近 56 天每天的知识沉淀 ---------- */

function smoothPath(pts){ // Catmull-Rom → 三次贝塞尔
  if(pts.length<2)return '';let d=`M${f(pts[0][0])} ${f(pts[0][1])}`;
  for(let i=0;i<pts.length-1;i++){const p0=pts[i-1]||pts[i],p1=pts[i],p2=pts[i+1],p3=pts[i+2]||p2;
    d+=` C${f(p1[0]+(p2[0]-p0[0])/6)} ${f(p1[1]+(p2[1]-p0[1])/6)} ${f(p2[0]-(p3[0]-p1[0])/6)} ${f(p2[1]-(p3[1]-p1[1])/6)} ${f(p2[0])} ${f(p2[1])}`}
  return d;
}
function waveSvg(vals,idle){
  const n=vals.length,max=Math.max(1,...vals),N=220,H=60,B=50,pts=[];
  // 每天的新增 → 一个「快起慢落」的不对称脉冲峰；再叠一层有机的细小起伏，表示系统一直在活跃运行
  for(let k=0;k<=N;k++){const x=k/N,day=x*(n-1);let y=0;
    for(let i=0;i<n;i++){const v=vals[i];if(!v)continue;const d=day-i,amp=10+30*Math.sqrt(v/max);y+=d<0?amp*Math.exp(-(d*d)/0.55):amp*Math.exp(-d/1.6)*(1-Math.exp(-(d+.35)*3.2))}
    const org=4.2*Math.sin(x*17.3+.7)+2.6*Math.sin(x*41.1+2.1)+1.6*Math.sin(x*97+.3)+1.1*Math.sin(x*163+1.9);
    pts.push([x*1000,Math.max(3,Math.min(B-1,B-(idle?6:4)-Math.max(0,org+5)*.55-y))])}
  waveYs=pts.map(p=>p[1]/H);const line=smoothPath(pts.filter((_,k)=>k%2===0||k===N)),last=pts[pts.length-1];
  return {svg:`<defs><linearGradient id="pwStroke" x1="0" x2="1"><stop offset="0" stop-color="var(--wave)" stop-opacity=".25"/><stop offset=".55" stop-color="var(--wave)" stop-opacity=".75"/><stop offset="1" stop-color="var(--wave)" stop-opacity="1"/></linearGradient><linearGradient id="pwArea" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="var(--wave)" stop-opacity="${idle?.06:.16}"/><stop offset="1" stop-color="var(--wave)" stop-opacity="0"/></linearGradient></defs>
    <line class="pw-mid" x1="0" y1="${B}" x2="1000" y2="${B}"/>
    <path class="pw-area" d="${line} L1000 ${B} L0 ${B} Z" fill="url(#pwArea)"/>
    <path class="pw-line" d="${line}" stroke="url(#pwStroke)"/>
    <path class="pw-flow a" d="${line}" pathLength="1000"/><path class="pw-flow b" d="${line}" pathLength="1000"/>`,endY:last[1]/H};
}
let pulseDays=[],waveYs=[],pulseAnim=null,pulseTarget=null,pulseCur=null;
function waveYAt(x){if(!waveYs.length)return .8;const t=x*(waveYs.length-1),i=Math.floor(t),fr=t-i;return (waveYs[i]??waveYs.at(-1))*(1-fr)+(waveYs[i+1]??waveYs.at(-1))*fr}
function pulseTick(){if(pulseTarget==null){pulseAnim=null;return}const st=$('#pulseStage');st?.classList.toggle('at-rest',!st.classList.contains('is-hover'));pulseCur=pulseCur==null?pulseTarget:pulseCur+(pulseTarget-pulseCur)*.22;
  const dot=$('#pulseDot'),tip=$('#pulseTip'),cur=$('#pulseCursor'),x=pulseCur,y=waveYAt(x);
  if(dot){dot.style.left=(x*100).toFixed(3)+'%';dot.style.top=(y*100).toFixed(2)+'%'}
  if(cur)cur.style.left=(x*100).toFixed(3)+'%';
  if(tip){tip.style.left=(x*100).toFixed(3)+'%';tip.style.top=(y*100).toFixed(2)+'%';tip.classList.toggle('flip-l',x>.86);tip.classList.toggle('flip-r',x<.14)}
  pulseAnim=Math.abs(pulseTarget-pulseCur)>.0005?requestAnimationFrame(pulseTick):null}
function renderPulse(){
  const r=model.recent,svg=$('#pulseSvg'),read=$('#pulseRead');if(!svg)return;
  const latest=r?.items?.[0]?.modified_at||model.lastSync;
  $('#pulseState').textContent=model.connected?'界渡运行中':'等待连接知识库';
  $('#pulseSub').textContent=latest?`最近入库 ${shortDate(latest)}`:'资料只存在这台电脑';
  if(!r){pulseDays=[];const w=waveSvg(Array(56).fill(0),true);svg.innerHTML=w.svg;pulseRest();$('#pulseFrom').textContent='';read.textContent='重启一次预览服务后，这里会跟着你每天的资料律动';read.dataset.base=read.textContent;return}
  const daily=r.daily||[],start=new Date(r.start+'T00:00:00');
  pulseDays=daily.map((v,i)=>{const d=new Date(start);d.setDate(d.getDate()+i);return {v,t:`${d.getFullYear()}年${d.getMonth()+1}月${d.getDate()}日`,s:`${String(d.getMonth()+1).padStart(2,'0')}月${String(d.getDate()).padStart(2,'0')}日`,i}});
  const w=waveSvg(daily,false);svg.innerHTML=w.svg;pulseRest();
  const total=daily.reduce((a,b)=>a+b,0);
  $('#pulseFrom').textContent=pulseDays[0]?.t.replace(/^\d+年/,'')||'';
  read.textContent=`近 ${daily.length} 天沉淀 ${fmt(total)} 份`;read.dataset.base=read.textContent;
  $('#pulseWave').setAttribute('aria-label',`近 ${daily.length} 天每天新增的资料数量，共 ${total} 份`);
}
function pulseHover(i,x){
  const x0=pulseDays[i];if(!x0)return;
  pulseTarget=x;if(!pulseAnim)pulseAnim=requestAnimationFrame(pulseTick);
  $('#pulseStage')?.classList.add('is-hover');
  const tip=$('#pulseTip');if(tip&&tip.dataset.i!==String(i)){tip.dataset.i=i;tip.innerHTML=`<span>${esc(x0.s)}</span><i></i><b>${x0.v} 份</b>`;tip.title=x0.t}
  const off=pulseDays.length-subBars.length;if(subBars.length===14&&i>=off)subDay(i-off);else if(subHold)subRelease();
}
function pulseLeave(){$('#pulseStage')?.classList.remove('is-hover');pulseTarget=null;pulseCur=null;subRelease()}
function pulseRest(){pulseTarget=1;pulseCur=1;const dot=$('#pulseDot'),st=$('#pulseStage');if(dot){dot.style.left='100%';dot.style.top=(waveYAt(1)*100).toFixed(2)+'%'}if(st&&!st.classList.contains('is-hover'))st.classList.add('at-rest')}
function renderSubRecent(){
  const box=$('#subRecent');if(!box)return;const items=(model.recent?.items||[]).slice(0,3);
  box.innerHTML=items.map((x,i)=>`<li><button type="button" data-rrow="${i}" style="--tint:${subMarks[i]?.tint||'var(--accent)'}"><i></i><span><b>${esc(x.title.replace(/\.md$/i,'').replace(/\s*\[[0-9a-f]{6,}\]$/i,''))}</b><small>${esc(x.kind==='chat'?x.group+' 聊天':GROUP_NAME(x.group))} · ${esc(shortDate(x.modified_at))}</small></span><em aria-hidden="true">↗</em></button></li>`).join('');
}
/* 主副双盘之间的两道能量弧 */
function drawLinks(){
  const svg=$('#studioLinks'),con=$('.sh-console',host),a=$('#studioStage'),b=$('#subStage');if(!svg||!con||!a||!b)return;
  const C=con.getBoundingClientRect(),A=a.getBoundingClientRect(),B=b.getBoundingClientRect();if(!C.width||!B.width)return;
  const ax=A.left-C.left+A.width/2,ay=A.top-C.top+A.height/2,ar=A.width*218/760,bx=B.left-C.left+B.width/2,by=B.top-C.top+B.height/2,br=B.width*204/440;
  const P=(cx,cy,r,deg)=>[cx+r*Math.cos(rad(deg)),cy+r*Math.sin(rad(deg))];
  const arc=(d1,d2,lift)=>{const [x1,y1]=P(ax,ay,ar+6,d1),[x2,y2]=P(bx,by,br+6,d2),mx=(x1+x2)/2,my=(y1+y2)/2+lift;return `M${f(x1)} ${f(y1)} Q${f(mx)} ${f(my)} ${f(x2)} ${f(y2)}`};
  const dy=Math.max(40,(ar+br)*.24);
  svg.setAttribute('viewBox',`0 0 ${f(C.width)} ${f(C.height)}`);
  svg.innerHTML=`<defs><linearGradient id="lkGrad" x1="0" x2="1"><stop offset="0" stop-color="var(--glow)" stop-opacity=".05"/><stop offset=".5" stop-color="var(--glow)" stop-opacity=".55"/><stop offset="1" stop-color="var(--glow)" stop-opacity=".08"/></linearGradient></defs>
    <path class="lk base" d="${arc(-16,196,-dy)}"/><path class="lk flow" d="${arc(-16,196,-dy)}"/>
    <path class="lk base" d="${arc(16,164,dy)}"/><path class="lk flow r" d="${arc(16,164,dy)}"/>`;
}
async function openRecent(x){
  if(!x)return;
  if(x.kind==='chat'){
    switchTo('archives');
    try{const t=x.title.replace(/\s*\[[0-9a-f]{6,}\]$/i,'');if(typeof archiveListState==='object'&&typeof loadRawSessions==='function'){archiveListState.client='all';archiveListState.status='all';archiveListState.query=t;archiveListState.offset=0;await loadRawSessions();const row=await waitFor(()=>$('#rawSessionList [data-raw-session]'));row?.click()}}catch(_){}
    return;
  }
  if(!x.id)return gotoFolder(x.group);
  switchTo('files');
  const row=await waitFor(()=>$(`[data-object-id="${CSS.escape(x.id)}"] .knowledge-object-main`));
  if(row){row.scrollIntoView({block:'center',behavior:reduceMotion.matches?'auto':'smooth'});row.click()}
}

/* ---------- 外观切换胶囊 ---------- */
const MODE_ICON={light:'<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M4.6 4.6l1.4 1.4M18 18l1.4 1.4M2.5 12h2M19.5 12h2M4.6 19.4L6 18M18 6l1.4-1.4"/>',dark:'<path d="M19.5 14.5A8 8 0 0 1 9.5 4.5a8 8 0 1 0 10 10z"/>',schedule:'<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>'};
const schedNote=(f,t)=>`${f} 到${toMin(t)<=toMin(f)?'次日 ':''}${t} 用深色，其余时间用浅色。会一直按这个时间自动切换，直到你选回「浅色」或「深色」。`;
let syncModeUI=()=>{};
function buildModeSwitch(){
  const bar=$('.topbar');if(!bar||$('.theme-mode',bar))return;
  const box=document.createElement('div');box.className='theme-mode';
  const s=readSched();
  box.innerHTML=`<div class="tm-seg" role="radiogroup" aria-label="外观">${[['light','浅色'],['dark','深色'],['schedule','定时']].map(([k,t])=>`<button type="button" role="radio" data-mode="${k}"><svg class="ic" viewBox="0 0 24 24" aria-hidden="true">${MODE_ICON[k]}</svg><span>${t}</span></button>`).join('')}</div>
    <div class="tm-pop" id="tmPop" hidden role="dialog" aria-label="定时深色">
      <b>每天定时切到深色</b>
      <div class="tm-times"><label><span>从</span><input type="time" id="tmFrom" value="${s.from}"></label><label><span>到</span><input type="time" id="tmTo" value="${s.to}"></label></div>
      <p id="tmNote"></p>
      <div class="tm-actions"><button type="button" id="tmCancel">取消</button><button type="button" class="primary" id="tmSave">保存</button></div>
    </div>`;
  bar.insertBefore(box,$('.system-status',bar));
  const pop=$('#tmPop',box);
  const sync=()=>{const m=readMode(),sc=readSched();$$('[data-mode]',box).forEach(b=>b.setAttribute('aria-checked',String(b.dataset.mode===m)));$('[data-mode="schedule"] span',box).textContent=m==='schedule'?`定时 ${sc.from}–${sc.to}`:'定时';$('#tmNote',box).textContent=schedNote(sc.from,sc.to)};
  box.addEventListener('click',e=>{const b=e.target.closest('[data-mode]');if(!b)return;const m=b.dataset.mode;
    if(m==='schedule'){pop.hidden=!pop.hidden;if(!pop.hidden)$('#tmFrom').focus();if(readMode()!=='schedule'){ls.set(MODE_KEY,'schedule');applyTheme(true)}}
    else{pop.hidden=true;ls.set(MODE_KEY,m);applyTheme(true)}
    sync()});
  const preview=()=>{const f=$('#tmFrom').value,t=$('#tmTo').value;if(f&&t)$('#tmNote').textContent=schedNote(f,t)};
  $('#tmFrom').addEventListener('input',preview);$('#tmTo').addEventListener('input',preview);
  $('#tmSave').addEventListener('click',()=>{const f=$('#tmFrom').value,t=$('#tmTo').value;if(!f||!t||f===t){$('#tmNote').textContent='开始和结束时间不能一样。';return}ls.set(SCHED_KEY,JSON.stringify({from:f,to:t}));ls.set(MODE_KEY,'schedule');applyTheme(true);pop.hidden=true;sync()});
  $('#tmCancel').addEventListener('click',()=>{pop.hidden=true;const s=readSched();$('#tmFrom').value=s.from;$('#tmTo').value=s.to;sync()});
  document.addEventListener('pointerdown',e=>{if(!pop.hidden&&!box.contains(e.target))pop.hidden=true});
  document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!pop.hidden)pop.hidden=true});
  syncModeUI=sync;sync();
}
setInterval(()=>applyTheme(true),30000);
window.addEventListener('storage',e=>{if(e.key===MODE_KEY||e.key===SCHED_KEY)applyTheme(true)});
window.jieduStudio={refresh:()=>refresh(true),get overview(){return lastRaw?.ov||null},get theme(){return root.dataset.theme},setMode(m){ls.set(MODE_KEY,m);applyTheme(true);syncModeUI()}};

/* ---------- 搜索面板（Ctrl+K） ---------- */
const COMMANDS=[
  {t:'首页',d:'回到知识表盘',go:()=>switchTo('overview')},
  {t:'管家',d:'和界渡管家聊天、问用法',go:()=>switchTo('butler')},
  {t:'知识',d:'打开、导入和管理资料',go:()=>switchTo('files')},
  {t:'聊天记录',d:'翻看和 AI 的原话',go:()=>switchTo('archives')},
  {t:'整理知识',d:'把聊天变成能长期用的结论',go:()=>switchTo('memory')},
  {t:'连接 AI',d:'让 Codex、Hermes 等读你的知识库',go:()=>switchTo('access')},
  {t:'导入资料',d:'把文件、文件夹、截图收进来',go:()=>{switchTo('files');setTimeout(()=>$('#openKnowledgeImport')?.click(),0)}},
  {t:'设置',d:'密码、保护和其他选项',go:()=>switchTo('protection')},
  {t:'故障排查',d:'出问题时来这里看',go:()=>switchTo('troubleshooting')},
  {t:'工作区',d:'检查或更换知识库文件夹',go:()=>switchTo('workspace')}
];
let palette,pIndex=0,pItems=[],lastFocus=null;
function buildPalette(){
  palette=document.createElement('div');palette.className='studio-palette';palette.hidden=true;
  palette.innerHTML=`<div class="palette-scrim" data-close></div><div class="palette-box" role="dialog" aria-modal="true" aria-label="搜索"><div class="palette-input">${icon('search')}<input id="studioPaletteInput" placeholder="输入问题搜索知识，或输入页面名称跳转" autocomplete="off" spellcheck="false"><kbd>Esc</kbd></div><div class="palette-list" id="studioPaletteList" role="listbox"></div><div class="palette-foot"><span><kbd>↑</kbd><kbd>↓</kbd> 选择</span><span><kbd>Enter</kbd> 打开</span><span><kbd>Esc</kbd> 关闭</span></div></div>`;
  document.body.append(palette);
  const input=$('#studioPaletteInput');
  input.addEventListener('input',()=>{pIndex=0;paint()});
  input.addEventListener('keydown',e=>{const n=Math.max(1,pItems.length);if(e.key==='ArrowDown'){e.preventDefault();pIndex=(pIndex+1)%n;paint(false)}else if(e.key==='ArrowUp'){e.preventDefault();pIndex=(pIndex-1+n)%n;paint(false)}else if(e.key==='Enter'){e.preventDefault();choose(pIndex)}else if(e.key==='Escape'){e.preventDefault();closePalette()}});
  palette.addEventListener('click',e=>{if(e.target.closest('[data-close]'))return closePalette();const it=e.target.closest('[data-pi]');if(it)choose(Number(it.dataset.pi))});
  palette.addEventListener('pointermove',e=>{const it=e.target.closest('[data-pi]');if(it&&Number(it.dataset.pi)!==pIndex){pIndex=Number(it.dataset.pi);paint(false)}});
}
function paint(rebuild=true){
  const q=$('#studioPaletteInput').value.trim();
  if(rebuild){
    const cmds=COMMANDS.filter(c=>!q||c.t.includes(q)||c.d.includes(q)).map(c=>({...c,tag:'页面'}));
    const cats=(model?.cats||[]).filter(c=>!q||c.label.includes(q)||c.key.includes(q)).map(c=>({t:c.label,d:`${fmt(c.count)} 份 · ${c.hint}`,go:()=>gotoFolder(c.key),tag:'分类'}));
    pItems=[...(q?[{t:`在知识里搜索「${q}」`,d:'按问题找到原文和出处',go:()=>runSearch(q),tag:'搜索',primary:true}]:[]),...cats,...cmds];
  }
  $('#studioPaletteList').innerHTML=pItems.map((it,i)=>`<div class="palette-item${i===pIndex?' active':''}${it.primary?' primary':''}" data-pi="${i}" role="option" aria-selected="${i===pIndex}"><span class="pi-tag">${esc(it.tag||'')}</span><span class="pi-main"><b>${esc(it.t)}</b><small>${esc(it.d||'')}</small></span><span class="pi-enter" aria-hidden="true">↵</span></div>`).join('')||'<div class="palette-empty">没有匹配的页面</div>';
  $('#studioPaletteList .active')?.scrollIntoView({block:'nearest'});
}
function openPalette(){if(!palette)buildPalette();lastFocus=document.activeElement;palette.hidden=false;requestAnimationFrame(()=>palette.classList.add('open'));const i=$('#studioPaletteInput');i.value='';pIndex=0;paint();i.focus()}
function closePalette(){if(!palette||palette.hidden)return;palette.classList.remove('open');setTimeout(()=>{palette.hidden=true},160);lastFocus?.focus?.()}
function choose(i){const it=pItems[i];if(!it)return;closePalette();it.go()}
document.addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'){e.preventDefault();palette&&!palette.hidden?closePalette():openPalette()}});

/* ---------- 启动 ---------- */
function start(){
  build();buildModeSwitch();refresh(true);
  setInterval(()=>{if($('#overview')?.classList.contains('active'))refresh()},20000);
  addEventListener('jiedu:ai-state',relive);addEventListener('jiedu:home-ai-show',relive);
  addEventListener('storage',e=>{if(e.key===AI_PICK_KEY)relive()});
  document.addEventListener('visibilitychange',()=>{if(!document.hidden&&$('#overview')?.classList.contains('active'))refresh()});
  const ov=$('#overview');if(!ov)return;const sync=()=>document.body.classList.toggle('studio-home-active',ov.classList.contains('active'));
  let wasActive=ov.classList.contains('active');
  new MutationObserver(()=>{sync();const on=ov.classList.contains('active');if(on&&!wasActive){pickQuip();tickClock()}wasActive=on;if(on)refresh()}).observe(ov,{attributes:true,attributeFilter:['class']});sync();
}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',start);else start();
})();
