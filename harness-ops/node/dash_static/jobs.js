/* Reads stay host-only. Mutations require steering access; recovery is separate evidence. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const API = '/dash/api';
  const LOG_CHUNK = 64 * 1024;
  const LOG_WINDOW = 256 * 1024;
  let jobLeasesRevision = 0, jobLeasesBusy = false, jobLeasesRead = false;
  let nodeRevision = 0, listRevision = 0, detailRevision = 0, artifactRevision = 0, metadataRevision = 0, logRevision = 0;
  let jobsCursor = null, filesCursor = null, selected = '', directory = '';
  let logOffset = 0, logText = '', logDecoder = new TextDecoder();
  let activeFilters = {query: '', state: ''};
  let currentJob = null, access = null, accessRevision = 0, accessBusy = false, accessError = '';
  const cancelling = new Set(), cancelReceipts = new Map(), releasing = new Set(), releaseReceipts = new Map();
  const finalizing = new Set(), finalizeReceipts = new Map();
  const reconciling = new Set(), reconcileReceipts = new Map();
  let nodeInfo = null, leaseSnapshot = null, queueRevision = 0, submissionBusy = false, pendingRequest = null, submittedJob = '';
  const REQUEST_STORAGE = 'hgamey.pending-job-request';
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
    if (storageFailed(job)) return [PHASES.storage_blocked, 'unknown'];
    if (job.phase === 'finalizing') return [PHASES.finalizing, 'unknown'];
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
    renderSessionControls();
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
    const allowed = !!access?.steer && !accessBusy && !pending && !terminal && supported && !saving;
    const queued = ['preparing', 'queued'].includes(currentJob.phase);
    const stopping = currentJob.phase === 'cancelling' && currentJob.cancel?.status !== 'not_applied';
    $('cancel-job').disabled = !allowed || !currentJob.controls?.cancel_supported || stopping;
    $('force-open').disabled = !allowed || !currentJob.controls?.force_supported;
    $('force-submit').disabled = $('force-open').disabled || !$('force-confirm').checked;
    $('cancel-job').setAttribute('aria-busy', String(pending));
    $('cancel-job').textContent = pending ? '提交中…' : queued ? '撤销排队' : '请求停止';
    $('force-submit').setAttribute('aria-busy', String(pending));
    let reason = saving ? '结束记录尚未提交；请核对下方结果保存状态，停止请求不能代替保存。' : !supported ? '旧版任务仅支持查看，当前没有可核验的停止接口。' : terminal ? '任务执行已结束，无法继续发送停止请求。' :
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
    const leaseBlocked = ['write', 'recovery'].includes(currentJob.device_access) && (leaseSnapshot?.status !== 'clear' || leaseStorageBlocked(leaseSnapshot));
    const hostProblem = hostCompletionProblem(currentJob);
    $('recovery-prepare-panel').hidden = !blocked;
    $('prepare-recovery').disabled = !blocked || saving || !access?.steer || accessBusy || !recoverySupported() || !!pendingRequest || submissionBusy;
    message('recovery-prepare-help', saving ? '结束记录尚未提交，暂不能以此任务创建恢复任务。请先处理存储问题并重试保存结果。' : !recoverySupported() ? '此节点未提供恢复任务能力。' : pendingRequest ? '先找回或确认上方未决提交，再准备恢复任务。' : !access?.steer || accessBusy ? '请先解锁任务操作权限。' : '只预填恢复目标和模式；请由人或 Agent 填写实际恢复命令，执行后仍需核查并解除资源保留。');
    $('queue-release-note').disabled = releaseBusy;
    $('queue-release-submit').disabled = !blocked || !access?.steer || accessBusy || releaseBusy || saving || !!hostProblem || leaseBlocked || !$('queue-release-note').value.trim();
    $('queue-release-submit').setAttribute('aria-busy', String(releaseBusy));
    $('queue-release-submit').textContent = releaseBusy ? '提交中…' : '确认解除资源保留';
    message('queue-release-help', saving ? '结束记录尚未保存，不能通过解除资源保留绕过。请先处理存储问题并重试保存结果。' : !access?.steer || accessBusy ? '请先解锁任务操作权限。' : hostProblem || (leaseBlocked ? '设备写入或恢复任务尚未满足放行条件：实际 lease 需 clear，存储也需就绪。请核查屏障与存储状态后刷新队列；仅填写说明不能放行。' : '请填写核查说明；解除后，等待同一资源的任务可以继续调度。'));
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
    const disabled = !!problem || !access?.steer || accessBusy || pending || !!receipt?.unconfirmed;
    $('execution-reconcile-check').disabled = disabled;
    $('execution-reconcile-force-open').disabled = disabled;
    $('execution-reconcile-force-submit').disabled = disabled || !$('execution-reconcile-force-confirm').checked;
    for (const id of ['execution-reconcile-check', 'execution-reconcile-force-submit']) $(id).setAttribute('aria-busy', String(pending));
    $('execution-reconcile-check').textContent = pending ? '正在提交核查…' : '核查执行域';
    $('execution-reconcile-refresh').disabled = pending;
    message('execution-reconcile-help', pending ? '只提交一次，接受请求不代表宿主收尾完成。' : receipt?.unconfirmed ? '上次请求结果未确认。请先读取原任务状态；不会自动重发核查或强制操作。' : problem || (!access?.steer || accessBusy ? '请先解锁任务操作权限。' : '普通核查不杀进程；仅检查当前绑定域，域空后交还输出。强制操作需单独确认。'));
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
    $('retry-finalize').disabled = !failed || !supported || !access?.steer || accessBusy || pending;
    $('retry-finalize').setAttribute('aria-busy', String(pending));
    $('retry-finalize').textContent = pending ? '正在重试保存…' : '重试保存结果';
    message('finalize-help', pending ? '正在同步原有结果记录，不会重新执行命令。' : saving ? '命令已返回结束记录，正在确认保存；当前仍未确认作业完成。' : failed ?
      (!supported ? '此节点未提供 jobs.durability-v1，无法从本页重试保存；请升级节点。' : !access?.steer || accessBusy ? '记录保存失败，资源继续保留。请先解锁任务操作权限，再重试保存。' : '记录保存失败，资源继续保留。修复存储问题后可重试；只保存原有结果，不重新执行命令。') : '保存回执仅说明存储提交情况；设备恢复与资源释放请另行核对。', failed);
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
      ['设备访问', ({none: '宿主任务，无设备访问', write: '设备写入', recovery: '恢复任务'})[job.device_access] || '旧记录未提供'], ['恢复目标', job.recovery_for || job.queue?.recovery_for || '无'],
      ['调度状态', ({preparing: '准备中', queued: '排队中', claimed: '已分配执行', blocked: '保留资源，待核查', done: '调度已结束'})[job.queue?.state] || '未记录'], ['声明资源', job.queue?.resources?.join('、') || '无'], ['等待原因', waitingText(job.queue?.waiting)],
      ['命令退出码', job.exit], ...groupFacts, ['请求时间', time(cancellation?.requested_at)],
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
    const label = ({capacity: '等待执行名额', resource: '等待独占资源', preparing: '正在准备任务', recovery: '等待核查并解除资源保留', durability: '等待保存结束记录', device_lease: '等待实际设备 lease'})[waiting?.kind] || '无等待原因';
    return `${label}${waiting?.reason ? `：${waiting.reason}` : ''}${waiting?.lease_status ? `（${waiting.lease_status}）` : ''}`;
  }
  function openJobButton(id, label = '查看任务') {
    const button = el('button', label, 'ghost');
    button.type = 'button';
    button.addEventListener('click', () => { selectJob(id); $('detail-heading').focus(); });
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
    message('lease-storage-message', `${description}${storage?.error ? ` 错误：${storage.error}` : ''}`, !!storage?.error || (!!storage && storage.readiness !== 'ready'));
    $('lease-storage-details').hidden = !storage;
    facts('lease-storage-paths', storage ? [['状态根', storage.state_dir], ['兼容锁目录', storage.devlock_dir], ['设备 lease 根', storage.device_lease_dir]] : []);
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
    message('lease-message', error || (storageBlocked ? '存储尚未就绪，不能据此判断设备空闲或放行设备任务。' : !lease ? '此节点尚未返回实际 lease 记录，不能据此判断设备空闲。' : lease.error ? `读取异常：${lease.error}` : `兼容锁：${lease.bridge_held === true ? '占用' : lease.bridge_held === false ? '空闲' : '未知'}。实际 lease 与队列资源分别核查；资源保留不占用执行名额。`), !!error || !!lease?.error || storageBlocked);
    renderLeaseStorage(lease?.storage);
    $('lease-sampled').textContent = lease ? `采样于 ${time(lease.sampled_at)}` : '';
    $('lease-list').replaceChildren(...(lease?.entries || []).map(entry => {
      const item = el('li'), details = el('details');
      const role = ({outer_queue: '外层队列锁', device_lease: '设备租约'})[entry.role] || '角色未报告';
      const label = ({active: '活动记录', command_pending: '命令待处理', recovery_required: '需要恢复', abandoned: '已遗留', unknown: '状态未知'})[entry.state] || text(entry.state);
      details.append(el('summary', `${role}${entry.serial ? ` · ${entry.serial}` : ''} · ${label}`));
      const info = el('dl', null, 'facts');
      const holder = entry.holder_verified === true ? '已核验' : entry.holder_verified === false ? '核验未通过' : entry.role === 'device_lease' ? '不适用（设备租约）' : '未知';
      for (const [name, value] of [['标签（仅展示）', entry.label], ['实例 ID', entry.instance_id], ['关联外层实例', entry.outer_instance_id], ['外层持有者核验', holder], ...hostProcessFacts(entry), ['原因', entry.reason || '未记录'], ['记录位置', entry.directory]]) info.append(el('dt', name), el('dd', text(value)));
      details.append(info);
      if (entry.job_id) details.append(openJobButton(entry.job_id, `关联任务 ${entry.job_id}`));
      if (entry.recovery_context != null) details.append(el('pre', typeof entry.recovery_context === 'string' ? entry.recovery_context : JSON.stringify(entry.recovery_context, null, 2), 'lease-context'));
      item.append(details); return item;
    }));
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
    message('lease-message', '正在读取实际设备 lease…');
    try {
      const queue = await json(`${API}/queue`);
      if (revision !== queueRevision) return;
      renderLease(queue.lease);
      $('queue-summary').replaceChildren(...[[queue.active_count, `执行中 / ${queue.max_running} 个名额`], [queue.queued_count, '排队'], [queue.blocked_count, '保留资源']].map(([value, label]) => {
        const part = el('span'); part.append(el('strong', text(value)), el('span', label)); return part;
      }));
      $('queue-summary').hidden = false;
      for (const job of queue.items) {
        const item = el('li', null, 'queue-item');
        const identity = el('div');
        identity.append(el('p', job.job_id, 'task-item-id'), el('span', job.state_reason?.kind === 'durability' ? PHASES.storage_blocked : PHASES[job.phase] || job.phase, `task-state ${job.phase === 'blocked' || job.state_reason?.kind === 'durability' ? 'unknown' : ''}`));
        const wait = el('div');
        wait.append(el('p', waitingText(job.waiting)), el('p', job.resources.length ? `独占：${job.resources.join('、')}` : '未声明独占资源', 'queue-resource'));
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
      if (revision === queueRevision) { message('queue-message', error.status === 404 ? '此节点尚未提供队列入口。已有任务记录仍可查看。' : errorText(error), true); renderLease(null, '实际设备 lease 未能取得，请刷新核对。'); }
    } finally {
      if (revision === queueRevision) { regionBusy('queue-panel', false); busy('refresh-queue', false); }
    }
  }
  function savePending() {
    try { if (pendingRequest) sessionStorage.setItem(REQUEST_STORAGE, pendingRequest.id); else sessionStorage.removeItem(REQUEST_STORAGE); }
    catch (_) { /* The visible request ID remains available if browser storage is unavailable. */ }
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
  function renderSubmission() {
    const supported = nodeInfo?.capabilities?.includes('jobs.idempotency-v1') && nodeInfo?.execution?.bins?.length;
    const allowed = supported && access?.steer && !accessBusy;
    const refilling = !!pendingRequest?.refilling;
    const locked = (!!pendingRequest && !refilling) || submissionBusy;
    const mode = $('submit-device-access').value;
    const recovery = mode === 'recovery';
    const policy = pendingRequest?.body ? pendingRequest.body.execution_policy || '' : $('submit-execution-policy').value;
    const policyProblem = executionPolicyProblem(policy);
    for (const id of ['submit-bin', 'submit-label', 'submit-argv']) $(id).disabled = !allowed || locked;
    $('submit-execution-policy').disabled = !allowed || locked;
    message('execution-policy-help', `${pendingRequest?.body ? '重试保留原请求策略。' : ''}${policyProblem || (!nodeInfo ? '正在核对执行策略能力…' : !nodeInfo.capabilities?.includes('jobs.execution-policy-v1') ? '此节点未提供策略能力。兼容默认不发送新字段，实际执行范围未知。' : policy ? `${policy}：${nodeInfo.execution?.process_policies?.[policy]?.reason || '以任务中 worker 的实际执行记录为准。'}` : '兼容默认不发送策略字段，沿节点原有行为；当前原进程组策略不涵盖 setsid 脱组后代。')} 设备恢复另行核查。`, !!policyProblem);
    $('submit-device-access').disabled = !allowed || locked || !recoverySupported();
    $('submit-resources').disabled = !allowed || locked || recovery;
    $('submit-recovery-for').disabled = !allowed || locked || !recoverySupported();
    $('submit-recovery-for').required = recovery;
    $('recovery-target-field').hidden = !recovery;
    message('device-access-help', !nodeInfo ? '正在核对设备访问能力…' : !recoverySupported() ? '此节点尚未提供设备访问分类与恢复任务能力；可继续按节点默认提交。' : recovery ? '恢复任务继承目标的独占资源。请填写实际恢复命令；运行结束不会自动解除原任务屏障。' : '按实际操作选择访问模式。默认由节点按命令保守判断；无设备访问只适用于宿主任务。');
    $('resources-help').textContent = recovery ? '恢复任务由节点继承目标资源，此处不另行声明。' : '相同资源的任务依次执行。资源声明只用于排队，不授予设备操作权限。';
    $('submit-job').disabled = !allowed || locked || !!policyProblem || (!!mode && !recoverySupported()) || (!pendingRequest?.id && !globalThis.crypto?.randomUUID);
    $('submit-job').setAttribute('aria-busy', String(submissionBusy));
    $('submit-job').textContent = submissionBusy ? '正在核对请求…' : refilling ? '使用原请求 ID 提交' : '提交任务';
    message('submit-help', refilling ? '将复用原请求 ID。请尽量填写原内容；内容不同可能返回 409 冲突，应优先查询。' : pendingRequest ? '本次请求尚未确认，先按原 ID 查询，暂不创建新请求。' : !nodeInfo ? '等待取得节点命令。' : !supported ? '此节点尚不支持可靠提交，已有任务仍可查看。' : !access?.steer || accessBusy ? '先在上方解锁任务操作权限。' : !globalThis.crypto?.randomUUID ? '请通过 HTTPS 或本机地址访问后提交。' : '提交后以节点返回的实际阶段为准。');
    $('request-id').readOnly = !!pendingRequest;
    $('request-id').disabled = submissionBusy;
    if (pendingRequest) $('request-id').value = pendingRequest.id;
    $('request-lookup').disabled = submissionBusy;
    $('request-lookup').setAttribute('aria-busy', String(submissionBusy));
    $('submit-refill').hidden = !pendingRequest || !!pendingRequest.body || refilling;
    $('submit-refill').disabled = !allowed || submissionBusy;
    $('submit-retry').hidden = !pendingRequest?.body || pendingRequest.conflict;
    $('submit-retry').disabled = !allowed || submissionBusy || !!policyProblem;
    renderSessionTemplate(allowed);
    renderRefbenchTemplate(allowed);
    renderControls();
  }
  function renderSessionTemplate(allowed) {
    const unavailable = !allowed || !!pendingRequest || submissionBusy || !nodeInfo?.execution?.bins?.includes('phonefarm');
    for (const id of ['template-serial', 'template-package', 'template-mode', 'template-seconds', 'template-power', 'template-so', 'template-apply']) $(id).disabled = unavailable;
    message('template-power-help', $('template-power').value === 'battery' ? '首次采样准备电池条件，跨轮保持；会话停止或同上下文恢复时还原所绑定的充电控制。已实测放电时可能无需修改控制。' : '只观察当前供电状态，不请求修改充电控制。功耗是否可用仍以每轮观测和采样窗口为准。');
    message('template-status', pendingRequest ? '存在未确认提交，先按原请求 ID 找回；模板不会覆盖该请求。' : unavailable ? '需解锁任务操作，且节点提供 phonefarm 命令，才能填入模板。' : '模板只填入参数；核对下方命令后，再点击“提交任务”。');
  }
  function applySessionTemplate() {
    if ($('template-apply').disabled || pendingRequest || submissionBusy) return;
    const serial = $('template-serial').value.trim(), pkg = $('template-package').value.trim();
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
    const unavailable = !allowed || !!pendingRequest || submissionBusy || !nodeInfo?.execution?.bins?.includes('phonefarm');
    for (const id of ['refbench-serial', 'refbench-frames', 'refbench-intensity', 'refbench-timeout', 'refbench-so', 'refbench-apply']) $(id).disabled = unavailable;
    message('refbench-status', pendingRequest ? '存在未确认提交，先按原请求 ID 找回；模板不会覆盖该请求。' : unavailable ? '需解锁任务操作，且节点提供 phonefarm 命令，才能填入模板。' : '模板只填入参数；核对下方命令后，再点击“提交任务”。');
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
    await Promise.all([loadJobs(), loadQueue(), detailRevision === selectionVersion ? selectJob(job.id) : null]);
  }
  async function submitJob(retry = false) {
    if (submissionBusy || (retry ? $('submit-retry').disabled : $('submit-job').disabled)) return;
    if (!retry) {
      try {
        const argv = JSON.parse($('submit-argv').value);
        if (!Array.isArray(argv) || !argv.every(value => typeof value === 'string')) throw new Error('参数必须是 JSON 字符串数组，例如 ["--help"]。');
        const body = {bin: $('submit-bin').value, argv, label: $('submit-label').value.trim()};
        const policy = $('submit-execution-policy').value;
        const policyProblem = executionPolicyProblem(policy);
        if (policyProblem) throw new Error(policyProblem);
        if (policy) body.execution_policy = policy;
        const mode = $('submit-device-access').value;
        if (mode) body.device_access = mode;
        if (mode === 'recovery') {
          const target = $('submit-recovery-for').value.trim();
          if (!/^[0-9a-f]{12}$/.test(target)) throw new Error('恢复任务需要原资源保留任务的 12 位 ID。');
          body.recovery_for = target;
        }
        const resources = $('submit-resources').value.split(/\r?\n/).map(value => value.trim()).filter(Boolean);
        if (resources.length && mode !== 'recovery') body.resources = [...new Set(resources)];
        pendingRequest = {id: pendingRequest?.id || crypto.randomUUID(), body};
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
      releaseReceipts.set(job.id, {message: result.released === true ? '资源保留已解除，后续任务可继续调度。核查说明已记录；此操作不代表平台验证了设备恢复。' : result.job && storageFailed(result.job) ? `核查记录保存失败，资源仍然保留。请处理存储问题后重试保存结果。${result.job.queue?.state_reason?.message || ''}` : '资源未释放，请核对最新任务状态。', error: result.released !== true});
    } catch (error) {
      releaseReceipts.set(job.id, {message: !error.status || error.status >= 500 ? '释放结果尚未确认，请刷新任务和队列核对。' : error.status === 409 ? `资源未释放：当前条件不允许放行，请核查实际 lease 与任务状态。${errorText(error)}` : errorText(error), error: true});
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
    updateJobLeaseAccess();
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
      else $('submit-bin').value = $('submit-device-access').value === 'recovery' ? '' : bins[0] || '';
      facts('node-info', [
        ['节点 ID', node.node_id], ['访问入口', location.origin],
        ['宿主', `${text(node.host?.hostname)} · ${text(node.host?.platform)}`],
        ['服务版本', `${text(node.service?.revision)}${node.service?.dirty === true ? '（有本地修改）' : ''}`],
        ['任务目录', node.storage?.jobs_root], ['数据目录', node.storage?.data_root],
        ['服务启动', time(node.started_at)], ...processPolicyFacts(node)
      ]);
      $('node-summary').textContent = `${text(node.node_id)} · ${text(node.host?.hostname)}`;
      $('node-summary').hidden = false;
      $('node-details').hidden = false;
      message('node-message', '');
      $('node-updated').textContent = readTime();
    } catch (error) {
      if (revision === nodeRevision) message('node-message', errorText(error), true);
    } finally { if (revision === nodeRevision) { regionBusy('node-card', false); renderSubmission(); updateJobLeaseAccess(); renderSessionControls(); } }
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
    resetJobLeases();
    resetSession();
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
    history.replaceState(null, '', `${location.pathname}${location.search}#job=${encodeURIComponent(id)}`);
    try {
      const job = await json(jobURL());
      if (revision !== detailRevision) return;
      currentJob = job;
      if (reconcileReceipts.get(id)?.unconfirmed) reconcileReceipts.set(id, {message: '已重新读取原任务状态。请核对宿主核查与输出交还记录，再决定是否操作。', error: false});
      updateJobLeaseAccess();
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
      facts('job-info', [['任务 ID', job.id], ...(job.request_id ? [['请求 ID', job.request_id]] : []), ['命令', job.bin], ['开始', time(job.started)], [job.phase === 'finalizing' || storageFailed(job) ? '候选结束时间（未提交）' : '结束', time(job.ended)], ['记录说明', job.note || '无']]);
      $('job-command').textContent = JSON.stringify({bin: job.bin, argv: job.argv || []}, null, 2);
      renderExecution(job);
      $('detail-content').hidden = false;
      $('detail-placeholder').hidden = true;
      message('detail-message', '');
      resetLog();
      await Promise.all([loadArtifacts(), loadLog(), job.bin === 'phonefarm' && job.argv?.[0] === 'gray-layer' && job.argv?.[1] === 'session' ? loadSession() : null]);
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
  let sessionState = null, sessionFresh = false, sessionRevision = 0, sessionBusy = false, sessionActionBusy = false;
  let sessionImageURL = null, sessionImageKey = '', receiptRevision = 0, sessionSampleReceipt = null, sessionQualityReceipt = null, sampleFrame = null;
  let referenceRun = null;
  const pendingActions = new Map();
  const sessionRecoveries = new Map(), sessionRecovering = new Set();
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
    if (!receipt || ['accepted', 'started', 'unknown'].includes(receipt.status)) pending.push({request_id: request.request_id, action: request.action || 'unknown'});
    pendingActions.set(id, pending);
    try { if (pending.length) sessionStorage.setItem(sessionKey(id), JSON.stringify(pending)); else sessionStorage.removeItem(sessionKey(id)); } catch (_) { /* Request IDs remain visible in this page. */ }
  }
  function resetSession() {
    stopReference('任务详情已切换或刷新。');
    referenceRun = null;
    $('reference-result').hidden = true;
    message('reference-message', '按标签查询共享回执；本页不保存整组进度。');
    ++sessionRevision; ++receiptRevision;
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
  function sessionRecoveryRequest(id = selected) {
    if (!sessionRecoveries.has(id)) {
      let saved = null;
      try { saved = JSON.parse(sessionStorage.getItem(`hgamey.session-recovery.${id}`) || 'null'); } catch (_) { /* The displayed ID can still be queried manually. */ }
      sessionRecoveries.set(id, saved && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(saved.request_id) ? {
        request_id: saved.request_id, job_id: /^[0-9a-f]{12}$/.test(saved.job_id) ? saved.job_id : null,
        previous_job_id: /^[0-9a-f]{12}$/.test(saved.previous_job_id) ? saved.previous_job_id : null, prepared: saved.prepared === true
      } : null);
    }
    return sessionRecoveries.get(id);
  }
  function keepSessionRecovery(id, record) {
    sessionRecoveries.set(id, record);
    try { sessionStorage.setItem(`hgamey.session-recovery.${id}`, JSON.stringify({request_id: record.request_id, job_id: record.job_id || null, previous_job_id: record.previous_job_id || null, prepared: record.prepared === true})); }
    catch (_) { /* Keep the same request ID visible even if browser storage is unavailable. */ }
  }
  function sessionRecoveryProblem() {
    if (!currentJob || currentJob.id !== selected) return '请先读取原任务状态。';
    if (storageFailed(currentJob) || currentJob.phase === 'finalizing') return '原任务结束记录尚未确认保存，请先处理结果保存。';
    if (currentJob.state !== 'finished' || currentJob.queue?.state !== 'blocked') return '原任务须已结束并保留资源；请先刷新任务核对状态。';
    const hostProblem = hostCompletionProblem(currentJob, true);
    if (hostProblem) return hostProblem;
    if (!sessionFresh || sessionBusy || !sessionState) return '请先成功读取最新会话状态。';
    if (sessionState.phase !== 'recovery_required') return '当前会话没有报告需要继续恢复。';
    if (sessionState.live_owner !== false) return '原会话 owner 尚未确认退出，不能另建恢复任务。';
    return '';
  }
  function renderSessionRecovery() {
    const record = currentJob && sessionRecoveryRequest(), pending = sessionRecovering.has(selected);
    const supported = nodeInfo?.capabilities?.includes('jobs.session-recovery-v1');
    $('session-recover-panel').hidden = !currentJob || (!record && sessionState?.phase !== 'recovery_required');
    $('session-recover-submit').hidden = !supported || !!record?.job_id;
    $('session-recover-submit').disabled = !supported || !access?.steer || accessBusy || pending || !!record?.job_id || ((!record || record.prepared) && (!!sessionRecoveryProblem() || !globalThis.crypto?.randomUUID));
    $('session-recover-submit').textContent = pending ? '正在核对恢复请求…' : record?.prepared ? '提交新恢复尝试' : record ? '使用原请求 ID 重试' : '提交恢复任务';
    $('session-recover-submit').setAttribute('aria-busy', String(pending));
    $('session-recover-query').hidden = !record;
    $('session-recover-query').disabled = pending;
    $('session-recover-query').setAttribute('aria-busy', String(pending));
    $('session-recover-job').hidden = !record?.job_id;
    if (record?.job_id) $('session-recover-job').href = `/dash/jobs#job=${encodeURIComponent(record.job_id)}`;
    $('session-recover-new').hidden = !supported || !record?.job_id;
    $('session-recover-new').disabled = !supported || !record?.job_id || !access?.steer || accessBusy || pending || !!sessionRecoveryProblem() || !globalThis.crypto?.randomUUID;
    $('session-recover-new').setAttribute('aria-busy', String(pending));
    $('session-recover-previous').hidden = !record?.previous_job_id;
    if (record?.previous_job_id) $('session-recover-previous').href = `/dash/jobs#job=${encodeURIComponent(record.previous_job_id)}`;
    $('session-recover-request').textContent = record ? `恢复请求 ID：${record.request_id}` : '';
    message('session-recover-help', !supported ? '此节点未提供异步会话恢复能力；仍可查看已有恢复记录。' : record?.job_id ? `恢复任务已记录，请打开任务核查执行与证据。创建成功不代表恢复完成，原资源不会自动释放。新建尝试前会读取上一任务，确认已结束且调度资源已释放。${sessionRecoveryProblem()}` : record?.prepared ? `新请求 ID 已生成，尚未提交。${sessionRecoveryProblem() || '核对后点击“提交新恢复尝试”；上次任务原件仍可查看。'}` : record ? '本次请求 ID 已保留。结果不确定时先查询；重试仍使用同一 ID，不会改用新 ID 创建任务。' : !access?.steer || accessBusy ? '请先解锁任务操作权限。' : sessionRecoveryProblem() || (!globalThis.crypto?.randomUUID ? '请通过 HTTPS 或本机地址提交。' : '节点根据原任务绑定的会话与设备创建恢复任务，无需填写路径；提交后异步执行，原资源继续保留。'));
    message('session-recover-result', record?.message || (record ? '可按原请求 ID 查询节点中的最新任务记录。' : ''), !!record?.error);
  }
  function acceptSessionRecovery(id, record, job) {
    if (!/^[0-9a-f]{12}$/.test(job?.id) || job.recovery_for !== id) throw new Error('恢复任务回包未匹配原任务，结果尚未确认。');
    keepSessionRecovery(id, {...record, prepared: false, job_id: job.id, message: `恢复任务 ${job.id} · ${status(job)[0]}。提交不代表恢复完成，请核对任务记录。`, error: false});
  }
  async function submitSessionRecovery() {
    if (!currentJob || $('session-recover-submit').disabled) return;
    const id = selected, record = {...(sessionRecoveryRequest(id) || {request_id: crypto.randomUUID()}), prepared: false};
    keepSessionRecovery(id, {...record, message: '正在提交恢复任务；请求 ID 已固定。', error: false});
    sessionRecovering.add(id); renderSessionRecovery();
    try {
      acceptSessionRecovery(id, record, await post(`${API}/jobs/${id}/session/recover`, {}, {'X-Request-ID': record.request_id}));
    } catch (error) {
      keepSessionRecovery(id, {...record, message: `${errorText(error)} 请先查询此恢复请求；重试仍使用原 ID，不会自动重发。`, error: true});
      if (error.status === 401 || error.status === 403) await loadAccess();
    } finally { sessionRecovering.delete(id); if (selected === id) renderSessionRecovery(); }
  }
  async function querySessionRecovery() {
    const id = selected, record = sessionRecoveryRequest(id);
    if (!record || sessionRecovering.has(id)) return;
    sessionRecovering.add(id); renderSessionRecovery();
    try {
      acceptSessionRecovery(id, record, await json(`${API}/requests/${encodeURIComponent(record.request_id)}`));
    } catch (error) {
      keepSessionRecovery(id, {...record, message: error.status === 404 ? '暂未找到此恢复请求。保留原 ID，可稍后查询或使用原 ID 重试；不会自动新建请求。' : `${errorText(error)} 保留原恢复请求 ID，请核对后再继续。`, error: true});
    } finally { sessionRecovering.delete(id); if (selected === id) renderSessionRecovery(); }
  }
  async function prepareSessionRecoveryAttempt() {
    if (!currentJob || $('session-recover-new').disabled) return;
    const id = selected, record = sessionRecoveryRequest(id), revision = sessionRevision;
    sessionRecovering.add(id); renderSessionRecovery();
    try {
      const previous = await json(`${API}/jobs/${record.job_id}`);
      if (selected !== id || sessionRevision !== revision) return;
      if (previous?.id !== record.job_id || previous.recovery_for !== id) throw new Error('上次恢复任务身份未确认，不能生成新请求 ID。');
      if (previous.state !== 'finished') throw new Error('上次恢复任务尚未结束或状态未知，不能生成新请求 ID。');
      if (previous.queue?.state !== 'done') throw new Error(previous.queue?.state === 'blocked' ? '上次恢复任务仍保留资源。请打开上一恢复任务核查；未完成恢复时不能释放，不能绕过。' : '上次恢复任务的调度结束状态未确认，不能生成新请求 ID。');
      const problem = sessionRecoveryProblem();
      if (problem) throw new Error(problem);
      keepSessionRecovery(id, {request_id: crypto.randomUUID(), previous_job_id: record.job_id, prepared: true,
        message: '已确认上次恢复任务结束且调度资源已释放。新请求 ID 已准备，尚未提交。', error: false});
    } catch (error) {
      keepSessionRecovery(id, {...record, message: `${errorText(error)} 原请求 ID 保留，未创建新尝试。`, error: true});
    } finally { sessionRecovering.delete(id); if (selected === id) renderSessionRecovery(); }
  }
  function sessionAllowed(action) {
    const controller = sessionState?.controller;
    const actor = $('session-actor').value.trim();
    const pending = sessionPending();
    return !!(sessionFresh && !sessionBusy && !sessionActionBusy && sessionSupported() && access?.steer && !accessBusy &&
      (!referenceRun || action === 'stop') && sessionState?.live_owner && sessionState.accepting && sessionState.allowed_actions?.includes(action) &&
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
    $('session-query').disabled = !currentJob || !sessionSupported() || sessionActionBusy;
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
    message('session-pending', pending.length ? `未决请求：${pending.map(v => v.request_id).join('、')}。先查询回执；不会自动重发输入。` : '回执读取不触发设备动作。accepted 仅表示入队，completed 才表示该动作结束。');
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
      const receipt = await json(`${API}/jobs/${id}/session/actions/${encodeURIComponent(requestId)}`);
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
    $('reference-start').disabled = !readable || !access?.steer || accessBusy || sessionActionBusy || !!sessionPending().length;
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
      const value = await json(`${API}/jobs/${run.job}/session/actions/${run.ids[name]}`);
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
      if (!access?.steer || accessBusy) throw new Error('操作权限已改变，停止后续提交。');
      const d = referenceDescriptor(await json(`${API}/jobs/${run.job}/session`), run.job);
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
      const receipt = await post(`${API}/jobs/${run.job}/session/actions`, requestBody);
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
    const run = {job: selected, label, execute, options, ids: {capture: `gr-${label}-00`, sample: `gr-${label}-01`, marker: `gr-${label}-02`}, receipts: {}, cancelled: false};
    referenceRun = run;
    renderSessionControls(); renderReferenceResult(run);
    message('reference-message', '正在读取平台上的三份回执；尚未发送动作。');
    try {
      const initial = referenceDescriptor(await json(`${API}/jobs/${run.job}/session`), run.job); referenceCurrent(run);
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
    for (const [key, value] of [['标签', entry.label], ['原因', recovery.reason || '未记录'], ['报告范围', structuredText(recovery.scope)], ['证据来源', recovery.evidence_origin || '未记录'], ['记录更新', time(entry.updated_at)]]) info.append(el('dt', key), el('dd', text(value)));
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
  $('session-recover-submit').addEventListener('click', submitSessionRecovery);
  $('session-recover-query').addEventListener('click', querySessionRecovery);
  $('session-recover-new').addEventListener('click', prepareSessionRecoveryAttempt);
  $('session-recover-job').addEventListener('click', event => {
    const job = sessionRecoveryRequest()?.job_id;
    if (job) { event.preventDefault(); selectJob(job); }
  });
  $('session-recover-previous').addEventListener('click', event => {
    const job = sessionRecoveryRequest()?.previous_job_id;
    if (job) { event.preventDefault(); selectJob(job); }
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
  window.addEventListener('pagehide', () => { stopReference('页面已离开。'); ++sessionRevision; ++receiptRevision; if (sessionImageURL) URL.revokeObjectURL(sessionImageURL); });

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
  $('load-job-leases').addEventListener('click', loadJobLeases);
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
  $('submit-open-job').addEventListener('click', () => { if (submittedJob) { selectJob(submittedJob); $('detail-heading').focus(); } });
  $('submit-device-access').addEventListener('change', renderSubmission);
  $('submit-execution-policy').addEventListener('change', renderSubmission);
  $('prepare-recovery').addEventListener('click', () => {
    if ($('prepare-recovery').disabled || !currentJob) return;
    $('submit-device-access').value = 'recovery';
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
