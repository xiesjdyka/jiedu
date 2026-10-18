/* Shared dialog and small-screen navigation behavior. */
(() => {
  const sidebar=document.querySelector('body>.sidebar');
  const bar=document.createElement('div');bar.className='mobile-workspace-bar';
  bar.innerHTML='<span>界渡</span><button type="button" aria-expanded="false" aria-controls="workspaceNavigation">菜单</button>';
  sidebar.id='workspaceNavigation';sidebar.before(bar);
  const toggle=bar.querySelector('button'),shade=document.createElement('button');shade.type='button';shade.className='workspace-navigation-shade';shade.setAttribute('aria-label','关闭导航');sidebar.before(shade);
  const media=matchMedia('(max-width: 768px)');let navOpen=false;
  function syncDrawer(){sidebar.inert=media.matches&&!navOpen;if(sidebar.inert)sidebar.setAttribute('aria-hidden','true');else sidebar.removeAttribute('aria-hidden')}
  function closeNavigation({returnFocus=true}={}){const wasOpen=navOpen;navOpen=false;document.body.classList.remove('mobile-nav-open');toggle.setAttribute('aria-expanded','false');sidebar.removeAttribute('role');sidebar.removeAttribute('aria-modal');document.querySelector('body>main').inert=false;syncDrawer();if(wasOpen&&returnFocus)toggle.focus({preventScroll:true})}
  function openNavigation(){navOpen=true;syncDrawer();document.body.classList.add('mobile-nav-open');toggle.setAttribute('aria-expanded','true');sidebar.setAttribute('role','dialog');sidebar.setAttribute('aria-modal','true');document.querySelector('body>main').inert=true;(sidebar.querySelector('nav button.active')||sidebar.querySelector('button')).focus()}
  toggle.onclick=()=>navOpen?closeNavigation():openNavigation();shade.onclick=()=>closeNavigation();
  window.closeWorkspaceNavigation=()=>closeNavigation();
  media.addEventListener('change',()=>closeNavigation({returnFocus:false}));
  syncDrawer();
  document.addEventListener('keydown',event=>{if(!navOpen)return;if(event.key==='Escape'){event.preventDefault();closeNavigation()}else if(event.key==='Tab'){const items=[toggle,...sidebar.querySelectorAll('button:not(:disabled),a[href]')],first=items[0],last=items.at(-1);if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus()}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus()}}});
  function normalizeDialog(dialog){
    if(dialog.querySelector(':scope>.dialog-head,:scope>.butler-dialog-head'))return;
    const form=dialog.querySelector(':scope>form[method=dialog]'),heading=dialog.querySelector(':scope>h2');
    if(!form||form.children.length!==1||!heading)return;
    form.classList.add('dialog-head');form.prepend(heading);const close=form.querySelector('button');close.type='submit';close.textContent='×';close.setAttribute('aria-label','关闭'+heading.textContent);
  }
  document.querySelectorAll('dialog').forEach(normalizeDialog);
  new MutationObserver(records=>{for(const record of records)for(const node of record.addedNodes)if(node.nodeType===1){if(node.matches('dialog'))normalizeDialog(node);node.querySelectorAll('dialog').forEach(normalizeDialog)}}).observe(document.body,{childList:true,subtree:true});
})();
