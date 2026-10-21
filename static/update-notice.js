/* One update summary per verified installed version. State lives in the app, not browser storage. */
(() => {
  const panel = document.getElementById('updateNotice');
  const settings = document.getElementById('updateNoticeSettings');
  if (!panel || !settings) return;
  let installation = null, health = null, checks = null, polls = 0;
  const $ = selector => panel.querySelector(selector);
  const labels = {codex:'ChatGPT 工作（Codex）', hermes:'Hermes'};
  const versionParts = value => String(value || '').split('.').map(x => Number(x) || 0);
  const older = (left, right) => { const a=versionParts(left),b=versionParts(right); for(let i=0;i<3;i++){if((a[i]||0)!==(b[i]||0))return (a[i]||0)<(b[i]||0)}return false };
  const visibleChanges = notice => {
    const previous = notice.previous_version;
    const rows = [];
    if (!previous || older(previous,'1.1.5')) rows.push('可读取的本机聊天历史会显示补齐进度；普通文件也能单独建立检索。');
    if (!previous || older(previous,'1.1.6')) rows.push('更新后会核对本机功能与保留的设置，告诉你哪些仍需决定。','知识 Wiki 增加用途、模型选择和每日整理说明。','已接入历史全部补齐并无待核对项后，可复制提炼母档指令。','可为当前工作区生成、修改并保存新会话启动提示词。');
    return rows;
  };
  function sourcePresent(source, client) {
    if (client?.installed === false) return false;
    return client?.installed === true || !!(source?.detected && source?.readable);
  }
  function summarize(notice, results) {
    const sourceData = results[0].status==='fulfilled' ? results[0].value : null;
    const clientData = results[1].status==='fulfilled' ? results[1].value : null;
    const accessData = results[2].status==='fulfilled' ? results[2].value : null;
    const indexData = results[3].status==='fulfilled' ? results[3].value : null;
    const facts = [], found = [], configure = [];
    const sources = sourceData?.sources || [], clients = clientData?.clients || [];
    for (const id of ['codex','hermes']) {
      const source=sources.find(x=>x.id===id), client=clients.find(x=>x.id===id);
      if (!sourcePresent(source,client)) continue;
      found.push(labels[id]);
      const paused=source?.history?.status==='paused'||source?.live?.status==='paused';
      if (paused) facts.push({kind:'preserved',text:`${labels[id]} 的自动保存仍按你原来的设置暂停。要开启时请在界渡里自行选择。`});
      else if (sourceData && source?.configured) facts.push({kind:'ok',text:`${labels[id]} 的本机聊天来源已连接，界渡会按现有范围继续处理。`});
      else if (sourceData) { facts.push({kind:'needs-choice',text:`${labels[id]} 已在本机找到，聊天保存还需要检查连接。`}); configure.push(labels[id]); }
      else facts.push({kind:'unknown',text:`${labels[id]} 已找到，但聊天保存状态暂时无法确认。`});
      if (clientData && client?.configured === false && !configure.includes(labels[id])) {
        facts.push({kind:'needs-choice',text:`${labels[id]} 的知识读取连接还没有配置。`}); configure.push(labels[id]);
      } else if (!clientData && !configure.includes(labels[id])) {
        facts.push({kind:'unknown',text:`${labels[id]} 的知识读取连接暂时无法确认。`}); configure.push(labels[id]);
      } else if (client?.configured && accessData) {
        const registered=(accessData.registered||[]).find(row=>row.claimed_id===id&&row.status!=='revoked');
        if (registered?.last_read_at) facts.push({kind:'ok',text:`${labels[id]} 已完成一次获授权资料的真实读取。`});
        else { facts.push({kind:'needs-choice',text:`${labels[id]} 已配置，但还没有看到真实读取成功的记录。`}); if(!configure.includes(labels[id]))configure.push(labels[id]); }
      }
    }
    if (sourceData && !found.length) facts.push({kind:'unknown',text:'本机尚未发现可读取的聊天客户端。安装后可在设置里重新检查。'});
    if (!sourceData && !clientData) facts.push({kind:'unknown',text:'本机聊天来源暂时无法检查；这不代表连接已经完成。'});
    const ordinaryCount=indexData?.content_markdown_count ?? indexData?.markdown_count ?? indexData?.scan?.markdown_count;
    if (ordinaryCount === 0) facts.push({kind:'ok',text:'当前工作区还没有普通资料；暂时无需建立检索。'});
    else if (indexData?.workspace_index?.vector_ready === true) facts.push({kind:'ok',text:'普通资料已能按关键词和意思搜索。'});
    else if (indexData?.workspace_index?.ready === true) facts.push({kind:'optional',text:'普通资料已能按关键词搜索；想按意思搜索，可在“知识”页建立语义索引。'});
    else if (ordinaryCount > 0 && indexData?.workspace_index) facts.push({kind:'needs-choice',text:'工作区已有普通资料，检索尚未建立；需要时可在“知识”页建立索引。'});
    else facts.push({kind:'unknown',text:'资料检索状态暂时无法确认；稍后可重新检查。'});
    if (accessData) {
      const denied=(accessData.registered||[]).flatMap(row=>row.policies||[]).filter(row=>row.decision==='deny').length;
      facts.push({kind:denied?'preserved':'ok',text:denied?`已有 ${denied} 项关闭的权限，界渡已保留你的选择。`:'现有权限保持不变；这次更新不会自动扩大读取范围。'});
    } else facts.push({kind:'unknown',text:'权限状态暂时无法确认；原有允许、拒绝和范围设置不会被改动。'});
    const unknown = facts.some(fact=>fact.kind==='unknown') || results.some(result=>result.status!=='fulfilled');
    const needsChoice = facts.some(fact=>fact.kind==='needs-choice');
    const optional = facts.some(fact=>fact.kind==='optional');
    const preserved = facts.some(fact=>fact.kind==='preserved');
    const prompt = configure.length ? `请帮我检查界渡 ${notice.version} 的本机连接，只处理这台电脑已发现的：${configure.join('、')}。先在界渡正式界面核对实际安装位置、已连接的工作区和现有设置。只配置尚未连接的客户端；对已经授权的一小段资料做一次真实读取核验。配置文件存在或复制了这段话都不算完成。不要改源码或开发环境；配对秘密只通过界渡正式流程处理，不在报告中公开密钥或私人正文。保留我已经设定的拒绝、暂停、归档范围和历史记录；如果需要扩大权限或恢复自动保存，先说明影响，等我自己决定。最后用短句告诉我：已验证、仍需我选择、暂时无法确认的事项。` : '';
    return {facts,found,configure,prompt,unknown,needsChoice,optional,preserved};
  }
  function rows(host, values, className='') {
    host.replaceChildren();
    for (const value of values) { const row=document.createElement(className==='list'?'li':'p'); row.textContent=className==='list'?value:value.text; if(className!=='list')row.className=value.kind; host.append(row); }
  }
  async function refresh() {
    $('#updateNoticeCheck').textContent='正在检查本机来源、资料检索和现有权限…';
    $('#updateNoticeRetry').disabled=true;
    const results=await Promise.allSettled([
      api('/api/local-sources/status?t='+Date.now(),{ttl:0,force:true,scope:'health'}),
      api('/api/local-ai/status?t='+Date.now(),{ttl:0,force:true,scope:'health'}),
      api('/api/client-access/status?t='+Date.now(),{ttl:0,force:true,scope:'health'}),
      api('/api/knowledge-base?t='+Date.now(),{ttl:0,force:true,scope:'health'})]);
    checks=summarize(installation,results);
    rows($('#updateNoticeFacts'),checks.facts);
    $('#updateNoticeCheck').textContent=checks.unknown?'有些状态暂时无法确认。你可以继续使用，稍后再检查。':checks.needsChoice?'有连接或检索需要检查；你原来的设置已保留。':checks.optional?'已可使用；按意思搜索可以稍后设置。':checks.preserved?'更新已生效；原有暂停和权限选择已保留，无需操作。':'已生效，无需操作。';
    $('#updateNoticePromptWrap').hidden=!checks.prompt;
    $('#updateNoticePrompt').value=checks.prompt;
    $('#updateNoticeTechnical').textContent=`本机已安装 ${installation.version}；此前版本 ${installation.previous_version||'无法确认'}。检测到：${checks.found.join('、')||'暂无'}。仅核对状态，未更改权限、资料、归档范围或进度。`;
    $('#updateNoticeRetry').disabled=false;
  }
  function reveal() {
    $('#updateNoticeTitle').textContent=`界渡已更新到 ${installation.version}`;
    $('#updateNoticeIntro').textContent='更新已装好。这里告诉你现在能用什么，以及有没有需要你决定的设置。关闭后也能在“本机设置”里重看。';
    rows($('#updateNoticeChanges'),visibleChanges(installation),'list');
    $('#updateNoticeSaveState').textContent='';
    document.getElementById('brandSequence')?.classList.add('done');
    panel.hidden=false;document.body.classList.add('first-run-open');
    document.querySelector('.sidebar').inert=true;document.querySelector('body>main').inert=true;
    $('#updateNoticeClose').focus({preventScroll:true});
    refresh();
  }
  async function dismiss() {
    panel.hidden=true;document.body.classList.remove('first-run-open');
    document.querySelector('.sidebar').inert=false;document.querySelector('body>main').inert=false;
    try { installation=await apiPost('/api/update-notice/acknowledge',{}); }
    catch { const message='关闭状态暂时没保存；下次启动可能再提醒。'; document.getElementById('updateNoticeSettings').querySelector('p').textContent=message; showToast(message); }
  }
  async function reloadState({automatic=false}={}) {
    try { installation=await api('/api/update-notice?t='+Date.now(),{ttl:0,force:true,scope:'health'}); }
    catch { installation={state:'unconfirmed',message:'安装状态暂时无法读取，请稍后重试。'}; }
    settings.hidden=installation.kind!=='upgrade'&&!['unconfirmed','pending_confirmation'].includes(installation.state);
    if (['unconfirmed','pending_confirmation'].includes(installation.state)) {
      settings.querySelector('p').textContent=installation.message;
      if (automatic && polls++<40) setTimeout(()=>reloadState({automatic:true}),3000);
      return;
    }
    if (installation.state==='ready'&&installation.kind==='upgrade') {
      settings.querySelector('p').textContent=`已安装 ${installation.version}；可以重看本次更新和检查结果。`;
      if (automatic&&installation.show_update&&!document.getElementById('firstRunGuide').hidden) return;
      if (automatic&&installation.show_update) reveal();
    }
  }
  $('#updateNoticeClose').addEventListener('click',dismiss);
  $('#updateNoticeRetry').addEventListener('click',refresh);
  $('#updateNoticeCopy').addEventListener('click',()=>copyText($('#updateNoticePrompt').value));
  document.getElementById('reopenUpdateNotice').addEventListener('click',async()=>{await reloadState();if(installation.state==='ready'&&installation.kind==='upgrade')reveal();});
  window.jieduUpdateNotice={initialize:async (currentHealth,initial)=>{
    health=currentHealth;installation=initial;
    settings.hidden=initial?.kind!=='upgrade'&&!['unconfirmed','pending_confirmation'].includes(initial?.state);
    if(initial?.state==='ready'&&initial.kind==='upgrade') {
      settings.querySelector('p').textContent=`已安装 ${initial.version}；可以重看本次更新和检查结果。`;
      if(initial.show_update)reveal();
    } else if(['unconfirmed','pending_confirmation'].includes(initial?.state)) {
      settings.querySelector('p').textContent=initial.message;
      setTimeout(()=>reloadState({automatic:true}),3000);
    }
  },summarize,visibleChanges};
})();
