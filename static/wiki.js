let wikiRequestSequence=0,wikiRefreshTimer=null,wikiViewGroup='',wikiData=null,wikiRenderSignature='';
async function wikiCompatibilityStatus(d){
  // Older running backends expose associated IDs, never their pending count.
  // Read the actual queue; a failed lookup is unknown, not zero pending.
  if(!d.candidate_summary){
    const ids=new Set(d.candidate_ids||[]);
    try{
      const rows=ids.size?await api('/api/memory/candidates',{ttl:0,force:true,scope:'wiki'}):[];
      if(!Array.isArray(rows))throw Error('candidate state unavailable');
      const replaced=new Set(rows.filter(r=>r.status==='written').flatMap(r=>r.supersedes||[]));
      d.candidate_summary={pending:0,saved:0};
      for(const r of rows){if(!ids.has(r.candidate_id)||replaced.has(r.candidate_id))continue;if(['pending','approved_staging'].includes(r.status))d.candidate_summary.pending++;if(r.status==='written')d.candidate_summary.saved++}
    }catch(e){d.candidate_summary={unavailable:true}}
  }
  if(d.phase==='needs_update'&&['unprocessed','reading','read_complete'].some(k=>(d.counts?.[k]||0)>0)){
    const age=Date.now()-Date.parse(d.last_activity_at||'');
    if(Number.isFinite(age))d.phase=age<=300000?'running':'waiting_for_ai';
    d.next_round_changed_sources=d.next_round_changed_sources||d.counts?.stale||0;
  }
  return d;
}
function renderKnowledgeWiki(d){
  const signature=JSON.stringify([d,wikiViewGroup]);if(signature===wikiRenderSignature&&$('#wikiHub').dataset.loaded==='true'&&$('#wikiHub').querySelector('button'))return;wikiRenderSignature=signature;
  const host=$('#wikiHub'),connected=(d.connected_clients||[]).length>0;
  const started=!!(d.started_at||d.enabled||(d.jobs||[]).length),counts=d.counts||{},done=(counts.processed||0)+(counts.skipped||0),total=Object.values(counts).reduce((a,b)=>a+b,0),schedule=d.schedule||{},suggestions=d.candidate_summary||{};
  const manualRecovery=d.enabled&&d.phase!=='running'&&schedule.recovery?.available;
  const openDetails=new Set([...host.querySelectorAll('details[open][data-wiki-detail]')].map(n=>n.dataset.wikiDetail));
  const focused=host.contains(document.activeElement)?document.activeElement.id:'';
  const labels={paused:'Wiki整理已暂停',running:'正在整理',waiting_for_ai:'等待 AI 继续',failed:'整理遇到问题',needs_update:'有新内容待整理',up_to_date:'本批资料已检查',disabled:'尚未开始'};
  const groups=new Map();for(const page of d.pages||[]){const key=page.kind+' · '+page.group;if(!groups.has(key))groups.set(key,[]);groups.get(key).push(page)}
  if(!groups.has(wikiViewGroup))wikiViewGroup='';
  const copyButton=(label,primary=false)=>`<button id="wikiCopy" class="${primary?'primary':'secondary'}" type="button">${label}</button>`;
  const detail=`<details class="wiki-run-details" data-wiki-detail="run"><summary>整理详情</summary><div><p>${counts.reading||0} 份读取中 · ${counts.read_complete||0} 份处理中 · ${counts.failed||0} 份失败</p><p>每日任务：${esc(schedule.label||'未安排')}</p>${schedule.last_started_at?`<p>本轮开始：${esc(formatTime(schedule.last_started_at))}</p>`:''}${schedule.last_success_at?`<p>上次完成：${esc(formatTime(schedule.last_success_at))}</p>`:''}<p>${esc(schedule.failure||schedule.execution_notice||'每日更新尚未安排')}</p>${d.reason?`<p>${esc(d.reason)}</p>`:''}<p>来源阅读记录：${esc(d.review_receipt?.updated_at?formatTime(d.review_receipt.updated_at):'暂无')}</p><p>来源扫描和处理不等于知识已写入。此处仅统计本轮 Wiki 建议，全局建议在整理中心核对后保存。</p>${d.enabled&&!['failed','waiting_for_ai','needs_update'].includes(d.phase)?copyButton('复制继续整理指令'):''}</div></details>`;
  if(!started){
    host.innerHTML=`<section class="wiki-welcome"><span class="access-kicker">让知识逐渐积累</span><h2>把零散经验，整理成随时能用的知识</h2><p>让 AI 按项目和主题整理已有资料，保留每条知识的出处。</p><ol class="wiki-steps"><li class="${connected?'complete':''}"><span>1</span><div><strong>连接 AI</strong><small>${connected?'已连接，可以开始':'先在「AI 连接」中完成连接'}</small></div></li><li><span>2</span><div><strong>把指令发给 AI</strong><small>由 AI 开始整理</small></div></li><li><span>3</span><div><strong>回来查看知识</strong><small>按项目浏览，随时核对来源</small></div></li></ol>${connected?copyButton('复制启动指令',true):'<button id="wikiConnect" class="primary" type="button">去连接 AI</button>'}<p id="wikiActionFeedback" role="status"></p><p class="wiki-footnote">原有资料仍可在「知识」页查看。</p></section>`;
  }else{
    let daily=schedule.next_expected_at?'下次更新 '+formatTime(schedule.next_expected_at):schedule.last_success_at?'最近更新 '+formatTime(schedule.last_success_at):schedule.arrangement==='arranged'?'每日更新已安排':'每日更新未安排';
    if(!d.enabled)daily='恢复整理后继续更新';
    const recover=['paused','failed','waiting_for_ai','needs_update'].includes(d.phase),dailyIssue=d.enabled&&d.phase!=='running'&&['interrupted','failed','missing_receipt','unverified'].includes(schedule.execution_state);
    const nextSteps={paused:'仅暂停 Wiki 整理，归档和搜索仍可使用，已有进度已保留。恢复后等待 AI 接着处理。',failed:'把继续指令发给 AI，请它检查读取失败的来源后重试。',waiting_for_ai:'把继续指令发给 AI，从上次的位置接着整理。',needs_update:'资料有更新，把继续指令发给 AI 即可处理新增内容。'};
    const recovery=manualRecovery?`<div class="wiki-recovery" role="status"><p>${schedule.execution_state==='interrupted'?'本次自动整理已中断':'本次自动整理尚未确认完成'}，已有进度已保留。可把补跑指令发给原 AI，接着整理。</p>${copyButton('复制手动补跑指令')}</div>`:recover?`<div class="wiki-recovery" role="status"><p>${esc(nextSteps[d.phase])}</p>${d.phase==='paused'?'<button id="wikiResume" class="primary" type="button">恢复Wiki整理</button>':copyButton('复制继续整理指令')}</div>`:dailyIssue?`<div class="wiki-recovery" role="status"><p>每日更新暂未完成。把继续指令发给 AI，检查后接着整理。</p>${copyButton('复制继续整理指令')}</div>`:'';
    // Keep a single copy action when a daily warning is shown.
    const details=manualRecovery||(dailyIssue&&!recover)?detail.replace(copyButton('复制继续整理指令'),''):detail;
    host.innerHTML=`<div class="wiki-statusbar"><span><i aria-hidden="true"></i>${esc(labels[d.phase]||'整理中')}${total?' · 已处理来源 '+done+'/'+total+' 项':''}</span><span>${esc(daily)}</span>${d.next_round_changed_sources?`<span>${d.next_round_changed_sources} 项变更留待下一轮</span>`:''}<button id="wikiRefresh" class="wiki-text-button" type="button">刷新</button>${d.enabled?'<button id="wikiPause" class="wiki-text-button" type="button">暂停Wiki整理</button>':''}${suggestions.pending?`<button id="wikiReview" class="wiki-text-button" type="button">本轮 Wiki ${suggestions.pending} 条待核对</button>`:''}${suggestions.saved?`<span>本轮 Wiki ${suggestions.saved} 条已保存</span>`:''}${suggestions.unavailable?'<span>建议状态待刷新</span>':''}</div>${recovery}<div id="wikiCollection"></div>${details}<p id="wikiActionFeedback" role="status"></p>`;
    const collection=$('#wikiCollection');
    const pageRow=p=>`<article class="wiki-entry"><h3>${esc(p.title)}</h3><small>${esc(p.temporal_label||'记录时间待核实')}</small><div class="wiki-entry-body">${renderSafeMarkdown(p.body_preview||'')}</div><button class="wiki-document" data-wiki-path="${esc(p.path)}" type="button">${p.body_truncated?'继续阅读知识正文':'打开知识原页'}</button>${p.sources?.length?`<details data-wiki-detail="${esc(p.path)}"><summary>核对原话 · ${p.sources.length} 处引用</summary>${p.sources.map(s=>`<div class="wiki-citation"><blockquote>${esc(s.quote||'旧记录未保存引文')}</blockquote><button class="wiki-source-link" data-wiki-entry="${esc(p.path)}" data-wiki-source="${esc(s.source_id||'')}" data-wiki-fallback="${esc(s.path)}" type="button">查看原话与上下文</button></div>`).join('')}</details>`:'<small>来源待补充</small>'}</article>`;
    const entityHeader=e=>e?`<section class="wiki-entity-summary"><strong>${esc(e.status||'最近状态待核实')}</strong><p>${e.status_basis==='owner_confirmed'?'用户确认于 '+esc(e.confirmed_at):'历史资料不代表当前仍在推进。'}</p>${e.relationship_note?`<p>${esc(e.relationship_note)}</p>`:''}${e.phases?.length?`<details><summary>历史名称与阶段（可能重叠）</summary>${e.phases.map(p=>`<p>${esc(p.name||p.label||p.title||p.path||'')} · ${esc(p.description||p.note||'')}</p>`).join('')}</details>`:''}</section>`:'';
    if(wikiViewGroup){
      const pages=groups.get(wikiViewGroup);collection.innerHTML=`<div class="wiki-group-heading"><button id="wikiBack" class="wiki-text-button" type="button">← 返回项目与主题</button><h2>${esc(wikiViewGroup.split(' · ').slice(1).join(' · '))}</h2><p>${pages.length} 篇知识</p></div>${pages[0].group==='日常事项'?'':entityHeader(pages[0].entity)}<div class="wiki-entries">${pages.map(p=>(pages[0].group==='日常事项'?`<h3 class="wiki-daily-name">${esc(p.entity_name||p.title)}</h3>`:'')+pageRow(p)).join('')}</div>`;
      $('#wikiBack').onclick=()=>{wikiViewGroup='';renderKnowledgeWiki(wikiData)};
    }else{
      collection.innerHTML=`<div class="wiki-collection-heading"><h2>项目与主题</h2><span>${groups.size} 个分类 · ${(d.pages||[]).length} 篇知识</span></div>${groups.size?`<div class="wiki-group-grid">${[...groups].map(([key,pages])=>`<button class="wiki-group-card" data-wiki-group="${esc(key)}" type="button"><small>${esc(pages[0].kind)}</small><strong>${esc(pages[0].group)}</strong><span>${esc(pages[0].group==='日常事项'?'零散事项与历史记录':pages[0].entity?.status||'最近状态待核实')}</span><span>${pages.length} 篇知识 <b aria-hidden="true">→</b></span></button>`).join('')}</div>`:'<div class="wiki-empty"><h3>正在积累第一批知识</h3><p>整理后的内容保存后，会按项目和主题出现在这里。</p></div>'}${d.pages?.length?`<section class="wiki-recent"><h3>知识记录</h3><div>${d.pages.slice(0,3).map(p=>`<button class="wiki-recent-link" data-wiki-path="${esc(p.path)}" type="button"><span>${esc(p.title)}</span><small>${esc(p.group)}</small></button>`).join('')}</div>${d.pages.length>3?`<details data-wiki-detail="recent"><summary>展开更多记录</summary>${d.pages.slice(3,10).map(p=>`<button class="wiki-recent-link" data-wiki-path="${esc(p.path)}" type="button"><span>${esc(p.title)}</span><small>${esc(p.group)}</small></button>`).join('')}</details>`:''}</section>`:''}`;
      host.querySelectorAll('[data-wiki-group]').forEach(b=>b.onclick=()=>{wikiViewGroup=b.dataset.wikiGroup;renderKnowledgeWiki(wikiData)});
    }
    host.querySelectorAll('[data-wiki-source]').forEach(b=>b.onclick=()=>openWikiEvidence(b.dataset.wikiEntry,b.dataset.wikiSource,b.dataset.wikiFallback));
    host.querySelectorAll('[data-wiki-path]').forEach(b=>b.onclick=()=>openRetrievedSource(b.dataset.wikiPath));
  }
  host.dataset.loaded='true';host.querySelectorAll('details[data-wiki-detail]').forEach(n=>{n.open=openDetails.has(n.dataset.wikiDetail)});
  $('#wikiConnect')?.addEventListener('click',()=>switchPage('access'));$('#wikiReview')?.addEventListener('click',()=>switchPage('memory'));$('#wikiRefresh')?.addEventListener('click',loadKnowledgeWiki);
  $('#wikiCopy')?.addEventListener('click',async()=>{try{if(!await copyText(manualRecovery?schedule.recovery.instructions:d.instructions))throw Error('copy failed');$('#wikiActionFeedback').textContent=manualRecovery?'已复制，发给原 AI 即可接着整理。':started?'已复制，发给 AI 即可继续。':'已复制，发给已连接的 AI 即可开始。'}catch(e){$('#wikiActionFeedback').textContent='复制未完成，请再试一次。'}});
  $('#wikiPause')?.addEventListener('click',async event=>{event.currentTarget.disabled=true;try{await apiPost('/api/wiki/pause',{});await loadKnowledgeWiki()}catch(e){$('#wikiActionFeedback').textContent='暂停未完成，请刷新后重试。';event.currentTarget.disabled=false}});
  $('#wikiResume')?.addEventListener('click',async event=>{event.currentTarget.disabled=true;try{await apiPost('/api/wiki/resume',{});await loadKnowledgeWiki()}catch(e){$('#wikiActionFeedback').textContent='恢复未完成，请刷新后重试。';event.currentTarget.disabled=false}});
  if(focused)document.getElementById(focused)?.focus({preventScroll:true});
}
async function loadKnowledgeWiki(){
  clearTimeout(wikiRefreshTimer);const sequence=++wikiRequestSequence,navigation=pageNavigationSequence,host=$('#wikiHub');
  if(!host.dataset.loaded)host.innerHTML='<p role="status">正在加载…</p>';
  try{const raw=await api('/api/wiki/status',{ttl:0,force:true,scope:'wiki'});if(sequence!==wikiRequestSequence||navigation!==pageNavigationSequence||activePage!=='wiki')return;const d=await wikiCompatibilityStatus(raw);if(sequence!==wikiRequestSequence||navigation!==pageNavigationSequence||activePage!=='wiki')return;wikiData=d;renderKnowledgeWiki(d);wikiRefreshTimer=setTimeout(function poll(){if(activePage!=='wiki')return;if(document.querySelector('dialog[open]')){wikiRefreshTimer=setTimeout(poll,15000);return}loadKnowledgeWiki()},15000)}
  catch(e){if(sequence!==wikiRequestSequence||navigation!==pageNavigationSequence||activePage!=='wiki'||e.name==='AbortError')return;wikiRenderSignature='';host.innerHTML=`<section class="wiki-welcome" role="alert"><h2>${e.status===404?'知识 Wiki 等待后台更新':'暂时连不上 Wiki'}</h2><p>${e.status===404?'后台加载新版本后，刷新此页即可使用。':'请确认界渡正在运行，然后重试。已有知识会保留。'}</p><button id="wikiRetry" class="secondary" type="button">重试</button></section>`;$('#wikiRetry').onclick=loadKnowledgeWiki}
}

async function openWikiEvidence(entry,sourceId,path,reverify=false){
  sourceReaderCleanup?.();const seq=++knowledgeReadSequence,dialog=$('#knowledgeObjectDialog');
  $('#knowledgeObjectDialogKicker').textContent='知识出处';$('#knowledgeObjectDialogTitle').textContent='原话与上下文';$('#knowledgeObjectDialogError').textContent='';
  const primary=$('#knowledgeObjectDialogPrimary');primary.hidden=false;primary.disabled=false;primary.textContent='关闭';primary.onclick=()=>dialog.close();
  $('#knowledgeObjectDialogBody').textContent=reverify?'正在当前原档核对消息身份和原话…':'正在核对原始引用…';if(!dialog.open)dialog.showModal();
  try{
    const endpoint=reverify?'/api/wiki/evidence/reverify?':'/api/wiki/evidence?';
    const d=sourceId?await api(endpoint+new URLSearchParams({entry_path:entry,source_id:sourceId}),{ttl:0,force:true,scope:'wiki-evidence'}):{located:false,reason:'旧记录没有可核验的引用位置。',source_path:path};
    if(seq!==knowledgeReadSequence||!dialog.open)return;
    const current=d.verification==='current_reverification';
    const notice=current?(d.located?'已在当前原档重新核验。旧文件版本校验结果不变，以下定位仅对应当前原档。':'当前原档重新核验未通过：'+(d.reason||'原话位置待核实')):d.located?'已核对原始来源，以下为这条知识对应的原话。':d.reason||'原话位置待核实';
    $('#knowledgeObjectDialogBody').innerHTML=`<p class="knowledge-source-safety" role="status">${esc(notice)}</p><blockquote class="wiki-evidence-quote">${esc(d.quote||'旧记录未保存引文')}</blockquote>${d.located?`<details class="wiki-evidence-context"><summary>展开所在消息与前后文</summary><div>${esc(d.core||d.quote)}</div>${d.before?`<details><summary>更早的邻近内容</summary><p>${esc(d.before)}</p></details>`:''}${d.after?`<details><summary>后续的邻近内容</summary><p>${esc(d.after)}</p></details>`:''}<small>仅展示邻近上下文。</small></details>`:''}${!d.located&&d.quote&&sourceId?`<button id="wikiReverify" class="secondary" type="button">${current?'重新尝试核验':'在当前原档重新核验'}</button><p class="knowledge-source-safety">只核对这条引用的当前位置，不改写旧证据或知识正文。</p>`:''}<button id="wikiWholeConversation" class="secondary" type="button">打开完整会话</button><details><summary>来源信息</summary><p>${esc(d.source_path||path)}</p>${d.message_id?`<p>消息：${esc(d.message_id)}</p>`:''}${current&&d.located?`<p>当前核验时间：${esc(formatTime(d.checked_at))}</p><p>引文校验：${esc(d.quote_sha256)}</p><p>消息正文校验：${esc(d.message_content_sha256)}</p>`:''}</details>`;
    $('#wikiWholeConversation').onclick=()=>openRetrievedSource(d.source_path||path);
    $('#wikiReverify')?.addEventListener('click',()=>openWikiEvidence(entry,sourceId,path,true));
  }catch(e){if(seq===knowledgeReadSequence)$('#knowledgeObjectDialogError').textContent=reverify&&e.status===404?'当前后台尚未加载重新核验入口，原有引用与完整会话仍保留。':'暂时无法核对引用，请刷新后重试。'}
}
