/* Reads stay host-only. Mutations require steering access; recovery is separate evidence. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const API = '/dash/api';
  const LOG_CHUNK = 64 * 1024;
  const LOG_WINDOW = 256 * 1024;
  let nodeRevision = 0, listRevision = 0, detailRevision = 0, artifactRevision = 0, metadataRevision = 0, logRevision = 0;
  let jobsCursor = null, filesCursor = null, selected = '', directory = '';
  let logOffset = 0, logText = '', logDecoder = new TextDecoder();
  let activeFilters = {query: '', state: ''};
  let currentJob = null, access = null, accessRevision = 0, accessBusy = false, accessError = '';
  const cancelling = new Set(), cancelReceipts = new Map(), releasing = new Set(), releaseReceipts = new Map();
  let nodeInfo = null, queueRevision = 0, submissionBusy = false, pendingRequest = null, submittedJob = '';
  const REQUEST_STORAGE = 'hgamey.pending-job-request';
  const PHASES = {preparing: '准备中', queued: '排队中', blocked: '资源保留 · 待核查', starting: '正在启动', running: '运行中', cancelling: '停止请求处理中', finished: '已结束', interrupted: '执行中断'};

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
  function bytes(value) {
    if (!Number.isFinite(value)) return '大小未记录';
    if (value < 1024) return `${value} B`;
    if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
    return `${(value / 1024 / 1024).toFixed(1)} MiB`;
  }
  function facts(id, rows) {
    $(id).replaceChildren(...rows.flatMap(([key, value]) => [el('dt', key), el('dd', text(value))]));
  }
  function status(job) {
    if (job.queue?.state === 'blocked' || job.phase === 'blocked') return ['资源保留 · 待核查', 'unknown'];
    if (['preparing', 'queued'].includes(job.phase)) return [PHASES[job.phase], ''];
    if (job.phase === 'interrupted' || job.outcome === 'interrupted') return ['执行中断 · 待核查', 'unknown'];
    if (job.phase === 'cancelling') return ['停止请求处理中', 'unknown'];
    if (job.phase === 'starting') return ['正在启动', 'running'];
    if (job.outcome === 'cancelled') return job.recovery?.status === 'not_needed' ? ['已取消 · 无需恢复', 'finished'] : ['已停止 · 恢复需核查', 'unknown'];
    if (job.outcome === 'failed') return [`执行失败${Number.isInteger(job.exit) ? ` · exit ${job.exit}` : ''}`, 'failed'];
    if (job.outcome === 'succeeded') return [`命令执行完成${Number.isInteger(job.exit) ? ` · exit ${job.exit}` : ''}`, 'finished'];
    if (job.lost) return ['退出状态丢失', 'unknown'];
    if (job.state === 'running') return ['运行中', 'running'];
    if (job.exit === 0) return ['已结束 · exit 0', 'finished'];
    if (Number.isInteger(job.exit)) return [`执行失败 · exit ${job.exit}`, 'failed'];
    return ['退出码未知', 'unknown'];
  }
  function jobURL(suffix = '', params = {}) {
    const query = new URLSearchParams(params);
    return `${API}/jobs/${encodeURIComponent(selected)}${suffix}${query.size ? '?' + query : ''}`;
  }
  function errorText(error) {
    return error.name === 'AbortError' ? '请求超时，请重试。' : (error.message || '网络错误，请重试。');
  }
  async function request(url, consume) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(url, {credentials: 'same-origin', cache: 'no-store', signal: controller.signal});
      if (response.status === 401 || response.status === 403 || (response.redirected && new URL(response.url).pathname === '/dash/login')) {
        const error = new Error('查看登录已失效，请重新登录。');
        error.status = response.status;
        throw error;
      }
      if (!response.ok) { const error = new Error(`读取失败（HTTP ${response.status}），可刷新重试。`); error.status = response.status; throw error; }
      return await consume(response);
    } finally { clearTimeout(timeout); }
  }
  async function json(url) {
    return request(url, async response => {
      if (!(response.headers.get('content-type') || '').includes('application/json')) throw new Error('服务返回了非 JSON 内容，请检查登录或节点连接。');
      return response.json();
    });
  }
  async function post(url, body, headers = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(url, {method: 'POST', credentials: 'same-origin', signal: controller.signal,
        headers: {'Content-Type': 'application/json', 'X-HG': '1', ...headers}, body: JSON.stringify(body)});
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
        error.status = response.status;
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
    const allowed = !!access?.steer && !accessBusy && !pending && !terminal && supported;
    const queued = ['preparing', 'queued'].includes(currentJob.phase);
    const stopping = currentJob.phase === 'cancelling' && currentJob.cancel?.status !== 'not_applied';
    $('cancel-job').disabled = !allowed || !currentJob.controls?.cancel_supported || stopping;
    $('force-open').disabled = !allowed || !currentJob.controls?.force_supported;
    $('force-submit').disabled = $('force-open').disabled || !$('force-confirm').checked;
    $('cancel-job').setAttribute('aria-busy', String(pending));
    $('cancel-job').textContent = pending ? '提交中…' : queued ? '撤销排队' : '请求停止';
    $('force-submit').setAttribute('aria-busy', String(pending));
    let reason = !supported ? '旧版任务仅支持查看，当前没有可核验的停止接口。' : terminal ? '任务执行已结束，无法继续发送停止请求。' :
      !access?.steer || accessBusy ? '请先在上方解锁任务操作权限。' : stopping ? '停止请求已记录，等待任务处理；刷新任务可核对进度。' :
      !currentJob.controls?.cancel_supported ? '当前任务不支持请求停止。' : queued ? '撤销尚未执行的任务，不发送进程信号。请以最新任务状态确认撤销结果。' : '请求停止只发送 SIGINT，不会自动升级终止。接受请求不代表任务已停止或设备已恢复。';
    if (currentJob.controls?.reason) reason += ` ${currentJob.controls.reason}`;
    $('cancel-help').textContent = reason;
    const receipt = cancelReceipts.get(currentJob.id);
    $('cancel-result').hidden = !receipt;
    if (receipt) message('cancel-result', receipt.message, receipt.error);
    const blocked = currentJob.queue?.state === 'blocked';
    $('queue-release-panel').hidden = !blocked;
    const releaseBusy = releasing.has(currentJob.id);
    $('queue-release-note').disabled = releaseBusy;
    $('queue-release-submit').disabled = !blocked || !access?.steer || accessBusy || releaseBusy || !$('queue-release-note').value.trim();
    $('queue-release-submit').setAttribute('aria-busy', String(releaseBusy));
    $('queue-release-submit').textContent = releaseBusy ? '提交中…' : '确认解除资源保留';
    message('queue-release-help', !access?.steer || accessBusy ? '请先解锁任务操作权限。' : '请填写核查说明；解除后，等待同一资源的任务可以继续调度。');
    const released = releaseReceipts.get(currentJob.id);
    $('queue-release-result').hidden = !released;
    if (released) message('queue-release-result', released.message, released.error);
  }
  function renderExecution(job) {
    const cancellation = job.cancel;
    facts('execution-info', [['执行阶段', PHASES[job.phase] || '旧版记录未提供'],
      ['调度状态', ({preparing: '准备中', queued: '排队中', claimed: '已分配执行', blocked: '保留资源，待核查', done: '调度已结束'})[job.queue?.state] || '未记录'], ['声明资源', job.queue?.resources?.join('、') || '无'], ['等待原因', waitingText(job.queue?.waiting)],
      ['退出码', job.exit], ['请求时间', time(cancellation?.requested_at)],
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
      await Promise.all([loadQueue(), selected === id ? selectJob(id, true) : null]);
    }
  }

  function waitingText(waiting) {
    return ({capacity: '等待执行名额', resource: '等待独占资源', preparing: '正在准备任务', recovery: '等待核查并解除资源保留'})[waiting?.kind] || '无等待原因';
  }
  function openJobButton(id, label = '查看任务') {
    const button = el('button', label, 'ghost');
    button.type = 'button';
    button.addEventListener('click', () => { selectJob(id); $('detail-heading').focus(); });
    return button;
  }
  async function loadQueue() {
    const revision = ++queueRevision;
    regionBusy('queue-panel', true);
    busy('refresh-queue', true, '读取中…');
    $('queue-summary').hidden = true;
    $('queue-list').replaceChildren();
    $('queue-updated').textContent = '';
    message('queue-message', '正在读取队列…');
    try {
      const queue = await json(`${API}/queue`);
      if (revision !== queueRevision) return;
      $('queue-summary').replaceChildren(...[[queue.active_count, `执行中 / ${queue.max_running} 个名额`], [queue.queued_count, '排队'], [queue.blocked_count, '保留资源']].map(([value, label]) => {
        const part = el('span'); part.append(el('strong', text(value)), el('span', label)); return part;
      }));
      $('queue-summary').hidden = false;
      for (const job of queue.items) {
        const item = el('li', null, 'queue-item');
        const identity = el('div');
        identity.append(el('p', job.job_id, 'task-item-id'), el('span', PHASES[job.phase] || job.phase, `task-state ${job.phase === 'blocked' ? 'unknown' : ''}`));
        const wait = el('div');
        wait.append(el('p', waitingText(job.waiting)), el('p', job.resources.length ? `独占：${job.resources.join('、')}` : '未声明独占资源', 'queue-resource'));
        if (job.waiting?.blocking_jobs?.length) {
          const blockers = el('div', null, 'queue-blockers');
          for (const id of job.waiting.blocking_jobs) blockers.append(openJobButton(id, `占用任务 ${id}`));
          wait.append(blockers);
        }
        item.append(identity, wait, openJobButton(job.job_id));
        $('queue-list').append(item);
      }
      message('queue-message', queue.dispatcher_error ? `调度异常：${queue.dispatcher_error}。队列记录保留，请核查调度服务后刷新。` : queue.items.length ? '按声明的资源调度；资源保留的任务需核查后释放。' : '当前队列为空。', !!queue.dispatcher_error);
      $('queue-updated').textContent = readTime();
    } catch (error) {
      if (revision === queueRevision) message('queue-message', error.status === 404 ? '此节点尚未提供队列入口。已有任务记录仍可查看。' : errorText(error), true);
    } finally {
      if (revision === queueRevision) { regionBusy('queue-panel', false); busy('refresh-queue', false); }
    }
  }
  function savePending() {
    try { if (pendingRequest) sessionStorage.setItem(REQUEST_STORAGE, pendingRequest.id); else sessionStorage.removeItem(REQUEST_STORAGE); }
    catch (_) { /* The visible request ID remains available if browser storage is unavailable. */ }
  }
  function renderSubmission() {
    const supported = nodeInfo?.capabilities?.includes('jobs.idempotency-v1') && nodeInfo?.execution?.bins?.length;
    const allowed = supported && access?.steer && !accessBusy;
    const locked = !!pendingRequest || submissionBusy;
    for (const id of ['submit-bin', 'submit-label', 'submit-argv', 'submit-resources']) $(id).disabled = !allowed || locked;
    $('submit-job').disabled = !allowed || locked || !globalThis.crypto?.randomUUID;
    $('submit-job').setAttribute('aria-busy', String(submissionBusy));
    $('submit-job').textContent = submissionBusy ? '正在核对请求…' : '提交任务';
    message('submit-help', pendingRequest ? '本次请求尚未确认，先按原 ID 查询，暂不创建新请求。' : !nodeInfo ? '等待取得节点命令。' : !supported ? '此节点尚不支持可靠提交，已有任务仍可查看。' : !access?.steer || accessBusy ? '先在上方解锁任务操作权限。' : !globalThis.crypto?.randomUUID ? '请通过 HTTPS 或本机地址访问后提交。' : '提交后以节点返回的实际阶段为准。');
    $('request-id').readOnly = !!pendingRequest;
    $('request-id').disabled = submissionBusy;
    if (pendingRequest) $('request-id').value = pendingRequest.id;
    $('request-lookup').disabled = submissionBusy;
    $('request-lookup').setAttribute('aria-busy', String(submissionBusy));
    $('submit-retry').hidden = !pendingRequest?.body || pendingRequest.conflict;
    $('submit-retry').disabled = !allowed || submissionBusy;
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
    await Promise.all([loadJobs(), loadQueue(), detailRevision === selectionVersion ? selectJob(job.id) : null]);
  }
  async function submitJob(retry = false) {
    if (submissionBusy || (retry ? $('submit-retry').disabled : $('submit-job').disabled)) return;
    if (!retry) {
      try {
        const argv = JSON.parse($('submit-argv').value);
        if (!Array.isArray(argv) || !argv.every(value => typeof value === 'string')) throw new Error('参数必须是 JSON 字符串数组，例如 ["--help"]。');
        const body = {bin: $('submit-bin').value, argv, label: $('submit-label').value.trim()};
        const resources = $('submit-resources').value.split(/\r?\n/).map(value => value.trim()).filter(Boolean);
        if (resources.length) body.resources = [...new Set(resources)];
        pendingRequest = {id: crypto.randomUUID(), body};
        savePending();
      } catch (error) { submissionMessage(error instanceof SyntaxError ? '参数不是有效的 JSON，请填写字符串数组。' : error.message, true); return; }
    }
    const attempt = pendingRequest;
    const selectionVersion = detailRevision;
    submissionBusy = true;
    submittedJob = '';
    $('submit-open-job').hidden = true;
    submissionMessage('正在提交；本次请求 ID 已固定。', false, attempt.id);
    renderSubmission();
    try {
      const job = await post(`${API}/jobs`, attempt.body, {'X-Request-ID': attempt.id});
      await submissionFound(job, attempt.id, selectionVersion);
    } catch (error) {
      if (error.status >= 400 && error.status < 500 && error.status !== 409) { pendingRequest = null; savePending(); }
      else if (error.status === 409) attempt.conflict = true;
      submissionMessage(error.status === 409 ? '请求 ID 或参数冲突。已保留原 ID，请查询对应任务并核对参数；不会自动重试。' : pendingRequest ? '提交结果未确认，节点可能已记录。请按原请求 ID 查询；重试也会使用同一 ID 和参数。' : errorText(error), true, attempt.id);
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
    try { await submissionFound(await json(`${API}/requests/${encodeURIComponent(id)}`), id, selectionVersion, true); }
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
      releaseReceipts.set(job.id, {message: result.released ? '资源保留已解除，后续任务可继续调度。核查说明已记录；此操作不代表平台验证了设备恢复。' : '资源未释放，请核对最新任务状态。', error: !result.released});
    } catch (error) {
      releaseReceipts.set(job.id, {message: !error.status || error.status >= 500 ? '释放结果尚未确认，请刷新任务和队列核对。' : errorText(error), error: true});
      if (error.status === 401 || error.status === 403) await loadAccess();
    } finally {
      releasing.delete(job.id);
      await Promise.all([loadQueue(), selected === job.id ? selectJob(job.id, true) : null]);
    }
  }

  async function loadNode() {
    const revision = ++nodeRevision;
    nodeInfo = null;
    renderSubmission();
    regionBusy('node-card', true);
    $('node-summary').hidden = true;
    $('node-details').hidden = true;
    $('node-updated').textContent = '';
    message('node-message', '正在读取节点信息…');
    try {
      const node = await json(`${API}/node`);
      if (revision !== nodeRevision) return;
      nodeInfo = node;
      const chosen = $('submit-bin').value;
      const bins = node.execution?.bins || [];
      $('submit-bin').replaceChildren(...bins.map(bin => { const option = el('option', bin); option.value = bin; return option; }));
      if (bins.includes(chosen)) $('submit-bin').value = chosen;
      else $('submit-bin').value = bins[0] || '';
      facts('node-info', [
        ['节点 ID', node.node_id], ['访问入口', location.origin],
        ['宿主', `${text(node.host?.hostname)} · ${text(node.host?.platform)}`],
        ['服务版本', `${text(node.service?.revision)}${node.service?.dirty === true ? '（有本地修改）' : ''}`],
        ['任务目录', node.storage?.jobs_root], ['数据目录', node.storage?.data_root],
        ['服务启动', time(node.started_at)]
      ]);
      $('node-summary').textContent = `${text(node.node_id)} · ${text(node.host?.hostname)}`;
      $('node-summary').hidden = false;
      $('node-details').hidden = false;
      message('node-message', '');
      $('node-updated').textContent = readTime();
    } catch (error) {
      if (revision === nodeRevision) message('node-message', errorText(error), true);
    } finally { if (revision === nodeRevision) { regionBusy('node-card', false); renderSubmission(); } }
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
    const query = new URLSearchParams({limit: '50', ...activeFilters});
    if (append && jobsCursor) query.set('cursor', jobsCursor);
    try {
      const result = await json(`${API}/jobs?${query}`);
      if (revision !== listRevision) return;
      if (!Array.isArray(result.items)) throw new Error('任务列表格式不正确。');
      result.items.forEach(addJob);
      jobsCursor = result.next_cursor || null;
      $('more-jobs').hidden = !jobsCursor;
      const count = $('task-list').children.length;
      $('task-count').textContent = `${count} 条已加载`;
      $('task-count').hidden = false;
      message('list-message', count ? `已显示 ${count} 个任务${jobsCursor ? '，还有更多' : ''}。` : (activeFilters.query || activeFilters.state ? '没有匹配的任务，可调整搜索条件。' : '这个节点还没有任务记录。'));
      const warnings = Array.isArray(result.warnings) ? result.warnings : [];
      $('list-warnings').textContent = warnings.map(w => typeof w === 'string' ? w : JSON.stringify(w)).join('；');
      $('list-warnings').hidden = !warnings.length;
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
    const revision = ++detailRevision;
    ++artifactRevision;
    ++metadataRevision;
    ++logRevision;
    if (!keepDirectory) directory = '';
    $('detail-content').hidden = true;
    $('force-form').hidden = true;
    $('force-confirm').checked = false;
    $('queue-release-panel').open = false;
    $('queue-release-note').value = '';
    $('detail-placeholder').hidden = false;
    $('detail-updated').textContent = '';
    $('refresh-detail').hidden = false;
    busy('refresh-detail', true, '读取中…');
    regionBusy('task-detail', true);
    message('detail-message', `正在读取 ${id}…`);
    highlight();
    history.replaceState(null, '', `${location.pathname}${location.search}#job=${encodeURIComponent(id)}`);
    try {
      const job = await json(jobURL());
      if (revision !== detailRevision) return;
      currentJob = job;
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
      facts('job-info', [['任务 ID', job.id], ...(job.request_id ? [['请求 ID', job.request_id]] : []), ['命令', job.bin], ['开始', time(job.started)], ['结束', time(job.ended)], ['记录说明', job.note || '无']]);
      $('job-command').textContent = JSON.stringify({bin: job.bin, argv: job.argv || []}, null, 2);
      renderExecution(job);
      $('detail-content').hidden = false;
      $('detail-placeholder').hidden = true;
      message('detail-message', '');
      resetLog();
      await Promise.all([loadArtifacts(), loadLog()]);
    } catch (error) {
      if (revision === detailRevision) {
        const receipt = cancelReceipts.get(id);
        message('detail-message', `${errorText(error)}${receipt ? ` ${receipt.message}` : ''}`, true);
      }
    }
    finally {
      if (revision === detailRevision) {
        busy('refresh-detail', false);
        regionBusy('task-detail', false);
      }
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
      download.href = jobURL('/out', {path: file.path});
      download.download = file.name;
      actions.append(info, download);
      item.append(actions);
    }
    $('artifact-list').append(item);
  }
  async function loadArtifacts(append = false) {
    const revision = ++artifactRevision;
    ++metadataRevision;
    if (!append) { filesCursor = null; $('artifact-list').replaceChildren(); $('more-artifacts').hidden = true; }
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
      $('artifact-meta').textContent = `${path}\n大小：${bytes(meta.size)}\nSHA-256：${text(meta.sha256)}\n运行中的文件可能继续变化。`;
    } catch (error) { if (revision === artifactRevision && metadata === metadataRevision) $('artifact-meta').textContent = errorText(error); }
    finally {
      button.disabled = false;
      button.setAttribute('aria-busy', 'false');
      button.textContent = '校验信息';
      if (revision === artifactRevision && metadata === metadataRevision) regionBusy('artifact-meta', false);
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
      message('log-message', data.byteLength ? `已读取至第 ${logOffset} 字节；每次最多 64 KiB，保留最近 262,144 个字符。` : `第 ${logOffset} 字节之后暂无新日志。运行中的任务可稍后再读取。`);
    } catch (error) { if (revision === logRevision) message('log-message', errorText(error), true); }
    finally {
      if (revision === logRevision) {
        busy('log-next', false);
        busy('log-reset', false);
        regionBusy('task-log', false);
      }
    }
  }

  $('filters').addEventListener('submit', event => {
    event.preventDefault();
    activeFilters = {query: $('query').value.trim(), state: $('state').value};
    loadJobs();
  });
  $('state').addEventListener('change', () => $('filters').requestSubmit());
  $('more-jobs').addEventListener('click', () => loadJobs(true));
  async function refreshAll() {
    busy('refresh-all', true, '刷新中…');
    try { await Promise.all([loadNode(), loadQueue(), loadJobs(), loadAccess(), selected ? selectJob(selected, true) : null]); }
    finally { busy('refresh-all', false); }
  }
  $('refresh-all').addEventListener('click', refreshAll);
  $('refresh-detail').addEventListener('click', () => { if (selected) selectJob(selected, true); });
  $('refresh-artifacts').addEventListener('click', () => loadArtifacts());
  $('more-artifacts').addEventListener('click', () => loadArtifacts(true));
  $('log-next').addEventListener('click', loadLog);
  $('log-reset').addEventListener('click', () => { resetLog(); loadLog(); });
  $('access-toggle').addEventListener('click', () => {
    if (access?.steer) setAccess(false);
    else { $('unlock-form').hidden = false; $('steer-password').focus(); }
  });
  $('unlock-form').addEventListener('submit', event => { event.preventDefault(); setAccess(true); });
  $('unlock-dismiss').addEventListener('click', closeUnlock);
  $('cancel-job').addEventListener('click', () => cancelJob(false));
  $('force-open').addEventListener('click', () => { $('force-form').hidden = false; $('force-confirm').focus(); });
  $('force-confirm').addEventListener('change', renderControls);
  $('force-dismiss').addEventListener('click', () => { $('force-form').hidden = true; $('force-confirm').checked = false; renderControls(); });
  $('force-form').addEventListener('submit', event => { event.preventDefault(); cancelJob(true); });
  $('refresh-queue').addEventListener('click', loadQueue);
  $('submit-job-form').addEventListener('submit', event => { event.preventDefault(); submitJob(); });
  $('submit-retry').addEventListener('click', () => submitJob(true));
  $('request-lookup-form').addEventListener('submit', event => { event.preventDefault(); lookupRequest(); });
  $('submit-open-job').addEventListener('click', () => { if (submittedJob) { selectJob(submittedJob); $('detail-heading').focus(); } });
  $('queue-release-note').addEventListener('input', renderControls);
  $('queue-release-form').addEventListener('submit', event => { event.preventDefault(); releaseQueue(); });
  try {
    const id = sessionStorage.getItem(REQUEST_STORAGE);
    if (id) {
      pendingRequest = {id};
      $('submit-panel').open = true;
      submissionMessage('有一条未确认的提交。请先按原请求 ID 查询；参数未保存在浏览器中。', true, id);
    }
  } catch (_) { /* Storage is optional; manual request lookup remains available. */ }
  const initial = new URLSearchParams(location.hash.slice(1)).get('job');
  if (initial && /^[0-9a-f]{12}$/.test(initial)) selected = initial;
  else if (initial) message('detail-message', '任务 ID 格式不正确。', true);
  refreshAll();
})();
