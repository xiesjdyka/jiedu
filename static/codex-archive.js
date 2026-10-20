// Read-only status polling; importing is owned by the standalone worker.
let codexArchiveTimer = null, codexArchiveBusy = false;

function codexReferenceDescription(row) {
  const kind={uploaded:'上传附件',delivery_link:'交付文件链接',text_reference:'正文文件引用',legacy_reference:'旧文件引用'}[row.kind]||'文件引用';
  const reason=row.reason_detail||(row.reason==='incomplete_reference'?'原记录的文件位置不完整':'原记录指向的位置当前不可访问');
  return `${kind} · ${reason}`;
}

async function loadCodexArchiveStatus() {
  const box = document.querySelector('#codexArchiveStatus');
  if (!box || codexArchiveBusy) return;
  clearTimeout(codexArchiveTimer);
  const open = new Set([...box.querySelectorAll('details[open]')].map(x => x.dataset.codexDetails));
  const referenceDetailHTML=box.querySelector('#codexReferenceDetails')?.innerHTML||'';
  try {
    const d = await api('/api/codex-archive/summary', {force:true, ttl:0});
    if(d.recovery_required){
      box.innerHTML=`<div class="memory-test-empty" role="alert"><h3>归档进度需要恢复</h3><p>${esc(d.last_error)}</p><p>采集已停止保护原记录。这不是手动暂停，请先恢复并核对进度；不要重新初始化或重复导入。</p></div>`;
      return;
    }
    const stamp=Date.parse(d.last_sync_at||''),fresh=Number.isFinite(stamp)&&Date.now()-stamp>=0&&Date.now()-stamp<120000;
    const h = d.history || {}, counts = h.counts || {}, cursors = d.current_cursors || {}, f=d.format_review||{};
    const labels = {not_started:'尚未开始', paused:'已暂停，进度已保存', importing:'正在保存历史聊天', completed:'历史聊天已保存', completed_with_issues:'历史聊天需要核对'};
    const stateLabels = {complete:'正文已保存', synced:'正文已保存', reading:'继续导入中', pending:'等待导入', processing:'正在读取', needs_review:'正文完整性待核对', failed:'读取失败，可重试', source_missing:'来源暂时缺失', excluded:'已移出范围', partial_line_pending:'等待原消息写完'};
    const scanned = h.total_bytes ? Math.min(100, Math.floor(100*h.processed_bytes/h.total_bytes)) : 0;
    const referenceStats=d.attachment_references||{},referenceKinds=referenceStats.by_kind||{},upload=referenceKinds.uploaded||{},delivery=referenceKinds.delivery_link||{},textReference=referenceKinds.text_reference||{};
    const attachmentSaved = Math.max(0, (d.total_attachment_count||0)-(d.missing_attachment_count||0));
    const issues = (h.items||[]).filter(x => ['failed','needs_review','source_missing','excluded'].includes(x.status));
    const auditBusy=(f.pending_sources||0)>0, unresolved=d.needs_review_event_count||0;
    const bodiesVerified=f.started&&!auditBusy&&!f.failed_sources&&!unresolved&&f.completed_sources===f.sources;
    const heading=!d.enabled?'自动归档已暂停':!d.discovered_session_count?'未发现本机 Codex 会话来源':f.failed_sources?'正文核对遇到问题':auditBusy?'正在核对并补齐历史聊天':unresolved?'还有内容尚未确认':bodiesVerified?'历史聊天正文已核对':labels[h.status]||'正在读取状态';
    const resolved=f.resolved_categories||{}, internalCount=Object.entries(resolved).reduce((sum,[kind,count])=>sum+(kind==='public_message'?0:count),0);
    const auditPercent=f.target_bytes?Math.min(100,Math.floor(100*f.checked_bytes/f.target_bytes)):0;
    box.innerHTML = `
      <div class="codex-import-heading"><div><small>界渡后台 · Codex 自动归档</small><h3>${esc(heading)}</h3></div><span>${d.last_error?'采集异常，请检查':!d.enabled?'已暂停':!d.snapshot_available?'等待首次检查':!fresh?'最近采集待核验':'自动同步已开启'}</span></div>
      ${d.live_backlog?.pending_bytes?`<p class="codex-import-note" role="status">有新内容正在同步（${d.live_backlog.pending_bytes<1048576?Math.max(1,Math.ceil(d.live_backlog.pending_bytes/1024))+' KB':(d.live_backlog.pending_bytes/1048576).toFixed(1)+' MiB'}）。${d.archive_policy==='paused'?'继续归档后会从原进度续接。':'后台会接着保存，不需要重新导入。'}</p>`:''}
      <p class="codex-import-note">${bodiesVerified?'已逐项核对本批来源中的聊天正文；新消息继续自动保存。附件是否取得原件单独列出。':auditBusy?'正在对照原始记录，补齐旧格式聊天；已经保存的消息不会重复添加。':unresolved?'下列待核对内容尚不能算作已完整归档。源文件和已存记录均保留。':!d.discovered_session_count?'目前没有可读取的本机 Codex 会话；发现来源后界渡才会开始补齐和持续采集。':'界渡后台自动保存可读取的本机 Codex 对话，重启后从原进度继续。'}</p>
      ${f.started?`<label class="codex-import-progress">正文核对 · ${auditBusy?auditPercent+'%':bodiesVerified?'已完成':'有待确认内容'}<progress max="100" value="${auditPercent}"></progress></label><p class="codex-import-note">已核对 ${f.verified_session_ids?.length||0} 个会话，${f.completed_sources||0} / ${f.sources||0} 份来源。核对范围截止本批捕获的来源位置。</p>`:h.batch_id?`<label class="codex-import-progress">来源文件读取 · ${scanned}%<progress max="100" value="${scanned}"></progress></label><p class="codex-import-note">文件读取比例不等于聊天完整性；未知内容会单独提示。</p>`:'<p class="codex-import-note">先选择归档范围，界渡会保存首次导入进度。</p>'}
      <div class="knowledge-base-summary codex-import-summary">
        <div><span>已保存正文</span><strong>${d.total_archived_message_count??'待核对'} 条</strong></div>
        <div><span>本次补齐旧消息</span><strong>${f.repaired_messages||0} 条</strong></div>
        <div><span>文件引用未取得原件</span><strong>${referenceStats.unavailable_references??'待分类'} 次</strong></div>
        <div><span>${auditBusy?'旧记录待核对':'尚未识别的内容'}</span><strong>${unresolved} 项</strong></div>
        <div><span>读取失败的会话</span><strong>${d.failed_session_count??'待核对'} 个</strong></div>
        <div><span>最近检查</span><strong>${esc(formatTime(d.last_sync_at))}</strong></div>
      </div>
      ${(referenceStats.unavailable_references||0)>0?'<p class="codex-import-note">部分文件链接未取得原件；链接所在的聊天正文仍保留。缺失原件不计作正文丢失，详情见下方“附件原件情况”。</p>':''}
      ${f.errors?.length?`<div role="alert">${f.errors.map(x=>`<p class="codex-import-note">${esc(x.source_file)}：${esc(x.error)}</p>`).join('')}</div>`:''}
      ${f.started?`<details data-codex-details="format-explanation"><summary>核对了哪些内容？</summary><p>聊天正文按原会话、消息编号与来源位置核对。已识别的内部记录不放进聊天，也不算作遗漏。</p><p>已核清 ${internalCount} 项内部记录：用量统计 ${resolved.usage||0}，智能体内部通信 ${(resolved.agent_message||0)+(resolved.agent_routing||0)}，工具活动 ${resolved.tool_activity||0}，语音开始或结束 ${resolved.voice_lifecycle||0}。旧待核对记录和核对依据仍保留。</p><p>旧格式聊天已核清 ${resolved.public_message||0} 项；本次实际新增 ${f.repaired_messages||0} 条。剩余 ${unresolved} 项未确认内容不会标为完成。</p></details>`:''}
      ${h.deferred_partial_bytes?'<p class="codex-import-note">冻结批次时有消息尚未写完，将在原消息完整后续采。</p>':''}
      <div class="knowledge-base-actions">
        <button id="toggleCodexArchive" class="copy-button" ${!d.enabled&&d.resume_policy==='selected'&&!d.selected_session_ids?.length?'disabled':''}>${d.enabled?'暂停归档':'继续历史补齐＋新消息'}</button>
        <button id="syncCodexArchive" class="execute" ${d.enabled?'':'disabled'}>立即检查与重试</button>
        ${d.archive_policy!=='all'&&d.resume_policy!=='all'?'<button id="allCodexArchive" class="copy-button">启用自动归档：全部可读历史＋新消息</button>':''}
      </div>
      <p class="codex-import-note">暂停同时停止历史导入与新消息采集；已有档案和进度会保留。未取得的文件保留原路径或链接；重试时会核验原件，不能用同名新文件替代。</p>
      ${issues.length?`<details data-codex-details="issues"><summary>需要关注的来源 · ${issues.length}${h.details_limited?'（最多展示30份）':'份'}</summary><div class="interface-list">${issues.map(x=>`<div class="interface-row"><div><b>${esc((d.discovered_sessions||[]).find(s=>s.session_id===x.session_id)?.title||'未命名对话')}</b><small>${esc(stateLabels[x.status]||x.status)}${x.error?' · '+esc(x.error):''}</small><small class="selectable">${esc(x.source_file)}</small></div></div>`).join('')}</div></details>`:''}
      ${d.discovery_issues?.length?`<p class="codex-import-note">有 ${d.discovery_issues.length} 份来源无法识别或读取，未计入已完成范围。</p><details data-codex-details="discovery"><summary>查看无法读取的来源</summary>${d.discovery_issues.map(x=>`<p class="codex-import-note">${esc(x.source_file)} · ${esc(x.reason)}</p>`).join('')}</details>`:''}
      <details data-codex-details="scope"><summary>归档范围 · ${d.authorized_session_count||0} / ${d.discovered_session_count||0} 个会话</summary><p>${d.archive_policy==='all'||d.resume_policy==='all'?'包含以后新增的本地对话；已移出的会话继续排除。':'仅归档下面选中的会话。'}</p><div class="interface-list">${(d.discovered_sessions||[]).map(x=>{const c=cursors[x.session_id]||{};return `<div class="interface-row"><div><b>${esc(x.title)}</b><small>${x.authorized?`${esc(stateLabels[c.body_status]||'等待导入')} · ${c.archived_message_count||0} 条正文${c.missing_attachment_count?' · '+c.missing_attachment_count+' 条文件引用未取得原件':''}${c.attachment_failed_count?' · 附件保存失败':''}`:'不在归档范围'} · ${x.fragment_count||1} 份日志</small>${c.last_error?`<small>${esc(c.last_error)}</small>`:''}</div><button class="copy-button" data-codex-session="${esc(x.session_id)}" data-authorized="${x.authorized?'true':'false'}">${x.authorized?'移出范围':'加入归档'}</button></div>`}).join('')||'<p>暂未发现本地 Codex 对话。</p>'}</div></details>
      <details data-codex-details="attachment-references"><summary>附件原件情况 · ${referenceStats.unavailable_references||0} 次引用未取得 / ${referenceStats.unique_unavailable_sources||0} 个不同文件来源</summary><p>${esc(referenceStats.notice||'引用尚未分类，请先核对来源。')}</p>${referenceStats.recovery_review?.reviewed?`<p>已逐条核对 ${referenceStats.recovery_review.reviewed} 次历史引用，已补回 ${referenceStats.recovery_review.recovered} 次引用的可核验内容${referenceStats.recovery_review.rendered_image_copies?`（其中 ${referenceStats.recovery_review.rendered_image_copies} 次为当时工具返回的图片副本，不能保证原文件编码相同）`:""}。其余保留原链接与具体原因；同一文件可能在不同日期被多次引用。</p>`:''}<button id="loadCodexReferenceDetails" class="copy-button">查看具体文件与原因</button><div id="codexReferenceDetails"></div>${(referenceStats.unavailable||[]).map(row=>`<p class="selectable">${esc({uploaded:'上传附件',delivery_link:'交付链接',text_reference:'正文引用',legacy_reference:'旧引用，类别待核对'}[row.kind]||'类别待核对')} · ${esc(row.original_name||row.reference)} · ${esc(codexReferenceDescription(row))}<br>${esc(row.title||'')} ${esc(row.message_timestamp?formatTime(row.message_timestamp):'')}<br>${esc(row.source_path||row.reference)}</p>`).join('')}</details>
      <p class="codex-import-note">${esc(d.notice||'')} · 状态时间 ${esc(formatTime(d.snapshot_updated_at||d.last_sync_at))}</p>
      <p id="codexArchiveFeedback" role="status" aria-live="polite"></p>`;
    box.querySelectorAll('details').forEach(x => {x.open=open.has(x.dataset.codexDetails)});
    box.querySelector('#codexReferenceDetails').innerHTML=referenceDetailHTML;
    async function action(button, route, payload, message) {
      codexArchiveBusy=true; button.disabled=true;
      const feedback=box.querySelector('#codexArchiveFeedback');feedback.textContent='正在处理这一批，请稍候…';
      try {
        const result=await apiPost('/api/codex-archive/'+route, {...payload,owner_confirmed:true});
        const failure=(result.sessions||[]).some(x=>x.status==='failed');
        showToast(route==='sync'?(result.status==='busy'?'归档正在处理，稍后自动刷新':`本次新增 ${result.saved_messages||0} 条${failure?'，部分项目仍需重试':''}`):message);
        codexArchiveBusy=false;await loadCodexArchiveStatus();
      } catch(e) {feedback.textContent='操作未完成：'+e.message;button.disabled=false;codexArchiveBusy=false;if(route==='configure'&&payload.policy==='all')throw e;}
    }
    box.querySelector('#loadCodexReferenceDetails').onclick=async event=>{const button=event.currentTarget;button.disabled=true;const host=box.querySelector('#codexReferenceDetails');host.textContent='正在核对文件引用详情…';try{const full=await api('/api/codex-archive/status',{force:true,ttl:0});const currentHost=box.querySelector('#codexReferenceDetails');if(!currentHost)return;currentHost.innerHTML=(full.attachment_references?.unavailable||[]).map(row=>`<p class="selectable">${esc(row.original_name||row.reference)} · ${esc(codexReferenceDescription(row))}<br>${esc(row.title||'')} ${esc(row.message_timestamp?formatTime(row.message_timestamp):'')}<br>${esc(row.source_path||row.reference)}</p>`).join('')||'<p>本次没有未取得原件的文件引用。</p>'}catch(error){if(error.name!=='AbortError')host.textContent='详情读取失败，请重试。'}finally{button.disabled=false}};
    box.querySelector('#toggleCodexArchive').onclick=e=>action(e.currentTarget,'configure',{enabled:!d.enabled},d.enabled?'已暂停，进度保留':'将从保存的进度继续');
    box.querySelector('#syncCodexArchive').onclick=e=>action(e.currentTarget,'sync',{},'');
    box.querySelector('#allCodexArchive')?.addEventListener('click',e=>{
      const button=e.currentTarget;
      openCodexAllConfirm(button,()=>action(button,'configure',{policy:'all'},'已启用全部本地对话归档'));
    });
    box.querySelectorAll('[data-codex-session]').forEach(button=>button.onclick=()=>{
      const id=button.dataset.codexSession, authorized=button.dataset.authorized==='true';
      action(button,'configure',{authorize:authorized?[]:[id],exclude:authorized?[id]:[],include:authorized?[]:[id]},authorized?'已停止此会话后续归档':'已加入归档范围');
    });
  } catch(e) {
    if(e.name!=='AbortError')box.innerHTML=`<div class="memory-test-empty"><span>Codex 归档状态暂时无法读取</span><p>${esc(e.message)}</p></div>`;
  } finally {
    if(document.querySelector('#archives.active'))codexArchiveTimer=setTimeout(()=>{if(document.querySelector('#archives.active'))loadCodexArchiveStatus()},5000);
  }
}

function openCodexAllConfirm(trigger,submit){
  let dialog=document.querySelector('#codexAllConfirm');
  if(!dialog){dialog=document.createElement('dialog');dialog.id='codexAllConfirm';dialog.className='hermes-history-confirm';dialog.setAttribute('aria-labelledby','codexAllConfirmTitle');dialog.innerHTML='<h2 id="codexAllConfirmTitle">归档全部本地 Codex 对话？</h2><p>包含界渡已识别且可读取的本机历史来源，以及以后新增的可读消息；已移出的会话继续排除。保存原始正文与明确的附件引用，不自动提炼知识。</p><p id="codexAllConfirmFeedback" role="status"></p><div class="hermes-history-confirm-actions"><button type="button" id="codexAllCancel" class="secondary">取消</button><button type="button" id="codexAllCommit" class="primary">确认归档全部</button></div>';document.body.append(dialog)}
  const cancel=dialog.querySelector('#codexAllCancel'),commit=dialog.querySelector('#codexAllCommit'),feedback=dialog.querySelector('#codexAllConfirmFeedback');let busy=false;
  cancel.disabled=false;commit.disabled=false;feedback.textContent='';commit.textContent='确认归档全部';
  cancel.onclick=()=>dialog.close();dialog.oncancel=event=>{if(busy)event.preventDefault()};dialog.onclose=()=>{if(trigger?.isConnected)trigger.focus({preventScroll:true})};
  commit.onclick=async()=>{if(busy)return;busy=true;cancel.disabled=true;commit.disabled=true;commit.textContent='正在确认…';try{await submit();dialog.close()}catch(error){feedback.textContent='操作未完成：'+error.message}finally{busy=false;cancel.disabled=false;commit.disabled=false;commit.textContent='确认归档全部'}};
  dialog.showModal();requestAnimationFrame(()=>cancel.focus());
}


