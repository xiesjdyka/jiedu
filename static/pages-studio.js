/* 子页面增强（预览重构）：知识页 —— 列表筛选 / 文件夹折叠 / Markdown 排版阅读 */
(function(){'use strict';
const $=(s,r=document)=>r.querySelector(s),$$=(s,r=document)=>[...r.querySelectorAll(s)];
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

/* ---------- 轻量 Markdown 渲染（只读展示，全部转义，不执行任何内容） ---------- */
function inline(t){
  return esc(t)
    .replace(/`([^`]+)`/g,'<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g,'$1<em>$2</em>')
    .replace(/\[\[([^\]|]+)(?:\|([^\]]+))?\]\]/g,(_,a,b)=>`<span class="md-ref" title="${a}">${b||a}</span>`)
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g,'<span class="md-ref" title="$2">$1</span>');
}
function renderMarkdown(src){
  const lines=String(src||'').replace(/\r\n?/g,'\n').split('\n'),out=[];let i=0,para=[],list=null;
  const flushPara=()=>{if(para.length){out.push(`<p>${inline(para.join(' '))}</p>`);para=[]}};
  const flushList=()=>{if(list){out.push(`<${list.tag}>${list.items.map(x=>`<li>${inline(x)}</li>`).join('')}</${list.tag}>`);list=null}};
  const flush=()=>{flushPara();flushList()};
  if(lines[0]?.trim()==='---'){const end=lines.indexOf('---',1);if(end>0){out.push(`<div class="md-front">${lines.slice(1,end).map(l=>`<span>${esc(l)}</span>`).join('')}</div>`);i=end+1}}
  for(;i<lines.length;i++){
    const l=lines[i],t=l.trim();
    if(/^```/.test(t)){flush();const buf=[];i++;while(i<lines.length&&!/^```/.test(lines[i].trim()))buf.push(lines[i++]);out.push(`<pre class="md-code">${esc(buf.join('\n'))}</pre>`);continue}
    if(!t){flush();continue}
    let m;
    if((m=t.match(/^(#{1,6})\s+(.*)$/))){flush();const n=Math.min(m[1].length,4);out.push(`<h${n}>${inline(m[2])}</h${n}>`);continue}
    if(/^(-{3,}|\*{3,}|_{3,})$/.test(t)){flush();out.push('<hr>');continue}
    if(/^\|.*\|$/.test(t)){flush();const rows=[];while(i<lines.length&&/^\|.*\|$/.test(lines[i].trim()))rows.push(lines[i++].trim());i--;
      const cells=r=>r.slice(1,-1).split('|').map(c=>c.trim());const body=rows.filter(r=>!/^\|[\s:|-]+\|$/.test(r));
      out.push(`<div class="md-table"><table>${body.map((r,k)=>`<tr>${cells(r).map(c=>k===0&&rows[1]&&/^\|[\s:|-]+\|$/.test(rows[1])?`<th>${inline(c)}</th>`:`<td>${inline(c)}</td>`).join('')}</tr>`).join('')}</table></div>`);continue}
    if((m=t.match(/^>\s?(.*)$/))){flush();const buf=[m[1]];while(i+1<lines.length&&/^>\s?/.test(lines[i+1].trim()))buf.push(lines[++i].trim().replace(/^>\s?/,''));out.push(`<blockquote>${inline(buf.join(' '))}</blockquote>`);continue}
    if((m=t.match(/^[-*+]\s+(?:\[( |x|X)\]\s+)?(.*)$/))){flushPara();if(!list||list.tag!=='ul'){flushList();list={tag:'ul',items:[]}}list.items.push((m[1]?(m[1]===' '?'☐ ':'☑ '):'')+m[2]);continue}
    if((m=t.match(/^\d+[.)、]\s+(.*)$/))){flushPara();if(!list||list.tag!=='ol'){flushList();list={tag:'ol',items:[]}}list.items.push(m[1]);continue}
    if(list&&/^\s{2,}\S/.test(l)){list.items[list.items.length-1]+=' '+t;continue}
    flushList();para.push(t);
  }
  flush();return out.join('');
}
let readerMode='pretty';
function enhanceReader(){
  const chunk=$('#reader .chunk');if(!chunk||chunk.dataset.enhanced)return;
  const pre=$(':scope>pre',chunk);if(!pre)return;chunk.dataset.enhanced='1';
  const view=document.createElement('div');view.className='md-view selectable';view.innerHTML=renderMarkdown(pre.textContent);pre.after(view);
  const head=$('.chunk-head',chunk);
  if(head){const seg=document.createElement('div');seg.className='reader-mode';seg.setAttribute('role','group');seg.setAttribute('aria-label','阅读方式');
    seg.innerHTML='<button type="button" data-mode="pretty">排版</button><button type="button" data-mode="raw">原文</button>';head.prepend(seg);
    seg.addEventListener('click',e=>{const b=e.target.closest('[data-mode]');if(!b)return;readerMode=b.dataset.mode;apply()})}
  const apply=()=>{chunk.classList.toggle('raw-mode',readerMode==='raw');$$('.reader-mode button',chunk).forEach(b=>b.classList.toggle('on',b.dataset.mode===readerMode))};apply();
  const title=$('#reader .doc-title');if(title)title.textContent=title.textContent.replace(/\.md$/i,'');
}

/* ---------- 知识列表：筛选 / 折叠 / 精简 ---------- */
const collapsed=new Set();let filterText='';
function tidyList(){
  const list=$('#fileList');if(!list)return;
  $$('.file-group',list).forEach(g=>{
    const head=$('.file-group-title',g),name=$('b',head)?.textContent||'';
    if(head&&!head.dataset.fold){head.dataset.fold='1';head.setAttribute('role','button');head.tabIndex=0;head.title='点一下折叠 / 展开';
      head.insertAdjacentHTML('afterbegin','<i class="fold-caret" aria-hidden="true"></i>');
      const toggle=()=>{g.classList.toggle('collapsed');g.classList.contains('collapsed')?collapsed.add(name):collapsed.delete(name)};
      head.addEventListener('click',toggle);head.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();toggle()}})}
    g.classList.toggle('collapsed',collapsed.has(name)&&!filterText);
    $$('.knowledge-object-row',g).forEach(row=>{
      const p=$('.file-path',row),parent=$('.file-parent',row);
      if(p&&!p.dataset.tidy){p.dataset.tidy='1';p.textContent=p.textContent.replace(/\.md$/i,'')}
      if(parent&&!parent.dataset.tidy){parent.dataset.tidy='1';const rest=parent.textContent.split('/').slice(1).join(' / ');parent.textContent=rest;parent.hidden=!rest}
    });
  });
  applyFilter();
}
function applyFilter(){
  const list=$('#fileList');if(!list)return;const q=filterText.toLowerCase();let shown=0;
  $$('.file-group',list).forEach(g=>{let n=0;$$('.knowledge-object-row',g).forEach(row=>{const hit=!q||row.textContent.toLowerCase().includes(q);row.hidden=!hit;if(hit)n++});g.hidden=q&&!n;if(q&&n)g.classList.remove('collapsed');shown+=n});
  let none=$('.kfilter-none',list);
  if(q&&!shown){if(!none){none=document.createElement('div');none.className='kfilter-none';list.append(none)}none.innerHTML=`<span>没有叫「${esc(filterText)}」的资料</span><button type="button" class="copy-button" data-full-search>在全文里搜</button>`;none.querySelector('[data-full-search]').onclick=()=>{if(typeof window.openKnowledgeSearch==='function')window.openKnowledgeSearch('workspace');setTimeout(()=>{const input=$('#sharedQueryInput');if(input){input.value=filterText;input.dispatchEvent(new Event('input',{bubbles:true}))}},80)}}
  else none?.remove();
}
function setupKnowledge(){
  const page=$('#files');if(!page||page.dataset.studio)return;page.dataset.studio='1';
  const bar=$('.knowledge-object-toolbar',page),metrics=$('#knowledgeObjectMetrics');
  if(bar&&metrics){bar.prepend(metrics)}
  const listPanel=$('.file-list',page),filters=$('.knowledge-object-filters',page);
  if(listPanel&&filters&&!$('#knowledgeNameFilter')){
    const box=document.createElement('label');box.className='kfilter';box.innerHTML='<span class="sr-only">筛选文件名</span><input id="knowledgeNameFilter" type="search" placeholder="筛选文件名" autocomplete="off">';
    filters.before(box);
    let t;box.querySelector('input').addEventListener('input',e=>{clearTimeout(t);t=setTimeout(()=>{filterText=e.target.value.trim();applyFilter()},120)});
  }
  const listHost=$('#fileList');if(listHost)new MutationObserver(()=>tidyList()).observe(listHost,{childList:true});
  const reader=$('#reader');if(reader)new MutationObserver(()=>enhanceReader()).observe(reader,{childList:true,subtree:false});
  tidyList();enhanceReader();
}
/* ---------- 管家：液态光核空状态 + 去掉重复说明 ---------- */
function markButlerNav(){$$('#butlerSettingsDialog .butler-settings-nav button').forEach(b=>b.classList.toggle('is-on',b.getAttribute('aria-current')==='page'))}
function setupButler(){
  const nav=$('#butlerSettingsDialog .butler-settings-nav');if(nav){nav.addEventListener('click',()=>setTimeout(markButlerNav,30));new MutationObserver(markButlerNav).observe($('#butlerSettingsDialog'),{attributes:true,attributeFilter:['open']})}
  const title=$('#butler .butler-welcome-title');
  if(title&&!$('.butler-orb',title))title.insertAdjacentHTML('afterbegin','<span class="butler-orb" aria-hidden="true"><span class="core-plate"></span></span>');
  const tidy=()=>$$('#butlerSettingsDialog p').forEach(p=>{if(/^文件访问范围在“权限”中设置/.test(p.textContent.trim()))p.classList.add('studio-dup-note')});
  tidy();const dlg=$('#butlerSettingsDialog');if(dlg)new MutationObserver(tidy).observe(dlg,{attributes:true,attributeFilter:['open']});
}
/* ---------- 连接 AI：按状态文字给客户端 / 聊天来源打上颜色 ---------- */
const toneOf=t=>/需要|失效|异常|失败|错误|重新/.test(t)?'warn':/已连接|已验证|持续|正常|已启用|运行中|读取已验证|读取成功/.test(t)?'ok':'idle';
function tagAccess(){
  $$('#access .client-identity-card').forEach(card=>{
    const tone=$('.identity-status',card)?.textContent==='已连接'?'ok':'idle';
    if(card.dataset.tone!==tone)card.dataset.tone=tone;
  });
  const other=$('#access .other-client-identities');if(other&&!other.dataset.studioOpen){other.dataset.studioOpen='1';other.open=true}
  $$('#access .local-chat-card').forEach(c=>{const t=$('.local-chat-card-head strong',c)?.textContent||'';const v=toneOf(t);if(c.dataset.tone!==v)c.dataset.tone=v});
  accessLock();
}
/* r24：未连接时，权限组照常展示但覆一层磨砂锁定；连接后平滑解锁 */
const LOCK_GROUPS=[['知识读取',['读取核心背景','读取项目资料','搜索普通知识','读取已有聊天原文']],['聊天保存',['允许客户端提交聊天']],['允许操作',['创建整理建议','提交建议供核对']]];
const lockSwitch=(b,s,on)=>`<label class="client-access-switch"><span><b>${b}</b>${s?`<small>${s}</small>`:''}</span><input type="checkbox" ${on?'checked':''} disabled tabindex="-1"><i></i></label>`;
let accessPrev={};
function accessLock(){const bc=$('#access .branded-connect');
  if(bc&&!$('.studio-lock',bc)){const name=$('.branded-connect-overlay h3',bc)?.textContent.replace(/^连接\s*/,'').trim()||'';const lk=document.createElement('div');lk.className='studio-lock';
    lk.innerHTML=`<div class="sl-body" inert><section class="legacy-permissions"><h4>${escH(name)} 的权限</h4>${lockSwitch('允许此连接访问界渡','连接成功后，可随时在这里收回',true)}<div class="permission-groups">${LOCK_GROUPS.map(([g,items])=>`<details open><summary><b>${g}</b></summary><div>${items.map(t=>lockSwitch(t,'',true)).join('')}</div></details>`).join('')}</div></section></div><div class="sl-veil"><span class="sl-pill"><i></i>复制接入提示词发给智能体，连接成功后自动解锁</span></div>`;
    bc.append(lk);
  }
  const key=$('#access .client-identity-card[aria-pressed="true"]')?.dataset.selectClient||'';const panel=$('#access .client-permission-panel');if(!panel||!key)return;
  const tone=$('#access .client-identity-card[aria-pressed="true"]')?.dataset.tone||'';if(panel.dataset.tone!==tone)panel.dataset.tone=tone;
  const state=$('.branded-connect',panel)?'locked':$('.legacy-permissions,.permission-groups',panel)?'open':'other';
  if(accessPrev[key]==='locked'&&state==='open'){panel.classList.remove('sl-unlock');void panel.offsetWidth;panel.classList.add('sl-unlock');setTimeout(()=>panel.classList.remove('sl-unlock'),1200)}
  accessPrev[key]=state}
function setupAccess(){const hub=$('#access .access-hub');if(!hub)return;tagAccess();new MutationObserver(()=>tagAccess()).observe(hub,{childList:true,subtree:true,characterData:true})}
/* ---------- 品牌标：双门 + 绸带，矢量重绘，深浅双色自适应 ---------- */
let markSeq=0;
function jieduMark(){const k='jm'+(++markSeq);return `<svg class="jiedu-mark" viewBox="0 0 48 48" aria-hidden="true">
  <defs><linearGradient id="${k}d" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="var(--mark-a)"/><stop offset="1" stop-color="var(--mark-b)"/></linearGradient>
  <linearGradient id="${k}r" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="var(--mark-r1)"/><stop offset="1" stop-color="var(--mark-r2)"/></linearGradient></defs>
  <path class="jm-door" fill="url(#${k}d)" fill-rule="evenodd" d="M7.5 8.6 21.6 14.6v18.8L7.5 39.4zM11 14v20l7.1-3V17z"/>
  <path class="jm-door" fill="url(#${k}d)" fill-rule="evenodd" d="M40.5 8.6 26.4 14.6v18.8l14.1 6zM37 14v20l-7.1-3V17z"/>
  <path class="jm-ribbon" stroke="url(#${k}r)" d="M12.6 18.9C19.2 16.2 23.6 17.8 25.6 23c1.7 4.4 4.9 6.6 10 5.5"/>
</svg>`}
function setupMarks(){
  const side=$('.identity .system-mark');if(side&&!$('.jiedu-mark',side)){side.innerHTML=jieduMark();side.classList.add('vector')}
  const bb=$('.butler-brand-logo');if(bb&&bb.tagName==='IMG'){const w=document.createElement('span');w.className='butler-brand-logo vector';w.innerHTML=jieduMark();bb.replaceWith(w)}
}
/* ---------- 设置：「本次更新」并进「关于界渡与更新」 ---------- */
function mergeUpdate(){const card=$('#protection .software-update-card'),note=$('#updateNoticeSettings');if(card&&note&&note.parentElement!==card)card.append(note)}
function setupSettings(){const page=$('#protection');if(!page)return;mergeUpdate();new MutationObserver(mergeUpdate).observe(page,{childList:true})}
/* ---------- 工作区：新会话启动 + 提示词编辑收进抽屉（刷新后自动重新收好） ---------- */
function wrapWorkspace(){
  const host=$('#knowledgeBaseStatus');if(!host)return;
  const parts=[$('.startup-instruction',host),$('#workspaceStartupPrompt',host)].filter(x=>x&&!x.closest('.studio-drawer'));
  if(!parts.length)return;
  let d=$('.studio-drawer[data-k="startup"]',host);
  if(!d){d=document.createElement('details');d.className='studio-drawer';d.dataset.k='startup';d.innerHTML='<summary>新会话启动与提示词</summary>';parts[0].before(d)}
  parts.forEach(x=>d.append(x));
}
function setupWorkspace(){const host=$('#knowledgeBaseStatus');if(!host)return;wrapWorkspace();new MutationObserver(()=>wrapWorkspace()).observe(host,{childList:true})}
/* ---------- 自定义下拉框：接管全站原生 <select>，弹出界渡自己的玻璃菜单 ---------- */
let selPop=null,selOwner=null,selActive=-1;
function selLabel(sel){const o=sel.options[sel.selectedIndex];return o?o.textContent.trim():(sel.getAttribute('placeholder')||'请选择')}
function syncSelect(sel){const w=sel.parentElement;if(!w||!w.classList.contains('s-select'))return;const b=w.querySelector('.s-select-btn');const t=selLabel(sel);if(b.firstChild.textContent!==t)b.firstChild.textContent=t;b.disabled=sel.disabled;w.hidden=sel.hidden||getComputedStyle(sel).display==='none'&&sel.style.display==='none';b.title=t}
function enhanceSelect(sel){
  if(sel.dataset.studioSel||sel.multiple||sel.size>1||sel.closest('.s-select'))return;sel.dataset.studioSel='1';
  const full=sel.offsetWidth&&sel.parentElement&&sel.offsetWidth>=sel.parentElement.clientWidth-6;
  const w=document.createElement('span');w.className='s-select'+(full?' full':'');
  sel.before(w);w.append(sel);
  const b=document.createElement('button');b.type='button';b.className='s-select-btn';b.setAttribute('aria-haspopup','listbox');
  b.innerHTML='<span></span><i aria-hidden="true"></i>';const lab=sel.id&&document.querySelector(`label[for="${CSS.escape(sel.id)}"]`);b.setAttribute('aria-label',(sel.getAttribute('aria-label')||lab?.textContent||sel.closest('label')?.textContent||'选择').trim().slice(0,40));
  w.append(b);syncSelect(sel);
  b.addEventListener('click',e=>{e.stopPropagation();selOwner===sel?closeSel():openSel(sel,b)});
  b.addEventListener('keydown',e=>{if(['ArrowDown','ArrowUp'].includes(e.key)){e.preventDefault();openSel(sel,b)}});
  sel.addEventListener('change',()=>syncSelect(sel));
  /* r46：代码里直接改 value / selectedIndex 不会触发事件，这里在实例上包一层，赋值后立即同步按钮文字，取代 800ms 轮询 */
  for(const k of ['value','selectedIndex']){const d=Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,k);if(d?.set)Object.defineProperty(sel,k,{configurable:true,get(){return d.get.call(this)},set(v){d.set.call(this,v);syncSelect(this)}})}
  new MutationObserver(()=>syncSelect(sel)).observe(sel,{childList:true,subtree:true,attributes:true,attributeFilter:['disabled','hidden','style']});
}
function openSel(sel,btn){
  closeSel();selOwner=sel;const host=btn.closest('dialog[open]')||document.body;
  selPop=document.createElement('div');selPop.className='s-select-pop';selPop.setAttribute('role','listbox');
  const opts=[...sel.options];
  selPop.innerHTML='';[...sel.children].forEach(ch=>{const add=o=>{const i=opts.indexOf(o);const el=document.createElement('div');el.className='s-opt'+(o.selected?' sel':'')+(o.disabled?' dis':'');el.setAttribute('role','option');el.dataset.i=i;el.innerHTML=`<span>${esc(o.textContent.trim())}</span><i aria-hidden="true"></i>`;selPop.append(el)};if(ch.tagName==='OPTGROUP'){selPop.insertAdjacentHTML('beforeend',`<div class="s-group">${esc(ch.label)}</div>`);[...ch.children].forEach(add)}else if(ch.tagName==='OPTION')add(ch)});
  host.append(selPop);
  const r=btn.getBoundingClientRect(),H=Math.min(selPop.scrollHeight,320),below=innerHeight-r.bottom-12;
  const hr=host===document.body?{left:0,top:0}:host.getBoundingClientRect();
  const inDlg=host!==document.body,sx=inDlg?host.scrollLeft:0,sy=inDlg?host.scrollTop:0;selPop.style.position=inDlg?'absolute':'fixed';
  selPop.style.minWidth=Math.max(r.width,160)+'px';selPop.style.left=(r.left-hr.left+sx)+'px';
  selPop.style.top=((below>=H||below>=r.top-12?r.bottom+6:r.top-6-H)-hr.top+sy)+'px';selPop.style.maxHeight=Math.max(140,Math.min(320,below>=H?below:r.top-12))+'px';
  btn.classList.add('open');selActive=sel.selectedIndex;markSel();
  requestAnimationFrame(()=>selPop?.classList.add('in'));
  selPop.addEventListener('click',e=>{const o=e.target.closest('.s-opt');if(!o||o.classList.contains('dis'))return;pickSel(Number(o.dataset.i))});
  selPop.addEventListener('pointermove',e=>{const o=e.target.closest('.s-opt');if(o){selActive=Number(o.dataset.i);markSel(false)}});
}
function markSel(scroll=true){if(!selPop)return;selPop.querySelectorAll('.s-opt').forEach(o=>o.classList.toggle('act',Number(o.dataset.i)===selActive));const a=selPop.querySelector('.s-opt.act');if(a&&scroll)a.scrollIntoView({block:'nearest'})}
function pickSel(i){const sel=selOwner;if(!sel)return;if(sel.selectedIndex!==i){sel.selectedIndex=i;sel.dispatchEvent(new Event('input',{bubbles:true}));sel.dispatchEvent(new Event('change',{bubbles:true}))}syncSelect(sel);const b=sel.parentElement.querySelector('.s-select-btn');closeSel();b?.focus()}
function closeSel(){if(selPop){selPop.remove();selPop=null}if(selOwner)selOwner.parentElement?.querySelector('.s-select-btn')?.classList.remove('open');selOwner=null}
document.addEventListener('click',e=>{if(selPop&&!e.target.closest('.s-select-pop'))closeSel()});
document.addEventListener('keydown',e=>{if(!selPop||!selOwner)return;const opts=[...selOwner.options];
  if(e.key==='Escape'){e.preventDefault();e.stopPropagation();const b=selOwner.parentElement.querySelector('.s-select-btn');closeSel();b?.focus();return}
  if(e.key==='ArrowDown'||e.key==='ArrowUp'){e.preventDefault();let i=selActive;do{i+=e.key==='ArrowDown'?1:-1}while(i>=0&&i<opts.length&&opts[i].disabled);if(i>=0&&i<opts.length){selActive=i;markSel()}}
  if(e.key==='Enter'||e.key===' '){e.preventDefault();if(selActive>=0)pickSel(selActive)}
},true);
addEventListener('resize',closeSel);document.addEventListener('scroll',e=>{if(selPop&&!e.target.closest?.('.s-select-pop'))closeSel()},true);
function enhanceAllSelects(root=document){root.querySelectorAll('main select, dialog select').forEach(enhanceSelect)}
let selTimer=0;
function setupTopbar(){const f=()=>{const y=(document.scrollingElement||document.documentElement).scrollTop>4;$$('.topbar').forEach(t=>t.classList.toggle('is-stuck',y))};addEventListener('scroll',f,{passive:true});addEventListener('resize',f);f()}
function tidyCounts(){$$('#pendingMemoryList span,#pendingMemoryList p').forEach(e=>{if(!e.children.length)e.classList.toggle('studio-zero',/^共\s*0\s*条$/.test(e.textContent.trim()))})}
function setupTidy(){const h=$('#pendingMemoryList');if(!h)return;tidyCounts();new MutationObserver(tidyCounts).observe(h,{childList:true,subtree:true,characterData:true})}
function setupSelects(){enhanceAllSelects();new MutationObserver(()=>{clearTimeout(selTimer);selTimer=setTimeout(()=>enhanceAllSelects(),60)}).observe(document.body,{childList:true,subtree:true});
  /* 页面切换后可见性可能变化，切页时补同步一次即可 */
  document.addEventListener('click',e=>{if(e.target.closest('[data-page],[data-go]'))setTimeout(()=>document.querySelectorAll('main .page.active .s-select>select').forEach(syncSelect),60)},true)}

/* ---- r23：故障排查健康总览 / 知识与聊天记录空状态 / 设置页关于卡 ---- */
const escH=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
/* r46：旧故障排查健康条（healthTone / setupHealth）已删除，故障排查页由 diagnostics.js 渲染 */
function folderTiles(){return $$('#fileList .file-group-title').map(t=>{const raw=t.querySelector('b')?.textContent.trim()||'',n=t.querySelector('span')?.textContent.trim()||'';const m=raw.match(/^(\d+)_(.*)$/);return {raw,no:m?m[1]:'',name:m?m[2]:raw,n,el:t}})}
function renderFilesEmpty(e){const tiles=folderTiles();const sig=tiles.map(t=>t.raw+t.n).join('|');if(e.dataset.studio===sig)return;e.dataset.studio=sig;e.classList.add('studio-empty');
  e.innerHTML=`<div class="se-orb"><i class="se-ico se-book"></i></div><span>选一份资料开始阅读</span><p>正文、来源和保存状态会显示在这里</p>`+(tiles.length?`<div class="se-label">按目录浏览</div><div class="se-folders">${tiles.slice(0,8).map((t,i)=>`<button type="button" class="se-folder" data-i="${i}"><small>${escH(t.no)}</small><b>${escH(t.name)}</b><em>${escH(t.n)} 份</em></button>`).join('')}</div>`:'')+`<div class="se-actions"><button type="button" class="secondary" data-act="search">搜索本机资料</button><button type="button" class="primary" data-act="import">导入资料</button></div>`}
function renderArchiveEmpty(e){const none=/没有找到会话/.test($('#archives')?.textContent||'');const sig=none?'none':'some';if(e.dataset.studio===sig)return;e.dataset.studio=sig;e.classList.add('studio-empty');
  e.innerHTML=`<div class="se-orb"><i class="se-ico se-chat"></i></div>`+(none?`<span>还没有保存的会话</span><p>连接 AI 后，聊过的内容会自动保存到这里</p>`:`<span>选一场会话查看原文</span><p>在左侧选中会话，聊天原文和已取得的附件会显示在这里</p>`)+`<div class="se-actions"><button type="button" class="secondary" data-act="sources">管理来源</button><button type="button" class="primary" data-act="access">去连接 AI</button></div>`}
function setupEmpty(){const run=()=>{$$('#files .content-panel .empty').forEach(renderFilesEmpty);$$('#rawSessionDetail .empty').forEach(renderArchiveEmpty)};
  document.addEventListener('click',e=>{const f=e.target.closest('.se-folder');if(f){const t=folderTiles()[+f.dataset.i];if(t){if(t.el.closest('.file-group')?.classList.contains('folded'))t.el.click();t.el.scrollIntoView({behavior:'smooth',block:'start'});t.el.classList.remove('se-flash');void t.el.offsetWidth;t.el.classList.add('se-flash')}return}
    const a=e.target.closest('.studio-empty [data-act]');if(!a)return;const act=a.dataset.act;if(act==='search')$('#openKnowledgeSearch')?.click();else if(act==='import')$('#openKnowledgeImport')?.click();else if(act==='sources')$('#openArchiveSources')?.click();else if(act==='access')window.switchPage?.('access')});
  run();for(const id of ['files','archives']){const r=$('#'+id);if(r)new MutationObserver(()=>requestAnimationFrame(run)).observe(r,{childList:true,subtree:true})}}
function aboutMark(){const c=$('#protection .software-update-card');if(!c||$('.about-mark',c))return;const m=document.createElement('div');m.className='about-mark';m.innerHTML=jieduMark()+'<div><b>界渡</b><small>你的资料，你的 AI，一处管好</small></div>';c.prepend(m)}
function wsPath(){const e=$$('#workspace *').find(x=>!x.children.length&&/^[A-Za-z]:\\/.test(x.textContent.trim()));return e?e.textContent.trim():''}
function settingsExtras(){const hub=$('#protection');if(!hub||!$('#protection .protection-hub'))return;aboutMark();
  if(!$('#studioLook',hub)){const c=document.createElement('section');c.id='studioLook';c.className='studio-set-card';
    c.innerHTML=`<div class="ssc-head"><h2>外观</h2><small>浅色、深色，或按时间自动切换</small></div><div class="look-grid" role="radiogroup" aria-label="外观">`+[['light','晨雾','明亮通透'],['dark','曜石','沉静专注'],['schedule','定时','白天浅色，夜里深色']].map(([m,t,d])=>`<button type="button" role="radio" class="look-tile" data-mode="${m}"><span class="look-pv look-${m}"><i></i><i></i><i></i></span><b>${t}</b><small>${d}</small></button>`).join('')+`</div>`;
    c.addEventListener('click',e=>{const b=e.target.closest('.look-tile');if(b){window.jieduStudio?.setMode(b.dataset.mode);syncLook()}});hub.append(c)}
  if(!$('#studioHomeAi',hub)&&window.jieduAiState){const c=document.createElement('section');c.id='studioHomeAi';c.className='studio-set-card';
    c.innerHTML=`<div class="ssc-head"><h2>首页外部智能域</h2><small>首页圆盘右侧展示哪些智能体</small></div>
      <div class="ai-sub"><b>展示哪些</b><button type="button" class="ai-pick-reset" id="aiPickReset">恢复默认自动排序</button></div>
      <div class="ai-pick" id="aiPick" role="group" aria-label="指定首页展示的智能体"></div>
      <p class="ai-count-note" id="aiPickNote" aria-live="polite"></p>`;
    c.addEventListener('click',e=>{const S=window.jieduAiState;
      const chip=e.target.closest('[data-ai-pick]');if(chip){const k=chip.dataset.aiPick,{pick}=S.homePrefs();
        let cur=pick||S.homeSelection().shown.map(a=>a.key);
        if(cur.includes(k)){if(cur.length<=1){chip.classList.remove('nope');void chip.offsetWidth;chip.classList.add('nope');return syncAiShow('至少保留 1 个；想交给系统自动挑，点「恢复默认自动排序」')}cur=cur.filter(x=>x!==k)}
        else cur=[...cur,k];
        aiWarn.t=0;S.setHomePrefs({pick:S.order.filter(x=>cur.includes(x))});return syncAiShow()}
      if(e.target.closest('#aiPickReset')){aiWarn.t=0;S.setHomePrefs({pick:null});return syncAiShow()}});
    window.jieduAiState.refresh();hub.append(c)}
  if(!$('#studioData',hub)){const c=document.createElement('section');c.id='studioData';c.className='studio-set-card';
    c.innerHTML=`<div class="ssc-head"><h2>数据位置</h2><small>所有资料只保存在这台电脑上</small></div><div class="data-row"><span class="data-ico"></span><code class="selectable" id="studioDataPath">读取中</code><button type="button" class="secondary" data-go="workspace">打开工作区</button></div>`;
    c.addEventListener('click',e=>{const b=e.target.closest('[data-go]');if(b)window.switchPage?.(b.dataset.go)});hub.append(c)}
  settingsCols();const pe=$('#studioDataPath');if(pe&&!pe.dataset.req){pe.dataset.req='1';fetch('/api/knowledge-base').then(r=>r.json()).then(d=>{pe.textContent=d.vault_path||d.selected_workspace_path||wsPath()||'尚未连接知识库';pe.title=pe.textContent}).catch(()=>{pe.textContent=wsPath()||'暂时读不到';delete pe.dataset.req})}syncLook();syncAiShow()}
function settingsCols(){const pg=$('#protection');if(!pg)return;let L=$('#protection>.set-col.l');
  if(!L){L=document.createElement('div');L.className='set-col l';pg.append(L)}
  /* r39：左栏 本机设置 / 外观 / 数据位置，右栏 关于与更新 / 首页外部智能域，两栏高度接近 */
  /* r41：单列——本机设置 / 外观 / 首页外部智能域 / 数据位置 / 关于与更新 */
  const want=[[L,['.protection-hub','#studioLook','#studioHomeAi','#studioData','.software-update-card']]];
  for(const [col,sels] of want)sels.forEach((sel,i)=>{const e=$('#protection '+sel);if(e&&(e.parentElement!==col||col.children[i]!==e))col.insertBefore(e,col.children[i]||null)})}
let aiWarn={t:0,msg:''};
function syncAiShow(w){if(w)aiWarn={t:Date.now(),msg:w};const warn=Date.now()-aiWarn.t<2600?aiWarn.msg:'';if(w)setTimeout(()=>syncAiShow(),2700);const S=window.jieduAiState,c=$('#studioHomeAi');if(!S||!c)return;const {pick}=S.homePrefs(),sel=S.homeSelection(),list=S.list;
  const box=$('#aiPick',c),on=new Set(sel.shown.map(a=>a.key));
  const html=list.map(a=>`<button type="button" class="ai-chip" data-ai-pick="${a.key}" data-state="${a.state}" aria-pressed="${on.has(a.key)}" title="${a.label} · ${a.statusLabel}"><i></i>${a.label}</button>`).join('');
  if(box.dataset.html!==html){box.innerHTML=html;box.dataset.html=html}
  box.classList.toggle('auto',!pick);$('#aiPickReset',c).hidden=!pick;
  const restN=sel.rest.length,tail=restN?`，其余 ${restN} 个合并为「其余 ${restN} 个」`:'';
  const note=$('#aiPickNote',c),text=warn||(pick?`首页显示你选的 ${sel.shown.length} 个${tail}。`:`自动优先显示已验证、需处理和待验证的连接，共 ${sel.shown.length} 个${tail}。点标签可指定首页显示的连接。`);
  if(note.textContent!==text)note.textContent=text;
  note.classList.toggle('warn',!!warn);}
function syncLook(){let m='light';try{m=localStorage.getItem('jiedu.themeMode')||'light'}catch(e){}$$('#studioLook .look-tile').forEach(b=>b.setAttribute('aria-checked',String(b.dataset.mode===m)))}
function setupAbout(){aboutMark();settingsExtras();syncAiShow();const c=$('#protection .software-update-card');if(c)new MutationObserver(()=>{if(!$('.about-mark',c))aboutMark()}).observe(c,{childList:true});const pg=$('#protection');let wasOn=pg?.classList.contains('active');
  /* r46：去掉 1.2 秒轮询——切到设置页时补齐一次，主题切换时同步外观选中态；AI 状态监听只在这里挂一次 */
  if(pg)new MutationObserver(()=>{const on=pg.classList.contains('active');if(on&&!wasOn)settingsExtras();wasOn=on;if($('#protection>.software-update-card,#protection>.protection-hub,#protection>#studioLook,#protection>#studioData')||on)settingsCols()}).observe(pg,{attributes:true,attributeFilter:['class'],childList:true});
  new MutationObserver(syncLook).observe(document.documentElement,{attributes:true,attributeFilter:['data-theme']});
  document.addEventListener('click',e=>{if(e.target.closest('[data-mode]'))setTimeout(syncLook,0)});
  addEventListener('jiedu:ai-state',()=>syncAiShow())}
/* r24：「预览完整指令」按钮位置固定——指令框移到按钮行下方，平滑展开/收起 */
function promptPanels(){$$('#memory .wiki-guide-prompt-option').forEach(sec=>{const d=$('details.wiki-guide-prompt-preview',sec);if(!d)return;let panel=$('.prompt-panel',sec);
  if(!panel){panel=document.createElement('div');panel.className='prompt-panel';panel.innerHTML='<div class="prompt-panel-in"></div>';sec.append(panel);d.addEventListener('toggle',()=>panel.classList.toggle('open',d.open))}
  const pre=$('pre',d);if(pre)$('.prompt-panel-in',panel).append(pre);panel.classList.toggle('open',d.open)})}
function setupPrompts(){promptPanels();const host=$('#memory');if(host)new MutationObserver(()=>{if($('#memory details.wiki-guide-prompt-preview pre'))promptPanels()}).observe(host,{childList:true,subtree:true})}
/* r25：微光骨架屏——"正在读取…"这类占位文字换成条状骨架，数据到了再柔和渐显 */
const SK_RE=/^(正在读取|正在加载|读取中|加载中|正在获取)/;
let skSet=new Set(),skTimer=0;
function skKind(e){const r=e.getBoundingClientRect();const inline=/^(SPAN|B|STRONG|SMALL|EM|I)$/.test(e.tagName)&&r.width<180;return inline?'pill':'block'}
function skScan(){skTimer=0;
  for(const e of [...skSet]){const gone=!e.isConnected||!SK_RE.test((e.textContent||'').trim());if(gone){skSet.delete(e);e.classList.remove('sk','sk-pill','sk-block');const host=e.isConnected?e.parentElement:e.__skHost;if(host&&host.isConnected){host.classList.remove('sk-in');void host.offsetWidth;host.classList.add('sk-in');setTimeout(()=>host.classList.remove('sk-in'),700)}}}
  const scope=$$('main .page.active:not(#overview), dialog[open]');for(const root of scope){const w=document.createTreeWalker(root,NodeFilter.SHOW_TEXT);let n;while((n=w.nextNode())){const t=n.nodeValue.trim();if(!t||t.length>26||!SK_RE.test(t))continue;const e=n.parentElement;if(!e||skSet.has(e)||e.children.length||e.closest('button,select,option,.s-select,textarea,pre,label,.studio-health,.lx-enter'))continue;if(!e.offsetParent)continue;
    e.__skHost=e.parentElement;const k=skKind(e);e.classList.add('sk','sk-'+k);skSet.add(e)}}}
function skQueue(){if(!skTimer)skTimer=setTimeout(skScan,60)}
function setupSkeleton(){const m=$('main');if(m)new MutationObserver(skQueue).observe(m,{childList:true,subtree:true,characterData:true});document.addEventListener('click',e=>{if(e.target.closest('[data-page],[data-go],.se-folder'))setTimeout(skQueue,40)},true);new MutationObserver(skQueue).observe(document.body,{childList:true});skScan();
  /* r46：去掉 1.5 秒整页重扫——切页（.page 的 class 变化）或对话框打开时补扫一次 */
  const pm=new MutationObserver(skQueue);$$('main .page').forEach(p=>pm.observe(p,{attributes:true,attributeFilter:['class']}));
  new MutationObserver(skQueue).observe(document.body,{attributes:true,attributeFilter:['open'],subtree:true})}
/* r46：右上角运行状态点改由 app.js 订阅 client-state.js 的 jiedu:health 直接设置（规则与故障排查页同一份） */
function start(){setupKnowledge();setupButler();setupAccess();setupMarks();setupSettings();setupWorkspace();setupSelects();setupTopbar();setupTidy();setupEmpty();setupAbout();setupPrompts();setupSkeleton()}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',start);else start();
window.jieduPages={renderMarkdown};
})();
