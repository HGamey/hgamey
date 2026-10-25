/* Directory reads use host caches. Remote device sampling is an explicit action. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const AUTO_MS = 10000;
  let revision = 0, controller = null, timer = null, loading = false, disposed = false;
  let latest = null, receivedAt = 0, lastStartedAt = 0, authLost = false;
  let localIdentity = null, localQueue = null, localQueueError = '', localReceivedAt = 0, directoryKnown = false, directoryFailed = false;
  let selected = new URLSearchParams(location.hash.slice(1)).get('node') || 'local', entries = new Map();
  if (!/^[a-z0-9_-]{1,64}$/.test(selected)) selected = 'local';
  let access = null, accessBusy = false, accessError = '', editor = null, writing = false, toggleDraft = null;
  let runtimeBusy = false, runtimeNotice = '', runtimeError = false;
  let deviceRegistry = null, registryBusy = false, registryWriting = false, registryAttempted = false, registryController = null;
  let registryError = '', registryNotice = '', registryReadAt = null, deviceEditor = null;
  const uncertainUpdates = new Set();
  const CONNECTION = {unverified: '尚未核验', ready: '节点协议可达', unavailable: '连接不可用', unauthorized: '凭据未获授权', incompatible: '节点协议不兼容', identity_mismatch: '节点身份不匹配', disabled: '登记已停用', no_credentials: '未配置凭据'};
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
    const source = local ? null : nodePageURL('jobs', entry);
    $('runtime-source-link').hidden = !source;
    if (source) { $('runtime-source-link').href = source; $('runtime-source-link').target = '_blank'; $('runtime-source-link').rel = 'noopener noreferrer'; }
    message('runtime-refresh-help', !local ? `以下程序身份来自目录快照 ${time(entry?.snapshot?.at)}；${entryStale(entry || {}) ? '目录观测已陈旧，当前入口身份未知。' : '观察状态只适用于该快照。'}本页不刷新远端程序，请到源节点明确操作。` : runtimeBusy ? '正在执行有界宿主身份查询，下方保留上次观察；不操作设备，不自动重试。' : runtimeNotice || (!supported ? '此节点未声明 node.runtime-v1，不能刷新程序观察。' : !access?.steer || accessBusy ? '读取缓存无需解锁；重新执行程序身份观察需先解锁目录操作。' : '仅此按钮执行新的程序身份观察；自动刷新只读取缓存并检查文件变化。'), local && runtimeError);
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
  function entryStale(entry) { return directoryFailed || entry.stale || !entry.enabled || entry.connection?.status !== 'ready' || !entry.snapshot || Date.now() / 1000 - entry.snapshot.at > 30; }
  function nodePageURL(page, entry = remoteEntry(), job = '') {
    let url;
    if (selected === 'local') return HGTarget.url(`/dash/${page}${job ? `#job=${encodeURIComponent(job)}` : ''}`);
    if (!entry?.snapshot?.url || !/^[0-9a-f]{32}$/.test(entry.node_id) || entry.snapshot.identity?.node_id !== entry.node_id) return null;
    try {
      url = new URL(entry.snapshot.url);
      if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) return null;
      url.pathname = url.pathname.replace(/\/$/, '') + `/dash/${page}`; url.search = ''; url.hash = job ? `job=${encodeURIComponent(job)}` : '';
      url.searchParams.set('expected_node_id',entry.node_id); return url.href;
    } catch (_) { return null; }
  }
  const nodeJobsURL = (entry = remoteEntry(), job = '') => nodePageURL('jobs', entry, job);
  function jobLink(id) {
    const url = nodeJobsURL(remoteEntry(), id), link = el(url ? 'a' : 'span', `任务 ${id}`);
    if (url) { link.href = url; if (selected !== 'local') { link.target = '_blank'; link.rel = 'noopener noreferrer'; } }
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
      !$('auto-refresh').checked ? '自动刷新已关闭，可手动读取目录与快照。' : '可见时每 10 秒读取目录并请求到期缓存；远端设备采样需明确点击。';
  }
  function schedule() {
    clearTimeout(timer);
    refreshNote();
    if (disposed || document.hidden || authLost || !$('auto-refresh').checked) return;
    timer = setTimeout(() => refresh(), Math.max(AUTO_MS, lastStartedAt + AUTO_MS - Date.now()));
  }
  async function json(path, signal, body) {
    const response = await HGTarget.fetch(path, {credentials: 'same-origin', cache: 'no-store', signal,
      ...(body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json', 'X-HG': '1'}, body: JSON.stringify(body)})});
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
  async function post(path, body) {
    const pending = new AbortController(), timeout = setTimeout(() => pending.abort(), 15000);
    try { return await json(path, pending.signal, body); } finally { clearTimeout(timeout); }
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
    $('device-registry-scope').textContent = local ? '设备登记是维护者声明，不等于物理同机核验、设备在线或并发执行资格。登记原件的 execution_scope 是当时记录；当前执行范围以任务绑定与 lease 协议事实为准。' : '目录快照不包含此处的权威设备登记。请到源节点读取和编辑；本页不会将远端设备写入当前后台。登记也不等于物理同机核验、设备在线或并发资格。';
    const source = local ? null : nodePageURL('fleet');
    $('device-registry-source').hidden = !source;
    if (source) { $('device-registry-source').href = source; $('device-registry-source').target = '_blank'; $('device-registry-source').rel = 'noopener noreferrer'; }
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
      const devices = entry.snapshot?.fleet?.devices;
      choice.append(head, el('span', `${text(entry.node_id)}${entry.tags?.length ? ` · ${entry.tags.join(' / ')}` : ''}`, 'node-choice-meta'),
        el('span', Array.isArray(devices) ? `上次设备记录：${devices.length} 台${entryStale(entry) ? ' · 目录观测已陈旧' : ''}` : '尚无设备列表，不能据此判定零设备', 'node-choice-meta'),
        el('span', `最近成功：${time(entry.connection?.last_seen_at)}${entry.refreshing ? ' · 目录刷新已排队或执行中' : ''}`, 'node-choice-meta'));
      choice.addEventListener('click', () => selectNode(entry.name)); row.append(choice); rows.push(row);
    }
    $('node-directory').replaceChildren(...rows);
  }
  document.addEventListener('hgamey-target-change', () => renderManagement());
  function renderManagement() {
    const entry = remoteEntry(), writable = directoryKnown && access?.steer && HGTarget.canWrite() && !accessBusy && !writing && !registryWriting;
    $('register-open').disabled = !writable;
    $('registration-edit').disabled = !entry || !writable;
    $('registration-toggle').disabled = !entry || !writable || uncertainUpdates.has(selected);
    $('registration-toggle').textContent = entry?.enabled ? '停用登记' : '启用登记';
    $('remote-actions').hidden = selected === 'local';
    $('remote-devices').disabled = !HGTarget.canWrite() || !entry || !entry.enabled || !entry.credential_present || entry.refreshing || loading || authLost;
    $('remote-devices').setAttribute('aria-busy', String(!!entry?.refreshing));
    $('remote-devices').textContent = entry?.refreshing ? '目录请求处理中…' : '请求远端设备采样';
    $('disable-submit').disabled = !writable || uncertainUpdates.has(selected);
    $('access-toggle').disabled = accessBusy || writing || registryWriting || !access?.steer_configured;
    $('access-toggle').textContent = accessBusy ? '读取权限…' : access?.steer ? '锁定目录操作' : '解锁目录操作';
    $('access-toggle').setAttribute('aria-busy', String(accessBusy));
    message('access-message', accessError || (accessBusy ? '正在核对目录操作权限…' : !access ? '权限尚未取得，仅查看目录。' : !access.steer_configured ? '本节点未配置掌舵密码，目录保持只读。' : access.steer ? '已解锁登记、更新和停用；不改变远端任务。' : '当前只读；目录查询与明确设备采样可用，修改登记需解锁。'), !!accessError);
    for (const id of ['registration-name', 'registration-url', 'registration-tags', 'registration-note', 'registration-token', 'registration-clear-token', 'registration-ssh', 'registration-goals-dir']) $(id).disabled = !writable || !!editor?.needsRead;
    $('registration-name').disabled = !writable || !!editor?.revision || !!editor?.needsRead;
    $('registration-token').disabled ||= $('registration-clear-token').checked;
    $('registration-save').disabled = !writable || !editor || !!editor.needsRead;
    $('registration-save').setAttribute('aria-busy', String(writing));
    $('registration-save').textContent = writing ? '正在保存…' : editor?.revision != null ? '保存更新' : '保存登记';
    $('registration-reload').hidden = !editor?.needsRead;
    $('registration-reload').disabled = writing || loading;
    $('registration-close').disabled = writing;
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
    editor = null; $('registration-token').value = ''; $('registration-form').hidden = true;
  }
  function openEditor(entry = null) {
    if (writing || !access?.steer || !directoryKnown) return;
    editor = {name: entry?.name || '', revision: entry?.revision ?? null};
    $('registration-form').hidden = false;
    $('registration-heading').textContent = entry ? `更新登记 · ${entry.name}` : '登记节点';
    for (const [id, value] of [['name', entry?.name], ['url', entry?.url], ['tags', entry?.tags?.join(', ')], ['note', entry?.note], ['ssh', entry?.ssh], ['goals-dir', entry?.goals_dir]]) $('registration-' + id).value = value || '';
    $('registration-token').value = ''; $('registration-clear-token').checked = false;
    $('credential-clear-field').hidden = !entry;
    $('credential-help').textContent = `${entry ? `当前${entry.credential_present ? '已有' : '没有'}远端凭据。` : ''}空白不发送；更新时保留原凭据。凭据不回显、不存入浏览器存储。`;
    $('registration-revision').textContent = entry ? `编辑版本：${entry.revision}。保存时核对版本，不覆盖并行修改。` : '登记后可刷新目录核验远端身份；保存登记不会主动采样设备。';
    message('registration-message', ''); renderManagement();
    $(entry ? 'registration-url' : 'registration-name').focus();
  }
  async function saveRegistration() {
    if ($('registration-save').disabled) return;
    const name = $('registration-name').value.trim(), attempt = editor;
    if (!/^[a-z0-9_-]{1,64}$/.test(name) || name === 'local') { message('registration-message', '登记名格式不正确或使用了保留名 local。', true); return; }
    const body = {url: $('registration-url').value.trim(), tags: $('registration-tags').value.split(',').map(s => s.trim()).filter(Boolean), note: $('registration-note').value, ssh: $('registration-ssh').value, goals_dir: $('registration-goals-dir').value};
    if (attempt.revision == null) body.name = name; else body.revision = attempt.revision;
    const token = $('registration-token').value.trim(); $('registration-token').value = '';
    if ($('registration-clear-token').checked) body.token = null; else if (token) body.token = token;
    attempt.name = name; suspend(); writing = true; renderManagement(); message('registration-message', '正在保存，写入不会自动重试…');
    try {
      const entry = await post(attempt.revision == null ? '/dash/api/nodes' : `/dash/api/nodes/${name}`, body);
      entries.set(entry.name, entry);
      if (editor === attempt) {
        attempt.revision = entry.revision; $('credential-clear-field').hidden = false; $('registration-clear-token').checked = false;
        message('registration-message', '登记已保存。刷新目录核对节点身份；保存不代表远端可达或设备存在。'); $('registration-revision').textContent = `已保存版本：${entry.revision}`;
        $('credential-help').textContent = `当前${entry.credential_present ? '已有' : '没有'}远端凭据。空白不发送；更新时保留原凭据。凭据不回显、不存入浏览器存储。`;
      }
      renderDirectory(); if (selected === entry.name) renderSelected();
    } catch (error) {
      if (editor === attempt) {
        attempt.needsRead = !error.status || error.status >= 500 || error.status === 409;
        message('registration-message', `${error.message} ${attempt.needsRead ? '请先读取已保存配置；不会自动重试或覆盖。凭据输入已清空。' : '未确认保存，请检查输入或操作权限。凭据输入已清空。'}`, true);
      }
      if (error.auth) await loadAccess();
    } finally { writing = false; renderManagement(); schedule(); }
  }
  async function reloadRegistration() {
    if (!editor?.needsRead || writing || loading) return;
    const attempt = editor; suspend(); writing = true; renderManagement();
    try {
      const entry = await json(`/dash/api/nodes/${attempt.name}`); entries.set(entry.name, entry);
      if (editor === attempt) { writing = false; openEditor(entry); message('registration-message', '已读取已保存配置并替换草稿。请核对内容；没有重新发送写入。'); }
      renderDirectory(); if (selected === entry.name) renderSelected();
    } catch (error) {
      if (editor === attempt) {
        if (error.status === 404 && attempt.revision == null) { attempt.needsRead = false; message('registration-message', '当前未找到此登记。请核对参数后显式保存；凭据需要重新填写。'); }
        else message('registration-message', `读取未完成：${error.message}。没有重新写入。`, true);
      }
    } finally { writing = false; renderManagement(); schedule(); }
  }
  async function toggleRegistration() {
    const attempt = toggleDraft;
    if (!attempt || writing || !access?.steer || uncertainUpdates.has(attempt.name)) return;
    suspend(); writing = true; renderManagement();
    try {
      const entry = await post(`/dash/api/nodes/${attempt.name}`, {revision: attempt.revision, enabled: !attempt.enabled});
      entries.set(entry.name, entry);
      if (selected === entry.name) { $('disable-confirm').hidden = true; message('remote-action-message', entry.enabled ? '登记已启用。可刷新目录核对状态。' : '登记已停用，历史观测保留；远端任务不受影响。'); renderSelected(); }
      renderDirectory();
    } catch (error) {
      if (!error.status || error.status >= 500 || error.status === 409) uncertainUpdates.add(attempt.name);
      if (selected === attempt.name) { $('disable-confirm').hidden = true; message('remote-action-message', `${error.message}。请刷新目录核对结果，不会自动重试。`, true); }
      if (error.auth) await loadAccess();
    } finally { toggleDraft = null; writing = false; renderManagement(); schedule(); }
  }
  function processPolicyFacts(node) {
    if (!node?.capabilities?.includes('jobs.execution-policy-v1')) return [['进程执行策略能力', '未提供，执行范围未知']];
    return [['进程执行策略协议', 'jobs.execution-policy-v1'], ...[['process_group', '原进程组策略'], ['contained', '约束域策略']].map(([key, label]) => {
      const policy = node.execution?.process_policies?.[key];
      const description = !policy ? '未提供' : policy.available === true ? `可选择 · 后端：${text(policy.backend)} · 完成范围：${text(policy.completion_scope)}` : '不可用';
      return [`${label}（${key}）`, `${description}${policy?.reason ? `。${policy.reason}` : ''}`];
    })];
  }
  function renderNode(node, entry = null) {
    $('node-summary').textContent = `${entry ? `${entry.name} · ` : '本节点 · '}${text(node?.node_id || entry?.node_id)}${node?.host?.hostname ? ` · ${node.host.hostname}` : ''}`;
    $('node-updated').textContent = entry ? `目录快照：${time(entry.snapshot?.at)}` : `读取于 ${new Date().toLocaleTimeString()}`;
    message('node-message', entry ? `${CONNECTION[entry.connection?.status] || '连接状态未知'}${entry.refreshing ? ' · 目录刷新已排队或执行中' : ''}。最近尝试：${time(entry.connection?.attempted_at)}；最近成功：${time(entry.connection?.last_seen_at)}。${entry.connection?.error ? ` ${typeof entry.connection.error === 'string' ? entry.connection.error : JSON.stringify(entry.connection.error)}` : ''}${entryStale(entry) ? ' 当前连接或目录观测已陈旧，以下为最后取得的信息。' : ''}` : '', !!entry?.connection?.error);
    $('node-details').hidden = false;
    facts($('node-info'), [...(entry ? [['登记名', entry.name], ['配置版本', entry.revision], ['配置入口', entry.url], ['快照来源', entry.snapshot?.url], ['远端凭据', entry.credential_present ? '已保存（不回显）' : '未配置'], ['标签', entry.tags?.join('、')], ['备注', entry.note], ['SSH 元数据', entry.ssh], ['Goals 目录元数据', entry.goals_dir]] : []),
      ['节点 ID', node?.node_id || entry?.node_id], ['访问入口', entry ? entry.snapshot?.url : location.origin], ['宿主系统', node?.host?.platform],
      ...serviceFacts(node?.service), ['服务启动', time(node?.started_at)],
      ['任务目录', node?.storage?.jobs_root], ['数据目录', node?.storage?.data_root], ...processPolicyFacts(node)]);
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
    message('node-queue-message', !queue ? '尚未取得节点任务队列。' : `${text(queue.active_count)} 项执行中 / ${text(queue.max_running)} 个名额，${text(queue.queued_count)} 项排队，${text(queue.blocked_count)} 项保留资源。${historical ? ' 当前调度状态未知。' : ''}${queue.dispatcher_error ? ` 调度异常：${queue.dispatcher_error}` : ''}`, !!queue?.dispatcher_error);
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
  async function refresh(manual = false, devices = false) {
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
    const directoryRequest = json(devices ? '/dash/api/nodes' : '/dash/api/nodes?refresh=1', signal).then(result => {
      if (!current()) return;
      if (result.schema_version !== 1 || !Array.isArray(result.items)) throw new Error('目录接口版本不支持。');
      directoryKnown = true; directoryFailed = false; localIdentity = result.local;
      entries = new Map(result.items.map(entry => [entry.name, entry]));
      for (const name of entries.keys()) uncertainUpdates.delete(name);
      message('directory-message', `${entries.size} 个远端登记；连接状态与设备观测分开记录。`);
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
          message('remote-action-message', entry.refreshing ? '远端设备采样请求已排队或处理中。当前仍显示已有快照，请核对后续采样时间。' : '已读取目录回执，请核对采样时间与诊断；请求不代表已完成设备采样。');
        }
      }
      if (!current()) return;
      authLost = false;
      $('page-error').hidden = true;
      $('login-link').hidden = true;
      renderSelected();
    } catch (error) {
      if (!current()) return;
      if (devices && error.status === 409) {
        message('remote-action-message', `已有目录刷新进行中，请在完成后显式重试设备采样。${error.message}。不会自动重放。`, true);
        renderSelected(); return;
      }
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
    closeEditor(); toggleDraft = null; $('disable-confirm').hidden = true;
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
  $('refresh-fleet').addEventListener('click', () => refresh(true));
  $('refresh-runtime').addEventListener('click', refreshRuntime);
  $('device-registry-refresh').addEventListener('click', readDeviceRegistry);
  $('device-registry-create').addEventListener('click', () => openDeviceEditor());
  $('device-registry-close').addEventListener('click', () => { if (!registryWriting && !registryBusy) { deviceEditor = null; renderManagement(); } });
  $('device-registry-form').addEventListener('submit', event => { event.preventDefault(); saveDeviceRegistry(); });
  $('device-registry-reload').addEventListener('click', reloadDeviceEditor);
  $('remote-devices').addEventListener('click', () => { if (!$('remote-devices').disabled) refresh(true, true); });
  $('register-open').addEventListener('click', () => openEditor());
  $('registration-edit').addEventListener('click', () => openEditor(remoteEntry()));
  $('registration-close').addEventListener('click', closeEditor);
  $('registration-form').addEventListener('submit', event => { event.preventDefault(); saveRegistration(); });
  $('registration-clear-token').addEventListener('change', () => { if ($('registration-clear-token').checked) $('registration-token').value = ''; renderManagement(); });
  $('registration-reload').addEventListener('click', reloadRegistration);
  $('registration-toggle').addEventListener('click', () => {
    if ($('registration-toggle').disabled) return;
    toggleDraft = {...remoteEntry()};
    if (toggleDraft.enabled) $('disable-confirm').hidden = false; else toggleRegistration();
  });
  $('disable-submit').addEventListener('click', toggleRegistration);
  $('disable-close').addEventListener('click', () => { toggleDraft = null; $('disable-confirm').hidden = true; });
  $('access-toggle').addEventListener('click', () => { if (access?.steer) setAccess(false); else { $('unlock-form').hidden = false; $('steer-password').focus(); } });
  $('unlock-form').addEventListener('submit', event => { event.preventDefault(); setAccess(true); });
  $('unlock-close').addEventListener('click', closeUnlock);
  $('auto-refresh').addEventListener('change', schedule);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) suspend();
    else if ($('auto-refresh').checked && !authLost && Date.now() - lastStartedAt >= AUTO_MS) refresh();
    else schedule();
  });
  window.addEventListener('pagehide', () => { disposed = true; registryController?.abort(); closeUnlock(); $('registration-token').value = ''; suspend(); });
  window.addEventListener('pageshow', event => { if (event.persisted) { disposed = false; schedule(); } });
  renderDirectory(); loadAccess(); refresh(true);
})();
