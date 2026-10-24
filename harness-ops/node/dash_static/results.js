/* Explicit, immutable result records. Source checks are not scientific qualification. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id), API = '/dash/api';
  const KEY = 'hgamey.result-index.pending', ID = /^[A-Za-z0-9._-]{1,64}$/;
  const OUTCOMES = {gain: '报告有收益', no_gain: '报告无收益', regression: '报告退步', failed: '失败', not_run: '未运行', unknown: '结论未知'};
  const SCOPES = {module: '单模块', game_loop: '游戏主循环', display: '显示', gpu_submit_proxy: 'GPU 提交代理', power: '供电', image: '画质', unknown: '未知范围'};
  const METHODS = {measured: '实测声明', derived: '推导', estimated: '估计', reported: '报告值', unknown: '未知方法'};
  const FILTERS = ['game_id', 'game_version', 'device', 'research_id', 'direction_id', 'outcome', 'job_id'];
  let node = null, access = null, accessBusy = false, accessError = '', writeBusy = false, lookupBusy = false;
  let records = new Map(), selected = '', cursor = null, listQuery = '', listBusy = false, listRevision = 0, listController = null;
  let detailRevision = 0, detailController = null, pending = null, disposed = false, mode = 'register';
  const text = value => value == null || value === '' ? '未提供' : String(value);
  const time = value => Number.isFinite(value) ? new Date(value * 1000).toLocaleString() : '未记录';
  const validID = value => typeof value === 'string' && ID.test(value) && value !== '.' && value !== '..';
  const capable = () => node?.capabilities?.includes('results.index-v1') === true;
  const canWrite = () => capable() && access?.steer === true && !accessBusy && HGTarget.canWrite();
  const pathFor = (operation, id) => `${API}/${operation === 'import' ? 'result-imports' : 'results'}/${encodeURIComponent(id)}`;
  const errorText = error => error.name === 'AbortError' ? '请求超时或已取消，不能据此判定是否保存。' : error.message;
  function el(tag, value, className) { const item = document.createElement(tag); if (value != null) item.textContent = String(value); if (className) item.className = className; return item; }
  function message(id, value, error = false) { $(id).textContent = value; $(id).classList.toggle('task-error', error); }
  function facts(element, values) { element.replaceChildren(...values.flatMap(([label, value]) => [el('dt', label), el('dd', text(value))])); }
  function link(value, label, target = HGTarget.binding()) {
    const item = el('a', label);
    try {
      const url = new URL(value, location.origin);
      if (url.origin !== location.origin || url.username || url.password) return el('span', `${label}（链接不可用）`);
      if (/^\/(results|result-imports)\//.test(url.pathname)) url.pathname = '/dash/api' + url.pathname;
      if (!url.pathname.startsWith('/dash/')) return el('span', `${label}（链接不可用）`);
      item.href = HGTarget.url(url.pathname + url.search + url.hash, target);
    } catch (_) { return el('span', `${label}（链接不可用）`); }
    return item;
  }
  function badge(element, outcome) { element.textContent = OUTCOMES[outcome] || '结论未知'; element.className = `task-state ${['failed', 'regression'].includes(outcome) ? 'failed' : 'unknown'}`; }
  async function request(path, {body, target, signal} = {}) {
    const response = await HGTarget.fetch(path, {credentials: 'same-origin', cache: 'no-store', signal,
      ...(body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json', 'X-HG': '1'}, body: JSON.stringify(body)})}, target === undefined ? {} : {target});
    let value = null;
    try { value = await response.json(); } catch (_) { /* A malformed success has an unknown write outcome. */ }
    if (!response.ok) {
      const reason = typeof value?.error === 'string' ? value.error : value?.error?.message;
      const error = new Error(reason || (response.status === 401 ? '登录已失效，请重新登录。' : response.status === 403 ? '没有掌舵权限。' : `请求失败（HTTP ${response.status}）`));
      error.status = response.status; error.code = value?.code || value?.error?.code; throw error;
    }
    if (!value || typeof value !== 'object') throw new Error('节点没有返回可核对的 JSON，保存结果未知。');
    return value;
  }
  function checkRecord(value, operation, id, target) {
    if (value.schema_version !== 1 || value.node_id !== target?.node_id || (operation === 'import' ? value.import_id : value.result_id) !== id) throw new Error('记录的节点或 ID 不匹配，保留原请求待核对。');
    if (operation === 'import' ? !Array.isArray(value.result_ids) : !value.manifest) throw new Error('记录内容不完整，保留原请求待核对。');
    return value;
  }
  function savePending() {
    try {
      if (pending) sessionStorage.setItem(KEY, JSON.stringify({operation: pending.operation, id: pending.id, target: pending.target}));
      else sessionStorage.removeItem(KEY);
    } catch (_) { message('write-message', '浏览器无法保存未决 ID，请自行记录下方 ID 和目标后再离开。', true); }
  }
  function renderControls() {
    const allowed = canWrite(), locked = writeBusy || !!pending && !pending.refill;
    $('open-register').disabled = !allowed || writeBusy || !!pending && pending.operation !== 'register';
    $('open-import').disabled = !allowed || writeBusy || !!pending && pending.operation !== 'import';
    $('access-toggle').disabled = accessBusy || !access?.steer_configured;
    $('access-toggle').textContent = accessBusy ? '核对权限…' : access?.steer ? '锁定操作' : '解锁操作';
    $('access-toggle').setAttribute('aria-busy', String(accessBusy));
    message('access-message', accessError || (accessBusy ? '正在读取操作权限…' : !access ? '权限未知，保持只读。' : access.steer ? '可登记结果和导入报告；不会启动执行任务。' : access.steer_configured ? '可查看结果和原件；登记与导入需要掌舵权限。' : '此节点未配置掌舵密码，保持只读。'), !!accessError);
    for (const id of ['manifest-input', 'import-adapter', 'import-file', 'import-labels']) $(id).disabled = locked;
    $('import-id').disabled = locked || !!pending;
    for (const id of ['register-id', 'import-generate']) $(id).disabled = writeBusy || !!pending;
    for (const id of ['register-submit', 'import-submit']) { $(id).disabled = !allowed || locked; $(id).setAttribute('aria-busy', String(writeBusy)); }
    $('register-submit').textContent = writeBusy && mode === 'register' ? '正在保存…' : '保存结果记录';
    $('import-submit').textContent = writeBusy && mode === 'import' ? '正在导入…' : '导入并登记';
    $('write-close').disabled = writeBusy;
    $('lookup-submit').disabled = lookupBusy || !capable();
    $('pending-panel').hidden = !pending;
    if (pending) {
      const issue = HGTarget.problem(pending.target || null);
      $('pending-info').textContent = `${pending.operation === 'import' ? '导入' : '结果'} ID：${pending.id}\n节点：${pending.target?.node_id || '旧记录未绑定'}\n入口：${pending.target?.entry || '未记录'}`;
      message('pending-help', issue || (pending.refill ? '正在按原 ID 重填。不同内容会返回冲突；请尽量恢复原内容，提交不会创建新 ID。' : pending.missing ? '此次查询未找到原 ID；不证明早先写入不会完成。可显式使用原 ID 重试，节点会核对相同内容。' : '结果尚待核对。先查询原 ID；不会自动重发、换 ID 或改绑节点。刷新后需重选原文件。'), !!issue);
      $('pending-query').disabled = writeBusy || !capable() || !!issue;
      $('pending-refill').hidden = !!pending.body || !pending.missing || pending.refill;
      $('pending-refill').disabled = !allowed || writeBusy || !!issue;
      $('pending-retry').hidden = !pending.body || !pending.missing || pending.refill;
      $('pending-retry').disabled = !allowed || writeBusy || !!issue;
    }
  }
  async function loadAccess() {
    accessBusy = true; renderControls();
    try { access = await request(`${API}/access`); accessError = ''; $('login-link').hidden = true; }
    catch (error) { access = null; accessError = errorText(error); $('login-link').hidden = error.status !== 401; }
    finally { accessBusy = false; if (!disposed) renderControls(); }
  }
  async function setAccess(body, lock = false) {
    if (accessBusy) return;
    accessBusy = true; renderControls(); $('unlock-submit').disabled = true;
    try { await request(`${API}/steer/${lock ? 'lock' : 'unlock'}`, {body}); $('unlock-form').hidden = true; message('unlock-message', ''); }
    catch (error) { message('unlock-message', errorText(error), true); }
    finally { $('steer-password').value = ''; accessBusy = false; $('unlock-submit').disabled = false; await loadAccess(); }
  }
  function renderList() {
    $('result-count').textContent = `${records.size} 条已载入`;
    $('result-list').replaceChildren(...[...records.values()].map(item => {
      const li = el('li'), button = el('button', null, 'result-choice'), state = el('span'); button.type = 'button';
      button.setAttribute('aria-current', String(item.result_id === selected)); badge(state, item.outcome);
      button.append(el('strong', item.title || item.result_id), el('span', `${item.result_id} · ${time(item.registered_at)}`, 'result-meta'),
        el('span', [item.labels?.game_id, item.labels?.game_version, item.labels?.device].filter(Boolean).join(' · ') || '未提供游戏 / 设备标签', 'result-meta'),
        el('span', `${Number.isInteger(item.metric_count) ? item.metric_count : '未知'} 项指标 · ${Number.isInteger(item.source_count) ? item.source_count : '未知'} 项来源`, 'result-meta'), state);
      button.addEventListener('click', () => { selectResult(item.result_id); $('detail-heading').focus(); });
      const add = makeButton('加入比较', () => addCompareColumn(item.result_id)); add.classList.add('compare-list-add'); add.disabled = !!compareProblem() || compareColumns.length >= 4; li.append(button, add); return li;
    }));
    $('more-results').hidden = !cursor; $('more-results').disabled = listBusy;
  }
  async function loadList(append = false) {
    if (!capable() || disposed) return;
    const revision = ++listRevision, target = HGTarget.binding();
    listController?.abort(); listController = new AbortController();
    if (!append) { cursor = null; const filters = new URLSearchParams(); for (const key of FILTERS) { const value = $('filters').elements.namedItem(key).value.trim(); if (value) filters.set(key, value); } listQuery = filters.toString(); }
    const query = new URLSearchParams(listQuery);
    query.set('limit', '20'); if (append && cursor) query.set('cursor', cursor);
    listBusy = true; $('result-list').setAttribute('aria-busy', 'true'); $('filter-submit').disabled = true; $('more-results').disabled = true;
    message('list-message', append ? '正在读取下一页…' : '正在读取索引…');
    try {
      const value = await request(`${API}/results?${query}`, {signal: listController.signal, target});
      if (disposed || revision !== listRevision) return;
      if (value.schema_version !== 1 || value.node_id !== target?.node_id || !Array.isArray(value.items)) throw new Error('结果目录格式或节点身份不匹配。');
      if (!append) records = new Map();
      for (const item of value.items) records.set(item.result_id, item);
      cursor = value.next_cursor || null;
      message('list-message', records.size ? '选择记录查看声明、来源与当前任务。列表不会检查或扫描全部原件。' : '没有匹配的已登记记录；这不代表节点上没有实验原件。');
    } catch (error) { if (!disposed && revision === listRevision) message('list-message', `目录读取失败：${errorText(error)}${records.size ? ' 上次列表保留，尚未更新。' : ''}`, true); }
    finally { if (!disposed && revision === listRevision) { listBusy = false; $('result-list').setAttribute('aria-busy', 'false'); $('filter-submit').disabled = false; renderList(); } }
  }
  function metricItem(metric, evidence) {
    const item = el('li'), heading = el('div', null, 'metric-head'), details = el('details'), values = el('dl', null, 'facts');
    heading.append(el('strong', `${text(metric.name)} · ${text(metric.condition)}`), el('span', `${Number.isFinite(metric.value) ? String(metric.value) : '未知'}${metric.unit ? ` ${metric.unit}` : '（单位未提供）'}`, 'metric-value'));
    item.append(heading, el('p', `${SCOPES[metric.scope] || text(metric.scope)} · ${METHODS[metric.method] || text(metric.method)}${metric.region ? ` · ${{full_frame: '全帧', roi: 'ROI 区域', unknown: '区域未知'}[metric.region] || metric.region}` : ''}`, 'note'));
    if (!Number.isFinite(metric.value) || metric.unavailable_reason) item.append(el('p', metric.unavailable_reason || '没有提供有限数值。', 'note'));
    item.append(el('p', evidence?.status === 'matched_source' ? '登记时数值与来源字节匹配；不代表测量方法或收益已验证。' : `来源核对未验证${evidence?.reason ? `：${evidence.reason}` : '，请核查原件。'}`, 'note'));
    facts(values, [['窗口', metric.window], ['分母', metric.denominator], ['供电轨', metric.power_rail], ['图像域', metric.image_domain], ['区域', metric.region], ['来源名', metric.source?.name], ['JSON Pointer', metric.source?.pointer]]);
    details.append(el('summary', '指标范围与来源'), values); item.append(details); return item;
  }
  function sourceItem(source, observation, current, target = HGTarget.binding()) {
    const item = el('li'), head = el('div', null, 'source-head'), values = el('dl', null, 'facts');
    const states = {available: '可读取', unavailable: '不可读取', changed: '已改变', external: '外部历史声明'};
    head.append(el('strong', source.name), el('span', `当前：${states[current?.status] || '未提供观测'}`, 'note')); item.append(head);
    const rows = [['类型', ({job_output: '任务输出', imported_json: '已导入 JSON', external_reference: '外部引用（仅声明）'})[source.kind] || source.kind],
      ['登记时', states[observation?.status] || '未提供观测'], ['登记时原因', observation?.reason], ['当前原因', current?.reason]];
    if (source.kind === 'job_output') rows.push(['来源任务', source.job_id], ['输出路径', source.path]);
    else if (source.kind === 'imported_json') rows.push(['导入 ID', source.import_id]);
    else rows.push(['历史位置', source.locator], ['声明大小', source.declared_size == null ? null : `${source.declared_size} bytes`], ['声明 SHA256', source.declared_sha256]);
    if (observation?.size != null) rows.push(['登记时大小', `${observation.size} bytes`]);
    if (observation?.sha256) rows.push(['登记时 SHA256', observation.sha256]);
    facts(values, rows); item.append(values);
    if (current?.status === 'available' && current.url && source.kind !== 'external_reference') item.append(link(current.url, '打开登记原件', target));
    return item;
  }
  function renderDetail(value) {
    detailRecord = value; renderCompareControls();
    const m = value.manifest, current = value.current || {}, origin = m.origin || {};
    $('detail-content').hidden = false; $('result-title').textContent = m.title || value.result_id; badge($('result-outcome'), m.outcome);
    $('detail-updated').textContent = `登记于 ${time(value.registered_at)}；当前来源读取于 ${new Date().toLocaleString()}`;
    facts($('result-info'), [['结果 ID', value.result_id], ['节点', value.node_id], ['登记来源', value.provenance?.kind === 'import' ? `报告导入 · ${text(value.provenance.import_id)}` : value.provenance?.kind === 'registration' ? '显式登记' : '未提供'], ['关联任务', m.job_id || '无'],
      ...Object.entries({game_id: '游戏', game_version: '版本', device: '设备标签', research_id: '研究', direction_id: '方向'}).map(([key, label]) => [label, m.labels?.[key]]),
      ['原报告结论', m.source_outcome], ['生产者声明', origin.producer], ['生产者版本', origin.producer_version], ['证据来源声明', origin.evidence_origin || 'unknown'], ['观测时间声明', time(origin.observed_at)], ['替代旧记录', m.supersedes || '无']]);
    $('result-claims').replaceChildren(...(m.claims || []).map(value => el('li', value)));
    $('result-limitations').replaceChildren(...(m.limitations || []).map(value => el('li', value)));
    const evidence = new Map((value.metric_evidence || []).map(item => [item.index, item]));
    $('metric-list').replaceChildren(...(m.metrics?.length ? m.metrics.map((item, i) => metricItem(item, evidence.get(i))) : [el('li', '没有登记数值指标；不推断为零或实验失败。', 'note')]));
    const observations = new Map((value.source_observations || []).map(item => [item.name, item])), sources = new Map((current.sources || []).map(item => [item.name, item]));
    $('source-list').replaceChildren(...(m.sources?.length ? m.sources.map(item => sourceItem(item, observations.get(item.name), sources.get(item.name))) : [el('li', '没有登记来源；当前无法据此核对原件。', 'note')]));
    const job = current.job, record = job?.record;
    message('current-job-message', !m.job_id ? '没有关联本节点执行任务。历史引用不会按同名 ID 自动匹配。' : job?.status !== 'available' || !record ? `当前任务不可读取：${job?.reason || '未提供记录'}。结果登记仍保留。` : '这里是任务执行与恢复事实，不能代替上方实验结论。', !!m.job_id && (!record || job?.status !== 'available'));
    facts($('current-job-info'), record ? [['执行状态', record.phase || record.state], ['命令结果', record.outcome], ['退出码', Number.isInteger(record.exit) ? record.exit : '未知'], ['队列', record.queue?.state], ['恢复', record.recovery?.status || '未报告']] : []);
    $('current-job-link').hidden = !m.job_id; if (m.job_id) $('current-job-link').href = HGTarget.url(`/dash/jobs#job=${encodeURIComponent(m.job_id)}`);
    $('manifest-view').textContent = JSON.stringify(m, null, 2);
    $('evidence-view').textContent = JSON.stringify({provenance: value.provenance, source_observations: value.source_observations, metric_evidence: value.metric_evidence, current: value.current}, null, 2);
  }
  async function selectResult(id) {
    if (!validID(id) || !capable() || disposed) return;
    const changed = selected !== id, revision = ++detailRevision, target = HGTarget.binding(); selected = id;
    detailController?.abort(); detailController = new AbortController(); if (changed) { $('detail-content').hidden = true; detailRecord = null; renderCompareControls(); }
    renderList(); $('refresh-detail').disabled = true; $('result-detail').setAttribute('aria-busy', 'true');
    message('detail-message', `正在读取 ${id}…`); history.replaceState(null, '', `${location.pathname}${location.search}#result=${encodeURIComponent(id)}`);
    try {
      const value = checkRecord(await request(pathFor('register', id), {signal: detailController.signal, target}), 'register', id, target);
      if (disposed || revision !== detailRevision) return;
      renderDetail(value); message('detail-message', '');
    } catch (error) { if (!disposed && revision === detailRevision) message('detail-message', `读取失败：${errorText(error)}${!$('detail-content').hidden ? ' 上次详情保留，当前状态尚未更新。' : ''}`, true); }
    finally { if (!disposed && revision === detailRevision) { $('refresh-detail').disabled = false; $('result-detail').setAttribute('aria-busy', 'false'); } }
  }
  // Comparison is a read-only view of explicitly selected registered values.
  let compareTarget = null, compareColumns = [], compareRows = [{id: 'row-1', label: '指标 1', metric_indexes: []}];
  let compareNextColumn = 1, compareNextRow = 2, compareContext = null, compareRevision = 0, compareBusy = false, compareController = null;
  let detailRecord = null, compareCache = new Map(), compareLoads = new Map();
  const compareCapable = () => node?.capabilities?.includes('results.compare-v1') === true;
  const compareProblem = () => !compareCapable() ? '此节点未提供 results.compare-v1；可保留和导出选择，不能读取比较。' : !HGTarget.canWrite() ? '节点身份尚未绑定，或缺少 node.target-v1；不会转投其他节点。' : compareTarget ? HGTarget.problem(compareTarget) : '';
  const structuredText = value => value == null || value === '' ? '未记录' : typeof value === 'object' ? JSON.stringify(value) : String(value);
  const COMPARE_REASONS = {not_selected: '未选择指标', result_unavailable: '结果不可读取', invalid_index: '指标索引越界', value_unavailable: '数值缺失', unit_unavailable: '单位未记录', unit_mismatch: '单位不同', scope_unavailable: '范围未知', scope_mismatch: '范围不同', arithmetic_overflow: '算术溢出', baseline_nonpositive: '基线不大于零，百分比未定义'};
  const DESCRIPTORS = {name: '名称', condition: '条件', unit: '单位', scope: '范围', method: '方法', window: '窗口', denominator: '分母', power_rail: '供电轨', image_domain: '图像域', region: '区域'};
  function comparisonChanged() {
    compareRevision++; compareController?.abort(); compareBusy = false;
    $('compare-output').hidden = true; $('compare-share-result').hidden = true; $('compare-spec-panel').hidden = true;
    message('compare-message', '选择已改变，请读取对照；不会自动重新计算或改写原结果。'); renderCompareControls();
  }
  function renderCompareControls() {
    const issue = compareProblem(), full = compareColumns.length >= 4;
    message('compare-capability', issue || `比较目标：${compareTarget?.node_id || HGTarget.binding()?.node_id || '未绑定'} · 仅同节点登记值；描述字段一致也不代表科学可比。`, !!issue);
    $('compare-add').disabled = !!issue || full;
    $('compare-add-detail').disabled = !!issue || full || !detailRecord || detailRecord.result_id !== selected;
    document.querySelectorAll('.compare-list-add').forEach(button => { button.disabled = !!issue || full; });
    $('compare-run').disabled = !!issue || compareBusy || compareColumns.length < 2;
    $('compare-run').textContent = compareBusy ? '正在读取对照…' : '读取登记值对照'; $('compare-run').setAttribute('aria-busy', String(compareBusy));
    $('compare-output').setAttribute('aria-busy', String(compareBusy));
    $('compare-add-row').disabled = compareRows.length >= 16 || !compareColumns.length;
    $('compare-export').disabled = compareColumns.length < 2;
    $('compare-share').disabled = compareColumns.length < 2 || !!issue;
  }
  function makeButton(label, callback) { const button = el('button', label, 'ghost'); button.type = 'button'; button.addEventListener('click', callback); return button; }
  function renderCompareEditor() {
    $('compare-columns').replaceChildren(...compareColumns.map((column, index) => {
      const card = el('div', null, 'compare-column'), label = el('label', index === 0 ? '基线标签' : `候选 ${index} 标签`), input = el('input');
      input.value = column.label; input.maxLength = 128; input.addEventListener('input', () => { column.label = input.value; comparisonChanged(); refreshPickerLabels(); }); label.append(input);
      card.append(label, el('p', column.result_id, 'result-meta'));
      const cached = compareCache.get(column.result_id);
      card.append(el('p', compareLoads.has(column.result_id) ? '正在读取可选指标…' : cached?.value ? `${cached.value.manifest.metrics?.length || 0} 项原指标；请逐行明确选择。` : cached?.error || '尚未读取指标。', cached?.error ? 'note task-error' : 'note'));
      const actions = el('div', null, 'result-actions');
      if (index) actions.append(makeButton('设为基线', () => { compareColumns.unshift(...compareColumns.splice(index, 1)); for (const row of compareRows) row.metric_indexes.unshift(...row.metric_indexes.splice(index, 1)); comparisonChanged(); renderCompareEditor(); }));
      actions.append(makeButton('移除此列', () => { compareColumns.splice(index, 1); for (const row of compareRows) row.metric_indexes.splice(index, 1); comparisonChanged(); renderCompareEditor(); }));
      if (cached?.error) actions.append(makeButton('重读指标', () => loadCompareRecord(column.result_id)));
      card.append(actions); return card;
    }));
    $('compare-rows').replaceChildren(...compareRows.map(row => {
      const block = el('div', null, 'compare-edit-row'), heading = el('div', null, 'section-heading'), label = el('label', '指标行标题'), input = el('input');
      input.value = row.label; input.maxLength = 128; input.addEventListener('input', () => { row.label = input.value; comparisonChanged(); }); label.append(input); heading.append(label);
      const remove = makeButton('移除此行', () => { compareRows = compareRows.filter(item => item !== row); comparisonChanged(); renderCompareEditor(); }); remove.disabled = compareRows.length <= 1; heading.append(remove);
      const pickers = el('div', null, 'compare-pickers');
      compareColumns.forEach((column, index) => {
        const field = el('label'), name = el('span'), select = el('select'), cached = compareCache.get(column.result_id), metrics = cached?.value?.manifest?.metrics || [];
        name.dataset.comparePicker = column.id; field.append(name); select.setAttribute('aria-label', `${row.label} · ${column.label || column.id}`);
        select.append(new Option('未选择（保留空项）', ''));
        metrics.forEach((metric, i) => select.append(new Option(`#${i} · ${text(metric.name)} · ${text(metric.condition)} · ${Number.isFinite(metric.value) ? metric.value : '未知'} ${metric.unit || ''} · ${SCOPES[metric.scope] || text(metric.scope)}`, String(i))));
        const chosen = row.metric_indexes[index];
        if (chosen != null && chosen >= metrics.length) select.append(new Option(`#${chosen}（原索引；当前指标未取得或越界）`, String(chosen)));
        select.value = chosen == null ? '' : String(chosen); select.disabled = compareLoads.has(column.result_id) || !cached?.value;
        select.addEventListener('change', () => { row.metric_indexes[index] = select.value === '' ? null : Number(select.value); comparisonChanged(); }); field.append(select); pickers.append(field);
      });
      block.append(heading, pickers); return block;
    }));
    refreshPickerLabels(); renderCompareControls();
  }
  function refreshPickerLabels() {
    for (const item of document.querySelectorAll('[data-compare-picker]')) {
      const index = compareColumns.findIndex(column => column.id === item.dataset.comparePicker), column = compareColumns[index];
      item.textContent = `${index === 0 ? '基线' : `候选 ${index}`} · ${column?.label || column?.id || ''}`;
    }
  }
  async function loadCompareRecord(id) {
    if (compareLoads.has(id) || compareProblem() || !compareTarget) return;
    const target = compareTarget, controller = new AbortController(), timer = setTimeout(() => controller.abort(), 20000); compareLoads.set(id, controller); renderCompareEditor();
    try {
      const value = checkRecord(await request(pathFor('register', id), {target, signal: controller.signal}), 'register', id, target);
      if (!disposed && compareTarget === target) compareCache.set(id, {value});
    } catch (error) { if (!disposed && compareTarget === target) compareCache.set(id, {error: `指标读取失败：${error.name === 'AbortError' ? '读取超时或取消' : error.message}。原选择保留，仍可读取部分对照。`}); }
    finally { clearTimeout(timer); if (compareLoads.get(id) === controller) compareLoads.delete(id); if (!disposed && compareTarget === target) renderCompareEditor(); }
  }
  function addCompareColumn(id, value = null) {
    if (compareProblem() || compareColumns.length >= 4) return;
    if (!validID(id)) { message('compare-message', '请输入有效的本节点结果 ID。', true); return; }
    if (!compareTarget) compareTarget = HGTarget.binding();
    const title = (value?.manifest?.title || records.get(id)?.title || id).slice(0, 128);
    while (compareColumns.some(column => column.id === `column-${compareNextColumn}`)) compareNextColumn++;
    compareColumns.push({id: `column-${compareNextColumn++}`, label: title, result_id: id}); compareRows.forEach(row => row.metric_indexes.push(null));
    if (value?.node_id === compareTarget.node_id && value.result_id === id) compareCache.set(id, {value});
    comparisonChanged(); $('compare-panel').open = true; renderCompareEditor(); $('compare-panel').scrollIntoView({block: 'start'});
    if (!compareCache.get(id)?.value) loadCompareRecord(id);
  }
  function comparisonSpec() {
    const spec = {schema: 'phonefarm/result-compare@1', node_id: compareTarget?.node_id, columns: compareColumns.map(column => ({...column})), rows: compareRows.map(row => ({...row, metric_indexes: [...row.metric_indexes]}))};
    if (compareContext !== null) spec.context = structuredClone(compareContext);
    validateComparisonSpec(spec); return spec;
  }
  function validateComparisonSpec(spec) {
    if (new TextEncoder().encode(JSON.stringify(spec)).length > 16384) throw new Error('比较选择超过 16 KiB，请精简标签和范围声明。');
    if (!spec || spec.schema !== 'phonefarm/result-compare@1' || !/^[0-9a-f]{32}$/.test(spec.node_id || '') || !Array.isArray(spec.columns) || spec.columns.length < 2 || spec.columns.length > 4 || !Array.isArray(spec.rows) || spec.rows.length < 1 || spec.rows.length > 16) throw new Error('比较需明确节点、2–4 列和 1–16 个指标行。');
    if (new Set(spec.columns.map(item => item.id)).size !== spec.columns.length || new Set(spec.rows.map(item => item.id)).size !== spec.rows.length) throw new Error('比较列或行的 ID 重复。');
    for (const column of spec.columns) if (!validID(column.id) || !validID(column.result_id) || typeof column.label !== 'string' || !column.label.trim() || column.label.length > 128) throw new Error('每列需有效 ID、结果 ID 和非空标签。');
    for (const row of spec.rows) if (!validID(row.id) || typeof row.label !== 'string' || !row.label.trim() || row.label.length > 128 || !Array.isArray(row.metric_indexes) || row.metric_indexes.length !== spec.columns.length || row.metric_indexes.some(index => index !== null && (!Number.isInteger(index) || index < 0 || index > 255))) throw new Error('每行需非空标题，并为每列明确选择 0–255 的指标索引或空项。');
    if ('context' in spec) {
      if (!spec.context || typeof spec.context !== 'object' || Array.isArray(spec.context) || ('scope' in spec.context && (typeof spec.context.scope !== 'string' || !spec.context.scope.trim()))) throw new Error('比较范围需非空 scope 文字；不填写可省略。');
      for (const key of ['assumptions', 'interventions']) if (key in spec.context && (!Array.isArray(spec.context[key]) || spec.context[key].length > 16 || spec.context[key].some(value => typeof value !== 'string' || !value.trim()))) throw new Error('假设或干预需最多 16 项非空文字。');
    }
  }
  function resetComparison() {
    compareController?.abort(); for (const controller of compareLoads.values()) controller.abort(); compareLoads.clear(); compareCache.clear(); compareTarget = null;
    compareColumns = []; compareRows = [{id: 'row-1', label: '指标 1', metric_indexes: []}]; compareNextColumn = 1; compareNextRow = 2; compareContext = null;
    for (const id of ['compare-scope', 'compare-assumptions', 'compare-interventions']) $(id).value = '';
    const url = new URL(location.href); url.searchParams.delete('compare'); history.replaceState(null, '', url.pathname + url.search + url.hash);
    comparisonChanged(); renderCompareEditor(); message('compare-message', '已清空本页比较选择；原结果和登记请求不变。');
  }
  function displayDelta(delta) {
    const reason = value => COMPARE_REASONS[value] || value || '未提供计算依据';
    const signed = value => `${value > 0 ? '+' : ''}${value}`;
    return `登记值差：${Number.isFinite(delta?.value) ? `${signed(delta.value)} ${delta.unit || ''}` : `未计算（${reason(delta?.reason)}）`}\n相对基线：${Number.isFinite(delta?.percent) ? `${signed(delta.percent)}%` : `未计算（${reason(delta?.percent_reason || delta?.reason)}）`}`;
  }
  function renderComparison(value, target) {
    const output = $('compare-output'); output.replaceChildren(); output.hidden = false;
    output.append(el('h2', '登记值对照'), el('p', `基线：${value.columns[0].label} · ${value.columns[0].result_id}；节点 ${value.node_id}。读取于 ${new Date().toLocaleString()}。`, 'note'), el('p', '差值由节点按 candidate − baseline 计算，百分比以正基线为分母。这里不判断优劣、不建立科学可比资格；单位和范围冲突不会换算。', 'note'));
    if (value.partial) output.append(el('p', '部分结果不可读取；其余登记值保留，缺项没有填零。', 'note task-error'));
    if (value.spec.context) { const context = el('dl', null, 'facts'); facts(context, [['作者声明的范围', value.spec.context.scope], ['假设', (value.spec.context.assumptions || []).join('；')], ['有意干预', (value.spec.context.interventions || []).join('；')]]); output.append(context); }
    for (const row of value.rows) {
      const section = el('section', null, 'compare-matrix-row'), cells = el('div', null, 'compare-cells'); cells.style.setProperty('--compare-count', String(value.columns.length)); section.append(el('h3', row.label));
      row.cells.forEach((cell, index) => {
        const column = value.columns[index], card = el('div', null, 'compare-cell'); card.append(el('h4', `${index === 0 ? '基线' : `候选 ${index}`} · ${column.label}`), el('p', `${column.result_id} · ${cell.metric_index == null ? '未选指标' : `指标 #${cell.metric_index}`}`, 'result-meta'));
        if (cell.status === 'available' && cell.metric) { const metrics = el('ul', null, 'metric-list'); metrics.append(metricItem(cell.metric, cell.metric_evidence)); card.append(metrics); }
        else card.append(el('p', ({not_selected: '此列未选择本行指标。', result_unavailable: '结果不可读取。', invalid_index: '指标索引越界。'})[cell.status] || '指标状态未知。', 'note'), el('p', cell.reason || column.error?.message || '', 'note'));
        card.append(el('p', displayDelta(cell.delta), 'compare-delta'));
        const relation = cell.descriptor_relation, status = {match: '已记录字段一致', mismatch: '描述字段存在差异', incomplete: '描述字段记录不全', unavailable: '描述关系不可读取'}[relation?.status] || '描述关系未知';
        card.append(el('strong', status)); const differences = el('ul', null, 'compare-relations');
        for (const field of relation?.fields || []) if (field.relation !== 'match') differences.append(el('li', `${DESCRIPTORS[field.field] || field.field} · ${field.relation === 'mismatch' ? '不同' : '未完整记录'}：基线 ${structuredText(field.baseline)} → 本列 ${structuredText(field.candidate)}`));
        card.append(differences); const all = el('details'), table = el('dl', null, 'facts'); facts(table, (relation?.fields || []).map(field => [DESCRIPTORS[field.field] || field.field, `基线 ${structuredText(field.baseline)} / 本列 ${structuredText(field.candidate)} · ${{match: '一致', mismatch: '不同', unrecorded: '未记录'}[field.relation] || field.relation}`])); all.append(el('summary', '全部描述字段'), table); card.append(all); cells.append(card);
      }); section.append(cells); output.append(section);
    }
    for (const [index, column] of value.columns.entries()) {
      const section = el('section', null, 'compare-provenance'), record = column.result, manifest = record?.manifest;
      section.append(el('h3', `${index === 0 ? '基线' : `候选 ${index}`} · ${column.label}`), el('p', `${column.node_id} / ${column.result_id}`, 'result-meta'));
      if (column.status !== 'available' || !manifest) { section.append(el('p', `结果不可读取：${column.error?.message || '原因未提供'}`, 'note task-error')); output.append(section); continue; }
      const details = el('dl', null, 'facts'); facts(details, [['登记时间', time(record.registered_at)], ['原登记结论', OUTCOMES[manifest.outcome] || '未知'], ['原报告结论', manifest.source_outcome], ['游戏 / 版本', [manifest.labels?.game_id, manifest.labels?.game_version].filter(Boolean).join(' / ')], ['设备标签', manifest.labels?.device], ['证据来源声明', manifest.origin?.evidence_origin], ['关联任务', manifest.job_id || '无'], ['当前队列 / 恢复', record.current?.job?.record ? `${text(record.current.job.record.queue?.state)} / ${text(record.current.job.record.recovery?.status)}` : record.current?.job?.reason || '未取得当前任务记录']]); section.append(details);
      section.append(link(`/dash/results#result=${encodeURIComponent(column.result_id)}`, '查看原结果与完整声明', target));
      const notes = el('ul', null, 'result-notes'); for (const claim of manifest.claims || []) notes.append(el('li', `原声明：${claim}`)); for (const limit of manifest.limitations || []) notes.append(el('li', `局限：${limit}`)); section.append(notes);
      const observations = new Map((record.source_observations || []).map(item => [item.name, item])), current = new Map((record.current?.sources || []).map(item => [item.name, item])), sources = el('ul', null, 'source-list');
      for (const source of manifest.sources || []) sources.append(sourceItem(source, observations.get(source.name), current.get(source.name), target));
      if (!manifest.sources?.length) sources.append(el('li', '没有登记来源，无法据此核对原件。', 'note')); section.append(sources); output.append(section);
    }
  }
  async function runComparison() {
    if (compareBusy || compareProblem()) return;
    let spec; try { spec = comparisonSpec(); } catch (error) { message('compare-message', error.message, true); return; }
    const target = compareTarget, revision = ++compareRevision, controller = new AbortController(); compareController?.abort(); compareController = controller; compareBusy = true;
    const timer = setTimeout(() => controller.abort(), 20000); $('compare-output').hidden = true; renderCompareControls(); message('compare-message', '正在读取所选结果与节点计算的登记值差…');
    try {
      const value = await request(`${API}/result-compare?${new URLSearchParams({spec: JSON.stringify(spec)})}`, {target, signal: controller.signal});
      if (disposed || revision !== compareRevision) return;
      if (HGTarget.problem(target)) throw new Error(HGTarget.problem(target));
      if (value.schema_version !== 1 || value.node_id !== target.node_id || value.basis !== 'registered_values' || value.comparability !== 'not_established' || JSON.stringify(value.spec) !== JSON.stringify(spec) || value.baseline_column_id !== spec.columns[0].id || !Array.isArray(value.columns) || value.columns.length !== spec.columns.length || !Array.isArray(value.rows) || value.rows.length !== spec.rows.length || value.columns.some((column, index) => column.id !== spec.columns[index].id || column.result_id !== spec.columns[index].result_id) || value.rows.some((row, index) => row.id !== spec.rows[index].id || !Array.isArray(row.cells) || row.cells.length !== spec.columns.length || row.cells.some((cell, i) => cell.column_id !== spec.columns[i].id))) throw new Error('比较响应与原节点或选择不一致；保留原选择待核对。');
      for (const column of value.columns) { if (column.status === 'available' && column.result?.manifest) compareCache.set(column.result_id, {value: column.result}); else compareCache.set(column.result_id, {error: column.error?.message || '结果不可读取'}); }
      renderCompareEditor(); renderComparison(value, target); message('compare-message', '只读对照已返回；原登记、来源状态与任务恢复分别展示。');
    } catch (error) { if (!disposed && revision === compareRevision) message('compare-message', `对照读取失败：${error.name === 'AbortError' ? '读取超时或取消' : error.message}。原选择保留，可显式重试。`, true); }
    finally { clearTimeout(timer); if (!disposed && revision === compareRevision) { compareBusy = false; renderCompareControls(); } }
  }
  function exportComparison(share = false) {
    try {
      const spec = comparisonSpec(), raw = JSON.stringify(spec, null, 2); $('compare-spec').textContent = raw; $('compare-spec-panel').hidden = false;
      if (share) {
        if (compareProblem()) throw new Error(compareProblem());
        const url = new URL('/dash/results', location.origin); url.searchParams.set('expected_node_id', spec.node_id); url.searchParams.set('compare', JSON.stringify(spec));
        $('compare-share-url').value = url.href; $('compare-share-link').href = url.href; $('compare-share-result').hidden = false; message('compare-message', '分享链接固定当前节点、列、行和范围声明；只读重载，未包含凭据。');
      } else {
        const url = URL.createObjectURL(new Blob([raw + '\n'], {type: 'application/json'})), a = el('a'); a.href = url; a.download = 'result-compare.json'; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); message('compare-message', '选择 JSON 已导出；下方也保留可复制文本。它不包含新的测量或可比资格。');
      }
    } catch (error) { message('compare-message', error.message, true); }
  }
  function restoreComparison(raw) {
    if (!raw) return;
    $('compare-panel').open = true;
    try {
      const spec = JSON.parse(raw); validateComparisonSpec(spec); compareTarget = {entry: location.origin, node_id: spec.node_id}; compareColumns = spec.columns; compareRows = spec.rows;
      compareNextColumn = 1; while (compareColumns.some(column => column.id === `column-${compareNextColumn}`)) compareNextColumn++;
      compareNextRow = 1; while (compareRows.some(row => row.id === `row-${compareNextRow}`)) compareNextRow++;
      compareContext = 'context' in spec ? spec.context : null;
      $('compare-scope').value = spec.context?.scope || ''; $('compare-assumptions').value = (spec.context?.assumptions || []).join('\n'); $('compare-interventions').value = (spec.context?.interventions || []).join('\n');
      renderCompareEditor(); if (!compareProblem()) runComparison();
    } catch (error) { message('compare-message', `分享选择不可读取：${error.message}。没有改投其他节点或发起比较。`, true); }
  }
  function defaultManifest(id, jobID = null) {
    return {schema: 'phonefarm/result@1', result_id: id, title: '', job_id: jobID, labels: {game_id: null, game_version: null, device: null, research_id: null, direction_id: null},
      origin: {producer: null, producer_version: null, evidence_origin: 'unknown', observed_at: null}, outcome: 'unknown', source_outcome: null, sources: [], metrics: [], claims: [], limitations: [], supersedes: null};
  }
  function openWrite(operation, jobID = null) {
    if (pending && pending.operation !== operation) return;
    mode = operation; $('write-panel').hidden = false; $('register-form').hidden = operation !== 'register'; $('import-form').hidden = operation !== 'import';
    $('write-heading').textContent = operation === 'import' ? '导入小报告' : '登记结果';
    if (operation === 'register' && !$('manifest-input').value) $('manifest-input').value = JSON.stringify(defaultManifest(pending?.id || crypto.randomUUID(), jobID), null, 2);
    if (operation === 'import' && !$('import-id').value) $('import-id').value = pending?.id || crypto.randomUUID();
    renderControls();
  }
  function parseManifest() {
    const value = JSON.parse($('manifest-input').value);
    if (!value || Array.isArray(value) || value.schema !== 'phonefarm/result@1' || !validID(value.result_id) || typeof value.title !== 'string' || !value.title.trim() || value.title.length > 240) throw new Error('请填写 phonefarm/result@1、有效 result_id 和非空标题（最多 240 字符）。');
    if (new TextEncoder().encode(JSON.stringify(value)).length > 262144) throw new Error('结果登记超过 256 KiB；大原件请保留为命名来源。');
    if (pending && value.result_id !== pending.id) throw new Error(`此未决记录必须保留原 ID：${pending.id}`);
    return value;
  }
  async function importBody() {
    const id = $('import-id').value.trim(), file = $('import-file').files[0];
    if (!validID(id) || id.length > 48) throw new Error('导入 ID 须为 1–48 位字母、数字、点、下划线或短横线。');
    if (pending && id !== pending.id) throw new Error(`必须保留原导入 ID：${pending.id}`);
    if (!file) throw new Error('请重新选择原 JSON 文件；浏览器没有保存文件内容。');
    if (file.size > 1048576) throw new Error('文件超过 1 MiB，请选择小 JSON 报告。');
    const labels = $('import-labels').value.trim() ? JSON.parse($('import-labels').value) : {};
    if (!labels || typeof labels !== 'object' || Array.isArray(labels)) throw new Error('标签必须是 JSON 对象。');
    const adapter = $('import-adapter').value, content = await file.text();
    if (new TextEncoder().encode(content).length > 1048576) throw new Error('解码后的 UTF-8 文本超过 1 MiB。');
    return {schema: 'phonefarm/result-import@1', import_id: id, adapter, filename: file.name, content, labels};
  }
  function receiptLinks(element, value, operation, target) {
    element.replaceChildren();
    const ids = operation === 'import' ? value.result_ids : [value.result_id];
    for (const id of ids) {
      const a = link(`/dash/results#result=${encodeURIComponent(id)}`, `查看结果 ${id}`, target);
      a.addEventListener('click', event => { if (!HGTarget.problem(target)) { event.preventDefault(); selectResult(id); } }); element.append(a);
    }
    if (operation === 'import' && value.source_url) element.append(link(value.source_url, `导入原件 · ${value.size} bytes`, target));
  }
  function found(value, item, fromQuery) {
    checkRecord(value, item.operation, item.id, item.target);
    if (pending === item) { pending = null; savePending(); }
    receiptLinks($('write-links'), value, item.operation, item.target);
    message('write-message', `${fromQuery ? '已找到原 ID 记录；请核对其不可变内容。' : '记录已保存。'}${item.operation === 'import' ? ` 登记了 ${value.result_ids.length} 条结果；原报告字节已保留。` : ''} 登记不表示实验或设备恢复已通过。`);
    loadList(); renderControls();
  }
  async function sendPending(item) {
    if (!item.body || !canWrite() || writeBusy || HGTarget.problem(item.target || null)) return;
    writeBusy = true; item.missing = false; item.refill = false; savePending(); renderControls();
    message('write-message', `正在保存 ${item.id}；写入只发送一次…`); $('write-links').replaceChildren();
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 15000);
    try {
      const value = await request(`${API}/${item.operation === 'import' ? 'result-imports' : 'results'}`, {body: item.body, target: item.target, signal: controller.signal});
      if (!disposed) found(value, item, false);
    } catch (error) {
      if (disposed) return;
      if (error.status && error.status >= 400 && error.status < 500 && error.status !== 409) {
        if (pending === item) { pending = null; savePending(); }
        message('write-message', `节点拒绝了此次保存（${item.id}）：${errorText(error)}。可修改后再提交。`, true);
      } else message('write-message', `${error.status === 409 ? 'ID 或目标冲突；保留原 ID，先查询原记录。' : '保存结果不确定；保留原 ID 和内容，不自动重试。'} ${errorText(error)}`, true);
      if ([401, 403].includes(error.status)) { access = null; accessError = errorText(error); $('login-link').hidden = error.status !== 401; }
    } finally { clearTimeout(timer); writeBusy = false; if (!disposed) renderControls(); }
  }
  async function submit(operation) {
    if (!canWrite() || writeBusy || pending && !pending.refill) return;
    let body;
    // File.text is asynchronous; hold the form and its target while reading local bytes.
    const target = pending?.target || HGTarget.binding(), originalPending = pending;
    writeBusy = true; renderControls();
    try {
      body = operation === 'import' ? await importBody() : parseManifest(); HGTarget.assert(target);
      if (disposed || pending !== originalPending) return;
      pending = {operation, id: operation === 'import' ? body.import_id : body.result_id, target, body}; savePending();
    } catch (error) { message('write-message', errorText(error), true); return; }
    finally { writeBusy = false; if (!disposed) renderControls(); }
    if (!disposed) await sendPending(pending);
  }
  async function queryPending() {
    const item = pending; if (!item || writeBusy || HGTarget.problem(item.target || null)) return;
    writeBusy = true; renderControls(); message('write-message', `正在查询原 ID ${item.id}…`);
    try {
      const value = await request(pathFor(item.operation, item.id), {target: item.target});
      if (!disposed && pending === item) found(value, item, true);
    } catch (error) {
      if (!disposed && pending === item) { item.missing = error.status === 404; message('write-message', error.status === 404 ? '当前节点尚无此 ID；请使用下面的原 ID 操作，不能据此换 ID 重发。' : `原 ID 查询失败：${errorText(error)}。未提交新请求。`, true); }
    } finally { writeBusy = false; if (!disposed) renderControls(); }
  }
  async function lookup() {
    if (lookupBusy || !capable()) return;
    const operation = $('lookup-kind').value, id = $('lookup-id').value.trim(), target = HGTarget.binding();
    if (!validID(id) || operation === 'import' && id.length > 48) { message('lookup-message', '请输入有效的原 ID。', true); return; }
    if (pending?.id === id && pending.operation === operation) { openWrite(operation); await queryPending(); return; }
    lookupBusy = true; $('lookup-submit').disabled = true; $('lookup-links').replaceChildren(); message('lookup-message', '正在只读查询…');
    try { const value = checkRecord(await request(pathFor(operation, id), {target}), operation, id, target); if (!disposed) { receiptLinks($('lookup-links'), value, operation, target); message('lookup-message', '已找到记录。此查询没有提交、重跑或导入任何内容。'); } }
    catch (error) { if (!disposed) message('lookup-message', errorText(error), true); }
    finally { lookupBusy = false; if (!disposed) $('lookup-submit').disabled = false; }
  }
  async function refresh() {
    $('refresh-all').disabled = true; $('refresh-all').setAttribute('aria-busy', 'true');
    try {
      node = await HGTarget.refresh();
      message('capability-message', capable() ? '只检索本节点已登记结果；按需读取详情时核对明确来源，不采集设备。' : '此节点未提供 results.index-v1，无法读取或写入新结果索引。旧任务与原件仍在任务页。', !capable());
      if (capable()) await Promise.all([loadList(), selected ? selectResult(selected) : null]);
    } catch (error) { node = null; message('capability-message', errorText(error), true); }
    finally { $('refresh-all').disabled = false; $('refresh-all').setAttribute('aria-busy', 'false'); renderControls(); renderCompareControls(); }
  }
  $('open-register').addEventListener('click', () => openWrite('register'));
  $('open-import').addEventListener('click', () => openWrite('import'));
  $('write-close').addEventListener('click', () => { $('write-panel').hidden = true; });
  $('register-id').addEventListener('click', () => {
    if (pending || writeBusy) return;
    try { const value = $('manifest-input').value.trim() ? JSON.parse($('manifest-input').value) : defaultManifest(''); if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('请先修正 JSON 对象。'); value.result_id = crypto.randomUUID(); $('manifest-input').value = JSON.stringify(value, null, 2); }
    catch (error) { message('write-message', `未覆盖原内容：${errorText(error)}`, true); }
  });
  $('import-generate').addEventListener('click', () => { if (!pending && !writeBusy) $('import-id').value = crypto.randomUUID(); });
  $('import-file').addEventListener('change', () => { const file = $('import-file').files[0]; message('import-file-info', file ? `${file.name} · ${file.size} bytes；尚未上传。` : '未选文件；可重新选择原文件续接同一 ID。', !!file && file.size > 1048576); });
  $('register-form').addEventListener('submit', event => { event.preventDefault(); submit('register'); });
  $('import-form').addEventListener('submit', event => { event.preventDefault(); submit('import'); });
  $('pending-query').addEventListener('click', queryPending);
  $('pending-retry').addEventListener('click', () => { if (pending?.missing) sendPending(pending); });
  $('pending-refill').addEventListener('click', () => { if (!pending || pending.body || !pending.missing || !canWrite()) return; pending.refill = true; openWrite(pending.operation); if (pending.operation === 'import') $('import-id').value = pending.id; else { try { const value = JSON.parse($('manifest-input').value); value.result_id = pending.id; $('manifest-input').value = JSON.stringify(value, null, 2); } catch (_) { /* Keep invalid draft for correction; submit checks the original ID. */ } } renderControls(); });
  $('filters').addEventListener('submit', event => { event.preventDefault(); cursor = null; loadList(); });
  $('filter-reset').addEventListener('click', () => { $('filters').reset(); cursor = null; loadList(); });
  $('more-results').addEventListener('click', () => loadList(true));
  $('refresh-detail').addEventListener('click', () => selectResult(selected));
  $('refresh-all').addEventListener('click', async () => { await Promise.all([refresh(), loadAccess()]); });
  $('lookup-form').addEventListener('submit', event => { event.preventDefault(); lookup(); });
  $('access-toggle').addEventListener('click', () => { if (access?.steer) setAccess({}, true); else { $('unlock-form').hidden = false; $('steer-password').focus(); } });
  $('unlock-close').addEventListener('click', () => { $('steer-password').value = ''; $('unlock-form').hidden = true; });
  $('unlock-form').addEventListener('submit', event => { event.preventDefault(); setAccess({pw: $('steer-password').value}); });
  $('compare-add-form').addEventListener('submit', event => { event.preventDefault(); addCompareColumn($('compare-result-id').value.trim()); });
  $('compare-add-detail').addEventListener('click', () => { if (detailRecord?.result_id === selected) addCompareColumn(selected, detailRecord); });
  $('compare-add-row').addEventListener('click', () => { if (compareRows.length >= 16) return; while (compareRows.some(row => row.id === `row-${compareNextRow}`)) compareNextRow++; compareRows.push({id: `row-${compareNextRow++}`, label: `指标 ${compareRows.length + 1}`, metric_indexes: compareColumns.map(() => null)}); comparisonChanged(); renderCompareEditor(); });
  for (const id of ['compare-scope', 'compare-assumptions', 'compare-interventions']) $(id).addEventListener('input', () => {
    compareContext = {};
    if ($('compare-scope').value.trim()) compareContext.scope = $('compare-scope').value.trim();
    for (const key of ['assumptions', 'interventions']) { const values = $(`compare-${key}`).value.split('\n').map(value => value.trim()).filter(Boolean); if (values.length) compareContext[key] = values; }
    if (!Object.keys(compareContext).length) compareContext = null;
    comparisonChanged();
  });
  $('compare-run').addEventListener('click', runComparison);
  $('compare-reset').addEventListener('click', resetComparison);
  $('compare-export').addEventListener('click', () => exportComparison());
  $('compare-share').addEventListener('click', () => exportComparison(true));
  document.addEventListener('hgamey-target-change', () => { renderControls(); renderCompareControls(); if (compareTarget && HGTarget.problem(compareTarget)) { compareRevision++; compareController?.abort(); compareBusy = false; renderCompareControls(); } });
  window.addEventListener('hashchange', () => { const id = new URLSearchParams(location.hash.slice(1)).get('result'); if (validID(id)) selectResult(id); });
  window.addEventListener('pagehide', () => { disposed = true; listController?.abort(); detailController?.abort(); compareController?.abort(); for (const controller of compareLoads.values()) controller.abort(); });
  try { const saved = JSON.parse(sessionStorage.getItem(KEY) || 'null'); if (saved && ['register', 'import'].includes(saved.operation) && validID(saved.id)) pending = {operation: saved.operation, id: saved.id, target: saved.target || null}; } catch (_) { /* Damaged storage does not establish a request or target. */ }
  const query = new URLSearchParams(location.search), hash = new URLSearchParams(location.hash.slice(1));
  for (const key of FILTERS) if (query.has(key)) $('filters').elements.namedItem(key).value = query.get(key);
  if (pending) openWrite(pending.operation);
  else if (/^[0-9a-f]{12}$/.test(hash.get('register') || '')) openWrite('register', hash.get('register'));
  selected = validID(hash.get('result')) ? hash.get('result') : '';
  loadAccess();
  HGTarget.ready().then(value => {
    node = value; message('capability-message', capable() ? '只检索本节点已登记结果；按需读取详情时核对明确来源，不采集设备。' : '此节点未提供 results.index-v1；新结果索引不可用，旧任务与原件仍在任务页。', !capable());
    renderControls(); renderCompareEditor(); restoreComparison(query.get('compare')); if (capable()) { loadList(); if (selected) selectResult(selected); }
  }).catch(error => { message('capability-message', errorText(error), true); renderControls(); });
})();
