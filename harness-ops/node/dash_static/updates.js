/* Host-only update jobs. Pending submissions keep their original node and body. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const KEY = 'hgamey.pending-node-update', PENDING_SCHEMA = 'hgamey/node-update-pending@1';
  const LOCK = 'hgamey.pending-node-update.lock';
  const REQUEST_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/, RUN_ID = /^[0-9a-f]{32}$/;
  const components = ['phonefarm', 'game_opt_loop', 'harness-ops'];
  let identity = null, report = null, fresh = false, refreshing = false;
  let access = null, accessBusy = false, accessError = '', actionBusy = false;
  let pending = null, storageError = '';
  const object = value => !!value && typeof value === 'object' && !Array.isArray(value);
  const time = value => Number.isFinite(value) ? new Date(value * 1000).toLocaleString() : '未记录';
  const timestamp = value => typeof value === 'number' && Number.isFinite(value) && value >= 0;
  const json = value => JSON.stringify(value, null, 2);
  const el = (tag, value) => { const item = document.createElement(tag); item.textContent = String(value); return item; };
  function message(id, value, error = false) { $(id).textContent = value; $(id).classList.toggle('error', error); }
  function facts(id, rows) { $(id).replaceChildren(...rows.flatMap(([key, value]) => [el('dt', key), el('dd', value ?? '未记录')])); }
  const errorText = error => error.name === 'AbortError' ? '请求超时，结果未确认。' : error.message;
  const locksAvailable = () => typeof globalThis.navigator?.locks?.request === 'function';
  const lockUnavailable = '当前浏览器不支持 Web Locks，不能安全保存或解除跨标签页的原请求。请使用支持该功能的 HTTPS / 本机浏览器，或通过 CLI 操作；已有请求仍可只读查回。';
  async function withPendingLock(action) {
    if (!locksAvailable()) throw new Error(lockUnavailable);
    return navigator.locks.request(LOCK, {mode:'exclusive'}, action);
  }
  function targetValid(target) {
    if (!object(target) || !RUN_ID.test(target.node_id) || typeof target.entry !== 'string') return false;
    try { const u = new URL(target.entry); return ['http:', 'https:'].includes(u.protocol) && u.origin === target.entry; } catch (_) { return false; }
  }
  function validArgv(argv) {
    if (!Array.isArray(argv) || argv.length !== 2 || !argv.every(value => typeof value === 'string')) return false;
    if (argv[0] === '--rollback') return RUN_ID.test(argv[1]);
    if (argv[0] !== '--git') return false;
    const split = argv[1].indexOf('@'), component = argv[1].slice(0, split), ref = argv[1].slice(split + 1);
    return split > 0 && components.includes(component) && !!ref && ref.length <= 256 && !/^[-]/.test(ref) && !/[\s\x00-\x1f\x7f]/.test(ref);
  }
  function validBody(body) {
    return object(body) && Object.keys(body).sort().join(',') === 'argv,bin,device_access,resources' &&
      body.bin === 'update_node' && body.device_access === 'none' && Array.isArray(body.resources) &&
      body.resources.length === 0 && validArgv(body.argv);
  }
  function validateReport(value, nodeId) {
    const updater = value?.updater, maintenance = value?.maintenance;
    if (value?.schema !== 'hgamey/node-updates@1' || value.scope !== 'host-only' || value.node_id !== nodeId ||
        !timestamp(value.observed_at) || !object(value.layout) ||
        !['phonefarm', 'game_opt_loop', 'harness_ops', 'state_dir'].every(key => typeof value.layout[key] === 'string') ||
        !object(updater) || updater.contract !== 'hgamey/node-update@1' || typeof updater.available !== 'boolean' ||
        !['observed', 'stale', 'unknown'].includes(updater.identity_state) ||
        updater.available !== (updater.identity_state === 'observed') || !timestamp(updater.observed_at) ||
        !(updater.reason === null || typeof updater.reason === 'string') ||
        !(updater.executable === null || typeof updater.executable === 'string') ||
        !object(maintenance) || !['clear', 'blocked', 'unknown'].includes(maintenance.state) ||
        !timestamp(maintenance.observed_at) || typeof maintenance.reason !== 'string' ||
        maintenance.all_participants_verified !== false ||
        (maintenance.state === 'blocked' ? !RUN_ID.test(maintenance.run_id) : maintenance.run_id !== null) ||
        !Array.isArray(value.limitations) || !value.limitations.every(item => typeof item === 'string')) {
      throw new Error('安装状态结构或节点身份不一致，已拒绝使用；请核对服务版本与目标节点。');
    }
    return value;
  }
  function readPending() {
    try {
      const raw = localStorage.getItem(KEY);
      if (raw === null) { pending = null; storageError = ''; return; }
      const value = JSON.parse(raw);
      if (value?.schema !== PENDING_SCHEMA || typeof value.id !== 'string' || !REQUEST_ID.test(value.id) || !targetValid(value.target) || !validBody(value.body)) throw new Error('invalid pending');
      pending = value; storageError = '';
    } catch (_) {
      pending = null;
      storageError = '无法读取浏览器中保留的原更新请求。为避免重复执行，新请求已禁用；请保留浏览器记录，由维护者核对原 ID、节点和参数。';
    }
  }
  async function storePending(attempt) {
    // Only browser storage is locked; the network never holds this lock.
    await withPendingLock(() => {
      HGTarget.assert(attempt.target);
      if (localStorage.getItem(KEY) !== null) throw new Error('此入口已有保留的更新请求，请先查询原请求。');
      const raw = JSON.stringify(attempt);
      localStorage.setItem(KEY, raw);
      if (localStorage.getItem(KEY) !== raw) throw new Error('未能确认原请求已保存，没有发送提交。');
      pending = attempt;
    });
  }
  async function clearPending(attempt) {
    await withPendingLock(() => {
      HGTarget.assert(attempt.target);
      const saved = JSON.parse(localStorage.getItem(KEY) || 'null');
      if (JSON.stringify(saved) !== JSON.stringify(attempt)) throw new Error('浏览器中保留的请求已变化，未解除保留；请重新核对。');
      localStorage.removeItem(KEY);
      if (localStorage.getItem(KEY) !== null) throw new Error('无法解除浏览器中的原请求保留；仍禁止新提交。');
      pending = null;
    });
  }
  async function request(path, target, body, requestId) {
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 15000);
    const init = {credentials:'same-origin', cache:'no-store', signal:controller.signal};
    if (body !== undefined) Object.assign(init, {method:'POST', body:JSON.stringify(body),
      headers:{'Content-Type':'application/json', 'X-HG':'1', ...(requestId ? {'X-Request-ID':requestId} : {})}});
    try {
      const response = await HGTarget.fetch('/dash/api/' + path, init, {target});
      const isJSON = (response.headers.get('content-type') || '').includes('application/json');
      let value;
      try { if (isJSON) value = await response.json(); } catch (_) { throw new Error('节点返回了损坏的 JSON，结果未确认。'); }
      if (!response.ok) {
        const detail = typeof value?.error === 'string' ? value.error : value?.error?.message;
        const error = new Error((({401:'查看登录已失效。', 403:'操作未获授权，请重新解锁。', 409:'请求或目标冲突。'})[response.status] ||
          '请求失败（HTTP ' + response.status + '）。') + (detail ? ' ' + detail : ''));
        error.status = response.status; throw error;
      }
      if (!isJSON) throw new Error('节点未返回 JSON，结果未确认；请核对登录和连接。');
      return value;
    } finally { clearTimeout(timer); }
  }
  function commonProblem() {
    return storageError || (pending ? '上次更新请求尚未确认，先按原 ID 查询；不会另开请求或自动重发。' : '') ||
      (actionBusy ? '正在处理原请求，请等待本次请求返回。' : '') ||
      (!locksAvailable() ? lockUnavailable : '') ||
      (refreshing ? '正在读取安装与维护状态。' : '') ||
      HGTarget.problem() ||
      (!fresh ? '当前安装状态未确认，请点击「刷新状态」。' : '') ||
      (!identity?.capabilities?.includes('node.updates-v1') ? '此节点未提供 node.updates-v1，请核对部署版本并刷新目标身份。' : '') ||
      (!HGTarget.canWrite() ? '当前节点未提供目标绑定写入能力。' : '') ||
      (!report?.updater.available ? '安全更新入口不可用：' + (report?.updater.reason || '身份未确认') + '。请核对安装，必要时由维护者明确重启服务后再检查。' : '') ||
      (report?.maintenance.state === 'unknown' ? '维护记录未知或不可读，必须先核对记录，不能据此更新或猜测回滚 ID。' : '') ||
      (accessBusy || !access?.steer ? '当前为只读模式；请解锁任务操作权限。' : '');
  }
  function bodyFor(kind) {
    return {bin:'update_node', argv:kind === 'update' ? ['--git', $('update-component').value + '@' + $('update-ref').value.trim()] :
      ['--rollback', $('rollback-run').value.trim()], device_access:'none', resources:[]};
  }
  function operationProblem(kind) {
    const common = commonProblem(); if (common) return common;
    const body = bodyFor(kind);
    if (kind === 'update' && report.maintenance.state !== 'clear') return '安装存在未完成维护，只允许精确回滚所记录的原 run。';
    if (!validBody(body)) return kind === 'update' ? '请选择单个组件并填写明确的 Git ref（不能含空白或以 - 开头）。' : '请填写原更新的完整 32 位小写 run ID。';
    if (kind === 'rollback' && report.maintenance.state === 'blocked' && body.argv[1] !== report.maintenance.run_id) return '只允许回滚维护记录中的原 run ID。';
    if (!globalThis.crypto?.randomUUID) return '请通过 HTTPS 或本机地址访问后提交。';
    return '';
  }
  function render() {
    document.querySelector('main').dataset.stale = String(!!report && !fresh);
    $('refresh-status').disabled = refreshing || actionBusy || accessBusy;
    $('access-toggle').disabled = accessBusy || actionBusy || !access?.steer_configured;
    $('access-toggle').textContent = accessBusy ? '读取权限…' : access?.steer ? '锁定任务操作' : '解锁任务操作';
    $('unlock-submit').disabled = accessBusy || actionBusy;
    message('access-message', accessError || (!access ? '尚未取得权限，操作保持只读。' : !access.steer_configured ?
      '此节点尚未配置掌舵密码，操作保持只读。' : access.steer ? '已解锁。提交仍需核对目标、入口与维护状态。' : '当前为只读模式，可查看状态和查询原请求。'), !!accessError);
    message('action-message', commonProblem() || (report?.maintenance.state === 'blocked' ? '安装维护未完成：只可对下方记录的原 run 提交回滚。' : '核对目标和参数后提交。队列受理不代表更新已经完成。'));
    for (const kind of ['update', 'rollback']) {
      $(kind + '-submit').disabled = !!operationProblem(kind);
      $(kind + '-preview').textContent = json(bodyFor(kind));
    }
    for (const id of ['update-component', 'update-ref', 'rollback-run']) $(id).disabled = actionBusy || !!pending || !!storageError;
    if (report?.maintenance.state === 'blocked') $('rollback-run').readOnly = true; else $('rollback-run').readOnly = false;
    $('lookup-id').readOnly = !!pending;
    if (pending) $('lookup-id').value = pending.id;
    $('lookup-submit').disabled = actionBusy || !!(pending ? HGTarget.problem(pending.target) : HGTarget.problem()) || !REQUEST_ID.test($('lookup-id').value.trim());
    $('pending-details').hidden = !pending;
    $('pending-json').textContent = pending ? json(pending.body) : '';
    facts('pending-info', pending ? [['原 request ID', pending.id], ['原入口', pending.target.entry], ['原节点', pending.target.node_id]] : []);
    message('pending-message', storageError || (pending ? HGTarget.problem(pending.target) || '原请求已保留。只按原节点与原参数核对，暂不创建新请求。' : '此浏览器入口没有未确认的更新请求。'), !!storageError || !!(pending && HGTarget.problem(pending.target)));
  }
  function renderReport(value) {
    $('observed-at').textContent = time(value.observed_at);
    message('updater-message', value.updater.available ? '安全更新入口与服务启动时的文件观察一致；这不等于整个安装已验收。' :
      '安全更新入口不可用。' + (value.updater.reason || '未提供原因') + '。请维护者核对安装；文件变化后需明确重启服务再检查，页面不会自动重启。', !value.updater.available);
    facts('installation-info', [['phonefarm', value.layout.phonefarm], ['game_opt_loop', value.layout.game_opt_loop],
      ['harness-ops', value.layout.harness_ops], ['持久状态目录', value.layout.state_dir],
      ['更新入口', value.updater.executable], ['入口身份', value.updater.identity_state], ['入口观察时间', time(value.updater.observed_at)]]);
    const state = value.maintenance.state;
    $('maintenance-state').textContent = {clear:'没有记录的维护义务', blocked:'维护未完成', unknown:'维护状态未知'}[state];
    $('maintenance-state').className = 'task-state ' + (state === 'clear' ? 'finished' : state === 'blocked' ? 'interrupted' : 'unknown');
    message('maintenance-message', value.maintenance.reason, state === 'unknown');
    facts('maintenance-info', [['原 run ID', value.maintenance.run_id || '无可用记录'], ['观察时间', time(value.maintenance.observed_at)], ['参与者停止核验', '未进行；不能由本状态推断']]);
    if (state === 'blocked') $('rollback-run').value = value.maintenance.run_id;
    message('rollback-help', state === 'blocked' ? '仅允许回滚此维护记录的原 run；服务提交时会再次核查。' : '请从更新记录核对原 run ID；不会从任务 ID 推断。');
    $('status-json').textContent = json(value);
    $('limitations').replaceChildren(...value.limitations.map(item => el('li', item)));
  }
  async function refresh() {
    if (refreshing || actionBusy || accessBusy) return;
    refreshing = true; fresh = false; render(); message('status-message', report ? '正在读取；下方保留上次记录。' : '正在读取安装与维护状态…');
    try {
      identity = await HGTarget.ready();
      const target = HGTarget.binding();
      const reads = await Promise.allSettled([
        identity.capabilities?.includes('node.updates-v1') ? request('node-updates', target) : Promise.reject(new Error('节点未提供 node.updates-v1。请核对服务版本后刷新目标身份。')),
        request('access', target)
      ]);
      if (reads[1].status === 'fulfilled' && typeof reads[1].value?.steer === 'boolean' && typeof reads[1].value?.steer_configured === 'boolean') {
        access = reads[1].value; accessError = ''; $('access-login').hidden = true;
      } else { access = null; accessError = reads[1].status === 'rejected' ? errorText(reads[1].reason) : '权限响应不可读。'; $('access-login').hidden = reads[1].reason?.status !== 401; }
      if (reads[0].status === 'rejected') throw reads[0].reason;
      HGTarget.assert(target);
      report = validateReport(reads[0].value, target.node_id); fresh = true;
      renderReport(report); $('status-login').hidden = true;
      message('status-message', '状态已读取；不会自动轮询。安装变化或任务结束后，请显式刷新核对。');
    } catch (error) {
      message('status-message', errorText(error) + (report ? ' 下方保留上次记录，不能代表当前状态。' : ''), true);
      $('status-login').hidden = error.status !== 401;
    } finally { refreshing = false; document.querySelector('main').dataset.stale = String(!!report && !fresh); render(); }
  }
  async function acceptJob(job, attempt, saved) {
    HGTarget.assert(attempt.target);
    if (!/^[0-9a-f]{12}$/.test(job?.id) || job.request_id !== attempt.id || job.bin !== 'update_node' ||
        !validArgv(job.argv) || (attempt.body && JSON.stringify(job.argv) !== JSON.stringify(attempt.body.argv)) ||
        job.device_access !== 'none' || !Array.isArray(job.queue?.resources) || job.queue.resources.length !== 0 ||
        (Object.hasOwn(job, 'exe') && job.exe !== null)) throw new Error('返回任务的原请求 ID、命令、参数或资源声明不一致，未解除原请求保留。');
    let cleanupError = '';
    if (saved) {
      try { await clearPending(attempt); }
      catch (error) { cleanupError = ' 原请求仍保留：' + errorText(error); }
    }
    $('open-job').href = HGTarget.url('/dash/jobs#job=' + job.id, attempt.target);
    $('open-job').hidden = false;
    message('request-message', '已核对任务 ' + job.id + ' · request ' + attempt.id + '。这是任务受理记录；执行与重启结果请进入任务页核对。' + cleanupError, !!cleanupError);
    fresh = false;
  }
  async function submit(kind) {
    if (actionBusy) return;
    readPending(); render();
    const problem = operationProblem(kind); if (problem) { message('request-message', problem, true); return; }
    const attempt = {schema:PENDING_SCHEMA, id:crypto.randomUUID(), target:HGTarget.binding(), body:bodyFor(kind)};
    actionBusy = true; $('open-job').hidden = true; render();
    try { HGTarget.assert(attempt.target); await storePending(attempt); }
    catch (error) { actionBusy = false; readPending(); message('request-message', '没有发送提交：' + errorText(error), true); render(); return; }
    fresh = false; render();
    message('request-message', '正在提交一次；原 request ID 与完整参数已保存。');
    try { await acceptJob(await request('jobs', attempt.target, attempt.body, attempt.id), attempt, true); }
    catch (error) {
      message('request-message', errorText(error) + ' 保留原 ID 与完整参数，请先查询原请求；不会自动重发或创建另一请求。', true);
      if ([401, 403].includes(error.status)) { access = null; accessError = errorText(error); $('access-login').hidden = error.status !== 401; }
    } finally { actionBusy = false; document.querySelector('main').dataset.stale = String(!!report); render(); }
  }
  async function lookup() {
    if (actionBusy) return;
    const id = $('lookup-id').value.trim();
    if (!REQUEST_ID.test(id)) { message('request-message', '请填写有效的原 request ID。', true); return; }
    if (pending && pending.id !== id) { message('request-message', '存在未确认请求，请先查询保留的原 ID。', true); return; }
    const saved = pending?.id === id, attempt = saved ? pending : {id, target:HGTarget.binding()};
    actionBusy = true; $('open-job').hidden = true; render(); message('request-message', '正在查询原请求，不提交更新。');
    try { await acceptJob(await request('requests/' + encodeURIComponent(id), attempt.target), attempt, saved); }
    catch (error) {
      message('request-message', error.status === 404 ? '暂未找到原请求。保留原 ID 与参数；这不能证明此前未执行，请核对原节点后再查。' : errorText(error), true);
      $('status-login').hidden = error.status !== 401;
    } finally { actionBusy = false; render(); }
  }
  function closeUnlock() { $('unlock-form').hidden = true; $('steer-password').value = ''; }
  async function setAccess(unlock) {
    if (accessBusy || actionBusy) return;
    accessBusy = true;
    const body = unlock ? {pw:$('steer-password').value} : {};
    $('steer-password').value = ''; message('unlock-message', ''); render();
    try {
      await request('steer/' + (unlock ? 'unlock' : 'lock'), HGTarget.binding(), body);
      const value = await request('access', HGTarget.binding());
      if (typeof value?.steer !== 'boolean' || typeof value?.steer_configured !== 'boolean') throw new Error('权限响应不可读。');
      access = value; accessError = ''; $('access-login').hidden = true; closeUnlock();
    } catch (error) { access = null; accessError = errorText(error); message('unlock-message', accessError, true); $('access-login').hidden = error.status !== 401; }
    finally { accessBusy = false; render(); }
  }
  $('refresh-status').addEventListener('click', refresh);
  $('update-form').addEventListener('submit', event => { event.preventDefault(); submit('update'); });
  $('rollback-form').addEventListener('submit', event => { event.preventDefault(); submit('rollback'); });
  $('lookup-form').addEventListener('submit', event => { event.preventDefault(); lookup(); });
  for (const id of ['update-component', 'update-ref', 'rollback-run', 'lookup-id']) $(id).addEventListener('input', render);
  $('access-toggle').addEventListener('click', () => { if (access?.steer) setAccess(false); else { $('unlock-form').hidden = false; $('steer-password').focus(); } });
  $('unlock-form').addEventListener('submit', event => { event.preventDefault(); setAccess(true); });
  $('unlock-dismiss').addEventListener('click', closeUnlock);
  document.addEventListener('hgamey-target-change', () => {
    fresh = false;
    if (report) message('status-message', '目标身份观察已变化；下方保留上次安装记录，请刷新状态后再操作。');
    render();
  });
  window.addEventListener('storage', event => { if (event.key === KEY || event.key === null) { readPending(); render(); } });
  readPending(); render(); refresh();
})();
