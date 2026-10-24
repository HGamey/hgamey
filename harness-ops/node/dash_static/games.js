/* Version directory; host preparation and explicit device deployment retain separate evidence. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id), API = '/dash/api';
  const PENDING_KEY = 'hgamey.game-preparation.pending', REGISTRATION_KEY = 'hgamey.game-registration.pending';
  const ID = /^[a-z0-9][a-z0-9._-]{0,63}$/;
  const STAGES = {missing: '缺失', waiting: '等待资源锁', preparing: '准备中', incomplete: '资源未齐', downloading: '下载中', importing: '导入中', verifying: '校验中', verified: '已校验', unavailable: '缺少可用来源', failed: '失败', cancelled: '已取消', unknown: '未知'};
  const PHASES = {preparing: '任务准备中', queued: '已排队', starting: '正在启动', running: '执行中', cancelling: '停止请求处理中', finalizing: '正在保存结果', storage_blocked: '结果保存失败', interrupted: '执行中断', finished: '命令已结束'};
  let records = new Map(), selected = '', current = null, detailFresh = false, directoryReady = false;
  let access = null, accessBusy = false, accessError = '', listBusy = false, listRevision = 0, detailRevision = 0, detailController = null;
  let registrationBusy = false, registrationPending = null, lookupBusy = false, disposed = false;
  let nodeInfo = null;
  const preparing = new Set(), pending = new Map(), receipts = new Map();
  const uploads = window.createGameUploads({
    context: () => ({node: nodeInfo, canWrite: directoryReady && access?.steer && !accessBusy, detailFresh, selected, disposed}),
    refresh: binding => current && selected === `${binding.game_id}/${binding.version}` ? selectVersion(current, true) : Promise.resolve(),
    auth: loadAccess
  });
  const deployments = window.createGameDeployments({
    context: () => ({node: nodeInfo, record: current, canWrite: directoryReady && access?.steer && !accessBusy, detailFresh, selected, disposed}),
    refresh: binding => current && selected === `${binding.game_id}/${binding.version}` ? selectVersion(current, true) : Promise.resolve(),
    auth: loadAccess
  });
  const text = value => value == null || value === '' ? '未提供' : String(value);
  const validID = value => typeof value === 'string' && ID.test(value);
  const time = value => typeof value === 'number' ? new Date(value * 1000).toLocaleString() : '未记录';
  const keyOf = record => `${record.game_id}/${record.version}`;
  const versionPath = record => `${API}/games/${encodeURIComponent(record.game_id)}/versions/${encodeURIComponent(record.version)}`;
  function el(tag, value, className) { const node = document.createElement(tag); if (value != null) node.textContent = String(value); if (className) node.className = className; return node; }
  function message(id, value, error = false) { $(id).textContent = value; $(id).classList.toggle('task-error', error); }
  function facts(node, rows) { node.replaceChildren(...rows.flatMap(([label, value]) => [el('dt', label), el('dd', text(value))])); }
  function badge(node, value, kind = '') { node.textContent = value; node.className = `task-state ${kind}`; }
  function bytes(value) { if (typeof value !== 'number' || !Number.isFinite(value)) return '未知'; if (value < 1024) return `${value} B`; if (value < 1048576) return `${(value / 1024).toFixed(1)} KiB`; return `${(value / 1048576).toFixed(1)} MiB`; }
  function resourceState(state) { return ({verified: '本节点资源已校验', incomplete: '资源未齐', unknown: '资源状态未知'})[state] || '资源状态未知'; }
  function jobState(job) {
    if (job.phase === 'storage_blocked' || job.queue?.state_reason?.kind === 'durability') return ['结果保存失败', 'unknown'];
    if (job.phase === 'finalizing') return ['正在保存结果', 'unknown'];
    if (job.lost || job.phase === 'interrupted' || job.outcome === 'interrupted') return ['执行中断 · 需核查', 'unknown'];
    if (job.queue?.state === 'blocked') return ['资源保留 · 需核查', 'unknown'];
    if (job.state === 'unknown') return ['任务状态未知 · 需核查', 'unknown'];
    if (job.state !== 'finished') return [PHASES[job.phase] || '执行状态未知', 'running'];
    return [({succeeded: '命令已完成', failed: '任务失败', cancelled: '任务已取消'})[job.outcome] || '命令已结束', job.outcome === 'failed' ? 'failed' : ''];
  }
  function safeLink(value, fallback = '') {
    if (!value) return fallback;
    try { const url = new URL(value, location.origin); return url.origin === location.origin && !url.username && !url.password ? url.pathname + url.search + url.hash : fallback; }
    catch (_) { return fallback; }
  }
  function jobLink(job) { return safeLink(job.job_url, `/dash/jobs#job=${encodeURIComponent(job.id)}`); }
  function savePending() {
    try { sessionStorage.setItem(PENDING_KEY, JSON.stringify([...pending.values()].map(({game_id, version, request_id, conflict}) => ({game_id, version, request_id, conflict: !!conflict})))); }
    catch (_) { /* Request IDs remain visible for manual lookup. */ }
  }
  function saveRegistrationPending() {
    try { if (registrationPending) sessionStorage.setItem(REGISTRATION_KEY, JSON.stringify(registrationPending)); else sessionStorage.removeItem(REGISTRATION_KEY); }
    catch (_) { /* The game/version pair remains visible in the form receipt. */ }
  }
  function readSaved() {
    try {
      const saved = JSON.parse(sessionStorage.getItem(PENDING_KEY) || '[]');
      for (const item of Array.isArray(saved) ? saved : []) if (validID(item.game_id) && validID(item.version) && typeof item.request_id === 'string' && item.request_id.length > 0 && item.request_id.length <= 128) pending.set(keyOf(item), item);
      const registration = JSON.parse(sessionStorage.getItem(REGISTRATION_KEY) || 'null');
      if (registration && validID(registration.game_id) && validID(registration.version)) registrationPending = registration;
    } catch (_) { /* Invalid browser data does not establish server state. */ }
  }
  async function request(path, {body, requestID, signal} = {}) {
    const response = await fetch(path, {credentials: 'same-origin', cache: 'no-store', signal, ...(body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json', 'X-HG': '1', ...(requestID ? {'X-Request-ID': requestID} : {})}, body: JSON.stringify(body)})});
    if (response.status === 401 || response.status === 403 || (response.redirected && new URL(response.url).pathname === '/dash/login')) {
      const error = new Error(response.status === 403 ? '没有此操作的权限，请核对掌舵权限。' : '登录已失效，请重新登录。'); error.status = response.status; error.auth = true; throw error;
    }
    if (!(response.headers.get('content-type') || '').includes('application/json')) { const error = new Error('节点未返回可核对的 JSON。'); if (!response.ok) error.status = response.status; throw error; }
    const value = await response.json();
    if (!response.ok) { const error = new Error(typeof value.error === 'string' ? value.error : value.error?.message || `请求失败（HTTP ${response.status}）`); error.status = response.status; throw error; }
    return value;
  }
  async function post(path, body, requestID) {
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 15000);
    try { return await request(path, {body, requestID, signal: controller.signal}); } finally { clearTimeout(timeout); }
  }
  function renderAccess() {
    $('access-toggle').disabled = accessBusy || !access?.steer_configured;
    $('access-toggle').textContent = accessBusy ? '读取权限…' : access?.steer ? '锁定操作' : '解锁操作';
    $('access-toggle').setAttribute('aria-busy', String(accessBusy));
    message('access-message', accessError || (accessBusy ? '正在核对操作权限…' : !access ? '权限未知，保持只读。' : !access.steer_configured ? '本节点未配置掌舵密码，保持只读。' : access.steer ? '可登记、准备资源；设备操作需在准备区显式提交新装或资源部署任务。' : '可查看清单、缓存与原任务；登记、准备、新装和部署需解锁。'), !!accessError);
    const allowed = directoryReady && access?.steer && !accessBusy;
    $('register-open').disabled = !allowed || registrationBusy;
    $('register-submit').disabled = !allowed || registrationBusy || !!registrationPending;
    $('register-submit').textContent = registrationBusy ? '正在核对…' : '保存清单';
    $('register-submit').setAttribute('aria-busy', String(registrationBusy));
    $('manifest-input').disabled = registrationBusy || !!registrationPending;
    $('manifest-preview').disabled = registrationBusy || !!registrationPending;
    $('register-query').hidden = !registrationPending;
    $('register-query').disabled = registrationBusy;
    $('registration-close').disabled = registrationBusy;
    renderPrepare();
  }
  async function loadAccess() {
    accessBusy = true; renderAccess();
    try { access = await request(`${API}/access`); accessError = ''; $('login-link').hidden = true; }
    catch (error) { access = null; accessError = error.message; $('login-link').hidden = !error.auth; }
    finally { accessBusy = false; renderAccess(); }
  }
  function closeUnlock() { $('unlock-form').hidden = true; $('steer-password').value = ''; }
  async function setAccess(unlock) {
    if (accessBusy) return;
    const body = unlock ? {pw: $('steer-password').value} : {}; $('steer-password').value = ''; accessBusy = true; renderAccess(); $('unlock-submit').disabled = true;
    try { await post(`${API}/steer/${unlock ? 'unlock' : 'lock'}`, body); closeUnlock(); await loadAccess(); }
    catch (error) { access = null; accessError = error.message; message('unlock-message', error.message, true); }
    finally { accessBusy = false; $('unlock-submit').disabled = false; renderAccess(); }
  }
  async function loadNode() {
    try { const node = await request(`${API}/node`); nodeInfo = node; $('node-summary').textContent = `${text(node.node_id)} · ${text(node.host?.hostname)}`; message('node-message', '资源留在此节点的数据目录；本页不自动分发到其他节点。'); }
    catch (error) { nodeInfo = null; message('node-message', `节点身份读取失败：${error.message}`, true); }
    finally { uploads.update(); deployments.update(); }
  }
  function renderList() {
    const query = $('game-search').value.trim().toLowerCase();
    const values = [...records.values()].filter(record => `${record.title} ${record.game_id} ${record.version}`.toLowerCase().includes(query));
    $('game-count').textContent = `${records.size} 个版本`;
    $('game-list').replaceChildren(...values.map(record => {
      const item = el('li'), button = el('button', null, 'game-item'), state = el('span'); button.type = 'button'; button.setAttribute('aria-current', String(keyOf(record) === selected));
      badge(state, resourceState(record.resource_state), record.resource_state === 'verified' ? '' : 'unknown');
      button.append(el('strong', record.title || record.game_id), el('span', `${record.game_id} · ${record.version}`, 'game-item-meta'), state);
      button.addEventListener('click', () => { selectVersion(record); $('detail-heading').focus(); }); item.append(button); return item;
    }));
    if (!listBusy && directoryReady) message('list-message', records.size === 0 ? '尚未登记游戏版本。解锁后可登记已有清单。' : values.length === 0 ? '没有匹配的版本。' : '选择版本查看本节点资源观测。');
  }
  async function loadGames() {
    const revision = ++listRevision; listBusy = true; $('refresh-all').disabled = true; $('refresh-all').setAttribute('aria-busy', 'true'); message('list-message', '正在读取版本目录…');
    try {
      const result = await request(`${API}/games`); if (disposed || revision !== listRevision) return;
      if (result.schema_version !== 1 || !Array.isArray(result.items)) throw new Error('游戏目录接口版本不支持。');
      records = new Map(result.items.map(record => [keyOf(record), record])); directoryReady = true;
    } catch (error) { if (!disposed && revision === listRevision) { directoryReady = false; message('list-message', error.status === 404 ? '此节点尚未提供游戏资源目录；没有执行登记或准备。' : `目录读取失败：${error.message}。保留上次列表，当前状态待核对。`, true); } }
    finally { if (!disposed && revision === listRevision) { listBusy = false; $('refresh-all').disabled = false; $('refresh-all').setAttribute('aria-busy', 'false'); renderList(); renderAccess(); } }
  }
  function parseManifest() {
    const manifest = JSON.parse($('manifest-input').value);
    if (!manifest || typeof manifest !== 'object' || Array.isArray(manifest) || !validID(manifest.game_id) || !validID(manifest.version) || typeof manifest.title !== 'string' || !manifest.title.trim() || !Array.isArray(manifest.assets)) throw new Error('请填写有效 game_id、version、title 和 assets 数组。ID 首位须为小写字母或数字，最多 64 字符。');
    if (new TextEncoder().encode(JSON.stringify(manifest)).length > 65536) throw new Error('清单超过 64 KiB。');
    return manifest;
  }
  function previewManifest() {
    try { const value = parseManifest(); message('manifest-summary', `${value.title} · ${value.game_id}/${value.version} · ${value.assets.length} 项资源。这里只检查 JSON 概要，资源字段和来源由服务端核验。`); }
    catch (error) { message('manifest-summary', error instanceof SyntaxError ? 'JSON 格式不正确，请修正后再登记。' : error.message, true); }
  }
  async function registerManifest() {
    if ($('register-submit').disabled) return;
    let manifest; try { manifest = parseManifest(); } catch (error) { message('registration-message', error.message, true); return; }
    const selectionVersion = detailRevision;
    registrationBusy = true; renderAccess(); message('registration-message', '正在登记，写入不会自动重试…');
    try {
      const record = await post(`${API}/games`, manifest);
      if (record.game_id !== manifest.game_id || record.version !== manifest.version) throw new Error('登记回包未确认提交的版本。');
      records.set(keyOf(record), record); renderList(); message('registration-message', '清单已确认登记；相同内容复用原记录。资源尚需单独提交准备任务。');
      if (!disposed && detailRevision === selectionVersion) await selectVersion(record);
    } catch (error) {
      if (!error.status || error.status >= 500 || error.status === 409) {
        registrationPending = {game_id: manifest.game_id, version: manifest.version}; saveRegistrationPending();
        message('registration-message', error.status === 409 ? '同名版本内容冲突，不能覆盖。先读取原清单核对；修改内容请使用新 version。' : `${error.message} 登记结果未知，请读取 ${manifest.game_id}/${manifest.version}；不会自动重发。`, true);
      } else message('registration-message', `登记未确认：${error.message}`, true);
      if (error.auth) await loadAccess();
    } finally { registrationBusy = false; renderAccess(); }
  }
  async function queryRegistration() {
    if (!registrationPending || registrationBusy) return;
    const expected = registrationPending, selectionVersion = detailRevision; registrationBusy = true; renderAccess();
    try {
      const record = await request(versionPath(expected));
      if (record.game_id !== expected.game_id || record.version !== expected.version) throw new Error('回包版本不匹配。');
      registrationPending = null; saveRegistrationPending(); records.set(keyOf(record), record); renderList();
      message('registration-message', '找到已登记的同名版本。请核对下方完整清单；没有重新写入，不能仅凭同名断定内容相同。修改请使用新 version。');
      if (!disposed && detailRevision === selectionVersion) await selectVersion(record);
    } catch (error) {
      if (error.status === 404) { registrationPending = null; saveRegistrationPending(); message('registration-message', '当前未找到此版本。核对清单后可显式登记，没有自动重发。'); }
      else message('registration-message', `查询失败：${error.message}。保留版本身份，稍后再查。`, true);
    } finally { registrationBusy = false; renderAccess(); }
  }
  function orderedPreparations(record) { return [...(record.preparations || [])].sort((a, b) => (b.queue?.submitted_at || b.started || 0) - (a.queue?.submitted_at || a.started || 0)); }
  function renderAssets(record, preparations) {
    const assets = Array.isArray(record.assets) ? record.assets : [];
    $('asset-count').textContent = `${assets.length} 项清单资源`;
    $('asset-list').replaceChildren(...assets.map(asset => {
      const item = el('li', null, 'game-asset'), heading = el('div', null, 'asset-heading'), state = el('span'), observation = asset.observation || {};
      badge(state, `当前缓存：${STAGES[observation.state] || '未知'}`, observation.state === 'verified' ? '' : observation.state === 'failed' ? 'failed' : 'unknown');
      heading.append(el('h4', `${asset.name} · ${text(asset.role)}`), state); item.append(heading);
      item.append(el('p', `期望大小：${bytes(asset.size)} · 最近校验：${time(observation.verified_at)}`, 'asset-stage'));
      if (observation.error) item.append(el('p', text(observation.error), 'asset-error'));
      const preparation = preparations[0], progress = asset.progress;
      if (progress) {
        const active = preparation?.state === 'running' && ['starting', 'running', 'cancelling'].includes(preparation.phase);
        item.append(el('p', `最近任务进度${active ? '（任务执行中）' : '（历史记录）'}：${STAGES[progress.state] || text(progress.state)} · ${bytes(progress.bytes)} / ${bytes(progress.total)} · 报告时间：${time(progress.updated_at || preparation?.report?.updated_at)} · 任务 ${text(preparation?.id)}`, 'asset-stage'));
        if (active && ['downloading', 'importing', 'verifying'].includes(progress.state) && typeof progress.bytes === 'number' && typeof progress.total === 'number' && progress.total > 0) {
          const meter = el('progress', null, 'asset-progress'); meter.max = progress.total; meter.value = Math.max(0, Math.min(progress.total, progress.bytes)); meter.setAttribute('aria-label', `${asset.name} 最近准备记录字节进度`); item.append(meter);
        }
        if (progress.error) item.append(el('p', text(progress.error), 'asset-error'));
      }
      const details = el('details', null, 'asset-details'), info = el('dl', null, 'facts'), source = asset.source || {};
      details.append(el('summary', '来源、目标与校验值'));
      facts(info, [['来源类型', ({url: '公开 HTTP(S) URL', artifact: '本节点任务产物', unavailable: '来源不可用'})[source.kind]], ['来源', source.url || (source.kind === 'artifact' ? `任务 ${source.job_id}\n${source.path}` : source.reason)], ['逻辑目标', asset.target || '未指定'], ['SHA-256', asset.sha256], ['当前记录字节', `${bytes(observation.bytes)} / ${bytes(observation.total)}`]]);
      details.append(info); item.append(details, uploads.mount(record, asset)); return item;
    }));
    if (!assets.length) $('asset-list').append(el('li', '清单没有资源项；不能据此判断设备状态。', 'note'));
  }
  function renderPreparations(values) {
    $('preparation-count').textContent = `${values.length} 条任务记录`;
    $('preparations-empty').textContent = values.length ? '' : '此版本尚无准备任务。登记清单不会自动开始下载。';
    $('preparation-list').replaceChildren(...values.map(job => {
      const item = el('li', null, 'preparation'), heading = el('div', null, 'preparation-heading'), state = el('span'), [label, kind] = jobState(job);
      badge(state, label, kind); heading.append(el('strong', job.id), state); item.append(heading);
      item.append(el('p', `请求：${text(job.request_id)} · 提交：${time(job.queue?.submitted_at || job.started)}${job.exit != null ? ` · 退出码（诊断）：${job.exit}` : ''}`, 'game-item-meta'));
      item.append(el('p', job.report ? `准备报告：${STAGES[job.report.state] || text(job.report.state)} · ${time(job.report.updated_at)}` : '尚无准备报告；可能尚未产生或未能读取，请查看原任务。', 'note'));
      if (job.error || job.report_error) item.append(el('p', text(job.error || job.report_error), 'asset-error'));
      if (job.queue?.state_reason?.kind === 'durability') item.append(el('p', `结果保存：${text(job.queue.state_reason.message)}`, 'asset-error'));
      const links = el('div', null, 'preparation-links'), task = el('a', '查看任务、日志与输出'); task.href = jobLink(job); links.append(task);
      const reportURL = safeLink(job.report_url); if (reportURL && (job.report || job.report_error)) { const report = el('a', '准备报告原件'); report.href = reportURL; links.append(report); }
      item.append(links); return item;
    }));
  }
  function renderRecord(record) {
    $('detail-content').hidden = false; $('game-title').textContent = record.title || record.game_id;
    badge($('resource-state'), resourceState(record.resource_state), record.resource_state === 'verified' ? '' : 'unknown');
    facts($('game-info'), [['游戏 ID', record.game_id], ['不可变版本', record.version], ['节点 ID', record.node_id], ['登记时间', time(record.created_at)]]);
    $('game-manifest').textContent = JSON.stringify(record.manifest, null, 2);
    const preparations = orderedPreparations(record), attempt = pending.get(keyOf(record));
    if (attempt) {
      const known = preparations.find(job => job.request_id === attempt.request_id);
      if (known) { pending.delete(keyOf(record)); savePending(); receipts.set(keyOf(record), {job: known, message: '已从本版本持久记录找回原准备任务；资源状态仍以当前校验观测为准。'}); }
    }
    renderAssets(record, preparations); renderPreparations(preparations); renderPrepare();
  }
  async function selectVersion(identity, refresh = false) {
    if (selected && selected !== keyOf(identity)) uploads.pause('已切换版本，暂停发送后续块；原上传 ID 保留，返回后先查询原状态。');
    const key = keyOf(identity), version = ++detailRevision; detailController?.abort(); detailController = new AbortController();
    selected = key; detailFresh = false; if (!refresh || !current || keyOf(current) !== key) current = records.get(key) || null;
    history.replaceState(null, '', `${location.pathname}${location.search}#game=${encodeURIComponent(identity.game_id)}&version=${encodeURIComponent(identity.version)}`);
    $('refresh-detail').disabled = true; $('refresh-detail').setAttribute('aria-busy', 'true'); message('detail-message', '正在读取版本与当前资源观测…');
    if (current) renderRecord(current); else $('detail-content').hidden = true;
    renderList(); renderPrepare();
    try {
      const record = await request(versionPath(identity), {signal: detailController.signal});
      if (disposed || version !== detailRevision || selected !== key) return;
      if (keyOf(record) !== key) throw new Error('版本回包不匹配。');
      current = record; detailFresh = true; records.set(key, record); renderRecord(record); renderList();
      $('detail-updated').textContent = `读取于 ${new Date().toLocaleString()}`; message('detail-message', '');
    } catch (error) { if (!disposed && version === detailRevision) { message('detail-message', `读取失败：${error.message}。${current ? '下方保留上次记录，当前状态尚未确认。' : ''}`, true); if (current) badge($('resource-state'), '旧观测 · 当前未知', 'unknown'); } }
    finally { if (!disposed && version === detailRevision) { $('refresh-detail').disabled = false; $('refresh-detail').setAttribute('aria-busy', 'false'); renderPrepare(); } }
  }
  function renderPrepare() {
    uploads.update(); deployments.update();
    const attempt = pending.get(selected), receipt = receipts.get(selected), busy = preparing.has(selected), jobs = current?.preparations || [];
    const active = jobs.some(job => job.state !== 'finished'), blocked = jobs.some(job => job.queue?.state === 'blocked');
    const allowed = directoryReady && access?.steer && !accessBusy;
    $('prepare-submit').disabled = !current || !detailFresh || !allowed || busy || !!attempt || active || blocked || current.resource_state === 'verified' || !globalThis.crypto?.randomUUID;
    $('prepare-submit').textContent = busy ? '正在提交…' : jobs.length ? '新建准备尝试' : '提交准备任务';
    $('prepare-submit').setAttribute('aria-busy', String(busy));
    $('prepare-retry').hidden = !attempt || attempt.conflict;
    $('prepare-retry').disabled = !attempt || !allowed || busy;
    $('prepare-query').hidden = !attempt; $('prepare-query').disabled = busy || lookupBusy;
    $('prepare-request').textContent = attempt ? `未确认请求 ID：${attempt.request_id} · ${attempt.game_id}/${attempt.version}` : receipt?.job?.request_id ? `准备请求 ID：${receipt.job.request_id}` : '';
    $('prepare-job').hidden = !receipt?.job; if (receipt?.job) $('prepare-job').href = jobLink(receipt.job);
    message('prepare-message', receipt?.message || '', !!receipt?.error);
    message('prepare-help', attempt ? '保留原请求 ID 与版本。先查询；重试只发送同一 ID 的空请求体，不创建新 ID。' : !current || !detailFresh ? '先成功读取当前版本与资源状态。' : !directoryReady ? '目录接口尚未确认可用。' : !access?.steer || accessBusy ? '请先解锁操作权限。' : active ? '已有未结束的准备任务，请刷新或打开同一任务查看进度。' : blocked ? '旧准备任务仍保留资源，请先打开该任务核查。' : current.resource_state === 'verified' ? '本节点资源已有校验记录。设备状态仍未知；刷新版本可核对缓存身份是否改变。' : !globalThis.crypto?.randomUUID ? '请通过 HTTPS 或本机地址提交。' : '创建持久宿主任务，执行下载、导入与校验；不安装、不操作设备。失败或取消后新尝试使用新请求 ID，沿用内容缓存。');
  }
  async function prepare(retry = false) {
    if ($(retry ? 'prepare-retry' : 'prepare-submit').disabled) return;
    const key = selected, identity = retry ? pending.get(key) : {game_id: current.game_id, version: current.version, request_id: crypto.randomUUID()};
    pending.set(key, identity); savePending(); preparing.add(key); receipts.set(key, {message: '正在提交原请求，节点可能已记录；不会自动重试。'}); renderPrepare();
    try {
      const job = await post(versionPath(identity) + '/prepare', {}, identity.request_id);
      if (!job?.id || job.request_id !== identity.request_id || job.bin !== 'bundle_prepare') throw new Error('回包未确认此资源准备请求。');
      pending.delete(key); savePending(); receipts.set(key, {job, message: `准备任务已记录：${job.id} · ${jobState(job)[0]}。接受任务不代表资源已校验。`});
      if (!disposed && selected === key) await selectVersion(identity, true);
    } catch (error) {
      if (error.status >= 400 && error.status < 500 && error.status !== 409) { pending.delete(key); savePending(); }
      else if (error.status === 409) { identity.conflict = true; savePending(); }
      receipts.set(key, {message: error.status === 409 ? `请求 ID 与内容冲突：${error.message}。保留原 ID，先查询原任务；不会覆盖或自动换 ID。` : pending.has(key) ? `${error.message} 提交结果不确定，已保留原请求 ID；先查询，不自动重发。` : `准备请求未确认：${error.message}`, error: true});
      if (error.auth) await loadAccess();
    } finally { preparing.delete(key); if (selected === key) renderPrepare(); }
  }
  async function lookup(requestID, expectedKey = null) {
    if (lookupBusy || !requestID.trim()) return;
    lookupBusy = true; $('request-lookup').disabled = true; $('request-lookup').setAttribute('aria-busy', 'true'); $('lookup-job').hidden = true; message('lookup-message', '正在查询原请求…'); renderPrepare();
    try {
      const job = await request(`${API}/requests/${encodeURIComponent(requestID.trim())}`);
      if (!job?.id || job.request_id !== requestID.trim()) throw new Error('回包未匹配请求 ID。');
      $('lookup-job').href = jobLink(job); $('lookup-job').hidden = false;
      if (job.bin !== 'bundle_prepare') {
        if (expectedKey && pending.has(expectedKey)) { pending.get(expectedKey).conflict = true; savePending(); }
        message('lookup-message', expectedKey ? '此请求 ID 属于另一类任务。请打开原任务核对；不能作为当前版本的准备记录，也不会复用它提交新任务。' : `找到原任务 ${job.id} · ${jobState(job)[0]}。打开任务核对类型、绑定和原件；未重新提交。`, !!expectedKey);
        if (expectedKey) receipts.set(expectedKey, {job, message: '请求 ID 属于另一任务，已禁止按该 ID 重试准备；请核对原任务。', error: true});
      } else {
        message('lookup-message', `找到原准备任务 ${job.id} · ${jobState(job)[0]}。版本归属以清单的持久准备记录为准；没有重新提交。`);
        if (expectedKey) receipts.set(expectedKey, {job, message: '已找到原准备任务。正在核对本版本持久关联，不从普通任务回包推断资源已就绪。'});
        if (current && (!expectedKey || selected === expectedKey)) await selectVersion(current, true);
      }
    } catch (error) { message('lookup-message', error.status === 404 ? '尚未找到此请求。保留原请求 ID，稍后查询或明确使用原 ID 重试；没有自动创建新任务。' : `查询失败：${error.message}。保留原请求，不自动重发。`, true); }
    finally { lookupBusy = false; $('request-lookup').disabled = false; $('request-lookup').setAttribute('aria-busy', 'false'); renderPrepare(); }
  }
  $('refresh-all').addEventListener('click', () => { loadGames(); loadNode(); if (current) selectVersion(current, true); });
  $('refresh-detail').addEventListener('click', () => { if (current) selectVersion(current, true); });
  $('game-search').addEventListener('input', renderList);
  $('register-open').addEventListener('click', () => { $('registration-panel').hidden = false; $('manifest-input').focus(); });
  $('registration-close').addEventListener('click', () => { $('registration-panel').hidden = true; });
  $('manifest-preview').addEventListener('click', previewManifest);
  $('registration-form').addEventListener('submit', event => { event.preventDefault(); registerManifest(); });
  $('register-query').addEventListener('click', queryRegistration);
  $('access-toggle').addEventListener('click', () => { if (access?.steer) setAccess(false); else { $('unlock-form').hidden = false; $('steer-password').focus(); } });
  $('unlock-form').addEventListener('submit', event => { event.preventDefault(); setAccess(true); });
  $('unlock-close').addEventListener('click', closeUnlock);
  $('prepare-submit').addEventListener('click', () => prepare());
  $('prepare-retry').addEventListener('click', () => prepare(true));
  $('prepare-query').addEventListener('click', () => { const attempt = pending.get(selected); if (attempt) { $('request-id').value = attempt.request_id; lookup(attempt.request_id, selected); } });
  $('lookup-form').addEventListener('submit', event => { event.preventDefault(); const id = $('request-id').value.trim(); const owner = [...pending].find(([, value]) => value.request_id === id); lookup(id, owner?.[0] || null); });
  window.addEventListener('pagehide', () => { disposed = true; deployments.stop(); uploads.pause('页面已离开，停止发送；原上传 ID 保留，回来后重选文件并查询续传。'); ++listRevision; ++detailRevision; detailController?.abort(); closeUnlock(); });
  window.addEventListener('pageshow', event => { if (event.persisted) { disposed = false; loadAccess(); loadNode(); loadGames(); if (current) selectVersion(current, true); } });
  readSaved(); renderAccess(); loadAccess(); loadNode();
  if (registrationPending) { $('registration-panel').hidden = false; message('registration-message', `有未确认登记 ${registrationPending.game_id}/${registrationPending.version}。先读取原版本，未保存旧清单草稿。`); }
  const hash = new URLSearchParams(location.hash.slice(1)), initial = {game_id: hash.get('game'), version: hash.get('version')};
  loadGames().then(() => { if (!disposed && validID(initial.game_id) && validID(initial.version)) selectVersion(initial); });
})();
