(() => {
  let selected='',stream=null,epoch=0,job=null,submitting=false,composing=false,imeUntil=0,follow=true,elapsedTimer=null;
  let settingsLoaded=false,sessionRows=[],creatingSession=null;
  let sourceURL='',sourceCursor=null,sourceEpoch=0;
  const completedJobs=new Set(),knownJobs=new Map();
  const thoughts=new Map(),translations=new Map();
  const nodes=new Map(),drafts=new Map(),terminal=new Set(['completed','failed','canceled','interrupted']);
  const memoryKey=()=>`jiedu.butler.session.${workspaceRuntime?.workspace_id||'local'}`;
  const inputDrafts=new Map();
  const draftKey=(sid=selected)=>memoryKey()+'.draft.'+(sid||'new');
  function inputDraft(key=draftKey()){if(!inputDrafts.has(key)){let text='';try{text=sessionStorage.getItem(key)||''}catch(_){}inputDrafts.set(key,{text,revision:0})}return inputDrafts.get(key)}
  function saveInputDraft(key,text,revision=null){const old=inputDraft(key),row={text,revision:revision??old.revision+1};inputDrafts.set(key,row);try{sessionStorage.setItem(key,text)}catch(_){}return row}
  function restoreInputDraft(){const input=$('#butlerQuestion');input.value=inputDraft().text;resizeInput();renderAttachmentDrafts();busy()}
  function clearSubmittedDraft(ticket){const row=inputDraft(ticket.key);if(row.revision!==ticket.revision||row.text!==ticket.text)return;saveInputDraft(ticket.key,'');if(draftKey()===ticket.key){$('#butlerQuestion').value='';resizeInput();busy()}}

  function newSessionPending(){return creatingSession?.key===draftKey()&&creatingSession.workspace===memoryKey()&&creatingSession.version===epoch}

  const box=$('#butlerMessages'),reading=$('#butlerReading'),errorText=e=>protectionMessage(e,'暂时无法完成，请重试');
  function button(text,action,cls='butler-text-button'){const b=document.createElement('button');b.type='button';b.className=cls;b.textContent=text;b.onclick=action;return b;}
  const iconNames=new Set(['arrow-up','stop','ellipsis-horizontal','bars-3','x-mark','plus','adjustments-horizontal','arrow-left','chevron-down','clock','book-open','light-bulb','pencil-square','arrow-down','document-text','trash','clipboard-document','paper-clip']);
  function icon(name){const span=document.createElement('span');span.className='butler-icon';span.setAttribute('aria-hidden','true');if(iconNames.has(name))span.style.maskImage=`url('/static/vendor/heroicons/${name}.svg')`;return span;}
  document.querySelectorAll('#butler [data-butler-icon]').forEach(node=>{const name=node.dataset.butlerIcon;if(node.tagName==='SPAN'){node.style.maskImage=`url('/static/vendor/heroicons/${name}.svg')`;node.setAttribute('aria-hidden','true')}else node.replaceChildren(icon(name))});
  const imageDrafts=new Map(),MAX_IMAGES=4,MAX_IMAGE_BYTES=8*1024*1024,MAX_IMAGE_TOTAL=16*1024*1024;
  let imageSessionPromise=null,imageRestoreSequence=0;
  const imageRestoreRequests=new Map(),imageDraftInfo=new Map(),imageRemovedIds=new Map();
  function attachmentRows(key=draftKey()){if(!imageDrafts.has(key))imageDrafts.set(key,[]);return imageDrafts.get(key)}
  function imageName(row){return row.file?.name||row.metadata?.filename||'图片'}
  function imageBytes(row){return Number(row.file?.size??row.metadata?.bytes)||0}
  function imageRestoring(key=draftKey()){return imageRestoreRequests.has(key)}
  function imageLocked(){return submitting||!!(job&&!terminal.has(job.state))}
  function imageMutationLocked(key=draftKey()){return imageLocked()||imageRestoring(key)||(key===draftKey()&&newSessionPending())}
  function rememberRemovedImage(key,id){if(!id)return;if(!imageRemovedIds.has(key))imageRemovedIds.set(key,new Set());imageRemovedIds.get(key).add(id)}
  function releaseImagePreview(row){if(row.preview?.startsWith('blob:'))URL.revokeObjectURL(row.preview)}
  function visionSupported(){return window.ButlerControls?.visionSupported?.()===true}
  const imagePreview=document.createElement('dialog');imagePreview.id='butlerImagePreview';imagePreview.className='butler-dialog';imagePreview.setAttribute('aria-label','图片预览');
  imagePreview.innerHTML='<form method="dialog" class="butler-dialog-head"><h2 id="butlerPreviewName">图片预览</h2><button class="butler-icon-button" aria-label="关闭图片预览"></button></form><img id="butlerPreviewImage" alt="">';imagePreview.querySelector('button').append(icon('x-mark'));$('#butler').append(imagePreview);
  imagePreview.addEventListener('close',()=>$('#butlerPreviewImage').removeAttribute('src'));
  function showImage(url,name){const img=$('#butlerPreviewImage');img.src=url;img.alt=name||'已选择的图片';$('#butlerPreviewName').textContent=name||'图片预览';if(!imagePreview.open)imagePreview.showModal()}
  function storedImageURL(value){try{const url=new URL(value,location.origin);return url.origin===location.origin&&url.pathname.startsWith('/api/butler/sessions/')?url.pathname+url.search:null}catch(_){return null}}
  function imageControls(){
    const rows=attachmentRows(),locked=imageMutationLocked(),supported=visionSupported(),help=$('#butlerAttachmentHelp'),info=imageDraftInfo.get(draftKey());
    $('#butlerAttach').disabled=locked||!supported;$('#butlerImageInput').disabled=locked||!supported;
    $('#butlerAttach').title=supported?'添加图片 · 界渡应用上限：4张、每张8 MiB、总计16 MiB':'当前模型未确认支持图片；请选择支持图片的外部模型';
    for(const b of $('#butlerAttachmentDrafts').querySelectorAll('[data-image-mutation]'))b.disabled=locked;
    const notes=[];
    if(imageRestoring())notes.push('正在恢复本对话未发送的图片…');
    else if(info?.error)notes.push('图片草稿读取失败，请重新打开本对话或刷新后重试。');
    if(rows.length)notes.push(rows.some(r=>r.state==='failed')?'图片上传失败，请重试或移除。':rows.some(r=>r.state==='uploading')?'正在上传图片，完成后即可发送。':!supported?'当前模型不支持这些图片，请切换模型或移除图片。':!$('#butlerQuestion').value.trim()?'只发送图片时，管家将描述图片内容。':'按原图发送，不自动OCR。');
    if(info?.extraCount)notes.push(`另有 ${info.extraCount} 个附件的状态暂未确认，原件已保留并计入容量，不能自动发送。`);
    if(rows.length||info?.extraCount)notes.push(`界渡应用上限：4张、每张8 MiB、总计16 MiB。未发送的图片草稿保留${info?.retentionHours||24}小时，已发送原图不受此规则影响。`);
    help.textContent=notes.join(' ');
    help.hidden=!help.textContent;
  }
  function renderAttachmentDrafts(){
    const host=$('#butlerAttachmentDrafts');host.replaceChildren();const rows=attachmentRows();host.hidden=!rows.length;
    for(const row of rows){
      const card=document.createElement('div');card.className='butler-attachment-card';card.dataset.state=row.state;
      const filename=imageName(row),thumb=button('',()=>showImage(row.preview,filename),'butler-attachment-thumb');thumb.setAttribute('aria-label','预览 '+filename);const img=document.createElement('img');img.src=row.preview;img.alt=filename;thumb.append(img);
      const copy=document.createElement('div');copy.className='butler-attachment-copy';const name=document.createElement('strong');name.textContent=filename;name.title=filename;const status=document.createElement('small');status.textContent=row.state==='uploading'?'正在上传…':row.state==='removing'?'正在移除…':row.state==='failed'?(row.file?'上传失败':'请重新选择原图'):`${(imageBytes(row)/1024/1024).toFixed(1)} MiB · 已上传`;if(row.error)status.title=row.error;copy.append(name,status);
      if(row.state==='failed'&&row.file){const retry=button('重试上传',()=>uploadImage(row,row.key));retry.dataset.imageMutation='true';copy.append(retry)}
      const remove=button('',()=>removeImage(row,row.key),'butler-attachment-remove');remove.append(icon('x-mark'));remove.dataset.imageMutation='true';remove.setAttribute('aria-label','移除 '+filename);remove.title='移除图片';remove.disabled=imageMutationLocked();card.append(thumb,copy,remove);host.append(card);
    }imageControls();
  }
  function forgetImage(row,key){const rows=attachmentRows(key),index=rows.indexOf(row);if(index>=0)rows.splice(index,1);releaseImagePreview(row);if(draftKey()===key){renderAttachmentDrafts();busy()}}
  async function restoreAttachmentDrafts(sid){
    if(!sid)return;const key=draftKey(sid),workspace=memoryKey(),version=++imageRestoreSequence,startRows=new Set(attachmentRows(key)),pendingRows=new Set([...startRows].filter(row=>row.state!=='ready'));
    imageRestoreRequests.set(key,version);if(draftKey()===key){renderAttachmentDrafts();busy()}
    const current=()=>imageRestoreRequests.get(key)===version&&memoryKey()===workspace&&selected===sid&&activePage==='butler';
    try{
      const data=await api(`/api/butler/sessions/${sid}/attachments`,{ttl:0,force:true});if(!current())return;
      if(!Array.isArray(data.attachments))throw Error('图片草稿返回格式无效');
      const rows=attachmentRows(key),byId=new Map(rows.filter(row=>row.metadata?.id).map(row=>[row.metadata.id,row])),removed=imageRemovedIds.get(key)||new Set(),listed=[],seen=new Set();
      for(const metadata of data.attachments){
        const preview=storedImageURL(metadata?.url);if(!metadata?.id||metadata.session!==sid||!preview||!Number.isFinite(metadata.bytes)||metadata.bytes<=0||metadata.bytes>MAX_IMAGE_BYTES||seen.has(metadata.id))continue;
        seen.add(metadata.id);if(removed.has(metadata.id))continue;const old=byId.get(metadata.id);if(old?.cancelled||old?.state==='removing')continue;
        listed.push(old?Object.assign(old,{metadata}):{file:null,key,sid,metadata,preview,state:'ready',error:'',cancelled:false});
      }
      const keep=new Set(listed);for(const row of rows)if(!startRows.has(row)||pendingRows.has(row)||row.state!=='ready')keep.add(row);
      for(const row of rows)if(!keep.has(row))releaseImagePreview(row);imageDrafts.set(key,[...keep]);
      const count=Math.max(0,Number(data.quota?.count)||0),bytes=Math.max(0,Number(data.quota?.bytes)||0),knownBytes=data.attachments.filter(meta=>seen.has(meta?.id)).reduce((n,meta)=>n+(Number(meta.bytes)||0),0);
      imageDraftInfo.set(key,{extraCount:Math.max(Number(data.retained_unknown?.count)||0,count-seen.size),extraBytes:Math.max(Number(data.retained_unknown?.bytes)||0,bytes-knownBytes),retentionHours:Number(data.retention_hours)||24,error:false});
    }catch(error){if(current()&&error.name!=='AbortError')imageDraftInfo.set(key,{...imageDraftInfo.get(key),error:true})}
    finally{if(imageRestoreRequests.get(key)===version)imageRestoreRequests.delete(key);if(memoryKey()===workspace&&draftKey()===key&&activePage==='butler'){renderAttachmentDrafts();busy()}}
  }
  async function removeImage(row,key){
    if(imageMutationLocked(key))return;row.cancelled=true;row.controller?.abort();
    if(row.metadata){row.state='removing';if(draftKey()===key)renderAttachmentDrafts();try{await apiPost(`/api/butler/sessions/${row.sid}/attachments/${row.metadata.id}/remove`,{})}catch(error){row.cancelled=false;row.state='ready';feedback(errorText(error));if(draftKey()===key)renderAttachmentDrafts();return}}
    rememberRemovedImage(key,row.metadata?.id);forgetImage(row,key);
  }
  async function uploadImage(row,key){
    if(row.cancelled||imageMutationLocked(key))return;if(!row.file){feedback('这张图片没有可重传的本机文件，请移除后重新选择原图。');return}row.state='uploading';row.error='';row.controller=new AbortController();if(draftKey()===key){renderAttachmentDrafts();busy()}
    try{
      await loadProtectionStatus({quiet:true});
      const response=await fetch(`/api/butler/sessions/${row.sid}/attachments?filename=${encodeURIComponent(row.file.name)}`,{method:'POST',headers:{'content-type':row.file.type||'application/octet-stream','x-jiedu-local-action':'1','x-jiedu-csrf':protectionCsrf},body:row.file,signal:row.controller.signal});
      if(!response.ok)throw Object.assign(Error(await response.text()),{status:response.status});
      row.metadata=await response.json();if(!row.metadata?.id||!storedImageURL(row.metadata.url))throw Error('图片上传未返回可用预览，请重试');
      if(row.cancelled){await apiPost(`/api/butler/sessions/${row.sid}/attachments/${row.metadata.id}/remove`,{});return}
      for(const other of [...attachmentRows(key)])if(other!==row&&other.metadata?.id===row.metadata.id)forgetImage(other,key);
      row.state='ready';
    }catch(error){if(!row.cancelled){row.state='failed';row.error=errorText(error)}}
    finally{row.controller=null;if(draftKey()===key){renderAttachmentDrafts();busy()}}
  }
  async function ensureImageSession(){
    if(selected)return selected;if(imageSessionPromise)return imageSessionPromise;
    const origin=draftKey(),workspace=memoryKey();imageSessionPromise=(async()=>{const item=await apiPost('/api/butler/sessions',{});if(memoryKey()!==workspace||draftKey()!==origin)throw Error('对话已切换，请在当前对话重新添加图片');select(item.id,true);return item.id})().finally(()=>imageSessionPromise=null);return imageSessionPromise;
  }
  async function addImages(files){
    if(newSessionPending()){feedback('正在创建新对话，请稍候添加图片。');return}
    if(imageRestoring()){feedback('正在恢复图片草稿，请稍候。');return}
    if(imageLocked()){feedback('发送和生成期间不能更改图片。');return}
    if(!visionSupported()){feedback('当前模型未确认支持图片；请先选择支持图片的外部模型。');return}
    const inputs=Array.from(files||[]);if(!inputs.length)return;
    if(inputs.some(file=>!['image/png','image/jpeg','image/gif','image/webp'].includes(file.type))){feedback('仅支持 PNG、JPEG、GIF 和 WebP 图片。');return}
    if(inputs.some(file=>!file.size||file.size>MAX_IMAGE_BYTES)){feedback('每张图片须大于0且不超过8 MiB。');return}
    const workspace=memoryKey(),origin=draftKey();
    try{const sid=await ensureImageSession();if(memoryKey()!==workspace||selected!==sid)throw Error('对话已切换，请重新添加图片');const key=draftKey(sid),rows=attachmentRows(key),info=imageDraftInfo.get(key);if(imageRestoring(key))throw Error('正在恢复图片草稿，请稍候。');if(rows.length+(info?.extraCount||0)+inputs.length>MAX_IMAGES)throw Error('本对话未发送的图片最多4张，请先发送或移除已有草稿。');if(rows.reduce((n,row)=>n+imageBytes(row),0)+(info?.extraBytes||0)+inputs.reduce((n,file)=>n+file.size,0)>MAX_IMAGE_TOTAL)throw Error('本对话未发送的图片总计不能超过16 MiB。');for(const file of inputs){const row={file,key,sid,preview:URL.createObjectURL(file),state:'uploading',metadata:null,error:'',cancelled:false};rows.push(row);uploadImage(row,key)}renderAttachmentDrafts();busy()}
    catch(error){if(memoryKey()===workspace)feedback(errorText(error))}
  }
  function clearSentImages(key,ids){for(const id of ids)rememberRemovedImage(key,id);for(const row of [...attachmentRows(key)])if(ids.includes(row.metadata?.id))forgetImage(row,key)}
  function messageImages(message){
    if(!Array.isArray(message.attachments)||!message.attachments.length)return null;const gallery=document.createElement('div');gallery.className='butler-message-images';
    for(const item of message.attachments){const url=storedImageURL(item.url);if(!url)continue;const name=item.filename||'已发送图片',b=button('',()=>showImage(url,name),'butler-message-image');b.setAttribute('aria-label','查看图片 '+name);const img=document.createElement('img');img.src=url;img.alt=name;img.loading='lazy';img.referrerPolicy='no-referrer';b.append(img);gallery.append(b)}return gallery.children.length?gallery:null;
  }
  $('#butlerAttach').onclick=()=>$('#butlerImageInput').click();
  $('#butlerImageInput').onchange=event=>{addImages(event.target.files);event.target.value=''};
  $('#butlerQuestion').addEventListener('paste',event=>{const files=[...event.clipboardData?.items||[]].filter(item=>item.kind==='file'&&item.type.startsWith('image/')).map(item=>item.getAsFile()).filter(Boolean);if(files.length){event.preventDefault();addImages(files)}});
  $('#butlerForm').addEventListener('dragover',event=>{if(![...event.dataTransfer.types].includes('Files'))return;event.preventDefault();event.dataTransfer.dropEffect=imageMutationLocked()||!visionSupported()?'none':'copy';$('#butlerForm').classList.add('is-dragging')});
  $('#butlerForm').addEventListener('dragleave',event=>{if(!$('#butlerForm').contains(event.relatedTarget))$('#butlerForm').classList.remove('is-dragging')});
  $('#butlerForm').addEventListener('drop',event=>{event.preventDefault();$('#butlerForm').classList.remove('is-dragging');addImages(event.dataTransfer.files)});
  document.addEventListener('butler-model-state',()=>{imageControls();busy()});

  const mobile=matchMedia('(max-width:800px)');let drawerOpen=false,menuSession='',sidebarCollapsed=false,pendingDraft='';
  function drawer(open,restoreFocus=true){if(!open)document.querySelectorAll('#butler [popover]').forEach(p=>{if(p.matches(':popover-open'))p.hidePopover()});drawerOpen=open&&mobile.matches;$('#butler').classList.toggle('drawer-open',drawerOpen);$('#butlerDrawerBackdrop').hidden=!drawerOpen;$('#butlerMenuButton').setAttribute('aria-expanded',String(drawerOpen));$('#butlerSidebar').inert=mobile.matches&&!drawerOpen;$('#butlerSidebar').setAttribute('aria-hidden',String(mobile.matches&&!drawerOpen));$('#butler .butler-chat').inert=drawerOpen;sidebar();if(drawerOpen)$('#butlerCloseSidebar').focus();else if(restoreFocus&&mobile.matches)$('#butlerMenuButton').focus();}
  function sidebar(){const hidden=!mobile.matches&&sidebarCollapsed;$('#butler').classList.toggle('sidebar-collapsed',hidden);$('#butlerSidebar').inert=hidden||(mobile.matches&&!drawerOpen);$('#butlerSidebar').setAttribute('aria-hidden',String(hidden||(mobile.matches&&!drawerOpen)));$('#butlerMenuButton').hidden=!mobile.matches&&!hidden;$('#butlerMenuButton').setAttribute('aria-expanded',String(mobile.matches?drawerOpen:!hidden));}
  $('#butlerCollapseSidebar').onclick=()=>{sidebarCollapsed=true;sidebar();$('#butlerMenuButton').focus()};$('#butlerMenuButton').onclick=()=>{if(mobile.matches)drawer(true);else{sidebarCollapsed=false;sidebar();$('#butlerCollapseSidebar').focus()}};$('#butlerCloseSidebar').onclick=()=>drawer(false);$('#butlerDrawerBackdrop').onclick=()=>drawer(false);mobile.addEventListener('change',()=>{drawer(false,false);sidebar();resizeInput()});drawer(false,false);sidebar();
  document.addEventListener('keydown',e=>{if(!drawerOpen)return;if(e.key==='Escape'){e.preventDefault();drawer(false)}if(e.key==='Tab'){const items=[...$('#butlerSidebar').querySelectorAll('button:not([disabled])')].filter(x=>x.offsetParent!==null),first=items[0],last=items.at(-1);if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus()}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus()}}});
  $('#butlerBack').onclick=()=>{drawer(false,false);switchPage('overview')};$('#butlerSettingsButton').onclick=()=>{$('#butlerSettingsDialog').showModal();window.ButlerControls?.selectPage('models');window.ButlerControls?.load()};
  function popup(pop,anchor,above=false){if(pop.matches(':popover-open')){pop.hidePopover();return}const rect=anchor.getBoundingClientRect();pop.showPopover();pop.style.left=Math.max(12,Math.min(innerWidth-pop.offsetWidth-12,rect.right-pop.offsetWidth))+'px';pop.style.top=Math.max(12,Math.min(innerHeight-pop.offsetHeight-12,above?rect.top-pop.offsetHeight-8:rect.bottom+6))+'px';(pop.querySelector('button[aria-pressed=true]')||pop.querySelector('button'))?.focus();}
  const lengthLabels={'auto':'自动','512':'标准','1024':'适中','2048':'较长','4096':'长回答','max':'最大'};
  function choiceLabels(){const on=$('#butlerThinking').getAttribute('aria-pressed')==='true';const label=$('#butlerThinking').querySelector('span:last-child');if(label)label.textContent='深度思考 · '+(on?'开':'关');$('#butlerThinking').setAttribute('aria-label','深度思考'+(on?'已开启':'已关闭'));$('#butlerThinking').title=on?'深度思考已开启':'深度思考已关闭';$('#butlerLengthLabel').textContent='长度 · '+(lengthLabels[$('#butlerLimit').value]||'标准');$('#butlerLengthOptions').querySelectorAll('button').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.limit===$('#butlerLimit').value)));window.ButlerControls?.runtime();}
  for(const [value,label] of ['auto','512','1024','2048','4096','max'].map(value=>[value,lengthLabels[value]])){const b=button('',()=>{$('#butlerLimit').value=value;saveChoices();choiceLabels();$('#butlerLengthPopover').hidePopover();$('#butlerLengthButton').focus()});b.dataset.limit=value;const name=document.createElement('span');name.textContent=label;const hint=document.createElement('small');hint.textContent=value==='auto'?'随思考模式调整':value==='max'?'剩余可用额度':value+' 词元';b.append(name,hint);$('#butlerLengthOptions').append(b)}
  $('#butlerLengthButton').onclick=()=>popup($('#butlerLengthPopover'),$('#butlerLengthButton'),true);
  $('#butlerDeleteSession').onclick=()=>{$('#butlerSessionMenu').hidePopover();trash(menuSession)};
  function feedback(text){$('#butlerRunStatus').hidden=false;$('#butlerProgress').textContent=text;}
  function sourceLabel(source){
    if(source.kind==='product_catalog')return '界渡能力目录';
    if(source.kind==='product_topic')return String(source.content||'').split('\n')[0].split(' · ')[0].trim()||'产品主题说明';
    return source.source_path.split('/').pop();
  }
  function displayText(text,sources=[]){
    const known=new Map(sources.map(s=>[s.id,sourceLabel(s)]));
    return String(text||'').replace(/\[(S\d+)\]/g,(_,id)=>known.has(id)?`〔${known.get(id)}〕`:'〔来源待核对〕').replaceAll('[运行快照]','（依据本次运行记录）').replaceAll('本轮运行快照','查询时的运行状态');
  }
  function unmatchedCitations(text,sources=[]){
    const known=new Set(sources.map(source=>source.id));
    const prose=String(text||'').replace(/```[\s\S]*?(?:```|$)/g,'').replace(/`[^`\n]*`/g,'');
    const markers=[...prose.matchAll(/\[(S\d+|\d+)\](?!\()/g)].map(match=>match[1]);
    return [...new Set(markers.filter(marker=>!known.has(marker)))];
  }
  function citationNote(message){
    const unmatched=unmatchedCitations(message.content,message.sources);if(!unmatched.length)return null;
    const note=document.createElement('p');note.className='butler-output-notice';note.textContent='引用未能匹配：'+unmatched.map(value=>'['+value+']').join('、')+'。这些标记没有对应本轮来源，请从来源中核对；回答原文已保留。';return note;
  }
  function scrollLatest(){box.scrollTop=box.scrollHeight;follow=true;$('#butlerLatest').hidden=true;}
  function changed(){if(follow)scrollLatest();else $('#butlerLatest').hidden=false;}
  box.addEventListener('scroll',()=>{follow=box.scrollHeight-box.scrollTop-box.clientHeight<64;$('#butlerLatest').hidden=follow});
  $('#butlerLatest').onclick=scrollLatest;
  function saveChoices(){if(window.ButlerControls?.thinkingMode()==='switchable')window.ButlerThinkingPreference=$('#butlerThinking').getAttribute('aria-pressed')==='true';localStorage.setItem(memoryKey()+'.generation',JSON.stringify({thinking:window.ButlerThinkingPreference===true,output_limit:$('#butlerLimit').value}));}
  $('#butlerThinking').onclick=()=>{if(window.ButlerControls?.thinkingMode()!=='switchable')return;const enabled=$('#butlerThinking').getAttribute('aria-pressed')!=='true';$('#butlerThinking').setAttribute('aria-pressed',String(enabled));saveChoices();choiceLabels()};
  $('#butlerLimit').onchange=()=>{saveChoices();choiceLabels()};
  function loadChoices(){if(settingsLoaded)return;settingsLoaded=true;resizeInput();busy();let choice={};try{choice=JSON.parse(localStorage.getItem(memoryKey()+'.generation')||'{}')||{}}catch(_){}window.ButlerThinkingPreference=typeof choice.thinking==='boolean'?choice.thinking:true;$('#butlerThinking').setAttribute('aria-pressed',String(window.ButlerThinkingPreference));if([...$('#butlerLimit').options].some(o=>o.value===String(choice.output_limit)))$('#butlerLimit').value=String(choice.output_limit);choiceLabels();}
  function syncEmptyState(){const state=window.ButlerControls?.modelStatus?.()||'loading',none=state==='unconfigured';for(const host of document.querySelectorAll('#butler .butler-welcome,#butler .butler-empty')){host.dataset.modelState=state;const h=host.querySelector('h3'),p=host.querySelector('p');if(h)h.textContent=state==='loading'?'正在读取模型与对话…':state==='error'?'模型状态读取失败':none?'先选择模型':'今天想聊些什么？';if(p)p.textContent=state==='loading'?'读取完成后显示已有模型与历史对话。':state==='error'?'请重试读取；已有配置与历史记录仍保留。':none?'下载到本机，或接入你已有的大模型API。历史对话和设置仍可使用。':'从一句提问开始，把想法与知识连起来。';let b=host.querySelector('[data-choose-empty-model]');if(!b){b=button('选择模型',()=>$('#butlerChooseModel').click());b.dataset.chooseEmptyModel='true';host.append(b)}b.textContent=state==='error'?'重试读取':'选择模型';b.hidden=!(none||state==='error')}busy()}

  document.addEventListener('butler-model-state',syncEmptyState);
  function busy(){const active=job&&!terminal.has(job.state),images=attachmentRows();$('#butlerSend').disabled=submitting||!!active||newSessionPending()||imageRestoring()||(!$('#butlerQuestion').value.trim()&&!images.length)||images.some(row=>row.state!=='ready')||(images.length&&!visionSupported())||window.ButlerControls?.canChat()!==true;imageControls();$('#butlerSend').hidden=!!active;$('#butlerScope').disabled=!!active;$('#butlerThinking').disabled=submitting||!!active||window.ButlerControls?.thinkingMode()!=='switchable';$('#butlerLimit').disabled=submitting||!!active;$('#butlerLengthButton').disabled=submitting||!!active||window.ButlerControls?.generationManaged()===true;$('#butlerCancel').hidden=!active;$('#butlerNew').disabled=newSessionPending();$('#butlerNew').setAttribute('aria-busy',String(newSessionPending()));}
  function currentJob(value){if(!value)return value;const old=knownJobs.get(value.id);if(old&&Date.parse(old.updated)>Date.parse(value.updated))return old;value={...value,reasoning_started_at:value.reasoning_started_at||old?.reasoning_started_at,reasoning_finished_at:value.reasoning_finished_at||old?.reasoning_finished_at};knownJobs.set(value.id,value);return value;}
  const recovering=new Set();
  async function answerDirectly(id){
    if(recovering.has(id))return;recovering.add(id);
    try{await apiPost(`/api/butler/jobs/${id}/answer-directly`,{});showToast('仅本轮跳过思考，原记录已保留');await refreshConversation()}
    catch(e){feedback(errorText(e))}finally{recovering.delete(id);if(job)renderJob(job)}
  }
  function updateRecovery(article,id){
    let host=article.querySelector('.butler-recovery-result');if(!host){host=document.createElement('div');host.className='butler-recovery-result';article.append(host)}
    host.replaceChildren();const child=[...knownJobs.values()].filter(j=>j.parent_job===id&&j.requested?.operation==='direct_answer').sort((a,b)=>Date.parse(b.created)-Date.parse(a.created))[0];
    if(child&&!terminal.has(child.state)){host.textContent='正在直接回答 · 原思考已保留';return}
    if(child?.state==='completed'&&child.stage==='已完成'){host.textContent='已直接回答，请查看下方回答。';return}
    host.append(recoveryButton(id));
  }
  function recoveryButton(id){const b=button('跳过思考并回答',async()=>{b.disabled=true;try{await answerDirectly(id)}finally{b.disabled=false}});b.title='只影响本轮；思考和生成长度偏好保持';return b;}
  $('#butlerAnswerDirectly').onclick=()=>job&&answerDirectly(job.id);
  const productGroups=[
    {id:'read',title:'查找与导入资料',note:'导入、搜索、阅读原文',topics:['objects','conversion','ocr','search','overview','analysis'],page:'files'},
    {id:'archive',title:'保存与回看对话',note:'归档与来源覆盖',topics:['codex','hermes','sources','paused'],page:'archives'},
    {id:'organize',title:'整理与积累知识',note:'建议、知识页、版本',topics:['candidates','undo','projects','continuous','wiki','packages'],page:'memory'},
    {id:'ai',title:'使用与连接 AI',note:'问答、模型、授权',topics:['butler','provider','access','core'],page:'access'},
    {id:'workspace',title:'管理工作区',note:'连接、保护、运行',topics:['workspace','protection','export','resources','package','unfinished'],page:'workspace'}
  ];
  const productRoutes={objects:['files','打开知识资料'],conversion:['files','前往知识 · 导入资料'],ocr:['files','前往知识 · 导入资料'],search:['files','打开知识资料'],overview:['overview','查看首页'],analysis:['map','查看知识空间'],codex:['archives','查看对话档案'],hermes:['archives','查看对话档案'],sources:['archives','查看对话档案'],paused:['archives','查看已有档案'],candidates:['memory','打开整理中心'],undo:['memory','打开整理中心'],projects:['memory','打开整理中心'],continuous:['memory','打开整理中心'],wiki:['wiki','打开知识 Wiki'],packages:['memory','打开整理中心'],butler:['butler','回到管家问答'],provider:['butler','回到管家问答'],access:['access','打开 AI 连接'],core:['weight','查看会话刷新'],workspace:['workspace','打开工作区'],protection:['protection','打开设置'],export:['protection','查看保护设置'],resources:['overview','查看首页检索状态'],package:['protection','打开设置'],unfinished:['workspace','查看工作区']};
  const productTitles={objects:'导入与浏览资料',conversion:'把文档转成可阅读文字',ocr:'识别图片与 PDF 中的文字',search:'查找资料与查看出处',codex:'保存与回看 Codex 对话',hermes:'保存与回看 Hermes 对话',sources:'接收其他 AI 的对话',paused:'已暂停的自动归档',core:'让 AI 了解你的背景',candidates:'把对话与资料整理成知识',undo:'查看版本与撤销改动',continuous:'让 AI 持续整理资料',packages:'交给外部 AI 整理',analysis:'知识关系分析',provider:'选择外部模型服务'};
  let productData=null,productGroup='read',productRequest=0;
  const productOpen=new Set();
  function renderProductHelp(){
    if(!productData)return;
    const query=$('#butlerProductSearch').value.trim().toLocaleLowerCase(),group=productGroups.find(g=>g.id===productGroup)||productGroups[0];
    const topics=productData.topics.filter(t=>(query||group.topics.includes(t.id))&&(!query||[productTitles[t.id],t.title,t.summary,t.usage,t.details,t.limits,t.status_label].join(' ').toLocaleLowerCase().includes(query)));
    const navigation=$('#butlerProductGroups');if(!navigation.children.length)navigation.replaceChildren(...productGroups.map(g=>{const b=button('',()=>{productGroup=g.id;$('#butlerProductSearch').value='';renderProductHelp();$('#butlerProductTopics').scrollTop=0});b.className='product-help-group';b.dataset.group=g.id;b.setAttribute('aria-current',g.id===productGroup&&!query?'true':'false');const title=document.createElement('strong'),note=document.createElement('small');title.textContent=g.title;note.textContent=g.note;b.append(title,note);return b}));for(const b of navigation.children)b.setAttribute('aria-current',b.dataset.group===productGroup&&!query?'true':'false');
    $('#butlerProductGroupTitle').textContent=query?'搜索结果':group.title;
    $('#butlerProductCount').textContent=query?`${topics.length} 项结果 · 全部 ${productData.topics.length} 项功能`:`${topics.length} 项功能`;
    const host=$('#butlerProductTopics');host.replaceChildren();
    if(!topics.length){const p=document.createElement('p');p.className='product-help-empty';p.textContent='没有找到相关功能。试试“搜索”“对话”或“知识”。';host.append(p);return}
    for(const topic of topics){
      const friendly=productTitles[topic.id]||topic.title;const details=document.createElement('details');details.className='product-help-topic';details.dataset.topic=topic.id;details.open=productOpen.has(topic.id);
      const summary=document.createElement('summary');summary.setAttribute('aria-label',friendly+(details.open?'，收起用法与说明':'，展开用法与说明'));
      const text=document.createElement('span'),title=document.createElement('strong'),short=document.createElement('span');text.className='product-help-topic-text';title.textContent=friendly;short.textContent=topic.summary;text.append(title,short);
      const status=document.createElement('span');status.className='product-help-status '+topic.status;status.textContent=topic.status==='implemented'?'可使用':topic.status_label;
      const arrow=document.createElement('span');arrow.className='product-help-chevron';arrow.textContent='›';arrow.setAttribute('aria-hidden','true');summary.append(text,status,arrow);details.append(summary);
      const body=document.createElement('div');body.className='product-help-topic-body';if(friendly!==topic.title){const original=document.createElement('p');original.className='product-help-original-title';original.textContent='功能名称：'+topic.title;body.append(original)}
      for(const [label,value] of [['怎么用',topic.usage],['需要知道',topic.limits]]){const h=document.createElement('h4'),p=document.createElement('p');h.textContent=label;p.textContent=value;body.append(h,p)}
      const more=document.createElement('details'),moreHeading=document.createElement('summary'),moreText=document.createElement('p');more.className='product-help-more';moreHeading.textContent='更多说明';moreText.textContent=topic.details;more.append(moreHeading,moreText);body.append(more);
      if(topic.needs_recheck){const note=document.createElement('p');note.className='product-help-recheck';note.textContent='相关实现已有更新，以下说明仍需重新核对。';body.append(note)}
      const route=productRoutes[topic.id];if(route)body.append(button(route[1]+' →',()=>{$('#butlerProductDialog').close();switchPage(route[0])},'product-help-entry'));
      const evidence=document.createElement('details'),heading=document.createElement('summary');evidence.className='product-help-evidence';heading.textContent='技术依据与验证记录';const pre=document.createElement('pre');pre.textContent='核对说明：'+topic.verification+'\n目录版本：'+productData.version+'\n'+topic.evidence.map(e=>e.path+'\n记录 SHA256：'+e.sha256+(e.changed?'\n当前文件已变化，需重新核对。':'')).join('\n')+'\n已有验证记录（不代表本次全部重跑）：\n'+(topic.verification_records||[]).map(r=>r.path+' · '+r.kind).join('\n');evidence.append(heading,pre);body.append(evidence);details.append(body);
      details.addEventListener('toggle',()=>{summary.setAttribute('aria-label',friendly+(details.open?'，收起用法与说明':'，展开用法与说明'));if(details.isConnected)details.open?productOpen.add(topic.id):productOpen.delete(topic.id)});host.append(details);
    }
  }
  $('#butlerProductSearch').addEventListener('input',()=>{renderProductHelp();$('#butlerProductTopics').scrollTop=0});
  $('#butlerProductHelp').onclick=async()=>{
    drawer(false,false);const dialog=$('#butlerProductDialog'),host=$('#butlerProductTopics'),request=++productRequest;dialog.showModal();
    if(!productData){host.textContent='正在读取功能说明…';host.setAttribute('aria-busy','true')}
    try{const data=await api('/api/butler/product-help');if(request!==productRequest||!dialog.open)return;const changed=JSON.stringify(productData)!==JSON.stringify(data);productData=data;if(changed||!host.querySelector('details'))renderProductHelp()}catch(e){if(request===productRequest&&dialog.open&&!isRequestCancelled(e)){host.textContent=errorText(e)}}finally{if(request===productRequest)host.setAttribute('aria-busy','false')}
  };
  function renderJob(value){
    job=currentJob(value)||null;window.ButlerControls?.job(job);if(job?.kind==='translation')renderTranslation(job);busy();$('#butlerRunStatus').hidden=!(job&&(!terminal.has(job.state)||job.state==='failed'));
    const recover=!!job?.recovery_available;$('#butlerRecoveryBar').hidden=!recover;$('#butlerAnswerDirectly').disabled=!!job&&recovering.has(job.id);$('#butlerRecoveryNote').textContent=job?.reasoning_notice||'尚未形成答案。可只在本轮跳过思考，原记录保留。';
    $('#butlerProgress').textContent=job?(job.stage+(job.error?'：'+job.error:'')):'准备就绪';
    $('#butlerStepList').replaceChildren(...(job?.steps||[]).map(s=>{const li=document.createElement('li');li.textContent=s.label;return li}));
    $('#butlerJobSnapshot').hidden=!job?.details?.budget;$('#butlerJobSnapshot').onclick=()=>job&&openSnapshot(job.id);
    for(const article of nodes.values())if(article.dataset.recoveryJob)article.querySelector('.butler-recovery-result')?.toggleAttribute('hidden',article.dataset.recoveryJob===job?.id);
    if(job&&terminal.has(job.state)&&job.state!=='completed')syncDraft(job);
    const tick=()=>{$('#butlerElapsed').textContent=job?' · '+Math.max(0,Math.round(((terminal.has(job.state)?Date.parse(job.updated):Date.now())-Date.parse(job.created))/1000))+'秒':'';for(const d of drafts.values())if(d.thought)thoughtLabel(d.thought,currentJob(knownJobs.get(d.id))||{state:'running'})};
    clearInterval(elapsedTimer);tick();if(job&&!terminal.has(job.state))elapsedTimer=setInterval(tick,1000);
  }
  function actionButton(label,name,action){const b=button('',action,'butler-message-action');b.append(icon(name));b.title=label;b.setAttribute('aria-label',label);return b;}
  async function copyMessage(text){try{await navigator.clipboard.writeText(text);showToast('已复制')}catch(_){showToast('复制未成功，请选中文字复制')}}
  function resizeInput(){const input=$('#butlerQuestion');input.style.height='0px';input.style.height=Math.max(60,Math.min(180,Math.max(60,innerHeight*.25),input.scrollHeight))+'px';input.style.overflowY=input.scrollHeight>input.clientHeight?'auto':'hidden';}
  function rememberDraft(){saveInputDraft(draftKey(),$('#butlerQuestion').value);resizeInput();busy();}
  function editQuestion(text){const input=$('#butlerQuestion');if(input.value.trim()&&input.value!==text){pendingDraft={key:draftKey(),text};$('#butlerDraftPreview').textContent=text;$('#butlerDraftDialog').showModal();return}input.value=text;rememberDraft();input.focus();}
  $('#butlerKeepDraft').onclick=()=>{$('#butlerDraftDialog').close();$('#butlerQuestion').focus()};
  $('#butlerAppendDraft').onclick=()=>{if(!pendingDraft||pendingDraft.key!==draftKey()){$('#butlerDraftDialog').close();return}const input=$('#butlerQuestion'),combined=input.value+'\n\n'+pendingDraft.text;if(combined.length>input.maxLength){showToast('追加后超过输入上限，现有草稿已保留');return}input.value=combined;rememberDraft();$('#butlerDraftDialog').close();input.focus();showToast('已追加到草稿，尚未发送')};
  function sourcesPanel(message){
    const wrap=document.createElement('div');wrap.className='butler-message-actions';
    if(message.content)wrap.append(actionButton(message.role==='user'?'复制提问':'复制回答','clipboard-document',()=>copyMessage(message.content)));
    if(message.role==='user'){wrap.append(actionButton('编辑提问','pencil-square',()=>editQuestion(message.content)));return wrap}
    if(message.sources.length){const count=new Set(message.sources.map(s=>s.source_path)).size;const b=button('',()=>openEvidence(message));b.title='查看回答来源';b.append(icon('book-open'),document.createTextNode('来源 · '+count));wrap.append(b)}
    wrap.append(actionButton('本轮详情','ellipsis-horizontal',()=>openEvidence(message,true)));return wrap;
  }
  function openEvidence(message,detailsOnly=false){
    $('#butlerEvidenceTitle').textContent=detailsOnly?'本轮详情':'回答来源';const host=$('#butlerEvidenceBody');host.replaceChildren();
    if(!detailsOnly){const ids=new Set([...message.content.matchAll(/\[(S\d+)\]/g)].map(m=>m[1]));for(const [label,items] of [['回答标注的引用',message.sources.filter(s=>ids.has(s.id))],['本轮检索候选（不代表回答引用）',message.sources.filter(s=>!ids.has(s.id))]]){if(!items.length)continue;const h=document.createElement('h3');h.textContent=label;host.append(h);for(const source of items){const filename=sourceLabel(source),siblings=message.sources.filter(x=>x.source_path===source.source_path),ambiguous=new Set(message.sources.filter(x=>sourceLabel(x)===filename).map(x=>x.source_path)).size>1;const b=button((ambiguous&&!source.kind?.startsWith('product_')?source.source_path:filename)+(siblings.length>1?` · 片段${siblings.findIndex(x=>x.id===source.id)+1}/${siblings.length}`:''),()=>openSource(message.job,source.id),'butler-source-link');b.title=filename+' · '+(source.version||'');host.append(b)}}}
    const unmatched=citationNote(message);if(unmatched)host.append(unmatched);
    const info=generationInfo(message);if(info){info.open=true;host.append(info)}
    host.append(button('查看当时的运行记录',()=>openSnapshot(message.job)));
    if(message.reasoning){const explanation=document.createElement('p');explanation.textContent='中文概述由原记录对应的模型按需生成，并非原始思考全文，可能简化或误解内容。原始思考未完成时，概述只帮助理解已生成部分；完整原文保留在下面。';host.append(explanation);const details=document.createElement('details');const summary=document.createElement('summary');summary.textContent='查看思考原文';const pre=document.createElement('pre');pre.textContent=message.reasoning;details.append(summary,pre);host.append(details)}
    $('#butlerEvidenceDialog').showModal();
  }
  function answerMarkdown(text){
    const inline=value=>esc(value).replace(/`([^`\n]+)`|\*\*([^*\n]+)\*\*/g,(_,code,bold)=>code!==undefined?'<code>'+code+'</code>':'<strong>'+bold+'</strong>');
    const output=[];let list=[],kind='',listStart=1,paragraph=[],code=null;
    const flushParagraph=()=>{if(paragraph.length){output.push('<p>'+inline(paragraph.join('\n'))+'</p>');paragraph=[]}};
    const flushList=()=>{if(list.length){output.push('<'+kind+(kind==='ol'?' start="'+listStart+'"':'')+'>'+list.map(x=>'<li>'+inline(x)+'</li>').join('')+'</'+kind+'>');list=[];kind=''}};
    for(const raw of String(text||'').replace(/\r\n/g,'\n').split('\n')){const line=raw.trim();if(line.startsWith('```')){flushParagraph();flushList();if(code===null)code=[];else{output.push('<pre><code>'+esc(code.join('\n'))+'</code></pre>');code=null}continue}if(code!==null){code.push(raw);continue}if(!line){flushParagraph();flushList();continue}const heading=line.match(/^(#{1,4})\s+(.+)$/),bullet=line.match(/^([-*]|\d+[.)])\s+(.+)$/);if(heading){flushParagraph();flushList();output.push('<h4>'+inline(heading[2])+'</h4>')}else if(bullet){flushParagraph();const next=/\d/.test(bullet[1])?'ol':'ul';if(kind&&kind!==next)flushList();if(!list.length)listStart=parseInt(bullet[1],10)||1;kind=next;list.push(bullet[2])}else{flushList();paragraph.push(line)}}flushParagraph();flushList();if(code!==null)output.push('<pre><code>'+esc(code.join('\n'))+'</code></pre>');return output.join('');
  }
  function thoughtLabel(thought,value={}){const ended=value.reasoning_finished_at,stopped=['canceled','failed','interrupted'].includes(value.state)||(value.finish_reason==='length'&&!value.content),active=value.state==='running'&&!ended;const label=active?'正在思考':stopped?'思考已停止':'已思考';const start=Date.parse(value.reasoning_started_at),end=ended?Date.parse(ended):active?Date.now():NaN;thought.label.textContent=label+(Number.isFinite(start)&&Number.isFinite(end)?' · '+Math.max(0,Math.round((end-start)/1000))+'秒':'');}
  function thoughtNode(text,value={},open=false){const details=document.createElement('details');details.className='butler-thought';details.open=false;const summary=document.createElement('summary'),label=document.createElement('span'),chevron=icon('chevron-down');chevron.classList.add('butler-chevron');summary.append(icon('light-bulb'),label,chevron);const content=document.createElement('div');content.className='butler-thought-content';content.textContent=text;details.append(summary,content);const thought={details,content,label,summary,rawText:text};details.addEventListener('toggle',()=>{if(details.open)content.textContent=thought.rawText});thoughtLabel(thought,value);originalNote(thought,text);const parent=value.job||value.id;if(parent){thoughts.set(parent,thought);const tools=document.createElement('div');tools.className='butler-translation-tools';const translate=button('中文概述',async()=>{translate.disabled=true;try{await apiPost(`/api/butler/jobs/${parent}/translate`,{});await refreshConversation()}catch(e){feedback(errorText(e))}finally{translate.disabled=!!translations.get(parent)&&!terminal.has(translations.get(parent).state)}});translate.disabled=['running','queued'].includes(value.state);thought.translate=translate;const translated=document.createElement('div');translated.className='butler-translation';translated.hidden=true;thought.translated=translated;translate.onclick=((action)=>event=>{event.preventDefault();event.stopPropagation();details.open=true;action()})(translate.onclick);tools.append(translate);summary.append(tools);content.before(translated);if(translations.has(parent))renderTranslation(translations.get(parent))}return thought;}
  function originalNote(thought,text){thought.details.querySelector('.butler-thought-original')?.remove();if(!text)return;const latin=(text.match(/[A-Za-z]/g)||[]).length,han=(text.match(/[\u4e00-\u9fff]/g)||[]).length;const note=document.createElement('small');note.className='butler-thought-original';note.textContent='模型思考原文 · '+(latin>Math.max(30,han*2)?'以英文为主，未翻译':han>latin*2?'以中文为主':'混合语言')+'。可按需生成中文概述。';(thought.originalDisclosure||thought.details).append(note)}
  function renderTranslation(value){
    const previous=translations.get(value.parent_job);if(previous&&previous.id!==value.id&&(!value.created||Date.parse(previous.created)>Date.parse(value.created)))return;const same=previous?.id===value.id?previous:null;value={...same,...value,preview:value.preview??same?.preview??''};translations.set(value.parent_job,value);const thought=thoughts.get(value.parent_job);if(!thought)return;
    if(!thought.originalDisclosure){const raw=document.createElement('details'),summary=document.createElement('summary');raw.className='butler-original-disclosure';summary.textContent='查看思考原文';thought.content.replaceWith(raw);raw.append(summary,thought.content);const original=thought.details.querySelector('.butler-thought-original');if(original)raw.append(original);thought.originalDisclosure=raw}
    thought.translated.hidden=false;thought.translated.replaceChildren();const label=document.createElement('b');label.textContent='中文概述 · 非原文';label.title='按需生成的中文概述，并非原始思考全文；可能有偏差，请核对原文';const note=document.createElement('p');const budget=value.details?.budget||{};note.textContent=(budget.original_incomplete?'原始思考未完成。':'')+(budget.excerpt?`概述范围 · 原文前${budget.translated_characters}字 / 共${budget.source_characters}字。`:'')+(value.state==='completed'?(value.finish_reason==='length'?'概述未完成：已达到生成额度上限。':''):terminal.has(value.state)?'概述已停止，以下是未完成片段。'+(value.error||''):'正在生成中文概述…');const body=document.createElement('div'),obsolete=value.requested?.translation_format_version!==3;if(obsolete)note.textContent='旧版中文说明未通过质量核验，请生成中文概述。历史记录已保留。';else body.innerHTML=answerMarkdown(value.preview||'');note.hidden=!note.textContent;thought.translated.append(label,note,body);thought.translate.disabled=!terminal.has(value.state);thought.translate.textContent=!obsolete&&value.state==='completed'&&value.finish_reason==='stop'?'中文概述':'生成中文概述';
  }
  function generationInfo(value){const requested=value.requested||{},effective=value.effective||{};if(!Object.keys(requested).length)return null;const details=document.createElement('details');details.className='butler-generation-info';const summary=document.createElement('summary');summary.textContent='本轮设置';const p=document.createElement('p');p.textContent=effective.provider==='hermes'?'通过本机 Hermes 生成；思考和输出额度由 Hermes 管理，界渡的本机模型上限不应用于本轮。':`思考${requested.thinking?'开启':'关闭'} · 选择上限${requested.output_limit==='auto'?'自动':requested.output_limit==='max'?'最大':requested.output_limit}。`+(effective.model_used===false?'本轮直接读取运行状态，未使用模型。':effective.max_output_tokens?`本轮可生成最多${effective.max_output_tokens}词元，思考和回答共用额度。`:'尚未开始模型生成。');details.append(summary,p);return details;}
  function messageNode(message){
    if(nodes.has(message.id))return;
    
    if(message.role==='assistant'){completedJobs.add(message.job);drafts.get(message.job)?.article.remove();drafts.delete(message.job);}
    const article=document.createElement('article');article.className='butler-message '+message.role;article.dataset.messageId=message.id;
    const who=document.createElement('b');who.textContent=message.role==='user'?'你':'管家';const body=document.createElement('div');body.className='butler-message-body';
    if(message.role==='user')body.textContent=message.content;else body.innerHTML=answerMarkdown(displayText(message.content,message.sources));article.append(who);if(message.reasoning)article.append(thoughtNode(message.reasoning,{...message,state:'completed'},false).details);const images=messageImages(message);if(images)article.append(images);article.append(body);if(message.role==='user')article.append(sourcesPanel(message));
    if(message.role==='assistant'){const unmatched=citationNote(message);if(unmatched)article.append(unmatched);if(message.requested?.operation==='direct_answer'){const note=document.createElement('p');note.className='butler-output-notice';note.textContent='本轮已按你的选择跳过思考，长期偏好保持。';article.append(note)}if(message.reasoning_notice&&message.content){const note=document.createElement('p');note.className='butler-output-notice';note.textContent=message.reasoning_notice;article.append(note)}if(!message.content&&message.requested?.thinking){article.dataset.recoveryJob=message.job;updateRecovery(article,message.job);}if(message.output_notice&&message.content){const notice=document.createElement('p');notice.className='butler-output-notice';notice.textContent=message.requested?.output_limit==='max'&&!message.content&&message.output_notice.includes('提高上限')?'生成额度已用完，尚未形成回答。当前已使用输入之后的全部可用额度。可选择跳过思考并回答，仅影响本轮。':message.output_notice;article.append(notice)}article.append(sourcesPanel(message));}reading.append(article);nodes.set(message.id,article);
  }
  function ensureDraft(id){
    let draft=drafts.get(id);if(draft)return draft;
    const article=document.createElement('article');article.className='butler-message streaming';const b=document.createElement('b');b.textContent='管家';const body=document.createElement('div');body.className='butler-message-body';const notice=document.createElement('p');notice.className='butler-output-notice';article.append(b,body,notice);reading.append(article);draft={id,article,body,notice,text:'',reasoning:'',thought:null};drafts.set(id,draft);return draft;
  }
  function updateDraft(draft,channel,text){
    if(channel==='reasoning'){draft.reasoning=text;if(!draft.thought){draft.thought=thoughtNode('',{...knownJobs.get(draft.id),state:'running'},false);draft.body.before(draft.thought.details)}draft.thought.rawText=text;if(draft.thought.details.open)draft.thought.content.textContent=text;thoughtLabel(draft.thought,knownJobs.get(draft.id)||{state:'running'});}
    else {draft.text=text;if(draft.answerFrame==null)draft.answerFrame=requestAnimationFrame(()=>{draft.answerFrame=null;if(drafts.get(draft.id)!==draft)return;draft.body.innerHTML=answerMarkdown(displayText(draft.text).slice(0,65536));if(draft.text.length>65536)draft.body.append(document.createTextNode('（内容较长，完整原文保存在本轮记录。）'));changed()});}
  }
  function delta(data){
    if(data.kind==='translation'){const current=translations.get(data.parent_job),existing=current?.id===data.id?current:knownJobs.get(data.id);if(!existing){refreshConversation();return}if(current&&current.id!==data.id&&Date.parse(current.created)>Date.parse(existing.created))return;const previous=existing.preview||'';if([...previous].length!==data.offset){refreshConversation();return}renderTranslation({...existing,...data,preview:previous+data.text});changed();return}
    if(completedJobs.has(data.id))return;
    if(data.reasoning_started_at||data.reasoning_finished_at){const existing=knownJobs.get(data.id)||{id:data.id,state:'running'};knownJobs.set(data.id,{...existing,reasoning_started_at:data.reasoning_started_at||existing.reasoning_started_at,reasoning_finished_at:data.reasoning_finished_at||existing.reasoning_finished_at})}
    const draft=ensureDraft(data.id),channel=data.channel||'answer',previous=channel==='reasoning'?draft.reasoning:draft.text;
    if([...previous].length!==data.offset){refreshConversation();return;}
    updateDraft(draft,channel,previous+data.text);if(channel==='reasoning')changed();
  }
  function syncDraft(value){
    if(value?.kind==='translation'){renderTranslation(value);return}
    value=currentJob(value);if(value&&completedJobs.has(value.id))return;
    if(!value||value.state==='completed'||!(value.preview||value.reasoning||drafts.has(value.id)))return;
    const draft=ensureDraft(value.id);
    for(const [channel,text] of [['answer',value.preview],['reasoning',value.reasoning]]){const old=channel==='reasoning'?draft.reasoning:draft.text;if(text&&[...text].length>=[...old].length)updateDraft(draft,channel,text)}
    draft.notice.textContent=value.preview&&terminal.has(value.state)?'本次生成已停止，以下片段未完成。':'';
    
    if(draft.thought){thoughtLabel(draft.thought,value);if(terminal.has(value.state)&&!draft.stopped){draft.thought.details.open=false;draft.stopped=true}if(draft.thought.translate)draft.thought.translate.disabled=!terminal.has(value.state);}
    if(terminal.has(value.state)&&!draft.info){draft.info=button('本轮详情',()=>openEvidence({...value,job:value.id,content:value.preview||'',sources:[]},true));draft.info.className='butler-message-actions';draft.article.append(draft.info)}
  }
  function renderConversation(data,reset=false){
    if(reset){reading.replaceChildren();nodes.clear();drafts.clear();completedJobs.clear();knownJobs.clear();thoughts.clear();translations.clear();follow=true;}
    $('#butlerConversationTitle').textContent=data.session.title||'新对话';
    for(const value of data.jobs)currentJob(value);
    for(const m of data.messages){messageNode(m);if(m.role==='user'){const run=data.jobs.find(j=>j.id===m.job);if(run&&terminal.has(run.state)&&run.state!=='completed')syncDraft(run)}}
    for(const article of nodes.values())if(article.dataset.recoveryJob)updateRecovery(article,article.dataset.recoveryJob);
    for(const translation of [...data.jobs].reverse())if(translation.kind==='translation')renderTranslation(translation);
    $('#butler .butler-chat').classList.toggle('is-empty',!data.messages.length);
    const latest=data.jobs.find(j=>!terminal.has(j.state))||data.jobs[0];renderJob(latest);syncDraft(latest);
    if(!data.messages.length&&!reading.children.length){const p=document.createElement('div');p.className='butler-empty';const title=document.createElement('h3');title.textContent='今天想聊些什么？';const hint=document.createElement('p');hint.textContent=window.ButlerControls?.canChat()===false?'先选择模型：下载到本机，或接入你已有的大模型API。历史与设置始终可用。':'从一句提问开始，把想法与知识连起来。';p.append(title,hint);if(window.ButlerControls?.canChat()===false)p.append(button('选择模型',()=>$('#butlerChooseModel').click()));reading.append(p);}
    else box.querySelector('.butler-empty')?.remove();syncEmptyState();changed();
  }
  async function refreshConversation(){const sid=selected,version=epoch;try{if(sid){const data=await api('/api/butler/sessions/'+sid);if(version===epoch&&sid===selected)renderConversation(data)}}catch(e){if(version===epoch)feedback(errorText(e))}}
  function renderSessions(rows){sessionRows=rows;
    const container=$('#butlerSessions');container.replaceChildren();
    let lastGroup='';const today=new Date();today.setHours(0,0,0,0);const yesterday=new Date(today);yesterday.setDate(today.getDate()-1);
    for(const s of rows){const stamp=new Date(s.updated||s.created),group=stamp>=today?'今天':stamp>=yesterday?'昨天':'更早';if(group!==lastGroup){const heading=document.createElement('h3');heading.className='butler-session-group';heading.textContent=group;container.append(heading);lastGroup=group}const row=document.createElement('div');row.className='butler-session-row';const open=button('',()=>select(s.id),'butler-session-open');const label=document.createElement('span');label.className='butler-session-title';label.textContent=s.title;open.append(label);open.title=s.title;if(s.id===selected){open.setAttribute('aria-current','true');$('#butlerConversationTitle').textContent=s.title}const more=button('',()=>{menuSession=s.id;popup($('#butlerSessionMenu'),more)},'butler-session-more');more.append(icon('ellipsis-horizontal'));more.setAttribute('aria-label','对话“'+s.title+'”的更多操作');more.setAttribute('aria-haspopup','menu');row.append(open,more);container.append(row);}

  }
  async function updateSessions(){const version=epoch;try{const data=await api('/api/butler/sessions');if(version===epoch)renderSessions(data.sessions)}catch(e){feedback(errorText(e))}}
  function runtime(value){window.ButlerControls?.runtime(value);busy();}
  function connect(){
    stream?.close();const version=++epoch,sid=selected;stream=new EventSource('/api/butler/events?sid='+encodeURIComponent(sid));
    const handle=(name,fn)=>stream.addEventListener(name,event=>{if(version!==epoch)return;try{fn(JSON.parse(event.data))}catch(e){feedback(errorText(e))}});
    handle('snapshot',data=>{renderSessions(data.sessions);runtime(data.runtime);if(data.conversation)renderConversation(data.conversation);else if(!nodes.size)renderJob(null);});
    handle('delta',delta);handle('job',renderJob);handle('tool_activity',data=>{window.ButlerControls?.activity(data);refreshConversation()});handle('refresh',()=>refreshConversation());handle('runtime',runtime);
    handle('session_changed',()=>updateSessions());handle('session_restored',()=>updateSessions());
    handle('sessions_changed',()=>{if(selected)api('/api/butler/sessions/'+selected).catch(()=>select(''));updateSessions()});
    handle('session_deleted',data=>{if(data.id===selected)select('');else updateSessions()});
    handle('unavailable',data=>{stream.close();feedback(data.message);busy()});
    stream.onerror=()=>{if(version===epoch)$('#butlerModel').textContent='连接中断，正在恢复…'};
  }
  function select(sid,transfer=false){drawer(false,false);const previous=draftKey(),row=inputDraft(previous);if($('#butlerQuestion').value!==row.text)saveInputDraft(previous,$('#butlerQuestion').value);const carry=transfer?{...inputDraft(previous)}:null;selected=sid;if(carry){saveInputDraft(draftKey(),carry.text,carry.revision);saveInputDraft(previous,'')}pendingDraft=null;if($('#butlerDraftDialog').open)$('#butlerDraftDialog').close();restoreInputDraft();window.ButlerControls?.setSession(sid);if(!transfer)restoreAttachmentDrafts(sid);$('#butlerConversationTitle').textContent='新对话';$('#butler .butler-chat').classList.add('is-empty');localStorage.setItem(memoryKey(),sid);reading.replaceChildren();nodes.clear();drafts.clear();completedJobs.clear();knownJobs.clear();thoughts.clear();translations.clear();renderJob(null);syncEmptyState();follow=true;connect();}
  async function trash(sid){try{await apiPost(`/api/butler/sessions/${sid}/archive`,{});if(selected===sid)select('');await updateSessions();showToast('已归档对话，可从已归档对话中恢复')}catch(e){feedback(errorText(e))}}
  async function trashList(){const data=await api('/api/butler/sessions?deleted=true',{ttl:0,force:true});const c=$('#butlerTrashList');c.replaceChildren();if(!data.sessions.length)c.textContent='暂无已归档对话。';for(const s of data.sessions){const row=document.createElement('div');row.className='butler-session-row archive-conversation-row';const title=document.createElement('span');title.textContent=s.title;const actions=document.createElement('div');actions.className='row-actions';actions.append(button('取消归档',async()=>{try{await apiPost(`/api/butler/sessions/${s.id}/unarchive`,{});await trashList();await updateSessions();showToast('已取消归档')}catch(e){showToast(errorText(e))}}),button('永久删除…',()=>confirmDelete(s)));row.append(title,actions);c.append(row);}}
  const deleteDialog=document.createElement('dialog');deleteDialog.className='butler-dialog';deleteDialog.id='butlerDeleteConfirm';deleteDialog.innerHTML='<h2>删除这段对话？</h2><p id="butlerDeleteObject"></p><p class="delete-note">消息和运行记录将永久删除，无法恢复。独立原始档案保留。</p><p id="butlerDeleteError" role="alert"></p><div class="delete-actions"><button id="butlerDeleteCancel" type="button">取消</button><button id="butlerDeleteCommit" type="button" class="danger-button">删除</button></div>';$('#butler').append(deleteDialog);let deletion=null,deleteBusy=false;
  function confirmDelete(item){if(deleteBusy)return;deletion={id:item.id,title:item.title};$('#butlerDeleteObject').textContent=item.title;$('#butlerDeleteError').textContent='';$('#butlerDeleteCommit').disabled=false;$('#butlerDeleteCancel').disabled=false;deleteDialog.showModal();$('#butlerDeleteCancel').focus()}
  $('#butlerDeleteCancel').onclick=()=>{if(!deleteBusy)deleteDialog.close()};
  deleteDialog.addEventListener('cancel',e=>{if(deleteBusy)e.preventDefault()});
  $('#butlerDeleteCommit').onclick=async()=>{if(deleteBusy||!deletion)return;const target={...deletion},b=$('#butlerDeleteCommit'),cancel=$('#butlerDeleteCancel');deleteBusy=true;b.disabled=true;cancel.disabled=true;try{await apiPost(`/api/butler/sessions/${target.id}/delete`,{confirmed:true,expected_title:target.title});deleteDialog.close();if(selected===target.id)select('');await updateSessions();if($('#butlerTrashDialog').open)await trashList();showToast('已删除这段对话')}catch(e){$('#butlerDeleteError').textContent=errorText(e)}finally{deleteBusy=false;b.disabled=false;cancel.disabled=false}};
  $('#butlerPermanentDeleteSession').onclick=()=>{$('#butlerSessionMenu').hidePopover();const item=sessionRows.find(s=>s.id===menuSession);if(item)confirmDelete(item)};
  $('#butlerTrash').onclick=()=>{$('#butlerTrashDialog').showModal();trashList().catch(e=>{$('#butlerTrashList').textContent=errorText(e)})};
  $('#butlerNew').onclick=async()=>{
    if(newSessionPending())return;const ticket={key:draftKey(),workspace:memoryKey(),version:epoch,revision:inputDraft().revision,text:$('#butlerQuestion').value};creatingSession=ticket;busy();
    try{const s=await apiPost('/api/butler/sessions',{});if(creatingSession!==ticket||!newSessionPending())return;const edited=inputDraft(ticket.key).revision!==ticket.revision||$('#butlerQuestion').value!==ticket.text;select(s.id,edited);$('#butlerQuestion').focus()}
    catch(e){if(creatingSession===ticket&&newSessionPending())feedback(errorText(e))}finally{if(creatingSession===ticket){creatingSession=null;busy()}}
  };
  $('#butlerForm').onsubmit=async e=>{
    e.preventDefault();const question=$('#butlerQuestion').value.trim();if(newSessionPending()){feedback('正在创建新对话，输入已保留，请稍候再发送。');return}if(imageRestoring()){feedback('正在恢复图片草稿，请稍候。');return}if(window.ButlerControls?.canChat()!==true){const state=window.ButlerControls?.modelStatus?.();feedback(state==='loading'?'正在读取模型，请稍候。':state==='error'?'模型读取失败，请点击模型入口重试。':'请先选择已配置的模型；历史对话与设置仍可使用');$('#butlerChooseModel').focus();return}const imageRows=attachmentRows();if((!question&&!imageRows.length)||submitting||composing||(job&&!terminal.has(job.state)))return;if(imageRows.some(row=>row.state!=='ready')){feedback('请等待图片上传完成；失败图片请重试或移除。');return}if(imageRows.length&&!visionSupported()){feedback('当前模型不支持这些图片，请切换模型或移除图片。');return}const imageIds=imageRows.map(row=>row.metadata.id);
    const ticket={key:draftKey(),...inputDraft()},requestSession=selected,requestWorkspace=memoryKey();
    const thinking=$('#butlerThinking').getAttribute('aria-pressed')==='true',limit=$('#butlerLimit').value,output_limit=['auto','max'].includes(limit)?limit:Number(limit);
    submitting=true;busy();feedback('正在保存请求…');
    try{let sid=requestSession;if(!sid){sid=(await apiPost('/api/butler/sessions',{})).id;if(memoryKey()!==requestWorkspace)return;if(selected===requestSession){select(sid,true);ticket.key=draftKey(sid)}}await apiPost(`/api/butler/sessions/${sid}/ask`,{question,attachments:imageIds,thinking,output_limit,tools_enabled:window.ButlerControls?.toolsEnabled()===true,scope:$('#butlerScope').value,username:prefs.username==='我'?'':prefs.username||''});clearSentImages(ticket.key,imageIds);clearSubmittedDraft(ticket);if(memoryKey()===requestWorkspace&&sid===selected){await refreshConversation();follow=true;scrollLatest()}}
    catch(error){feedback(errorText(error))}finally{submitting=false;busy();}
  };
  const input=$('#butlerQuestion');input.addEventListener('input',rememberDraft);input.addEventListener('compositionstart',()=>{composing=true});input.addEventListener('compositionend',()=>{composing=false;imeUntil=performance.now()+80});
  input.addEventListener('keydown',e=>{if(e.key!=='Enter'||e.shiftKey)return;if(e.isComposing||composing||e.keyCode===229||performance.now()<imeUntil)return;e.preventDefault();if(!e.repeat)$('#butlerForm').requestSubmit()});
  $('#butlerCancel').onclick=async()=>{if(!job)return;const b=$('#butlerCancel');b.disabled=true;try{await apiPost(`/api/butler/jobs/${job.id}/cancel`,{});await refreshConversation()}catch(e){feedback(errorText(e))}finally{b.disabled=false}};
  async function readSource(append=false){const version=sourceEpoch,url=sourceURL;const data=await api(url+(sourceCursor?'?cursor='+encodeURIComponent(sourceCursor):''));if(version!==sourceEpoch)return;$('#butlerSourceTitle').textContent=sourceLabel(data.sent);$('#butlerSentSource').textContent=data.sent.content||'本轮正文因长度限制省略';$('#butlerSourceNote').textContent=`记录版本：${data.sent.version}。${data.version_changed?'原文已变化，下面是当前版本。':'这是提供给本轮回答的片段，不代表读完全文。'}${data.error||''}`;$('#butlerCurrentSource').textContent=(append?$('#butlerCurrentSource').textContent:'')+(data.current?.content||data.current?.text||'');sourceCursor=data.current?.next_cursor||null;$('#butlerSourceNext').hidden=!sourceCursor;}
  async function openSource(jid,id){sourceEpoch++;sourceURL=`/api/butler/jobs/${jid}/sources/${id}`;sourceCursor=null;$('#butlerSourceTitle').textContent='正在读取来源';$('#butlerSentSource').textContent='';$('#butlerCurrentSource').textContent='';$('#butlerSourceNote').textContent='';$('#butlerSourceNext').hidden=true;$('#butlerSource').showModal();try{await readSource()}catch(e){$('#butlerSourceNote').textContent=errorText(e)}}
  $('#butlerSourceNext').onclick=async()=>{const b=$('#butlerSourceNext');b.disabled=true;try{await readSource(true)}catch(e){$('#butlerSourceNote').textContent=errorText(e)}finally{b.disabled=false}};
  async function openSnapshot(jid){$('#butlerSnapshotBody').textContent='正在读取保存的记录…';$('#butlerSnapshot').showModal();try{const data=await api(`/api/butler/jobs/${jid}/snapshot`),s=data.snapshot;const rows=[['记录时间',s.captured_at],['管家模型',s.local_model?.model],['模型状态',s.local_model?.state],['向量模型',s.vector_model?.state],['索引待处理',s.evidence_queue?.pending],['最近索引结果',s.evidence_queue?.last_result],['Hermes最近检查',s.hermes?.last_live_sync||s.hermes?.last_sync],['本轮检索',data.retrieval?.effective_mode],['检索说明',data.retrieval?.fallback_reason||'无'],['产品目录版本',data.retrieval?.catalog_version],['输入词元',data.budget?.input_tokens],['历史消息',data.budget?.history_messages]];$('#butlerSnapshotBody').innerHTML='<dl>'+rows.map(([k,v])=>`<dt>${esc(k)}</dt><dd>${esc(v??'未记录')}</dd>`).join('')+'</dl>'}catch(e){$('#butlerSnapshotBody').textContent=errorText(e)}}
  window.leaveButler=()=>{drawer(false,false);document.querySelectorAll('#butler [popover]').forEach(p=>{if(p.matches(':popover-open'))p.hidePopover()});imageRestoreRequests.clear();epoch++;stream?.close();stream=null;clearInterval(elapsedTimer)};
  window.loadButler=async()=>{if(activePage!=='butler')return;loadChoices();if(stream)return;try{const version=epoch;const data=await api('/api/butler/sessions');if(activePage!=='butler'||version!==epoch)return;const stored=selected||localStorage.getItem(memoryKey());selected=data.sessions.some(s=>s.id===stored)?stored:data.sessions[0]?.id||'';try{const legacy=sessionStorage.getItem(memoryKey()+'.draft');if(legacy!==null){if(stored===selected&&sessionStorage.getItem(draftKey())===null)saveInputDraft(draftKey(),legacy);sessionStorage.removeItem(memoryKey()+'.draft')}}catch(_){}restoreInputDraft();window.ButlerControls?.setSession(selected);restoreAttachmentDrafts(selected);await window.ButlerControls?.load();connect()}catch(e){feedback(errorText(e))}};
  if(activePage==='butler')window.loadButler();
})();
