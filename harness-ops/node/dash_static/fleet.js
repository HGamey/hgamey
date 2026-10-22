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
  function renderNode(node) {
    nodeLoaded = true;
    $('node-summary').textContent = `${text(node.node_id)} · ${text(node.host?.hostname)}`;
    $('node-updated').textContent = `读取于 ${new Date().toLocaleTimeString()}`;
    $('node-message').textContent = '';
    $('node-message').classList.remove('task-error');
    $('node-details').hidden = false;
    facts($('node-info'), [['节点 ID', node.node_id], ['访问入口', location.origin], ['宿主系统', node.host?.platform],
      ['服务版本', `${text(node.service?.revision)}${node.service?.dirty === true ? '（有本地修改）' : ''}`],
      ['任务目录', node.storage?.jobs_root], ['数据目录', node.storage?.data_root]]);
  }
  function renderLock(lock) {
    const known = lock?.status === 'free' || lock?.status === 'held';
    const held = lock?.status === 'held';
    badge($('lock-state'), known ? (held ? '占用中' : '空闲') : '未知', !known || held ? 'unknown' : '');
    $('lock-owner').textContent = held ? text(lock.held_by) : known ? '未记录持锁者' : '无法确认占用情况';
    $('lock-message').textContent = lock?.error ? `锁记录读取失败：${lock.error}` : held && lock.stale ?
      '持锁心跳已过期，仍按占用显示；需要核查持锁进程与恢复状态。' : held ? '持锁者可能正在执行任务，请结合任务记录核对。' :
      known ? '当前锁记录为空；设备可用性还需结合采样与恢复状态。' : '尚未取得可信的锁状态。';
    $('lock-message').className = `lock-message ${lock?.error ? 'error' : held && lock.stale ? 'warning' : ''}`;
    facts($('lock-info'), known ? [['记录开始', lock.since], ['心跳距今', duration(lock.heartbeat_age_s)],
      ...(!held ? [['空闲时长', duration(lock.free_for_s)]] : [])] : []);
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
      $('sampling-policy').textContent = `设备采样策略：空闲间隔 ${numeric(p.fleet_every_free_s, ' 秒')}，占用间隔 ${numeric(p.fleet_every_held_s, ' 秒')}；释放后等待 ${numeric(p.free_grace_s, ' 秒')}，快照超过 ${numeric(p.stale_after_s, ' 秒')} 标记过期。页面刷新不会绕过此策略。`;
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
