/* 界渡 · 首页「全量启动脉冲」（第 27 轮）
   开场光晕散去、落到首页时由 launch.js 派发 jiedu:boot 触发，只跑一次：
   1. 主盘刻度顺时针依次点亮扫过，所有扇区随之逐个亮起；
   2. 外围每条引导线同时把一道能量脉冲送进中央核心，核心随即泛起一圈光；
   3. 两盘之间的连接线加速流动，右侧知识动态小盘完成一次扫描并落定；
   4. 底部波形从左到右涌过一道完整光波；
   之后所有临时状态撤掉，回到平常的静谧待机。 */
(function(){
  var reduce=window.matchMedia&&matchMedia('(prefers-reduced-motion: reduce)').matches;
  var timers=[];
  function later(ms,fn){timers.push(setTimeout(fn,ms))}
  function clear(){timers.forEach(clearTimeout);timers=[]}

  // 第 36 轮：写样式变量、读布局这些重活拆到 prepare()，在开场待机时提前静默做完；boot() 时只切换类名
  function prepare(){
    var home=document.querySelector('.studio-home');
    if(!home||reduce||home.dataset.bootReady==='1')return;
    var ticks=home.querySelectorAll('.studio-hub .spin.ticks .tick');
    for(var i=0;i<ticks.length;i++)ticks[i].style.setProperty('--bd',(i/ticks.length*.9).toFixed(3)+'s');
    var nodes=[].slice.call(home.querySelectorAll('.studio-hub g.hub-node'));
    nodes.sort(function(a,b){return (+a.dataset.index||0)-(+b.dataset.index||0)});
    nodes.forEach(function(n,k){n.style.setProperty('--bd',(.18+k*.085).toFixed(3)+'s')});
    var subTicks=home.querySelectorAll('.sub-hub .sub-spin .tick');
    for(i=0;i<subTicks.length;i++)subTicks[i].style.setProperty('--bd',(1.15+i/subTicks.length*.75).toFixed(3)+'s');
    var bars=home.querySelectorAll('.sub-hub .sub-bar');
    for(i=0;i<bars.length;i++)bars[i].style.setProperty('--bd',(1.2+i/Math.max(1,bars.length)*.7).toFixed(3)+'s');
    var sub=home.querySelector('.sub-hub');
    if(sub&&getComputedStyle(sub).position==='static'){sub.style.position='relative';sub.dataset.bootPos='1'}
    home.dataset.bootReady='1';
  }

  function boot(){
    var home=document.querySelector('.studio-home');
    if(!home||reduce)return;
    clear();
    var stale=home.querySelectorAll('.boot-scan,.pw-boot');for(var s=0;s<stale.length;s++)stale[s].remove();
    if(home.classList.contains('boot')){home.classList.remove('boot');void home.offsetWidth}
    prepare();
    var nodes=[].slice.call(home.querySelectorAll('.studio-hub g.hub-node'));

    home.classList.add('boot');

    // 2 所有引导线同时向核心送入能量
    later(1000,function(){nodes.forEach(function(n){n.classList.remove('pulse');void n.getBoundingClientRect();n.classList.add('pulse')})});
    later(1700,function(){nodes.forEach(function(n){n.classList.remove('pulse')})});
    later(1560,function(){var c=home.querySelector('.studio-core');if(!c)return;c.classList.remove('ripple');void c.offsetWidth;c.classList.add('ripple');home.classList.add('boot-core');
      later(1000,function(){c.classList.remove('ripple')})});

    // 3 连接线加速 + 小盘扫描
    later(1050,function(){home.classList.add('bridge-on','boot-bridge')});
    later(2350,function(){home.classList.remove('boot-bridge');if(!home.classList.contains('has-focus'))home.classList.remove('bridge-on')});
    var sub=home.querySelector('.sub-hub');
    if(sub){
      later(1150,function(){
        var scan=document.createElement('i');scan.className='boot-scan';scan.setAttribute('aria-hidden','true');sub.appendChild(scan);
        later(1100,function(){scan.remove();if(sub.dataset.bootPos){sub.style.position='';delete sub.dataset.bootPos}})
      })
    }

    // 4 底部波形：沿现有波形线克隆一条光波，从左涌到右
    var line=home.querySelector('.pw-svg .pw-line');
    if(line){
      later(1450,function(){
        line=home.querySelector('.pw-svg .pw-line')||line;if(!line.parentNode)return; /* r39：波形期间可能重绘过，取当前那条 */
        var w=line.cloneNode(false);w.setAttribute('class','pw-boot');w.setAttribute('pathLength','1000');w.removeAttribute('id');
        line.parentNode.appendChild(w);
        var wash=home.querySelector('.pulse-wave');if(wash)wash.classList.add('boot-wash');
        later(1500,function(){w.remove();if(wash)wash.classList.remove('boot-wash')})
      })
    }

    // 收尾：撤掉所有临时状态
    later(3300,function(){
      home.classList.remove('boot','boot-core');
      [].forEach.call(home.querySelectorAll('[style*="--bd"]'),function(el){el.style.removeProperty('--bd')});
      delete home.dataset.bootReady;
    });
  }

  window.addEventListener('jiedu:boot',boot);
  window.addEventListener('jiedu:boot-prepare',prepare);
  window.jieduBoot=boot;window.jieduBootPrepare=prepare;
})();
