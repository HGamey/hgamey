/* Explicit game preparation transactions share durable Jobs; rendering performs no device IO. */
(() => {
  'use strict';
  const API = '/dash/api', STORAGE = 'hgamey.game-deployment.pending';
  const OPERATIONS = {
    deploy:{label:'资源部署',recipe:'deployment',support:'deployment_support',history:'deployments',capability:'games.deploy-v1',schema:'phonefarm/game-deployment@1',check:'检查部署条件',apply:'部署到新资源目录',scope:'已安装且精确匹配的 Android 引擎：只向应用 files 下新建的独占目录写入所选资源，保留旧文件与存档。'},
    install:{label:'单 APK 新装',recipe:'installation',support:'installation_support',history:'installations',capability:'games.install-v1',schema:'phonefarm/game-installation@1',check:'检查新装条件',apply:'提交单 APK 新装',scope:'仅 Android user 0 明确缺席的单 APK 包。成功后保留新安装，不支持升级、替换、卸载或启动；应用资源、登录和场景仍需独立核验。'},
    initialize:{label:'首次运行初始化',recipe:'initialization',support:'initialization_support',history:'initializations',capability:'games.initialize-v1',schema:'phonefarm/game-initialization@1',check:'检查初始化条件',apply:'提交初始化尝试',scope:'目录已就绪时不启动；否则有界启动精确匹配的应用，观察存储目录后停止本次目标。首次运行产生的配置与副作用保留，不自动点击、授予权限或等待下载。'},
    scene:{label:'Doom 场景诊断',recipe:'scene_attempt',support:'scene_support',history:'scenes',capability:'games.scene-v1',schema:'phonefarm/game-scene@1',check:'检查场景条件',apply:'提交有界场景尝试',scope:'显式引用同节点的原资源部署，在新运行目录放入小种子，再进行一次有界 render demo 启动、观察和停止。原资源、运行证据与应用自身配置保留；诊断不证明同状态或性能实验资格。'}
  };
  const operationOf = value => value?.operation === undefined ? 'deploy' : value.operation;
  const operationName = value => OPERATIONS[value]?.label || '未知操作';
  const PHASE = {checked:'预检通过',preflight_failed:'预检未通过',applying:'资源部署中',committed:'新目录资源已提交',recovery_required:'需要恢复',restored:'事务已收尾',failed:'部署失败'};
  const FACT = {verified:'已验证',matched:'已匹配',unknown:'尚未核验',not_checked:'尚未核验',not_verified:'尚未验证',not_applicable:'不适用',failed:'验证失败',mismatch:'不匹配',absent:'未找到',pending:'待核验',unchanged:'未修改',needs_assistance:'未达标 · 需要协助',not_attempted:'未尝试',confirmed:'已确认',not_required:'无停止义务',required:'停止义务未完成',not_prepared:'尚未准备',prepared:'已准备',attempted:'已尝试',observed:'已有诊断观察'};
  const text = v => v == null || v === '' ? '未提供' : typeof v === 'object' ? JSON.stringify(v) : String(v);
  const time = v => { const date = typeof v === 'number' ? new Date(v * 1000) : typeof v === 'string' ? new Date(v) : null; return date && Number.isFinite(date.getTime()) ? date.toLocaleString() : '未记录'; };
  const keyOf = v => JSON.stringify([v.node_id,v.game_id,v.version]);
  const serialOK = v => typeof v === 'string' && v.length > 0 && v.length <= 256 && !/[\x00-\x1f\x7f]/.test(v);
  const modeName = (mode,operation='deploy') => mode==='apply'?({install:'新装',deploy:'部署到新目录',initialize:'初始化尝试',scene:'有界场景尝试'})[operation]||text(mode):({check:'只读预检',recover:'接续事务恢复'})[mode]||text(mode);
  const phaseName = (phase,operation) => ({install:{applying:'新装执行中',committed:'新装已提交 · 保留应用',failed:'新装失败'},initialize:{initialized:'存储目录已就绪',needs_assistance:'目录未达标 · 需要协助',recovery_required:'本次启动/停止义务待结算'},scene:{observed:'本次诊断已观察',needs_assistance:'场景未达标 · 需要协助',recovery_required:'本次场景尝试待恢复'}})[operation]?.[phase]||PHASE[phase]||text(phase);
  const sourceOK = (source,job,path) => source?.job_id===job && /^[0-9a-f]{12}$/.test(job) && source.path===path && Number.isSafeInteger(source.size) && source.size>0 && /^[0-9a-f]{64}$/.test(source.sha256);
  const deploymentOK = value => sourceOK(value?.report,value?.report?.job_id,'deployment.json') && sourceOK(value?.state,value?.report?.job_id,'game-deploy/session.json');
  function el(tag,value,className) { const n=document.createElement(tag); if(value!=null)n.textContent=String(value); if(className)n.className=className; return n; }
  function link(label,path) { if(!path)return null; const a=el('a',label); try { const u=new URL(path,location.origin); if(u.origin!==location.origin||u.username||u.password)return null; a.href=u.pathname+u.search+u.hash; return a; } catch(_){return null;} }
  const jobURL = job => `/dash/jobs#job=${encodeURIComponent(job.id)}`;
  const jobLabel = job => job.phase==='storage_blocked'?'结果保存失败':job.phase==='finalizing'?'正在保存结果':job.queue?.state==='blocked'?'保留资源 · 待核查':job.phase==='interrupted'?'执行中断':job.state==='finished'?({failed:'任务失败',cancelled:'任务已取消'})[job.outcome]||'命令已结束':({queued:'已排队',preparing:'正在准备',starting:'正在启动',running:'执行中',cancelling:'请求停止中'})[job.phase]||'执行状态待核对';

  window.createGameDeployments = function({context,refresh,auth}) {
    const views=new Map(), saved=new Map(), details=new Map(), reading=new Set(), expanded=new Set();
    let currentView=null, fleet=null, fleetBusy=false, fleetError='', fleetController=null, stopped=false;
    try { for(const p of JSON.parse(sessionStorage.getItem(STORAGE)||'[]')) if(p && ['deploy','recover'].includes(p.kind) && typeof p.request_id==='string' && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(p.request_id) && serialOK(p.serial) && p.node_id && p.game_id && p.version && (p.operation===undefined||Object.hasOwn(OPERATIONS,p.operation)) && ['check','apply','recover'].includes(p.mode) && (p.kind!=='recover'||/^[0-9a-f]{12}$/.test(p.source_job_id))) saved.set(keyOf(p),{...p,operation:p.operation||'deploy',queried:false}); } catch(_){/* Invalid local data cannot establish node state. */}
    const panel=document.getElementById('game-deployment');
    panel.append(el('h3','准备目标设备'),el('p','在同一目标上选择新装、初始化、资源部署或场景诊断；每一步独立排队、留证和恢复，不会自动串行执行下一步。','note'));
    const operationLabel=el('label','准备动作','deploy-operation'), operation=el('select'), operationHelp=el('p',null,'note');operation.id='deploy-operation';operation.setAttribute('aria-label','准备动作');for(const [key,config] of Object.entries(OPERATIONS)){const option=el('option',config.label);option.value=key;operation.append(option);}operationLabel.append(operation);
    const target=el('div',null,'deploy-target'), serialLabel=el('label','目标设备序列号'), serial=el('input'); serial.id='deploy-serial';serial.type='text';serial.autocomplete='off';serial.spellcheck=false;serial.maxLength=256;serial.placeholder='从列表选择，或手动填写 serial';serialLabel.append(serial);
    const listLabel=el('label','最近采样的设备'), device=el('select');device.id='deploy-device';listLabel.append(device);
    const fleetButton=el('button','刷新设备列表','ghost');fleetButton.type='button';fleetButton.id='deploy-refresh-devices';target.append(serialLabel,listLabel,fleetButton);
    const fleetNote=el('p',null,'note'), recipe=el('p',null,'note'), status=el('div',null,'deploy-status'), actions=el('div',null,'submission-actions');
    const sceneSource=el('section',null,'scene-source'), sourceLabel=el('label','原资源部署任务 ID'), sourceJob=el('input'), sourceRead=el('button','读取部署引用','ghost'), sourceNote=el('p',null,'note'), sourceFacts=el('details',null,'asset-details');
    sceneSource.id='scene-source';sourceJob.id='scene-source-job';sourceJob.type='text';sourceJob.autocomplete='off';sourceJob.spellcheck=false;sourceJob.maxLength=12;sourceJob.placeholder='12 位小写十六进制任务 ID';sourceLabel.append(sourceJob);sourceRead.id='scene-source-read';sourceRead.type='button';sourceNote.setAttribute('role','status');sceneSource.append(sourceLabel,sourceRead,sourceNote,sourceFacts);
    const check=el('button','检查部署条件','ghost'), apply=el('button','部署到新资源目录','ghost primary');check.type=apply.type='button';check.id='deploy-check';apply.id='deploy-apply';actions.append(check,apply);
    const help=el('p',null,'note'), message=el('p',null,'note');message.setAttribute('role','status');
    const requestPanel=el('div',null,'deploy-request'), requestText=el('p',null,'request-id'), requestActions=el('div',null,'submission-actions'), query=el('button','查询原请求','ghost'), retry=el('button','使用原请求 ID 重试','ghost');query.type=retry.type='button';query.id='deploy-query';retry.id='deploy-retry';requestActions.append(query,retry);requestPanel.append(requestText,requestActions);
    const receiptLinks=el('div',null,'preparation-links'), historyErrors=el('section'), historyHeading=el('h4','此目标的部署记录'), history=el('ul',null,'preparation-list'), empty=el('p',null,'note');historyErrors.id='deploy-history-errors';history.id='deploy-history';
    panel.append(target,fleetNote,operationLabel,operationHelp,recipe,sceneSource,status,actions,help,message,requestPanel,receiptLinks,historyErrors,historyHeading,history,empty);

    function persist() { try { sessionStorage.setItem(STORAGE,JSON.stringify([...saved.values()].map(({kind,operation,node_id,game_id,version,serial,mode,request_id,source_job_id,deployment,conflict})=>({kind,operation,node_id,game_id,version,serial,mode,request_id,source_job_id,...(deployment?{deployment}:{}),conflict:!!conflict})))); } catch(_){/* Visible request identity remains available for manual lookup. */} }
    function pending(v) { return saved.get(v.key); }
    function support(v,operation=v.operation) { const config=OPERATIONS[operation];return !!config && context().node?.capabilities?.includes(config.capability) && v.record[config.support]?.available===true; }
    function current(v) { return !stopped && !context().disposed && currentView===v && context().selected===`${v.record.game_id}/${v.record.version}`; }
    function canManage(v) { return current(v) && context().canWrite && context().detailFresh && context().node?.node_id===v.record.node_id; }
    function canWrite(v,operation=v.operation) { return canManage(v) && support(v,operation); }
    function canSubmit(v,p) { return p?.kind==='recover' ? canManage(v) : canWrite(v,p?.operation) && (p?.operation!=='scene'||deploymentOK(p.deployment)); }
    function sceneReady(v) { return v.sceneSerial===v.serial && v.sceneDeployment?.report.job_id===v.sceneJob && deploymentOK(v.sceneDeployment); }
    function invalidateScene(v) { if(!v)return;v.sceneController?.abort();v.sceneController=null;v.sceneRevision=(v.sceneRevision||0)+1;v.sceneDeployment=null;v.sceneReading=false;v.sceneError='';v.sceneAt=null; }
    function setMessage(v,value,error=false) { v.message=value;v.error=error; }
    function targetMatches(item,v,targetSerial=v.serial) { return item?.target?.node_id===v.record.node_id && item.target.serial===targetSerial; }
    function recordMatches(item,v,targetSerial=v.serial,operation=v.operation) { return targetMatches(item,v,targetSerial) && operationOf(item)===operation; }
    function reportMatches(report,v,targetSerial=v.serial,operation=v.operation) { return report?.schema===OPERATIONS[operation]?.schema && report.node_id===v.record.node_id && report.game_id===v.record.game_id && report.version===v.record.version && report.serial===targetSerial; }
    function allEntries(v) { return Object.entries(OPERATIONS).flatMap(([operation,config])=>(v.record[config.history]||[]).map(item=>({...item,operation:item.operation??operation}))); }
    function entries(v) { return allEntries(v).filter(item=>recordMatches(item,v)).sort((a,b)=>(b.queue?.submitted_at||b.started||0)-(a.queue?.submitted_at||a.started||0)); }
    function factRows(report) {
      const list=el('dl',null,'deploy-facts');
      const initialization=report?.schema===OPERATIONS.initialize.schema, scene=report?.schema===OPERATIONS.scene.schema;
      for(const [key,label] of [...(initialization?[['storage_initialization','存储目录初始化']]:[]),...(scene?[['runtime_seed','运行种子'],['launcher_configuration','启动配置'],['scene_observation','场景观察']]:[]),...((initialization||scene)?[['launch','启动尝试'],['stop','本次停止']]:[]),['installed','设备包状态'],['resources_deployed','设备资源'],['login','登录'],['scene_ready','场景']]) { const fact=report?.[key];list.append(el('dt',label),el('dd',key==='launch'&&fact?.status==='not_attempted'?'未尝试启动':FACT[fact?.status]||text(fact?.status))); }
      if(initialization||scene)list.append(el('dt','目标全部进程退出'),el('dd',report.stop?.all_target_processes_gone==='unknown'?'未知（unknown）':text(report.stop?.all_target_processes_gone)));
      if(scene)list.append(el('dt','限定恢复'),el('dd',({verified:'限定范围已结算',reported_complete:'调用者报告限定恢复完成',required:'仍需恢复',not_reported:'未报告'})[report.recovery?.status]||text(report.recovery?.status)));
      return list;
    }
    function evidence(report) {
      const box=el('details',null,'asset-details');box.append(el('summary','检查依据、访问范围与局限'));
      const facts=el('dl',null,'facts');
      const initialization=report.schema===OPERATIONS.initialize.schema, scene=report.schema===OPERATIONS.scene.schema;
      for(const [key,label] of [...(report.schema===OPERATIONS.install.schema?[['apk','APK 身份']]:[]),...(initialization?[['storage_initialization','存储目录依据']]:[]),...(scene?[['input','原部署与输入依据'],['runtime_seed','运行种子依据'],['launcher_configuration','启动配置依据'],['scene_observation','本次场景观察']]:[]),...((initialization||scene)?[['launch','启动尝试依据'],['stop','限定停止依据']]:[]),['installed','包状态依据'],['resources_deployed','资源依据'],['application_access','应用 UID 读取'],['login','登录依据'],['scene_ready','场景依据'],['recovery','恢复范围']]) facts.append(el('dt',label),el('dd',text(report[key])));
      box.append(facts,el('p',scene?'以上为本次诊断的历史观测。启动回执、日志或截图不能单独证明 demo 已载入、画面相同、模拟状态相同或性能实验资格。新运行证据与应用配置保留；恢复仅接续欠下的停止，不重放场景。':initialization?'目录就绪不代表资源已部署、应用读取或场景就绪。首次运行的配置与副作用保留；停止只核对本次 Launcher 尝试，数字 UID 进程观测不证明目标全部进程已退。以上为该次任务的历史观测。':'APK 元数据、实际安装、资源校验、应用读取和场景就绪分别核验。以上为该次任务的历史观测；安装成功不代表独立证书审计或游戏已就绪。','note'));
      if(report.checks)box.append(el('pre',JSON.stringify(report.checks,null,2),'command-box'));
      return box;
    }
    function renderHistory(v,items) {
      history.replaceChildren();empty.textContent=!v.serial?'选择目标后查看对应记录。':!items.length?`此版本尚无该目标的${operationName(v.operation)}记录。`:'';
      for(const item of items) {
        const row=el('li',null,'preparation'), heading=el('div',null,'preparation-heading'), badge=el('span',jobLabel(item),'task-state'+(item.queue?.state==='blocked'?' unknown':''));heading.append(el('strong',`${operationName(operationOf(item))} · ${modeName(item.mode,operationOf(item))} · ${item.id}`),badge);row.append(heading);
        row.append(el('p',`提交：${time(item.queue?.submitted_at||item.started)} · 请求：${text(item.request_id)}`,'game-item-meta'));
        const known=details.get(item.id), value=known||item, report=value.report;
        if(report && reportMatches(report,v)) { row.append(el('p',`报告：${phaseName(report.phase,v.operation)} · 历史观测 ${time(report.observed_at)}`,'note')); if(report.error)row.append(el('p',text(report.error),'asset-error')); }
        else row.append(el('p',report?'报告身份与目标不符，未作为设备事实展示。':'尚无可读事务报告；接受任务不代表设备准备或恢复已完成。','note'));
        if(value.report_error)row.append(el('p',`报告读取：${text(value.report_error)}`,'asset-error'));
        const links=el('div',null,'preparation-links');links.append(link('任务、日志与队列恢复',item.job_url)||link('任务、日志与队列恢复',jobURL(item)));const original=link(`${operationName(v.operation)}报告原件`,value.report_url||item.report_url);if(original)links.append(original);row.append(links);
        const box=el('details',null,'deploy-history-detail');box.open=expanded.has(item.id);box.addEventListener('toggle',()=>box.open?expanded.add(item.id):expanded.delete(item.id));box.append(el('summary','读取回执与恢复资格'));
        const read=el('button',reading.has(item.id)?'正在读取…':'读取最新回执','ghost');read.type='button';read.disabled=reading.has(item.id);read.setAttribute('aria-busy',String(reading.has(item.id)));read.addEventListener('click',()=>readDeployment(v,item));box.append(read);
        if(report && reportMatches(report,v))box.append(factRows(report),evidence(report));
        const recovery=known?.recovery, recover=el('button','提交恢复任务','ghost');recover.type='button';recover.disabled=!recordMatches(known,v)||!recovery?.supported||!recovery.eligible||!/^[0-9a-f]{12}$/.test(recovery.target_job_id)||!canManage(v)||v.busy||!!pending(v);recover.addEventListener('click',()=>start(v,'recover',item.id));
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
      fleetNote.textContent=fleetError || (!fleet?'尚未读取设备列表。手填或点击刷新；普通版本刷新不会采样手机。':`设备快照：${time(fleet.sampled_at)} · ${fleet.devices===null?'尚无成功采样':`${fleet.devices?.length||0} 台记录`}${fleet.stale?' · 过期':''}${fleet.error?` · 错误：${fleet.error}`:''}${fleet.deferred?` · 暂缓：${fleet.deferred}`:''}。旧快照不证明当前可用，设备任务会重新核查。`);
      const config=OPERATIONS[v.operation], plan=v.record.manifest?.[config.recipe];operation.value=v.operation;operationHelp.textContent=config.scope;check.textContent=config.check;apply.textContent=config.apply;
      recipe.textContent=plan?`配方目标：${text(plan.package)} · versionCode ${text(plan.version_code)} · ${v.operation==='scene'?`demo ${text(plan.demo_name)} · ${text(plan.renderer?.opengl)} · 观察 ${text(plan.wait_seconds)} 秒。使用已缓存的小种子与原部署资源；原描述性 scene 信息不作为执行配方。`:v.operation==='initialize'?`目录观察上限 ${text(plan.wait_seconds)} 秒。核对已装 APK 身份；不部署资源。`:`${v.operation==='install'?`APK ${text(plan.apk_asset_name)}`:`${plan.files?.length||0} 项资源`}。仅使用配方引用的已验证缓存；不会自动下载工具。`}`:`此版本尚无${config.label}配方，需要登记含 ${config.recipe} 的新版本。`;
      historyHeading.textContent=`此目标的${config.label}记录`;
      const p=pending(v),items=entries(v),active=allEntries(v).find(item=>targetMatches(item,v)&&(item.state!=='finished'||item.queue?.state==='blocked'));
      sceneSource.hidden=v.operation!=='scene';sourceJob.value=v.sceneJob||'';sourceJob.disabled=v.busy||!!p;sourceRead.disabled=v.busy||!!p||!!v.sceneReading||!support(v)||!plan||!context().detailFresh||context().node?.node_id!==v.record.node_id||!serialOK(v.serial)||!/^[0-9a-f]{12}$/.test(v.sceneJob||'');sourceRead.textContent=v.sceneReading?'正在读取两份引用…':'读取部署引用';sourceRead.setAttribute('aria-busy',String(!!v.sceneReading));
      sourceNote.textContent=v.sceneError||(v.sceneReading?'正在只读获取 deployment.json 与 game-deploy/session.json。':sceneReady(v)?`已读取 ${time(v.sceneAt)} · 原任务 ${v.sceneJob} · 当前目标 ${v.serial}。仅核对来源身份；提交后由任务核对原部署、设备和应用。`:'填写原资源部署任务 ID 并显式读取两份引用。允许同游戏旧版本的部署；变更目标或来源后需重新读取。');sourceNote.classList.toggle('task-error',!!v.sceneError);
      sourceFacts.replaceChildren();sourceFacts.hidden=!sceneReady(v);if(sceneReady(v)){sourceFacts.append(el('summary','已冻结的部署引用'),el('pre',JSON.stringify(v.sceneDeployment,null,2),'command-box'));const original=link('查看原资源部署任务',jobURL({id:v.sceneJob}));if(original)sourceFacts.append(original);}
      const problem=!support(v)?v.record[config.support]?.error||`此节点未提供${config.label}能力。`:!plan?`当前清单无${config.label}配方。`:!context().detailFresh?'当前版本尚未成功刷新，保留历史记录。':!context().canWrite?'请先解锁操作权限。':!serialOK(v.serial)?'选择目标设备，或填写有效的序列号。':p?'有未确认请求；先按原 ID 查询，不创建另一请求。':active?'该目标已有未结束或保留资源的任务，请先打开原任务核查。':v.operation==='scene'&&!sceneReady(v)?'先读取并核对原资源部署的两份引用。':!globalThis.crypto?.randomUUID?'请通过 HTTPS 或本机地址提交。':'';
      check.disabled=apply.disabled=!!problem||v.busy||!canWrite(v);check.setAttribute('aria-busy',String(v.busy&&v.busyOperation===v.operation&&v.busyMode==='check'));apply.setAttribute('aria-busy',String(v.busy&&v.busyOperation===v.operation&&v.busyMode==='apply'));
      help.textContent=(problem||({install:'预检只读；新装会再次确认包明确缺席与禁止替换能力。成功保留应用，资源部署与场景核验需另行提交。',initialize:'预检只读；初始化仅在目录未就绪且原应用已停止时尝试启动，之后确认停止。目录仍未达标但停止已确认时需要后续协助；停止义务未结算则先恢复。队列保留仍需显式核查。',scene:'预检只读，不创建运行目录或启动应用；场景尝试会放入小种子、有界启动并停止。场景未达标但停止已确认时需要协助，未知启动或停止结果须先恢复。不会自动进入性能测量。',deploy:'预检与部署都经设备队列执行。预检只读；部署会再次核对引擎与缓存，并创建新资源目录。'})[v.operation])+(v.operation==='install'&&!support(v)&&v.record.installation_support?.recovery_available===true?' 旧安装事务仍可读取恢复资格，是否允许提交以对应回执为准。':'');
      status.replaceChildren();const latest=items.map(item=>details.get(item.id)||item).find(item=>reportMatches(item.report,v));
      if(!v.serial)status.append(el('p','选择目标后显示当前准备动作的设备事实与原件。','note'));
      else if(!latest)status.append(el('p',`此目标尚无匹配的${config.label}观测；可先提交只读预检。`,'note'));
      else { status.append(el('p',`最近有据记录 · ${config.label} · ${modeName(latest.mode,v.operation)}${['initialize','scene'].includes(v.operation)?` · ${phaseName(latest.report.phase,v.operation)}`:''} · ${time(latest.report.observed_at)} · ${latest.job?.id||latest.id}`,'read-time'),factRows(latest.report),evidence(latest.report)); }
      message.textContent=v.message||'';message.classList.toggle('task-error',!!v.error);
      requestPanel.hidden=!p;requestText.textContent=p?`未确认 ${operationName(p.operation)} · ${modeName(p.mode,p.operation)} · 节点 ${p.node_id} · ${p.game_id}/${p.version} · 设备 ${p.serial}\n请求 ID：${p.request_id}${p.source_job_id?` · 原任务 ${p.source_job_id}`:''}${p.operation==='scene'&&p.kind!=='recover'?deploymentOK(p.deployment)?` · 资源来源 ${p.deployment.report.job_id}`:' · 原部署引用不完整，只能查询，不能重构请求':''}`:'';
      query.disabled=v.busy;retry.disabled=v.busy||!canSubmit(v,p)||!p?.queried||!!p?.conflict;retry.hidden=!p||!!p.conflict;
      receiptLinks.replaceChildren();if(v.job?.id)receiptLinks.append(link(`查看原任务 ${v.job.id}`,jobURL(v.job)));if(active&&active.id!==v.job?.id)receiptLinks.append(link(`查看阻断的${operationName(operationOf(active))}任务 ${active.id}`,jobURL(active)));
      historyErrors.replaceChildren();const errors=v.record.deployment_history_errors||[];historyErrors.hidden=!errors.length;
      if(errors.length){historyErrors.append(el('h4','未能归类的历史记录'),el('p','以下记录的操作或目标身份未确认，未用作当前设备的准备事实。','note'));for(const item of errors){const row=el('div',null,'preparation');row.append(el('strong',`任务 ${text(item.id)} · 状态未知`),el('p',text(item.report_error),'asset-error'));const original=link('查看原任务',item.job_url)||link('查看原任务',jobURL(item));if(original)row.append(original);historyErrors.append(row);}}
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
      if(value?.schema_version!==1||!value.job?.id||operationOf(value)!==p.operation||value.target?.node_id!==p.node_id||value.target.serial!==p.serial|| (p.kind==='deploy'&&value.mode!==p.mode) || (p.kind==='recover'&&value.job.recovery_for!==p.source_job_id))throw new Error('回执未匹配原操作、目标或请求类型；保留原 ID。');
      if(value.report && !reportMatches(value.report,v,p.serial,p.operation))throw new Error('报告与原操作/版本/设备绑定不匹配。');return value;
    }
    async function readDeployment(v,item) {
      if(reading.has(item.id))return;reading.add(item.id);expanded.add(item.id);render(v);
      try { const value=await http(`${API}/jobs/${encodeURIComponent(item.id)}/deployment`);if(value?.job?.id!==item.id||!recordMatches(value,v,item.target.serial,operationOf(item))||(value.report&&!reportMatches(value.report,v,item.target.serial,operationOf(item))))throw new Error('回执目标不匹配。');details.set(item.id,value); }
      catch(error){details.set(item.id,{...item,recovery:null,read_error:`读取失败：${error.message}；未确认恢复资格。`});if([401,403].includes(error.status))await auth();}
      finally {reading.delete(item.id);render(v);}
    }
    function invalidateRecovery(v,p) {
      if(p.kind!=='recover')return;
      for(const [id,value] of details) if(recordMatches(value,v,p.serial,p.operation)&&value.recovery) details.set(id,{...value,recovery:{...value.recovery,eligible:false,reason:'恢复任务已记录，原资格已失效；重新读取回执后再决定后续操作。'}});
    }
    async function submit(v,p) {
      if(v.busy||!canSubmit(v,p))return;v.busy=true;v.busyMode=p.mode;v.busyOperation=p.operation;setMessage(v,'正在提交；节点可能已记录，不会自动重试。');render(v);
      try {
        const path=p.kind==='recover'?`${API}/jobs/${encodeURIComponent(p.source_job_id)}/deployment/recover`:`${API}/games/${encodeURIComponent(p.game_id)}/versions/${encodeURIComponent(p.version)}/${p.operation}`;
        const body=p.kind==='recover'?{}:{node_id:p.node_id,serial:p.serial,mode:p.mode,...(p.operation==='scene'?{deployment:p.deployment}:{})};const job=await http(path,{body,requestID:p.request_id});
        if(!job?.id||job.request_id!==p.request_id||(p.kind==='recover'&&job.recovery_for!==p.source_job_id))throw new Error('回包未确认原请求身份。');
        saved.delete(v.key);persist();invalidateRecovery(v,p);v.job=job;setMessage(v,`${operationName(p.operation)} · ${modeName(p.mode,p.operation)}任务已记录：${job.id} · 设备 ${p.serial} · ${jobLabel(job)}。接受任务不代表设备准备或恢复已完成。`);
        if(current(v))await refresh(p);
      } catch(error) {
        if(error.status>=400&&error.status<500&&error.status!==409){saved.delete(v.key);persist();}
        setMessage(v,`${error.message}。${pending(v)?'保留原目标和请求 ID；先查询，再决定是否用原 ID 重试。':'节点明确拒绝此请求；修正条件后可新建尝试。'}`,true);if([401,403].includes(error.status))await auth();
      } finally {v.busy=false;render(v);}
    }
    async function start(v,mode,source) {
      if(!(mode==='recover'?canManage(v):canWrite(v))||v.busy||pending(v)||!serialOK(v.serial)||!globalThis.crypto?.randomUUID)return;
      if(mode==='recover') {const value=details.get(source);if(!value?.recovery?.eligible||!value.recovery.supported||!recordMatches(value,v)||!/^[0-9a-f]{12}$/.test(value.recovery.target_job_id))return;source=value.recovery.target_job_id;}
      if(mode!=='recover'&&v.operation==='scene'&&!sceneReady(v))return;
      const p={kind:mode==='recover'?'recover':'deploy',operation:v.operation,node_id:v.record.node_id,game_id:v.record.game_id,version:v.record.version,serial:v.serial,mode,request_id:crypto.randomUUID(),...(source?{source_job_id:source}:{}),...(mode!=='recover'&&v.operation==='scene'?{deployment:{report:{...v.sceneDeployment.report},state:{...v.sceneDeployment.state}}}:{}),queried:false};saved.set(v.key,p);persist();await submit(v,p);
    }
    async function queryPending(v) {
      const p=pending(v);if(!p||v.busy)return;let found=false;v.busy=true;setMessage(v,'正在查询原请求，没有重新提交。');render(v);
      try {
        const job=await http(`${API}/requests/${encodeURIComponent(p.request_id)}`);if(!job?.id||job.request_id!==p.request_id)throw new Error('原请求回包身份不匹配。');v.job=job;found=true;
        const value=verifyDeployment(await http(`${API}/jobs/${encodeURIComponent(job.id)}/deployment`),v,p);
        if(value.job.id!==job.id)throw new Error('任务回执身份不匹配。');details.set(job.id,value);saved.delete(v.key);persist();invalidateRecovery(v,p);
        setMessage(v,`已找回${operationName(p.operation)}原任务 ${job.id} · ${jobLabel(job)}，没有重复提交。设备事实以该任务的事务报告为准。`);if(current(v))await refresh(p);
      } catch(error) {if(found && error.status===404){p.conflict=true;persist();}setMessage(v,found&&error.status===404?'原请求已对应一个任务，但没有匹配的事务回执。请打开原任务核查，禁止将该 ID 用作另一设备准备操作。':error.status===404?'尚未找到原请求。保留原 ID；可稍后查询或明确使用原 ID 重试。':`查询失败：${error.message}。保留原绑定，不自动重发。`,true);if([401,403].includes(error.status))await auth();}
      finally {p.queried=true;v.busy=false;render(v);}
    }
    async function refreshFleet() {
      if(fleetBusy)return;fleetBusy=true;fleetError='';fleetController=new AbortController();render();
      try {const value=await http(`${API}/fleet?wait=5`,{signal:fleetController.signal});if(value.node_id!==context().node?.node_id)throw new Error('设备快照节点身份不匹配。');fleet=value;}
      catch(error){fleetError=`设备列表读取失败：${error.message}。保留旧快照，可手填 serial。`;if([401,403].includes(error.status))await auth();}
      finally {fleetBusy=false;fleetController=null;render();}
    }
    async function readSceneSources(v) {
      if(!current(v)||sourceRead.disabled)return;
      invalidateScene(v);const revision=v.sceneRevision,job=v.sceneJob,targetSerial=v.serial,nodeID=v.record.node_id,controller=new AbortController();v.sceneController=controller;v.sceneReading=true;render(v);
      try {
        const paths=['deployment.json','game-deploy/session.json'];
        const values=await Promise.all(paths.map(path=>http(`${API}/jobs/${encodeURIComponent(job)}/input-reference?path=${encodeURIComponent(path)}`,{signal:controller.signal})));
        if(!current(v)||v.sceneRevision!==revision)return;
        for(let i=0;i<values.length;i++)if(values[i]?.schema!=='phonefarm/job-input-reference@1'||values[i].node_id!==nodeID||!sourceOK(values[i].source,job,paths[i]))throw new Error('引用未匹配本节点、原部署任务或固定路径；未接受这组引用。');
        const sources=values.map(({source:{job_id,path,size,sha256}})=>({job_id,path,size,sha256}));v.sceneDeployment={report:sources[0],state:sources[1]};v.sceneSerial=targetSerial;v.sceneAt=Date.now()/1000;
      } catch(error) { if(current(v)&&v.sceneRevision===revision){v.sceneError=`读取部署引用失败：${error.message}。两份引用均未确认，不会提交。`;if([401,403].includes(error.status))await auth();} }
      finally {controller.abort();if(v.sceneRevision===revision){v.sceneController=null;v.sceneReading=false;render(v);}}
    }
    operation.addEventListener('change',()=>{if(currentView&&OPERATIONS[operation.value]){invalidateScene(currentView);currentView.operation=operation.value;currentView.message='';currentView.error=false;render();}});
    serial.addEventListener('input',()=>{if(currentView){invalidateScene(currentView);currentView.serial=serial.value.trim();currentView.message='';currentView.error=false;render();}});
    device.addEventListener('change',()=>{if(currentView&&device.value){invalidateScene(currentView);currentView.serial=device.value;currentView.message='';currentView.error=false;render();}});
    sourceJob.addEventListener('input',()=>{if(currentView){invalidateScene(currentView);currentView.sceneJob=sourceJob.value.trim();render();}});sourceRead.addEventListener('click',()=>currentView&&readSceneSources(currentView));
    fleetButton.addEventListener('click',refreshFleet);check.addEventListener('click',()=>{if(currentView&&!check.disabled)return start(currentView,'check');});apply.addEventListener('click',()=>{if(currentView&&!apply.disabled)return start(currentView,'apply');});
    query.addEventListener('click',()=>currentView&&queryPending(currentView));retry.addEventListener('click',()=>{if(currentView&&!retry.disabled)return submit(currentView,pending(currentView));});
    function update() {
      stopped=!!context().disposed;const record=context().record;
      if(!record||context().selected!==`${record.game_id}/${record.version}`){invalidateScene(currentView);currentView=null;panel.hidden=true;return;}
      const key=keyOf(record);if(currentView?.key!==key)invalidateScene(currentView);if(!views.has(key))views.set(key,{key,record,operation:saved.get(key)?.operation||(record.manifest?.deployment?'deploy':record.manifest?.installation?'install':record.manifest?.initialization?'initialize':record.manifest?.scene_attempt?'scene':'deploy'),serial:saved.get(key)?.serial||'',sceneJob:saved.get(key)?.deployment?.report?.job_id||'',busy:false,message:'',error:false,job:null});
      currentView=views.get(key);currentView.record=record;render();
    }
    return {update,stop(){stopped=true;fleetController?.abort();invalidateScene(currentView);}};
  };
})();
