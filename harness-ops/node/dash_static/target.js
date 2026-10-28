/* One connection target for dashboard reads, observations and writes. No credentials are stored. */
(() => {
  'use strict';
  const KEY='hgamey.target.pin', ID=/^[0-9a-f]{32}$/, origin=location.origin;
  const rawFetch=window.fetch.bind(window), operator=location.pathname==='/dash/control';
  const discovery=operator?'/dash/api/remote/node':'/dash/api/node';
  let pin=null, identity=null, verified=false, error='', flight=null, attempted=false;
  const explicit=new URL(location.href).searchParams.getAll('expected_node_id');
  try { const old=JSON.parse(sessionStorage.getItem(KEY)||'null');if(old?.entry===origin&&ID.test(old.node_id))pin=old; } catch (_) { /* A damaged locator never establishes identity. */ }
  if(explicit.length){if(explicit.length!==1||!ID.test(explicit[0]))error='入口中的目标 ID 无效。';else pin={entry:origin,node_id:explicit[0]};}
  function persist(){try{if(pin)sessionStorage.setItem(KEY,JSON.stringify(pin));}catch(_){/* Page links still retain the pin. */}}
  if(explicit.length===1&&!error)persist();
  const binding=()=>pin?{...pin}:null;
  const auth=p=>/^\/dash\/(?:login|logout)$/.test(p)||/^\/dash\/api\/(?:access|steer\/(?:unlock|lock)|remote\/(?:login|logout|whoami))$/.test(p);
  const observing=u=>/^\/dash\/api\/(?:home|state|fleet|shot|live\/(?:stream|frame))$/.test(u.pathname)||u.searchParams.has('devices')||/\/remote\/devices\/[^/]+\/screenshot$/.test(u.pathname);
  function problem(target=pin){
    if(!target||!ID.test(target.node_id)||target.entry!==origin)return target?'原请求属于另一入口；请回到原入口核对，不能转投当前节点。':'此旧请求未记录目标，不能自动查询或重投；请保留原 ID 并核对原节点。';
    if(!pin||target.node_id!==pin.node_id)return `原请求属于节点 ${target.node_id}；当前期望为 ${pin?.node_id||'未知'}，不会换目标接续。`;
    if(!verified)return error||'尚未核对当前节点身份。';
    return '';
  }
  function canWrite(){return verified&&identity?.capabilities?.includes('node.target-v1')===true;}
  function localError(message,code='target_unconfirmed'){const e=new Error(message);e.code=code;e.target=true;return e;}
  function url(value,target=pin){
    const u=new URL(value,location.href);
    if(!['http:','https:'].includes(u.protocol)||auth(u.pathname)||!u.pathname.startsWith('/dash'))return value;
    if(u.origin===origin&&observing(u)&&!canWrite())return '#connection-target';
    if(u.searchParams.has('expected_node_id'))return u.origin===origin?u.pathname+u.search+u.hash:u.href;
    if(!target||!ID.test(target.node_id))return value;
    if(typeof target.entry!=='string'||u.origin!==target.entry)return value;
    u.searchParams.set('expected_node_id',target.node_id);return u.origin===origin?u.pathname+u.search+u.hash:u.href;
  }
  function decorate(root=document){
    for(const a of root.querySelectorAll('a[href]')){const before=a.getAttribute('href');if(!before||before.startsWith('#'))continue;const after=url(before);if(after!==before)a.setAttribute('href',after);}
  }
  function render(){
    const box=document.getElementById('connection-target');if(!box)return;
    const status=box.querySelector('[data-target-status]');status.textContent=error||(!verified?'正在核对执行目标…':canWrite()?'已绑定此后台；本站导航与操作均属于下方节点。':'此节点未提供 node.target-v1，仅可查看缓存；写入与设备观察已禁用。');status.classList.toggle('target-error',!!error);
    const values={entry:origin,expected:pin?.node_id||'尚未绑定',actual:identity?.node_id||'未取得',host:identity?.host?.hostname||'未取得',data:identity?.storage?.data_root||'未取得',jobs:identity?.storage?.jobs_root||'未取得'};
    for(const [key,value] of Object.entries(values))box.querySelector(`[data-target-${key}]`).textContent=value;
    box.querySelector('button').disabled=!!flight;
    decorate();document.dispatchEvent(new CustomEvent('hgamey-target-change',{detail:{verified,canWrite:canWrite()}}));
  }
  async function refresh(){
    if(flight)return flight;
    if(explicit.length&&(explicit.length!==1||!ID.test(explicit[0])))throw localError(error,'invalid_target');
    attempted=true;verified=false;error='';
    flight=(async()=>{
      const ctl=new AbortController(),timer=setTimeout(()=>ctl.abort(),10000);
      try{
        const response=await rawFetch(discovery,{credentials:'same-origin',cache:'no-store',redirect:'error',signal:ctl.signal});
        if(!response.ok)throw localError(`无法读取节点身份（HTTP ${response.status}）；请核对登录后刷新目标。`);
        const value=await response.json();if(!ID.test(value?.node_id))throw localError('节点身份协议不可读。');identity=value;
        if(!pin){pin={entry:origin,node_id:value.node_id};persist();}
        if(pin.node_id!==value.node_id)throw localError(`节点身份不匹配：期望 ${pin.node_id}，实际 ${value.node_id}。保留原绑定，操作已停止。`,'target_mismatch');
        verified=true;return value;
      }catch(e){error=e.name==='AbortError'?'节点身份读取超时，原绑定保留。':e.message;throw e;}
      finally{clearTimeout(timer);}
    })();render();
    try{return await flight;}finally{flight=null;render();}
  }
  async function ready(){if(flight)return flight;if(!attempted)return refresh();if(!verified)throw localError(error||'节点身份未确认。');return identity;}
  async function request(value,init={},options={}){
    const u=new URL(value,location.href),method=(init.method||'GET').toUpperCase();
    if(u.origin!==origin)throw localError('本页请求不能自动转投另一入口。');
    if(auth(u.pathname))return rawFetch(value,{...init,redirect:'error'});
    const nodePath=u.pathname==='/dash/api/node'||u.pathname==='/dash/api/remote/node';
    if(nodePath){const data=await refresh();return new Response(JSON.stringify(data),{headers:{'Content-Type':'application/json'}});}
    await ready();
    const target=Object.hasOwn(options,'target')?options.target:pin,issue=problem(target);if(issue)throw localError(issue);
    const cachedFleet=method==='GET'&&u.pathname==='/dash/api/fleet'&&identity?.capabilities?.includes('fleet.cached-v1');
    if(!canWrite()&&cachedFleet){u.searchParams.delete('wait');u.searchParams.set('cached','1');}
    if(!canWrite()&&(method!=='GET'||(observing(u)&&!cachedFleet)))throw localError('此节点未提供 node.target-v1，写入和设备观察已禁用。','target_unsupported');
    const values=u.searchParams.getAll('expected_node_id');if(values.length&&(values.length!==1||values[0]!==target.node_id))throw localError('链接与请求的目标 ID 不一致。','invalid_target');
    if (/^\/dash\/api\/live\/(stream|frame)$/.test(u.pathname))u.searchParams.set('expected_node_id',target.node_id);
    const headers=new Headers(init.headers||{});const old=headers.get('X-Expected-Node-ID');if(old&&old!==target.node_id)throw localError('请求头的目标 ID 不一致。','invalid_target');headers.set('X-Expected-Node-ID',target.node_id);
    const response=await rawFetch(u.href,{...init,headers,redirect:'error'});
    if(response.status===409&&(response.headers.get('Content-Type')||'').includes('application/json')){
      try{const body=await response.clone().json();if((body.code||body.error?.code)==='target_mismatch'){verified=false;error='节点身份已变化，保留原请求与目标，停止后续操作。请刷新目标核对。';render();}}catch(_){/* Original response remains available to the caller. */}
    }
    return response;
  }
  function assert(target){const issue=problem(target);if(issue)throw localError(issue);}
  function mount(){
    const main=document.querySelector('main')||document.body,box=document.createElement('section');box.id='connection-target';box.className='connection-target';box.setAttribute('aria-label','当前连接与执行目标');
    box.innerHTML='<div class="target-heading"><strong>此后台的执行目标</strong><button type="button" class="ghost">刷新目标身份</button></div><p data-target-status role="status"></p><dl class="target-facts"><dt>访问入口</dt><dd data-target-entry></dd><dt>绑定节点</dt><dd data-target-expected></dd><dt>执行宿主</dt><dd data-target-host></dd><dt>数据目录</dt><dd data-target-data></dd></dl><details><summary>实际身份与任务目录</summary><dl class="target-facts"><dt>实际节点</dt><dd data-target-actual></dd><dt>任务目录</dt><dd data-target-jobs></dd></dl></details>';
    box.classList.add('collapsed');
    main.prepend(box);box.querySelector('button').addEventListener('click',()=>refresh().catch(()=>{}));
    box.addEventListener('click',(e)=>{if(e.target.closest('button'))return;box.classList.toggle('collapsed');});
    for(const nav of document.querySelectorAll('nav.nav')){nav.setAttribute('aria-label','此后台导航');nav.title='这些入口属于当前后台；远端目录选择只改变目录视图。';}
    const observer=new MutationObserver(records=>{for(const record of records)for(const n of record.addedNodes)if(n.nodeType===1){if(n.matches('a[href]')){const before=n.getAttribute('href');if(before&&!before.startsWith('#'))n.setAttribute('href',url(before));}decorate(n);}});observer.observe(document.body,{childList:true,subtree:true});
    document.addEventListener('click',event=>{const a=event.target.closest?.('a[href]');if(a&&!a.getAttribute('href').startsWith('#'))a.href=url(a.getAttribute('href'));},true);
    render();ready().catch(()=>{});
  }
  window.HGTarget={ready,refresh,fetch:request,url,binding,problem,assert,canWrite};
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',mount,{once:true});else mount();
})();
