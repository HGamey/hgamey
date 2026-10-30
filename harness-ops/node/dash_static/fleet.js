/* Joined computers report to this center; their phones are re-sampled only on an explicit request. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const AUTO_MS = 10000;
  let revision = 0, controller = null, timer = null, loading = false, disposed = false;
  let latest = null, receivedAt = 0, lastStartedAt = 0, authLost = false;
  let localIdentity = null, localQueue = null, localQueueError = '', localReceivedAt = 0, directoryKnown = false, directoryFailed = false;
  let selected = new URLSearchParams(location.hash.slice(1)).get('node') || 'local', entries = new Map();
  if (!/^[a-z0-9_-]{1,64}$/.test(selected)) selected = 'local';
  let access = null, accessBusy = false, accessError = '', editor = null, writing = false, inviteBusy = false;
  let runtimeBusy = false, runtimeNotice = '', runtimeError = false;
  let deviceRegistry = null, registryBusy = false, registryWriting = false, registryAttempted = false, registryController = null;
  let registryError = '', registryNotice = '', registryReadAt = null, deviceEditor = null;
  const CONNECTION = {ready: '已连接', disconnected: '已断开'};
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
  const sourceDirty = value => value === true ? '有本地修改' : value === false ? '声明无本地修改' : '未知';
  function exportCacheFacts(node) {
    const cache = node?.storage?.export_cache;
    if (!node?.capabilities?.includes('jobs.export-cache-root-v1') || !cache) return [['目录导出缓存位置', '未提供；旧节点位置未知，不从任务目录推断。']];
    return [['导出缓存模式', ({legacy_job_directory: '作业目录内（legacy_job_directory）', external: '独立缓存根（external）'})[cache.mode] || '未知'],
      ['导出缓存启动观察', ({available: '启动时已打开；不保证当前可写空间', unavailable: '不可用；不会退回作业目录'})[cache.availability] || '未知'],
      ['配置缓存根', cache.configured_root === null ? '未显式配置' : cache.configured_root], ['有效缓存根', cache.effective_root ?? '未取得'],
      ['缓存位置原因', cache.reason ?? '未报告错误'],
      ['缓存适用范围', cache.scope === 'directory_export' ? '仅当前作业的目录导出派生件；单文件下载与历史来源不依赖此缓存。实际请求仍需核对导出错误。' : cache.scope || '未知']];
  }
  function serviceFacts(service) {
    const runtime = service?.runtime;
    return [['服务实例 ID', service?.instance_id], ['服务启动时源码树 revision', service?.revision], ['启动源码树修改状态', sourceDirty(service?.dirty)],
      ['源码观察来源', service?.source_kind === 'startup_checkout_observation' ? '服务启动时源码树观察' : service?.source_kind || '旧记录未提供'], ['源码观察时间', time(service?.observed_at)],
      ['入口路径', runtime?.entry_path], ['Python 版本', runtime?.python_version], ['宿主运行身份观察时间', time(runtime?.observed_at)],
      ['安装构建仓库声明', runtime?.build?.repository], ['安装构建 revision 声明', runtime?.build?.revision], ['安装构建修改声明', sourceDirty(runtime?.build?.dirty)]];
  }
  function grayCompatibilityFacts(record) {
    const value = record?.compatibility?.gray_control;
    return [['灰层网关兼容', ({compatible: '兼容新建灰层会话', incompatible: '不兼容新建灰层会话', unknown: '兼容性未知'})[value?.status] || '未提供'],
      ['网关所需声明', value?.required?.join('、') || '未提供'], ['缺失声明', Array.isArray(value?.missing) ? value.missing.join('、') || '无缺项（仅此网关）' : '未知'],
      ['兼容性原因', value?.reason || '未报告'], ['兼容性边界', '仅此次配置 helper 观察；目录快照不是当前准入，旧 owner 和设备恢复另行核查。']];
  }
  function renderPrograms(node) {
    const entries = Object.entries(node?.execution?.programs || {});
    $('node-programs').replaceChildren(...entries.map(([name, record]) => {
      const detail = el('details', null, 'command-details'), info = el('dl', null, 'facts'), identity = record?.identity;
      const state = ({observed: '已观察配置入口', stale: '上次观察已过期', unknown: '身份未知'})[record?.state] || '身份未知';
      detail.append(el('summary', `${name} · ${state}${identity?.package_version ? ' · ' + identity.package_version : ''}`));
      facts(info, [['缓存状态', state], ['观察时间', time(record?.observed_at)], ['配置解析路径', record?.resolved_path], ['原因', record?.reason || '未报告原因'],
        ['声明适用范围', record?.state === 'observed' ? '此次配置入口观察；不等于某次作业实际执行身份。' : '如保留声明，仅是上次观察，当前入口身份未知。'],
        ['程序自报名称', identity?.program], ['程序包版本', identity?.package_version], ['构建来源 revision 声明', identity?.build?.source_revision], ['构建来源修改声明', sourceDirty(identity?.build?.source_dirty)],
        ['构建 target', identity?.build?.target], ['构建 profile', identity?.build?.profile], ['自报支持协议', identity?.supported_protocols?.join('、')], ...(name === 'phonefarm' ? grayCompatibilityFacts(record) : []), ['文件观察（stat）', record?.file ? JSON.stringify(record.file) : '未记录']]);
      detail.append(info); return detail;
    }));
    if (!entries.length) $('node-programs').append(el('p', '未记录配置程序观察；不能由命令名或相邻源码推断实际版本。', 'note'));
  }
  function renderRuntimeRefresh() {
    const entry = remoteEntry(), local = selected === 'local', supported = localIdentity?.capabilities?.includes('node.runtime-v1');
    $('refresh-runtime').hidden = !local;
    $('refresh-runtime').disabled = !local || !supported || !access?.steer || accessBusy || !HGTarget.canWrite() || runtimeBusy || loading || writing;
    $('refresh-runtime').setAttribute('aria-busy', String(runtimeBusy));
    $('refresh-runtime').textContent = runtimeBusy ? '正在观察本节点程序…' : '重新观察本节点配置程序';
    message('runtime-refresh-help', !local ? `以下程序身份来自 ${time(entry?.snapshot?.at)} 上传的快照${entryStale(entry || {}) ? '；这台电脑已断开或还没有快照，当前身份未知' : ''}。` : runtimeBusy ? '正在执行有界宿主身份查询，下方保留上次观察；不操作设备，不自动重试。' : runtimeNotice || (!supported ? '此节点未声明 node.runtime-v1，不能刷新程序观察。' : !access?.steer || accessBusy ? '读取缓存无需解锁；重新执行程序身份观察需先解锁目录操作。' : '仅此按钮执行新的程序身份观察；自动刷新只读取缓存并检查文件变化。'), local && runtimeError);
  }
  async function refreshRuntime() {
    if ($('refresh-runtime').disabled || selected !== 'local') return;
    const expected = localIdentity.node_id;
    runtimeBusy = true; runtimeNotice = ''; runtimeError = false; clearTimeout(timer); renderManagement();
    try {
      const node = await post('/dash/api/node/runtime/refresh', {});
      if (node.node_id !== expected) throw new Error('程序观察响应的节点身份不匹配。');
      localIdentity = node;
      runtimeNotice = '程序观察请求已完成，请核对各项 observed / stale / unknown；没有刷新设备、创建任务或改写历史。';
      if (!disposed && selected === 'local') renderNode(node);
    } catch (error) {
      runtimeError = true; runtimeNotice = `程序观察结果未取得：${error.message || '连接中断'}。下方保留上次观察，当前身份尚未确认；请读取节点缓存核对，不会自动重试。`;
      if ([401, 403].includes(error.status)) await loadAccess();
    } finally { runtimeBusy = false; if (!disposed) { renderManagement(); schedule(); } }
  }
  function badge(node, value, kind = '') { node.textContent = value; node.className = `task-state ${kind}`; }
  function message(id, value, error = false) { $(id).textContent = value; $(id).classList.toggle('task-error', error); }
  function remoteEntry() { return selected === 'local' ? null : entries.get(selected); }
  function entryStale(entry) { return directoryFailed || entry.stale || entry.connection?.status !== 'ready' || !entry.snapshot; }
  // Only this node's own pages link out: a joined computer's dashboard is not reachable through the center.
  const nodePageURL = (page, job = '') => selected === 'local' ? HGTarget.url(`/dash/${page}${job ? `#job=${encodeURIComponent(job)}` : ''}`) : null;
  function jobLink(id) {
    const url = nodePageURL('jobs', id), link = el(url ? 'a' : 'span', `任务 ${id}`);
    if (url) link.href = url;
    return link;
  }
  function busy(value) {
    loading = value;
    $('refresh-fleet').disabled = value;
    $('refresh-fleet').setAttribute('aria-busy', String(value));
    $('refresh-fleet').textContent = value ? '读取中…' : '刷新目录与快照';
    renderManagement();
  }
  function refreshNote() {
    $('refresh-note').textContent = authLost ? '登录已失效，自动刷新已暂停。' : document.hidden ? '页面不可见，自动刷新已暂停。' :
      !$('auto-refresh').checked ? '自动刷新已关闭，可手动读取目录与快照。' : '可见时每 10 秒读取目录；其他电脑的手机需明确点击才重新采样。';
  }
  function schedule() {
    clearTimeout(timer);
    refreshNote();
    if (disposed || document.hidden || authLost || !$('auto-refresh').checked) return;
    timer = setTimeout(() => refresh(), Math.max(AUTO_MS, lastStartedAt + AUTO_MS - Date.now()));
  }
  async function json(path, signal, body, method = body === undefined ? 'GET' : 'POST') {
    const response = await HGTarget.fetch(path, {credentials: 'same-origin', cache: 'no-store', signal, method,
      ...(method === 'GET' ? {} : {headers: {'Content-Type': 'application/json', 'X-HG': '1'}, ...(body === undefined ? {} : {body: JSON.stringify(body)})})});
    if (response.status === 401 || response.status === 403 || (response.redirected && new URL(response.url).pathname === '/dash/login')) {
      const error = new Error('登录已失效，请重新登录后刷新。');
      error.auth = true;
      error.status = response.status;
      throw error;
    }
    if (!(response.headers.get('content-type') || '').includes('application/json')) {
      const error = new Error(response.ok ? '节点未返回 JSON，请检查登录或连接。' : `请求失败（HTTP ${response.status}）`);
      if (!response.ok) error.status = response.status;
      throw error;
    }
    const value = await response.json();
    if (!response.ok) {
      const error = new Error(typeof value.error === 'string' ? value.error : value.error?.message || `请求失败（HTTP ${response.status}）`);
      error.status = response.status; throw error;
    }
    return value;
  }
  async function post(path, body, method) {
    const pending = new AbortController(), timeout = setTimeout(() => pending.abort(), 15000);
    try { return await json(path, pending.signal, body, method); } finally { clearTimeout(timeout); }
  }
  function registrySupported() { return localIdentity?.capabilities?.includes('devices.registry-v1') === true; }
  function registryWritable() {
    return selected === 'local' && registrySupported() && deviceRegistry?.node_id === localIdentity?.node_id && !registryError && access?.steer && !accessBusy && HGTarget.canWrite() && !registryBusy && !registryWriting && !writing;
  }
  function registryTimestamp(value) {
    if (!value) return '尚未保存';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? '时间未知' : date.toLocaleString();
  }
  function renderDeviceRegistry() {
    const local = selected === 'local', writable = registryWritable(), supported = registrySupported();
    $('device-registry-heading').textContent = local ? '本节点设备登记' : '所选节点设备登记';
    $('device-registry-local').hidden = !local;
    $('device-registry-refresh').hidden = !local;
    $('device-registry-create').hidden = !local;
    $('device-registry-refresh').disabled = !local || !supported || registryBusy || registryWriting || !!HGTarget.problem();
    $('device-registry-refresh').setAttribute('aria-busy', String(registryBusy));
    $('device-registry-refresh').textContent = registryBusy ? '正在读取登记…' : '读取设备登记';
    $('device-registry-create').disabled = !writable || !!deviceEditor;
    $('device-registry-scope').textContent = local ? '设备登记是维护者声明，不等于物理同机核验、设备在线或并发执行资格。登记原件的 execution_scope 是当时记录；当前执行范围以任务绑定与 lease 协议事实为准。' : '这台电脑的设备登记不经中心读取，只能在它本机查看和编辑。';
    const permission = !access?.steer ? '当前只读；创建和编辑需解锁目录操作。' : '';
    message('device-registry-message', registryBusy ? '正在读取宿主登记；旧记录保留，不查询设备。' : registryError ? `登记状态未确认：${registryError}。${deviceRegistry ? '下方保留上次读取的记录；' : ''}请显式重读，不会自动重试。` : !localIdentity ? '等待本节点身份…' : !supported ? '此节点未声明 devices.registry-v1，设备登记接口不可用。' : registryNotice || (!deviceRegistry ? '尚未读取设备登记。' : permission || '仅保存维护者声明；未执行设备探测。'), !!registryError);
    $('device-registry-meta').textContent = deviceRegistry ? `节点 ${deviceRegistry.node_id} · 登记 revision ${deviceRegistry.revision} · 更新于 ${registryTimestamp(deviceRegistry.updated_at)} · 读取于 ${time(registryReadAt)}。${permission}` : '';
    $('device-registry-empty').hidden = !deviceRegistry || deviceRegistry.devices.length > 0;
    $('device-registry-list').replaceChildren(...(deviceRegistry?.devices || []).map(device => {
      const row = el('li', null, 'device-registry-item'), head = el('div', null, 'section-heading'), title = el('div', null, 'device-head');
      const state = el('span'); badge(state, device.enabled ? '登记启用' : '登记停用', device.enabled ? '' : 'unknown');
      title.append(el('h3', device.label), state);
      const edit = el('button', '编辑登记', 'ghost'); edit.type = 'button'; edit.disabled = !writable || !!deviceEditor;
      edit.setAttribute('aria-label', `编辑设备登记 ${device.device_id}`); edit.addEventListener('click', () => openDeviceEditor(device));
      head.append(title, edit); row.append(head, el('p', `device_id · ${device.device_id}`, 'device-registry-identity'));
      const aliases = el('ul', null, 'device-registry-aliases'); aliases.append(...device.aliases.map(alias => el('li', alias))); row.append(aliases); return row;
    }));
    $('device-registry-form').hidden = !local || !deviceEditor;
    const fieldsDisabled = !writable || !!deviceEditor?.needsRead;
    for (const name of ['label', 'aliases', 'enabled']) $('device-registry-' + name).disabled = fieldsDisabled;
    $('device-registry-id').disabled = fieldsDisabled || !!deviceEditor?.existing;
    $('device-registry-save').disabled = fieldsDisabled || !deviceEditor;
    $('device-registry-save').setAttribute('aria-busy', String(registryWriting));
    $('device-registry-save').textContent = registryWriting ? '正在保存…' : '保存设备登记';
    $('device-registry-close').disabled = registryWriting || registryBusy;
    $('device-registry-reload').hidden = !deviceEditor?.needsRead;
    $('device-registry-reload').disabled = !local || registryBusy || registryWriting || !supported || !!HGTarget.problem();
    $('device-registry').setAttribute('aria-busy', String(registryBusy || registryWriting));
  }
  function acceptDeviceRegistry(value, expectedNode) {
    if (value.schema !== 'phonefarm/device-registry@1' || value.node_id !== expectedNode || !Number.isSafeInteger(value.revision) || value.revision < 0 || !Array.isArray(value.devices)) throw new Error('设备登记协议或节点身份不匹配。');
    deviceRegistry = value; registryReadAt = Date.now() / 1000; registryError = '';
  }
  async function readDeviceRegistry() {
    if (selected !== 'local' || !registrySupported() || registryBusy || registryWriting || disposed) return null;
    const expectedNode = localIdentity.node_id, pending = new AbortController();
    registryController = pending; registryBusy = true; registryAttempted = true; registryNotice = '';
    const timeout = setTimeout(() => pending.abort(), 15000); renderManagement();
    try {
      const value = await json('/dash/api/node/devices', pending.signal);
      acceptDeviceRegistry(value, expectedNode);
      registryNotice = '已读取设备登记；列表读取不会更新已打开草稿的编辑 revision。';
      return value;
    } catch (error) {
      registryError = error.name === 'AbortError' ? '读取已中止或超时' : error.message;
      if (error.auth) await loadAccess();
      return null;
    } finally { clearTimeout(timeout); registryBusy = false; registryController = null; if (!disposed) renderManagement(); }
  }
  function openDeviceEditor(device = null) {
    if (!registryWritable() || deviceEditor) return;
    deviceEditor = {existing: !!device, deviceId: device?.device_id || '', revision: deviceRegistry.revision, target: HGTarget.binding(), needsRead: false};
    fillDeviceEditor(device);
  }
  function fillDeviceEditor(device) {
    $('device-registry-form-heading').textContent = device ? `编辑设备 · ${device.device_id}` : '登记设备';
    $('device-registry-id').value = device?.device_id || '';
    $('device-registry-label').value = device?.label || '';
    $('device-registry-aliases').value = device?.aliases.join('\n') || '';
    $('device-registry-enabled').checked = device?.enabled ?? true;
    $('device-registry-edit-revision').textContent = `编辑 revision：${deviceEditor.revision}。保存会核对整个登记版本；不会覆盖并行修改。`;
    message('device-registry-edit-message', ''); renderManagement();
    $(device ? 'device-registry-label' : 'device-registry-id').focus();
  }
  async function saveDeviceRegistry() {
    if ($('device-registry-save').disabled || !deviceEditor) return;
    const attempt = deviceEditor, issue = HGTarget.problem(attempt.target);
    if (issue) { message('device-registry-edit-message', issue, true); return; }
    const device = {device_id: $('device-registry-id').value, label: $('device-registry-label').value, aliases: $('device-registry-aliases').value.split(/\r?\n/).filter(line => line !== ''), enabled: $('device-registry-enabled').checked};
    if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(device.device_id) || !device.label || /[\u0000-\u001f\u007f]/.test(device.label)) { message('device-registry-edit-message', '请填写有效设备 ID 和不含控制字符的标签。', true); return; }
    if (!device.aliases.length || device.aliases.length > 32 || new Set(device.aliases).size !== device.aliases.length || device.aliases.some(alias => /\s|[\u0000-\u001f\u007f]/.test(alias) || new TextEncoder().encode(alias).length > 256)) { message('device-registry-edit-message', '请每行填写一个原始 serial：1–32 个、不重复、不含空白或控制字符，每项最多 256 字节。不会自动修正别名。', true); return; }
    const body = {expected_revision: attempt.revision, device};
    attempt.deviceId = device.device_id; attempt.submitted = body;
    suspend(); registryWriting = true; renderManagement(); message('device-registry-edit-message', '正在保存设备登记，不会自动重试…');
    try {
      const value = await post('/dash/api/node/devices', body);
      acceptDeviceRegistry(value, attempt.target.node_id);
      registryNotice = `设备登记已保存，revision ${value.revision}；不代表设备已在线、已核验或可并发。`;
      if (deviceEditor === attempt) {
        attempt.revision = value.revision; attempt.existing = true; attempt.needsRead = false;
        $('device-registry-edit-revision').textContent = `已保存 revision：${value.revision}`;
        message('device-registry-edit-message', '登记已保存。启停和别名修改只作用于登记信息，不改变已有任务。');
      }
    } catch (error) {
      if (deviceEditor === attempt) {
        const uncertain = !error.status || error.status >= 500;
        attempt.needsRead = uncertain || error.status === 409;
        if (attempt.needsRead) registryError = error.message || '写入结果未确认';
        message('device-registry-edit-message', `${uncertain ? '保存结果未确认：' : '保存被拒绝：'}${error.name === 'AbortError' ? '请求超时。' : error.message} ${attempt.needsRead ? '草稿及原 expected_revision 已保留；请显式读取当前登记，核对后再保存，不会自动重放。' : '请检查输入或操作权限；没有自动重试。'}`, true);
      }
      if (error.auth) await loadAccess();
    } finally { registryWriting = false; if (!disposed) { renderManagement(); schedule(); } }
  }
  async function reloadDeviceEditor() {
    if ($('device-registry-reload').disabled || !deviceEditor) return;
    const attempt = deviceEditor, value = await readDeviceRegistry();
    if (!value || deviceEditor !== attempt || selected !== 'local') return;
    const saved = value.devices.find(device => device.device_id === attempt.deviceId);
    attempt.revision = value.revision; attempt.needsRead = false; attempt.existing = !!saved;
    if (saved) fillDeviceEditor(saved);
    else { $('device-registry-edit-revision').textContent = `当前 revision：${value.revision}。未找到此 ID；新建草稿保留，请核对后再保存。`; renderManagement(); }
    message('device-registry-edit-message', saved ? '已读取当前登记并替换此设备草稿。请核对内容后显式保存；没有重新发送写入。' : '当前未找到此设备 ID，保留新建输入并更新编辑 revision。请核对后显式保存；没有重新发送写入。');
  }
  function renderDirectory() {
    const local = el('li'), button = el('button', null, 'node-choice');
    button.type = 'button'; button.setAttribute('aria-current', String(selected === 'local'));
    button.append(el('strong', '本节点'), el('span', text(localIdentity?.node_id), 'node-choice-meta'));
    button.addEventListener('click', () => selectNode('local')); local.append(button);
    const rows = [local];
    for (const entry of entries.values()) {
      const row = el('li'), choice = el('button', null, 'node-choice'), head = el('div', null, 'node-choice-head');
      choice.type = 'button'; choice.setAttribute('aria-current', String(selected === entry.name));
      const state = el('span'); badge(state, CONNECTION[entry.connection?.status] || '连接状态未知', entry.connection?.status === 'ready' && !entryStale(entry) ? '' : 'unknown');
      head.append(el('strong', entry.name), state);
      const fleet = entry.snapshot?.fleet, devices = fleet?.devices;
      choice.append(head, el('span', `${text(entry.node_id)}${entry.tags?.length ? ` · ${entry.tags.join(' / ')}` : ''}`, 'node-choice-meta'),
        el('span', Array.isArray(devices) ? `${devices.length} 台手机 · 采样于 ${time(fleet.sampled_at)}${fleet.stale || entryStale(entry) ? ' · 已过期' : ''}` : '还没有手机读数', 'node-choice-meta'),
        el('span', `最近联系：${time(entry.connection?.last_seen_at)}`, 'node-choice-meta'));
      choice.addEventListener('click', () => selectNode(entry.name)); row.append(choice); rows.push(row);
    }
    $('node-directory').replaceChildren(...rows);
  }
  document.addEventListener('hgamey-target-change', () => renderManagement());
  function renderManagement() {
    const entry = remoteEntry(), writable = directoryKnown && access?.steer && HGTarget.canWrite() && !accessBusy && !writing && !registryWriting;
    $('invite-open').disabled = !writable;
    $('invite-submit').disabled = !writable || inviteBusy;
    $('invite-submit').setAttribute('aria-busy', String(inviteBusy));
    $('meta-edit').disabled = !entry || !writable;
    $('remote-actions').hidden = selected === 'local';
    $('remote-devices').disabled = !HGTarget.canWrite() || !entry || entry.connection?.status !== 'ready' || loading || authLost;
    $('access-toggle').disabled = accessBusy || writing || registryWriting || !access?.steer_configured;
    $('access-toggle').textContent = accessBusy ? '读取权限…' : access?.steer ? '锁定目录操作' : '解锁目录操作';
    $('access-toggle').setAttribute('aria-busy', String(accessBusy));
    message('access-message', accessError || (accessBusy ? '正在核对目录操作权限…' : !access ? '权限尚未取得，仅查看目录。' : !access.steer_configured ? '本节点未配置掌舵密码，目录保持只读。' : access.steer ? '已解锁：可生成加入码、改备注和标签。' : '当前只读；生成加入码、改备注和标签需解锁。'), !!accessError);
    for (const id of ['meta-tags', 'meta-note']) $(id).disabled = !writable;
    $('meta-save').disabled = !writable || !editor;
    $('meta-save').setAttribute('aria-busy', String(writing));
    $('meta-save').textContent = writing ? '正在保存…' : '保存';
    $('meta-close').disabled = writing;
    $('refresh-fleet').disabled = loading || runtimeBusy || registryWriting;
    renderRuntimeRefresh();
    renderDeviceRegistry();
  }
  async function loadAccess() {
    accessBusy = true; renderManagement();
    try { access = await json('/dash/api/access'); accessError = ''; }
    catch (error) { access = null; accessError = error.message; }
    finally { accessBusy = false; renderManagement(); }
  }
  function closeUnlock() { $('unlock-form').hidden = true; $('steer-password').value = ''; }
  async function setAccess(unlock) {
    if (accessBusy || writing) return;
    const body = unlock ? {pw: $('steer-password').value} : {};
    $('steer-password').value = ''; accessBusy = true; renderManagement();
    $('unlock-submit').disabled = true; message('unlock-message', '');
    try { await post(`/dash/api/steer/${unlock ? 'unlock' : 'lock'}`, body); closeUnlock(); await loadAccess(); }
    catch (error) { access = null; accessError = error.message; message('unlock-message', error.message, true); }
    finally { accessBusy = false; $('unlock-submit').disabled = false; renderManagement(); }
  }
  function closeEditor() {
    if (writing) return;
    editor = null; $('meta-form').hidden = true;
  }
  function openEditor(entry) {
    if (writing || !access?.steer || !entry) return;
    editor = {name: entry.name, revision: entry.revision};
    $('meta-form').hidden = false; $('meta-heading').textContent = `改备注和标签 · ${entry.name}`;
    $('meta-tags').value = entry.tags?.join(', ') || ''; $('meta-note').value = entry.note || '';
    message('meta-message', ''); renderManagement(); $('meta-note').focus();
  }
  async function saveEditor() {
    if ($('meta-save').disabled) return;
    const attempt = editor, path = `/dash/api/nodes/${encodeURIComponent(attempt.name)}`;
    const body = {revision: attempt.revision, tags: $('meta-tags').value.split(',').map(s => s.trim()).filter(Boolean), note: $('meta-note').value};
    suspend(); writing = true; renderManagement(); message('meta-message', '正在保存…');
    try {
      const entry = await post(path, body);
      entries.set(entry.name, entry);
      if (editor === attempt) { editor = null; $('meta-form').hidden = true; message('remote-action-message', '备注和标签已保存。'); }
      renderDirectory(); if (selected === entry.name) renderSelected();
    } catch (error) {
      if (editor === attempt && error.status === 409) {
        // Changed elsewhere: take the new revision, keep the draft, and let the user save again.
        try {
          const entry = await json(path); entries.set(entry.name, entry); attempt.revision = entry.revision; renderDirectory();
          message('meta-message', `刚被别处改过（现在是：标签 ${entry.tags?.join(', ') || '无'}；备注 ${entry.note || '无'}）。你的修改还在，确认后再保存。`, true);
        } catch (again) { message('meta-message', `${error.message}；重新读取也失败：${again.message}`, true); }
      } else if (editor === attempt) message('meta-message', error.message, true);
      if (error.auth) await loadAccess();
    } finally { writing = false; renderManagement(); schedule(); }
  }
  // Join codes for other computers: shown once, listed until used, revocable.
  const shellWord = value => /^[\w@%+=:,./-]+$/.test(value) ? value : `'${value.replace(/'/g, `'\\''`)}'`;
  async function makeInvite() {
    if ($('invite-submit').disabled) return;
    const name = $('invite-name').value.trim(), root = $('invite-root').value.trim(), port = $('invite-port').value.trim(), hours = Number($('invite-hours').value);
    if (!/^[a-z0-9_-]{1,64}$/.test(name)) { message('invite-message', '节点名只能用小写字母、数字、下划线或连字符。', true); return; }
    if (name === 'local' || entries.has(name)) { message('invite-message', `「${name}」已经在用，换一个名字。`, true); return; }
    if (!(hours > 0)) { message('invite-message', '有效时长要大于 0。', true); return; }
    if (port && !(Number.isInteger(Number(port)) && Number(port) >= 1 && Number(port) <= 65535)) { message('invite-message', '端口要在 1–65535 之间。', true); return; }
    inviteBusy = true; $('invite-result').hidden = true; message('invite-message', ''); renderManagement();
    try {
      const value = await post('/dash/api/node-invites', {hours, note: name});
      $('invite-command').textContent = `python3 deploy.py join --center ${shellWord(location.origin)} --invite ${shellWord(value.code)} --name ${name}${root ? ` --root ${shellWord(root)}` : ''}${port && Number(port) !== 8765 ? ` --port ${Number(port)}` : ''}`;
      $('invite-result').hidden = false;
      message('invite-message', `${time(value.expires_at)} 前有效。`);
      await loadInvites();
    } catch (error) { message('invite-message', error.message, true); if (error.auth) await loadAccess(); }
    finally { inviteBusy = false; renderManagement(); }
  }
  async function loadInvites() {
    if (!access?.steer) return;
    try {
      const {invites} = await json('/dash/api/node-invites');
      $('invite-list').replaceChildren(...invites.map(invite => {
        const row = el('li'), revoke = el('button', '撤销', 'ghost');
        revoke.type = 'button'; revoke.dataset.invite = invite.id;
        revoke.addEventListener('click', async () => {
          revoke.disabled = true;
          try { await post(`/dash/api/node-invites/${encodeURIComponent(invite.id)}`, undefined, 'DELETE'); await loadInvites(); }
          catch (error) { revoke.disabled = false; message('invite-message', `撤销失败：${error.message}`, true); }
        });
        row.append(el('span', `${invite.note || '未命名'} · ${invite.expires_at * 1000 < Date.now() ? '已过期' : `${time(invite.expires_at)} 前有效`}`), revoke); return row;
      }));
      $('invite-none').hidden = invites.length > 0;
    } catch (error) { message('invite-message', `加入码列表读取失败：${error.message}`, true); }
  }
  function openInvite() {
    if ($('invite-open').disabled) return;
    $('invite-form').hidden = false; $('invite-result').hidden = true; $('invite-command').textContent = ''; message('invite-message', '');
    $('invite-name').focus(); loadInvites();
  }
  function closeInvite() { $('invite-form').hidden = true; $('invite-command').textContent = ''; }
  function processPolicyFacts(node) {
    if (!node?.capabilities?.includes('jobs.execution-policy-v1')) return [['进程执行策略能力', '未提供，执行范围未知']];
    return [['进程执行策略协议', 'jobs.execution-policy-v1'], ...[['process_group', '原进程组策略']].map(([key, label]) => {
      const policy = node.execution?.process_policies?.[key];
      const description = !policy ? '未提供' : policy.available === true ? `可选择 · 后端：${text(policy.backend)} · 完成范围：${text(policy.completion_scope)}` : '不可用';
      return [`${label}（${key}）`, `${description}${policy?.reason ? `。${policy.reason}` : ''}`];
    })];
  }
  function hostSlotsText(slots) {
    return ['cpu', 'gpu', 'io'].map(key => `${key.toUpperCase()} ${Number.isSafeInteger(slots?.[key]) && slots[key] >= 0 ? slots[key] : '未知'}`).join(' / ');
  }
  function renderNode(node, entry = null) {
    $('node-summary').textContent = `${entry ? `${entry.name} · ` : '本节点 · '}${text(node?.node_id || entry?.node_id)}${node?.host?.hostname ? ` · ${node.host.hostname}` : ''}`;
    $('node-updated').textContent = entry ? `最近快照：${time(entry.snapshot?.at)}` : `读取于 ${new Date().toLocaleTimeString()}`;
    message('node-message', entry ? `${CONNECTION[entry.connection?.status] || '连接状态未知'}。最近联系：${time(entry.connection?.last_seen_at)}。${entry.connection?.error ? ` 这次请求没成功：${typeof entry.connection.error === 'string' ? entry.connection.error : JSON.stringify(entry.connection.error)}。` : ''}${entryStale(entry) ? ' 以下是最后一次收到的信息。' : ''}` : '', !!entry?.connection?.error);
    $('node-details').hidden = false;
    facts($('node-info'), [...(entry ? [['节点名', entry.name], ['加入时间', time(entry.created_at)], ['标签', entry.tags?.join('、')], ['备注', entry.note]] : [['访问入口', location.origin]]),
      ['节点 ID', node?.node_id || entry?.node_id], ['宿主系统', node?.host?.platform],
      ...serviceFacts(node?.service), ['服务启动', time(node?.started_at)],
      ['任务目录', node?.storage?.jobs_root], ['数据目录', node?.storage?.data_root],
      ['宿主调度容量（身份快照）', hostSlotsText(node?.execution?.host_capacity?.slots)],
      ['槽位含义', '配置声明的并发调度容量，不代表硬件数量、性能或手机存在；设备采样状态单独显示。'],
      ...exportCacheFacts(node), ...processPolicyFacts(node)]);
    renderPrograms(node);
  }
  function hostProcessFacts(entry) {
    if (entry.role !== 'device_lease') return [];
    const host = entry.host_process;
    const status = !host ? '未提供' : ({alive: '同一宿主实例存活', previous_boot: '来自先前宿主启动', exited: '宿主 owner 已退出', zombie: '宿主 owner 已退出（僵尸态）', pid_reused: '宿主 PID 已被复用', legacy: '旧记录未记录宿主实例', unknown: '宿主实例无法确认'})[host.status] || '宿主实例无法确认';
    return [['宿主 owner 实例', status], ...(host ? [['宿主 PID', host.pid], ['宿主进程启动身份', host.start], ['宿主 boot ID', host.host_boot_id], ['宿主身份原因', host.reason]] : []),
      ['身份核查范围', '仅宿主 owner 实例；不证明原进程组已结束或设备已恢复。']];
  }
  function renderLock(lock, historical = false, snapshotAt = null) {
    const lease = lock?.lease, entries = Array.isArray(lease?.entries) ? lease.entries : [];
    const blocked = lease?.status === 'blocked';
    const unknown = lease?.status === 'unknown' || (lock?.status !== 'free' && lock?.status !== 'held');
    const known = !blocked && !unknown;
    const held = lease?.status === 'held' || lock?.status === 'held';
    const state = blocked ? '需要恢复' : unknown ? '身份或状态未知' : held ? (entries.some(e => e.role === 'device_lease') ? '设备租约占用' : entries.some(e => e.role === 'outer_queue') ? '外层队列占用' : '占用中 · 身份未确认') : '锁记录空闲';
    badge($('lock-state'), historical ? `旧记录 · ${state}（当前未知）` : state, historical || blocked || unknown || held ? 'unknown' : '');
    $('lock-owner').textContent = lock?.held_by ? `标签：${lock.held_by}` : held || blocked || unknown ? '未记录展示标签' : '没有占用标签';
    const reason = lease?.error || lock?.error;
    $('lock-message').textContent = (historical ? `上次目录观测：${time(snapshotAt)}；保留旧记录供核查，当前锁与设备可用性未确认。 ` : '') + (blocked ? `恢复屏障保留；持有者退出或心跳过期不代表恢复完成。${reason ? ` ${reason}` : ''}` :
      unknown ? `身份或状态尚未确认，保持阻断。${reason ? ` ${reason}` : ''}` : reason ? `锁记录读取失败：${reason}` : held && lock.stale ?
      '心跳已过期，需核查租约状态；仍按占用显示。' : held ? '占用记录不代表正在测量；请结合租约与任务原件核对。' :
      historical ? '上次锁记录为空，不代表现在空闲。' : '当前锁记录为空；设备可用性还需结合采样与恢复状态。');
    $('lock-message').className = `lock-message ${reason ? 'error' : historical || blocked || unknown || held && lock.stale ? 'warning' : ''}`;
    facts($('lock-info'), known ? [['记录开始', lock.since], [historical ? '观测时心跳年龄' : '心跳距今', duration(lock.heartbeat_age_s)],
      ...(!held ? [[historical ? '观测时空闲时长' : '空闲时长', duration(lock.free_for_s)]] : [])] : []);
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
        details.append(jobLink(entry.job_id));
      }
      item.append(details); return item;
    }));
    $('lock-identities-empty').hidden = entries.length > 0;
    $('lock-identities-empty').textContent = lease?.status === 'clear' ? `${historical ? '上次观测' : ''}没有租约记录。` : '未取得实例记录；不据标签认定持有者。';
    const queue = Array.isArray(lock?.queue) ? lock.queue : [];
    $('lock-queue').replaceChildren(...queue.map(label => el('li', label)));
    $('queue-summary').textContent = known ? `队列与最近事件 · ${queue.length} 条排队记录` : '队列与最近事件';
    $('queue-empty').hidden = queue.length > 0;
    $('queue-empty').textContent = known ? `${historical ? '上次观测' : ''}没有排队记录。` : '尚未取得队列。';
    const events = Array.isArray(lock?.recent) ? lock.recent : [];
    $('lock-events').replaceChildren(...events.map(event => {
      const item = el('li');
      item.append(el('time', event.at), el('span', `${({acquire: '获取', release: '释放', steal: '接管'})[event.action] || text(event.action)} · ${text(event.label)}`));
      return item;
    }));
    $('events-empty').hidden = events.length > 0;
    $('events-empty').textContent = known ? '没有近期记录。' : '尚未取得事件记录。';
  }
  function renderQueue(queue, historical = false) {
    $('node-queue-summary').textContent = historical ? '节点任务队列 · 上次快照' : '节点任务队列';
    message('node-queue-message', !queue ? '尚未取得节点任务队列。' : `${text(queue.active_count)} 项执行中 / ${text(queue.max_running)} 个名额，${text(queue.queued_count)} 项排队，${text(queue.blocked_count)} 项保留资源。宿主调度槽位（本次队列快照）：声明 ${hostSlotsText(queue.host_capacity?.slots)}；已保留 ${hostSlotsText(queue.host_capacity?.reserved)}；可调度 ${hostSlotsText(queue.host_capacity?.available)}。${historical ? ' 当前调度状态未知。' : ''}${queue.dispatcher_error ? ` 调度异常：${queue.dispatcher_error}` : ''}`, !!queue?.dispatcher_error);
    $('node-job-queue').replaceChildren(...(queue?.items || []).map(job => {
      const item = el('li'); item.append(jobLink(job.job_id), el('span', ` · ${text(job.phase)}`));
      if (job.waiting?.kind) item.append(el('p', `等待：${({capacity: '执行名额', resource: '独占资源', recovery: '恢复核查', device_lease: '设备租约', durability: '保存结果', preparing: '准备任务'})[job.waiting.kind] || job.waiting.kind}${job.waiting.reason ? ` · ${job.waiting.reason}` : ''}`, 'note'));
      return item;
    }));
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
    extra.dataset.device = JSON.stringify([selected, device.transport, device.serial]);
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
  function renderFleet(snapshot, failed = false, historical = false, snapshotAt = null) {
    renderLock(snapshot?.lock, failed || historical, snapshotAt);
    const devices = snapshot?.devices;
    const hasSample = Array.isArray(devices);
    const age = snapshot?.age_s == null ? null : snapshot.age_s + Math.floor((Date.now() - receivedAt) / 1000);
    const stale = failed || historical || snapshot?.stale || (age != null && age > (snapshot?.policy?.stale_after_s ?? 90));
    badge($('sample-state'), !hasSample ? '未采集' : stale ? '旧快照' : snapshot.deferred ? '采样暂缓 · 缓存快照' : snapshot.error ? '尝试失败 · 缓存快照' : snapshot.refreshing ? '更新中' : '已有快照',
      !hasSample || stale || snapshot?.error || snapshot?.deferred ? 'unknown' : '');
    $('sample-time').textContent = snapshot?.sampled_at == null ? '尚无成功采样记录' : `最后成功采样：${time(snapshot.sampled_at)} · ${age == null ? '年龄未知' : duration(age) + '前'}`;
    $('sample-attempt').textContent = snapshot?.attempted_at == null ? '尚无采样尝试记录' : `最近已知尝试：${time(snapshot.attempted_at)}${failed ? '（上次返回记录）' : ''}`;
    const notices = [];
    if (failed && hasSample) notices.push('连接失败，以下保留上次取得的设备快照，当前设备状态未确认。');
    if (historical && hasSample) notices.push(`目录观测已陈旧（${time(snapshotAt)}）。重新读取缓存不会更新设备采样时间。`);
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
    } else $('sampling-policy').textContent = '尚未取得此节点的设备采样策略。远端目录缓存刷新不触发设备采样。';
  }
  function renderSelected(failed = false) {
    const entry = remoteEntry();
    if (selected === 'local') {
      receivedAt = localReceivedAt;
      renderNode(localIdentity); renderFleet(latest, failed, false, localReceivedAt ? localReceivedAt / 1000 : null); renderQueue(localQueue, failed || !!localQueueError);
      if (localQueueError) message('node-queue-message', `节点队列读取失败：${localQueueError}。${localQueue ? '下方保留上次取得的记录，当前调度状态未知。' : '尚未取得队列。'}`, true);
    } else if (entry) {
      receivedAt = entry.snapshot?.at ? entry.snapshot.at * 1000 : Date.now();
      renderNode(entry.snapshot?.identity, entry);
      renderFleet(entry.snapshot?.fleet, failed, entryStale(entry), entry.snapshot?.at);
      renderQueue(entry.snapshot?.queue, failed || entryStale(entry));
    } else {
      $('node-summary').textContent = selected;
      $('node-updated').textContent = '';
      message('node-message', '目录中尚未取得此登记，请刷新目录或选择其他节点。', true);
      $('node-details').hidden = true;
      receivedAt = Date.now(); renderFleet(null); renderQueue(null);
    }
    for (const page of ['jobs','games','control']) {
      const link=$(`node-${page}-link`), url=nodePageURL(page);link.hidden=!url;
      if(url){link.href=url;link.target=selected==='local'?'_self':'_blank';link.rel='noopener noreferrer';}
    }
    renderManagement();
    if (selected === 'local' && registrySupported() && !registryAttempted && !disposed) readDeviceRegistry();
  }
  async function refresh(manual = false, devices = false, pull = false) {
    if (loading || writing || runtimeBusy || registryWriting || disposed || document.hidden) return;
    clearTimeout(timer);
    const version = ++revision;
    const requestController = new AbortController();
    controller = requestController;
    const signal = requestController.signal;
    const timeout = setTimeout(() => requestController.abort(), 15000);
    lastStartedAt = Date.now();
    busy(true);
    const name = selected, current = () => version === revision && selected === name && !disposed;
    // `refresh=1` asks every joined computer for its current snapshot; only the refresh button does that.
    const directoryRequest = json(pull ? '/dash/api/nodes?refresh=1' : '/dash/api/nodes', signal).then(result => {
      if (!current()) return;
      if (result.schema_version !== 1 || !Array.isArray(result.items)) throw new Error('目录接口版本不支持。');
      directoryKnown = true; directoryFailed = false; localIdentity = result.local;
      entries = new Map(result.items.map(entry => [entry.name, entry]));
      message('directory-message', entries.size ? `${entries.size} 台电脑已加入。` : '还没有其他电脑加入。');
      renderDirectory();
    }).catch(async error => {
      if (!current()) return;
      directoryFailed = true;
      if (error.status === 404) {
        directoryKnown = false;
        message('directory-message', '此节点尚未提供共享目录，仍可查看本节点设备。');
        const node = await json('/dash/api/node', signal);
        if (current()) { localIdentity = node; renderDirectory(); }
      } else { message('directory-message', `目录读取失败：${error.message}。保留上次观测。`, true); throw error; }
    });
    try {
      if (name === 'local') {
        const [directory, fleet, queue] = await Promise.allSettled([directoryRequest, json('/dash/api/fleet?wait=5', signal), json('/dash/api/queue', signal)]);
        if (!current()) return;
        if (fleet.status === 'fulfilled') {
          if (fleet.value.schema_version !== 1) throw new Error('节点设备接口版本不支持。');
          latest = fleet.value; localReceivedAt = Date.now();
        }
        if (queue.status === 'fulfilled') { localQueue = queue.value; localQueueError = ''; }
        else localQueueError = queue.reason.message;
        if (directory.status === 'rejected') throw directory.reason;
        if (fleet.status === 'rejected') throw fleet.reason;
        if (queue.status === 'rejected' && queue.reason.auth) throw queue.reason;
      } else {
        await directoryRequest;
        if (!current()) return;
        if (devices) {
          const entry = await json(`/dash/api/nodes/${name}?devices=1`, signal);
          if (!current()) return;
          entries.set(name, entry); renderDirectory();
          message('remote-action-message', entry.connection?.error ? `这次没采到：${entry.connection.error}。下面仍是 ${time(entry.snapshot?.fleet?.sampled_at)} 的读数。` : `已重新采样，读数来自 ${time(entry.snapshot?.fleet?.sampled_at)}。`, !!entry.connection?.error);
        }
      }
      if (!current()) return;
      authLost = false;
      $('page-error').hidden = true;
      $('login-link').hidden = true;
      renderSelected();
    } catch (error) {
      if (!current()) return;
      authLost = !!error.auth;
      $('page-error').textContent = error.name === 'AbortError' ? '读取超时，请重试。设备与锁的当前状态尚未确认。' : error.message;
      $('page-error').hidden = false;
      $('login-link').hidden = !authLost;
      renderSelected(true);
    } finally {
      clearTimeout(timeout);
      if (current()) { busy(false); schedule(); }
    }
  }
  function selectNode(name) {
    if (name === selected) return;
    suspend(); selected = name;
    history.replaceState(null, '', `${location.pathname}${location.search}#node=${encodeURIComponent(name)}`);
    closeEditor();
    message('remote-action-message', '');
    $('devices').replaceChildren(); $('lock-leases').replaceChildren();
    renderDirectory(); renderSelected(); refresh(true);
  }
  function suspend() {
    clearTimeout(timer);
    ++revision;
    controller?.abort();
    busy(false);
    refreshNote();
  }
  $('refresh-fleet').addEventListener('click', () => refresh(true, false, true));
  $('refresh-runtime').addEventListener('click', refreshRuntime);
  $('device-registry-refresh').addEventListener('click', readDeviceRegistry);
  $('device-registry-create').addEventListener('click', () => openDeviceEditor());
  $('device-registry-close').addEventListener('click', () => { if (!registryWriting && !registryBusy) { deviceEditor = null; renderManagement(); } });
  $('device-registry-form').addEventListener('submit', event => { event.preventDefault(); saveDeviceRegistry(); });
  $('device-registry-reload').addEventListener('click', reloadDeviceEditor);
  $('remote-devices').addEventListener('click', () => { if (!$('remote-devices').disabled) refresh(true, true); });
  $('meta-edit').addEventListener('click', () => openEditor(remoteEntry()));
  $('meta-close').addEventListener('click', closeEditor);
  $('meta-form').addEventListener('submit', event => { event.preventDefault(); saveEditor(); });
  $('invite-open').addEventListener('click', openInvite);
  $('invite-close').addEventListener('click', closeInvite);
  $('invite-form').addEventListener('submit', event => { event.preventDefault(); makeInvite(); });
  $('invite-copy').addEventListener('click', async () => {
    try { await navigator.clipboard.writeText($('invite-command').textContent); $('invite-copy').textContent = '已复制'; }
    catch (_) { getSelection().selectAllChildren($('invite-command')); $('invite-copy').textContent = '已选中，按 Ctrl+C 复制'; }
    setTimeout(() => { $('invite-copy').textContent = '复制命令'; }, 2000);
  });
  $('access-toggle').addEventListener('click', () => { if (access?.steer) setAccess(false); else { $('unlock-form').hidden = false; $('steer-password').focus(); } });
  $('unlock-form').addEventListener('submit', event => { event.preventDefault(); setAccess(true); });
  $('unlock-close').addEventListener('click', closeUnlock);
  $('auto-refresh').addEventListener('change', schedule);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) suspend();
    else if ($('auto-refresh').checked && !authLost && Date.now() - lastStartedAt >= AUTO_MS) refresh();
    else schedule();
  });
  window.addEventListener('pagehide', () => { disposed = true; registryController?.abort(); closeUnlock(); closeInvite(); suspend(); });
  window.addEventListener('pageshow', event => { if (event.persisted) { disposed = false; schedule(); } });
  renderDirectory(); loadAccess(); refresh(true);
})();
