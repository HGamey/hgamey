/* Device resource deployment uses durable Jobs; ordinary rendering performs no device IO. */
(() => {
  'use strict';
  const API = '/dash/api', STORAGE = 'hgamey.game-deployment.pending';
  const PHASE = {checked:'预检通过',preflight_failed:'预检未通过',applying:'资源部署中',committed:'新目录资源已提交',recovery_required:'需要恢复',restored:'事务已收尾',failed:'部署失败'};
  const FACT = {verified:'已验证',matched:'已匹配',unknown:'尚未核验',not_checked:'尚未核验',not_verified:'尚未验证',not_applicable:'不适用',failed:'验证失败',mismatch:'不匹配',absent:'未找到',pending:'待核验',unchanged:'未修改'};
  const text = v => v == null || v === '' ? '未提供' : typeof v === 'object' ? JSON.stringify(v) : String(v);
  const time = v => { const date = typeof v === 'number' ? new Date(v * 1000) : typeof v === 'string' ? new Date(v) : null; return date && Number.isFinite(date.getTime()) ? date.toLocaleString() : '未记录'; };
  const keyOf = v => JSON.stringify([v.node_id,v.game_id,v.version]);
  const serialOK = v => typeof v === 'string' && v.length > 0 && v.length <= 256 && !/[\x00-\x1f\x7f]/.test(v);
  const modeName = v => ({check:'只读预检',apply:'部署到新目录',recover:'接续事务恢复'})[v] || text(v);
  function el(tag,value,className) { const n=document.createElement(tag); if(value!=null)n.textContent=String(value); if(className)n.className=className; return n; }
  function link(label,path) { if(!path)return null; const a=el('a',label); try { const u=new URL(path,location.origin); if(u.origin!==location.origin||u.username||u.password)return null; a.href=u.pathname+u.search+u.hash; return a; } catch(_){return null;} }
  const jobURL = job => `/dash/jobs#job=${encodeURIComponent(job.id)}`;
  const jobLabel = job => job.phase==='storage_blocked'?'结果保存失败':job.phase==='finalizing'?'正在保存结果':job.queue?.state==='blocked'?'保留资源 · 待核查':job.phase==='interrupted'?'执行中断':job.state==='finished'?({failed:'任务失败',cancelled:'任务已取消'})[job.outcome]||'命令已结束':({queued:'已排队',preparing:'正在准备',starting:'正在启动',running:'执行中',cancelling:'请求停止中'})[job.phase]||'执行状态待核对';

  window.createGameDeployments = function({context,refresh,auth}) {
    const views=new Map(), saved=new Map(), details=new Map(), reading=new Set(), expanded=new Set();
    let currentView=null, fleet=null, fleetBusy=false, fleetError='', fleetController=null, stopped=false;
    try { for(const p of JSON.parse(sessionStorage.getItem(STORAGE)||'[]')) if(p && ['deploy','recover'].includes(p.kind) && typeof p.request_id==='string' && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(p.request_id) && serialOK(p.serial) && p.node_id && p.game_id && p.version && ['check','apply','recover'].includes(p.mode) && (p.kind!=='recover'||/^[0-9a-f]{12}$/.test(p.source_job_id))) saved.set(keyOf(p),{...p,queried:false}); } catch(_){/* Invalid local data cannot establish node state. */}
    const panel=document.getElementById('game-deployment');
    panel.append(el('h3','部署本节点资源'),el('p','首批支持已安装且精确匹配的 Android 引擎，只向应用 files 下新建的独占目录写入所选资源；不会安装、升级、覆盖存档或启动游戏。','note'));
    const target=el('div',null,'deploy-target'), serialLabel=el('label','目标设备序列号'), serial=el('input'); serial.id='deploy-serial';serial.type='text';serial.autocomplete='off';serial.spellcheck=false;serial.maxLength=256;serial.placeholder='从列表选择，或手动填写 serial';serialLabel.append(serial);
    const listLabel=el('label','最近采样的设备'), device=el('select');device.id='deploy-device';listLabel.append(device);
    const fleetButton=el('button','刷新设备列表','ghost');fleetButton.type='button';fleetButton.id='deploy-refresh-devices';target.append(serialLabel,listLabel,fleetButton);
    const fleetNote=el('p',null,'note'), recipe=el('p',null,'note'), status=el('div',null,'deploy-status'), actions=el('div',null,'submission-actions');
    const check=el('button','检查部署条件','ghost'), apply=el('button','部署到新资源目录','ghost primary');check.type=apply.type='button';check.id='deploy-check';apply.id='deploy-apply';actions.append(check,apply);
    const help=el('p',null,'note'), message=el('p',null,'note');message.setAttribute('role','status');
    const requestPanel=el('div',null,'deploy-request'), requestText=el('p',null,'request-id'), requestActions=el('div',null,'submission-actions'), query=el('button','查询原请求','ghost'), retry=el('button','使用原请求 ID 重试','ghost');query.type=retry.type='button';query.id='deploy-query';retry.id='deploy-retry';requestActions.append(query,retry);requestPanel.append(requestText,requestActions);
    const receiptLinks=el('div',null,'preparation-links'), historyHeading=el('h4','此目标的部署记录'), history=el('ul',null,'preparation-list'), empty=el('p',null,'note');
    panel.append(target,fleetNote,recipe,status,actions,help,message,requestPanel,receiptLinks,historyHeading,history,empty);

    function persist() { try { sessionStorage.setItem(STORAGE,JSON.stringify([...saved.values()].map(({kind,node_id,game_id,version,serial,mode,request_id,source_job_id,conflict})=>({kind,node_id,game_id,version,serial,mode,request_id,source_job_id,conflict:!!conflict})))); } catch(_){/* Visible request identity remains available for manual lookup. */} }
    function pending(v) { return saved.get(v.key); }
    function support(v) { return context().node?.capabilities?.includes('games.deploy-v1') && v.record.deployment_support?.available===true; }
    function current(v) { return !stopped && !context().disposed && currentView===v && context().selected===`${v.record.game_id}/${v.record.version}`; }
    function canWrite(v) { return current(v) && context().canWrite && context().detailFresh && context().node?.node_id===v.record.node_id && support(v); }
    function setMessage(v,value,error=false) { v.message=value;v.error=error; }
    function recordMatches(item,v,targetSerial=v.serial) { return item?.target?.node_id===v.record.node_id && item.target.serial===targetSerial; }
    function reportMatches(report,v,targetSerial=v.serial) { return report?.schema==='phonefarm/game-deployment@1' && report.node_id===v.record.node_id && report.game_id===v.record.game_id && report.version===v.record.version && report.serial===targetSerial; }
    function entries(v) { return [...(v.record.deployments||[])].filter(item=>recordMatches(item,v)).sort((a,b)=>(b.queue?.submitted_at||b.started||0)-(a.queue?.submitted_at||a.started||0)); }
    function factRows(report) {
      const list=el('dl',null,'deploy-facts');
      for(const [key,label] of [['installed','已安装引擎'],['resources_deployed','设备资源'],['login','登录'],['scene_ready','场景']]) { const fact=report?.[key];list.append(el('dt',label),el('dd',FACT[fact?.status]||text(fact?.status==='unknown'?'尚未核验':fact?.status))); }
      return list;
    }
    function evidence(report) {
      const box=el('details',null,'asset-details');box.append(el('summary','检查依据、访问范围与局限'));
      const facts=el('dl',null,'facts');
      for(const [key,label] of [['installed','引擎依据'],['resources_deployed','资源依据'],['application_access','应用 UID 读取'],['login','登录依据'],['scene_ready','场景依据'],['recovery','恢复范围']]) facts.append(el('dt',label),el('dd',text(report[key])));
      box.append(facts,el('p','目录与文件校验、UID 读取、应用实际读取和进入场景是不同事实；以上为该次任务的历史观测。','note'));
      if(report.checks)box.append(el('pre',JSON.stringify(report.checks,null,2),'command-box'));
      return box;
    }
    function renderHistory(v,items) {
      history.replaceChildren();empty.textContent=!v.serial?'选择目标后查看对应记录。':!items.length?'此版本尚无该目标的部署记录。':'';
      for(const item of items) {
        const row=el('li',null,'preparation'), heading=el('div',null,'preparation-heading'), badge=el('span',jobLabel(item),'task-state'+(item.queue?.state==='blocked'?' unknown':''));heading.append(el('strong',`${modeName(item.mode)} · ${item.id}`),badge);row.append(heading);
        row.append(el('p',`提交：${time(item.queue?.submitted_at||item.started)} · 请求：${text(item.request_id)}`,'game-item-meta'));
        const known=details.get(item.id), value=known||item, report=value.report;
        if(report && reportMatches(report,v)) { row.append(el('p',`报告：${PHASE[report.phase]||text(report.phase)} · 历史观测 ${time(report.observed_at)}`,'note')); if(report.error)row.append(el('p',text(report.error),'asset-error')); }
        else row.append(el('p',report?'报告身份与目标不符，未作为设备事实展示。':'尚无可读部署报告；任务被接受不代表资源部署完成。','note'));
        if(value.report_error)row.append(el('p',`报告读取：${text(value.report_error)}`,'asset-error'));
        const links=el('div',null,'preparation-links');links.append(link('任务、日志与队列恢复',item.job_url)||link('任务、日志与队列恢复',jobURL(item)));const original=link('部署报告原件',value.report_url||item.report_url);if(original)links.append(original);row.append(links);
        const box=el('details',null,'deploy-history-detail');box.open=expanded.has(item.id);box.addEventListener('toggle',()=>box.open?expanded.add(item.id):expanded.delete(item.id));box.append(el('summary','读取回执与恢复资格'));
        const read=el('button',reading.has(item.id)?'正在读取…':'读取最新回执','ghost');read.type='button';read.disabled=reading.has(item.id);read.setAttribute('aria-busy',String(reading.has(item.id)));read.addEventListener('click',()=>readDeployment(v,item));box.append(read);
        if(report && reportMatches(report,v))box.append(factRows(report),evidence(report));
        const recovery=known?.recovery, recover=el('button','提交恢复任务','ghost');recover.type='button';recover.disabled=!recovery?.supported||!recovery.eligible||!/^[0-9a-f]{12}$/.test(recovery.target_job_id)||!canWrite(v)||v.busy||!!pending(v);recover.addEventListener('click',()=>start(v,'recover',item.id));
        box.append(el('p',known?.read_error||(!known?'先读取资格；恢复会创建独立任务，不在此处释放队列资源。':!recovery?.supported?recovery?.reason||'此记录未提供受控恢复入口，请打开原任务核查。':!recovery.eligible?recovery.reason||'当前不满足恢复条件。':'可提交原上下文的恢复任务；接受请求不代表恢复完成。'),'note'));
        if(recovery?.supported){if(recovery.target_job_id)box.append(el('p',`本次将接续阻断任务：${recovery.target_job_id}`,'note'),link('查看将接续的任务',jobURL({id:recovery.target_job_id})));box.append(recover);}row.append(box);history.append(row);
      }
    }
    function render(v=currentView) {
      if(!v || !current(v))return;
      panel.hidden=false;panel.setAttribute('aria-busy',String(v.busy));
      if(serial.value!==v.serial)serial.value=v.serial;
      device.replaceChildren(el('option','选择已采样设备（也可手填）'));device.children[0].value='';
      for(const d of fleet?.devices||[]) { if(typeof d.serial!=='string')continue; const option=el('option',`${d.model||d.serial} · ${d.serial} · ${d.transport||'传输未知'}${d.online===false?' · 离线快照':''}`);option.value=d.serial;device.append(option); }device.value='';
      fleetButton.disabled=fleetBusy;fleetButton.textContent=fleetBusy?'正在读取设备…':'刷新设备列表';fleetButton.setAttribute('aria-busy',String(fleetBusy));
      fleetNote.textContent=fleetError || (!fleet?'尚未读取设备列表。手填或点击刷新；普通版本刷新不会采样手机。':`设备快照：${time(fleet.sampled_at)} · ${fleet.devices===null?'尚无成功采样':`${fleet.devices?.length||0} 台记录`}${fleet.stale?' · 过期':''}${fleet.error?` · 错误：${fleet.error}`:''}${fleet.deferred?` · 暂缓：${fleet.deferred}`:''}。旧快照不证明当前可用，部署任务会重新核查。`);
      const plan=v.record.manifest?.deployment;recipe.textContent=plan?`配方目标：${text(plan.package)} · versionCode ${text(plan.version_code)} · ${plan.files?.length||0} 项资源。只检查配方引用的缓存，不因无关资源缺失判断部署失败。`:'此版本尚无部署配方，需要登记含 deployment 的新版本。';
      const p=pending(v),items=entries(v),active=items.find(item=>item.state!=='finished'||item.queue?.state==='blocked');
      const problem=!support(v)?v.record.deployment_support?.error||'此节点未提供资源部署能力。':!plan?'当前清单无部署配方。':!context().detailFresh?'当前版本尚未成功刷新，保留历史记录。':!context().canWrite?'请先解锁操作权限。':!serialOK(v.serial)?'选择目标设备，或填写有效的序列号。':p?'有未确认请求；先按原 ID 查询，不创建另一请求。':active?'该目标已有未结束或保留资源的任务，请先打开原任务核查。':!globalThis.crypto?.randomUUID?'请通过 HTTPS 或本机地址提交。':'';
      check.disabled=apply.disabled=!!problem||v.busy||!canWrite(v);check.setAttribute('aria-busy',String(v.busy&&v.operation==='check'));apply.setAttribute('aria-busy',String(v.busy&&v.operation==='apply'));
      help.textContent=problem||'预检与部署都经设备队列执行。预检只读；部署会再次核对引擎与缓存，并创建新资源目录。';
      status.replaceChildren();const latest=items.map(item=>details.get(item.id)||item).find(item=>reportMatches(item.report,v));
      if(!v.serial)status.append(el('p','选择目标后显示该设备的安装、资源、登录与场景依据。','note'));
      else if(!latest)status.append(el('p','此目标尚无匹配的部署观测；可先提交只读预检。','note'));
      else { status.append(el('p',`最近有据记录 · ${modeName(latest.mode)} · ${time(latest.report.observed_at)} · ${latest.job?.id||latest.id}`,'read-time'),factRows(latest.report),evidence(latest.report)); }
      message.textContent=v.message||'';message.classList.toggle('task-error',!!v.error);
      requestPanel.hidden=!p;requestText.textContent=p?`未确认 ${modeName(p.mode)} · 节点 ${p.node_id} · ${p.game_id}/${p.version} · 设备 ${p.serial}\n请求 ID：${p.request_id}${p.source_job_id?` · 原任务 ${p.source_job_id}`:''}`:'';
      query.disabled=v.busy;retry.disabled=v.busy||!canWrite(v)||!p?.queried||!!p?.conflict;retry.hidden=!p||!!p.conflict;
      receiptLinks.replaceChildren();if(v.job?.id)receiptLinks.append(link(`查看原任务 ${v.job.id}`,jobURL(v.job)));
      renderHistory(v,items);
    }
    async function http(path,{body,requestID,signal}={}) {
      const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),15000);const stop=()=>controller.abort();signal?.addEventListener('abort',stop,{once:true});
      try { const response=await fetch(path,{credentials:'same-origin',cache:'no-store',signal:controller.signal,...(body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-HG':'1','X-Request-ID':requestID},body:JSON.stringify(body)})});
        if(response.redirected && new URL(response.url).pathname==='/dash/login'){const e=new Error('登录已失效。');e.status=401;throw e;}
        const value=await response.json();if(!response.ok){const e=new Error(typeof value.error==='string'?value.error:value.error?.message||`HTTP ${response.status}`);e.status=response.status;throw e;}return value;
      } finally {clearTimeout(timeout);signal?.removeEventListener('abort',stop);}
    }
    function verifyDeployment(value,v,p) {
      if(value?.schema_version!==1||!value.job?.id||value.target?.node_id!==p.node_id||value.target.serial!==p.serial|| (p.kind==='deploy'&&value.mode!==p.mode) || (p.kind==='recover'&&value.job.recovery_for!==p.source_job_id))throw new Error('部署回执未匹配原目标或请求类型；保留原 ID。');
      if(value.report && !reportMatches(value.report,v,p.serial))throw new Error('部署报告与版本/设备绑定不匹配。');return value;
    }
    async function readDeployment(v,item) {
      if(reading.has(item.id))return;reading.add(item.id);expanded.add(item.id);render(v);
      try { const value=await http(`${API}/jobs/${encodeURIComponent(item.id)}/deployment`);if(value?.job?.id!==item.id||!recordMatches(value,v,item.target.serial))throw new Error('回执目标不匹配。');details.set(item.id,value); }
      catch(error){details.set(item.id,{...item,recovery:null,read_error:`读取失败：${error.message}；未确认恢复资格。`});if([401,403].includes(error.status))await auth();}
      finally {reading.delete(item.id);render(v);}
    }
    function invalidateRecovery(v,p) {
      if(p.kind!=='recover')return;
      for(const [id,value] of details) if(recordMatches(value,v,p.serial)&&value.recovery) details.set(id,{...value,recovery:{...value.recovery,eligible:false,reason:'恢复任务已记录，原资格已失效；重新读取回执后再决定后续操作。'}});
    }
    async function submit(v,p) {
      if(v.busy||!canWrite(v))return;v.busy=true;v.operation=p.mode;setMessage(v,'正在提交；节点可能已记录，不会自动重试。');render(v);
      try {
        const path=p.kind==='recover'?`${API}/jobs/${encodeURIComponent(p.source_job_id)}/deployment/recover`:`${API}/games/${encodeURIComponent(p.game_id)}/versions/${encodeURIComponent(p.version)}/deploy`;
        const body=p.kind==='recover'?{}:{node_id:p.node_id,serial:p.serial,mode:p.mode};const job=await http(path,{body,requestID:p.request_id});
        if(!job?.id||job.request_id!==p.request_id||(p.kind==='recover'&&job.recovery_for!==p.source_job_id))throw new Error('回包未确认原请求身份。');
        saved.delete(v.key);persist();invalidateRecovery(v,p);v.job=job;setMessage(v,`${modeName(p.mode)}任务已记录：${job.id} · 设备 ${p.serial} · ${jobLabel(job)}。接受任务不代表设备资源或恢复已完成。`);
        if(current(v))await refresh(p);
      } catch(error) {
        if(error.status>=400&&error.status<500&&error.status!==409){saved.delete(v.key);persist();}
        setMessage(v,`${error.message}。${pending(v)?'保留原目标和请求 ID；先查询，再决定是否用原 ID 重试。':'节点明确拒绝此请求；修正条件后可新建尝试。'}`,true);if([401,403].includes(error.status))await auth();
      } finally {v.busy=false;render(v);}
    }
    async function start(v,mode,source) {
      if(!canWrite(v)||v.busy||pending(v)||!serialOK(v.serial)||!globalThis.crypto?.randomUUID)return;
      if(mode==='recover') {const value=details.get(source);if(!value?.recovery?.eligible||!value.recovery.supported||!recordMatches(value,v)||!/^[0-9a-f]{12}$/.test(value.recovery.target_job_id))return;source=value.recovery.target_job_id;}
      const p={kind:mode==='recover'?'recover':'deploy',node_id:v.record.node_id,game_id:v.record.game_id,version:v.record.version,serial:v.serial,mode,request_id:crypto.randomUUID(),...(source?{source_job_id:source}:{}),queried:false};saved.set(v.key,p);persist();await submit(v,p);
    }
    async function queryPending(v) {
      const p=pending(v);if(!p||v.busy)return;let found=false;v.busy=true;setMessage(v,'正在查询原请求，没有重新提交。');render(v);
      try {
        const job=await http(`${API}/requests/${encodeURIComponent(p.request_id)}`);if(!job?.id||job.request_id!==p.request_id)throw new Error('原请求回包身份不匹配。');v.job=job;found=true;
        const value=verifyDeployment(await http(`${API}/jobs/${encodeURIComponent(job.id)}/deployment`),v,p);
        if(value.job.id!==job.id)throw new Error('任务回执身份不匹配。');details.set(job.id,value);saved.delete(v.key);persist();invalidateRecovery(v,p);
        setMessage(v,`已找回原任务 ${job.id} · ${jobLabel(job)}，没有重复提交。设备事实以该任务部署报告为准。`);if(current(v))await refresh(p);
      } catch(error) {if(found && error.status===404){p.conflict=true;persist();}setMessage(v,found&&error.status===404?'原请求已对应一个任务，但没有匹配的部署回执。请打开原任务核查，禁止将该 ID 用作另一部署。':error.status===404?'尚未找到原请求。保留原 ID；可稍后查询或明确使用原 ID 重试。':`查询失败：${error.message}。保留原绑定，不自动重发。`,true);if([401,403].includes(error.status))await auth();}
      finally {p.queried=true;v.busy=false;render(v);}
    }
    async function refreshFleet() {
      if(fleetBusy)return;fleetBusy=true;fleetError='';fleetController=new AbortController();render();
      try {const value=await http(`${API}/fleet?wait=5`,{signal:fleetController.signal});if(value.node_id!==context().node?.node_id)throw new Error('设备快照节点身份不匹配。');fleet=value;}
      catch(error){fleetError=`设备列表读取失败：${error.message}。保留旧快照，可手填 serial。`;if([401,403].includes(error.status))await auth();}
      finally {fleetBusy=false;fleetController=null;render();}
    }
    serial.addEventListener('input',()=>{if(currentView){currentView.serial=serial.value.trim();currentView.message='';currentView.error=false;render();}});
    device.addEventListener('change',()=>{if(currentView&&device.value){currentView.serial=device.value;currentView.message='';currentView.error=false;render();}});
    fleetButton.addEventListener('click',refreshFleet);check.addEventListener('click',()=>{if(currentView&&!check.disabled)return start(currentView,'check');});apply.addEventListener('click',()=>{if(currentView&&!apply.disabled)return start(currentView,'apply');});
    query.addEventListener('click',()=>currentView&&queryPending(currentView));retry.addEventListener('click',()=>{if(currentView&&!retry.disabled)return submit(currentView,pending(currentView));});
    function update() {
      stopped=!!context().disposed;const record=context().record;
      if(!record||context().selected!==`${record.game_id}/${record.version}`){currentView=null;panel.hidden=true;return;}
      const key=keyOf(record);if(!views.has(key))views.set(key,{key,record,serial:saved.get(key)?.serial||'',busy:false,message:'',error:false,job:null});
      currentView=views.get(key);currentView.record=record;render();
    }
    return {update,stop(){stopped=true;fleetController?.abort();}};
  };
})();
