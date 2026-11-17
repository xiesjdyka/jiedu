/* 界渡 · 故障排查页（第 41 轮）
 * 顶部总览栏 + 重新检查 / 状态卡片网格（首张本机服务，后接各 AI，可按状态筛选）/ 点卡片联动的诊断详情。
 * 所有状态只读 client-state.js：本机服务与右上角「界渡已运行」同一份结果同一句话；AI 状态与首页、「连接 AI」页同源。 */
(function(){
  'use strict';
  const $=(s,r=document)=>r.querySelector(s);
  const esc=v=>String(v??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
  const p2=n=>String(n).padStart(2,'0');
  const fullTime=v=>{const d=new Date(v);if(!v||isNaN(d))return '';return `${d.getFullYear()}年${d.getMonth()+1}月${d.getDate()}日 ${p2(d.getHours())}:${p2(d.getMinutes())}`};
  const S=()=>window.jieduAiState;
  const GROUP={ok:'正常',warn:'需处理',idle:'未接入',off:'未接入',down:'需处理'};
  const FILTERS=[['all','全部'],['ok','正常'],['warn','需处理'],['none','未接入']];
  const groupOf=st=>st==='ok'?'ok':st==='warn'||st==='down'?'warn':'none';
  let filter='all',selected='service',busy=false,mounted=false,lastGrid='',lastDetail='',lastBar='';

  const HIST={not_started:'还没开始',running:'正在补齐',in_progress:'正在补齐',completed:'已补齐',done:'已补齐',failed:'补齐失败',partial:'部分补齐',unknown:'暂时无法判断'};
  const LIVE={paused:'已暂停',stale:'等待下一次同步',not_connected:'没有开启',running:'正在采集',active:'正在采集',automatic:'正在采集',degraded:'在采集，有待处理',error:'采集出错'};
  const CONF={bundle_prepared:'扩展包已准备',disabled:'配置已停用',unreadable:'配置读不到',invalid:'配置格式有误',missing:'没有找到配置',mismatch:'配置与记录不一致'};

  function srcOf(k){return (S()?.sources?.sources||[]).find(x=>(x.id||x.source_id)===k)||null}
  function clientOf(k){return (S()?.status?.registered||[]).find(r=>r.client_type===k&&r.status!=='revoked')||null}
  function legacyOf(k){return (S()?.status?.legacy||[]).find(r=>r.configured&&(r.claimed_id===k||r.client_type===k))||null}

  function aiCards(){return (S()?.list||[]).map(a=>{const c=clientOf(a.key),src=srcOf(a.key);
    const label=a.state==='ok'?'已连接':a.state==='warn'?'需重连':'未接入';
    const sub=a.state==='ok'?(c?.last_read_at?'最近读取 '+fullTime(c.last_read_at):'已经能读你的知识库'):a.state==='warn'?a.hint:(src?.detected?'本机已发现，还没接入':'本机没有发现');
    return {key:a.key,name:a.label,state:a.state,label,sub,group:groupOf(a.state)}})}
  function serviceCard(){const st=S()?.serviceState(S()?.health)||{state:'warn',text:'正在连接',note:''};return {key:'service',name:'本机服务',state:st.state,label:st.text,sub:st.note,group:groupOf(st.state),service:true}}

  function cardHtml(c){return `<button type="button" class="diag-card${c.service?' is-service':''}" data-key="${c.key}" data-tone="${c.state==='down'?'down':c.state}" aria-pressed="${c.key===selected}"${filter!=='all'&&c.group!==filter?' hidden':''}>
    <span class="dc-top"><b>${esc(c.name)}</b><em class="dc-pill">${esc(c.label)}</em></span><small>${esc(c.sub)}</small></button>`}
  /* r47：骨架统一用全站 .jd-skel / .jd-bar 写法 */
  const skelCards=()=>Array.from({length:10},()=>`<div class="diag-card is-skel jd-skel" aria-hidden="true"><i></i><i></i></div>`).join('');

  function rows(list){return `<dl class="dd-rows">${list.filter(Boolean).map(([k,v])=>`<div><dt>${esc(k)}</dt><dd>${v}</dd></div>`).join('')}</dl>`}
  function detailHtml(cards){const c=cards.find(x=>x.key===selected)||cards[0];if(!c)return '';
    const head=`<div class="dd-head" data-tone="${c.state==='down'?'down':c.state}"><span class="dd-dot"></span><div><small>${c.service?'本机':'AI 客户端'}</small><h3>${esc(c.name)}</h3></div><em class="dc-pill">${esc(c.label)}</em></div>`;
    if(c.service){const h=S()?.health||{},vm=h.vector_model||{};
      return head+rows([
        ['运行状态',esc(c.label+(c.sub?'：'+c.sub:''))],
        ['控制台地址',`<code>${esc(location.origin)}</code>`],
        h.workspace_root&&['工作区',`<code>${esc(h.workspace_root)}</code>`],
        ['AI 工具接口',h.mcp==='ready'?'正常':h.mcp?'没有就绪':'正在确认'],
        ['语义搜索',vm.actual_vector_recall_ready?'已就绪':vm.installation_state==='missing'?'语义模型还没安装，目前按关键词搜索':'还没就绪，目前按关键词搜索'],
        ['最近检查',esc(fullTime(S()?.healthAt)||'正在检查')]
      ])+`<p class="dd-note">这里的状态与右上角「${esc(c.label)}」来自同一次检查。</p>`}
    const k=c.key,cl=clientOf(k),lg=legacyOf(k),src=srcOf(k),live=src?.live||{},hist=src?.history||{};
    const conn=c.state==='ok'?'已连接，最近一次读取成功':c.state==='warn'?(cl?'连接需要处理':lg?'发现旧的连接方式，需要重新接入':'连接需要处理'):cl?'已配置，等待第一次读取验证':'未接入';
    const conf=cl?(CONF[cl.configuration_state]||(cl.configuration_current===true?'配置有效':cl.configuration_current===false?'配置需要检查':'等待核对')):lg?'有旧配置':'还没有配置';
    const inst=cl?.installation_state==='present'||src?.detected?'本机已发现':cl?.installation_state==='absent'?'本机没有找到程序':'本机没有发现';
    const issues=(src?.source_issues||[]).length;
    const archive=src?(LIVE[live.status]||'暂时无法判断')+(Number(live.saved_messages)>0?` · 已保存 ${Number(live.saved_messages).toLocaleString('zh-CN')} 条消息`:'')+(live.last_message_at?` · 最近一条 ${fullTime(live.last_message_at)}`:''):'这个客户端不支持本机采集';
    const act=c.state==='ok'?'查看权限':c.state==='warn'?'去重新连接':'去接入';
    return head+rows([
      ['连接状态',esc(conn)],
      ['最近读取',esc(cl?.last_read_at?fullTime(cl.last_read_at):'还没有读取记录')],
      ['连接配置',esc(conf)],
      ['程序',esc(inst)],
      src&&['本机聊天记录',src.readable?'可以读取':src.detected?'找到了，但读不到':'没有找到'],
      src&&['自动归档',esc(archive)],
      src&&['历史补齐',esc(HIST[hist.status]||'暂时无法判断')],
      (issues||src?.last_error)&&['需要注意',esc(issues?`有 ${issues} 处来源问题，请到「聊天记录」里查看`:'最近一次采集出错，请重新检查')]
    ])+`<div class="dd-actions"><button type="button" class="primary" data-go-access="${k}">${act}</button></div>`}

  function barHtml(cards){const svc=cards[0],ais=cards.slice(1),n=g=>ais.filter(x=>x.group===g).length,tone=svc.state!=='ok'||n('warn')?'warn':'ok';
    const title=svc.state!=='ok'?svc.label:n('warn')?`${n('warn')} 个 AI 需要处理`:'一切正常';
    const sub=`本机服务 ${svc.label} · ${n('ok')} 个已连接 · ${n('warn')} 个需处理 · ${n('none')} 个未接入`;
    return {tone,title,sub,filters:FILTERS.map(([f,t])=>{const cnt=f==='all'?cards.length:cards.filter(x=>x.group===f).length;return `<button type="button" class="diag-chip" data-filter="${f}" aria-pressed="${filter===f}">${t}<span aria-label="${cnt} 项">${cnt}</span></button>`}).join('')}}

  function render(){const root=$('#diagRoot');if(!root)return;const ready=S()?.ready&&S()?.health;
    const grid=$('#diagGrid',root),detail=$('#diagDetail',root);
    if(!ready){if(!grid.querySelector('.is-skel'))grid.innerHTML=skelCards();detail.classList.add('is-loading');return}
    const cards=[serviceCard(),...aiCards()];if(!cards.some(c=>c.key===selected))selected='service';
    const b=barHtml(cards),barSig=JSON.stringify(b);
    if(barSig!==lastBar){lastBar=barSig;const bar=$('#diagBar',root);bar.dataset.tone=b.tone;$('#diagTitle',root).textContent=b.title;$('#diagSub',root).textContent=b.sub;$('#diagFilters',root).innerHTML=b.filters}
    $('#diagAt',root).textContent=S().healthAt?'最近检查 '+fullTime(Math.max(S().healthAt,S().at||0)):'';
    const g=cards.map(cardHtml).join('');if(g!==lastGrid){lastGrid=g;grid.innerHTML=g}
    const d=detailHtml(cards);if(d!==lastDetail){lastDetail=d;detail.innerHTML=d}detail.classList.remove('is-loading');
    const empty=!cards.some(c=>filter==='all'||c.group===filter);$('#diagEmpty',root).hidden=!empty}

  async function recheck(){if(busy)return;busy=true;const btn=$('#diagRecheck');if(btn){btn.disabled=true;btn.textContent='正在检查…'}
    try{await S()?.refreshAll()}finally{busy=false;if(btn){btn.disabled=false;btn.textContent='重新检查'}render()}}

  function mount(){if(mounted)return;const page=$('#troubleshooting');if(!page)return;mounted=true;
    const root=document.createElement('div');root.id='diagRoot';root.className='diag';
    root.innerHTML=`<section class="diag-bar" id="diagBar" aria-live="polite"><span class="db-dot" aria-hidden="true"></span><div class="db-text"><b id="diagTitle"><i class="jd-bar db-bar"></i></b><small id="diagSub"><i class="jd-bar db-bar short"></i></small></div><div class="db-side"><small id="diagAt"></small><button type="button" class="primary" id="diagRecheck">重新检查</button></div></section>
      <div class="diag-filters" id="diagFilters" role="group" aria-label="按状态筛选"></div>
      <div class="diag-body"><div><div class="diag-grid" id="diagGrid">${skelCards()}</div><p class="diag-empty" id="diagEmpty" hidden>这个分类下暂时没有项目</p></div><aside class="diag-detail is-loading" id="diagDetail" aria-live="polite"><div class="dd-skel jd-skel" aria-hidden="true"><i></i><i></i><i></i><i></i><i></i></div></aside></div>`;
    page.prepend(root);
    root.addEventListener('click',e=>{const card=e.target.closest('.diag-card[data-key]');if(card){selected=card.dataset.key;lastGrid='';render();if(matchMedia('(max-width:1100px)').matches)$('#diagDetail').scrollIntoView({behavior:'smooth',block:'start'});return}
      const chip=e.target.closest('[data-filter]');if(chip){filter=chip.dataset.filter;lastBar='';lastGrid='';render();return}
      if(e.target.closest('#diagRecheck'))return recheck();
      const go=e.target.closest('[data-go-access]');if(go){const k=go.dataset.goAccess;try{clientAccessSelected='preset:'+k}catch(_){}window.switchPage?.('access')}});
    ['jiedu:ai-state','jiedu:ai-data','jiedu:health'].forEach(ev=>addEventListener(ev,()=>{if($('#troubleshooting.active'))render()}));
    render()}

  window.jieduDiag={load(){mount();render();return recheck()},select(k){mount();selected=k;filter='all';lastBar='';lastGrid='';render()}};
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',mount);else mount();
})();
