/* One node, one shared snapshot. This page never captures screens or submits jobs. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const AUTO_MS = 10000;
  let revision = 0, controller = null, timer = null, loading = false, disposed = false;
  let latest = null, receivedAt = 0, lastStartedAt = 0, nodeLoaded = false, authLost = false;
  const text = value => value == null || value === '' ? '未知' : String(value);
  const numeric = (value, unit) => typeof value === 'number' && Number.isFinite(value) ? `${value}${unit}` : '未知';
  const time = value => value == null ? '未记录' : new Date(value * 1000).toLocaleString();
  function duration(value) {
    if (typeof value !== 'number' || !Number.isFinite(value)) return '未知';
    if (value < 60) return `${Math.floor(value)} 秒`;
    if (value < 3600) return `${Math.floor(value / 60)} 分钟`;
    return `${Math.floor(value / 3600)} 小时 ${Math.floor(value % 3600 / 60)} 分钟`;
  }
  function el(tag, value, className) {
    const node = document.createElement(tag);
    if (value != null) node.textContent = String(value);
    if (className) node.className = className;
    return node;
  }
  function facts(node, rows) { node.replaceChildren(...rows.flatMap(([label, value]) => [el('dt', label), el('dd', text(value))])); }
  function badge(node, value, kind = '') { node.textContent = value; node.className = `task-state ${kind}`; }
  function busy(value) {
    loading = value;
    $('refresh-fleet').disabled = value;
    $('refresh-fleet').setAttribute('aria-busy', String(value));
    $('refresh-fleet').textContent = value ? '读取中…' : '刷新快照';
  }
  function refreshNote() {
    $('refresh-note').textContent = authLost ? '登录已失效，自动刷新已暂停。' : document.hidden ? '页面不可见，自动刷新已暂停。' :
      !$('auto-refresh').checked ? '自动刷新已关闭，可手动读取快照。' : '可见时每 10 秒刷新页面快照；设备按节点策略采样。';
  }
  function schedule() {
    clearTimeout(timer);
    refreshNote();
    if (disposed || document.hidden || authLost || !$('auto-refresh').checked) return;
    timer = setTimeout(() => refresh(), Math.max(AUTO_MS, lastStartedAt + AUTO_MS - Date.now()));
  }
  async function json(path, signal) {
    const response = await fetch(path, {credentials: 'same-origin', cache: 'no-store', signal});
    if (response.status === 401 || response.status === 403 || (response.redirected && new URL(response.url).pathname === '/dash/login')) {
      const error = new Error('登录已失效，请重新登录后刷新。');
      error.auth = true;
      throw error;
    }
    if (!response.ok) throw new Error(`读取失败（HTTP ${response.status}），请重试。`);
    if (!(response.headers.get('content-type') || '').includes('application/json')) throw new Error('节点未返回 JSON，请检查登录或连接。');
    return response.json();
  }
  function processPolicyFacts(node) {
    if (!node?.capabilities?.includes('jobs.execution-policy-v1')) return [['进程执行策略能力', '未提供，执行范围未知']];
    return [['进程执行策略协议', 'jobs.execution-policy-v1'], ...[['process_group', '原进程组策略'], ['contained', '约束域策略']].map(([key, label]) => {
      const policy = node.execution?.process_policies?.[key];
      const description = !policy ? '未提供' : policy.available === true ? `可选择 · 后端：${text(policy.backend)} · 完成范围：${text(policy.completion_scope)}` : '不可用';
      return [`${label}（${key}）`, `${description}${policy?.reason ? `。${policy.reason}` : ''}`];
    })];
  }
  function renderNode(node) {
    nodeLoaded = true;
    $('node-summary').textContent = `${text(node.node_id)} · ${text(node.host?.hostname)}`;
    $('node-updated').textContent = `读取于 ${new Date().toLocaleTimeString()}`;
    $('node-message').textContent = '';
    $('node-message').classList.remove('task-error');
    $('node-details').hidden = false;
    facts($('node-info'), [['节点 ID', node.node_id], ['访问入口', location.origin], ['宿主系统', node.host?.platform],
      ['服务版本', `${text(node.service?.revision)}${node.service?.dirty === true ? '（有本地修改）' : ''}`],
      ['任务目录', node.storage?.jobs_root], ['数据目录', node.storage?.data_root], ...processPolicyFacts(node)]);
  }
  function hostProcessFacts(entry) {
    if (entry.role !== 'device_lease') return [];
    const host = entry.host_process;
    const status = !host ? '未提供' : ({alive: '同一宿主实例存活', previous_boot: '来自先前宿主启动', exited: '宿主 owner 已退出', zombie: '宿主 owner 已退出（僵尸态）', pid_reused: '宿主 PID 已被复用', legacy: '旧记录未记录宿主实例', unknown: '宿主实例无法确认'})[host.status] || '宿主实例无法确认';
    return [['宿主 owner 实例', status], ...(host ? [['宿主 PID', host.pid], ['宿主进程启动身份', host.start], ['宿主 boot ID', host.host_boot_id], ['宿主身份原因', host.reason]] : []),
      ['身份核查范围', '仅宿主 owner 实例；不证明原进程组已结束或设备已恢复。']];
  }
  function renderLock(lock) {
    const lease = lock?.lease, entries = Array.isArray(lease?.entries) ? lease.entries : [];
    const blocked = lease?.status === 'blocked';
    const unknown = lease?.status === 'unknown' || (lock?.status !== 'free' && lock?.status !== 'held');
    const known = !blocked && !unknown;
    const held = lease?.status === 'held' || lock?.status === 'held';
    const state = blocked ? '需要恢复' : unknown ? '身份或状态未知' : held ? (entries.some(e => e.role === 'device_lease') ? '设备租约占用' : entries.some(e => e.role === 'outer_queue') ? '外层队列占用' : '占用中 · 身份未确认') : '锁记录空闲';
    badge($('lock-state'), state, blocked || unknown || held ? 'unknown' : '');
    $('lock-owner').textContent = lock?.held_by ? `标签：${lock.held_by}` : held || blocked || unknown ? '未记录展示标签' : '没有占用标签';
    const reason = lease?.error || lock?.error;
    $('lock-message').textContent = blocked ? `恢复屏障保留；持有者退出或心跳过期不代表恢复完成。${reason ? ` ${reason}` : ''}` :
      unknown ? `身份或状态尚未确认，保持阻断。${reason ? ` ${reason}` : ''}` : reason ? `锁记录读取失败：${reason}` : held && lock.stale ?
      '心跳已过期，需核查租约状态；仍按占用显示。' : held ? '占用记录不代表正在测量；请结合租约与任务原件核对。' :
      '当前锁记录为空；设备可用性还需结合采样与恢复状态。';
    $('lock-message').className = `lock-message ${reason ? 'error' : blocked || unknown || held && lock.stale ? 'warning' : ''}`;
    facts($('lock-info'), known ? [['记录开始', lock.since], ['心跳距今', duration(lock.heartbeat_age_s)],
      ...(!held ? [['空闲时长', duration(lock.free_for_s)]] : [])] : []);
    const expanded = new Set([...$('lock-leases').querySelectorAll('details[open]')].map(node => node.dataset.lease));
    $('lock-leases').replaceChildren(...entries.map(entry => {
      const item = el('li'), details = el('details');
      details.dataset.lease = `${entry.directory || ''}:${entry.instance_id || ''}`;
      details.open = expanded.has(details.dataset.lease);
      const role = ({outer_queue: '外层队列锁', device_lease: '设备租约'})[entry.role] || '角色未报告';
      const state = ({active: '活动记录', command_pending: '命令待处理', recovery_required: '需要恢复', abandoned: '已遗留', unknown: '状态未知'})[entry.state] || text(entry.state);
      details.append(el('summary', `${role} · ${state}${entry.serial ? ` · ${entry.serial}` : ''}`));
      const holder = entry.holder_verified === true ? '已核验' : entry.holder_verified === false ? '核验未通过' : entry.role === 'device_lease' ? '不适用（设备租约）' : '未知';
      const info = el('dl', null, 'facts');
      facts(info, [['标签（仅展示）', entry.label], ['实例 ID', entry.instance_id], ['关联外层实例', entry.outer_instance_id], ['外层持有者核验', holder], ...hostProcessFacts(entry), ['原因', entry.reason], ['记录位置', entry.directory]]);
      details.append(info);
      if (entry.job_id) {
        const link = el('a', `关联任务 ${entry.job_id}`);
        link.href = `/dash/jobs#job=${encodeURIComponent(entry.job_id)}`;
        details.append(link);
      }
      item.append(details); return item;
    }));
    $('lock-identities-empty').hidden = entries.length > 0;
    $('lock-identities-empty').textContent = lease?.status === 'clear' ? '没有租约记录。' : '未取得实例记录；不据标签认定持有者。';
    const queue = Array.isArray(lock?.queue) ? lock.queue : [];
    $('lock-queue').replaceChildren(...queue.map(label => el('li', label)));
    $('queue-summary').textContent = known ? `队列与最近事件 · ${queue.length} 条排队记录` : '队列与最近事件';
    $('queue-empty').hidden = queue.length > 0;
    $('queue-empty').textContent = known ? '没有排队记录。' : '尚未取得队列。';
    const events = Array.isArray(lock?.recent) ? lock.recent : [];
    $('lock-events').replaceChildren(...events.map(event => {
      const item = el('li');
      item.append(el('time', event.at), el('span', `${({acquire: '获取', release: '释放', steal: '接管'})[event.action] || text(event.action)} · ${text(event.label)}`));
      return item;
    }));
    $('events-empty').hidden = events.length > 0;
    $('events-empty').textContent = known ? '没有近期记录。' : '尚未取得事件记录。';
  }
  function deviceCard(device) {
    const card = el('article', null, 'device-card');
    const header = el('div', null, 'device-head');
    const state = el('span');
    badge(state, device.online === true ? '在线 · 快照' : device.online === false ? '离线 · 快照' : '状态未知', device.online == null ? 'unknown' : '');
    header.append(el('h3', device.model || device.serial || '未命名设备'), state);
    card.append(header, el('p', `${text(device.serial)} · ${text(device.transport)}`, 'device-serial'));
    const metrics = el('dl', null, 'device-summary');
    for (const [name, value] of [['电量', numeric(device.battery?.level_pct, '%')], ['SoC 温度', numeric(device.thermal?.soc_max_c, ' °C')]]) {
      const cell = el('div'); cell.append(el('dt', name), el('dd', value)); metrics.append(cell);
    }
    const detail = el('dl', null, 'facts');
    facts(detail, [['系统', device.os || device.platform], ['连接状态', device.state],
      ['Root', device.root === true ? '已探测到' : device.root === false ? '未探测到' : '未知'],
      ['采样方式', ({full: '完整采样', light: '轻量采样', none: '未探测'})[device.probe_depth] || text(device.probe_depth)],
      ['前台应用', device.foreground], ['供电状态', device.battery?.on_battery === true ? '电池供电' : device.battery?.on_battery === false ? '非电池供电' : '未知']]);
    card.append(metrics, detail);
    const extra = el('details', null, 'device-details');
    extra.dataset.device = text(device.serial);
    extra.append(el('summary', '能力声明与未采集项'));
    const layers = el('ul', null, 'layer-list');
    for (const layer of device.layers || []) {
      const item = el('li');
      const heading = el('div', null, 'layer-heading');
      const support = el('span');
      badge(support, ({yes: '声明支持', if_met: '满足条件后支持', no: '不支持', unknown: '未知'})[layer.support] || '未知', layer.support === 'if_met' || layer.support === 'unknown' ? 'unknown' : '');
      heading.append(el('strong', ({sysparam: '系统参数', gray: '灰盒优化', shader: '着色器', measure: '测量'})[layer.layer] || text(layer.layer)), support);
      item.append(heading);
      if (layer.needs) item.append(el('span', `前提：${layer.needs}`, 'layer-reason'));
      if (layer.why) item.append(el('span', layer.why, 'layer-reason'));
      if (layer.blocked_by) item.append(el('span', `本设备阻塞：${layer.blocked_by}`, 'layer-reason layer-blocked'));
      layers.append(item);
    }
    extra.append(layers, el('p', '平台声明不等于已在本设备完成验证。', 'note'));
    const unknown = Array.isArray(device.unknown) ? device.unknown : [];
    if (unknown.length) {
      const list = el('ul', null, 'unknown-list');
      list.append(...unknown.map(reason => el('li', reason)));
      extra.append(list);
    } else if (!layers.children.length) extra.append(el('p', '没有能力或未采集项的详细记录。', 'note'));
    card.append(extra);
    return card;
  }
  function renderFleet(snapshot, failed = false) {
    renderLock(failed ? null : snapshot?.lock);
    const devices = snapshot?.devices;
    const hasSample = Array.isArray(devices);
    const age = snapshot?.age_s == null ? null : snapshot.age_s + Math.floor((Date.now() - receivedAt) / 1000);
    const stale = failed || snapshot?.stale || (age != null && age > (snapshot?.policy?.stale_after_s ?? 90));
    badge($('sample-state'), !hasSample ? '未采集' : stale ? '旧快照' : snapshot.deferred ? '采样暂缓 · 缓存快照' : snapshot.error ? '尝试失败 · 缓存快照' : snapshot.refreshing ? '更新中' : '已有快照',
      !hasSample || stale || snapshot?.error || snapshot?.deferred ? 'unknown' : '');
    $('sample-time').textContent = snapshot?.sampled_at == null ? '尚无成功采样记录' : `最后成功采样：${time(snapshot.sampled_at)} · ${age == null ? '年龄未知' : duration(age) + '前'}`;
    $('sample-attempt').textContent = snapshot?.attempted_at == null ? '尚无采样尝试记录' : `最近已知尝试：${time(snapshot.attempted_at)}${failed ? '（上次返回记录）' : ''}`;
    const notices = [];
    if (failed && hasSample) notices.push('连接失败，以下保留上次取得的设备快照，当前设备状态未确认。');
    if (snapshot?.error) notices.push(`最近一次尝试失败：${snapshot.error}`);
    if (stale && hasSample && !failed) notices.push('设备快照已过期，请勿据此判断当前可用性。');
    if (!failed && snapshot?.deferred) notices.push(`采样暂缓：${snapshot.deferred}${hasSample ? '。以下保留最后成功的设备快照，本次未更新设备数据。' : ''}`);
    if (!failed && snapshot?.refreshing) notices.push(hasSample ? '后台正在采样，当前展示上次成功取得的快照。' : '后台正在采样，尚未取得设备列表。');
    $('sample-message').textContent = notices.join('\n');
    $('sample-message').className = `fleet-notice ${snapshot?.error || snapshot?.deferred || stale ? 'warning' : ''}`;
    $('sample-message').hidden = !notices.length;
    $('device-count').textContent = hasSample ? `${devices.length} 台设备记录` : '尚未取得设备列表';
    const expanded = new Set([...$('devices').querySelectorAll('details[open]')].map(node => node.dataset.device));
    $('devices').replaceChildren(...(hasSample ? devices.map(deviceCard) : []));
    for (const details of $('devices').querySelectorAll('details')) details.open = expanded.has(details.dataset.device);
    $('devices-empty').hidden = hasSample && devices.length > 0;
    $('devices-empty').textContent = hasSample ? '上次成功采样未发现设备。' : '尚未取得设备列表；这不代表节点没有连接设备。';
    const notes = Array.isArray(snapshot?.notes) ? snapshot.notes : [];
    $('notes-list').replaceChildren(...notes.map(note => el('li', note)));
    $('fleet-notes').hidden = !notes.length;
    if (snapshot?.policy) {
      const p = snapshot.policy;
      $('sampling-policy').textContent = `设备采样策略：空闲采样间隔 ${numeric(p.fleet_every_free_s, ' 秒')}；占用、排队或待恢复时暂缓；释放后等待 ${numeric(p.free_grace_s, ' 秒')}，快照超过 ${numeric(p.stale_after_s, ' 秒')} 标记过期。页面刷新不会绕过此策略。`;
    }
  }
  async function refresh(manual = false) {
    if (loading || disposed || document.hidden) return;
    clearTimeout(timer);
    const version = ++revision;
    const requestController = new AbortController();
    controller = requestController;
    const signal = requestController.signal;
    const timeout = setTimeout(() => requestController.abort(), 15000);
    lastStartedAt = Date.now();
    busy(true);
    const current = () => version === revision && !disposed;
    const nodeRequest = manual || !nodeLoaded ? json('/dash/api/node', signal).then(node => {
      if (current()) renderNode(node);
    }).catch(error => {
      if (!current()) return;
      if (error.auth) authLost = true;
      $('node-message').textContent = `${error.name === 'AbortError' ? '节点身份读取超时。' : error.message}${nodeLoaded ? ' 上方仍为先前读取的信息。' : ''}`;
      $('node-message').classList.add('task-error');
    }) : Promise.resolve();
    try {
      const snapshot = await json('/dash/api/fleet?wait=5', signal);
      if (!current()) return;
      if (snapshot.schema_version !== 1) throw new Error('节点设备接口版本不支持，请检查服务版本。');
      latest = snapshot;
      receivedAt = Date.now();
      authLost = false;
      $('page-error').hidden = true;
      $('login-link').hidden = true;
      renderFleet(snapshot);
      if (!nodeLoaded && snapshot.node_id) $('node-summary').textContent = `节点 ${snapshot.node_id}`;
    } catch (error) {
      if (!current()) return;
      authLost = !!error.auth;
      $('page-error').textContent = error.name === 'AbortError' ? '读取超时，请重试。设备与锁的当前状态尚未确认。' : error.message;
      $('page-error').hidden = false;
      $('login-link').hidden = !authLost;
      renderFleet(latest, true);
    } finally {
      await nodeRequest;
      clearTimeout(timeout);
      if (current()) { busy(false); schedule(); }
    }
  }
  function suspend() {
    clearTimeout(timer);
    ++revision;
    controller?.abort();
    busy(false);
    refreshNote();
  }
  $('refresh-fleet').addEventListener('click', () => refresh(true));
  $('auto-refresh').addEventListener('change', schedule);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) suspend();
    else if ($('auto-refresh').checked && !authLost && Date.now() - lastStartedAt >= AUTO_MS) refresh();
    else schedule();
  });
  window.addEventListener('pagehide', () => { disposed = true; suspend(); });
  window.addEventListener('pageshow', event => { if (event.persisted) { disposed = false; schedule(); } });
  refresh(true);
})();
