/* Workspace-local prompt tools. Copy actions always fetch the current server state. */
(()=>{
  const $=selector=>document.querySelector(selector);
  const note=(host,message)=>{if(host)host.textContent=message};
  let lastGate=0,gateBusy=false;

  async function refreshGate(force=false){
    const host=$('#motherDistillation');if(!host||!$('#archives')?.classList.contains('active'))return;
    if(gateBusy||!force&&Date.now()-lastGate<12000)return;
    gateBusy=true;lastGate=Date.now();
    const message=$('#motherDistillationStatus'),button=$('#copyMotherPrompt');
    try{
      const state=await api('/api/prompt-workflow/distillation?t='+Date.now(),{ttl:0,force:true});
      button.disabled=!state.ready;
      const details=$('#motherDistillationDetails');details.hidden=state.ready;
      details.querySelector('p').textContent=(state.reasons||[]).join('；');
      note(message,state.ready?`已核对 ${state.sources.join('、')} 的历史补档和待核对项，可以复制提炼指令。`:
        state.sources?.length?'已接入的聊天历史仍在补齐，或有记录需要核对。完成后才能复制提炼指令。':'尚未接入可核验的聊天历史；请先完成归档。');
    }catch{button.disabled=true;note(message,'历史补档状态暂时无法核对，请稍后刷新。')}
    finally{gateBusy=false}
  }

  function attachMother(){
    const progress=$('#archiveProgress');if(!progress||$('#motherDistillation'))return;
    const card=document.createElement('section');card.id='motherDistillation';card.className='prompt-workflow-card';
    card.innerHTML='<div><h2>提炼母档</h2><p>全部已接入的历史来源完成补档后，复制指令交给你正在使用的 AI，按证据提出分类建议；正式知识仍须你审核。</p></div><p id="motherDistillationStatus" role="status">正在核对历史补档…</p><details id="motherDistillationDetails" hidden><summary>查看具体原因</summary><p></p></details><div class="prompt-workflow-actions"><button id="refreshMotherGate" type="button" class="copy-button">刷新条件</button><button id="copyMotherPrompt" type="button" class="primary" disabled>复制提炼母档提示词</button></div>';
    progress.after(card);
    $('#refreshMotherGate').onclick=()=>refreshGate(true);
    $('#copyMotherPrompt').onclick=async event=>{
      const button=event.currentTarget;button.disabled=true;
      try{const result=await api('/api/prompt-workflow/distillation/prompt?t='+Date.now(),{ttl:0,force:true});
        if(!await copyText(result.prompt))throw Error('复制失败');showToast('已复制；请交给已获准读取资料的 AI，候选仍需审核。');}
      catch(error){showToast(error.status===409?'历史补档状态已有变化，请刷新后重试。':'暂时无法复制，请重新核对补档状态。')}
      finally{refreshGate(true)}
    };
  }

  async function attachStartup(){
    const old=$('#knowledgeBaseStatus .startup-instruction');
    if(!old||old.dataset.promptAttached)return;
    old.dataset.promptAttached='true';
    const card=document.createElement('section');card.className='prompt-workflow-card startup-prompt-card';card.id='workspaceStartupPrompt';
    card.innerHTML='<div><h3>当前工作区的新会话提示词</h3><p>可从已确认的正式母档生成，在本机单独保存；切换工作区会读取对应版本。不会自动发给其他会话。</p></div><p id="startupPromptState" role="status">正在读取…</p><label for="startupPromptText">提示词正文</label><textarea id="startupPromptText" spellcheck="false" maxlength="12000"></textarea><small id="startupPromptSources"></small><div class="prompt-workflow-actions"><button id="generateStartupPrompt" type="button" class="copy-button">按正式母档生成并保存</button><button id="saveStartupPrompt" type="button" class="copy-button">保存修改</button><button id="restoreStartupPrompt" type="button" class="copy-button">恢复默认</button><button id="copyWorkspaceStartup" type="button" class="primary">复制当前提示词</button></div>';
    old.after(card);
    const state=$('#startupPromptState'),area=$('#startupPromptText'),sources=$('#startupPromptSources');
    async function load(){
      try{const data=await api('/api/prompt-workflow/startup?t='+Date.now(),{ttl:0,force:true});
        area.value=data.prompt||'';sources.textContent=(data.source_files?.length?'依据：'+data.source_files.join('、'):'')+
          (data.skipped_files?.length?`。${data.skipped_files.length} 份文件未收入或仅摘录前段；原因见提示词正文。`:'');
        note(state,data.mode==='generated'?'已从当前工作区正式母档生成并保存在本机。':data.mode==='custom'?'已保存你的修改。':'正在使用通用默认提示词。');
      }catch{area.value='';note(state,'工作区尚未加载，或提示词暂时无法读取。')}
    }
    $('#generateStartupPrompt').onclick=async event=>{const button=event.currentTarget;button.disabled=true;try{const data=await apiPost('/api/prompt-workflow/startup/generate',{});area.value=data.prompt;sources.textContent=(data.source_files?.length?'依据：'+data.source_files.join('、'):'')+(data.skipped_files?.length?`。${data.skipped_files.length} 份文件未收入或仅摘录前段；原因见提示词正文。`:'');note(state,'已从当前工作区正式母档生成并保存在本机。')}catch(error){note(state,error.message||'正式母档暂不可用；请先核对并批准母档。')}finally{button.disabled=false}};
    $('#saveStartupPrompt').onclick=async event=>{const button=event.currentTarget;button.disabled=true;try{await apiPost('/api/prompt-workflow/startup/save',{prompt:area.value});note(state,'已保存你的修改。')}catch(error){note(state,error.message||'保存失败，请检查提示词内容。')}finally{button.disabled=false}};
    $('#restoreStartupPrompt').onclick=async event=>{const button=event.currentTarget;button.disabled=true;try{await apiPost('/api/prompt-workflow/startup/restore-default',{});await load()}catch{note(state,'恢复默认失败，请重试。')}finally{button.disabled=false}};
    async function copyCurrent(){try{const data=await api('/api/prompt-workflow/startup?t='+Date.now(),{ttl:0,force:true});if(!await copyText(data.prompt||''))throw Error();showToast('已复制当前工作区的新会话提示词。')}catch{showToast('复制失败，请刷新后重试。')}}
    $('#copyWorkspaceStartup').onclick=copyCurrent;
    const legacy=$('#copyStartupInstruction');if(legacy)legacy.onclick=copyCurrent;
    await load();
  }

  async function attachWikiGuide(){
    const page=$('#wiki');if(!page||$('#wikiGuide'))return;
    const host=document.createElement('section');host.id='wikiGuide';host.className='prompt-workflow-card wiki-guide';
    host.innerHTML='<h2>知识 Wiki 使用说明</h2><p role="status">正在读取说明和每日安排…</p>';page.append(host);
    try{
      const [result,status]=await Promise.all([api('/api/wiki/guide',{ttl:0,force:true}),api('/api/wiki/status',{ttl:0,force:true})]);
      const guide=result.guide||{};
      const validPrompts=data=>{
        if(!data.workspace_id||!data.workspace_root||!data.daily_prompt||!data.schedule_setup_prompt)return false;
        const id='workspace_id='+JSON.stringify(data.workspace_id),root='workspace_path='+JSON.stringify(data.workspace_root);
        return [data.daily_prompt,data.schedule_setup_prompt].every(prompt=>prompt.includes(id)&&prompt.includes(root));
      };
      if(!validPrompts(result))throw Error('workspace-bound prompts unavailable');
      const scheduleText=d=>{
        const schedule=d.schedule||{},arrangement=schedule.arrangement;
        if(arrangement==='paused')return '你已暂停每日任务；原安排和进度保留，复制手动指令不会恢复它。';
        if(arrangement==='unavailable')return '每日任务状态暂时无法确认；请先核对现有任务，避免重复安排。';
        if(arrangement==='arranged'&&!d.enabled)return '每日任务已有安排，但 Wiki 整理当前暂停；原安排保留。';
        if(arrangement==='arranged'&&['failed','interrupted','missing_receipt','unverified'].includes(schedule.execution_state))return `每日任务已安排，但最近运行尚未确认完成：${schedule.failure||schedule.execution_notice||'请查看整理详情'}`;
        if(arrangement==='arranged')return `每日任务已安排：${schedule.label||'请查看本机运行记录'}`;
        return '每日自动任务尚未安排；复制指令只是手动整理。';
      };
      host.replaceChildren();
      const h=document.createElement('h2');h.textContent=guide.title||'知识 Wiki';host.append(h);
      for(const line of [guide.description,guide.model_choice])if(line){const p=document.createElement('p');p.textContent=line;host.append(p)}
      const label=document.createElement('h3');label.textContent='每天固定做什么';host.append(label);
      const list=document.createElement('ol');for(const line of guide.daily_steps||[]){const li=document.createElement('li');li.textContent=line;list.append(li)}host.append(list);
      const runDetails=document.createElement('details');runDetails.className='wiki-guide-run-summary';
      const runSummary=document.createElement('summary');runSummary.textContent='查看本工作区每日安排与运行摘要';runDetails.append(runSummary);
      const state=document.createElement('p');state.className='wiki-guide-schedule';state.textContent=scheduleText(status);runDetails.append(state);
      const refresh=document.createElement('button');refresh.type='button';refresh.className='copy-button';refresh.textContent='刷新运行状态';refresh.onclick=async()=>{try{const d=await api('/api/wiki/status?t='+Date.now(),{ttl:0,force:true});state.textContent=scheduleText(d)}catch{state.textContent='当前安排暂时无法核对，请稍后重试。'}};runDetails.append(refresh);host.append(runDetails);
      const intro=document.createElement('p');intro.textContent='下面分别是现在手动整理和安排每日任务。安排指令含完整每日执行正文；复制不会自动创建或运行任务。当前界渡可核验 Codex 的每日任务，其他客户端可先手动整理。';host.append(intro);
      const addPrompt=(title,notice,summary,id,text,buttonText,success)=>{
        const section=document.createElement('section');section.className='wiki-guide-prompt-option';
        const heading=document.createElement('h3');heading.textContent=title;section.append(heading);
        const explanation=document.createElement('p');explanation.textContent=notice;section.append(explanation);
        const details=document.createElement('details');details.className='wiki-guide-prompt-preview';
        const caption=document.createElement('summary');caption.textContent=summary;details.append(caption);
        const preview=document.createElement('pre');preview.id=id;preview.textContent=text;details.append(preview);section.append(details);
        const button=document.createElement('button');button.type='button';button.className='copy-button';button.textContent=buttonText;
        button.onclick=async()=>{button.disabled=true;try{
          const fresh=await api('/api/wiki/guide?t='+Date.now(),{ttl:0,force:true});
          if(!validPrompts(fresh))throw Error('workspace-bound prompts unavailable');
          document.getElementById('wikiManualPromptPreview').textContent=fresh.daily_prompt;
          document.getElementById('wikiSchedulePromptPreview').textContent=fresh.schedule_setup_prompt;
          if(!await copyText(preview.textContent))throw Error('copy failed');showToast(success);
        }catch{showToast('当前工作区指令无法核对，未复制；请刷新后重试。')}finally{button.disabled=false}};
        section.append(button);host.append(section);
      };
      addPrompt('现在手动整理',guide.manual_notice,'预览手动整理完整指令','wikiManualPromptPreview',result.daily_prompt,'复制手动整理指令','已复制手动指令；不会安排每日任务。');
      addPrompt('安排每日任务',guide.schedule_notice,'预览安排指令与每日执行正文','wikiSchedulePromptPreview',result.schedule_setup_prompt,'复制安排每日任务完整指令','已复制；交给已连接的 AI 后，请核对真实安排状态。');
    }catch{host.querySelector('p').textContent='当前工作区说明或指令暂时无法核对，请稍后刷新页面。'}
  }

  function setup(){attachMother();attachWikiGuide();attachStartup();
    const base=$('#knowledgeBaseStatus');if(base)new MutationObserver(()=>attachStartup()).observe(base,{childList:true,subtree:true});
    document.querySelector('[data-page="archives"]')?.addEventListener('click',()=>setTimeout(()=>refreshGate(true),0));
    setInterval(()=>refreshGate(),5000);
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',setup,{once:true});else setup();
})();
