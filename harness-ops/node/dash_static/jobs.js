/* Reads stay host-only. Mutations require steering access; recovery is separate evidence. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const API = '/dash/api';
  const LOG_CHUNK = 64 * 1024;
  const LOG_WINDOW = 256 * 1024;
  let jobLeasesRevision = 0, jobLeasesBusy = false, jobLeasesRead = false;
  let jobInputsRevision = 0, jobInputsBusy = false, jobInputsRead = false, inputReference = '', inputReferenceRevision = 0;
  let nodeRevision = 0, listRevision = 0, detailRevision = 0, artifactRevision = 0, metadataRevision = 0, logRevision = 0;
  let jobsCursor = null, filesCursor = null, selected = '', directory = '';
  let historySource = null, jobSources = [], jobSourcesRevision = 0, jobSourcesError = '';
  const historical = () => historySource !== null;
  let exportRevision = 0, exportController = null;
  let logOffset = 0, logText = '', logDecoder = new TextDecoder();
  let activeFilters = {query: '', state: ''};
  let currentJob = null, access = null, accessRevision = 0, accessBusy = false, accessError = '';
  const cancelling = new Set(), cancelReceipts = new Map(), releasing = new Set(), releaseReceipts = new Map();
  const finalizing = new Set(), finalizeReceipts = new Map();
  const reconciling = new Set(), reconcileReceipts = new Map();
  let nodeInfo = null, leaseSnapshot = null, queueRevision = 0, submissionBusy = false, pendingRequest = null, submittedJob = '';
  let runtimeBusy = false;
  let deviceRegistry = null, deviceBinding = null, deviceBindingBusy = false, deviceBindingRevision = 0, deviceBindingError = '';
  const REQUEST_STORAGE = 'hgamey.pending-job-request';
  const JOB_EVENT_STORAGE = 'hgamey.job-browser-position.';
  const jobEventPositions = new Map();
  let jobEventPosition = null, jobEventsController = null, jobEventsGeneration = 0, jobEventsBusy = false;
  let jobEventsTrimmed = 0, jobEventsStorageError = '', jobEventsDisposed = false;
  const PHASES = {preparing: '准备中', queued: '排队中', blocked: '资源保留 · 待核查', starting: '正在启动', running: '运行中', cancelling: '停止请求处理中', finalizing: '正在保存结束记录', storage_blocked: '结果保存失败 · 资源保留', finished: '已结束', interrupted: '执行中断'};

  const text = value => value == null ? '未记录' : String(value);
  function el(tag, value, className) {
    const node = document.createElement(tag);
    if (value != null) node.textContent = String(value);
    if (className) node.className = className;
    return node;
  }
  function message(id, value, error = false) {
    $(id).textContent = value;
    $(id).classList.toggle('task-error', error);
  }
  function busy(id, value, label) {
    const button = $(id);
    button.disabled = value;
    button.setAttribute('aria-busy', String(value));
    if (!button.dataset.idleLabel) button.dataset.idleLabel = button.textContent;
    button.textContent = value && label ? label : button.dataset.idleLabel;
  }
  function regionBusy(id, value) { $(id).setAttribute('aria-busy', String(value)); }
  function readTime() { return `读取于 ${new Date().toLocaleTimeString()}`; }
  function time(value) {
    if (value == null) return '未记录';
    const date = new Date(typeof value === 'number' ? value * 1000 : value);
    return Number.isNaN(date.getTime()) ? '未记录' : date.toLocaleString();
  }
  function renderCommitted(job) {
    const revision = job.status_revision, scope = job.status_revision_scope, record = job.committed_status;
    const known = Number.isSafeInteger(revision) && revision > 0 && scope === 'committed_job_state' && record;
    $('committed-summary').textContent = known ? `Node 已提交状态 · v${revision} · ${time(job.status_committed_at)}`
      : revision === 0 ? 'Node 已提交状态 · 尚未建立版本记录（0）' : 'Node 已提交状态 · 历史记录未提供';
    facts('committed-info', [['已提交版本', known ? revision : revision === 0 ? '0（尚未建立）' : '未提供，不回填'],
      ['提交时间', time(job.status_committed_at)], ['版本范围', scope === 'committed_job_state' ? 'Node 已提交作业状态（committed_job_state）' : '未提供'],
      ['已提交队列状态', record?.queue_state], ['最后提交的执行阶段', record?.phase], ['最后提交的恢复状态', record?.recovery_status]]);
    $('committed-record').textContent = record ? JSON.stringify(record, null, 2) : '尚无已提交状态原记录；当前执行观察仍见任务详情。';
  }
  const jobEventsSupported = () => nodeInfo?.capabilities?.includes('jobs.state-events-v1') === true;
  function jobEventLocation(position = jobEventPosition) {
    return position?.cursor ? {schema: 'phonefarm/job-browser-position@1', target: position.target, job_id: position.job, cursor: position.cursor} : null;
  }
  function jobEventsProblem() {
    if (!jobEventPosition) return '请先选择任务并核对节点。';
    return HGTarget.problem(jobEventPosition.target) || (!HGTarget.canWrite() ? '节点尚未提供可核验的显式目标绑定。' : '')
      || (!jobEventsSupported() ? '此节点未提供 jobs.state-events-v1；原任务和日志仍可读取，已保存游标保留。' : '')
      || (!currentJob || currentJob.id !== jobEventPosition.job ? '请先读取本任务的当前状态。' : '')
      || (currentJob.status_revision == null ? '此历史任务未提供 Node 提交记录；不能伪造其事件历史。' : '');
  }
  function renderJobEvents() {
    const problem = jobEventsProblem(), position = jobEventPosition;
    const readable = !problem && !jobEventsBusy && !jobEventsDisposed && !document.hidden;
    $('job-events-next').disabled = !readable || !!position?.issue;
    $('job-events-next').textContent = jobEventsBusy ? '读取中…' : position?.cursor ? '从原位置读取下一页' : '读取第一页';
    $('job-events-next').setAttribute('aria-busy', String(jobEventsBusy));
    $('job-events-restart').disabled = !readable;
    $('job-events-copy').disabled = !position?.cursor;
    $('job-events-list').setAttribute('aria-busy', String(jobEventsBusy));
    $('job-events-scope').textContent = position ? `任务 ${position.job} · 节点 ${position.target.node_id}` : '尚未绑定事件范围。';
    message('job-events-help', problem || position?.issue || '只读取 Node 已提交事件，每次一页；不会刷新执行状态、采集设备或启动任务。', !!position?.issue);
    $('job-events-position').textContent = !position ? '' : `${position.after != null ? `已浏览至事件 #${position.after}。` : position.cursor ? '已恢复原浏览游标，续读时核对事件流。' : '尚未浏览事件。'}${position.readAt ? ` 最近成功读取：${time(position.readAt)}。` : ''}`;
    $('job-events-newer').textContent = position?.latestRevision > (currentJob?.status_revision ?? Infinity)
      ? `事件包含 Node 已提交 v${position.latestRevision}，可点“刷新任务”读取当前状态；事件不会自动更改操作资格。` : '';
    message('job-events-storage', jobEventsStorageError);
    $('job-events-trimmed').textContent = jobEventsTrimmed ? `较早 ${jobEventsTrimmed} 条已移出本页；保留最近 200 条，原历史可显式从头读取。` : '';
  }
  function stopJobEvents(reason = '') {
    ++jobEventsGeneration; jobEventsController?.abort(); jobEventsController = null; jobEventsBusy = false;
    if (!jobEventsDisposed) { renderJobEvents(); if (reason) message('job-events-message', reason); }
  }
  function syncJobEventScope() {
    const target = HGTarget.binding(), job = historical() ? '' : selected;
    const key = target && job ? JOB_EVENT_STORAGE + JSON.stringify([target.entry, target.node_id, job]) : null;
    if (key === jobEventPosition?.key || !key && !jobEventPosition) { renderJobEvents(); return; }
    stopJobEvents(); jobEventsTrimmed = 0; jobEventsStorageError = '';
    $('job-events-list').replaceChildren(); $('job-events-location').hidden = true; $('job-events-location-json').value = '';
    if (!key) { jobEventPosition = null; renderJobEvents(); return; }
    let position = jobEventPositions.get(key);
    if (!position) {
      position = {key, target, job, cursor: null, after: null, readAt: null, latestRevision: null, issue: ''};
      try {
        const raw = localStorage.getItem(key);
        if (raw) {
          try {
            const saved = JSON.parse(raw);
            if (saved.schema !== 'phonefarm/job-browser-position@1' || saved.target?.entry !== target.entry || saved.target?.node_id !== target.node_id
                || saved.job_id !== job || typeof saved.cursor !== 'string' || !saved.cursor) throw new Error('invalid position');
            position.cursor = saved.cursor;
          } catch (_) { position.issue = '保存的浏览位置不可读，未自动归零；请保留原记录并显式从头读取。'; }
        }
      } catch (_) { jobEventsStorageError = '本地存储不可用，浏览位置仅保留在本页；离开前可复制。'; }
      jobEventPositions.set(key, position);
    }
    jobEventPosition = position; renderJobEvents();
    message('job-events-message', position.cursor ? '原浏览位置已载入，尚未请求事件；请显式续读。' : '尚未读取此任务事件。');
  }
  function checkedJobEventPage(value, position, startAfter) {
    if (value?.schema_version !== 1 || value.node_id !== position.target.node_id || value.job_id !== position.job
        || typeof value.stream_id !== 'string' || !value.stream_id || !Array.isArray(value.items) || value.items.length > 100
        || !Number.isSafeInteger(value.head) || value.head < 0 || !Number.isSafeInteger(value.next_after)
        || value.next_after < 0 || value.next_after > value.head || typeof value.next_cursor !== 'string' || !value.next_cursor
        || typeof value.has_more !== 'boolean') throw new Error('事件回包身份、范围或位置未确认。');
    let after = startAfter ?? -1;
    for (const item of value.items) {
      if (item.job_id !== position.job || !Number.isSafeInteger(item.sequence) || item.sequence <= 0 || item.sequence <= after
          || !Number.isSafeInteger(item.revision) || item.revision < 1 || typeof item.event_id !== 'string' || !item.event_id
          || !['admitted', 'queue_transition', 'worker_observation', 'baseline'].includes(item.kind)
          || !item.committed_status || typeof item.committed_status !== 'object') throw new Error('事件身份或顺序未确认。');
      after = item.sequence;
    }
    if (value.items.length ? value.next_after !== after : startAfter != null && value.next_after !== startAfter) throw new Error('事件续读位置不一致。');
    return value;
  }
  function jobEventRow(item) {
    const row = el('li'), state = item.committed_status;
    const kind = {admitted: '已接收任务', queue_transition: '队列状态提交', worker_observation: 'worker 观察提交', baseline: '开始建立记录'}[item.kind];
    row.append(el('strong', `#${item.sequence} · v${item.revision} · ${kind}`), el('p', `提交于 ${time(item.at)}`, 'read-time'));
    const info = el('dl', null, 'facts');
    for (const [name, value] of [['队列状态', state.queue_state], ['执行阶段', state.phase], ['结果', state.outcome], ['恢复状态', state.recovery_status]]) info.append(el('dt', name), el('dd', text(value)));
    const detail = el('details', null, 'command-details');
    detail.append(el('summary', '已提交事件原记录'), el('pre', JSON.stringify(item, null, 2), 'command-box'));
    row.append(info, detail); return row;
  }
  async function loadJobEvents(restart = false) {
    syncJobEventScope();
    if (jobEventsBusy || jobEventsDisposed || document.hidden || jobEventsProblem() || !restart && jobEventPosition.issue) return;
    const position = jobEventPosition, generation = ++jobEventsGeneration;
    const startAfter = restart ? 0 : position.cursor ? position.after : 0;
    const query = new URLSearchParams({job_id: position.job, limit: '100'});
    if (!restart && position.cursor) query.set('cursor', position.cursor); else query.set('after', '0');
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 15000);
    jobEventsController = controller; jobEventsBusy = true; renderJobEvents();
    message('job-events-message', restart ? '正在从头读取；成功前保留原游标…' : '正在从原位置读取一页…');
    try {
      const response = await HGTarget.fetch(`${API}/job-events?${query}`, {credentials: 'same-origin', cache: 'no-store', signal: controller.signal}, {target: position.target});
      const value = await response.json();
      if (!response.ok) throw new Error(`事件读取失败（HTTP ${response.status}${value.code ? ' · ' + value.code : ''}）：${typeof value.error === 'string' ? value.error : value.error?.message || value.reason || '请求未确认'}`);
      if (jobEventsDisposed || generation !== jobEventsGeneration || position !== jobEventPosition) return;
      checkedJobEventPage(value, position, startAfter);
      const rows = value.items.map(jobEventRow);
      if (restart) { $('job-events-list').replaceChildren(); jobEventsTrimmed = 0; position.latestRevision = null; }
      $('job-events-list').append(...rows);
      while ($('job-events-list').children.length > 200) { $('job-events-list').firstElementChild.remove(); ++jobEventsTrimmed; }
      position.cursor = value.next_cursor; position.after = value.next_after; position.readAt = Date.now() / 1000; position.issue = '';
      for (const item of value.items) position.latestRevision = Math.max(position.latestRevision || 0, item.revision);
      try { localStorage.setItem(position.key, JSON.stringify(jobEventLocation(position))); jobEventsStorageError = ''; }
      catch (_) { jobEventsStorageError = '无法保存浏览位置；当前内存位置仍可续读，离开前请复制。'; }
      message('job-events-message', `${value.items.length ? `本页读取 ${value.items.length} 条事件。` : '原位置之后暂无事件；当前 worker、lease 或日志仍可能变化。'}${value.has_more ? ' 还有更多，请显式读取下一页。' : ' 本次已读至可见末尾。'}不代表任务已结束或事件已处理。`);
    } catch (error) {
      if (!jobEventsDisposed && generation === jobEventsGeneration) message('job-events-message', `${errorText(error)} 原游标保留，未自动重试或归零。`, true);
    } finally {
      clearTimeout(timeout);
      if (!jobEventsDisposed && generation === jobEventsGeneration) { jobEventsBusy = false; jobEventsController = null; renderJobEvents(); }
    }
  }
  async function copyJobEventPosition() {
    const value = jobEventLocation(); if (!value) return;
    const key = jobEventPosition.key, raw = JSON.stringify(value, null, 2);
    $('job-events-location-json').value = raw; $('job-events-location').hidden = false; $('job-events-location').open = true;
    try {
      await navigator.clipboard.writeText(raw);
      if (jobEventPosition?.key === key) message('job-events-storage', '浏览位置已复制；仅代表已展示，业务处理确认由消费方保存。');
    } catch (_) {
      if (jobEventPosition?.key === key) { $('job-events-location-json').focus(); $('job-events-location-json').select(); message('job-events-storage', '请手动复制浏览位置 JSON；不含凭据或事件正文。'); }
    }
  }
  function bytes(value) {
    if (!Number.isFinite(value)) return '大小未记录';
    if (value < 1024) return `${value} B`;
    if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
    return `${(value / 1024 / 1024).toFixed(1)} MiB`;
  }
  const inputBytes = value => Number.isFinite(value) ? `${bytes(value)}（${value} 字节）` : '未记录';
  function inputLimitFacts(policy) {
    return [['压缩归档上限', inputBytes(policy.compressed_bytes)], ['展开 payload 上限', inputBytes(policy.expanded_bytes)],
      ['gzip 展开流上限', inputBytes(policy.tar_stream_bytes)], ['单个 PAX/GNU 扩展记录上限', inputBytes(policy.metadata_bytes)],
      ['成员 / 路径项上限', policy.entries], ['保留可用空间', inputBytes(policy.min_free_bytes)],
      ['适用范围', policy.scope === 'prepared_input_file_payload' ? '输入准备文件 payload（prepared_input_file_payload）' : policy.scope || '未记录']];
  }
  function renderInputLimits() {
    const supported = nodeInfo?.capabilities?.includes('jobs.input-limits-v1'), policy = supported && nodeInfo.execution?.input_limits;
    facts('node-input-limits-info', policy ? inputLimitFacts(policy) : []);
    message('node-input-limits-message', !nodeInfo ? '当前节点限值尚未取得。' : !supported ? '此节点未声明 jobs.input-limits-v1，输入写入限值未知。' : !policy ? '节点未返回输入限值，不能判断当前策略。' : '这是当前节点的输入准备策略；历史任务采用其原回执中的限值。');
    message('submit-input-limits', policy ? `输入准备共用展开文件 payload 上限 ${bytes(policy.expanded_bytes)}，包含归档文件、工件原件和实际派生 JSON。服务端检查为准；同请求 ID 不会重新准备。` : '输入准备限值尚未确认；不会用未知限值推断可用空间或改写原请求。');
  }
  function renderInputPreparation(job) {
    const receipt = job.input_preparation;
    $('input-write-details').hidden = !receipt;
    $('input-write-record').textContent = receipt ? JSON.stringify(receipt, null, 2) : '';
    if (!receipt) {
      message('input-write-message', '此任务未记录输入写入限值回执；不按当前节点策略回填历史字节数或准备结果。');
      facts('input-write-info', []); return;
    }
    const label = {pending: '准备尚未完成', ready: '输入写入准备就绪', failed: '输入写入准备失败', unknown: '输入写入回执不可读 · 准备结果未知'}[receipt.status] || '输入写入状态未知';
    const error = receipt.error;
    $('input-write-summary').textContent = `${label} · 更新于 ${time(receipt.updated_at)}`;
    message('input-write-message', `${label}。${receipt.status === 'failed' ? `保留任务 ${job.id}${job.request_id ? ` / 请求 ${job.request_id}` : ''}；原 ID 不会重新准备，修正输入后需显式新请求。` : receipt.status === 'pending' ? '请按需刷新任务，已写入字节不代表准备完成。' : receipt.status === 'ready' ? '不表示命令已运行或设备已恢复。' : '请保留原任务与请求 ID 核对；未取得可信回执，不自动重新准备。'}${error ? ` ${error.code || '准备错误'}：${error.message || '请查看完整回执。'}` : ''}`, receipt.status === 'failed' || !!error || !['pending', 'ready'].includes(receipt.status));
    facts('input-write-info', [['任务 ID', job.id], ['请求 ID', job.request_id], ['回执状态', receipt.status],
      ['声明 payload 字节', inputBytes(receipt.declared_bytes)], ['已写入 payload 字节', inputBytes(receipt.written_bytes)],
      ['完整 gzip 展开流', inputBytes(receipt.tar_stream_bytes)], ['最终命名空间项数', receipt.entries],
      ['开始准备', time(receipt.started_at)], ['回执更新', time(receipt.updated_at)],
      ...(receipt.space ? [['预检可用空间', inputBytes(receipt.space.available_bytes)], ['预检所需空间', inputBytes(receipt.space.required_bytes)],
        ['预检保留空间', inputBytes(receipt.space.reserve_bytes)], ['空间观察依据', receipt.space.basis === 'f_bavail' ? 'f_bavail（调用身份可用块）' : receipt.space.basis]] : [['空间预检', '未记录，不推断磁盘可用量']]),
      ...(error ? [['失败代码', error.code], ['错误状态码', error.status], ['失败原因', error.message]] : []),
      ...(receipt.limits ? inputLimitFacts(receipt.limits) : [['本次准备限值', '未记录，不用当前节点策略回填']])]);
  }
  function facts(id, rows) {
    $(id).replaceChildren(...rows.flatMap(([key, value]) => [el('dt', key), el('dd', text(value))]));
  }
  const sourceDirty = value => value === true ? '有本地修改' : value === false ? '声明无本地修改' : '未知';
  function runtimeFacts(runtime) {
    return [['入口路径', runtime?.entry_path || '未记录'], ['Python 版本', runtime?.python_version || '未记录'], ['宿主运行身份观察时间', time(runtime?.observed_at)],
      ['安装构建仓库声明', runtime?.build?.repository || '未记录'], ['安装构建 revision 声明', runtime?.build?.revision || '未记录'], ['安装构建修改声明', sourceDirty(runtime?.build?.dirty)]];
  }
  function exportCacheFacts(node) {
    const cache = node?.storage?.export_cache;
    if (!node?.capabilities?.includes('jobs.export-cache-root-v1') || !cache) return [['目录导出缓存位置', '未提供；旧节点位置未知，不从任务目录推断。']];
    return [['导出缓存模式', ({legacy_job_directory: '作业目录内（legacy_job_directory）', external: '独立缓存根（external）'})[cache.mode] || '未知'],
      ['导出缓存启动观察', ({available: '启动时已打开；不保证当前可写空间', unavailable: '不可用；不会退回作业目录'})[cache.availability] || '未知'],
      ['配置缓存根', cache.configured_root === null ? '未显式配置' : cache.configured_root], ['有效缓存根', cache.effective_root ?? '未取得'],
      ['缓存位置原因', cache.reason ?? '未报告错误'], ['旧导出缓存迁移', cache.legacy_entries_migrated === false ? '未迁移；本页不迁移或清理旧缓存' : '未提供'],
      ['缓存适用范围', cache.scope === 'directory_export' ? '仅当前作业的目录导出派生件；单文件下载与历史来源不依赖此缓存。实际请求仍需核对导出错误。' : cache.scope || '未知']];
  }
  function serviceFacts(service) {
    return [['服务实例 ID', service?.instance_id || '未记录'], ['服务启动时源码树 revision', service?.revision || '未记录'], ['启动源码树修改状态', sourceDirty(service?.dirty)],
      ['源码观察来源', service?.source_kind === 'startup_checkout_observation' ? '服务启动时源码树观察' : service?.source_kind || '旧记录未提供'], ['源码观察时间', time(service?.observed_at)], ...runtimeFacts(service?.runtime)];
  }
  function programFacts(identity) {
    return [['程序自报名称', identity?.program || '未记录'], ['程序包版本', identity?.package_version || '未记录'], ['构建来源 revision 声明', identity?.build?.source_revision || '未记录'],
      ['构建来源修改声明', sourceDirty(identity?.build?.source_dirty)], ['构建 target', identity?.build?.target || '未记录'], ['构建 profile', identity?.build?.profile || '未记录'], ['自报支持协议', identity?.supported_protocols?.join('、') || '未记录']];
  }
  function grayCompatibilityFacts(record) {
    const value = record?.compatibility?.gray_control;
    return [['灰层网关兼容', ({compatible: '兼容新建灰层会话', incompatible: '不兼容新建灰层会话', unknown: '兼容性未知'})[value?.status] || '未提供'],
      ['网关所需声明', value?.required?.join('、') || '未提供'], ['缺失声明', Array.isArray(value?.missing) ? value.missing.join('、') || '无缺项（仅此网关）' : '未知'],
      ['兼容性原因', value?.reason || '未报告'], ['兼容性边界', '仅当前配置 helper 的网关声明；旧 owner 能力、实际执行与设备恢复另行核查。']];
  }
  function isGraySession(body) {
    const argv = body?.argv;
    if (body?.bin !== 'phonefarm' || !Array.isArray(argv) || argv[0] !== 'gray-layer') return false;
    for (let i = 1; i < argv.length;) {
      if (argv[i] === '--lock-label' || argv[i] === '--lock-wait') i += 2;
      else if (argv[i] === '--no-devlock' || typeof argv[i] === 'string' && argv[i].startsWith('--lock-label=')) i++;
      else return argv[i] === 'session';
    }
    return false;
  }
  function graySessionProblem(body) {
    if (!isGraySession(body)) return '';
    if (body.exe != null) return 'program_override_unsupported：新灰层会话仅使用配置 PF，不能替换 exe。';
    if (!nodeInfo?.capabilities?.includes('node.protocol-compat-v1')) return '节点未提供 node.protocol-compat-v1，不能确认新灰层会话网关兼容性。旧任务查询、停止和恢复入口仍可用。';
    const compatibility = nodeInfo.execution?.programs?.phonefarm?.compatibility?.gray_control;
    if (compatibility?.status === 'compatible') return '';
    if (compatibility?.status === 'incompatible') return `program_protocol_unsupported：新灰层会话网关不兼容。缺失：${compatibility.missing?.join('、') || '未提供'}。${compatibility.reason || ''}`;
    return `program_identity_unavailable：配置 PF 身份或网关兼容性未知。${compatibility?.reason || ''} 请在运行身份区显式重新观察，不会自动刷新或换程序。`;
  }
  function submissionCommand() {
    if (pendingRequest?.body) return pendingRequest.body;
    try { return {bin: $('submit-bin').value, argv: JSON.parse($('submit-argv').value)}; } catch (_) { return null; }
  }
  function renderPrograms(node) {
    const entries = Object.entries(node?.execution?.programs || {});
    $('node-programs').replaceChildren(...entries.map(([name, record]) => {
      const detail = el('details', null, 'command-details'), info = el('dl', null, 'facts');
      const state = ({observed: '已观察配置入口', stale: '上次观察已过期', unknown: '身份未知'})[record?.state] || '身份未知';
      detail.append(el('summary', `${name} · ${state}${record?.identity?.package_version ? ' · ' + record.identity.package_version : ''}`));
      const rows = [['缓存状态', state], ['观察时间', time(record?.observed_at)], ['配置解析路径', record?.resolved_path || '未记录'], ['原因', record?.reason || '未报告原因'],
        ['声明适用范围', record?.state === 'observed' ? '此次配置入口观察；不等于某次作业实际执行身份。' : '如保留声明，仅是上次观察，当前入口身份未知。'],
        ...programFacts(record?.identity), ...(name === 'phonefarm' ? grayCompatibilityFacts(record) : []), ['文件观察（stat）', record?.file ? JSON.stringify(record.file) : '未记录']];
      info.append(...rows.flatMap(([key, value]) => [el('dt', key), el('dd', text(value))])); detail.append(info); return detail;
    }));
    if (!entries.length) $('node-programs').append(el('p', '未记录配置程序观察；不能由命令名或相邻源码推断实际版本。', 'note'));
  }
  function renderRuntimeRefresh() {
    const supported = nodeInfo?.capabilities?.includes('node.runtime-v1');
    $('refresh-runtime').disabled = runtimeBusy || !supported || !access?.steer || accessBusy || !HGTarget.canWrite();
    $('refresh-runtime').setAttribute('aria-busy', String(runtimeBusy));
    $('refresh-runtime').textContent = runtimeBusy ? '正在观察配置程序…' : '重新观察配置程序';
    message('runtime-refresh-help', runtimeBusy ? '正在执行有界宿主身份查询，下方保留上次观察；不操作设备，不自动重试。' : !nodeInfo ? '当前节点能力尚未取得，请先读取节点信息。' : !supported ? '此节点未声明 node.runtime-v1，不能刷新程序观察。' : !access?.steer || accessBusy ? '读取缓存无需解锁；重新执行程序身份观察需先解锁任务操作。' : '此按钮才执行新的程序身份观察；页面刷新只读取缓存并检查文件变化。');
  }
  function renderJobRuntime(job) {
    const execution = job.execution || {}, report = execution.program_identity;
    const state = ({pending: '等待主程序自报', reported: '主程序已自报', unknown: '主程序身份未知'})[report?.state] || '主程序身份未知（旧记录未提供）';
    $('job-runtime-summary').textContent = `本次运行入口身份 · ${state}`;
    facts('job-service-identity', execution.service_at_launch ? [['启动时节点 ID', execution.service_at_launch.node_id], ...serviceFacts(execution.service_at_launch.service)] : [['服务启动快照', '未记录；不从当前节点回填。']]);
    facts('job-worker-identity', execution.worker_identity ? runtimeFacts(execution.worker_identity) : [['worker 身份', '未记录；不能使用当前服务版本替代。']]);
    facts('job-program-identity', [['记录状态', state], ['证据来源', report?.source === 'program_self_report' ? '实际启动程序自报；不是恶意程序认证。' : report?.source || '未记录'],
      ['原因', report?.reason || (report ? '未报告原因' : '历史记录缺失，不重新执行程序补报。')], ['自报任务 ID', report?.record?.job_id || '未记录'], ['自报 PID', report?.record?.pid ?? '未记录'], ...programFacts(report?.record?.identity)]);
  }
  function budgetExceeded(job) { return Number.isFinite(job.execution?.time_budget?.exceeded_at); }
  function diskGuardTriggered(job) { return Number.isFinite(job.execution?.disk_guard?.triggered_at); }
  function status(job) {
    if (job.observation === 'historical_readonly') return [`原记录：${job.recorded_phase || job.recorded_state || '状态未记录'}${Number.isInteger(job.recorded_exit) ? ` · exit ${job.recorded_exit}` : ''}`, 'unknown'];
    const exceeded = budgetExceeded(job);
    const trigger = [exceeded ? '运行期限已触发' : '', diskGuardTriggered(job) ? '磁盘保护已触发' : ''].filter(Boolean).join('、');
    if (storageFailed(job)) return [PHASES.storage_blocked, 'unknown'];
    if (job.phase === 'finalizing') return [PHASES.finalizing, 'unknown'];
    if (job.queue?.state === 'blocked' || job.phase === 'blocked') return [trigger ? `资源保留 · ${trigger}` : '资源保留 · 待核查', 'unknown'];
    if (['preparing', 'queued'].includes(job.phase)) return [PHASES[job.phase], ''];
    if (job.phase === 'interrupted' || job.outcome === 'interrupted') return [trigger ? `执行中断 · ${trigger}` : '执行中断 · 待核查', 'unknown'];
    if (job.phase === 'cancelling') return [trigger ? `${trigger} · 停止处理中` : '停止请求处理中', 'unknown'];
    if (job.phase === 'starting') return ['正在启动', 'running'];
    if (trigger) return [`${trigger}${Number.isInteger(job.exit) ? ` · exit ${job.exit}` : ''}`, 'unknown'];
    if (job.outcome === 'cancelled') return job.recovery?.status === 'not_needed' ? ['已取消 · 无需恢复', 'finished'] : ['已停止 · 恢复需核查', 'unknown'];
    if (job.outcome === 'failed') return [`执行失败${Number.isInteger(job.exit) ? ` · exit ${job.exit}` : ''}`, 'failed'];
    if (job.outcome === 'succeeded') return [`命令执行完成${Number.isInteger(job.exit) ? ` · exit ${job.exit}` : ''}`, 'finished'];
    if (job.lost) return ['退出状态丢失', 'unknown'];
    if (job.state === 'running') return ['运行中', 'running'];
    if (job.exit === 0) return ['已结束 · exit 0', 'finished'];
    if (Number.isInteger(job.exit)) return [`执行失败 · exit ${job.exit}`, 'failed'];
    return ['退出码未知', 'unknown'];
  }
  function jobsPath() {
    return historySource ? `${API}/job-sources/${encodeURIComponent(historySource.source_id)}/jobs` : `${API}/jobs`;
  }
  function sourceParams(params = {}) {
    const query = new URLSearchParams(params);
    if (historySource) query.set('source_revision', historySource.revision);
    return query;
  }
  function jobURL(suffix = '', params = {}) {
    const query = sourceParams(params);
    return `${jobsPath()}/${encodeURIComponent(selected)}${suffix}${query.size ? '?' + query : ''}`;
  }
  function sourceValue(source = historySource) { return source ? JSON.stringify([source.source_id, source.revision]) : ''; }
  function updateJobHash() {
    const hash = new URLSearchParams();
    if (historySource) { hash.set('source', historySource.source_id); hash.set('source_revision', historySource.revision); }
    if (selected) hash.set('job', selected);
    history.replaceState(null, '', `${location.pathname}${location.search}${hash.size ? '#' + hash : ''}`);
  }
  function historyProblem() {
    if (!historySource) return '';
    if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(historySource.source_id || '')) return '历史来源 ID 无效；保留链接，请显式重新选择。';
    if (!Number.isSafeInteger(historySource.revision) || historySource.revision < 1) return '历史链接缺少有效 source_revision；保留原来源，请显式重新选择。';
    return nodeInfo?.capabilities?.includes('jobs.history-read-v1') ? '' : '当前节点未提供 jobs.history-read-v1；历史来源保留，未退回当前作业。';
  }
  function checkHistoryIdentity(value) {
    if (historySource && (value.node_id !== nodeInfo?.node_id || value.source_id !== historySource.source_id || value.source_revision !== historySource.revision)) throw new Error('历史回包与所选节点、来源或 revision 不一致。');
    return value;
  }
  function renderJobSource() {
    const select = $('job-source'), saved = sourceValue();
    select.replaceChildren(el('option', '当前作业'));
    select.firstElementChild.value = '';
    for (const source of jobSources) {
      const option = el('option', `${source.label || source.source_id} · r${source.revision}${source.availability === 'available' ? '' : ' · 不可读取'}`);
      option.value = sourceValue(source); select.append(option);
    }
    const source = jobSources.find(item => sourceValue(item) === saved);
    if (historySource && !source) {
      const option = el('option', `${historySource.source_id} · r${historySource.revision ?? '未指定'} · 原选择`);
      option.value = saved; select.append(option);
    }
    select.value = saved;
    $('state').disabled = historical();
    $('live-job-detail').hidden = historical(); $('historical-detail').hidden = !historical();
    $('job-events').hidden = historical(); $('directory-export-controls').hidden = historical();
    $('artifact-input-help').hidden = historical(); $('historical-file-help').hidden = !historical();
    message('job-source-message', jobSourcesError || historyProblem() || (historySource
      ? `只读历史：${historySource.source_id} · revision ${historySource.revision}。${source?.reason || '上方队列与提交仍属于当前作业。'}`
      : '当前作业；上方队列与提交始终属于当前节点的当前作业。'), !!jobSourcesError || !!historyProblem() || source?.availability === 'unavailable');
    $('job-source-root').textContent = source?.jobs_root ? `登记根目录：${source.jobs_root}` : historySource ? '原来源根路径尚未核对；不会自动选择新 revision。' : '';
  }
  async function loadJobSources() {
    const revision = ++jobSourcesRevision;
    busy('refresh-job-sources', true, '读取中…');
    try {
      if (!nodeInfo?.capabilities?.includes('jobs.history-read-v1')) { jobSources = []; jobSourcesError = '节点未提供历史来源能力；当前作业仍可读取。'; return; }
      const result = await json(`${API}/job-sources`);
      if (revision !== jobSourcesRevision) return;
      if (result.schema !== 'hgamey/job-sources@1' || result.node_id !== nodeInfo.node_id || !Array.isArray(result.items)) throw new Error('历史来源目录身份或格式不正确。');
      jobSources = result.items; jobSourcesError = '';
    } catch (error) { if (revision === jobSourcesRevision) jobSourcesError = errorText(error); }
    finally { if (revision === jobSourcesRevision) { busy('refresh-job-sources', false); renderJobSource(); } }
  }
  function changeJobSource(source) {
    historySource = source; selected = ''; currentJob = null; directory = ''; jobsCursor = filesCursor = null;
    ++detailRevision; ++listRevision; ++artifactRevision; ++metadataRevision; ++logRevision;
    resetSession(); resetJobInputs(); resetJobLeases(); resetInputReference(); resetDirectoryExport(); resetLog();
    stopJobEvents(); syncJobEventScope();
    $('task-list').replaceChildren(); $('artifact-list').replaceChildren();
    $('detail-content').hidden = true; $('detail-placeholder').hidden = false; $('refresh-detail').hidden = true;
    regionBusy('task-detail', false); message('detail-message', '选择一个任务，查看此来源的记录和文件。');
    activeFilters = {query: $('query').value.trim(), state: historical() ? '' : $('state').value};
    renderJobSource(); updateJobHash();
  }
  function selectCurrentJob(id, keepDirectory = false) {
    if (historical()) { changeJobSource(null); loadJobs(); }
    return selectJob(id, keepDirectory);
  }
  function errorText(error) {
    if (error.payload?.code?.startsWith('history_')) return `${error.payload.code}：${error.payload.error?.message || error.payload.error || error.message}；原来源与 revision 保留，请核对后显式重选。`;
    return error.name === 'AbortError' ? '请求超时，请重试。' : (error.message || '网络错误，请重试。');
  }
  async function request(url, consume, target = undefined, options = {}) {
    const controller = options.controller || new AbortController();
    const timeout = setTimeout(() => controller.abort(), options.timeoutMs || 15000);
    try {
      const response = await HGTarget.fetch(url, {credentials: 'same-origin', cache: 'no-store', signal: controller.signal}, target === undefined ? {} : {target});
      if (response.status === 401 || response.status === 403 || (response.redirected && new URL(response.url).pathname === '/dash/login')) {
        const error = new Error('查看登录已失效，请重新登录。');
        error.status = response.status;
        throw error;
      }
      if (!response.ok) {
        const error = new Error(`读取失败（HTTP ${response.status}），可刷新重试。`); error.status = response.status;
        if ((response.headers.get('content-type') || '').includes('application/json')) {
          try { error.payload = await response.json(); } catch (_) { /* A malformed error body does not establish absence. */ }
        }
        throw error;
      }
      return await consume(response);
    } finally { clearTimeout(timeout); }
  }
  async function json(url, target = undefined, options = {}) {
    return request(url, async response => {
      if (!(response.headers.get('content-type') || '').includes('application/json')) throw new Error('服务返回了非 JSON 内容，请检查登录或节点连接。');
      return response.json();
    }, target, options);
  }
  async function post(url, body, headers = {}, target = undefined) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await HGTarget.fetch(url, {method: 'POST', credentials: 'same-origin', signal: controller.signal,
        headers: {'Content-Type': 'application/json', 'X-HG': '1', ...headers}, body: JSON.stringify(body)}, target === undefined ? {} : {target});
      if (response.redirected && new URL(response.url).pathname === '/dash/login') {
        const error = new Error('查看登录已失效，请重新登录。');
        error.status = 401;
        throw error;
      }
      const data = (response.headers.get('content-type') || '').includes('application/json') ? await response.json() : {};
      if (!response.ok) {
        const prefix = ({401: '查看登录已失效，请重新登录。', 403: '操作未获授权，请重新解锁任务操作。',
          409: '请求冲突，请核对已有记录。'})[response.status] || `请求失败（HTTP ${response.status}）。`;
        const error = new Error(`${prefix}${data.error || data.reason || ''}`);
        error.status = response.status; error.payload = data;
        throw error;
      }
      if (!(response.headers.get('content-type') || '').includes('application/json')) throw new Error('请求结果未确认，节点未返回 JSON。');
      return data;
    } finally { clearTimeout(timeout); }
  }
  function renderAccess() {
    $('access-toggle').textContent = accessBusy ? '读取权限…' : access?.steer ? '锁定任务操作' : '解锁任务操作';
    $('access-toggle').disabled = accessBusy || !access?.steer_configured;
    $('access-toggle').setAttribute('aria-busy', String(accessBusy));
    message('access-message', accessError || (accessBusy ? '正在核对操作权限…' : !access ? '权限尚未取得，任务操作保持只读。' :
      !access.steer_configured ? '本节点尚未配置掌舵密码，任务操作保持只读。' : access.steer ? '已解锁，可提交任务、停止请求和资源释放确认。' : '当前为只读模式，解锁后可提交任务操作。'), !!accessError);
    renderControls();
    renderSubmission();
    renderSessionControls();
    renderRuntimeRefresh();
  }
  async function loadAccess() {
    const revision = ++accessRevision;
    accessBusy = true;
    renderAccess();
    try {
      const result = await json(`${API}/access`);
      if (revision !== accessRevision) return;
      access = result;
      accessError = '';
      $('access-login').hidden = true;
    } catch (error) {
      if (revision !== accessRevision) return;
      access = null;
      accessError = errorText(error);
      $('access-login').hidden = error.status !== 401;
    } finally { if (revision === accessRevision) { accessBusy = false; renderAccess(); } }
  }
  function closeUnlock() { $('unlock-form').hidden = true; $('steer-password').value = ''; }
  async function setAccess(unlock) {
    if (accessBusy) return;
    ++accessRevision;
    accessBusy = true;
    renderAccess();
    busy('unlock-submit', true, '解锁中…');
    const body = unlock ? {pw: $('steer-password').value} : {};
    $('steer-password').value = '';
    message('unlock-message', '');
    try {
      await post(`${API}/steer/${unlock ? 'unlock' : 'lock'}`, body);
      closeUnlock();
      await loadAccess();
    } catch (error) {
      accessError = errorText(error);
      access = null;
      $('access-login').hidden = error.status !== 401;
      if (unlock) message('unlock-message', errorText(error), true);
    } finally { accessBusy = false; busy('unlock-submit', false); renderAccess(); }
  }
  function renderControls() {
    if (!currentJob || currentJob.id !== selected) return;
    const terminal = currentJob.state === 'finished' || ['finished', 'interrupted'].includes(currentJob.phase);
    const supported = currentJob.execution?.kind === 'supervised-v1';
    const pending = cancelling.has(currentJob.id);
    const saving = currentJob.phase === 'finalizing' || storageFailed(currentJob);
    const allowed = !!access?.steer && HGTarget.canWrite() && HGTarget.canWrite() && !accessBusy && !pending && !terminal && supported && !saving;
    const queued = ['preparing', 'queued'].includes(currentJob.phase);
    const stopping = currentJob.phase === 'cancelling' && currentJob.cancel?.status !== 'not_applied';
    $('cancel-job').disabled = !allowed || !currentJob.controls?.cancel_supported || stopping;
    $('force-open').disabled = !allowed || !currentJob.controls?.force_supported;
    $('force-submit').disabled = $('force-open').disabled || !$('force-confirm').checked;
    $('cancel-job').setAttribute('aria-busy', String(pending));
    $('cancel-job').textContent = pending ? '提交中…' : queued ? '撤销排队' : '请求停止';
    $('force-submit').setAttribute('aria-busy', String(pending));
    let reason = saving ? '结束记录尚未提交；请核对下方结果保存状态，停止请求不能代替保存。' : !supported ? '旧版任务仅支持查看，当前没有可核验的停止接口。' : terminal ? '任务执行已结束，无法继续发送停止请求。' :
      !access?.steer || !HGTarget.canWrite() || accessBusy ? '请先在上方解锁任务操作权限。' : stopping ? '停止请求已记录，等待任务处理；刷新任务可核对进度。' :
      !currentJob.controls?.cancel_supported ? '当前任务不支持请求停止。' : queued ? '撤销尚未执行的任务，不发送进程信号。请以最新任务状态确认撤销结果。' : '请求停止只发送 SIGINT，不会自动升级终止。接受请求不代表任务已停止或设备已恢复。';
    if (currentJob.time_budget) reason += ' 另有本任务的运行预算，到期动作见预算记录；人工请求不重置既有升级时钟。';
    if (currentJob.disk_guard) reason += ' 另有磁盘软保护；触发动作与最近保存观察见下方，已有停止意图和信号计时不会重置。';
    if (currentJob.controls?.reason) reason += ` ${currentJob.controls.reason}`;
    $('cancel-help').textContent = reason;
    const receipt = cancelReceipts.get(currentJob.id);
    $('cancel-result').hidden = !receipt;
    if (receipt) message('cancel-result', receipt.message, receipt.error);
    const blocked = currentJob.queue?.state === 'blocked';
    $('queue-release-panel').hidden = !blocked;
    const releaseBusy = releasing.has(currentJob.id);
    const eligibility = currentJob.queue?.release_eligibility;
    const releaseProblem = eligibility ? eligibility.eligible === true ? '' : eligibility.reason || '节点未确认此任务的释放资格。' : currentJob.device_binding ? '节点未返回此绑定任务的释放资格，请刷新任务；不会用全域摘要推断设备范围。' :
      ['write', 'recovery'].includes(currentJob.device_access) && (leaseSnapshot?.status !== 'clear' || leaseStorageBlocked(leaseSnapshot)) ? '兼容全域任务尚未满足放行条件：实际 lease 需 clear，存储也需就绪。' : '';
    const leaseBlocked = !!releaseProblem;
    const hostProblem = hostCompletionProblem(currentJob);
    $('recovery-prepare-panel').hidden = !blocked;
    $('prepare-recovery').disabled = !blocked || saving || !access?.steer || !HGTarget.canWrite() || accessBusy || !recoverySupported() || !!pendingRequest || submissionBusy;
    message('recovery-prepare-help', saving ? '结束记录尚未提交，暂不能以此任务创建恢复任务。请先处理存储问题并重试保存结果。' : !recoverySupported() ? '此节点未提供恢复任务能力。' : pendingRequest ? '先找回或确认上方未决提交，再准备恢复任务。' : !access?.steer || !HGTarget.canWrite() || accessBusy ? '请先解锁任务操作权限。' : '只预填恢复目标和模式；请由人或 Agent 填写实际恢复命令，执行后仍需核查并解除资源保留。');
    $('queue-release-note').disabled = releaseBusy;
    $('queue-release-submit').disabled = !blocked || !access?.steer || !HGTarget.canWrite() || accessBusy || releaseBusy || saving || !!hostProblem || leaseBlocked || !$('queue-release-note').value.trim();
    $('queue-release-submit').setAttribute('aria-busy', String(releaseBusy));
    $('queue-release-submit').textContent = releaseBusy ? '提交中…' : '确认解除资源保留';
    message('queue-release-help', saving ? '结束记录尚未保存，不能通过解除资源保留绕过。请先处理存储问题并重试保存结果。' : !access?.steer || !HGTarget.canWrite() || accessBusy ? '请先解锁任务操作权限。' : hostProblem || (releaseProblem ? `${releaseProblem} 请核查后刷新任务；填写说明不能绕过屏障。` : '节点已返回当前任务的释放资格，提交时仍会重新核查。请填写说明；解除只影响此任务保留的资源。'));
    const released = releaseReceipts.get(currentJob.id);
    $('queue-release-result').hidden = !released;
    if (released) message('queue-release-result', released.message, released.error);
    renderFinalization(currentJob);
    renderReconciliation(currentJob);
  }
  function storageFailed(job) { return job.phase === 'storage_blocked' || job.queue?.state_reason?.kind === 'durability'; }
  function hostCompletionProblem(job, forSessionRecovery = false) {
    const execution = job.execution || {};
    const contained = job.execution_policy === 'contained' || execution.process_management?.policy === 'contained';
    if (!contained && !forSessionRecovery) return '';
    if ((forSessionRecovery && execution.kind !== 'supervised-v1') || execution.worker_alive !== false) return '原 worker 尚未确认结束，请先核查执行域；不能据此恢复会话或解除资源保留。';
    if (!contained) return job.phase === 'finished' && !job.lost && execution.owned_process_group?.status === 'absent' ? '' : '原进程组结束尚未确认，不能提交会话恢复任务。';
    const group = execution.owned_cgroup, reconciliation = execution.reconciliation;
    if (!execution.containment) {
      const noGroup = !group || Object.keys(group).length === 0;
      if (noGroup && !execution.process_management && reconciliation?.state === 'complete' && reconciliation.basis === 'unstarted_empty_domain') return '';
      if (job.phase === 'finished' && !job.lost && (group?.status === 'absent' || (noGroup && execution.process_management?.policy !== 'contained' && job.recovery?.status === 'not_needed'))) return '';
      return '执行域结束与输出交还尚未确认，请先核查执行域。';
    }
    if (group?.status !== 'absent') return '执行域尚未确认为空，请先核查执行域。';
    if (execution.containment.handoff?.status !== 'returned') return '执行域输出尚未确认交还，请先核查执行域。';
    if ((reconciliation || job.lost || job.phase === 'interrupted') && reconciliation?.state !== 'complete') return '执行域核查尚未完成，请先读取核查状态。';
    return '';
  }
  function reconciliationProblem(job) {
    if (!nodeInfo?.capabilities?.includes('jobs.execution-reconcile-v1')) return '此节点未提供 jobs.execution-reconcile-v1，执行域核查入口不可用。';
    if (storageFailed(job) || job.phase === 'finalizing') return '原任务结束记录尚未确认保存，请先处理结果保存。';
    if (['requested', 'running'].includes(job.execution?.reconciliation?.state)) return '核查请求仍在处理中，请读取原任务状态；不会重复提交。';
    if (job.state !== 'finished' || job.queue?.state !== 'blocked') return '原任务须已结束并保留队列资源，才能核查执行域。';
    if (job.execution_policy !== 'contained' && job.execution?.process_management?.policy !== 'contained') return '此任务未声明或实际使用 contained，不能按约束域核查。';
    if (job.execution?.worker_alive !== false) return '原 worker 尚未确认退出，不能接管核查执行域。';
    return '';
  }
  function renderReconciliation(job) {
    const record = job.execution?.reconciliation, handoff = job.execution?.containment?.handoff;
    const receipt = reconcileReceipts.get(job.id), pending = reconciling.has(job.id);
    const supported = nodeInfo?.capabilities?.includes('jobs.execution-reconcile-v1'), problem = reconciliationProblem(job);
    $('execution-reconcile-panel').hidden = !supported && !record && !handoff && !receipt;
    const disabled = !!problem || !access?.steer || !HGTarget.canWrite() || accessBusy || pending || !!receipt?.unconfirmed;
    $('execution-reconcile-check').disabled = disabled;
    $('execution-reconcile-force-open').disabled = disabled;
    $('execution-reconcile-force-submit').disabled = disabled || !$('execution-reconcile-force-confirm').checked;
    for (const id of ['execution-reconcile-check', 'execution-reconcile-force-submit']) $(id).setAttribute('aria-busy', String(pending));
    $('execution-reconcile-check').textContent = pending ? '正在提交核查…' : '核查执行域';
    $('execution-reconcile-refresh').disabled = pending;
    message('execution-reconcile-help', pending ? '只提交一次，接受请求不代表宿主收尾完成。' : receipt?.unconfirmed ? '上次请求结果未确认。请先读取原任务状态；不会自动重发核查或强制操作。' : problem || (!access?.steer || !HGTarget.canWrite() || accessBusy ? '请先解锁任务操作权限。' : '普通核查不杀进程；仅检查当前绑定域，域空后交还输出。强制操作需单独确认。'));
    facts('execution-reconcile-info', [['宿主核查状态', ({requested: '已请求，尚未完成', running: '核查中', blocked: '仍被阻断', complete: '宿主核查已完成', failed: '核查失败', unknown: '核查结果未知'})[record?.state] || '未提供'],
      ['强制结束选择', typeof record?.force === 'boolean' ? record.force ? '是 · 可结束域内残留进程' : '否 · 不杀进程' : '未提供'],
      ['请求时间', time(record?.requested_at)], ['结束时间', time(record?.finished_at)], ['核查错误', record?.error || '未报告'],
      ['输出交还', ({pending: '待交还', returned: '已交还'})[handoff?.status] || '未提供'], ['交还记录时间', time(handoff?.at)]]);
    $('execution-reconcile-result').hidden = !receipt;
    if (receipt) message('execution-reconcile-result', receipt.message, receipt.error);
  }
  async function reconcileExecution(force) {
    if (!currentJob || $(force ? 'execution-reconcile-force-submit' : 'execution-reconcile-check').disabled) return;
    const id = currentJob.id, revision = detailRevision;
    reconciling.add(id);
    $('execution-reconcile-force-form').hidden = true;
    $('execution-reconcile-force-confirm').checked = false;
    renderControls();
    try {
      const result = await post(`${API}/jobs/${id}/execution/reconcile`, {force});
      if (result.accepted !== true || result.job?.id !== id) throw new Error('核查回包未确认原任务，结果未知。');
      reconcileReceipts.set(id, {message: `${force ? '强制结束并交还' : '执行域核查'}请求已接受，请读取原任务确认进度。宿主核查不代表设备恢复或资源释放。`, error: false});
      if (selected === id && detailRevision === revision) {
        currentJob = result.job;
        const [label, kind] = status(currentJob);
        $('job-state').textContent = label; $('job-state').className = `task-state ${kind}`;
        $('detail-updated').textContent = readTime();
        renderExecution(currentJob);
      }
    } catch (error) {
      const uncertain = !error.status || error.status >= 500;
      reconcileReceipts.set(id, {message: `${errorText(error)} ${uncertain ? '结果未确认，只能先读取原任务状态核对；不会自动重发。' : '请求未被接受，请核对原任务与节点条件。'}`, error: true, unconfirmed: uncertain});
      if (error.status === 401 || error.status === 403) await loadAccess();
    } finally { reconciling.delete(id); if (selected === id && currentJob?.id === id) renderControls(); }
  }
  function renderFinalization(job) {
    const failed = storageFailed(job), saving = job.phase === 'finalizing';
    const receipt = finalizeReceipts.get(job.id), pending = finalizing.has(job.id);
    const supported = nodeInfo?.capabilities?.includes('jobs.durability-v1');
    $('finalize-panel').hidden = !failed && !saving && !receipt;
    $('retry-finalize').disabled = !failed || !supported || !access?.steer || !HGTarget.canWrite() || accessBusy || pending;
    $('retry-finalize').setAttribute('aria-busy', String(pending));
    $('retry-finalize').textContent = pending ? '正在重试保存…' : '重试保存结果';
    message('finalize-help', pending ? '正在同步原有结果记录，不会重新执行命令。' : saving ? '命令已返回结束记录，正在确认保存；当前仍未确认作业完成。' : failed ?
      (!supported ? '此节点未提供 jobs.durability-v1，无法从本页重试保存；请升级节点。' : !access?.steer || !HGTarget.canWrite() || accessBusy ? '记录保存失败，资源继续保留。请先解锁任务操作权限，再重试保存。' : '记录保存失败，资源继续保留。修复存储问题后可重试；只保存原有结果，不重新执行命令。') : '保存回执仅说明存储提交情况；设备恢复与资源释放请另行核对。', failed);
    const reason = job.queue?.state_reason;
    facts('finalize-info', [['保存阶段', reason?.stage], ['失败原因', reason?.message], ['记录时间', time(reason?.at)], ...(reason?.unstarted != null ? [['未执行依据（节点记录）', structuredText(reason.unstarted)]] : []), ['原命令退出码（诊断）', job.exit], ['执行返回码（诊断）', job.execution?.returncode]]);
    $('finalize-result').hidden = !receipt;
    if (receipt) message('finalize-result', receipt.message, receipt.error);
  }
  async function retryFinalize() {
    if (!currentJob || $('retry-finalize').disabled) return;
    const id = currentJob.id, revision = detailRevision;
    finalizing.add(id);
    renderControls();
    try {
      const result = await post(`${API}/jobs/${encodeURIComponent(id)}/retry-finalize`, {});
      finalizeReceipts.set(id, {message: result.storage_committed === true ? '结束记录的存储提交已确认；这不代表设备恢复完成，任务仍可能保留资源。' : `结束记录仍未保存，资源继续保留。${result.job?.queue?.state_reason?.message || '请核对最新存储错误后再重试。'}`, error: result.storage_committed !== true});
    } catch (error) {
      finalizeReceipts.set(id, {message: !error.status || error.status >= 500 ? `保存结果未确认，请刷新任务核对；不会自动重试。${errorText(error)}` : errorText(error), error: true});
      if (error.status === 401 || error.status === 403) await loadAccess();
    } finally {
      finalizing.delete(id);
      await Promise.all([loadQueue(), selected === id && detailRevision === revision ? selectJob(id, true) : null]);
    }
  }
  function budgetFacts(job) {
    const request = job.time_budget, record = job.execution?.time_budget;
    const action = value => ({graceful: '只请求 SIGINT', escalate: 'SIGINT 后 15 秒 TERM，再 15 秒 KILL'})[value] || text(value);
    const label = ({not_started: '尚未启动计时', running: '最近观察为运行中', exceeded: '已触发预算停止请求', finished: '未触发预算停止即观察到结束'})[record?.status] || 'worker 预算观察未提供';
    $('time-budget-summary').hidden = !request && !record;
    message('time-budget-summary', record ? `${label}。最近观察：${time(record.observed_at)}。${job.lost || job.execution?.worker_alive === false && job.state !== 'finished' ? 'worker 已丢失或不再存活，仅保留最后观察。' : ''}不按本页时钟推断到期、发信号或恢复。` : request ? '已声明运行预算，尚未取得 worker 计时观察；不从提交时间计算。' : '', budgetExceeded(job));
    return [
      ['声明运行预算', request ? `${request.wall_seconds} 秒 · ${action(request.timeout_action)}` : '未声明或旧记录未提供，不回填预算'],
      ['worker 预算状态', label], ...(record ? [
        ['worker 实际启动', time(record.started_at)], ['预算期限参考时刻', time(record.deadline_at)],
        ['最近预算观察', time(record.observed_at)], ['已观察运行时长', Number.isFinite(record.elapsed_seconds) ? `${record.elapsed_seconds} 秒` : '未观察'],
        ['预算触发时间', time(record.exceeded_at)], ['预算结束观察', time(record.finished_at)],
        ['预算边界', '期限触发不代表停止完成；未触发即结束也不由轮询时刻证明精确在限时内。']
      ] : [])
    ];
  }
  function stopIntentFacts(job) {
    const intent = job.execution?.stop_intent;
    $('stop-intent-summary').hidden = !intent;
    message('stop-intent-summary', intent ? '以下是 worker 最后成功保存的停止意图与记录错误历史，可能落后于实际执行。实际已发信号仍看信号记录；停止意图不证明任务已停或设备已恢复，错误历史不表示当前磁盘剩余量。' : '', Boolean(intent?.record_errors));
    if (!intent) return [];
    const causes = {execution_deadline: '运行预算期限', owner_record_error: 'worker 管理记录读写失败', disk_log_limit: '日志长度达到软阈值', disk_space_low: '文件系统可用量低于软阈值', disk_observation_failed: '磁盘观察失败'};
    const errors = intent.record_errors;
    return [
      ['已保存的停止原因', (intent.causes || []).map(cause => causes[cause] || cause).join('、') || '未新增停止原因；沿用已知取消意图'],
      ['停止意图请求时间', time(intent.requested_at)],
      ['已保存的停止模式', ({graceful: '柔性停止（SIGINT）', escalate: '允许按原信号链升级终止'})[intent.mode] || text(intent.mode)],
      ...(errors ? [['记录错误次数（已保存）', errors.count], ['首次记录错误', time(errors.first_at)],
        ['最近记录错误', time(errors.last_at)], ['记录错误 errno', errors.errno], ['记录错误说明', errors.message]] : [['记录错误历史', '本次保存的意图未包含错误历史']])
    ];
  }
  function diskGuardFacts(job) {
    const request = job.disk_guard, record = job.execution?.disk_guard, observation = record?.observation;
    const action = value => ({graceful: '柔性停止（只请求 SIGINT）', escalate: 'SIGINT 后 15 秒 TERM，再 15 秒 KILL'})[value] || text(value);
    const label = ({not_started: '尚未启动', running: '最近保存为运行中', triggered: '已触发停止请求', rejected: '启动前检查拒绝放行', finished: '已保存结束观察'})[record?.status] || 'worker 磁盘观察未提供';
    const reason = ({log_limit: '日志长度达到上限', disk_space_low: '文件系统可用量低于下限', observation_failed: '无法取得磁盘观察'})[record?.trigger_reason] || record?.trigger_reason || '未记录触发或拒绝原因';
    $('disk-guard-summary').hidden = !request && !record;
    message('disk-guard-summary', record ? `${label}。最近成功保存的观察：${time(record.observed_at)}；并非实时磁盘状态。${record.status === 'rejected' ? '启动前未放行，不据此补出命令退出码。' : '触发仅表示请求停止，实际退出、信号与设备恢复分别核对。'}` : request ? '已声明磁盘软保护，尚未取得 worker 观察；不能推断已开始检查或当前空间充足。' : '', record?.status === 'triggered' || record?.status === 'rejected' || observation?.status === 'unknown');
    if (!request && !record) return [];
    return [
      ['声明日志长度上限', request ? inputBytes(request.max_log_bytes) : '未提供，不从 worker 记录回填请求'],
      ['声明最低可用量', request ? inputBytes(request.min_free_bytes) : '未提供'], ['声明磁盘触发动作', request ? action(request.stop_action) : '未提供'],
      ['worker 磁盘保护状态', label], ...(record ? [
        ['worker 采用的日志上限', inputBytes(record.max_log_bytes)], ['worker 采用的可用量下限', inputBytes(record.min_free_bytes)], ['worker 磁盘触发动作', action(record.stop_action)],
        ['磁盘保护边界', record.enforcement === 'observed_stop_request' ? '观察后请求停止；非硬配额，阈值触发与信号处理期间仍可能写入。' : text(record.enforcement)],
        ['磁盘检查启动', time(record.started_at)], ['已保存观察的采样时间', time(record.observed_at)], ['磁盘触发时间', time(record.triggered_at)],
        ['磁盘触发 / 拒绝原因', reason], ['磁盘结束观察', time(record.finished_at)],
        ['磁盘观察完整性', ({known: '本次观察已取得', unknown: '本次观察未知；已读到的部分数值仍保留'})[observation?.status] || '尚未取得观察'],
        ['日志逻辑长度（已观察）', inputBytes(observation?.log_bytes)],
        ...(observation?.filesystems?.length ? observation.filesystems.map(fs => [`文件系统 ${text(fs.device)} · ${(fs.roles || []).join(' / ') || '角色未提供'}`, `${inputBytes(fs.available_bytes)} 可用（f_bavail；同一文件系统的角色已合并）`]) : [['文件系统可用量', '未提供，不按零或当前节点空间补值']]),
        ['磁盘观察错误代码', observation?.error?.code || '未报告'], ['磁盘观察错误', observation?.error?.message || '未报告'],
        ['空间范围', '共享文件系统余量不能归因于此任务；日志逻辑长度不是磁盘实际占用。不覆盖其他挂载、每用户配额或脱组后代。']
      ] : [])
    ];
  }
  function renderExecution(job) {
    const cancellation = job.cancel;
    const group = job.execution?.owned_process_group;
    const groupFacts = group ? [
      ['进程组核查范围', group.scope === 'original_process_group' ? '原进程组（original_process_group）' : text(group.scope)],
      ['原进程组状态', ({not_started: '未启动', pending: '等待收尾核查', absent: '核查时未发现原进程组', present: '原进程组仍存在', unknown: '无法确认原进程组是否结束'})[group.status] || '未识别的状态'],
      ['原进程组 PGID（诊断）', group.pgid], ['进程组核查时间', time(group.checked_at)],
      ['进程组核查错误', group.error || '未报告错误'],
      ['核查边界', '只核查原进程组；不涵盖通过 setsid 脱组的后代，也不证明设备已恢复。']
    ] : [['原进程组核查', '旧记录未提供，不能据此确认进程组已结束。']];
    const management = job.execution?.process_management;
    facts('execution-info', [['执行阶段', PHASES[job.phase] || '旧版记录未提供'],
      ['请求的进程策略', job.execution_policy || '旧记录未提供'],
      ['策略来源', ({explicit: '显式选择', legacy_default: '兼容默认'})[job.execution_policy_source] || '旧记录未提供'],
      ['实际执行策略（worker）', management?.policy || '未提供，不能由请求策略推断'], ['实际进程后端（worker）', management?.backend || '未提供'],
      ['实际完成范围（worker）', management?.completion_scope === 'original_process_group' ? '原进程组（original_process_group）' : management?.completion_scope || '未提供'],
      ['脱组后代', management?.detached_descendants === 'unverified' ? '未验证；不能据原进程组结束判断整棵进程树已结束。' : management?.detached_descendants || '未提供'],
      ['设备访问', ({none: '宿主任务，无设备访问', write: '设备写入', recovery: '恢复任务'})[job.device_access] || '旧记录未提供'], ['声明设备范围', scopeText(job.queue?.device_scope || {scope: job.device_binding ? 'device' : job.device_access === 'none' ? 'none' : 'global', binding: job.device_binding})],
      ...(job.device_binding ? bindingFacts(job.device_binding) : []), ['恢复目标', job.recovery_for || job.queue?.recovery_for || '无'],
      ['调度状态', ({preparing: '准备中', queued: '排队中', claimed: '已分配执行', blocked: '保留资源，待核查', done: '调度已结束'})[job.queue?.state] || '未记录'], ['声明资源', job.queue?.resources?.join('、') || '无'], ['等待原因', waitingText(job.queue?.waiting)],
      ['命令退出码', job.exit], ...jobCapacityFacts(job), ...budgetFacts(job), ...diskGuardFacts(job), ...stopIntentFacts(job), ...groupFacts, ['请求时间', time(cancellation?.requested_at)],
      ['首次停止来源', cancellation?.source === 'execution_deadline' ? '运行预算期限' : cancellation?.source || '未记录（可能为人工请求）'],
      ['升级来源', cancellation?.escalation_source === 'execution_deadline' ? '运行预算期限' : cancellation?.escalation_source || '未记录'],
      ['停止方式', ({graceful: '柔性停止（SIGINT）', escalate: '升级终止'})[cancellation?.mode] || '未记录'],
      ['请求处理', ({requested: '已记录，等待处理', signaled: '已发送信号', not_applied: '未应用'})[cancellation?.status] || '未记录'],
      ['恢复状态', ({not_reported: '尚未上报', required: '需要核查与恢复', not_needed: '任务报告无需恢复'})[job.recovery?.status] || '尚未上报'],
      ['恢复说明', job.recovery?.reason || '停止结果及退出码不能证明设备已恢复。']]);
    $('cancel-signals').replaceChildren(...(cancellation?.signals || []).map(signal => el('li', `${text(signal.name)} · ${time(signal.at)}`)));
    renderControls();
  }
  async function cancelJob(force) {
    const job = currentJob;
    if (!job || (force ? $('force-submit').disabled : $('cancel-job').disabled)) return;
    const id = job.id;
    cancelling.add(id);
    $('force-form').hidden = true;
    $('force-confirm').checked = false;
    renderControls();
    try {
      const result = await post(`${API}/jobs/${encodeURIComponent(id)}/cancel`, {force});
      cancelReceipts.set(id, {message: result.accepted ? `${force ? '升级终止' : ['preparing', 'queued'].includes(job.phase) ? '撤销排队' : '停止'}请求已记录，请以下方最新执行状态与恢复记录为准。` : `请求未接受：${result.reason || '请核对最新任务状态。'}`, error: !result.accepted});
    } catch (error) {
      const uncertain = !error.status;
      cancelReceipts.set(id, {message: uncertain ? '请求结果未确认，可能已被节点记录。请刷新任务核对后再决定是否重试。' : errorText(error), error: true});
      if (error.status === 401 || error.status === 403) await loadAccess();
    } finally {
      cancelling.delete(id);
      await Promise.all([loadQueue(), !historical() && selected === id ? selectJob(id, true) : null]);
    }
  }

  const SLOT_KEYS = ['cpu', 'gpu', 'io'];
  const slotName = key => ({cpu: 'CPU', gpu: 'GPU', io: 'I/O'})[key] || text(key);
  const slotsText = slots => slots ? SLOT_KEYS.map(key => `${slotName(key)} ${text(slots[key])}`).join(' / ') : '未提供';
  function shortageText(shortages) {
    return Array.isArray(shortages) ? shortages.map(item => `${slotName(item.resource)}：请求 ${text(item.requested)} / 总量 ${text(item.total)} / 保留 ${text(item.reserved)} / 可用 ${text(item.available)}`).join('；') : '';
  }
  function hostCapacityFacts(capacity) {
    return capacity ? [['宿主 slots 配置', slotsText(capacity.slots)],
      ['配置来源', ({initial_default: '首次兼容配置', file: '管理员配置文件'})[capacity.source] || text(capacity.source)],
      ['配置更新时间', time(capacity.updated_at)], ['容量依据', capacity.basis === 'declared_scheduler_slots' ? '声明调度 slots；不是硬件数量或 OS 限额' : '未提供声明调度依据']] : [['宿主 slots 配置', '未提供；不从硬件或作业列表推算']];
  }
  function renderHostCapacity(capacity, problem = '') {
    message('host-capacity-message', problem || (!capacity ? '节点未提供宿主容量记账，不能按缺值推断为 0。' : '下方为节点读取时的持久调度记账；不由网页重算。与上方作业执行名额分别约束。'), !!problem);
    facts('host-capacity-info', [...hostCapacityFacts(capacity), ...(capacity ? SLOT_KEYS.map(key => [slotName(key),
      `总量 ${text(capacity.slots?.[key])} · 保留 ${text(capacity.reserved?.[key])} · 可用 ${text(capacity.available?.[key])} · 超额 ${text(capacity.overcommitted?.[key])}`]) : [])]);
  }
  const hostReservationState = state => ({not_claimed: '尚未占位', reserved: '仍保留宿主 slots', released: '已记录宿主 slots 释放', historical_done: '旧完成记录，不构成宿主结束证明'})[state] || '未提供';
  function jobCapacityFacts(job) {
    const record = job.queue?.host_capacity;
    return [['原宿主 slots 声明', job.host_slots ? slotsText(job.host_slots) : '未显式声明'],
      ['本任务记账需求', record ? `${slotsText(record.slots)} · ${record.source === 'explicit' ? '显式声明' : record.source === 'legacy_default' ? '固定兼容需求' : '来源未知'}` : '节点未提供；不从当前配置回填'],
      ['宿主 slots 状态', hostReservationState(record?.state)],
      ...(record?.release ? [['宿主 slots 释放时间', time(record.release.at)], ['宿主释放依据', record.release.basis]] : []),
      ['调度边界', '宿主 slots 释放不代表设备恢复或独占资源保留已解除。']];
  }
  function waitingText(waiting) {
    const reason = waiting?.reason;
    const capacityReason = ({max_running: '等待作业执行名额', slots_unavailable: '等待宿主 slots', request_exceeds_current_total: '原请求超过当前宿主容量'})[reason];
    const label = waiting?.kind === 'capacity' ? capacityReason || '等待执行名额' : ({resource: '等待独占资源', preparing: '正在准备任务', recovery: '等待核查并解除资源保留', durability: '等待保存结束记录', device_lease: '等待实际设备 lease'})[waiting?.kind] || '无等待原因';
    const shortage = shortageText(waiting?.shortages);
    return `${label}${reason && !capacityReason ? `：${reason}` : ''}${shortage ? `；${shortage}` : ''}${waiting?.lease_status ? `（${waiting.lease_status}）` : ''}${waiting?.scope ? ` · ${scopeText(waiting.scope)}` : ''}`;
  }
  function scopeText(value) {
    const scope = typeof value === 'string' ? value : value?.scope;
    return scope === 'device' ? `已登记设备 ${value?.binding?.device_id || 'ID 未提供'}${value?.binding?.serial ? ` · ${value.binding.serial}` : ''}` : scope === 'global' ? '兼容全域（未声明设备绑定）' : scope === 'none' ? '宿主任务 · 无设备范围' : '设备范围未提供或未知';
  }
  function openJobButton(id, label = '查看任务') {
    const button = el('button', label, 'ghost');
    button.type = 'button';
    button.addEventListener('click', () => { selectCurrentJob(id); $('detail-heading').focus(); });
    return button;
  }
  function recoverySupported() { return !!nodeInfo?.capabilities?.includes('jobs.recovery-v1'); }
  function leaseStorageBlocked(lease) { return !!lease?.storage && lease.storage.readiness !== 'ready'; }
  function renderLeaseStorage(storage) {
    const states = {ready: ['存储已就绪', 'finished'], migration_required: ['需要协调迁移', 'unknown'], unavailable: ['存储不可用', 'unknown']};
    const state = states[storage?.readiness] || ['未报告存储状态', 'unknown'];
    $('lease-storage-state').textContent = state[0];
    $('lease-storage-state').className = `task-state ${state[1]}`;
    $('lease-storage-mode').textContent = storage ? `模式：${({default: '默认（default）', custom: '自定义（custom）'})[storage.mode] || text(storage.mode)}` : '';
    const description = !storage ? '节点未提供存储位置与准备状态，持久根尚未确认。' : storage.readiness === 'migration_required' ? '设备写入暂不可调度。迁移需先协调停止旧 worker；此页面不会初始化或迁移目录。' : storage.readiness === 'unavailable' ? '设备写入暂不可调度。请先处理存储错误，再刷新状态。' : '存储就绪只表示记录目录可用；设备占用与恢复仍以实际 lease 状态为准。';
    message('lease-storage-message', `${description}${storage ? ' DEVLOCK_DIR 对应锁域根，与队列目录分别记录；布局版本不等于并发资格，实际范围以冻结绑定和 lease 记录为准。队列目录不能作为新的执行锁域。' : ''}${storage?.error ? ` 错误：${storage.error}` : ''}`, !!storage?.error || (!!storage && storage.readiness !== 'ready'));
    $('lease-storage-details').hidden = !storage;
    const layout = storage?.layout_version === 1 ? '1 · 旧全域布局' : storage?.layout_version === 2 ? '2 · 版本化单根布局' : '未报告或未知；不从目录名称推断';
    facts('lease-storage-paths', storage ? [['状态根', storage.state_dir], ['锁域根', storage.devlock_dir],
      ['当前全局队列目录', storage.queue_dir || '未报告；不从锁域根推导路径'], ['布局版本', layout],
      ['锁域 ID（domain_id）', storage.domain_id || (storage.layout_version === 1 ? '未登记（旧布局）' : '未报告')], ['设备 lease 根', storage.device_lease_dir]] : []);
  }
  function hostProcessFacts(entry) {
    if (entry.role !== 'device_lease') return [];
    const host = entry.host_process;
    const status = !host ? '未提供' : ({alive: '同一宿主实例存活', previous_boot: '来自先前宿主启动', exited: '宿主 owner 已退出', zombie: '宿主 owner 已退出（僵尸态）', pid_reused: '宿主 PID 已被复用', legacy: '旧记录未记录宿主实例', unknown: '宿主实例无法确认'})[host.status] || '宿主实例无法确认';
    return [['宿主 owner 实例', status], ...(host ? [['宿主 PID', host.pid], ['宿主进程启动身份', host.start], ['宿主 boot ID', host.host_boot_id], ['宿主身份原因', host.reason]] : []),
      ['身份核查范围', '仅宿主 owner 实例；不证明原进程组已结束或设备已恢复。']];
  }
  function renderLease(lease, error = '') {
    leaseSnapshot = lease || null;
    const storageBlocked = leaseStorageBlocked(lease);
    const state = storageBlocked ? [lease.storage.readiness === 'migration_required' ? '等待存储迁移' : '存储不可用', 'unknown'] : ({clear: ['屏障已清除', 'finished'], held: ['设备占用', 'unknown'], blocked: ['需要恢复', 'unknown'], unknown: ['状态未知', 'unknown']})[lease?.status] || ['尚未取得状态', 'unknown'];
    $('lease-state').textContent = state[0];
    $('lease-state').className = `task-state ${state[1]}`;
    message('lease-message', error || (storageBlocked ? '存储尚未就绪，不能据此判断设备空闲或放行设备任务。' : !lease ? '此节点尚未返回实际 lease 记录，不能据此判断设备空闲。' : lease.error ? `读取异常：${lease.error}` : `兼容锁：${lease.bridge_held === true ? '占用' : lease.bridge_held === false ? '空闲' : '未知'}。实际 lease 与队列资源分别核查；资源保留不占用执行名额。${lease.device_scopes ? ' 此为聚合状态；单设备等待与释放资格以节点针对任务的判断为准。' : ''}`), !!error || !!lease?.error || storageBlocked);
    renderLeaseStorage(lease?.storage);
    $('lease-sampled').textContent = lease ? `采样于 ${time(lease.sampled_at)}` : '';
    $('lease-list').replaceChildren(...(lease?.entries || []).map(entry => {
      const item = el('li'), details = el('details');
      const role = ({outer_queue: '外层队列锁', device_lease: '设备租约'})[entry.role] || '角色未报告';
      const label = ({active: '活动记录', command_pending: '命令待处理', recovery_required: '需要恢复', abandoned: '已遗留', unknown: '状态未知'})[entry.state] || text(entry.state);
      details.append(el('summary', `${role}${entry.serial ? ` · ${entry.serial}` : ''} · ${label}`));
      const info = el('dl', null, 'facts');
      const holder = entry.holder_verified === true ? '已核验' : entry.holder_verified === false ? '核验未通过' : entry.role === 'device_lease' ? '不适用（设备租约）' : '未知';
      for (const [name, value] of [['标签（仅展示）', entry.label], ['实际 lease 范围', scopeText({scope: entry.lease_scope || entry.scope, binding: entry.device_binding})], ...(entry.device_binding ? bindingFacts(entry.device_binding) : []), ['实例 ID', entry.instance_id], ['关联外层实例', entry.outer_instance_id], ['外层持有者核验', holder], ...hostProcessFacts(entry), ['原因', entry.reason || '未记录'], ['记录位置', entry.directory]]) info.append(el('dt', name), el('dd', text(value)));
      details.append(info);
      if (entry.job_id) details.append(openJobButton(entry.job_id, `关联任务 ${entry.job_id}`));
      if (entry.recovery_context != null) details.append(el('pre', typeof entry.recovery_context === 'string' ? entry.recovery_context : JSON.stringify(entry.recovery_context, null, 2), 'lease-context'));
      item.append(details); return item;
    }));
    if (lease?.device_scopes) {
      const scopes = lease.device_scopes, item = el('li'), details = el('details'), info = el('dl', null, 'facts');
      const state = value => ({clear: '无屏障', held: '占用', blocked: '需要恢复', unknown: '未知'})[value] || '未提供';
      details.append(el('summary', '全域与各设备范围快照'));
      const rows = [['锁域 ID', scopes.domain_id], ['登记 revision', scopes.registry_revision], ['全域状态', state(scopes.global?.status)], ['全域原因', scopes.global?.error || '未报告'],
        ['全域排队', scopes.queue_waiting === true ? '有等待者' : scopes.queue_waiting === false ? '无等待者' : '未知'], ['不可归属记录', scopes.unknown === true ? '存在，保守阻断' : scopes.unknown === false ? '未报告' : '未知'],
        ...(scopes.devices || []).map(device => [`设备 ${device.device_id}`, `${state(device.status)}${device.error ? ` · ${device.error}` : ''}`])];
      info.append(...rows.flatMap(([name, value]) => [el('dt', name), el('dd', text(value))])); details.append(info); item.append(details); $('lease-list').prepend(item);
    }
    renderControls();
  }
  async function loadQueue() {
    const revision = ++queueRevision;
    regionBusy('queue-panel', true);
    busy('refresh-queue', true, '读取中…');
    $('queue-summary').hidden = true;
    $('queue-list').replaceChildren();
    $('queue-updated').textContent = '';
    message('queue-message', '正在读取队列…');
    renderLease(null);
    renderHostCapacity(null, '正在读取宿主调度容量…');
    message('lease-message', '正在读取实际设备 lease…');
    try {
      const queue = await json(`${API}/queue`);
      if (revision !== queueRevision) return;
      renderLease(queue.lease);
      renderHostCapacity(queue.host_capacity);
      $('queue-summary').replaceChildren(...[[queue.active_count, `已分配作业 / ${queue.max_running} 个名额`], [queue.queued_count, '排队'], [queue.blocked_count, '保留资源']].map(([value, label]) => {
        const part = el('span'); part.append(el('strong', text(value)), el('span', label)); return part;
      }));
      $('queue-summary').hidden = false;
      for (const job of queue.items) {
        const item = el('li', null, 'queue-item');
        const identity = el('div');
        identity.append(el('p', job.job_id, 'task-item-id'), el('span', job.state_reason?.kind === 'durability' ? PHASES.storage_blocked : PHASES[job.phase] || job.phase, `task-state ${job.phase === 'blocked' || job.state_reason?.kind === 'durability' ? 'unknown' : ''}`));
        const wait = el('div');
        wait.append(el('p', waitingText(job.waiting)), el('p', job.resources.length ? `独占：${job.resources.join('、')}` : '未声明独占资源', 'queue-resource'));
        wait.append(el('p', scopeText(job.device_scope || {scope: 'global'}), 'note'));
        if (job.host_capacity) wait.append(el('p', `${slotsText(job.host_capacity.slots)} · ${hostReservationState(job.host_capacity.state)}`, 'note'));
        if (job.state_reason?.kind === 'durability') wait.append(el('p', `保存失败：${text(job.state_reason.stage)} · ${text(job.state_reason.message)}`, 'note'));
        if (job.waiting?.blocking_jobs?.length) {
          const blockers = el('div', null, 'queue-blockers');
          for (const id of job.waiting.blocking_jobs) blockers.append(openJobButton(id, `占用任务 ${id}`));
          wait.append(blockers);
        }
        if (job.recovery_for) wait.append(openJobButton(job.recovery_for, `恢复目标 ${job.recovery_for}`));
        item.append(identity, wait, openJobButton(job.job_id));
        $('queue-list').append(item);
      }
      message('queue-message', queue.dispatcher_error ? `调度异常：${queue.dispatcher_error}。队列记录保留，请核查调度服务后刷新。` : queue.items.length ? '按声明的资源调度；资源保留的任务需核查后释放。' : '当前队列为空。', !!queue.dispatcher_error);
      $('queue-updated').textContent = readTime();
    } catch (error) {
      if (revision === queueRevision) { message('queue-message', error.status === 404 ? '此节点尚未提供队列入口。已有任务记录仍可查看。' : errorText(error), true); renderLease(null, '实际设备 lease 未能取得，请刷新核对。'); renderHostCapacity(null, '宿主容量未能读取，不沿用旧余量判断可调度。'); }
    } finally {
      if (revision === queueRevision) { regionBusy('queue-panel', false); busy('refresh-queue', false); }
    }
  }
  function savePending() {
    try { if (pendingRequest) sessionStorage.setItem(REQUEST_STORAGE, JSON.stringify({id: pendingRequest.id, target: pendingRequest.target,
      ...(pendingRequest.binding_recorded ? {binding_recorded: true, device_binding: pendingRequest.device_binding} : {}),
      ...(pendingRequest.budget_recorded ? {budget_recorded: true, time_budget: pendingRequest.time_budget} : {}),
      ...(pendingRequest.disk_guard_recorded ? {disk_guard_recorded: true, disk_guard: pendingRequest.disk_guard} : {}),
      ...(pendingRequest.host_slots_recorded ? {host_slots_recorded: true, host_slots: pendingRequest.host_slots} : {})})); else sessionStorage.removeItem(REQUEST_STORAGE); }
    catch (_) { /* The visible request ID remains available if browser storage is unavailable. */ }
  }
  const hostSlotsSupported = () => nodeInfo?.capabilities?.includes('jobs.host-capacity-v1') === true;
  function submissionHostSlots() {
    if (pendingRequest) return pendingRequest.body?.host_slots ?? (pendingRequest.host_slots_recorded ? pendingRequest.host_slots : null);
    if (!$('submit-host-slots').checked) return null;
    return Object.fromEntries(SLOT_KEYS.map(key => [key, $('submit-host-' + key).value.trim() === '' ? NaN : Number($('submit-host-' + key).value)]));
  }
  function hostSlotsProblem() {
    const slots = submissionHostSlots();
    if (slots == null) return '';
    if (Object.keys(slots).sort().join(',') !== 'cpu,gpu,io' || !SLOT_KEYS.every(key => Number.isInteger(slots[key]) && slots[key] >= (key === 'cpu' ? 1 : 0) && slots[key] <= 65535)) return 'CPU 须为 1–65535，GPU/I/O 须为 0–65535 的整数 slots；三项都需填写。';
    if (!hostSlotsSupported()) return '节点未提供 jobs.host-capacity-v1，不能提交显式 slots；不会删除原声明降级。';
    if ((pendingRequest?.body?.device_access || $('submit-device-access').value) === 'recovery') return '恢复任务暂不接受显式宿主 slots，使用兼容需求而不继承原任务计算需求。';
    return '';
  }
  function renderHostSlots(allowed) {
    const fixed = !!pendingRequest, slots = submissionHostSlots();
    if (fixed) { $('submit-host-slots').checked = !!slots; for (const key of SLOT_KEYS) $('submit-host-' + key).value = slots?.[key] ?? (key === 'cpu' ? 1 : 0); }
    const disabled = !allowed || fixed || submissionBusy || !hostSlotsSupported();
    $('submit-host-slots').disabled = disabled;
    for (const key of SLOT_KEYS) $('submit-host-' + key).disabled = disabled || !$('submit-host-slots').checked;
    const problem = hostSlotsProblem();
    message('host-slots-help', problem || (fixed ? pendingRequest.host_slots_recorded ? slots ? '原宿主 slots 声明已冻结；重试或刷新不改变需求，先按原 ID 查询。' : '原请求明确省略 slots 声明，本次重填仍省略。' : '旧未决请求未保存 slots 声明，不为原 ID 加入新需求；先查询原任务。' : !hostSlotsSupported() ? '节点未提供 jobs.host-capacity-v1；可继续不声明的兼容提交。' : '仅显式开启才发送三项声明；节点最终核准容量，不按当前余量承诺立即运行。'), !!problem);
  }
  const timeBudgetSupported = () => nodeInfo?.capabilities?.includes('jobs.time-budget-v1') === true;
  const diskGuardSupported = () => nodeInfo?.capabilities?.includes('jobs.disk-guard-v1') === true;
  function submissionDiskGuard() {
    if (pendingRequest) return pendingRequest.body?.disk_guard ?? (pendingRequest.disk_guard_recorded ? pendingRequest.disk_guard : null);
    if (!$('submit-disk-guard').checked) return null;
    return {max_log_bytes: Number($('submit-max-log-bytes').value), min_free_bytes: Number($('submit-min-free-bytes').value), stop_action: $('submit-disk-stop-action').value};
  }
  function diskGuardProblem() {
    const guard = submissionDiskGuard();
    if (guard == null) return '';
    if (Object.keys(guard).sort().join(',') !== 'max_log_bytes,min_free_bytes,stop_action' ||
        !['max_log_bytes', 'min_free_bytes'].every(key => Number.isSafeInteger(guard[key]) && guard[key] >= 1) || !['graceful', 'escalate'].includes(guard.stop_action)) return '两个阈值都须为 1–9007199254740991 的整数字节，并选择触发动作。';
    if (!diskGuardSupported()) return '节点未提供 jobs.disk-guard-v1，不能提交此声明；不会删除原阈值或更换请求 ID。';
    if ((pendingRequest?.body?.device_access || $('submit-device-access').value) === 'recovery') return '恢复任务暂不接受磁盘软保护声明，也不继承原任务的阈值。';
    return '';
  }
  function renderDiskGuard(allowed) {
    const fixed = !!pendingRequest, guard = submissionDiskGuard();
    if (fixed) {
      $('submit-disk-guard').checked = !!guard;
      $('submit-max-log-bytes').value = guard?.max_log_bytes ?? '';
      $('submit-min-free-bytes').value = guard?.min_free_bytes ?? '';
      $('submit-disk-stop-action').value = guard?.stop_action || 'graceful';
    }
    const disabled = !allowed || fixed || submissionBusy || !diskGuardSupported();
    $('submit-disk-guard').disabled = disabled;
    for (const id of ['submit-max-log-bytes', 'submit-min-free-bytes', 'submit-disk-stop-action']) $(id).disabled = disabled || !$('submit-disk-guard').checked;
    const problem = diskGuardProblem();
    message('disk-guard-help', problem || (fixed ? pendingRequest.disk_guard_recorded ? guard ? '原磁盘软阈值与触发动作已冻结，重载或重试不改变；先按原 ID 查询。' : '原请求明确省略磁盘保护，本次重填仍省略。' : '旧未决请求未保存磁盘声明，不给原 ID 加入新字段；先查询原任务。' : !diskGuardSupported() ? '节点未提供 jobs.disk-guard-v1；可继续不声明的兼容提交。' : '仅显式启用才发送 disk_guard。观察到日志长度达到上限、共享可用量低于下限或观察失败时请求停止；不保证停止完成。'), !!problem);
  }
  function submissionBudget() {
    if (pendingRequest) return pendingRequest.body?.time_budget ?? (pendingRequest.budget_recorded ? pendingRequest.time_budget : null);
    const raw = $('submit-wall-seconds').value.trim();
    return raw ? {wall_seconds: Number(raw), timeout_action: $('submit-timeout-action').value} : null;
  }
  function timeBudgetProblem() {
    const budget = submissionBudget();
    if (budget == null) return '';
    if (!Number.isInteger(budget.wall_seconds) || budget.wall_seconds < 1 || budget.wall_seconds > 604800 || !['graceful', 'escalate'].includes(budget.timeout_action)) return '运行时间须为 1–604800 的整数秒，并选择到期动作。';
    if (!timeBudgetSupported()) return '节点未提供 jobs.time-budget-v1，不能提交此预算；不会删除原预算或更换请求 ID。';
    if ((pendingRequest?.body?.device_access || $('submit-device-access').value) === 'recovery') return '恢复任务暂不接受运行预算；不会继承原任务的期限。';
    return '';
  }
  function renderTimeBudget(allowed) {
    const fixed = !!pendingRequest, budget = submissionBudget();
    if (fixed) { $('submit-wall-seconds').value = budget?.wall_seconds ?? ''; $('submit-timeout-action').value = budget?.timeout_action || 'graceful'; }
    const disabled = !allowed || fixed || submissionBusy || !timeBudgetSupported();
    $('submit-wall-seconds').disabled = disabled;
    $('submit-timeout-action').disabled = disabled || !$('submit-wall-seconds').value;
    const problem = timeBudgetProblem();
    message('time-budget-help', problem || (fixed ? pendingRequest.budget_recorded ? budget ? '原请求预算已冻结，重载或重试不会替换；先按原 ID 查询。' : '原请求明确未声明预算，本次重填仍省略。' : '旧未决请求未保存预算声明，不给原请求加入新预算；先按原 ID 查询。' : !timeBudgetSupported() ? '节点未提供 jobs.time-budget-v1；留空继续兼容提交，不发送预算字段。' : '留空不发送 time_budget。此预算与命令自身的 --max-seconds 等参数互相独立。'), !!problem);
  }
  const deviceLanesSupported = () => nodeInfo?.capabilities?.includes('devices.lanes-v1') === true;
  function validDeviceBinding(binding) {
    return binding?.schema === 'phonefarm/device-binding@1' && Object.keys(binding).sort().join(',') === 'device_id,domain_id,registry_revision,schema,serial' &&
      typeof binding.domain_id === 'string' && /^[0-9a-f]{32}$/.test(binding.domain_id) && Number.isSafeInteger(binding.registry_revision) && binding.registry_revision > 0 &&
      typeof binding.device_id === 'string' && /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(binding.device_id) &&
      typeof binding.serial === 'string' && !!binding.serial && new TextEncoder().encode(binding.serial).length <= 256 && !/[\s\x00-\x1f\x7f]/.test(binding.serial);
  }
  function bindingFacts(binding) {
    return binding ? [['登记设备 ID', binding.device_id], ['连接别名（serial）', binding.serial], ['冻结登记 revision', binding.registry_revision], ['冻结锁域 ID', binding.domain_id]] : [['设备范围', '兼容全域（未声明设备绑定）']];
  }
  function submissionBinding() {
    if (pendingRequest) return pendingRequest.body?.device_binding || pendingRequest.device_binding || null;
    return $('submit-device-scope').value === 'device' ? deviceBinding : null;
  }
  function deviceBindingProblem() {
    const binding = submissionBinding(), wantsDevice = pendingRequest ? !!binding : $('submit-device-scope').value === 'device';
    if (!wantsDevice) return '';
    if (!deviceLanesSupported()) return '节点未声明 devices.lanes-v1，不能提交设备绑定；不会降级为全域请求。';
    if (!binding) return deviceBindingError || '请读取登记、选择精确别名并核对绑定。';
    if (!validDeviceBinding(binding)) return '原设备绑定格式未确认，请先按请求 ID 找回，不能重新选择目标。';
    if (deviceBindingBusy && !pendingRequest) return '正在核对设备绑定，请等待结果。';
    const body = pendingRequest?.body;
    let argv = body?.argv;
    if (!argv) { try { argv = JSON.parse($('submit-argv').value); } catch (_) { return '绑定仅支持 phonefarm gray-layer session；请先填写有效参数。'; } }
    if ((body?.bin || $('submit-bin').value) !== 'phonefarm' || !Array.isArray(argv) || argv[0] !== 'gray-layer' || argv[1] !== 'session') return '本批设备绑定仅支持 phonefarm gray-layer session；其他命令仍使用兼容全域。';
    if (['none', 'recovery'].includes(body?.device_access || $('submit-device-access').value)) return '创建绑定会话不能选择宿主无设备或恢复模式；typed 恢复入口会继承原绑定。';
    return '';
  }
  function renderDeviceBinding(allowed) {
    const fixed = !!pendingRequest, locked = fixed || submissionBusy || deviceBindingBusy;
    const binding = submissionBinding();
    if (fixed) $('submit-device-scope').value = binding ? 'device' : 'global';
    const scoped = $('submit-device-scope').value === 'device';
    $('submit-device-scope').disabled = !allowed || locked;
    $('submit-device-scope').querySelector('[value="device"]').disabled = !deviceLanesSupported();
    $('device-binding-picker').hidden = !scoped;
    $('read-device-registry').disabled = locked || !deviceLanesSupported() || !!HGTarget.problem();
    $('read-device-registry').setAttribute('aria-busy', String(deviceBindingBusy));
    $('submit-device-id').disabled = !allowed || locked || !deviceRegistry || !!deviceBindingError;
    $('submit-device-alias').disabled = !allowed || locked || !$('submit-device-id').value || !!deviceBindingError;
    $('resolve-device-binding').disabled = !allowed || locked || !deviceLanesSupported() || !deviceRegistry || !$('submit-device-alias').value || !!deviceBindingError;
    $('resolve-device-binding').setAttribute('aria-busy', String(deviceBindingBusy));
    $('device-registry-link').href = HGTarget.url('/dash/fleet');
    const problem = deviceBindingProblem();
    const detail = fixed ? pendingRequest.binding_recorded ? '原请求设备声明已冻结，刷新登记不会改投；先按原 ID 查询。' : '旧未决记录未保存设备声明，不能补入当前登记；先按原 ID 查询。' : deviceBindingBusy ? '正在读取宿主登记或核对绑定，不采设备…' : deviceBindingError || !scoped ? deviceBindingError || '不发送 device_binding 字段，沿兼容全域执行。' : binding ? '绑定已核对；提交时随原请求固定。登记或启动条件变化仍可能被节点拒绝。' : '读取本节点登记后，显式选择设备和精确别名，再核对绑定。';
    message('device-binding-message', `${detail}${scoped && problem ? ` ${problem}` : ''}`, !!deviceBindingError || !!problem);
    $('device-binding-info').hidden = !binding && !deviceRegistry;
    facts('device-binding-info', binding ? bindingFacts(binding) : deviceRegistry ? [['登记节点', deviceRegistry.node_id], ['登记 revision', deviceRegistry.revision], ['启用的登记', deviceRegistry.devices.filter(device => device.enabled).length]] : []);
  }
  function selectDeviceAliases() {
    deviceBinding = null; deviceBindingError = ''; ++deviceBindingRevision;
    const device = deviceRegistry?.devices.find(item => item.device_id === $('submit-device-id').value);
    $('submit-device-alias').replaceChildren(el('option', '请选择精确别名'));
    $('submit-device-alias').firstChild.value = '';
    for (const alias of device?.aliases || []) { const option = el('option', alias); option.value = alias; $('submit-device-alias').append(option); }
    renderSubmission();
  }
  async function readDeviceRegistry() {
    if ($('read-device-registry').disabled) return;
    const revision = ++deviceBindingRevision, target = HGTarget.binding();
    deviceBindingBusy = true; deviceBinding = null; deviceBindingError = ''; renderSubmission();
    try {
      const value = await json(`${API}/node/devices`, target);
      if (revision !== deviceBindingRevision) return;
      HGTarget.assert(target);
      if (value.node_id !== target.node_id || !Array.isArray(value.devices) || !Number.isSafeInteger(value.revision)) throw new Error('登记身份或格式未确认。');
      deviceRegistry = value;
      const placeholder = el('option', value.devices.some(device => device.enabled) ? '请选择已启用设备' : '没有已启用的登记'); placeholder.value = '';
      $('submit-device-id').replaceChildren(placeholder);
      for (const device of value.devices.filter(item => item.enabled)) { const option = el('option', `${device.label} · ${device.device_id}`); option.value = device.device_id; $('submit-device-id').append(option); }
      $('submit-device-alias').replaceChildren(el('option', '请选择设备与别名')); $('submit-device-alias').firstChild.value = '';
    } catch (error) { if (revision === deviceBindingRevision) deviceBindingError = `${errorText(error)} 未核对绑定，不会降级或重试。`; }
    finally { if (revision === deviceBindingRevision) { deviceBindingBusy = false; renderSubmission(); } }
  }
  async function resolveDeviceBinding() {
    if ($('resolve-device-binding').disabled) return;
    const revision = ++deviceBindingRevision, target = HGTarget.binding(), serial = $('submit-device-alias').value, deviceId = $('submit-device-id').value;
    deviceBindingBusy = true; deviceBinding = null; deviceBindingError = ''; renderSubmission();
    try {
      const value = await json(`${API}/node/devices/resolve?serial=${encodeURIComponent(serial)}`, target);
      if (revision !== deviceBindingRevision) return;
      HGTarget.assert(target);
      if (value.node_id !== target.node_id || !validDeviceBinding(value.binding) || value.binding.serial !== serial || value.binding.device_id !== deviceId || value.binding.registry_revision !== deviceRegistry?.revision) throw new Error('返回绑定与选择/登记版本不一致，请显式重读登记后选择；当前没有绑定。');
      deviceBinding = value.binding; $('template-serial').value = serial;
    } catch (error) { if (revision === deviceBindingRevision) deviceBindingError = `${errorText(error)} 未固定绑定，不会自动重试。`; }
    finally { if (revision === deviceBindingRevision) { deviceBindingBusy = false; renderSubmission(); } }
  }
  function processPolicyFacts(node) {
    if (!node?.capabilities?.includes('jobs.execution-policy-v1')) return [['进程执行策略能力', '未提供，执行范围未知']];
    return [['进程执行策略协议', 'jobs.execution-policy-v1'], ...[['process_group', '原进程组策略'], ['contained', '约束域策略']].map(([key, label]) => {
      const policy = node.execution?.process_policies?.[key];
      const description = !policy ? '未提供' : policy.available === true ? `可选择 · 后端：${text(policy.backend)} · 完成范围：${text(policy.completion_scope)}` : '不可用';
      return [`${label}（${key}）`, `${description}${policy?.reason ? `。${policy.reason}` : ''}`];
    })];
  }
  function executionPolicyProblem(policy) {
    if (!policy) return '';
    if (!nodeInfo?.capabilities?.includes('jobs.execution-policy-v1')) return '此节点未提供执行策略协议，不能发送显式策略；可选择兼容默认，实际范围未知。';
    const capability = nodeInfo.execution?.process_policies?.[policy];
    return capability?.available === true ? '' : `${policy} 不可用：${capability?.reason || '节点未提供可用声明。'} 不会降级提交。`;
  }
  function inputsSupported() { return nodeInfo?.capabilities?.includes('jobs.inputs-v1') === true; }
  function parseInputManifest(value) {
    let inputs;
    try { inputs = JSON.parse(value); } catch (_) { throw new Error('输入工件清单不是有效的 JSON。'); }
    if (!inputs || inputs.schema !== 'phonefarm/job-inputs@1' || !Array.isArray(inputs.files) || !inputs.files.length || inputs.files.length > 32 ||
        !Array.isArray(inputs.bindings) || !['default', 'input'].includes(inputs.cwd)) throw new Error('输入清单需为 inputs@1 对象，包含 node_id、1–32 项 files、bindings 数组及 cwd（default/input）。');
    if (inputs.node_id !== nodeInfo?.node_id) throw new Error('输入工件只能引用当前节点；清单 node_id 与当前节点不一致。');
    if (new TextEncoder().encode(JSON.stringify(inputs)).length > 32 * 1024) throw new Error('输入工件清单不能超过 32 KiB。');
    return inputs;
  }
  function renderSubmission() {
    const supported = nodeInfo?.capabilities?.includes('jobs.idempotency-v1') && nodeInfo?.execution?.bins?.length;
    const allowed = supported && access?.steer && HGTarget.canWrite() && !accessBusy;
    const targetIssue = pendingRequest ? HGTarget.problem(pendingRequest.target || null) : HGTarget.problem();
    const refilling = !!pendingRequest?.refilling;
    const locked = (!!pendingRequest && !refilling) || submissionBusy;
    const mode = $('submit-device-access').value;
    const recovery = mode === 'recovery';
    const policy = pendingRequest?.body ? pendingRequest.body.execution_policy || '' : $('submit-execution-policy').value;
    const policyProblem = executionPolicyProblem(policy);
    const hasInputs = pendingRequest?.body ? !!pendingRequest.body.inputs : !!$('submit-inputs').value.trim();
    const inputsProblem = hasInputs && !inputsSupported();
    const bindingProblem = deviceBindingProblem();
    const budgetProblem = timeBudgetProblem();
    const guardProblem = diskGuardProblem();
    const slotsProblem = hostSlotsProblem();
    const gatewayProblem = graySessionProblem(submissionCommand());
    message('gray-gateway-help', isGraySession(submissionCommand()) ? pendingRequest ? `原请求优先查回；只有明确未找到时才按当前兼容性核准同 ID 新建。${gatewayProblem}` : gatewayProblem || '当前配置 PF 声明兼容此网关；创建和实际启动前仍由节点复核，不代表设备就绪。' : '', !!gatewayProblem);
    $('gray-gateway-help').hidden = !isGraySession(submissionCommand());
    renderTimeBudget(allowed);
    renderDiskGuard(allowed);
    renderHostSlots(allowed);
    for (const id of ['submit-bin', 'submit-label', 'submit-argv']) $(id).disabled = !allowed || locked;
    $('submit-inputs').disabled = !allowed || locked || !inputsSupported();
    $('clear-inputs').disabled = !allowed || locked || !$('submit-inputs').value.trim();
    message('inputs-help', !nodeInfo ? '正在核对输入工件能力…' : !inputsSupported() ? '此节点未提供 jobs.inputs-v1，不能提交输入清单。留空可继续使用原有任务入口。' : pendingRequest?.body ? '重试保持原输入清单与参数，按原请求 ID 核对结果。' : '从输出文件按需复制清单，或填写完整声明。留空不发送 inputs 字段；具体路径与绑定由节点校验。', inputsProblem);
    $('submit-execution-policy').disabled = !allowed || locked;
    message('execution-policy-help', `${pendingRequest?.body ? '重试保留原请求策略。' : ''}${policyProblem || (!nodeInfo ? '正在核对执行策略能力…' : !nodeInfo.capabilities?.includes('jobs.execution-policy-v1') ? '此节点未提供策略能力。兼容默认不发送新字段，实际执行范围未知。' : policy ? `${policy}：${nodeInfo.execution?.process_policies?.[policy]?.reason || '以任务中 worker 的实际执行记录为准。'}` : '兼容默认不发送策略字段，沿节点原有行为；当前原进程组策略不涵盖 setsid 脱组后代。')} 设备恢复另行核查。`, !!policyProblem);
    $('submit-device-access').disabled = !allowed || locked || !recoverySupported();
    $('submit-resources').disabled = !allowed || locked || recovery;
    $('submit-recovery-for').disabled = !allowed || locked || !recoverySupported();
    $('submit-recovery-for').required = recovery;
    $('recovery-target-field').hidden = !recovery;
    message('device-access-help', !nodeInfo ? '正在核对设备访问能力…' : !recoverySupported() ? '此节点尚未提供设备访问分类与恢复任务能力；可继续按节点默认提交。' : recovery ? '恢复任务继承目标的独占资源。请填写实际恢复命令；运行结束不会自动解除原任务屏障。' : '按实际操作选择访问模式。默认由节点按命令保守判断；无设备访问只适用于宿主任务。');
    $('resources-help').textContent = recovery ? '恢复任务由节点继承目标资源，此处不另行声明。' : submissionBinding() ? '节点自动保存绑定设备的必要资源；自由资源不能删除设备范围冲突，也不授予设备权限。' : '相同资源的任务依次执行。资源声明只用于排队，不授予设备操作权限。';
    $('submit-job').disabled = !allowed || !!targetIssue || (!pendingRequest && !!gatewayProblem) || locked || deviceBindingBusy || !!bindingProblem || !!budgetProblem || !!guardProblem || !!slotsProblem || !!policyProblem || inputsProblem || (!!mode && !recoverySupported()) || (!pendingRequest?.id && !globalThis.crypto?.randomUUID);
    $('submit-job').setAttribute('aria-busy', String(submissionBusy));
    $('submit-job').textContent = submissionBusy ? '正在核对请求…' : refilling ? '使用原请求 ID 提交' : '提交任务';
    message('submit-help', targetIssue || (refilling ? '将复用原请求 ID。请尽量填写原内容；内容不同可能返回 409 冲突，应优先查询。' : pendingRequest ? '本次请求尚未确认，先按原 ID 查询，暂不创建新请求。' : !nodeInfo ? '等待取得节点命令。' : !supported ? '此节点尚不支持可靠提交，已有任务仍可查看。' : !access?.steer || !HGTarget.canWrite() || accessBusy ? '先在上方解锁任务操作权限。' : !globalThis.crypto?.randomUUID ? '请通过 HTTPS 或本机地址访问后提交。' : '提交后以节点返回的实际阶段为准。'));
    $('request-id').readOnly = !!pendingRequest;
    $('request-id').disabled = submissionBusy;
    if (pendingRequest) $('request-id').value = pendingRequest.id;
    $('request-lookup').disabled = submissionBusy || !!targetIssue;
    $('request-lookup').setAttribute('aria-busy', String(submissionBusy));
    $('submit-refill').hidden = !pendingRequest || !!pendingRequest.body || refilling;
    $('submit-refill').disabled = !allowed || !!targetIssue || submissionBusy;
    $('submit-retry').hidden = !pendingRequest?.body || pendingRequest.conflict;
    $('submit-retry').disabled = !allowed || !!targetIssue || submissionBusy || !!bindingProblem || !!budgetProblem || !!guardProblem || !!slotsProblem || !!policyProblem || inputsProblem;
    renderDeviceBinding(allowed);
    renderSessionTemplate(allowed);
    renderRefbenchTemplate(allowed);
    renderControls();
  }
  function renderSessionTemplate(allowed) {
    const unavailable = !allowed || !!pendingRequest || submissionBusy || !nodeInfo?.execution?.bins?.includes('phonefarm');
    for (const id of ['template-serial', 'template-package', 'template-mode', 'template-seconds', 'template-power', 'template-so', 'template-apply']) $(id).disabled = unavailable;
    if ($('submit-device-scope').value === 'device') { $('template-serial').disabled = true; $('template-apply').disabled = unavailable || !deviceBinding || deviceBindingBusy; }
    message('template-power-help', $('template-power').value === 'battery' ? '首次采样准备电池条件，跨轮保持；会话停止或同上下文恢复时还原所绑定的充电控制。已实测放电时可能无需修改控制。' : '只观察当前供电状态，不请求修改充电控制。功耗是否可用仍以每轮观测和采样窗口为准。');
    message('template-status', pendingRequest ? '存在未确认提交，先按原请求 ID 找回；模板不会覆盖该请求。' : unavailable ? '需解锁任务操作，且节点提供 phonefarm 命令，才能填入模板。' : '模板只填入参数；核对下方命令后，再点击“提交任务”。');
  }
  function applySessionTemplate() {
    if ($('template-apply').disabled || pendingRequest || submissionBusy) return;
    const serial = submissionBinding()?.serial || $('template-serial').value.trim(), pkg = $('template-package').value.trim();
    const mode = $('template-mode').value, seconds = Number($('template-seconds').value), policy = $('template-power').value;
    if (!serial || !pkg || !['frameaudit', 'frameaudit-ts', 'probe', 'target', 'passdump', 'loadop'].includes(mode) ||
        !Number.isInteger(seconds) || seconds < 1 || seconds > 3600 || !['observe', 'battery'].includes(policy)) {
      message('template-status', '请填写设备、包名、支持的模式、1–3600 秒预算及供电策略；尚未填入。', true); return;
    }
    const argv = ['gray-layer', 'session', '--serial', serial, '--package', pkg, '--mode', mode, '--out', '@out', '--max-seconds', String(seconds), '--power-policy', policy];
    const so = $('template-so').value.trim();
    if (so) argv.push('--so', so);
    $('submit-bin').value = 'phonefarm';
    $('submit-argv').value = JSON.stringify(argv, null, 2);
    $('submit-device-access').value = recoverySupported() ? 'write' : '';
    $('submit-recovery-for').value = '';
    if (!$('submit-label').value.trim()) $('submit-label').value = `灰层会话 · ${pkg}`;
    renderSubmission();
    message('template-status', `已填入 ${policy} 策略的新会话参数，尚未提交。请核对下方 argv；运行中的会话不受影响。`);
  }
  function renderRefbenchTemplate(allowed) {
    const unavailable = !allowed || !!pendingRequest || submissionBusy || !nodeInfo?.execution?.bins?.includes('phonefarm') || $('submit-device-scope').value === 'device';
    for (const id of ['refbench-serial', 'refbench-frames', 'refbench-intensity', 'refbench-timeout', 'refbench-so', 'refbench-apply']) $(id).disabled = unavailable;
    message('refbench-status', pendingRequest ? '存在未确认提交，先按原请求 ID 找回；模板不会覆盖该请求。' : $('submit-device-scope').value === 'device' ? 'refbench 尚未支持设备 lane；如需此模板，请显式选择兼容全域。' : unavailable ? '需解锁任务操作，且节点提供 phonefarm 命令，才能填入模板。' : '模板只填入参数；核对下方命令后，再点击“提交任务”。');
  }
  function applyRefbenchTemplate() {
    if ($('refbench-apply').disabled || pendingRequest || submissionBusy) return;
    const serial = $('refbench-serial').value.trim(), frames = Number($('refbench-frames').value);
    const intensity = Number($('refbench-intensity').value), timeout = Number($('refbench-timeout').value);
    if (!serial || !Number.isInteger(frames) || frames < 60 || frames > 100000 ||
        !Number.isInteger(intensity) || intensity < 1 || intensity > 64 ||
        !Number.isInteger(timeout) || timeout < 1 || timeout > 600) {
      message('refbench-status', '请填写设备、60–100000 帧、1–64 强度及 1–600 秒单臂超时；尚未填入。', true); return;
    }
    const argv = ['gray-layer', 'refbench-loadop', '--serial', serial, '--frames', String(frames), '--intensity', String(intensity), '--timeout-seconds', String(timeout), '--out', '@out', '--json'];
    const so = $('refbench-so').value.trim();
    if (so) argv.push('--so', so);
    $('submit-bin').value = 'phonefarm';
    $('submit-argv').value = JSON.stringify(argv, null, 2);
    $('submit-device-access').value = recoverySupported() ? 'write' : '';
    $('submit-recovery-for').value = '';
    if (!$('submit-label').value.trim()) $('submit-label').value = 'refbench · 四臂 loadop';
    renderSubmission();
    message('refbench-status', '已填入四臂 refbench 参数，尚未提交。请核对设备、参数与独占资源后，再提交任务。');
  }
  function submissionMessage(value, error = false, id = '') {
    $('submission-receipt').hidden = false;
    message('submit-message', value, error);
    $('submit-request-id').textContent = id ? `请求 ID：${id}` : '';
  }
  async function submissionFound(job, id, selectionVersion, lookup = false) {
    if (!job?.id) throw new Error('节点未返回任务 ID，提交结果尚未确认。');
    pendingRequest = null;
    savePending();
    submittedJob = job.id;
    $('submit-open-job').hidden = false;
    submissionMessage(`${lookup ? '找到此请求对应的任务，请核对执行参数' : '任务已记录'}：${job.id} · ${status(job)[0]}。`, false, id);
    await Promise.all([loadJobs(), loadQueue(), detailRevision === selectionVersion ? selectCurrentJob(job.id) : null]);
  }
  async function submitJob(retry = false) {
    if (submissionBusy || (retry ? $('submit-retry').disabled : $('submit-job').disabled)) return;
    const originalPending = !!pendingRequest;
    if (!retry) {
      try {
        const argv = JSON.parse($('submit-argv').value);
        if (!Array.isArray(argv) || !argv.every(value => typeof value === 'string')) throw new Error('参数必须是 JSON 字符串数组，例如 ["--help"]。');
        const body = {bin: $('submit-bin').value, argv, label: $('submit-label').value.trim()};
        const inputText = $('submit-inputs').value.trim();
        if (inputText) {
          if (!inputsSupported()) throw new Error('此节点未提供 jobs.inputs-v1，未提交输入清单。');
          body.inputs = parseInputManifest(inputText);
        }
        const policy = $('submit-execution-policy').value;
        const policyProblem = executionPolicyProblem(policy);
        if (policyProblem) throw new Error(policyProblem);
        if (policy) body.execution_policy = policy;
        const budgetProblem = timeBudgetProblem();
        if (budgetProblem) throw new Error(budgetProblem);
        const timeBudget = submissionBudget();
        if (timeBudget) body.time_budget = timeBudget;
        const budgetRecorded = pendingRequest ? pendingRequest.budget_recorded === true : true;
        const guardProblem = diskGuardProblem();
        if (guardProblem) throw new Error(guardProblem);
        const diskGuard = submissionDiskGuard();
        if (diskGuard) body.disk_guard = diskGuard;
        const diskGuardRecorded = pendingRequest ? pendingRequest.disk_guard_recorded === true : true;
        const slotsProblem = hostSlotsProblem();
        if (slotsProblem) throw new Error(slotsProblem);
        const hostSlots = submissionHostSlots();
        if (hostSlots) body.host_slots = hostSlots;
        const hostSlotsRecorded = pendingRequest ? pendingRequest.host_slots_recorded === true : true;
        const mode = $('submit-device-access').value;
        if (mode) body.device_access = mode;
        if (mode === 'recovery') {
          const target = $('submit-recovery-for').value.trim();
          if (!/^[0-9a-f]{12}$/.test(target)) throw new Error('恢复任务需要原资源保留任务的 12 位 ID。');
          body.recovery_for = target;
        }
        const resources = $('submit-resources').value.split(/\r?\n/).map(value => value.trim()).filter(Boolean);
        if (resources.length && mode !== 'recovery') body.resources = [...new Set(resources)];
        const bindingProblem = deviceBindingProblem();
        if (bindingProblem) throw new Error(bindingProblem);
        const binding = submissionBinding();
        if (binding) body.device_binding = binding;
        pendingRequest = {id: pendingRequest?.id || crypto.randomUUID(), target: pendingRequest ? pendingRequest.target : HGTarget.binding(), body,
          binding_recorded: true, device_binding: binding, budget_recorded: budgetRecorded, time_budget: timeBudget, host_slots_recorded: hostSlotsRecorded, host_slots: hostSlots,
          disk_guard_recorded: diskGuardRecorded, disk_guard: diskGuard};
        savePending();
      } catch (error) { submissionMessage(error instanceof SyntaxError ? '参数不是有效的 JSON，请填写字符串数组。' : error.message, true); return; }
    }
    const attempt = pendingRequest;
    const selectionVersion = detailRevision;
    let originalLookup = false;
    submissionBusy = true;
    submittedJob = '';
    $('submit-open-job').hidden = true;
    submissionMessage('正在提交；本次请求 ID 已固定。', false, attempt.id);
    renderSubmission();
    try {
      if (isGraySession(attempt.body)) {
        let alreadyRecorded = false;
        if (originalPending) {
          originalLookup = true;
          let existing;
          try { existing = await json(`${API}/requests/${encodeURIComponent(attempt.id)}`, attempt.target || null); }
          catch (error) { if (error.status !== 404 || error.payload?.error !== 'no such job') throw error; }
          if (existing) {
            if (existing.request_id !== attempt.id) throw new Error('返回任务未绑定原请求 ID；保留原请求，不继续提交。');
            alreadyRecorded = true;
          }
          originalLookup = false;
        }
        const problem = alreadyRecorded ? '' : graySessionProblem(attempt.body);
        if (problem) { submissionMessage(`${problem} 本次未发送创建请求；保留原参数和 ID。`, true, attempt.id); return; }
      }
      const job = await post(`${API}/jobs`, attempt.body, {'X-Request-ID': attempt.id}, attempt.target || null);
      await submissionFound(job, attempt.id, selectionVersion);
    } catch (error) {
      if (originalLookup) { submissionMessage(`原请求查询未确认：${errorText(error)} 保留原 ID 和输入，没有发送创建请求。`, true, attempt.id); return; }
      const gatewayRejection = ['program_protocol_unsupported', 'program_identity_unavailable', 'program_override_unsupported', 'program_configuration_changed'].includes(error.payload?.code);
      if (gatewayRejection) {
        submissionMessage(`${error.payload.code}：${error.payload.error || error.message}。${error.payload.command_started === false ? '本次新 producer 未启动；' : ''}保留原请求 ID 和输入，先查询原任务。不会自动刷新或重试。`, true, attempt.id); return;
      }
      if (error.status >= 400 && error.status < 500 && error.status !== 409) { pendingRequest = null; savePending(); }
      else if (error.status === 409) attempt.conflict = true;
      const capacityIssue = error.payload?.code === 'host_capacity_exceeded' ? `宿主 slots 请求超过节点配置，未被准入。${shortageText(error.payload.shortages)}。请修改明确需求后提交，不会自动缩减。` : '';
      submissionMessage(capacityIssue || (error.status === 409 ? '请求 ID 或参数冲突。已保留原 ID，请查询对应任务并核对参数；不会自动重试。' : pendingRequest ? '提交结果未确认，节点可能已记录。请按原请求 ID 查询；重试也会使用同一 ID 和参数。' : errorText(error)), true, attempt.id);
      if (error.status === 401 || error.status === 403) await loadAccess();
    } finally { submissionBusy = false; renderSubmission(); }
  }
  async function lookupRequest() {
    if (submissionBusy) return;
    const id = $('request-id').value.trim();
    if (!id) return;
    const selectionVersion = detailRevision;
    submissionBusy = true;
    $('submit-open-job').hidden = true;
    submissionMessage('正在查询请求记录…', false, id);
    renderSubmission();
    try { await submissionFound(await json(`${API}/requests/${encodeURIComponent(id)}`, pendingRequest?.id === id ? pendingRequest.target || null : HGTarget.binding()), id, selectionVersion, true); }
    catch (error) {
      submissionMessage(error.status === 404 ? '暂未找到此请求。若此前提交结果不确定，请保留原 ID 稍后再查；当前页面保有参数时也可用原 ID 重试。' : errorText(error), true, id);
    } finally { submissionBusy = false; renderSubmission(); }
  }
  async function releaseQueue() {
    const job = currentJob;
    if (!job || $('queue-release-submit').disabled) return;
    const note = $('queue-release-note').value.trim();
    releasing.add(job.id);
    renderControls();
    try {
      const result = await post(`${API}/jobs/${encodeURIComponent(job.id)}/queue-release`, {note});
      releaseReceipts.set(job.id, {message: result.released === true ? '资源保留已解除，后续任务可继续调度。核查说明已记录；此操作不代表平台验证了设备恢复。' : result.job && storageFailed(result.job) ? `核查记录保存失败，资源仍然保留。请处理存储问题后重试保存结果。${result.job.queue?.state_reason?.message || ''}` : '资源未释放，请核对最新任务状态。', error: result.released !== true});
    } catch (error) {
      releaseReceipts.set(job.id, {message: !error.status || error.status >= 500 ? '释放结果尚未确认，请刷新任务和队列核对。' : error.status === 409 ? `资源未释放：当前条件不允许放行，请核查实际 lease 与任务状态。${errorText(error)}` : errorText(error), error: true});
      if (error.status === 401 || error.status === 403) await loadAccess();
    } finally {
      releasing.delete(job.id);
      await Promise.all([loadQueue(), !historical() && selected === job.id ? selectJob(job.id, true) : null]);
    }
  }

  async function loadNode(refreshRuntime = false) {
    if (runtimeBusy || refreshRuntime && $('refresh-runtime').disabled) return;
    const expected = nodeInfo?.node_id;
    if (refreshRuntime) runtimeBusy = true;
    const revision = ++nodeRevision;
    nodeInfo = null;
    renderInputLimits();
    renderRuntimeRefresh();
    renderSubmission();
    updateJobLeaseAccess();
    updateJobInputAccess();
    regionBusy('node-card', true);
    if (!refreshRuntime) {
      $('node-summary').hidden = true;
      $('node-details').hidden = true;
      $('node-updated').textContent = '';
    }
    message('node-message', refreshRuntime ? '正在重新观察配置程序；下方仍是上次取得的信息…' : '正在读取节点信息…');
    try {
      const node = refreshRuntime ? await post(`${API}/node/runtime/refresh`, {}) : await json(`${API}/node`);
      if (revision !== nodeRevision) return;
      if (refreshRuntime && node.node_id !== expected) throw new Error('程序观察响应的节点身份不匹配。');
      nodeInfo = node;
      const chosen = $('submit-bin').value;
      const bins = node.execution?.bins || [];
      $('submit-bin').replaceChildren(...bins.map(bin => { const option = el('option', bin); option.value = bin; return option; }));
      if (bins.includes(chosen)) $('submit-bin').value = chosen;
      else $('submit-bin').value = $('submit-device-access').value === 'recovery' ? '' : bins[0] || '';
      facts('node-info', [
        ['节点 ID', node.node_id], ['访问入口', location.origin],
        ['宿主', `${text(node.host?.hostname)} · ${text(node.host?.platform)}`],
        ...serviceFacts(node.service),
        ['任务目录', node.storage?.jobs_root], ['数据目录', node.storage?.data_root], ...exportCacheFacts(node),
        ['服务启动', time(node.started_at)], ...processPolicyFacts(node), ...hostCapacityFacts(node.execution?.host_capacity)
      ]);
      renderPrograms(node);
      $('node-summary').textContent = `${text(node.node_id)} · ${text(node.host?.hostname)}`;
      $('node-summary').hidden = false;
      $('node-details').hidden = false;
      message('node-message', refreshRuntime ? '程序观察请求已完成，请核对各项 observed / stale / unknown；没有改写历史作业。' : '');
      $('node-updated').textContent = readTime();
    } catch (error) {
      if (revision === nodeRevision) message('node-message', refreshRuntime ? `程序观察结果未取得：${error.message || '连接中断'}。下方保留上次观察，当前身份尚未确认；请读取节点缓存核对，不会自动重试。` : errorText(error), true);
      if (refreshRuntime && [401, 403].includes(error.status)) await loadAccess();
    } finally { if (refreshRuntime) runtimeBusy = false; if (revision === nodeRevision) { regionBusy('node-card', false); renderSubmission(); updateJobLeaseAccess(); updateJobInputAccess(); renderSessionControls(); renderRuntimeRefresh(); renderJobEvents(); renderInputLimits(); renderExportLimits(); } }
  }
  function highlight() {
    for (const button of $('task-list').querySelectorAll('button')) {
      if (button.dataset.job === selected) button.setAttribute('aria-current', 'true');
      else button.removeAttribute('aria-current');
    }
  }
  function addJob(job) {
    const item = el('li');
    const button = el('button', null, 'task-item');
    button.type = 'button';
    button.dataset.job = job.id;
    button.append(el('div', job.label || job.bin || '未命名任务', 'task-item-title'));
    const row = el('div', null, 'task-item-line');
    const [label, kind] = status(currentJob?.id === job.id ? currentJob : job);
    row.append(el('span', job.id, 'task-item-id'), el('span', label, `task-state ${kind}`));
    const command = [job.bin, ...(Array.isArray(job.argv) ? job.argv : [])].map(text).join(' ');
    button.append(row, el('span', time(job.started), 'task-item-time'), el('span', command.length > 220 ? command.slice(0, 220) + '…' : command, 'task-item-command'));
    button.addEventListener('click', () => {
      if (job.id !== selected) selectJob(job.id);
      if (window.matchMedia('(max-width: 780px)').matches) $('detail-heading').focus();
    });
    item.append(button);
    $('task-list').append(item);
  }
  async function loadJobs(append = false) {
    const revision = ++listRevision;
    if (!append) {
      jobsCursor = null;
      $('task-list').replaceChildren();
      $('more-jobs').hidden = true;
      $('task-count').hidden = true;
      $('list-warnings').hidden = true;
    }
    regionBusy('task-browser', true);
    busy('search-jobs', true, '搜索中…');
    busy('more-jobs', true, '正在加载…');
    message('list-message', append ? '正在读取更多任务…' : '正在读取任务…');
    const query = sourceParams({limit: '50', query: activeFilters.query, ...(!historical() ? {state: activeFilters.state} : {})});
    if (append && jobsCursor) query.set('cursor', jobsCursor);
    try {
      if (historyProblem()) throw new Error(historyProblem());
      const result = await json(`${jobsPath()}?${query}`);
      if (revision !== listRevision) return;
      checkHistoryIdentity(result);
      if (!Array.isArray(result.items)) throw new Error('任务列表格式不正确。');
      result.items.forEach(addJob);
      jobsCursor = result.next_cursor || null;
      $('more-jobs').hidden = !jobsCursor;
      const count = $('task-list').children.length;
      $('task-count').textContent = `${count} 条已加载`;
      $('task-count').hidden = false;
      message('list-message', count ? `已显示 ${count} 个任务${jobsCursor ? '，还有更多' : ''}。` : (activeFilters.query || activeFilters.state ? '没有匹配的任务，可调整搜索条件。' : historical() ? (result.warning_count ? '部分历史元信息不可读取，请核对下方警告；不能据此判断没有任务。' : '此来源未返回任务记录。') : '这个节点还没有任务记录。'));
      const warnings = Array.isArray(result.warnings) ? result.warnings : [];
      $('list-warnings').textContent = warnings.map(w => typeof w === 'string' ? w : JSON.stringify(w)).join('；');
      if (result.warning_count > warnings.length) $('list-warnings').textContent += `（共 ${result.warning_count} 项警告）`;
      $('list-warnings').hidden = !warnings.length && !result.warning_count;
      highlight();
    } catch (error) {
      if (revision === listRevision) message('list-message', errorText(error), true);
    } finally {
      if (revision === listRevision) {
        busy('search-jobs', false);
        busy('more-jobs', false);
        regionBusy('task-browser', false);
      }
    }
  }
  async function selectJob(id, keepDirectory = false) {
    if (typeof id !== 'string' || !/^[0-9a-f]{12}$/.test(id)) {
      message('detail-message', '任务 ID 格式不正确。', true);
      return;
    }
    selected = id;
    currentJob = null;
    stopJobEvents(); syncJobEventScope();
    resetJobLeases();
    resetJobInputs();
    resetInputReference();
    resetSession();
    resetDirectoryExport();
    const revision = ++detailRevision;
    ++artifactRevision;
    ++metadataRevision;
    ++logRevision;
    if (!keepDirectory) directory = '';
    $('detail-content').hidden = true;
    $('force-form').hidden = true;
    $('force-confirm').checked = false;
    $('execution-reconcile-force-form').hidden = true;
    $('execution-reconcile-force-confirm').checked = false;
    $('queue-release-panel').open = false;
    $('queue-release-note').value = '';
    $('detail-placeholder').hidden = false;
    $('detail-updated').textContent = '';
    $('refresh-detail').hidden = false;
    busy('refresh-detail', true, '读取中…');
    regionBusy('task-detail', true);
    message('detail-message', `正在读取 ${id}…`);
    highlight();
    updateJobHash();
    renderJobSource();
    try {
      if (historyProblem()) throw new Error(historyProblem());
      const job = await json(jobURL());
      if (revision !== detailRevision) return;
      checkHistoryIdentity(job);
      if (historical()) {
        const [label, kind] = status(job);
        $('job-title').textContent = job.label || job.bin || '未命名历史任务';
        $('job-state').textContent = label; $('job-state').className = `task-state ${kind}`;
        $('detail-updated').textContent = `${readTime()} · 文件可由外部程序修改，不是存储快照`;
        facts('job-info', [['节点', job.node_id], ['来源', job.source_id], ['来源 revision', job.source_revision], ['任务 ID', job.id], ['命令', job.bin], ['原开始字段', time(job.started)], ['原状态', job.recorded_state], ['原阶段', job.recorded_phase], ['记录中的退出码', job.recorded_exit], ['退出码来源', ({meta: '原 meta', exitcode: '原 exitcode 文件'})[job.recorded_exit_source] || '未记录']]);
        $('historical-record').textContent = JSON.stringify(job.record ?? null, null, 2);
        $('detail-content').hidden = false; $('detail-placeholder').hidden = true;
        resetLog(); await Promise.all([loadArtifacts(), loadLog()]); return;
      }
      currentJob = job;
      if (reconcileReceipts.get(id)?.unconfirmed) reconcileReceipts.set(id, {message: '已重新读取原任务状态。请核对宿主核查与输出交还记录，再决定是否操作。', error: false});
      updateJobLeaseAccess();
      updateJobInputAccess();
      renderSessionControls();
      const [label, kind] = status(job);
      $('job-title').textContent = job.label || job.bin || '未命名任务';
      $('job-state').textContent = label;
      $('job-state').className = `task-state ${kind}`;
      for (const button of $('task-list').querySelectorAll('button')) {
        if (button.dataset.job !== job.id) continue;
        const state = button.querySelector('.task-state');
        if (state) { state.textContent = label; state.className = `task-state ${kind}`; }
      }
      $('detail-updated').textContent = readTime();
      facts('job-info', [['任务 ID', job.id], ...(job.request_id ? [['请求 ID', job.request_id]] : []), ['命令', job.bin], ['提交 / 旧开始字段', time(job.started)], [job.phase === 'finalizing' || storageFailed(job) ? '候选结束时间（未提交）' : '结束', time(job.ended)], ['记录说明', job.note || '无']]);
      renderCommitted(job);
      renderInputPreparation(job);
      $('job-command').textContent = JSON.stringify({bin: job.bin, argv: job.argv || []}, null, 2);
      renderJobRuntime(job);
      $('job-results-link').href = HGTarget.url(`/dash/results?job_id=${encodeURIComponent(job.id)}`);
      $('job-register-result').href = HGTarget.url(`/dash/results#register=${encodeURIComponent(job.id)}`);
      renderExecution(job);
      $('detail-content').hidden = false;
      $('detail-placeholder').hidden = true;
      message('detail-message', '');
      resetLog();
      await Promise.all([loadArtifacts(), loadLog(), job.bin === 'phonefarm' && job.argv?.[0] === 'gray-layer' && job.argv?.[1] === 'session' ? loadSession() : null]);
    } catch (error) {
      if (revision === detailRevision) {
        const receipt = historical() ? null : cancelReceipts.get(id);
        message('detail-message', `${errorText(error)}${receipt ? ` ${receipt.message}` : ''}`, true);
      }
    }
    finally {
      if (revision === detailRevision) {
        busy('refresh-detail', false);
        regionBusy('task-detail', false);
        renderJobEvents();
      }
    }
  }
  let sessionState = null, sessionFresh = false, sessionRevision = 0, sessionBusy = false, sessionActionBusy = false;
  let sessionImageURL = null, sessionImageKey = '', receiptRevision = 0, sessionSampleReceipt = null, sessionQualityReceipt = null, sampleFrame = null;
  let referenceRun = null;
  const pendingActions = new Map();
  const sessionRecoveries = new Map(), sessionRecovering = new Set();
  let sessionRecoveryPlan = null, sessionPlanTarget = null, sessionPlanFresh = false, sessionPlanBusy = false;
  let sessionPlanRevision = 0, sessionPlanReadAt = null, sessionPlanError = '';
  const SESSION_PHASES = {starting: '正在准备', ready: '可交互', executing: '动作执行中', measuring: '正在测量 · 其他操作暂停', restoring: '正在恢复', closed: '会话已关闭', recovery_required: '需要恢复'};
  const ACTION_STATES = {accepted: '请求已记录，尚未执行完成。', started: '动作执行中。', completed: '此动作已完成；请核对结果与证据。', failed: '动作失败，请核对错误和恢复状态。', unknown: '执行结果未知，不要换 ID 重复输入。', not_executed: '请求未执行。'};
  function sessionKey(id = selected) { return `hgamey.session-actions.${id}`; }
  function sessionPending(id = selected) {
    if (!pendingActions.has(id)) {
      let saved = [];
      try { saved = JSON.parse(sessionStorage.getItem(sessionKey(id)) || '[]'); } catch (_) { /* Query by ID remains available. */ }
      pendingActions.set(id, Array.isArray(saved) ? saved.filter(v => v && typeof v.request_id === 'string').slice(0, 16) : []);
    }
    return pendingActions.get(id);
  }
  function keepAction(id, request, receipt) {
    let pending = sessionPending(id).filter(item => item.request_id !== request.request_id);
    if (!receipt || ['accepted', 'started', 'unknown'].includes(receipt.status)) pending.push({request_id: request.request_id, action: request.action || 'unknown', target: Object.hasOwn(request, 'target') ? request.target : (sessionPending(id).find(p => p.request_id === request.request_id)?.target || (!receipt ? HGTarget.binding() : null))});
    pendingActions.set(id, pending);
    try { if (pending.length) sessionStorage.setItem(sessionKey(id), JSON.stringify(pending)); else sessionStorage.removeItem(sessionKey(id)); } catch (_) { /* Request IDs remain visible in this page. */ }
  }
  function resetSession() {
    stopReference('任务详情已切换或刷新。');
    referenceRun = null;
    $('reference-result').hidden = true;
    message('reference-message', '按标签查询共享回执；本页不保存整组进度。');
    ++sessionRevision; ++receiptRevision; ++sessionPlanRevision;
    sessionRecoveryPlan = sessionPlanTarget = sessionPlanReadAt = null;
    sessionPlanFresh = sessionPlanBusy = false; sessionPlanError = '';
    sessionState = null; sessionFresh = false; sessionBusy = sessionActionBusy = false;
    sessionSampleReceipt = sessionQualityReceipt = null;
    $('session-quality-label').value = '';
    $('session-quality-result').hidden = true;
    sampleFrame = null;
    $('session-sample-finite').checked = false;
    $('session-sample-steps').value = '';
    sessionImageKey = '';
    if (sessionImageURL) URL.revokeObjectURL(sessionImageURL);
    sessionImageURL = null;
    $('session-image').removeAttribute('src');
    $('session-frame').hidden = true;
    $('session-content').hidden = true;
    $('session-receipt-detail').hidden = true;
    $('session-sample-result').hidden = true;
    message('session-receipt-message', '');
    message('session-message', '只读取宿主记录，不会采集新画面。');
    $('session-request-id').value = sessionPending()[0]?.request_id || '';
    renderSessionControls();
  }
  function sessionSupported() { return !!nodeInfo?.capabilities?.includes('jobs.session-control-v1'); }
  const isSessionJobID = value => typeof value === 'string' && /^[0-9a-f]{12}$/.test(value);
  const sessionRecoveryChainSupported = () => nodeInfo?.capabilities?.includes('jobs.session-recovery-chain-v1') === true;
  const sessionRecoveryCandidate = () => currentJob?.bin === 'phonefarm' && currentJob.argv?.[0] === 'gray-layer' &&
    ['session', 'session-recover'].includes(currentJob.argv?.[1]);
  function sessionRecoveryRequest(id = selected) {
    if (!sessionRecoveries.has(id)) {
      let saved = null;
      try { saved = JSON.parse(sessionStorage.getItem(`hgamey.session-recovery.${id}`) || 'null'); } catch (_) { /* The displayed ID can still be queried manually. */ }
      sessionRecoveries.set(id, saved && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(saved.request_id) ? {
        request_id: saved.request_id, target: saved.target || null, job_id: isSessionJobID(saved.job_id) ? saved.job_id : null,
        // Older requests were submitted to their storage-key entry, never to a freshly discovered leaf.
        target_job_id: Object.hasOwn(saved, 'target_job_id') ? saved.target_job_id : id,
        source_job_id: saved.source_job_id || null,
        previous_job_id: isSessionJobID(saved.previous_job_id) ? saved.previous_job_id : null, prepared: saved.prepared === true,
        rejection: saved.rejection || null, previous_requests: Array.isArray(saved.previous_requests) ? saved.previous_requests : []
      } : null);
    }
    return sessionRecoveries.get(id);
  }
  function keepSessionRecovery(id, record) {
    sessionRecoveries.set(id, record);
    try { sessionStorage.setItem(`hgamey.session-recovery.${id}`, JSON.stringify({request_id: record.request_id, target: record.target,
      target_job_id: record.target_job_id, source_job_id: record.source_job_id || null, job_id: record.job_id || null,
      previous_job_id: record.previous_job_id || null, prepared: record.prepared === true,
      rejection: record.rejection || null, previous_requests: record.previous_requests || []})); }
    catch (_) { /* Keep the same request ID visible even if browser storage is unavailable. */ }
  }
  function sessionRecoveryProblem() {
    if (!currentJob || currentJob.id !== selected) return '请先读取原任务状态。';
    if (sessionRecoveryChainSupported()) {
      if (sessionPlanBusy) return '正在读取恢复计划，请稍候。';
      if (!sessionPlanFresh || !sessionRecoveryPlan) return '请先读取最新恢复计划；旧计划不能作为新尝试的资格。';
      if (!sessionRecoveryPlan.supported || !sessionRecoveryPlan.eligible) return sessionRecoveryPlan.reason || '节点报告当前不能继续恢复，请核查任务与收据。';
      return '';
    }
    if (storageFailed(currentJob) || currentJob.phase === 'finalizing') return '原任务结束记录尚未确认保存，请先处理结果保存。';
    if (currentJob.state !== 'finished' || currentJob.queue?.state !== 'blocked') return '原任务须已结束并保留资源；请先刷新任务核对状态。';
    const hostProblem = hostCompletionProblem(currentJob, true);
    if (hostProblem) return hostProblem;
    if (!sessionFresh || sessionBusy || !sessionState) return '请先成功读取最新会话状态。';
    if (sessionState.phase !== 'recovery_required') return '当前会话没有报告需要继续恢复。';
    if (sessionState.live_owner !== false) return '原会话 owner 尚未确认退出，不能另建恢复任务。';
    return '';
  }
  function recoveryJobLink(id, target, label = id) {
    const link = el('a', label);
    link.href = HGTarget.url(`${target?.entry || location.origin}/dash/jobs#job=${encodeURIComponent(id)}`, target || null);
    link.addEventListener('click', event => {
      if (event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || HGTarget.problem(target || null)) return;
      event.preventDefault(); selectCurrentJob(id); $('detail-heading').focus();
    });
    return link;
  }
  function renderSessionRecoveryPlan() {
    const chain = sessionRecoveryChainSupported();
    $('session-recovery-plan-refresh').hidden = !chain;
    $('session-recovery-plan-refresh').disabled = !currentJob || sessionPlanBusy || sessionRecovering.has(selected) || !!HGTarget.problem();
    $('session-recovery-plan-refresh').setAttribute('aria-busy', String(sessionPlanBusy));
    $('session-recovery-plan-refresh').textContent = sessionPlanBusy ? '读取计划中…' : sessionRecoveryPlan ? '刷新恢复计划' : '读取恢复计划';
    $('session-recovery-plan').hidden = !chain;
    const plan = sessionRecoveryPlan;
    message('session-recovery-plan-message', sessionPlanBusy ? '只读取宿主恢复计划，不执行设备操作…' : sessionPlanError ||
      (!plan ? '尚未读取恢复链。先核对原会话来源和当前阻断叶子。' : !sessionPlanFresh ? '此计划已失效，请重新读取后再创建尝试。' :
        plan.eligible ? '当前计划允许接续；提交时节点会再次核查唯一叶子。' : plan.reason || '当前不可接续，请核查任务及恢复收据。'), !!sessionPlanError || !!plan && !plan.eligible);
    $('session-recovery-plan-time').textContent = sessionPlanReadAt == null ? '' : `计划读取于 ${time(sessionPlanReadAt)}${sessionPlanFresh ? '' : ' · 上次结果，当前资格未确认'}`;
    $('session-recovery-plan-info').replaceChildren(); $('session-recovery-attempts').replaceChildren();
    if (!plan) return;
    for (const [label, id] of [['查询入口', plan.job_id], ['原会话来源', plan.source_job_id], ['当前恢复目标', plan.target_job_id]]) {
      const value = el('dd'); value.append(isSessionJobID(id) ? recoveryJobLink(id, sessionPlanTarget) : el('span', '未确认'));
      $('session-recovery-plan-info').append(el('dt', label), value);
    }
    for (const [label, value] of [['会话 ID', plan.session_id], ['设备', plan.serial], ['继承设备范围', scopeText({scope: plan.device_binding ? 'device' : 'global', binding: plan.device_binding})],
      ...(plan.device_binding ? bindingFacts(plan.device_binding) : []), ['原来源内的相对路径', plan.session_path]])
      $('session-recovery-plan-info').append(el('dt', label), el('dd', text(value)));
    for (const attempt of plan.attempts) {
      const item = el('li', null, 'lease-receipt');
      const heading = el('h4'); heading.append(recoveryJobLink(attempt.job_id, sessionPlanTarget, `恢复任务 ${attempt.job_id}`));
      item.append(heading, el('p', `恢复目标 ${text(attempt.recovery_for)} · 调度 ${attempt.queue_state === 'blocked' ? '保留资源，待核查' : text(attempt.queue_state)}`, 'note'),
        el('p', `请求 ID：${text(attempt.request_id)}`, 'request-id'));
      $('session-recovery-attempts').append(item);
    }
    if (!plan.attempts.length) $('session-recovery-attempts').append(el('li', '尚无此来源的恢复任务记录。', 'note'));
  }
  async function loadSessionRecoveryPlan() {
    if (!selected || !currentJob || !sessionRecoveryChainSupported() || sessionPlanBusy) return null;
    const id = selected, revision = ++sessionPlanRevision, target = HGTarget.binding();
    sessionPlanBusy = true; sessionPlanFresh = false; sessionPlanError = ''; renderSessionRecovery();
    try {
      const plan = await json(`${API}/jobs/${id}/session/recovery`, target);
      if (selected !== id || revision !== sessionPlanRevision) return null;
      HGTarget.assert(target);
      if (plan.schema_version !== 1 || plan.node_id !== target.node_id || plan.job_id !== id ||
          typeof plan.supported !== 'boolean' || typeof plan.eligible !== 'boolean' || !Array.isArray(plan.attempts) ||
          plan.attempts.some(attempt => !isSessionJobID(attempt.job_id) || !isSessionJobID(attempt.recovery_for)) ||
          plan.eligible && (!plan.supported || !isSessionJobID(plan.source_job_id) || !isSessionJobID(plan.target_job_id)))
        throw new Error('恢复计划身份或格式未确认，不能据此创建任务。');
      sessionRecoveryPlan = plan; sessionPlanTarget = target; sessionPlanFresh = true; sessionPlanReadAt = Date.now() / 1000;
      return plan;
    } catch (error) {
      if (selected === id && revision === sessionPlanRevision) sessionPlanError = `${errorText(error)} ${error.payload?.error || error.payload?.reason || ''} 新尝试资格未确认。`;
      return null;
    } finally { if (selected === id && revision === sessionPlanRevision) { sessionPlanBusy = false; renderSessionRecovery(); } }
  }
  function renderSessionRecovery() {
    const record = currentJob && sessionRecoveryRequest(), pending = sessionRecovering.has(selected);
    const supported = nodeInfo?.capabilities?.includes('jobs.session-recovery-v1'), chain = sessionRecoveryChainSupported();
    const targetIssue = record ? HGTarget.problem(record.target || null) || (!isSessionJobID(record.target_job_id) ? '原恢复入口身份未确认，不能查询或重投。' : '') : HGTarget.problem();
    $('session-recover-panel').hidden = !currentJob || (!record && sessionState?.phase !== 'recovery_required' && !(chain && sessionRecoveryCandidate()));
    renderSessionRecoveryPlan();
    $('session-recover-submit').hidden = !supported || !!record?.job_id || !!record?.rejection;
    $('session-recover-submit').disabled = !!targetIssue || !supported || !access?.steer || !HGTarget.canWrite() || accessBusy || pending || !!record?.job_id || !!record?.rejection ||
      (!record && (!!sessionRecoveryProblem() || !globalThis.crypto?.randomUUID));
    $('session-recover-submit').textContent = pending ? '正在核对恢复请求…' : record?.prepared ? '提交新恢复尝试' : record ? '使用原请求 ID 重试' : '提交恢复任务';
    $('session-recover-submit').setAttribute('aria-busy', String(pending));
    $('session-recover-query').hidden = !record;
    $('session-recover-query').disabled = pending || !!targetIssue;
    $('session-recover-query').setAttribute('aria-busy', String(pending));
    $('session-recover-job').hidden = !record?.job_id || !record?.target;
    if (record?.job_id) $('session-recover-job').href = HGTarget.url(`${record.target?.entry || location.origin}/dash/jobs#job=${encodeURIComponent(record.job_id)}`, record.target || null);
    $('session-recover-new').hidden = !supported || !(record?.job_id || chain && record?.rejection);
    $('session-recover-new').disabled = !!targetIssue || !supported || !access?.steer || !HGTarget.canWrite() || accessBusy || pending || sessionPlanBusy ||
      (chain && sessionPlanFresh && sessionRecoveryPlan?.eligible === false) || (!chain && !!sessionRecoveryProblem()) || !globalThis.crypto?.randomUUID;
    $('session-recover-new').setAttribute('aria-busy', String(pending));
    $('session-recover-previous').hidden = !record?.previous_job_id || !record?.target;
    if (record?.previous_job_id) $('session-recover-previous').href = HGTarget.url(`${record.target?.entry || location.origin}/dash/jobs#job=${encodeURIComponent(record.previous_job_id)}`, record.target || null);
    $('session-recover-request').textContent = record ? `恢复请求 ID：${record.request_id}\n固定提交入口：${text(record.target_job_id)}${record.source_job_id ? ` · 原会话来源：${record.source_job_id}` : ''}` : '';
    let help = targetIssue;
    if (!help && !supported) help = '此节点未提供异步会话恢复能力；仍可查看已有恢复记录。';
    else if (!help && record?.job_id) help = chain ? '恢复任务已记录。新尝试会重新读取节点计划并冻结当前叶子；失败任务的资源保留不要求提前释放。提交成功不代表恢复完成。' :
      `恢复任务已记录；旧节点未提供恢复链计划。新尝试前仍须核对上一任务已结束且调度资源已释放。${sessionRecoveryProblem()}`;
    else if (!help && record?.rejection) help = `节点已明确拒绝本次新请求（${record.rejection.code}）。原 ID 和入口保留；如需继续，请明确点击“新建恢复尝试”核对新计划。`;
    else if (!help && record?.prepared) help = '新请求 ID 与提交入口已固定，尚未提交。刷新计划不会改投；提交时节点会再次核对当前叶子。';
    else if (!help && record) help = '结果不确定时先查询原请求；明确重试仍使用同一 ID、节点和入口，不随新计划改投。';
    else if (!help && (!access?.steer || !HGTarget.canWrite() || accessBusy)) help = '请先解锁任务操作权限。';
    else if (!help) help = sessionRecoveryProblem() || (!globalThis.crypto?.randomUUID ? '请通过 HTTPS 或本机地址提交。' : '按上方恢复目标提交异步任务，原会话和失败记录保留。');
    message('session-recover-help', help);
    message('session-recover-result', record?.message || (record?.rejection ? record.rejection.message : record ? '可按原请求 ID 查询节点中的最新任务记录。' : ''), !!record?.error || !!record?.rejection);
    const previous = record?.previous_requests || [];
    $('session-recovery-previous-requests').hidden = !previous.length;
    $('session-recovery-request-history').replaceChildren(...previous.map(item => {
      const row = el('li', null, 'lease-receipt'); row.append(el('p', `请求 ${item.request_id} · 固定入口 ${text(item.target_job_id)}`, 'request-id'));
      if (item.job_id && item.target) row.append(recoveryJobLink(item.job_id, item.target, `恢复任务 ${item.job_id}`));
      if (item.rejection) row.append(el('p', `${text(item.rejection.code)}：${text(item.rejection.message)}`, 'note'));
      return row;
    }));
  }
  function acceptSessionRecovery(id, record, job) {
    if (!isSessionJobID(job?.id) || job.recovery_for !== record.target_job_id) throw new Error('恢复任务回包未匹配本次固定入口，结果尚未确认。');
    keepSessionRecovery(id, {...record, rejection: null, prepared: false, job_id: job.id, message: `恢复任务 ${job.id} · ${status(job)[0]}。提交不代表恢复完成，请核对任务记录。`, error: false});
  }
  async function submitSessionRecovery() {
    if (!currentJob || $('session-recover-submit').disabled) return;
    const id = selected, existing = sessionRecoveryRequest(id), chain = sessionRecoveryChainSupported();
    const record = {...(existing || {request_id: crypto.randomUUID(), target: HGTarget.binding(),
      target_job_id: chain ? sessionRecoveryPlan.target_job_id : id, source_job_id: chain ? sessionRecoveryPlan.source_job_id : id}), prepared: false};
    keepSessionRecovery(id, {...record, message: '正在提交恢复任务；请求 ID、节点与入口已固定。', error: false});
    sessionRecovering.add(id); renderSessionRecovery();
    try {
      acceptSessionRecovery(id, record, await post(`${API}/jobs/${record.target_job_id}/session/recover`, {}, {'X-Request-ID': record.request_id}, record.target || null));
    } catch (error) {
      const code = error.payload?.code;
      const rejection = error.status === 409 && ['session_recovery_leaf', 'session_recovery_blocked'].includes(code) ?
        {code, message: errorText(error), target_job_id: error.payload?.target_job_id || null} : null;
      keepSessionRecovery(id, {...record, rejection, message: rejection ? `${rejection.message} 原请求未创建任务，入口未改投。` :
        `${errorText(error)} 请先查询此恢复请求；重试仍使用原 ID 和入口，不会自动重发。`, error: true});
      if (error.status === 401 || error.status === 403) await loadAccess();
    } finally { sessionRecovering.delete(id); if (selected === id) { sessionPlanFresh = false; renderSessionRecovery(); } }
  }
  async function querySessionRecovery() {
    const id = selected, record = sessionRecoveryRequest(id);
    if (!record || sessionRecovering.has(id)) return;
    sessionRecovering.add(id); renderSessionRecovery();
    try {
      acceptSessionRecovery(id, record, await json(`${API}/requests/${encodeURIComponent(record.request_id)}`, record.target || null));
    } catch (error) {
      keepSessionRecovery(id, {...record, message: error.status === 404 ? '暂未找到此恢复请求。保留原 ID 和入口，可稍后查询或明确同 ID 重试；不会自动新建请求。' : `${errorText(error)} 保留原恢复请求 ID，请核对后再继续。`, error: true});
    } finally { sessionRecovering.delete(id); if (selected === id) renderSessionRecovery(); }
  }
  async function prepareSessionRecoveryAttempt() {
    if (!currentJob || $('session-recover-new').disabled) return;
    const id = selected, record = sessionRecoveryRequest(id), revision = detailRevision;
    sessionRecovering.add(id); renderSessionRecovery();
    try {
      let targetJob = id, sourceJob = id;
      if (sessionRecoveryChainSupported()) {
        const plan = await loadSessionRecoveryPlan();
        if (selected !== id || detailRevision !== revision) return;
        if (!plan) throw new Error(sessionPlanError || '恢复计划未确认，原请求保留。');
        if (!plan.supported || !plan.eligible) throw new Error(plan.reason || '当前不能继续恢复。');
        targetJob = plan.target_job_id; sourceJob = plan.source_job_id;
      } else {
        const previous = await json(`${API}/jobs/${record.job_id}`, record.target || null);
        if (selected !== id || detailRevision !== revision) return;
        if (previous?.id !== record.job_id || previous.recovery_for !== record.target_job_id) throw new Error('上次恢复任务身份未确认，不能生成新请求 ID。');
        if (previous.state !== 'finished') throw new Error('上次恢复任务尚未结束或状态未知，不能生成新请求 ID。');
        if (previous.queue?.state !== 'done') throw new Error(previous.queue?.state === 'blocked' ? '旧节点无法接续阻断的恢复子任务。请打开上一任务核查；未完成恢复时不能释放，不能绕过。' : '上次恢复任务的调度结束状态未确认，不能生成新请求 ID。');
        const problem = sessionRecoveryProblem(); if (problem) throw new Error(problem);
      }
      const previousRequests = [...(record.previous_requests || []), {request_id: record.request_id, target: record.target,
        target_job_id: record.target_job_id, job_id: record.job_id || null, rejection: record.rejection || null}];
      keepSessionRecovery(id, {request_id: crypto.randomUUID(), target: HGTarget.binding(), target_job_id: targetJob, source_job_id: sourceJob,
        previous_job_id: record.job_id || record.previous_job_id || null, previous_requests: previousRequests, prepared: true,
        message: `新请求 ID 已准备，固定入口 ${targetJob}，尚未提交；旧请求与任务记录仍保留。`, error: false});
    } catch (error) {
      keepSessionRecovery(id, {...record, message: `${errorText(error)} 原请求 ID 保留，未创建新尝试。`, error: true});
    } finally { sessionRecovering.delete(id); if (selected === id) renderSessionRecovery(); }
  }
  function sessionAllowed(action) {
    const controller = sessionState?.controller;
    const actor = $('session-actor').value.trim();
    const pending = sessionPending();
    return !!(sessionFresh && !sessionBusy && !sessionActionBusy && sessionSupported() && access?.steer && HGTarget.canWrite() && !accessBusy &&
      pending.every(p => !HGTarget.problem(p.target || null)) && (!referenceRun || action === 'stop') && sessionState?.live_owner && sessionState.accepting && sessionState.allowed_actions?.includes(action) &&
      (sessionState.phase !== 'measuring' || action === 'stop') &&
      (['take_control', 'stop'].includes(action) || controller?.actor_id === actor) &&
      (!pending.length || action === 'stop' && !pending.some(p => p.action === 'stop')) && /^[A-Za-z0-9_-]{1,64}$/.test(actor) && globalThis.crypto?.randomUUID);
  }
  function sampleFrameFresh(frame) {
    if (!frame || frame.rotation == null) return false;
    const age = Date.now() - new Date(typeof frame.captured_at === 'number' ? frame.captured_at * 1000 : frame.captured_at).getTime();
    return Number.isFinite(age) && age >= 0 && age <= 30000;
  }
  function sampleInputIssue() {
    if (sessionState?.action_capabilities?.sample !== 2) return '当前会话仅支持无输入采样；请关闭有限输入选项。';
    if (!sampleFrame) return '尚未绑定原图。请先明确采集原图，再绑定并核对坐标。';
    const latest = sessionState?.latest_image;
    if (!latest || ['request_id', 'width', 'height', 'rotation'].some(key => latest[key] !== sampleFrame[key])) return '当前原图已变化。核对步骤坐标后，请明确重新绑定；不会自动缩放坐标。';
    if (!sampleFrameFresh(sampleFrame)) return '绑定原图已过期或方向未知。请明确采集新原图，再重新绑定；要求 30 秒内的原图。';
    return '';
  }
  function bindSampleFrame() {
    if (sessionFresh && sessionState?.action_capabilities?.sample === 2 && sampleFrameFresh(sessionState.latest_image)) sampleFrame = {...sessionState.latest_image};
    renderSessionControls();
  }
  function renderSampleInput() {
    const finite = $('session-sample-finite').checked, capable = sessionState?.action_capabilities?.sample === 2;
    const measuring = sessionState?.phase === 'measuring';
    const paused = sessionActionBusy || measuring;
    $('session-sample-finite').disabled = paused || (!capable && !finite);
    $('session-finite-fields').hidden = !finite;
    $('session-sample-steps').disabled = paused;
    $('session-sample-example').disabled = paused;
    $('session-sample-rebind').disabled = paused || !sessionFresh || !capable || !sampleFrameFresh(sessionState?.latest_image);
    message('session-finite-help', capable ? '默认无输入。开启后只执行明确填写的有限步骤；不会自动采图或还原游戏视角。' : '此会话未提供有限输入采样能力；仍可使用无输入采样。');
    const issue = finite && !measuring ? sampleInputIssue() : '';
    const frameText = sampleFrame ? `绑定原图 ${sampleFrame.request_id} · ${sampleFrame.width} × ${sampleFrame.height} · 方向 ${sampleFrame.rotation} · ${time(sampleFrame.captured_at)}。` : '';
    message('session-sample-frame', `${frameText}${measuring ? '本次计划使用此原图；输入几何核验记录见原件。' : issue || '输入前 owner 会再次核验原图新鲜度、尺寸、方向及坐标。'}`, !!issue);
    const seconds = Number($('session-sample-seconds').value);
    message('session-sample-plan', `支持 sleep、tap、swipe、repeat。声明等待与滑动总时长须至少 ${Number.isInteger(seconds) && seconds >= 1 && seconds <= 120 ? seconds + 10 : '采样秒数 + 10'} 秒、最多 600 秒；不会自动补等待。实际耗时另记，完整计划由 owner 校验。`);
    return !issue;
  }
  function renderSessionControls() {
    renderSessionRecovery();
    $('session-panel').hidden = !currentJob || currentJob.bin !== 'phonefarm';
    $('session-refresh').disabled = !currentJob || !sessionSupported() || sessionBusy;
    $('session-refresh').setAttribute('aria-busy', String(sessionBusy));
    $('session-refresh').textContent = sessionBusy ? '读取中…' : sessionState ? '刷新已存状态' : '读取会话状态';
    if (currentJob?.bin === 'phonefarm' && !sessionSupported()) message('session-message', nodeInfo ? '此节点尚未提供交互会话能力；已有任务记录仍可查看。' : '正在核对节点会话能力…');
    document.querySelectorAll('[data-session-action]').forEach(button => {
      button.disabled = !sessionAllowed(button.dataset.sessionAction);
      button.setAttribute('aria-busy', String(sessionActionBusy && button.dataset.sessionAction === $('session-panel').dataset.pendingAction));
    });
    const kind = $('session-input-kind').value;
    $('session-input-submit').disabled = !sessionAllowed(kind) || !sessionState?.latest_image;
    $('session-input-submit').textContent = kind === 'tap' ? '发送点击' : '发送滑动';
    $('session-swipe-fields').hidden = kind !== 'swipe';
    const storedAction = sessionPending().find(p => p.request_id === $('session-request-id').value.trim());
    $('session-query').disabled = !currentJob || !sessionSupported() || sessionActionBusy || !!(storedAction && HGTarget.problem(storedAction.target || null));
    const measuring = sessionState?.phase === 'measuring';
    const samplePosting = sessionActionBusy && $('session-panel').dataset.pendingAction === 'sample';
    const inputReady = renderSampleInput();
    $('session-sample-submit').disabled = !sessionAllowed('sample') || !inputReady;
    $('session-sample-submit').setAttribute('aria-busy', String(samplePosting));
    $('session-sample-submit').textContent = samplePosting ? '投递中…' : measuring ? '采样进行中' : '开始采样';
    $('session-sample-seconds').disabled = measuring || sessionActionBusy;
    $('session-render-comm').disabled = measuring || sessionActionBusy;
    message('session-sample-help', !sessionState?.allowed_actions?.includes('sample') && !measuring ? '当前会话未提供采样动作；旧节点或模式保持不可用。' :
      measuring ? '正在测量，显示的是先前采集的画面。其他截图、输入及灰层动作暂停；可请求停止，当前有界计划结束后收尾。' :
      '1–120 秒，默认 30 秒。线程筛选留空仍采原始数据，帧相关指标保持未知。提交只表示入队，请按原请求 ID 查询结果。');
    renderQualityControls();
    renderReferenceControls();
    const pending = sessionPending();
    message('session-pending', (storedAction && HGTarget.problem(storedAction.target || null)) || (pending.length ? `未决请求：${pending.map(v => v.request_id).join('、')}。先查询回执；不会自动重发输入。` : '回执读取不触发设备动作。accepted 仅表示入队，completed 才表示该动作结束。'));
    message('session-action-help', !access?.steer ? '查看权限只能读状态和已有画面；采集或输入需先解锁任务操作。' :
      !sessionFresh ? '状态尚未取得或读取失败；刷新成功前暂停新动作。' :
      sessionState?.phase === 'closed' ? '会话已关闭。请核对限定范围恢复报告，刷新任务后从现有入口核查并解除资源保留。' :
      !sessionState?.live_owner ? '会话 owner 已不存活；不能继续输入，请核对恢复上下文。' :
      !sessionState.accepting ? '会话已停止接收新动作。请核对恢复记录；关闭后刷新任务以处理资源保留。' :
      sessionState.phase === 'measuring' ? '测量中只执行本次计划，暂停其他截图、输入及灰层动作。旧图与原采集时间保留；停止请求会在安全边界处理。' :
      pending.length ? '先核对未决请求。仍可请求停止并恢复；结果未知的输入不会自动重放。' :
      sessionState.controller?.actor_id !== $('session-actor').value.trim() ? '当前输入属于其他控制者。请先申请接管，再执行截图、输入或灰层动作。' :
      '所有动作由原会话 owner 串行执行；仅开放服务报告可用的动作。');
  }
  function renderSessionState() {
    const s = sessionState;
    if (!s) return;
    $('session-content').hidden = false;
    $('session-phase').textContent = SESSION_PHASES[s.phase] || text(s.phase);
    $('session-phase').className = `task-state ${s.phase === 'ready' ? 'running' : ['recovery_required', 'restoring'].includes(s.phase) ? 'unknown' : ''}`;
    $('session-updated').textContent = `状态更新 ${time(s.updated_at)} · ${readTime()}`;
    const c = s.controller || {};
    facts('session-info', [['会话', s.session_id], ['设备 / 模式', `${text(s.serial)} · ${text(s.mode)}`], ['目标', s.package],
      ['实际 lease 范围', scopeText({scope: s.lease_scope || (s.device_binding ? 'device' : 'global'), binding: s.device_binding})], ...(s.device_binding ? bindingFacts(s.device_binding) : []),
      ['当前控制者', `${text(c.actor_id)} · ${text(c.kind)} · epoch ${text(c.epoch)}`], ['接管到期', time(c.expires_at)], ['资源预算截止', time(s.deadline_at)], ['owner 存活', s.live_owner ? '是（宿主锁核验）' : '否']]);
    const recovery = s.recovery || {};
    const recoveryState = ({not_reported: '尚未报告恢复', required: '需要继续恢复', reported_complete: '已报告限定范围恢复；仍需核查任务资源保留'})[recovery.status] || text(recovery.status);
    facts('session-recovery', [['恢复状态', recoveryState], ['上下文', recovery.session_path], ['报告', (recovery.reports || []).join('\n') || '尚无'], ['错误', recovery.error || '无']]);
    const power = s.power || {};
    const stage = ({not_started: '尚未准备', observed_no_mutation: '已观测放电 · 未修改控制', recorded: '已绑定原值', suspended: '充电控制已暂停', restoring: '充电控制恢复中', restored: '充电控制已恢复', unmodified: '未修改充电控制'})[power.stage] || '未知';
    $('session-power-summary').textContent = `固定供电策略 · ${powerPolicy(power.policy)} · ${stage}`;
    facts('session-power-info', [['固定策略', powerPolicy(power.policy)], ['准备 / 恢复阶段', stage], ['本会话修改控制', typeof power.control_modified_by_session === 'boolean' ? power.control_modified_by_session ? '是' : '否' : '未知'],
      ['初始观测', powerObservation(power.initial)], ['最近观测', powerObservation(power.last_observation)]]);
    renderSampleResult(sessionSampleReceipt || {result: s.latest_sample}, !sessionSampleReceipt);
    if (!$('session-quality-label').value && s.latest_quality?.label) $('session-quality-label').value = s.latest_quality.label;
    renderQualityResult(sessionQualityReceipt?.result?.quality || s.latest_quality, !sessionQualityReceipt);
    const frame = s.latest_image;
    $('session-image-empty').hidden = !!frame;
    if (frame) {
      const when = new Date(typeof frame.captured_at === 'number' ? frame.captured_at * 1000 : frame.captured_at).getTime();
      const age = Number.isFinite(when) ? `${Math.max(0, Math.floor((Date.now() - when) / 1000))} 秒前` : '时间未知';
      $('session-image-info').textContent = `${frame.width} × ${frame.height} · 方向 ${text(frame.rotation)} · 原始 PNG · 采集于 ${time(frame.captured_at)}（读取时 ${age}）· frame ${frame.request_id}`;
      $('session-image-link').href = jobURL('/out', {path: frame.path});
      $('session-image-link').download = '';
    }
  }
  async function loadSession() {
    if (!selected || !sessionSupported() || sessionBusy) return;
    const id = selected, revision = ++sessionRevision;
    sessionBusy = true; sessionFresh = false;
    renderSessionControls();
    message('session-message', '正在读取会话与已有画面，不采集新图…');
    try {
      const state = await json(`${API}/jobs/${id}/session`);
      if (selected !== id || revision !== sessionRevision) return;
      if (state.schema !== 'phonefarm/gray-control@1' || state.job_id !== id || typeof state.live_owner !== 'boolean') throw new Error('会话身份或状态格式不正确。');
      sessionState = state; sessionFresh = true;
      renderSessionState();
      message('session-message', '显示宿主已存状态。采集原图需要单独提交动作。');
      const frame = state.latest_image;
      if (frame && frame.path !== sessionImageKey) {
        const blob = await request(`${API}/jobs/${id}/out?${new URLSearchParams({path: frame.path})}`, r => r.blob());
        if (selected !== id || revision !== sessionRevision) return;
        if (sessionImageURL) URL.revokeObjectURL(sessionImageURL);
        sessionImageURL = URL.createObjectURL(blob); sessionImageKey = frame.path;
        $('session-image').src = sessionImageURL;
        $('session-frame').hidden = false;
      }
      if (!frame) $('session-frame').hidden = true;
    } catch (error) {
      if (selected === id && revision === sessionRevision) {
        sessionFresh = false;
        message('session-message', error.status === 404 ? '此任务尚未产生灰层控制会话，或会话记录不可用。' : `${errorText(error)} 已显示的状态/画面是上次读取结果。`, true);
      }
    } finally { if (selected === id && revision === sessionRevision) { sessionBusy = false; renderSessionControls(); } }
  }
  function showSessionReceipt(receipt) {
    $('session-receipt-detail').hidden = false;
    $('session-receipt').textContent = JSON.stringify(receipt, null, 2);
    message('session-receipt-message', `${receipt.request_id}：${ACTION_STATES[receipt.status] || '状态未知。'}${receipt.error ? ' ' + (typeof receipt.error === 'string' ? receipt.error : JSON.stringify(receipt.error)) : ''}`, ['failed', 'unknown'].includes(receipt.status));
    sessionSampleReceipt = receipt.result?.collection && !receipt.result?.quality ? receipt : null;
    sessionQualityReceipt = receipt.result?.quality ? receipt : null;
    renderQualityResult(sessionQualityReceipt?.result?.quality || sessionState?.latest_quality, !sessionQualityReceipt);
    renderSampleResult(sessionSampleReceipt || {result: sessionState?.latest_sample}, !sessionSampleReceipt);
  }
  function renderQualityControls() {
    const label = $('session-quality-label').value.trim(), capable = sessionState?.action_capabilities?.quality === 1;
    const valid = /^[A-Za-z0-9_-]{1,64}$/.test(label), latest = sessionState?.latest_quality;
    const known = latest?.label === label, phase = known ? latest.phase : undefined;
    const posting = sessionActionBusy && $('session-panel').dataset.pendingAction === 'quality';
    $('session-quality-label').disabled = sessionActionBusy;
    for (const [op, predecessor] of [['begin', null], ['checkpoint', 'begun'], ['finish', 'checkpointed']]) {
      const button = $(`session-quality-${op}`);
      button.disabled = !capable || !valid || !sessionAllowed('quality') || !!latest?.active_attempt || (known && phase !== predecessor);
      button.setAttribute('aria-busy', String(posting && $('session-panel').dataset.qualityOp === op));
    }
    message('session-quality-help', !capable ? '当前会话未提供画质检查点能力；旧 owner 保持不可用。' :
      !valid ? '填写 1–64 位字母、数字、下划线或短横线标签。' :
      latest?.active_attempt ? '当前检查点存在未决采集尝试。请查询原请求回执，不换 ID 重复采集。' :
      known && phase === 'collected' ? '此标签三个阶段已采集齐全；已有阶段不可覆盖。新一组采集需使用新标签。' :
      known ? '按成功阶段继续。失败且回执已明确后，可用相应按钮提交新的采集请求；旧失败原件保留。' :
      '此标签的历史阶段未在当前摘要中提供，由 owner 核验顺序；已有标签不能再次开始。', false);
  }
  function renderQualityResult(quality, fromCache = false) {
    $('session-quality-result').hidden = !quality;
    if (!quality) return;
    const phase = ({begun: '开始图已采集', checkpointed: '中间图已采集', collected: '三个阶段已采集齐全'})[quality.phase] || '尚无成功阶段';
    const last = quality.last_attempt, active = quality.active_attempt;
    const error = value => typeof value === 'string' ? value : value?.message || value?.code || '';
    message('session-quality-status', `${quality.label} · ${phase}${active ? ' · 存在未决采集尝试' : ''}${last?.status === 'failed' ? ` · 最近尝试失败：${error(last.error) || '原因未提供'}` : ''}。`, last?.status === 'failed');
    facts('session-quality-info', [['来源', fromCache ? '会话共享记录 · 最近标签' : '当前查询的动作回执'], ['记录更新', time(quality.updated_at)], ['未决采集请求', active?.request_id || '无'], ['最近请求', last?.request_id || '尚无']]);
    const addLink = (list, name, path) => {
      if (typeof path !== 'string' || !path || path.startsWith('/') || /[\u0000-\u001f\u007f]/.test(path) || path.split('/').some(p => !p || p === '.' || p === '..')) return;
      const item = el('li'), link = el('a', name); link.href = jobURL('/out', {path}); link.download = ''; item.append(link); list.append(item);
    };
    const artifacts = $('session-quality-artifacts'); artifacts.replaceChildren();
    addLink(artifacts, '共享清单 quality.json', quality.path);
    const stages = $('session-quality-stages'); stages.replaceChildren();
    const records = [['begin', '1 · 开始图'], ['checkpoint', '2 · 中间图'], ['finish', '3 · 最终图']].map(([op, title]) => ({title, attempt: quality.stages?.[op]}));
    if (last?.status === 'failed') records.push({title: '最近失败尝试 · 原件保留', attempt: last});
    for (const {title, attempt} of records) {
      const card = el('div', null, 'session-quality-stage'); card.append(el('h5', title));
      if (!attempt) { card.append(el('p', '尚未采集成功。', 'note')); stages.append(card); continue; }
      card.append(el('p', `${text(attempt.actor_id)} · epoch ${text(attempt.control_epoch)} · 请求 ${text(attempt.request_id)} · ${time(attempt.finished_at || attempt.started_at)}`, 'note'));
      if (attempt.error) card.append(el('p', error(attempt.error), 'note error'));
      const image = attempt.image, preview = attempt.preview, marker = attempt.marker;
      if (image) card.append(el('p', `原图 ${image.width} × ${image.height} · 方向 ${text(image.rotation)} · 采集于 ${time(image.captured_at)}`, 'note'));
      if (preview) card.append(el('p', `半尺寸预览 ${preview.width} × ${preview.height} · 派生图 · ${preview.method === 'stride2_rgb_even_coordinates' ? '取原图偶数坐标 RGB，非原图' : text(preview.method)}`, 'note'));
      if (marker) card.append(el('p', `marker 观测于 ${time(marker.observed_at)}；与截图分开记录，不保证原子同帧。`, 'note'));
      const links = el('ul', null, 'session-sample-artifacts');
      addLink(links, '原始 PNG', image?.path); addLink(links, '半尺寸预览 PNG', preview?.path); addLink(links, 'marker 原件', marker?.path); addLink(links, '本次尝试记录', attempt.path);
      card.append(links); stages.append(card);
    }
  }
  function powerPolicy(policy) { return ({observe: 'observe · 仅观察', battery: 'battery · 电池条件化'})[policy] || '未知'; }
  function powerObservation(value) {
    if (!value) return '未知';
    const battery = value.battery || value; // Initial battery facts are flat; later observations wrap them.
    return `${value.on_battery === true ? '已观测电池放电' : value.on_battery === false ? '未观测为电池放电' : '放电状态未知'} · ${battery.status || '电池状态未知'} · 电流 ${battery.current_ua ?? '未知'} μA · 电量 ${battery.capacity_pct ?? '未知'}% · ${time(value.observed_at)}`;
  }
  function renderSampleResult(receipt, fromCache = false) {
    const result = receipt.result;
    $('session-sample-result').hidden = !result?.collection;
    if (!result?.collection) return;
    const collection = result.collection;
    const complete = collection.status === 'complete';
    message('session-sample-status', `${complete ? '采集已完成，可核对原始数据。' : collection.status === 'failed' ? '采集失败，请核对原件引用与失败原因。' : '采集状态未知，请核对报告。'}${collection.error ? ` ${collection.error}` : ''}`, !complete);
    const metric = value => typeof value === 'number' && Number.isFinite(value) ? value.toFixed(3) : '未知';
    const metrics = result.metrics || {};
    facts('session-sample-info', [['请求 ID', receipt.request_id || result.request_id || '未知'], ['请求时长', Number.isInteger(result.requested_seconds) ? `${result.requested_seconds} 秒` : '未知'],
      ['GPU submit proxy', `${metric(metrics.gpu_submit_fps)}${Number.isFinite(metrics.gpu_submit_fps) ? ' /s' : ''}`],
      ['环境采样全窗平均功率', `${metric(metrics.power_w_mean)}${Number.isFinite(metrics.power_w_mean) ? ' W' : ''}`],
      ['来源', fromCache ? `最近采样缓存 · ${result.path || '报告路径未记录'}` : '当前查询的动作回执'],
      ...(result.input_kind ? [['输入方式', ({idle: '无输入', finite: '显式有限输入'})[result.input_kind] || '未知'],
        ['声明计划时长', Number.isFinite(result.planned_input_ms) ? `${result.planned_input_ms / 1000} 秒（非实测耗时）` : '未知'],
        ['输入参考原图', result.initial_frame ? `${text(result.initial_frame.request_id)} · ${text(result.initial_frame.width)} × ${text(result.initial_frame.height)} · 方向 ${text(result.initial_frame.rotation)}` : '无']] : [])]);
    const power = result.power || {}, trace = result.metric_provenance?.trace_energy || {}, window = trace.window || {};
    message('session-sample-power-status', trace.available === true ? 'trace 窗口估计可用：由电池采样积分得到，不是游戏每帧能耗。' : `trace 窗口估计${trace.available === false ? '不可用' : '资格未知'}：${trace.reason || '未提供可核验的窗口资格记录。'}`, trace.available === false);
    const traceMetric = (value, unit) => trace.available === true && Number.isFinite(value) ? `${metric(value)} ${unit}（估计）` : '未知';
    facts('session-sample-power', [['本轮策略', powerPolicy(power.policy)], ['采样前观测', powerObservation(power.before)], ['采样后观测', powerObservation(power.after)], ['供电条件错误', power.error || (result.power ? '无记录错误' : '未知')],
      ['trace 窗口能量', traceMetric(metrics.trace_energy_j, 'J')], ['trace 窗口平均功率', traceMetric(metrics.trace_power_w_mean, 'W')],
      ['估计方法 / rail', `${trace.method || '未知'} / ${trace.rail || '未知'}`], ['trace 时间窗', window.clock === 'uptime' && Number.isFinite(window.start) && Number.isFinite(window.end) ? `uptime ${window.start}–${window.end} 秒` : '未知']]);
    const limitations = Array.isArray(result.limitations) ? result.limitations.filter(v => typeof v === 'string') : [];
    $('session-sample-limitations').replaceChildren(...limitations.map(value => el('li', value)));
    $('session-sample-limitations').hidden = !limitations.length;
    $('session-sample-artifacts').replaceChildren();
    const artifacts = [{name: '采集报告 run.json', path: result.path}, ...(Array.isArray(result.artifacts) ? result.artifacts : [])];
    const seen = new Set();
    for (const artifact of artifacts) {
      const path = artifact?.path;
      if (typeof path !== 'string' || !path || path.startsWith('/') || /[\u0000-\u001f\u007f]/.test(path) || path.split('/').some(p => !p || p === '.' || p === '..') || seen.has(path)) continue;
      seen.add(path);
      const item = el('li'), link = el('a', artifact.name || path);
      link.href = jobURL('/out', {path}); link.download = '';
      item.append(link); $('session-sample-artifacts').append(item);
    }
    if (!seen.size) $('session-sample-artifacts').append(el('li', '尚无可读取的原件引用。'));
  }
  async function querySessionReceipt() {
    const id = selected, requestId = $('session-request-id').value.trim();
    if (!/^[A-Za-z0-9_-]{1,64}$/.test(requestId) || !id || sessionActionBusy) { message('session-receipt-message', '请填写有效请求 ID。', true); return; }
    const revision = ++receiptRevision;
    busy('session-query', true, '查询中…');
    try {
      const saved = sessionPending(id).find(p => p.request_id === requestId);
      const receipt = await json(`${API}/jobs/${id}/session/actions/${encodeURIComponent(requestId)}`, saved ? saved.target || null : HGTarget.binding());
      if (selected !== id || revision !== receiptRevision) return;
      if (receipt.request_id !== requestId || receipt.schema !== 'phonefarm/gray-control-action-result@1') throw new Error('动作回执身份不匹配。');
      keepAction(id, sessionPending(id).find(p => p.request_id === requestId) || {request_id: requestId}, receipt);
      showSessionReceipt(receipt);
      await loadSession();
    } catch (error) {
      if (selected === id && revision === receiptRevision) message('session-receipt-message', `${errorText(error)} 保留原请求 ID；未找到回执不等于输入没有执行。`, true);
    } finally { if (selected === id && revision === receiptRevision) { busy('session-query', false); renderSessionControls(); } }
  }
  async function sendSessionAction(action, params = {}) {
    if (!sessionAllowed(action)) return;
    if (action === 'stop') stopReference('已请求停止会话，停止本页后续提交。');
    const id = selected, session = sessionState, revision = ++receiptRevision;
    const requestBody = {schema: 'phonefarm/gray-control-action@1', request_id: crypto.randomUUID(), session_id: session.session_id,
      actor_id: $('session-actor').value.trim(), control_epoch: session.controller.epoch, action, params};
    keepAction(id, requestBody);
    $('session-request-id').value = requestBody.request_id;
    $('session-panel').dataset.pendingAction = action;
    sessionActionBusy = true;
    renderSessionControls();
    message('session-receipt-message', `正在投递 ${requestBody.request_id}；请求已在本页固定，不自动重发。`);
    try {
      const receipt = await post(`${API}/jobs/${id}/session/actions`, requestBody);
      if (receipt.request_id !== requestBody.request_id || receipt.session_id !== session.session_id) throw new Error('动作回执身份不匹配。');
      keepAction(id, requestBody, receipt);
      if (selected === id && revision === receiptRevision) showSessionReceipt(receipt);
    } catch (error) {
      if (error.status >= 400 && error.status < 500 && error.status !== 409) keepAction(id, requestBody, {status: 'not_executed'});
      if (selected === id && revision === receiptRevision) message('session-receipt-message', `${errorText(error)} 请求 ID：${requestBody.request_id}。请先查询回执，不换 ID 重复输入。`, true);
      if (error.status === 401 || error.status === 403) await loadAccess();
    } finally {
      if (selected === id && revision === receiptRevision) { sessionActionBusy = false; renderSessionControls(); await loadSession(); }
    }
  }

  // This view is disposable. Platform receipts, not browser storage, determine group progress.
  function renderReferenceControls() {
    const active = !!referenceRun;
    const readable = !!currentJob && sessionSupported() && !active;
    $('reference-query').disabled = !readable;
    $('reference-query').setAttribute('aria-busy', String(active && !referenceRun.execute));
    $('reference-label').disabled = active;
    $('reference-fields').disabled = active;
    $('reference-start').disabled = !readable || !access?.steer || !HGTarget.canWrite() || accessBusy || sessionActionBusy || !!sessionPending().length;
    $('reference-start').setAttribute('aria-busy', String(active && referenceRun.execute));
    $('reference-start').textContent = active && referenceRun.execute ? '正在核对 / 采集…' : '采集本组参考数据';
    $('reference-stop').hidden = !active;
    $('reference-stop').disabled = !!referenceRun?.cancelled;
    $('session-actor').disabled = active;
  }
  function stopReference(reason) {
    if (!referenceRun) return;
    referenceRun.cancelled = true;
    message('reference-message', `${reason} 已发动作由 owner 收尾；请重新查询本组原回执，不会自动续跑。`, true);
    renderReferenceControls();
  }
  function referenceCurrent(run) {
    HGTarget.assert(run.target);
    if (referenceRun !== run || selected !== run.job || run.cancelled || document.hidden) throw new Error('本页编排已停止，未发送后续动作。');
  }
  function referenceTime(value) {
    return new Date(typeof value === 'number' ? value * 1000 : value).getTime();
  }
  function referenceOptions() {
    const profile = {schema: 'phonefarm/gray-reference-profile@1', package: $('reference-package').value.trim(),
      width: Number($('reference-width').value), height: Number($('reference-height').value), rotation: Number($('reference-rotation').value)};
    const seconds = Number($('reference-seconds').value), policy = $('reference-power').value;
    const actor = $('session-actor').value.trim(), renderComm = $('reference-render-comm').value.trim();
    if (!/^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+$/.test(profile.package) ||
      !['width', 'height'].every(k => Number.isInteger(profile[k]) && profile[k] >= 1 && profile[k] <= 8192) ||
      $('reference-rotation').value === '' || !Number.isInteger(profile.rotation) || profile.rotation < 0 || profile.rotation > 3) throw new Error('请明确填写包名、1–8192 的原图宽高及方向 0–3。');
    if (!['observe', 'battery'].includes(policy) || !Number.isInteger(seconds) || seconds < 1 || seconds > 120 || !/^[A-Za-z0-9_-]{1,64}$/.test(actor)) throw new Error('请明确供电期望、1–120 秒和有效操作归属。');
    if (renderComm && (new TextEncoder().encode(renderComm).length > 64 || /\p{Cc}/u.test(renderComm))) throw new Error('参考渲染线程最多 64 个 UTF-8 字节，不能含控制字符。');
    let input;
    try { input = JSON.parse($('reference-input').value); } catch (_) { throw new Error('参考有限输入必须是有效 JSON。'); }
    if (!Array.isArray(input) && !(input && typeof input === 'object' && Object.keys(input).length === 1 && Array.isArray(input.steps))) throw new Error('参考有限输入须为数组或仅含 steps 数组的对象。');
    return {profile, seconds, policy, actor, input, renderComm};
  }
  function referenceDescriptor(value, job) {
    if (value?.schema !== 'phonefarm/gray-control@1' || value.producer !== 'phonefarm' || value.job_id !== job || typeof value.session_id !== 'string') throw new Error('会话身份不匹配，停止本组操作。');
    return value;
  }
  function referenceReceipt(run, name, value) {
    if (value?.schema !== 'phonefarm/gray-control-action-result@1' || value.session_id !== run.session || value.request_id !== run.ids[name]) throw new Error('参考动作回执身份不匹配，停止本组操作。');
    return value;
  }
  async function readReferenceReceipt(run, name) {
    referenceCurrent(run);
    try {
      const value = await json(`${API}/jobs/${run.job}/session/actions/${run.ids[name]}`, run.target);
      referenceCurrent(run);
      return referenceReceipt(run, name, value);
    } catch (error) {
      referenceCurrent(run);
      if (error.status === 404 && error.payload?.schema === 'phonefarm/gray-control-error@1' && error.payload.code === 'not_found') return null;
      throw error;
    }
  }
  function referenceComplete(run) {
    const r = run.receipts;
    if (!['capture', 'sample', 'marker'].every(name => r[name]?.status === 'completed' && r[name]?.result && typeof r[name].result === 'object')) return false;
    return r.capture.result.image?.request_id === run.ids.capture && r.sample.result.initial_frame?.request_id === run.ids.capture &&
      r.sample.result.input_kind === 'finite' && r.sample.result.collection?.status === 'complete' &&
      r.marker.result.kind === 'marker' && typeof r.marker.result.path === 'string';
  }
  async function referenceGuard(run, minimumSeconds = 0) {
    const until = Date.now() + 2000;
    while (true) {
      referenceCurrent(run);
      if (!access?.steer || !HGTarget.canWrite() || accessBusy) throw new Error('操作权限已改变，停止后续提交。');
      const d = referenceDescriptor(await json(`${API}/jobs/${run.job}/session`, run.target), run.job);
      referenceCurrent(run);
      const c = d.controller || {};
      if (d.session_id !== run.session || d.package !== run.options.profile.package || d.mode !== 'passdump') throw new Error('会话 / 包名 / passdump 模式不符，停止后续提交。');
      if (c.actor_id !== run.options.actor || c.epoch !== run.epoch || (c.expires_at != null && !(referenceTime(c.expires_at) > Date.now()))) throw new Error('控制者、epoch 或接管期限已变化；本组不会接纳新的控制权。');
      if (d.live_owner !== true || d.accepting !== true || d.action_capabilities?.sample !== 2 || !['capture', 'sample', 'marker'].every(a => d.allowed_actions?.includes(a))) throw new Error('owner 不可用或缺少参考采集能力。');
      if (d.power?.policy !== run.options.policy) throw new Error('会话固定供电策略不符合明确期望；未修改策略。');
      if (!(referenceTime(d.deadline_at) > Date.now() + minimumSeconds * 1000)) throw new Error('会话预算不足以覆盖下一步。');
      if (d.phase === 'ready') return d;
      if (d.phase !== 'executing' || Date.now() >= until) throw new Error('owner 未就绪，停止后续提交。');
      await new Promise(resolve => setTimeout(resolve, 100));
    }
  }
  function referenceImage(run, result) {
    const image = result?.image, profile = run.options.profile;
    if (!image || image.request_id !== run.ids.capture || !['width', 'height', 'rotation'].every(k => Number.isInteger(image[k]) && image[k] === profile[k])) throw new Error('本组原图与显式 profile 几何不符；未发送采样。');
    const age = Date.now() - referenceTime(image.captured_at);
    if (!(age >= 0 && age <= 30000)) throw new Error('本组原图已过期或时间无效；未发送采样。');
    return image;
  }
  async function submitReference(run, name, params, minimumSeconds = 0) {
    const d = await referenceGuard(run, minimumSeconds);
    if (name === 'sample') {
      referenceImage(run, run.receipts.capture?.result);
      if (d.latest_image?.request_id !== run.ids.capture) throw new Error('本组原图已被另一张采集替换；未发送采样。');
    }
    referenceCurrent(run);
    const requestBody = {schema: 'phonefarm/gray-control-action@1', request_id: run.ids[name], session_id: run.session,
      actor_id: run.options.actor, control_epoch: run.epoch, action: name, params};
    run.submitted = name;
    message('reference-message', `正在投递 ${run.ids[name]}；固定本次身份，仅发送一次。`);
    try {
      const receipt = await post(`${API}/jobs/${run.job}/session/actions`, requestBody, {}, run.target);
      referenceCurrent(run);
      run.receipts[name] = referenceReceipt(run, name, receipt);
    } catch (error) {
      referenceCurrent(run);
      try { run.receipts[name] = await readReferenceReceipt(run, name); } catch (lookupError) { run.lookupError = lookupError.message; }
      referenceCurrent(run);
      renderReferenceResult(run);
      throw new Error(`投递结果未确认（${run.ids[name]}）。仅查询了一次原 ID，停止后续步骤；${run.lookupError || error.message}`);
    }
    renderReferenceResult(run);
    const until = Date.now() + (name === 'sample' ? 720 : 120) * 1000;
    while (['accepted', 'started'].includes(run.receipts[name]?.status)) {
      if (Date.now() >= until) throw new Error(`原请求 ${run.ids[name]} 仍未完成，停止等待，不会重发。`);
      await new Promise(resolve => setTimeout(resolve, 1000));
      run.receipts[name] = await readReferenceReceipt(run, name);
      referenceCurrent(run);
      renderReferenceResult(run);
      if (!run.receipts[name]) throw new Error('已接受的请求当前不可读取；结果未知，停止后续步骤。');
    }
    if (run.receipts[name]?.status !== 'completed') throw new Error(`${run.ids[name]} 未完成；请核对错误和原件，未发送后续步骤。`);
    return run.receipts[name].result;
  }
  function renderReferenceResult(run) {
    if (selected !== run.job) return;
    $('reference-result').hidden = false;
    const sample = run.receipts.sample?.result;
    facts('reference-info', [['本组', run.label], ['会话', run.session || '正在核对'], ['固定 actor / epoch', run.options ? `${run.options.actor} / ${text(run.epoch)}` : '只读查询'],
      ...(sample ? [['回执采样秒数', sample.requested_seconds], ['回执计划时长', Number.isFinite(sample.planned_input_ms) ? `${sample.planned_input_ms / 1000} 秒` : '未知'], ['回执供电策略', powerPolicy(sample.power?.policy)], ['回执渲染线程', sample.metric_provenance?.frame?.render_comm || '未选择 / 未记录；帧指标未知']] : [])]);
    const stages = $('reference-stages'); stages.replaceChildren();
    for (const [name, title] of [['capture', '原图'], ['sample', '有限输入采样'], ['marker', 'marker']]) {
      const receipt = run.receipts[name], result = receipt?.result;
      const card = el('div', null, 'session-quality-stage');
      card.append(el('h5', `${title} · ${receipt === null ? '明确未找到' : receipt ? ({accepted: '已接受，待完成', started: '有 started 记录，待核实', completed: '动作已完成', failed: '失败', unknown: '结果未知', not_executed: '未执行'})[receipt.status] || '未知状态' : '未取得回执'}`));
      card.append(el('p', run.ids[name], 'note'));
      if (receipt?.error) card.append(el('p', typeof receipt.error === 'string' ? receipt.error : receipt.error.message || JSON.stringify(receipt.error), 'note task-error'));
      const links = el('ul', null, 'session-sample-artifacts');
      const receiptItem = el('li'), receiptLink = el('a', '原请求回执');
      receiptLink.href = `${API}/jobs/${run.job}/session/actions/${run.ids[name]}`; receiptLink.download = ''; receiptItem.append(receiptLink); links.append(receiptItem);
      const artifacts = name === 'capture' ? [{name: '原始 PNG', path: result?.image?.path}] : name === 'marker' ? [{name: 'marker 原件', path: result?.path}] : [{name: '采样报告 run.json', path: result?.path}, ...(Array.isArray(result?.artifacts) ? result.artifacts : [])];
      const seen = new Set();
      for (const artifact of artifacts) {
        const path = artifact?.path;
        if (typeof path !== 'string' || !path || path.startsWith('/') || /[\u0000-\u001f\u007f]/.test(path) || path.split('/').some(p => !p || p === '.' || p === '..') || seen.has(path)) continue;
        seen.add(path);
        const item = el('li'), link = el('a', artifact.name || path); link.href = `${API}/jobs/${run.job}/out?${new URLSearchParams({path})}`; link.download = ''; item.append(link); links.append(item);
      }
      card.append(links); stages.append(card);
    }
  }
  async function runReference(execute) {
    if (referenceRun || !selected || !sessionSupported() || (execute && $('reference-start').disabled)) return;
    const label = $('reference-label').value.trim();
    if (!/^[A-Za-z0-9_-]{1,48}$/.test(label)) { message('reference-message', '标签须为 1–48 位字母、数字、下划线或短横线。未发送动作。', true); return; }
    let options;
    try { if (execute) options = referenceOptions(); } catch (error) { message('reference-message', error.message, true); return; }
    const run = {job: selected, target: HGTarget.binding(), label, execute, options, ids: {capture: `gr-${label}-00`, sample: `gr-${label}-01`, marker: `gr-${label}-02`}, receipts: {}, cancelled: false};
    referenceRun = run;
    renderSessionControls(); renderReferenceResult(run);
    message('reference-message', '正在读取平台上的三份回执；尚未发送动作。');
    try {
      const initial = referenceDescriptor(await json(`${API}/jobs/${run.job}/session`, run.target), run.job); referenceCurrent(run);
      run.session = initial.session_id; run.epoch = initial.controller?.epoch;
      for (const name of ['capture', 'sample', 'marker']) { run.receipts[name] = await readReferenceReceipt(run, name); renderReferenceResult(run); }
      if (Object.values(run.receipts).some(r => r !== null)) {
        message('reference-message', referenceComplete(run) ? '本组三份证据已完整，显示原回执参数；没有重采，也未请求停止会话。' : '本组已有部分记录或未知结果。停止整组执行；请按原 ID 核查，不能自动接续或换 ID 重采。', !referenceComplete(run));
        return;
      }
      if (!execute) { message('reference-message', '三份回执均明确不存在。填写并核对参数后，才能显式采集本组。'); return; }
      if (!Number.isSafeInteger(run.epoch) || run.epoch < 0) throw new Error('控制 epoch 无效，未发送动作。');
      const capture = await submitReference(run, 'capture', {}, options.seconds + 10);
      const image = referenceImage(run, capture);
      const params = {seconds: options.seconds, input: options.input, frame_id: image.request_id, ...(options.renderComm ? {render_comm: options.renderComm} : {})};
      const sample = await submitReference(run, 'sample', params, options.seconds + 10);
      if (sample?.collection?.status !== 'complete') throw new Error('采样失败或采集结果不完整；未请求 marker，原件保留。');
      await submitReference(run, 'marker', {});
      if (!referenceComplete(run)) throw new Error('动作回执未绑定本组所需证据；请核对原件。');
      message('reference-message', '本组三份证据已完整，场景一致性与渲染精度仍需核查。未请求停止会话。');
    } catch (error) {
      if (referenceRun === run && selected === run.job) message('reference-message', `${error.message} 已有原件与请求 ID 保留；本流程未请求 stop。`, true);
    } finally {
      if (referenceRun === run) {
        referenceRun = null;
        if (selected === run.job) { renderReferenceResult(run); renderSessionControls(); }
      }
    }
  }

  function inputErrorText(error) {
    const detail = error.payload?.error;
    const reason = typeof detail === 'string' ? detail : detail?.message;
    return reason ? `${errorText(error)} ${reason}` : errorText(error);
  }
  function updateJobInputAccess() {
    const supported = inputsSupported();
    $('load-job-inputs').disabled = !currentJob || !supported || jobInputsBusy;
    $('load-job-inputs').setAttribute('aria-busy', String(jobInputsBusy));
    $('load-job-inputs').textContent = jobInputsBusy ? '读取中…' : jobInputsRead ? '刷新输入记录' : '读取输入记录';
    if (!jobInputsRead && !jobInputsBusy) message('job-inputs-message', !nodeInfo ? '节点能力尚未取得。' : !supported ? '此节点未提供输入工件记录能力，不能判断输入准备情况。' : '按需读取声明和准备回执；准备就绪不表示命令已运行或设备已恢复。');
    message('artifact-input-help', !nodeInfo ? '正在核对输入引用能力…' : !supported ? '此节点未提供输入引用能力；仍可查看和下载原件。' : '“复制输入清单”只校验所选小文件；需源任务已完成宿主收尾。不会自动提交任务或批量计算哈希。');
    for (const button of $('artifact-list').querySelectorAll('[data-input-reference]')) button.disabled = !supported || button.getAttribute('aria-busy') === 'true';
  }
  function resetJobInputs() {
    ++jobInputsRevision;
    jobInputsBusy = false;
    jobInputsRead = false;
    regionBusy('job-inputs', false);
    $('job-inputs-content').hidden = true;
    $('job-inputs-list').replaceChildren();
    $('job-inputs-record').textContent = '';
    $('job-inputs-updated').textContent = '';
    updateJobInputAccess();
  }
  async function loadJobInputs() {
    if ($('load-job-inputs').disabled || !selected) return;
    const id = selected, revision = ++jobInputsRevision;
    jobInputsBusy = true;
    jobInputsRead = false;
    $('job-inputs-content').hidden = true;
    $('job-inputs-list').replaceChildren();
    $('job-inputs-updated').textContent = '';
    regionBusy('job-inputs', true);
    updateJobInputAccess();
    message('job-inputs-message', '正在读取输入声明与准备回执…');
    try {
      const result = await json(`${API}/jobs/${encodeURIComponent(id)}/inputs`);
      if (revision !== jobInputsRevision || selected !== id) return;
      if (result.schema !== 'phonefarm/job-input-status@1' || result.job_id !== id || !result.preparation) throw new Error('输入记录格式不正确，不能判断准备情况。');
      const preparation = result.preparation, declared = result.declared;
      const labels = {pending: '输入准备中', ready: '输入已冻结', failed: '输入准备失败', not_declared: '未声明输入', not_prepared: '输入尚未准备'};
      const descriptions = {pending: '准备尚未完成，请刷新记录核对。', ready: '输入冻结已完成；不表示命令已运行或设备已恢复。', failed: '输入准备失败，此任务未启动。修好来源后需显式新请求；原 ID 仍返回此次结果。', not_declared: '此任务没有声明 inputs；旧上传或其他输入不在此记录范围内。', not_prepared: '尚无已完成准备的依据，可能从未开始或已取消；执行状态请看任务记录。'};
      message('job-inputs-message', `${descriptions[preparation.status] || '输入准备状态未知，请核对完整回执。'}${preparation.error ? ` 原因：${preparation.error}` : ''}`, preparation.status === 'failed' || !!preparation.error || !labels[preparation.status]);
      facts('job-inputs-info', [['准备状态', labels[preparation.status] || text(preparation.status)], ['来源节点', result.node_id], ['工作目录', declared?.cwd === 'input' ? '本任务输入目录' : declared?.cwd === 'default' ? '程序默认目录' : '未声明'], ['开始准备', time(preparation.started_at)], ['准备记录更新', time(preparation.updated_at)]]);
      for (const file of declared?.files || []) {
        const prepared = preparation.files?.find(item => item.name === file.name);
        const item = el('li', null, 'lease-receipt'), info = el('dl', null, 'facts');
        item.append(el('h4', file.name));
        const source = file.source || {};
        for (const [key, value] of [['输入位置', file.target], ['准备状态', ({pending: '待完成', ready: '已冻结', failed: '失败'})[prepared?.status] || '未记录'], ['来源路径', source.path], ['声明大小', bytes(source.size)], ['来源 SHA-256', source.sha256], ...(prepared?.error ? [['原因', prepared.error]] : [])]) info.append(el('dt', key), el('dd', text(value)));
        item.append(info);
        const actions = el('div', null, 'receipt-actions');
        actions.append(openJobButton(source.job_id, `来源任务 ${source.job_id}`));
        item.append(actions);
        $('job-inputs-list').append(item);
      }
      for (const binding of declared?.bindings || []) {
        const item = el('li', null, 'lease-receipt');
        item.append(el('h4', `JSON 派生声明 · ${binding.target}`), el('p', `源输入：${binding.source}。绑定会保留原 JSON；是否完成以准备回执为准，派生文件不沿用源 SHA。`, 'note'));
        const mappings = el('ul', null, 'lease-evidence');
        for (const value of binding.values || []) mappings.append(el('li', `${value.pointer || '根字段'} → 输入 ${value.input}`));
        item.append(mappings);
        $('job-inputs-list').append(item);
      }
      $('job-inputs-record').textContent = JSON.stringify(result, null, 2);
      $('job-inputs-content').hidden = false;
      $('job-inputs-updated').textContent = readTime();
      jobInputsRead = true;
    } catch (error) {
      if (revision === jobInputsRevision && selected === id) { message('job-inputs-message', inputErrorText(error), true); jobInputsRead = true; }
    } finally {
      if (revision === jobInputsRevision && selected === id) { jobInputsBusy = false; regionBusy('job-inputs', false); updateJobInputAccess(); }
    }
  }
  function updateJobLeaseAccess() {
    const supported = nodeInfo?.capabilities?.includes('jobs.leases-v1');
    $('load-job-leases').disabled = !currentJob || !supported || jobLeasesBusy;
    $('load-job-leases').setAttribute('aria-busy', String(jobLeasesBusy));
    $('load-job-leases').textContent = jobLeasesBusy ? '读取中…' : jobLeasesRead ? '刷新 lease 证据' : '读取 lease 证据';
    if (!jobLeasesRead && !jobLeasesBusy) message('job-leases-message', !nodeInfo ? '节点能力尚未取得，暂不能读取任务 lease 证据。' : !supported ? '此节点未提供任务关联的 lease 证据能力，不能据此判断恢复状态。' : '按需读取本任务记录，不访问或操作设备。');
  }
  function resetJobLeases() {
    ++jobLeasesRevision;
    jobLeasesBusy = false;
    jobLeasesRead = false;
    regionBusy('job-leases', false);
    $('job-leases-list').replaceChildren();
    $('job-leases-errors').replaceChildren();
    $('job-leases-errors').hidden = true;
    $('job-leases-updated').textContent = '';
    updateJobLeaseAccess();
  }
  const structuredText = value => value == null ? '未记录' : typeof value === 'string' ? value : JSON.stringify(value, null, 2);
  function addJobLease(entry, id) {
    const item = el('li', null, 'lease-receipt'), heading = el('div', null, 'section-heading');
    const state = ({active: 'lease 使用中', released: 'lease 已释放', retained: 'lease 保留待恢复'})[entry.state] || text(entry.state);
    heading.append(el('h4', `${entry.serial || '设备未记录'} · ${entry.lease_id}`), el('span', state, `task-state ${entry.state === 'retained' ? 'unknown' : ''}`));
    const recovery = entry.recovery || {};
    const report = ({not_reported: '尚未报告恢复完成。', required: '报告要求继续恢复。', reported_complete: '调用者报告局部完成；仅覆盖下述范围。'})[recovery.status] || '恢复报告状态未知。';
    item.append(heading, el('p', `${report}${recovery.evidence_origin === 'synthetic' ? ' 模拟证据（synthetic），不是真机恢复证明。' : ''}`, 'lease-report'));
    const info = el('dl', null, 'facts');
    for (const [key, value] of [['标签', entry.label], ['实际 lease 范围', scopeText({scope: entry.lease_scope || (entry.device_binding ? 'device' : 'global'), binding: entry.device_binding})],
      ...(entry.device_binding ? bindingFacts(entry.device_binding) : []), ['原因', recovery.reason || '未记录'], ['恢复报告范围', structuredText(recovery.scope)], ['证据来源', recovery.evidence_origin || '未记录'], ['记录更新', time(entry.updated_at)]]) info.append(el('dt', key), el('dd', text(value)));
    item.append(info);
    const details = el('details');
    details.append(el('summary', '恢复上下文与证据引用'), el('pre', `上下文：\n${structuredText(recovery.context)}`, 'lease-context'), el('p', '已有证据路径（仅展示，不直接打开）', 'note'));
    const evidence = el('ul', null, 'lease-evidence');
    for (const path of recovery.evidence || []) evidence.append(el('li', path));
    if (!evidence.children.length) evidence.append(el('li', '未记录证据路径'));
    details.append(evidence);
    item.append(details);
    const actions = el('div', null, 'receipt-actions');
    if (entry.artifact_path) {
      const download = el('a', '下载原始收据');
      download.href = `${API}/jobs/${encodeURIComponent(id)}/out?${new URLSearchParams({path: entry.artifact_path})}`;
      download.download = entry.artifact_path.split('/').pop();
      actions.append(download);
    }
    if (entry.recovery_of?.job_id) actions.append(openJobButton(entry.recovery_of.job_id, `先前任务 ${entry.recovery_of.job_id}`), el('span', `先前 lease：${text(entry.recovery_of.lease_id)}`, 'note'));
    item.append(actions);
    $('job-leases-list').append(item);
  }
  async function loadJobLeases() {
    if ($('load-job-leases').disabled || !selected) return;
    const id = selected, revision = ++jobLeasesRevision;
    jobLeasesBusy = true;
    jobLeasesRead = false;
    $('job-leases-list').replaceChildren();
    $('job-leases-errors').replaceChildren();
    $('job-leases-errors').hidden = true;
    $('job-leases-updated').textContent = '';
    updateJobLeaseAccess();
    regionBusy('job-leases', true);
    message('job-leases-message', '正在读取任务 lease 证据…');
    try {
      const result = await json(`${API}/jobs/${encodeURIComponent(id)}/leases`);
      if (revision !== jobLeasesRevision || selected !== id) return;
      const warning = result.status === 'partial' || result.status === 'unknown' || result.errors.length > 0;
      const description = ({not_reported: '尚无任务关联的 lease 收据；这不证明任务未操作设备或设备已恢复。', recorded: `已读取 ${result.entries.length} 份 lease 收据，请核查各自的范围与来源。`, partial: '只取得部分 lease 证据；下列错误尚未解决。', unknown: 'lease 证据状态未知，请核查读取错误。'})[result.status] || 'lease 证据状态未识别，请核查节点版本。';
      message('job-leases-message', description, warning);
      result.entries.forEach(entry => addJobLease(entry, id));
      $('job-leases-errors').replaceChildren(...result.errors.map(error => el('li', `${error.path || '未指明路径'}：${error.error}`)));
      $('job-leases-errors').hidden = !result.errors.length;
      $('job-leases-updated').textContent = readTime();
      jobLeasesRead = true;
    } catch (error) {
      if (revision === jobLeasesRevision && selected === id) { message('job-leases-message', error.status === 404 ? '任务不存在或节点未提供此证据入口，请刷新任务与节点信息。' : errorText(error), true); jobLeasesRead = true; }
    } finally {
      if (revision === jobLeasesRevision && selected === id) { jobLeasesBusy = false; regionBusy('job-leases', false); updateJobLeaseAccess(); }
    }
  }
  function breadcrumbs() {
    const root = el('button', '输出根目录');
    root.type = 'button';
    root.addEventListener('click', () => openDirectory(''));
    $('breadcrumbs').replaceChildren(root);
    let path = '';
    for (const part of directory.split('/').filter(Boolean)) {
      path = path ? `${path}/${part}` : part;
      const destination = path;
      const button = el('button', part);
      button.type = 'button';
      button.addEventListener('click', () => openDirectory(destination));
      $('breadcrumbs').append(el('span', '/'), button);
    }
    $('breadcrumbs').lastElementChild.setAttribute('aria-current', 'location');
  }
  function openDirectory(path) { directory = path; loadArtifacts(); }
  function renderExportLimits() {
    const supported = nodeInfo?.capabilities?.includes('jobs.export-limits-v1'), policy = supported ? nodeInfo.execution?.export_limits : null;
    message('export-policy-message', policy ? '节点声明的单次目录导出政策；不回填旧节点限值。' : '此节点未提供目录导出政策，限值未知；不会按新版限额推断。');
    facts('export-policy-info', [...exportCacheFacts(nodeInfo), ...(policy ? [['源文件逻辑字节上限', inputBytes(policy.source_bytes)], ['压缩输出字节上限', inputBytes(policy.compressed_bytes)],
      ['目录与文件总条目上限', policy.entries], ['目录深度上限', policy.max_depth], ['写入时最低保留空间', inputBytes(policy.min_free_bytes)],
      ['限值范围', policy.scope === 'directory_export_cache_generation' ? '目录导出缓存生成（含根目录；压缩输出含格式头尾和填充）' : text(policy.scope)]] : [])]);
  }
  function resetDirectoryExport() {
    ++exportRevision;
    exportController?.abort(); exportController = null;
    busy('prepare-directory-export', false);
    $('prepare-directory-export').disabled = historical() || !selected || !!HGTarget.problem();
    $('directory-export-download').hidden = true;
    $('directory-export-download').removeAttribute('href');
    $('directory-export-info').hidden = true; $('directory-export-info').replaceChildren();
    $('directory-export-error').hidden = true; $('directory-export-error').textContent = '';
    message('directory-export-message', '仅点击准备按钮才请求当前目录打包。页面离开不代表节点已停止生成缓存；不会自动重试。');
  }
  async function prepareDirectoryExport() {
    if (historical() || !selected || $('prepare-directory-export').disabled || exportController) return;
    const id = selected, path = directory, target = HGTarget.binding(), revision = ++exportRevision;
    const controller = new AbortController(); exportController = controller;
    busy('prepare-directory-export', true, '正在准备目录…');
    $('directory-export-download').hidden = true; $('directory-export-download').removeAttribute('href');
    $('directory-export-info').hidden = true; $('directory-export-info').replaceChildren();
    $('directory-export-error').hidden = true; $('directory-export-error').textContent = '';
    message('directory-export-message', `正在准备 ${path || '输出根目录'}；只发送一次元数据请求，可能需要等待压缩完成。`);
    const query = new URLSearchParams({path, meta: '1'});
    try {
      const meta = await json(`${API}/jobs/${encodeURIComponent(id)}/out?${query}`, target, {controller, timeoutMs: 600000});
      if (revision !== exportRevision || selected !== id || directory !== path) return;
      if (meta?.kind !== 'tar.gz' || meta.path !== (path || '.') || !Number.isSafeInteger(meta.size) || meta.size < 0 || typeof meta.etag !== 'string' || !meta.etag) throw new Error('目录元数据未确认，未提供下载链接。');
      facts('directory-export-info', [['已准备目录', path || '输出根目录'], ['压缩文件大小', inputBytes(meta.size)], ['本页收到回包', new Date().toLocaleString()]]);
      $('directory-export-info').hidden = false;
      const link = $('directory-export-download');
      link.href = HGTarget.url(`${API}/jobs/${encodeURIComponent(id)}/out?${new URLSearchParams({path, etag: meta.etag})}`, target);
      link.download = `${id}-${path.split('/').pop() || 'out'}.tar.gz`; link.hidden = false;
      message('directory-export-message', '目录已准备，可通过链接下载；不会把 tar 读入页面内存。源文件变化后可能需重新显式准备。');
    } catch (error) {
      if (revision !== exportRevision || selected !== id || directory !== path) return;
      const code = error.payload?.code, hint = code === 'export_budget_exceeded' ? '请选择较小子目录或原文件。' : code === 'export_busy' ? '此任务已有目录缓存正在生成；不是源文件变化，请稍后手动核对。' : '';
      message('directory-export-message', `${code ? `${code}（HTTP ${error.status}）` : error.name === 'AbortError' ? '准备请求超时，结果尚未确认。' : error.message || '目录准备结果未确认。'} ${hint} 未自动重试；原文件入口仍可使用。`, true);
      if (error.payload) { $('directory-export-error').textContent = JSON.stringify(error.payload, null, 2); $('directory-export-error').hidden = false; }
    } finally {
      if (revision === exportRevision) { exportController = null; busy('prepare-directory-export', false); $('prepare-directory-export').disabled = !!HGTarget.problem(target); }
    }
  }
  function addArtifact(file) {
    const item = el('li', null, 'artifact-item');
    const main = el('div', null, 'artifact-main');
    if (file.kind === 'directory') {
      const button = el('button', `${file.name}/`, 'file-name');
      button.type = 'button';
      button.addEventListener('click', () => openDirectory(file.path));
      main.append(button, el('span', '目录', 'artifact-sub'));
    } else {
      main.append(el('span', file.name));
      main.append(el('span', file.kind === 'file' ? `${bytes(file.size)} · ${time(file.modified)}` : `不可读取：${file.reason || '类型不支持'}`, 'artifact-sub'));
    }
    item.append(main);
    if (file.kind === 'file') {
      const actions = el('div', null, 'artifact-actions');
      const info = el('button', '校验信息', 'ghost');
      info.type = 'button';
      info.addEventListener('click', () => loadMetadata(file.path, info));
      const download = el('a', '下载');
      download.href = HGTarget.url(jobURL('/out', {path: file.path}));
      download.download = file.name;
      const reference = el('button', '复制输入清单', 'ghost');
      reference.type = 'button';
      reference.dataset.inputReference = file.path;
      reference.disabled = !inputsSupported();
      reference.addEventListener('click', () => loadInputReference(file.path, reference));
      actions.append(info);
      if (!historical()) actions.append(reference);
      actions.append(download);
      item.append(actions);
    }
    $('artifact-list').append(item);
  }
  async function loadArtifacts(append = false) {
    const revision = ++artifactRevision;
    ++metadataRevision;
    resetInputReference();
    if (!append) { resetDirectoryExport(); filesCursor = null; $('artifact-list').replaceChildren(); $('more-artifacts').hidden = true; }
    $('artifact-meta').hidden = true;
    regionBusy('artifacts', true);
    busy('refresh-artifacts', true, '读取中…');
    busy('more-artifacts', true, '正在加载…');
    breadcrumbs();
    message('artifact-message', '正在读取目录…');
    const params = {path: directory, limit: '100'};
    if (append && filesCursor) params.cursor = filesCursor;
    try {
      const result = await json(jobURL('/artifacts', params));
      if (revision !== artifactRevision) return;
      checkHistoryIdentity(result);
      if (!Array.isArray(result.items)) throw new Error('文件列表格式不正确。');
      result.items.forEach(addArtifact);
      filesCursor = result.next_cursor || null;
      $('more-artifacts').hidden = !filesCursor;
      const count = $('artifact-list').children.length;
      message('artifact-message', count ? `已显示 ${count} 项。` : '这个目录还没有输出文件。');
    } catch (error) { if (revision === artifactRevision) message('artifact-message', errorText(error), true); }
    finally {
      if (revision === artifactRevision) {
        busy('refresh-artifacts', false);
        busy('more-artifacts', false);
        regionBusy('artifacts', false);
      }
    }
  }
  async function loadMetadata(path, button) {
    const revision = artifactRevision;
    const metadata = ++metadataRevision;
    $('artifact-meta').hidden = false;
    regionBusy('artifact-meta', true);
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    button.textContent = '计算中…';
    $('artifact-meta').textContent = `正在计算校验信息：${path}`;
    try {
      const meta = await json(jobURL('/out', {path, meta: '1'}));
      if (revision !== artifactRevision || metadata !== metadataRevision) return;
      checkHistoryIdentity(meta);
      $('artifact-meta').textContent = `${path}\n大小：${bytes(meta.size)}\nSHA-256：${text(meta.sha256)}\n${historical() ? '历史文件也可能被外部程序修改。' : '运行中的文件可能继续变化。'}`;
    } catch (error) { if (revision === artifactRevision && metadata === metadataRevision) $('artifact-meta').textContent = errorText(error); }
    finally {
      button.disabled = false;
      button.setAttribute('aria-busy', 'false');
      button.textContent = '校验信息';
      if (revision === artifactRevision && metadata === metadataRevision) regionBusy('artifact-meta', false);
    }
  }
  function resetInputReference() {
    ++inputReferenceRevision;
    inputReference = '';
    $('artifact-input-reference').hidden = true;
    $('artifact-input-manifest').value = '';
    busy('copy-input-manifest', false);
    $('copy-input-manifest').disabled = true;
    regionBusy('artifact-input-reference', false);
  }
  async function copyInputManifest() {
    if (!inputReference || document.hidden) return;
    const revision = inputReferenceRevision, value = inputReference;
    busy('copy-input-manifest', true, '复制中…');
    try {
      if (!navigator.clipboard?.writeText) throw new Error('此浏览器未提供剪贴板写入。');
      await navigator.clipboard.writeText(value);
      if (revision === inputReferenceRevision) message('artifact-input-message', '输入清单已复制，可粘贴到上方提交表单；尚未提交任务。');
    } catch (_) {
      if (revision === inputReferenceRevision) {
        message('artifact-input-message', '清单已生成，但剪贴板写入未获允许。可选中下方完整清单手动复制。', true);
        $('artifact-input-manifest').focus();
        $('artifact-input-manifest').select();
      }
    } finally { if (revision === inputReferenceRevision) busy('copy-input-manifest', false); }
  }
  async function loadInputReference(path, button) {
    if (button.disabled || !inputsSupported() || !selected) return;
    resetInputReference();
    const id = selected, revision = inputReferenceRevision;
    $('artifact-input-reference').hidden = false;
    regionBusy('artifact-input-reference', true);
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    button.textContent = '核对引用…';
    message('artifact-input-message', `正在核对来源并计算单个文件引用：${path}`);
    try {
      const result = await json(`${API}/jobs/${encodeURIComponent(id)}/input-reference?${new URLSearchParams({path})}`);
      if (revision !== inputReferenceRevision || selected !== id) return;
      const source = result.source;
      if (result.schema !== 'phonefarm/job-input-reference@1' || typeof result.node_id !== 'string' || !result.node_id ||
          source?.job_id !== id || source.path !== path || !Number.isSafeInteger(source.size) || source.size < 0 || !/^[0-9a-f]{64}$/.test(source.sha256)) throw new Error('节点返回的输入引用不完整，未生成清单。');
      const name = path.split('/').pop().replace(/[^A-Za-z0-9._-]/g, '_').replace(/^\.+/, '').slice(0, 64) || 'input';
      inputReference = JSON.stringify({schema: 'phonefarm/job-inputs@1', node_id: result.node_id, files: [{name, target: `inputs/${name}`, source}], bindings: [], cwd: 'default'}, null, 2);
      $('artifact-input-manifest').value = inputReference;
      $('copy-input-manifest').disabled = false;
      message('artifact-input-message', '清单已生成，尚未提交任务。');
      await copyInputManifest();
    } catch (error) {
      if (revision === inputReferenceRevision && selected === id) message('artifact-input-message', inputErrorText(error), true);
    } finally {
      button.disabled = !inputsSupported();
      button.setAttribute('aria-busy', 'false');
      button.textContent = '复制输入清单';
      if (revision === inputReferenceRevision && selected === id) regionBusy('artifact-input-reference', false);
    }
  }
  function resetLog() {
    ++logRevision;
    logOffset = 0;
    logText = '';
    logDecoder = new TextDecoder();
    $('job-log').textContent = '';
    busy('log-next', false);
    busy('log-reset', false);
  }
  async function loadLog() {
    const revision = ++logRevision;
    const start = logOffset;
    regionBusy('task-log', true);
    busy('log-next', true, '读取中…');
    busy('log-reset', true);
    message('log-message', `正在从第 ${start} 字节读取…`);
    try {
      const data = await request(jobURL('/log', {offset: String(start), limit: String(LOG_CHUNK)}), async response => {
        if (!response.body) throw new Error('浏览器不支持分段读取日志。');
        const reader = response.body.getReader();
        const chunks = [];
        let length = 0;
        try {
          while (length < LOG_CHUNK) {
            const {done, value} = await reader.read();
            if (done) break;
            const chunk = value.subarray(0, LOG_CHUNK - length);
            chunks.push(chunk);
            length += chunk.byteLength;
          }
        } finally { await reader.cancel(); }
        const result = new Uint8Array(length);
        let at = 0;
        for (const chunk of chunks) { result.set(chunk, at); at += chunk.byteLength; }
        return result;
      });
      if (revision !== logRevision) return;
      logOffset += data.byteLength;
      logText += logDecoder.decode(data, {stream: true});
      if (logText.length > LOG_WINDOW) logText = logText.slice(-LOG_WINDOW);
      $('job-log').textContent = logText;
      message('log-message', data.byteLength ? `已读取至第 ${logOffset} 字节；每次最多 64 KiB，保留最近 262,144 个字符。` : `第 ${logOffset} 字节之后暂无新日志。${historical() ? '不据此判断任务当前是否仍在运行。' : '运行中的任务可稍后再读取。'}`);
    } catch (error) { if (revision === logRevision) message('log-message', errorText(error), true); }
    finally {
      if (revision === logRevision) {
        busy('log-next', false);
        busy('log-reset', false);
        regionBusy('task-log', false);
      }
    }
  }

  let webActor;
  try { webActor = sessionStorage.getItem('hgamey.session-actor'); } catch (_) { /* Optional preference. */ }
  webActor ||= `web-${globalThis.crypto?.randomUUID?.() || 'unavailable'}`;
  $('session-actor').value = webActor;
  try { sessionStorage.setItem('hgamey.session-actor', webActor); } catch (_) { /* Manual actor entry remains available after reload. */ }
  $('session-actor').addEventListener('change', () => {
    try { sessionStorage.setItem('hgamey.session-actor', $('session-actor').value.trim()); } catch (_) { /* Optional preference. */ }
    renderSessionControls();
  });
  $('session-refresh').addEventListener('click', loadSession);
  $('session-recovery-plan-refresh').addEventListener('click', loadSessionRecoveryPlan);
  $('session-recover-submit').addEventListener('click', submitSessionRecovery);
  $('session-recover-query').addEventListener('click', querySessionRecovery);
  $('session-recover-new').addEventListener('click', prepareSessionRecoveryAttempt);
  $('session-recover-job').addEventListener('click', event => {
    const job = sessionRecoveryRequest()?.job_id;
    if (job) { event.preventDefault(); selectCurrentJob(job); }
  });
  $('session-recover-previous').addEventListener('click', event => {
    const job = sessionRecoveryRequest()?.previous_job_id;
    if (job) { event.preventDefault(); selectCurrentJob(job); }
  });
  $('session-receipt-form').addEventListener('submit', event => { event.preventDefault(); querySessionReceipt(); });
  $('reference-query').addEventListener('click', () => runReference(false));
  $('reference-form').addEventListener('submit', event => { event.preventDefault(); runReference(true); });
  $('reference-stop').addEventListener('click', () => stopReference('已停止本页后续提交。'));
  $('reference-example').addEventListener('click', () => {
    $('reference-input').value = JSON.stringify([{action: 'sleep', ms: 3000}, ...Array.from({length: 3}, () => ({action: 'exec', cmd: 'input swipe 900 400 2000 400 12000'})), {action: 'sleep', ms: 1000}], null, 2);
  });
  document.addEventListener('visibilitychange', () => { if (document.hidden) stopReference('页面已隐藏。'); });
  $('session-quality-label').addEventListener('input', renderQualityControls);
  for (const op of ['begin', 'checkpoint', 'finish']) $('session-quality-' + op).addEventListener('click', () => {
    renderQualityControls();
    if ($('session-quality-' + op).disabled) return;
    $('session-panel').dataset.qualityOp = op;
    sendSessionAction('quality', {op, label: $('session-quality-label').value.trim()});
  });
  $('session-input-kind').addEventListener('change', renderSessionControls);
  $('session-sample-finite').addEventListener('change', () => {
    if ($('session-sample-finite').checked && !sampleFrame) bindSampleFrame();
    else renderSessionControls();
  });
  $('session-sample-rebind').addEventListener('click', bindSampleFrame);
  $('session-sample-seconds').addEventListener('input', renderSampleInput);
  $('session-sample-example').addEventListener('click', () => {
    $('session-sample-seconds').value = '30';
    $('session-sample-steps').value = JSON.stringify([{action: 'sleep', ms: 6000}, {action: 'repeat', count: 2, steps: [
      {action: 'swipe', x: 80, y: 100, to_x: 120, to_y: 100, duration_ms: 1000}, {action: 'sleep', ms: 16000}]}], null, 2);
    renderSampleInput();
  });
  $('session-sample-form').addEventListener('submit', event => {
    event.preventDefault();
    const seconds = Number($('session-sample-seconds').value);
    if (!Number.isInteger(seconds) || seconds < 1 || seconds > 120) {
      message('session-sample-help', '采样秒数必须是 1–120 的整数；未发送请求。', true); return;
    }
    const renderComm = $('session-render-comm').value.trim();
    if (renderComm && (new TextEncoder().encode(renderComm).length > 64 || /\p{Cc}/u.test(renderComm))) {
      message('session-sample-help', '线程筛选最多 64 个 UTF-8 字节，不能含控制字符；未发送请求。', true); return;
    }
    const params = {seconds, ...(renderComm ? {render_comm: renderComm} : {})};
    if ($('session-sample-finite').checked) {
      const issue = sampleInputIssue();
      if (issue) { renderSessionControls(); message('session-sample-help', `${issue} 未发送请求。`, true); return; }
      try {
        const input = JSON.parse($('session-sample-steps').value);
        if (!Array.isArray(input) && !(input && typeof input === 'object' && Object.keys(input).length === 1 && Array.isArray(input.steps))) throw new Error();
        params.input = input;
        params.frame_id = sampleFrame.request_id;
      } catch (_) { message('session-sample-help', '有限步骤须为 JSON 数组或仅含 steps 数组的对象；未发送请求。', true); return; }
    }
    sendSessionAction('sample', params);
  });
  document.querySelectorAll('[data-session-action]').forEach(button => button.addEventListener('click', () => {
    const action = button.dataset.sessionAction;
    let params = {};
    if (action === 'take_control') {
      const ttl = Number($('session-ttl').value);
      if (!Number.isInteger(ttl) || ttl < 1 || ttl > 300) { message('session-receipt-message', '接管时长须为 1–300 秒。', true); return; }
      params = {kind: 'human', ttl_seconds: ttl};
    } else if (['audit', 'trigger'].includes(action)) {
      const go = Number($('session-go').value);
      if (!Number.isInteger(go) || go < 1) { message('session-receipt-message', 'go 须为正整数。', true); return; }
      params = {go};
    }
    sendSessionAction(action, params);
  }));
  $('session-input-form').addEventListener('submit', event => {
    event.preventDefault();
    const frame = sessionState?.latest_image, kind = $('session-input-kind').value;
    if (!frame || !sessionAllowed(kind)) return;
    const x1 = Number($('session-x1').value), y1 = Number($('session-y1').value);
    const x2 = Number($('session-x2').value), y2 = Number($('session-y2').value), duration = Number($('session-duration').value);
    const point = (x, y) => Number.isInteger(x) && Number.isInteger(y) && x >= 0 && y >= 0 && x < frame.width && y < frame.height;
    if (!point(x1, y1) || kind === 'swipe' && (!point(x2, y2) || !Number.isInteger(duration) || duration < 1 || duration > 10000)) {
      message('session-receipt-message', '请按当前原图填写有效像素坐标；滑动时长须为 1–10000 毫秒。', true); return;
    }
    sendSessionAction(kind, kind === 'tap' ? {x: x1, y: y1, frame_id: frame.request_id} : {x1, y1, x2, y2, duration_ms: duration, frame_id: frame.request_id});
  });
  $('session-image').addEventListener('click', event => {
    const frame = sessionState?.latest_image, image = $('session-image');
    if (!frame || !image.naturalWidth || !sessionFresh) return;
    const rect = image.getBoundingClientRect(), scale = Math.min(rect.width / frame.width, rect.height / frame.height);
    const x = (event.clientX - rect.left - (rect.width - frame.width * scale) / 2) / scale;
    const y = (event.clientY - rect.top - (rect.height - frame.height * scale) / 2) / scale;
    if (x >= 0 && y >= 0 && x < frame.width && y < frame.height) {
      $('session-x1').value = Math.floor(x); $('session-y1').value = Math.floor(y);
      message('session-receipt-message', '起点坐标已填写，尚未发送输入。');
    }
  });
  window.addEventListener('pagehide', () => { stopReference('页面已离开。'); ++sessionRevision; ++receiptRevision; ++sessionPlanRevision; if (sessionImageURL) URL.revokeObjectURL(sessionImageURL); });

  $('filters').addEventListener('submit', event => {
    event.preventDefault();
    activeFilters = {query: $('query').value.trim(), state: $('state').value};
    loadJobs();
  });
  $('state').addEventListener('change', () => $('filters').requestSubmit());
  $('more-jobs').addEventListener('click', () => loadJobs(true));
  async function refreshAll() {
    busy('refresh-all', true, '刷新中…');
    try { await loadNode(); await Promise.all([loadJobSources(), loadQueue(), loadJobs(), loadAccess(), selected ? selectJob(selected, true) : null]); }
    finally { busy('refresh-all', false); }
  }
  $('refresh-all').addEventListener('click', refreshAll);
  $('refresh-job-sources').addEventListener('click', loadJobSources);
  $('apply-job-source').addEventListener('click', () => {
    const value = $('job-source').value;
    if (value === '') changeJobSource(null);
    else { const [source_id, revision] = JSON.parse(value); changeJobSource({source_id, revision}); }
    loadJobs();
  });
  $('refresh-runtime').addEventListener('click', () => loadNode(true));
  $('refresh-detail').addEventListener('click', () => { if (selected) selectJob(selected, true); });
  $('load-job-leases').addEventListener('click', loadJobLeases);
  $('load-job-inputs').addEventListener('click', loadJobInputs);
  $('copy-input-manifest').addEventListener('click', copyInputManifest);
  $('refresh-artifacts').addEventListener('click', () => loadArtifacts());
  $('prepare-directory-export').addEventListener('click', prepareDirectoryExport);
  $('more-artifacts').addEventListener('click', () => loadArtifacts(true));
  $('log-next').addEventListener('click', loadLog);
  $('log-reset').addEventListener('click', () => { resetLog(); loadLog(); });
  $('job-events-next').addEventListener('click', () => loadJobEvents());
  $('job-events-restart').addEventListener('click', () => loadJobEvents(true));
  $('job-events-copy').addEventListener('click', copyJobEventPosition);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden && jobEventsBusy) stopJobEvents('页面已隐藏，原浏览位置保留；回来后请显式继续。');
    else renderJobEvents();
  });
  window.addEventListener('pagehide', () => { jobEventsDisposed = true; stopJobEvents(); });
  $('access-toggle').addEventListener('click', () => {
    if (access?.steer) setAccess(false);
    else { $('unlock-form').hidden = false; $('steer-password').focus(); }
  });
  $('unlock-form').addEventListener('submit', event => { event.preventDefault(); setAccess(true); });
  $('unlock-dismiss').addEventListener('click', closeUnlock);
  $('cancel-job').addEventListener('click', () => cancelJob(false));
  $('retry-finalize').addEventListener('click', retryFinalize);
  $('execution-reconcile-check').addEventListener('click', () => reconcileExecution(false));
  $('execution-reconcile-refresh').addEventListener('click', () => { if (selected && !$('execution-reconcile-refresh').disabled) selectJob(selected, true); });
  $('execution-reconcile-force-open').addEventListener('click', () => {
    if ($('execution-reconcile-force-open').disabled) return;
    $('execution-reconcile-force-form').hidden = false;
    $('execution-reconcile-force-confirm').checked = false;
    renderControls();
    $('execution-reconcile-force-confirm').focus();
  });
  $('execution-reconcile-force-confirm').addEventListener('change', renderControls);
  $('execution-reconcile-force-form').addEventListener('submit', event => { event.preventDefault(); reconcileExecution(true); });
  $('execution-reconcile-force-dismiss').addEventListener('click', () => { $('execution-reconcile-force-form').hidden = true; $('execution-reconcile-force-confirm').checked = false; renderControls(); });
  $('force-open').addEventListener('click', () => { $('force-form').hidden = false; $('force-confirm').focus(); });
  $('force-confirm').addEventListener('change', renderControls);
  $('force-dismiss').addEventListener('click', () => { $('force-form').hidden = true; $('force-confirm').checked = false; renderControls(); });
  $('force-form').addEventListener('submit', event => { event.preventDefault(); cancelJob(true); });
  $('refresh-queue').addEventListener('click', loadQueue);
  $('submit-job-form').addEventListener('submit', event => { event.preventDefault(); submitJob(); });
  $('submit-inputs').addEventListener('input', renderSubmission);
  $('clear-inputs').addEventListener('click', () => { if (!$('clear-inputs').disabled) { $('submit-inputs').value = ''; renderSubmission(); } });
  $('session-template-form').addEventListener('submit', event => { event.preventDefault(); applySessionTemplate(); });
  $('refbench-template-form').addEventListener('submit', event => { event.preventDefault(); applyRefbenchTemplate(); });
  $('template-power').addEventListener('change', () => renderSessionTemplate(!$('submit-bin').disabled));
  $('submit-retry').addEventListener('click', () => submitJob(true));
  $('submit-refill').addEventListener('click', () => {
    if ($('submit-refill').disabled || !pendingRequest || pendingRequest.body) return;
    pendingRequest.refilling = true;
    submissionMessage('请按原任务重新填写参数。提交将复用此请求 ID；不同内容可能返回 409 冲突，请优先查询已有任务。', false, pendingRequest.id);
    renderSubmission();
    $('submit-argv').focus();
  });
  $('request-lookup-form').addEventListener('submit', event => { event.preventDefault(); lookupRequest(); });
  $('submit-open-job').addEventListener('click', () => { if (submittedJob) { selectCurrentJob(submittedJob); $('detail-heading').focus(); } });
  $('submit-device-access').addEventListener('change', renderSubmission);
  $('submit-device-scope').addEventListener('change', () => { deviceBinding = null; deviceBindingError = ''; ++deviceBindingRevision; deviceBindingBusy = false; renderSubmission(); });
  $('read-device-registry').addEventListener('click', readDeviceRegistry);
  $('submit-device-id').addEventListener('change', selectDeviceAliases);
  $('submit-device-alias').addEventListener('change', () => { deviceBinding = null; deviceBindingError = ''; ++deviceBindingRevision; renderSubmission(); });
  $('resolve-device-binding').addEventListener('click', resolveDeviceBinding);
  for (const id of ['submit-bin', 'submit-argv']) $(id).addEventListener('input', () => {
    if (!pendingRequest && deviceBinding) { deviceBinding = null; deviceBindingError = ''; ++deviceBindingRevision; }
    renderSubmission();
  });
  $('submit-execution-policy').addEventListener('change', renderSubmission);
  $('submit-host-slots').addEventListener('change', renderSubmission);
  for (const key of SLOT_KEYS) $('submit-host-' + key).addEventListener('input', renderSubmission);
  $('submit-wall-seconds').addEventListener('input', renderSubmission);
  $('submit-timeout-action').addEventListener('change', renderSubmission);
  $('submit-disk-guard').addEventListener('change', renderSubmission);
  for (const id of ['submit-max-log-bytes', 'submit-min-free-bytes']) $(id).addEventListener('input', renderSubmission);
  $('submit-disk-stop-action').addEventListener('change', renderSubmission);
  $('prepare-recovery').addEventListener('click', () => {
    if ($('prepare-recovery').disabled || !currentJob) return;
    $('submit-device-access').value = 'recovery';
    $('submit-device-scope').value = 'global'; deviceBinding = null; deviceBindingError = '';
    $('submit-recovery-for').value = currentJob.id;
    $('submit-label').value = `恢复 ${currentJob.id}`;
    $('submit-bin').value = '';
    $('submit-argv').value = '';
    $('submit-resources').value = '';
    $('submit-panel').open = true;
    submittedJob = '';
    $('submit-open-job').hidden = true;
    submissionMessage('已准备恢复表单，尚未提交。请填写实际恢复命令和参数；原任务的屏障不会自动解除。');
    renderSubmission();
    $('submit-bin').focus();
  });
  $('queue-release-note').addEventListener('input', renderControls);
  $('queue-release-form').addEventListener('submit', event => { event.preventDefault(); releaseQueue(); });
  try {
    const raw = sessionStorage.getItem(REQUEST_STORAGE);
    if (raw) {
      let saved; try { saved = JSON.parse(raw); } catch (_) { saved = {id: raw}; }
      pendingRequest = {id: saved.id, target: saved.target || null, binding_recorded: saved.binding_recorded === true,
        device_binding: saved.binding_recorded === true ? saved.device_binding : null,
        budget_recorded: saved.budget_recorded === true, time_budget: saved.budget_recorded === true ? saved.time_budget : null,
        disk_guard_recorded: saved.disk_guard_recorded === true, disk_guard: saved.disk_guard_recorded === true ? saved.disk_guard : null,
        host_slots_recorded: saved.host_slots_recorded === true, host_slots: saved.host_slots_recorded === true ? saved.host_slots : null};
      const id = pendingRequest.id;
      $('submit-panel').open = true;
      submissionMessage('有一条未确认的提交。请先按原请求 ID 查询；参数未保存在浏览器中。', true, id);
    }
  } catch (_) { /* Storage is optional; manual request lookup remains available. */ }
  const initialParams = new URLSearchParams(location.hash.slice(1));
  if (initialParams.has('source')) historySource = {source_id: initialParams.get('source'), revision: initialParams.has('source_revision') ? Number(initialParams.get('source_revision')) : null};
  renderJobSource();
  const initial = initialParams.get('job');
  if (initial && /^[0-9a-f]{12}$/.test(initial)) selected = initial;
  else if (initial) message('detail-message', '任务 ID 格式不正确。', true);
  document.addEventListener('hgamey-target-change', () => { resetDirectoryExport(); ++deviceBindingRevision; deviceBindingBusy = false; deviceBinding = null; deviceRegistry = null; deviceBindingError = ''; renderAccess(); if (jobEventsBusy && HGTarget.problem(jobEventPosition?.target)) stopJobEvents('节点身份需要重新核对；原浏览位置保留。'); syncJobEventScope(); });
  window.addEventListener('pagehide', resetDirectoryExport);
  refreshAll();
})();
