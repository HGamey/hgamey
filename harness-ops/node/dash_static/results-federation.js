/* Selected-node index reads through this hub only. Local writes and target pins are untouched. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id), FIELDS = ['game_id', 'game_version', 'device', 'research_id', 'direction_id', 'outcome', 'job_id'];
  const NODE_ID = /^[0-9a-f]{32}$/;
  const STATUS = {ok: '索引已读取', unavailable: '连接不可用', unauthorized: '源凭据未获授权', unsupported: '源节点能力不支持', identity_mismatch: '源身份不匹配', configuration_changed: '登记配置已变化', disabled: '登记已停用', no_credentials: '未配置源凭据', unknown_node: '登记不存在', busy: '检索槽忙', invalid_cursor: '原分页游标无效'};
  const OUTCOME = {gain: '报告有收益', no_gain: '报告无收益', regression: '报告退步', failed: '失败', not_run: '未运行', unknown: '结论未知'};
  let supported = false, directoryKnown = false, directoryBusy = false, directoryVersion = 0, generation = 0, disposed = false;
  let entries = new Map(), chosen = new Map(), groups = new Map(), activeSignature = '', activeHub = null;
  const flights = new Set();
  const time = value => Number.isFinite(value) ? new Date(value * 1000).toLocaleString() : '未取得';
  const targetKey = target => JSON.stringify([target.name, target.node_id, target.revision]);
  const el = (tag, value, className) => { const node = document.createElement(tag); if (value != null) node.textContent = String(value); if (className) node.className = className; return node; };
  function message(id, value, error = false) { $(id).textContent = value; $(id).classList.toggle('task-error', error); }
  function filters() { const values = {}; for (const key of FIELDS) { const value = $('filters').elements.namedItem(key).value.trim(); if (value) values[key] = value; } return values; }
  function signature() { return JSON.stringify({targets: [...chosen.values()].map(t => [t.name, t.node_id, t.revision]).sort((a,b) => a[0].localeCompare(b[0])), filters: filters()}); }
  function canRead() { return supported && HGTarget.canWrite() && !document.hidden && !disposed; }
  function currentContext() { return activeSignature === signature() && !HGTarget.problem(activeHub); }
  function validEntry(entry) { return NODE_ID.test(entry.node_id || '') && Number.isInteger(entry.revision) && entry.revision > 0; }
  function boundEntry(entry) { return {name: entry.name, node_id: entry.node_id, revision: entry.revision}; }
  async function get(path, target, signal) {
    const response = await HGTarget.fetch(path, {credentials: 'same-origin', cache: 'no-store', signal}, {target});
    let value; try { value = await response.json(); } catch (_) { throw new Error('节点未返回可核对的 JSON。'); }
    if (!response.ok) throw new Error(typeof value.error === 'string' ? value.error : value.error?.message || `读取失败（HTTP ${response.status}）`);
    return value;
  }
  function controls() {
    const allowed = canRead(), busy = [...groups.values()].some(g => g.busy);
    $('federation-directory-refresh').disabled = !allowed || directoryBusy;
    $('federation-directory-refresh').setAttribute('aria-busy', String(directoryBusy));
    $('federation-directory-refresh').textContent = directoryBusy ? '读取登记…' : '读取节点目录';
    $('federation-search').disabled = !allowed || !directoryKnown || !chosen.size || busy;
    $('federation-search').setAttribute('aria-busy', String(busy));
    $('federation-selection').textContent = `已选 ${chosen.size} / 4 个远端；本站写入目标保持不变`;
    for (const input of $('federation-targets').querySelectorAll('input')) {
      const entry = entries.get(input.value);
      input.disabled = !allowed || !directoryKnown || !entry || !validEntry(entry) || !chosen.has(input.value) && chosen.size >= 4;
    }
  }
  function renderDirectory() {
    $('federation-targets').replaceChildren(...[...entries.values()].map(entry => {
      const label = el('label', null, 'federation-target'), box = el('input'), body = el('span');
      box.type = 'checkbox'; box.value = entry.name; box.checked = chosen.has(entry.name);
      body.append(el('strong', entry.name), el('span', entry.url || '入口未提供', 'result-meta'), el('span', `节点 ${entry.node_id || '尚未绑定'} · 配置 ${entry.revision ?? '未知'}`, 'result-meta'));
      const reason = !validEntry(entry) ? '尚无绑定身份，请先在节点目录核对。' : !entry.enabled ? '登记已停用；显式查询会返回停用原因。' : entry.connection?.status === 'ready' ? '上次目录连接可达；结果能力在检索时核对。' : `上次目录状态：${entry.connection?.status || '未核验'}。可显式检索，失败单独显示。`;
      body.append(el('span', reason, 'result-meta'));
      box.addEventListener('change', () => {
        if (!box.checked) chosen.delete(entry.name);
        else if (validEntry(entry) && chosen.size < 4 && ![...chosen.values()].some(t => t.node_id === entry.node_id)) chosen.set(entry.name, boundEntry(entry));
        else box.checked = false;
        changed(); renderDirectory();
      });
      label.append(box, body); return label;
    })); controls();
  }
  async function loadDirectory() {
    if (!canRead() || directoryBusy) return;
    const version = ++directoryVersion, target = HGTarget.binding(); directoryBusy = true; controls(); message('federation-directory-message', '正在只读获取已登记节点…');
    try {
      const value = await get('/dash/api/nodes', target);
      if (disposed || version !== directoryVersion) return;
      if (value.schema_version !== 1 || value.local?.node_id !== target.node_id || !Array.isArray(value.items)) throw new Error('节点目录与当前后台身份不匹配。');
      entries = new Map(value.items.map(item => [item.name, item])); directoryKnown = true;
      let removed = false;
      for (const [name, selected] of chosen) { const entry = entries.get(name); if (!entry || !validEntry(entry) || targetKey(selected) !== targetKey(entry)) { chosen.delete(name); removed = true; } }
      if (removed) changed();
      message('federation-directory-message', removed ? '部分登记身份或版本已变化，已撤下原选择；请重新核对后明确选择。上次检索结果仍标注原条件。' : entries.size ? '仅取得登记元数据。选择不会发起远端请求，原凭据不回显。' : '尚无远端登记。可先到节点目录登记并核对身份。', removed);
      renderDirectory();
    } catch (error) { if (!disposed && version === directoryVersion) { directoryKnown = false; message('federation-directory-message', `目录读取失败：${error.message}。上次登记显示保留，新的节点选择暂不可用。`, true); } }
    finally { if (!disposed && version === directoryVersion) { directoryBusy = false; controls(); } }
  }
  function stopReads(reason) {
    generation++;
    for (const controller of flights) controller.abort(); flights.clear();
    for (const group of groups.values()) if (group.busy) { group.busy = false; group.status = 'unknown'; group.error = reason; group.failedCursor = group.requestedCursor; }
  }
  function changed() {
    stopReads('选择或筛选已改变，未确认此前在途读取；没有自动重新检索。');
    if (groups.size) message('federation-message', '选择或筛选已修改。下方保留上次检索及其原条件；请显式检索所选远端，旧页不能混入新条件。', true);
    renderGroups(); controls();
  }
  function detailLink(item, source) {
    try {
      const url = new URL(item.detail_url), endpoint = new URL(source.url);
      if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.origin !== endpoint.origin || !url.pathname.endsWith('/dash/results') ||
          url.searchParams.getAll('expected_node_id').length !== 1 || url.searchParams.get('expected_node_id') !== source.node_id || new URLSearchParams(url.hash.slice(1)).get('result') !== item.result_id) throw new Error('unbound link');
      const a = el('a', '到源节点查看详情与原件'); a.href = url.href; a.target = '_blank'; a.rel = 'noopener noreferrer'; return a;
    } catch (_) { return el('span', '源详情链接未确认，未创建跳转。', 'note'); }
  }
  function renderGroups() {
    const matching = currentContext();
    $('federation-results').replaceChildren(...[...groups.values()].map(group => {
      const section = el('section', null, 'federation-group'), header = el('div', null, 'section-heading'), state = el('span', group.busy ? '正在读取…' : STATUS[group.status] || '状态未知', `task-state ${group.status === 'ok' && !group.busy ? '' : 'unknown'}`);
      section.dataset.node = group.target.node_id; section.setAttribute('aria-busy', String(group.busy));
      header.append(el('h3', group.target.name), state); section.append(header);
      section.append(el('p', `${group.source.url || '入口未知'}\n节点 ${group.target.node_id} · 配置 ${group.target.revision}`, 'federation-source'));
      section.append(el('p', `本组条件：${Object.entries(group.filters).map(([key,value]) => `${key}=${value}`).join(' · ') || '全部已登记结果'} · 每页最多 20 条`, 'note'));
      section.append(el('p', `最近请求：${time(group.attemptedAt)} · 最后成功读索引：${time(group.readAt)}`, 'read-time'));
      if (group.error) section.append(el('p', `${group.error}${group.hasPage ? ' 上次结果、成功时间与分页位置保留。' : ' 尚未取得此节点的结果，不表示零条。'}`, 'note task-error'));
      if (!matching) section.append(el('p', '这是上次选择与筛选下的结果；当前条件已变化，续页暂停。', 'note'));
      if (group.hasPage) {
        section.append(el('p', `已载入 ${group.items.size} 条${group.status !== 'ok' ? '（上次结果）' : ''}；不表示全局总数。`, 'note'));
        const list = el('ul', null, 'federation-items');
        for (const item of group.items.values()) {
          const li = el('li'); li.append(el('strong', item.title || item.result_id), el('span', `${item.result_id} · ${OUTCOME[item.outcome] || '结论未知'}`, 'result-meta'),
            el('span', [item.labels?.game_id, item.labels?.game_version, item.labels?.device].filter(Boolean).join(' · ') || '未提供游戏 / 设备标签', 'result-meta'), group.navigationAvailable ? detailLink(item, group.source) : el('span', '本次来源入口未确认；请先到节点目录核对。', 'note')); list.append(li);
        }
        if (!group.items.size) list.append(el('li', '此节点在本组筛选下返回空页。', 'note'));
        section.append(list);
      }
      const actions = el('div', null, 'result-actions'), retry = el('button', group.status !== 'ok' ? group.failedCursor ? '重试原下一页' : '重试此节点' : '重新检索此节点', 'ghost'); retry.type = 'button'; retry.disabled = !canRead() || !matching || group.busy;
      retry.addEventListener('click', () => queryGroups([group], group.status === 'ok' ? null : group.failedCursor)); actions.append(retry);
      if (group.cursor) { const more = el('button', '加载此节点下一页', 'ghost'); more.type = 'button'; more.disabled = !canRead() || !matching || group.busy || group.status !== 'ok'; more.addEventListener('click', () => queryGroups([group], group.cursor)); actions.append(more); }
      section.append(actions); return section;
    }));
  }
  function applyPage(group, page, cursor) {
    if (!page || targetKey(page.source || {}) !== targetKey(group.target) || (page.requested_cursor ?? null) !== cursor) throw new Error('返回来源身份、配置版本或游标不匹配，旧页保留。');
    group.attemptedAt = page.attempted_at ?? group.attemptedAt;
    group.navigationAvailable = typeof page.source.url === 'string' && !!page.source.url;
    group.status = page.status; group.error = page.error?.message || null;
    if (page.status !== 'ok') { group.failedCursor = cursor; return; }
    if (!Array.isArray(page.items)) throw new Error('成功分项未返回结果数组。');
    if (cursor === null) group.items = new Map();
    for (const item of page.items) group.items.set(item.result_id, item);
    group.source = page.source; group.hasPage = true; group.readAt = page.read_at; group.cursor = page.next_cursor || null; group.failedCursor = null;
  }
  async function queryGroups(selectedGroups, cursor = null) {
    if (!canRead() || !currentContext() || !selectedGroups.length || selectedGroups.some(group => group.busy)) return;
    if (cursor !== null && selectedGroups.length !== 1) return;
    const version = generation, target = activeHub, controller = new AbortController(), query = new URLSearchParams(selectedGroups[0].filters);
    query.set('targets', JSON.stringify(selectedGroups.map(group => group.target))); query.set('limit', '20'); if (cursor !== null) query.set('cursor', cursor);
    for (const group of selectedGroups) { group.busy = true; group.requestedCursor = cursor; group.attemptedAt = Date.now() / 1000; }
    flights.add(controller); const timer = setTimeout(() => controller.abort(), 12000); renderGroups(); controls();
    try {
      const value = await get(`/dash/api/result-search?${query}`, target, controller.signal);
      if (disposed || version !== generation) return;
      if (value.schema_version !== 1 || value.node_id !== target.node_id || !Array.isArray(value.nodes)) throw new Error('跨节点响应与当前后台身份不匹配。');
      for (const group of selectedGroups) {
        try { applyPage(group, value.nodes.find(item => item.source?.name === group.target.name), cursor); }
        catch (error) { group.status = 'unknown'; group.error = error.message; group.failedCursor = cursor; }
      }
      const failed = [...groups.values()].filter(group => group.status !== 'ok').length;
      message('federation-message', failed ? `${failed} 个节点分项尚未取得本次结果；已成功的节点独立保留。可按原条件显式重试失败项。` : '所选节点的本次页已读取；分页分别推进，不做跨节点排名、时钟排序或总数推断。', !!failed);
    } catch (error) {
      if (disposed || version !== generation) return;
      for (const group of selectedGroups) { group.status = 'unknown'; group.error = error.name === 'AbortError' ? '本次读取超时或中止；没有自动重试。' : error.message; group.failedCursor = cursor; }
      message('federation-message', '本次读取未确认。各节点上次结果与分页位置保留；没有自动回到第一页。', true);
    } finally {
      clearTimeout(timer); flights.delete(controller);
      if (!disposed && version === generation) { for (const group of selectedGroups) group.busy = false; renderGroups(); controls(); }
    }
  }
  function searchSelected() {
    if (!canRead() || !directoryKnown || !chosen.size || chosen.size > 4 || [...groups.values()].some(g => g.busy)) return;
    stopReads('已开始新的显式检索。');
    const nextSignature = signature(), same = nextSignature === activeSignature && !HGTarget.problem(activeHub), previous = groups;
    activeSignature = nextSignature; activeHub = HGTarget.binding(); const currentFilters = filters(); groups = new Map();
    for (const target of chosen.values()) {
      const old = same ? previous.get(targetKey(target)) : null;
      groups.set(targetKey(target), old || {target: {...target}, source: {...target, url: entries.get(target.name)?.url}, filters: {...currentFilters}, items: new Map(), hasPage: false, cursor: null, readAt: null, attemptedAt: null, status: 'unknown', error: null, busy: false, failedCursor: null, navigationAvailable: false});
    }
    queryGroups([...groups.values()]);
  }
  $('federation-panel').addEventListener('toggle', () => { if ($('federation-panel').open && !directoryKnown && !directoryBusy) loadDirectory(); });
  $('federation-directory-refresh').addEventListener('click', loadDirectory);
  $('federation-search').addEventListener('click', searchSelected);
  $('filters').addEventListener('input', changed);
  $('filters').addEventListener('change', changed);
  $('filter-reset').addEventListener('click', changed);
  document.addEventListener('hgamey-target-change', () => { if (HGTarget.problem()) stopReads('当前后台身份未确认，检索已停止。'); controls(); renderGroups(); });
  document.addEventListener('visibilitychange', () => { if (document.hidden) { stopReads('页面已隐藏，本次读取未确认；回到页面后请显式重试。'); renderGroups(); } controls(); });
  window.addEventListener('pagehide', () => { disposed = true; stopReads('页面已离开。'); directoryVersion++; });
  HGTarget.ready().then(node => {
    supported = node.capabilities?.includes('results.federation-v1') === true;
    message('federation-capability', supported ? '远端检索只读取索引；不会迁移结果或原件。源站详情需要浏览器可达并单独登录。' : '此后台未提供 results.federation-v1，远端检索不可用；本地结果功能保持原有范围。', !supported);
    controls(); if ($('federation-panel').open) loadDirectory();
  }).catch(error => { message('federation-capability', error.message, true); controls(); });
})();
