/* 界渡 · 开场「次元光隙」（第 33 轮微调）
   构图沿用第 32 轮：灭点在画面右侧偏上（x 63%、y 43%），两片云壁之间透出光；左下三缕流光能量丝汇入。
   本轮：光隙不再有任何几何外壳和硬竖线，只是中心柔亮、向外高斯弥散的光核 + 细微星芒；
   云海改为离线预渲染的轻盈贴图（远景薄雾 + 主体云纱 + 朝光隙放射的丝缕，深色星云薄纱 / 浅色高空薄云），
   页面直接用 <img> 加载，首帧就位，运行时零计算，只做 GPU transform 漂移 / 推近。
   交接错峰：白光铺满 → 遮罩后切到首页并收起所有重图层 → 静止保持一拍 → 白光柔和淡出，同时才开始首页落定与全量脉冲。
   指针始终为默认箭头。 */
(function(){
  var L=document.getElementById('studioLaunch');if(!L)return;
  var seen=false;try{seen=localStorage.getItem('jiedu.brandSequence.seen')==='1'||sessionStorage.getItem('jiedu.brandSequence.seen')==='1'}catch(e){}
  if(!seen){L.remove();return}
  var root=document.documentElement,reduce=window.matchMedia&&matchMedia('(prefers-reduced-motion: reduce)').matches;
  var dark=root.dataset.theme==='dark';
  function $(s){return L.querySelector(s)}
  var VX=.63,VY=.43;
  L.style.setProperty('--vx',VX*100+'%');L.style.setProperty('--vy',VY*100+'%');
  var cv=$('canvas.lx-space'),ctx=cv.getContext('2d'),rift=$('.lx-rift'),strands=$('.lx-strands'),
      after=$('.lx-after'),flash=$('.lx-flash'),hint=$('.lx-enter'),back=$('.lx-cloud.back'),near=$('.lx-cloud.near');
  [back,near].forEach(function(el){var o=((VX+.08)/1.16*100).toFixed(2)+'% '+((VY+.08)/1.16*100).toFixed(2)+'%';el.style.transformOrigin=o;el.style.objectPosition=o});

  /* ---------- 左下三缕流光能量丝，顺着云间峡谷汇入光隙 ---------- */
  var SP=[[[-.06,1.06],[.22,.86],[.42,.6],[VX,VY+.02]],[[-.08,.88],[.2,.76],[.44,.56],[VX,VY+.01]],[[.06,1.1],[.3,.92],[.5,.66],[VX,VY+.03]]];
  (function(){var d='';SP.forEach(function(p,i){d+='<path class="s'+i+'" d="M'+p[0][0]*1000+' '+p[0][1]*1000+'C'+p[1][0]*1000+' '+p[1][1]*1000+' '+p[2][0]*1000+' '+p[2][1]*1000+' '+p[3][0]*1000+' '+p[3][1]*1000+'"/>'});
    strands.innerHTML='<svg viewBox="0 0 1000 1000" preserveAspectRatio="none" aria-hidden="true"><defs><linearGradient id="lxSt" x1="0" y1="1" x2="1" y2="0"><stop offset="0" stop-color="var(--st0)" stop-opacity="0"/><stop offset=".55" stop-color="var(--st0)" stop-opacity=".6"/><stop offset="1" stop-color="var(--st1)"/></linearGradient></defs>'+
      '<g class="glow">'+d+'</g><g class="soft">'+d+'</g><g class="line">'+d+'</g></svg>'})();

  /* ---------- 云海：离线预渲染贴图，直接加载（首帧就位，运行时不做任何生成）---------- */
  [['back',back],['near',near]].forEach(function(p){if(!p[1])return;try{p[1].decoding='sync'}catch(e){}p[1].src='/static/launch-'+(dark?'dark':'light')+'-'+p[0]+'.webp?v=c34'});

  /* ---------- Canvas：3D 纵深的发光粒子（以偏心灭点为原点透视投影）----------
     三类粒子：远近错落的呼吸光点（星尘）、带景深虚化的漂浮微尘、沿三缕能量流汇入光隙的明亮光珠。
     深色用叠加发光（lighter）；浅色单独一套高对比方案：暖金 / 琥珀 / 虹彩光粒，白热核心 + 饱和色光环，亮背景下依然清晰。
     跃迁：每颗粒子拉出「由细渐粗的柔和光尾 + 头部高亮发光核」，尾长随速度连续增长，平滑过渡，没有突然切换。 */
  var PAL=dark?{
    c:['255,255,255','206,222,255','186,170,255','255,224,170','150,236,255'],w:[.36,.26,.18,.12,.08],
    tail:['232,238,255','206,222,255','196,182,255','255,226,180','170,236,255'],blend:'lighter',orb:['255,240,214','214,226,255'],dust:'200,196,255',alphaK:1
  }:{
    c:['255,168,52','246,192,76','236,132,92','150,118,240','70,166,226'],w:[.34,.3,.14,.12,.1],
    tail:['255,176,64','250,198,90','240,150,110','160,132,246','90,176,232'],blend:'source-over',orb:['255,170,48','255,206,96'],dust:'240,168,80',alphaK:1
  };
  // 发光粒子贴图：白热核心 → 饱和色 → 柔光晕；浅色的光晕更饱和，保证在亮背景上可见
  function glowSprite(rgb,sz,lightBg,iris){var s=document.createElement('canvas');s.width=s.height=sz;var g=s.getContext('2d'),h=sz/2,gr=g.createRadialGradient(h,h,0,h,h,h);
    gr.addColorStop(0,lightBg?'rgba(255,252,240,1)':'rgba(255,255,255,1)');gr.addColorStop(.12,'rgba('+rgb+',1)');gr.addColorStop(.32,'rgba('+rgb+','+(lightBg?.62:.5)+')');
    if(iris){gr.addColorStop(.5,'rgba(236,140,220,.22)');gr.addColorStop(.66,'rgba(120,196,255,.16)')}
    gr.addColorStop(1,'rgba('+rgb+',0)');g.fillStyle=gr;g.fillRect(0,0,sz,sz);return s}
  var sprites=PAL.c.map(function(c){return glowSprite(c,48,!dark,false)}),irisSpr=glowSprite(PAL.c[0],64,!dark,true),
      orbSpr=PAL.orb.map(function(c){return glowSprite(c,64,!dark,!dark)}),dustSpr=glowSprite(PAL.dust,64,!dark,false);
  var W=0,H=0,vx=0,vy=0,F=1,Fk=1,ZMAX=16,V0=.42;
  function resize(){W=innerWidth;H=innerHeight;vx=W*VX;vy=H*VY;F=H*1.2;Fk=F*ZMAX*.32;cv.width=W;cv.height=H}
  resize();
  function pick(w){var r=Math.random(),a=0;for(var i=0;i<w.length;i++){a+=w[i];if(r<a)return i}return 0}
  var P=[],ORB=[];
  function seed(p,fresh){p.z=fresh?.5+Math.random()*(ZMAX-.5):ZMAX-Math.random()*1.2;var sx=-.12*W+Math.random()*1.24*W,sy=-.12*H+Math.random()*1.24*H;
    p.x=(sx-vx)*p.z/Fk;p.y=(sy-vy)*p.z/Fk;p.ph=Math.random()*6.28;p.br=.6+Math.random()*1.8;
    if(p.k==='dust'){p.s=.03+Math.random()*.05;p.c=0}else{p.s=Math.random()<.07?.018+Math.random()*.014:.005+Math.random()*.011;p.c=pick(PAL.w);p.iris=!dark&&Math.random()<.08}}
  var NS=dark?720:640,ND=dark?120:110;
  for(var i=0;i<NS;i++){var p={k:'star'};seed(p,true);P.push(p)}
  for(i=0;i<ND;i++){p={k:'dust'};seed(p,true);P.push(p)}
  function bez(pp,t){var a=1-t;return [a*a*a*pp[0][0]+3*a*a*t*pp[1][0]+3*a*t*t*pp[2][0]+t*t*t*pp[3][0],a*a*a*pp[0][1]+3*a*a*t*pp[1][1]+3*a*t*t*pp[2][1]+t*t*t*pp[3][1]]}
  for(i=0;i<(dark?66:78);i++)ORB.push({k:i%3,t:Math.random(),v:.045+Math.random()*.07,o:(Math.random()-.5)*.016,c:Math.random()*2|0,s:.7+Math.random()*1.6,ph:Math.random()*6.28});
  var t=0,wt=0,going=false,running=true,frozen=false,raf=0,lastNow=0,TA=1300,VMAX=46;
  // 平滑加速：起步柔和、后段迅猛（ease-in 的五次方混合），避免速度突变
  function speed(){if(!going)return V0;var q=Math.min(1,wt*1000/TA),e=q*q*(.35+.65*q*q*q);return V0+VMAX*e}
  function step(dt){
    t+=dt;if(going)wt+=dt;var v=speed(),d=v*dt,warp=Math.max(0,Math.min(1,(v-1.5)/16)),tl=v*.08;
    ctx.clearRect(0,0,W,H);ctx.globalCompositeOperation=PAL.blend;
    // 沿能量流汇入光隙的光珠（越接近光隙越小越亮，跃迁时淡出）
    var sf=1-Math.min(1,wt*2.4);
    if(sf>0)for(var i=0;i<ORB.length;i++){var o=ORB[i];o.t+=o.v*dt*(1+v*.15);if(o.t>1)o.t-=1;var pt=bez(SP[o.k],o.t),x=pt[0]*W+o.o*W*(1-o.t),y=pt[1]*H+o.o*H*(1-o.t);
      var pulse=.75+.25*Math.sin(t*3+o.ph),r=(1-o.t*.75)*o.s*8*pulse,a=Math.min(1,o.t*6)*Math.min(1,(1-o.t)*5)*sf;
      if(a<.03)continue;ctx.globalAlpha=a*(dark?.95:1);ctx.drawImage(orbSpr[o.c],x-r,y-r,r*2,r*2)}
    // 星尘与漂浮微尘
    var tails=tl>.035?{}:null,heads=[];
    for(i=0;i<P.length;i++){var p=P[i];p.z-=d*(p.k==='dust'?.8:1);if(p.z<.12){seed(p,false);continue}
      if(p.k==='dust'){p.x+=Math.sin(t*.35+p.ph)*.0009*p.z;p.y+=Math.cos(t*.27+p.ph)*.0006*p.z}
      var kk=Fk/p.z,sx=vx+p.x*kk,sy=vy+p.y*kk;
      if(sx<-120||sx>W+120||sy<-120||sy>H+120){seed(p,false);continue}
      var fade=Math.min(1,(ZMAX-p.z)/4)*Math.min(1,(p.z-.12)/.5),breath=.68+.32*Math.sin(t*p.br+p.ph);
      var rp=p.k==='dust'?Math.min(26,p.s*kk*.06):Math.max(.6,Math.min(4.6,p.s*kk*.105));
      if(p.k==='dust'){var near=Math.min(1,1.6/p.z),da=fade*(dark?.22:.3)*(1-near*.55)*(1-warp*.8);if(da>.02){ctx.globalAlpha=da*breath;ctx.drawImage(dustSpr,sx-rp,sy-rp,rp*2,rp*2)}continue}
      var al=fade*(going?Math.max(breath,warp):breath);if(al<.03)continue;
      if(tails&&fade>.25){var k1=Fk/(p.z+tl),tx=vx+p.x*k1,ty=vy+p.y*k1,dx=sx-tx,dy=sy-ty,len=Math.sqrt(dx*dx+dy*dy);
        if(len>1.5){var hw=rp*(.55+warp*.5),nx=-dy/len*hw,ny=dx/len*hw,key=p.c;(tails[key]||(tails[key]=[])).push(tx,ty,sx+nx,sy+ny,sx-nx,sy-ny,sx+dx/len*hw,sy+dy/len*hw)}}
      heads.push(p.iris?-1:p.c,sx,sy,rp,al);
    }
    // 光尾：由细渐粗的楔形（尾端为尖、头端最宽），先画一层宽而淡的柔光，再画实体
    if(tails){for(var key in tails){var arr=tails[key],col=PAL.tail[+key],j;ctx.fillStyle='rgba('+col+',1)';
      for(var pass=0;pass<2;pass++){var wk=pass?1:2.4;ctx.globalAlpha=pass?(dark?.55:.7):(dark?.16:.2);ctx.beginPath();
        for(j=0;j<arr.length;j+=8){var hx=(arr[j+2]+arr[j+4])/2,hy=(arr[j+3]+arr[j+5])/2,ox=(arr[j+2]-hx)*wk,oy=(arr[j+3]-hy)*wk;
          ctx.moveTo(arr[j],arr[j+1]);ctx.lineTo(hx+ox,hy+oy);ctx.quadraticCurveTo(arr[j+6]+(arr[j+6]-hx)*(wk-1),arr[j+7]+(arr[j+7]-hy)*(wk-1),hx-ox,hy-oy);ctx.closePath()}
        ctx.fill()}}}
    // 头部发光核（待机时就是呼吸光点）
    for(i=0;i<heads.length;i+=5){var c=heads[i],hr=heads[i+3]*(2.6-.8*Math.min(1,warp*2.5)+1.2*warp);ctx.globalAlpha=heads[i+4];ctx.drawImage(c<0?irisSpr:sprites[c],heads[i+1]-hr,heads[i+2]-hr,hr*2,hr*2)}
    ctx.globalAlpha=1;ctx.globalCompositeOperation='source-over';
  }
  function frame(now){var dt=lastNow?Math.min(.05,(now-lastNow)/1000):1/60;lastNow=now;if(!frozen)step(dt);if(running)raf=requestAnimationFrame(frame)}
  addEventListener('resize',resize);
  if(reduce){running=false;step(1/60)}else raf=requestAnimationFrame(frame);

  /* ---------- 跃迁 ---------- */
  var anims=[];
  function A(el,kf,opt){if(!el)return null;var a=el.animate(kf,Object.assign({fill:'forwards'},opt));anims.push(a);return a}
  function later(ms,fn){return setTimeout(fn,ms)}
  function boot(){window.dispatchEvent(new CustomEvent('jiedu:boot'))}
  function stopCanvas(){running=false;cancelAnimationFrame(raf)}
  function end(){stopCanvas();removeEventListener('resize',resize);document.removeEventListener('keydown',key,true);L.remove()}
  function ez(q){return Math.pow(q,2.4)}
  var ZD=8;
  // base：图层在点击那一帧的实际变换（云层待机时在 CSS 漂移中），跃迁从这里接着推，不跳回原点
  function persp(pow,f0,f1,base){base=base&&base!=='none'?base+' ':'';var k=[],n=32;for(var j=0;j<=n;j++){var q=j/n,cz=(ZD-.55)*ez(q),s=Math.pow(ZD/(ZD-cz),pow),o=s<f0?1:Math.max(0,1-(s-f0)/(f1-f0));
    k.push({offset:q,transform:base+'scale('+s.toFixed(4)+')',opacity:+o.toFixed(3)})}return k}
  function grab(el){if(!el)return '';var m=getComputedStyle(el).transform;try{el.getAnimations().forEach(function(a){if(a instanceof CSSAnimation)a.pause()})}catch(e){}return m}
  // 首页提前静默就位：开场待机、浏览器空闲时就在不透明遮罩后面切到首页并预备好全量脉冲（只做这两件，不额外提升图层、不暂停动画）
  var prepared=false;
  function prepareHome(){if(prepared)return;prepared=true;
    try{if(typeof window.switchPage==='function')window.switchPage('overview')}catch(e){}
    setTimeout(function(){try{window.dispatchEvent(new CustomEvent('jiedu:boot-prepare'))}catch(e){}},400)}
  (window.requestIdleCallback||function(f){return setTimeout(f,800)})(prepareHome,{timeout:1500});
  var T_FLASH=820,T_WHITE=1560,FADE=700;
  function hideHeavy(){[back,near,rift,strands,cv].forEach(function(el){if(el)el.style.visibility='hidden'})}
  // 白光铺满的那一帧直接开始连续淡出（不再停顿）：开场重图层已被白光盖住，立刻收起；
  // 主界面不做缩放（它内含大量毛玻璃，缩放会让每帧重算背景模糊），落定感改由余晖层轻微外扩来表现
  function swap(freeze){
    stopCanvas();hideHeavy();prepareHome();
    if(freeze)return;
    A(flash,[{opacity:1},{opacity:0}],{duration:460,easing:'cubic-bezier(.25,.1,.25,1)',composite:'replace'});
    A(after,[{transform:'scale(1)'},{transform:'scale(1.08)'}],{duration:FADE,easing:'cubic-bezier(.2,.6,.3,1)'});
    A(L,[{opacity:1},{opacity:.9,offset:.18},{opacity:0}],{duration:FADE,easing:'cubic-bezier(.33,0,.25,1)'});
    later(200,boot);later(FADE+40,end);
  }
  function go(opt){
    if(going)return;going=true;opt=opt||{};
    L.classList.add('lx-go');
    if(reduce){prepareHome();A(L,[{opacity:1},{opacity:0}],{duration:300});later(320,function(){boot();end()});return}
    // 点击反馈：文字与两侧光带亮起、轻微扩散回弹，随即淡出，和跃迁同时开始、无缝衔接
    tapFeedback();
    // 镜头冲向光隙：光核横向化开、流光丝与前后两层云按透视以不同速度推近
    A(rift,[{transform:'scale(1,1)',opacity:1},{transform:'scale(1.5,1.1)',opacity:1,offset:.5},{transform:'scale(6,2.2)',opacity:1}],{duration:TA,easing:'cubic-bezier(.5,0,.8,.5)'});
    var mNear=grab(near),mBack=grab(back);
    A(strands,persp(.8,1.8,3.2),{duration:TA,easing:'linear'});
    A(near,persp(1.1,1.5,3,mNear),{duration:TA,easing:'linear'});
    A(back,persp(.45,7,10,mBack),{duration:TA,easing:'linear'});
    // 中心白光：小尺寸绘制，放大绽放铺满全屏
    var FS=dark?4.2:8.6;  // 深色：只放大到星辉紫银光晕包住画面，不铺成纯白；浅色：通透暖晨光铺满
    A(flash,[{transform:'translate(-50%,-50%) scale(.08)',opacity:0},{transform:'translate(-50%,-50%) scale(.4)',opacity:1,offset:.18},{transform:'translate(-50%,-50%) scale('+(FS*.21).toFixed(2)+')',opacity:1,offset:.62},{transform:'translate(-50%,-50%) scale('+FS+')',opacity:1}],{duration:T_WHITE-T_FLASH,delay:T_FLASH,easing:'cubic-bezier(.45,.05,.55,1)'});
    A(cv,[{opacity:1},{opacity:0}],{duration:240,delay:T_WHITE-260,easing:'ease-in'});
    A(after,[{opacity:0},{opacity:1}],{duration:60,delay:T_WHITE-40});
    if(opt.freeze)return;
    later(T_WHITE,function(){swap(false)});
  }
  function tapFeedback(){
    var b=hint&&hint.querySelector('b'),band=hint&&hint.querySelector('.band'),line=hint&&hint.querySelector('i'),tap=hint&&hint.querySelector('.tap');
    [b,band,line].forEach(function(el){try{el&&el.getAnimations().forEach(function(a){a.pause()})}catch(e){}});
    var UP='cubic-bezier(.2,.8,.3,1)',BACK='cubic-bezier(.4,0,.3,1)';
    // 0 → 190ms 亮起并向外扩散；190 → 400ms 轻微回弹；之后柔和淡出（跃迁同时已经开始）
    A(hint,[{transform:'translateX(-50%) scale(1)',opacity:1,easing:UP},{transform:'translateX(-50%) scale(1.055)',opacity:1,offset:.3,easing:BACK},{transform:'translateX(-50%) scale(1.015)',opacity:.95,offset:.62,easing:'ease-in'},{transform:'translateX(-50%) scale(1.03)',opacity:0}],{duration:640});
    if(b)A(b,[{opacity:getComputedStyle(b).opacity,easing:UP},{opacity:1,offset:.3},{opacity:1}],{duration:640});
    A(band,[{transform:'scaleX(1)',opacity:1,easing:UP},{transform:'scaleX(1.16)',opacity:1,offset:.3,easing:BACK},{transform:'scaleX(1.06)',opacity:.95}],{duration:640});
    A(line,[{transform:'scaleX(1)',opacity:1,easing:UP},{transform:'scaleX(1.22)',opacity:1,offset:.3,easing:BACK},{transform:'scaleX(1.08)',opacity:.95}],{duration:640});
    A(tap,[{transform:'scale(.55)',opacity:0,easing:UP},{transform:'scale(1.1)',opacity:1,offset:.3,easing:'ease-out'},{transform:'scale(1.55)',opacity:0}],{duration:640});
  }
  function key(e){if(e.key==='Enter'||e.key===' '||e.key==='Escape'){e.preventDefault();e.stopPropagation();go()}}
  L.addEventListener('click',function(){go()});document.addEventListener('keydown',key,true);
  try{L.focus({preventScroll:true})}catch(e){}
  window.jieduLaunch={enter:function(){go()},
    _at:function(sec){if(!going)go({freeze:true});frozen=true;anims.forEach(function(a){a._held=true});while(wt<sec-1e-6)step(Math.min(1/60,sec-wt));anims.forEach(function(a){a.pause();a.currentTime=sec*1000})},
    _finish:function(){var c=wt*1000;frozen=false;lastNow=0;anims.forEach(function(a){a._held=false;a.play()});later(Math.max(0,T_WHITE-c),function(){swap(false)})}};
})();
