/* Research metadata and asynchronous assistance. No execution or device actions. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id), API = '/dash/api', KEY = 'hgamey.research.pending';
  const KINDS = {research: '研究目标', direction: '研究方向', request: '协助请求', feedback: '答复记录'};
  const STATES = {
    research: {active: '推进中', idle: '暂空闲', paused: '已暂停', closed: '已关闭'},
    direction: {proposed: '待探索', preparing: '准备中', ready: '可开展', running: '进行中', waiting_device: '等待设备', waiting_human: '等待协助', waiting_material: '等待素材', awaiting_evaluation: '等待评价', verified: '已验证声明', no_gain: '无收益声明', rejected: '已排除', deferred: '已暂缓'},
    request: {open: '开放', closed: '已关闭'}
  };
  const TYPES = {guidance: '技术指导', assistance: '操作或素材协助', evaluation: '主观评价'};
  const FIELDS = {
    research: [
      ['title', '研究标题', 'text', true], ['objective', '目标与判据', 'long', true],
      ['game', '游戏声明', 'json', false, 'null 或 {"game_id":"doom","game_version":"版本"}；不会准备或安装游戏。'],
      ['constraints', '约束（每行一项）', 'lines'], ['device_requirements', '设备要求（每行一项）', 'lines'],
      ['state', '声明状态', 'state'], ['state_reason', '状态理由', 'long'],
      ['budgets', '预算声明', 'json', false, '[{"name":"时间","limit":120,"unit":"minute"}]；只记录预算，不是执行硬限额。'],
      ['coverage', '方向覆盖计划', 'json', false, '[{"key":"resolution","disposition":"planned","reason":null}]；可用 planned / in_progress / not_applicable / blocked / rejected / evidence_available。']
    ],
    direction: [
      ['research_id', '所属研究 ID', 'id', true], ['title', '方向标题', 'text', true], ['hypothesis', '假设与验证目标', 'long', true],
      ['priority', '优先级（声明整数，可空）', 'integer'], ['priority_reason', '排序理由', 'long'],
      ['prerequisites', '前提（每行一项）', 'lines'], ['cost_expectation', '预期成本', 'long'], ['expected_benefit', '预期收益', 'long'],
      ['state', '声明状态', 'state'], ['state_reason', '状态理由', 'long'],
      ['next_experiment', '下一实验计划', 'json', false, 'null 或 {"title":"…","plan":"…","research_revision":1}；仅保存计划，不执行命令。'],
      ['dependencies', '局部依赖与显式解除', 'json', false, '[{"id":"help","kind":"request","target_id":"问题ID","target_revision":1,"blocking":true,"reason":"需要素材","resolution":null}]。解除用 {"decision":"satisfied","reason":"依据…","feedback_ids":["答复ID"]}；也可 waived。目标更新或收到答复不会自动解除。'],
      ['feedback_decisions', '答复处理决定', 'json', false, '[{"feedback_id":"答复ID","decision":"adopt","reason":"采用依据"}]；decision 可 adopt / defer / reject，采用不等于解除依赖。']
    ],
    request: [
      ['research_id', '所属研究 ID', 'id', true], ['type', '请求类型', 'type'], ['title', '问题标题', 'text', true],
      ['need', '需要什么', 'long', true], ['why', '为什么需要', 'long', true], ['resume_hint', '答复后如何继续', 'long'],
      ['context', '固定问题上下文', 'json', true, '{"research_revision":1,"directions":[{"id":"方向ID","revision":1}]}；空 directions 表示研究整体问题。'],
      ['supersedes', '替代的旧请求 ID（可空）', 'id'], ['due_at', '期望答复时间（Unix 秒，可空）', 'number', false, '到期只提示，不自动关闭或解阻。'],
      ['state', '请求状态', 'state'], ['close_reason', '关闭理由', 'long']
    ],
    feedback: [
      ['request_id', '原请求 ID', 'id', true], ['request_revision', '原请求版本', 'integer', true],
      ['text', '答复内容', 'long', true], ['supersedes', '修正的旧答复 ID（可空）', 'id']
    ]
  };
  const EVIDENCE = ['evidence_refs', '证据引用', 'json', false, '[{"kind":"result_source","node_id":"当前节点ID","result_id":"结果ID","source_name":"来源名"}]。也支持 job、result 和纯文字 external；评价请求至少引用一个 result_source。'];
  let node = null, access = null, accessBusy = false, accessError = '', disposed = false;
  let records = [], listKind = 'research', listQuery = '', listCursor = null, listBusy = false, listEpoch = 0, listController = null;
  let selection = null, detail = null, detailEpoch = 0, detailController = null, detailFresh = false;
  let historyCursor = null, historyBusy = false, historyEpoch = 0, eventsAfter = 0, eventsScope = null, eventsBusy = false;
  let draft = null, pending = null, writeBusy = false, lookupBusy = false, conflictLatest = null, relatedBusy = false;
  const validID = value => typeof value === 'string' && /^[A-Za-z0-9._-]{1,64}$/.test(value) && !['.', '..'].includes(value);
  const capable = () => node?.capabilities?.includes('research.state-v1') === true;
  const writable = () => capable() && access?.steer === true && !accessBusy && HGTarget.canWrite();
  const text = value => value == null || value === '' ? '未提供' : String(value);
  const time = value => Number.isFinite(value) ? new Date(value * 1000).toLocaleString() : '未记录';
  const stateText = (kind, value) => STATES[kind]?.[value] || (kind === 'feedback' ? '追加答复' : text(value));
  const objectPath = (kind, id) => `${API}/research-state/${kind}/${encodeURIComponent(id)}`;
  const clone = value => JSON.parse(JSON.stringify(value));
  const keyOf = value => value ? `${value.kind}/${value.id}` : '';
  const failText = error => error.name === 'AbortError' ? '请求超时或已取消；不能据此判定写入结果。' : error.message;
  function el(tag, value, className) { const item = document.createElement(tag); if (value != null) item.textContent = String(value); if (className) item.className = className; return item; }
  function message(id, value, error = false) { $(id).textContent = value; $(id).classList.toggle('task-error', error); }
  function facts(element, values) { element.replaceChildren(...values.flatMap(([label, value]) => [el('dt', label), el('dd', text(value))])); }
  function jsonDetails(title, value) { const item = el('details', null, 'research-json'); item.append(el('summary', title), el('pre', JSON.stringify(value, null, 2), 'command-box')); return item; }
  function objectLink(kind, id, label, revision, target = HGTarget.binding()) {
    const link = el('a', label), query = new URLSearchParams({kind, id});
    if (Number.isInteger(revision)) query.set('revision', revision);
    link.href = HGTarget.url(`/dash/research?${query}`, target); return link;
  }
  async function request(path, {body, signal, target} = {}) {
    const ctl = signal ? null : new AbortController(), timer = ctl ? setTimeout(() => ctl.abort(), 20000) : null;
    try {
      const response = await HGTarget.fetch(path, {credentials: 'same-origin', cache: 'no-store', signal: signal || ctl.signal,
        ...(body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json', 'X-HG': '1'}, body: JSON.stringify(body)})}, target === undefined ? {} : {target});
      let value; try { value = await response.json(); } catch (_) { /* A malformed response does not prove a failed write. */ }
      if (!response.ok) {
        const error = new Error((typeof value?.error === 'string' ? value.error : value?.error?.message) || `请求失败（HTTP ${response.status}）`);
        error.status = response.status; error.code = value?.code || value?.error?.code; error.currentRevision = value?.current_revision; throw error;
      }
      if (!value || typeof value !== 'object') throw new Error('节点未返回可核对的 JSON，写入结果未知。');
      return value;
    } finally { if (timer) clearTimeout(timer); }
  }
  function checkedNode(value, target) {
    if (value.schema_version !== 1 || value.node_id !== target?.node_id) throw new Error('响应节点与固定目标不一致，保留原身份。');
    return value;
  }
  function checkedObject(value, kind, id, target, revision = null) {
    checkedNode(value, target);
    if (value.kind !== kind || value.id !== id || !Number.isInteger(value.revision) || !value.data || (revision != null && value.revision !== revision)) throw new Error('对象或修订身份不匹配。');
    return value;
  }
  function checkedReceipt(value, operation, target) {
    checkedNode(value, target);
    if (value.operation_id !== operation || !KINDS[value.object?.kind] || !validID(value.object?.id) || !Number.isInteger(value.object?.revision)) throw new Error('操作回执身份不匹配。');
    return value;
  }
  function persistPending() {
    try {
      if (pending) { const {kind, id, operation_id, target, base_revision} = pending; sessionStorage.setItem(KEY, JSON.stringify({kind, id, operation_id, target, base_revision})); }
      else sessionStorage.removeItem(KEY);
    } catch (_) { message('write-message', '无法保存未决操作定位，请自行记下原操作 ID 与节点后再离开。', true); }
  }
  function renderControls() {
    const allowed = writable(), locked = writeBusy || !!pending && !pending.refill;
    $('new-object').textContent = `新建${KINDS[$('filter-kind').value]}`;
    $('new-object').disabled = !allowed || !!pending || writeBusy || relatedBusy;
    $('access-toggle').disabled = accessBusy || !access?.steer_configured;
    $('access-toggle').textContent = accessBusy ? '读取权限…' : access?.steer ? '锁定操作' : '解锁操作';
    $('access-toggle').setAttribute('aria-busy', String(accessBusy));
    message('access-message', accessError || (accessBusy ? '正在读取权限…' : !access ? '权限未知，保持只读。' : access.steer ? '可保存研究元数据与答复；不会启动任务。' : access.steer_configured ? '可查看研究；保存与答复需要掌舵权限。' : '未配置掌舵权限，保持只读。'), !!accessError);
    $('save-object').disabled = !allowed || locked || !draft || !!HGTarget.problem(draft?.target || null);
    $('save-object').textContent = writeBusy ? '正在保存或核对…' : draft?.base_revision ? '保存新版本' : '创建记录';
    $('save-object').setAttribute('aria-busy', String(writeBusy));
    $('edit-id').disabled = locked || !!draft?.base_revision || !!pending;
    $('edit-actor').disabled = locked; $('edit-reason').disabled = locked;
    for (const field of $('edit-fields').querySelectorAll('input,textarea,select')) field.disabled = locked || field.dataset.immutable === 'true';
    HGResearchEvaluation.setLocked(locked || !allowed);
    $('editor-close').disabled = writeBusy || !draft;
    $('editor-close').textContent = $('edit-form').hidden ? '展开草稿' : '收起草稿';
    $('refresh-detail').disabled = !selection || !capable();
    $('edit-object').disabled = !allowed || !detailFresh || !!pending || writeBusy;
    for (const id of ['new-direction', 'new-request', 'reply-request', 'close-request']) $(id).disabled = !allowed || !detailFresh || !!pending || writeBusy || relatedBusy;
    $('operation-query').disabled = lookupBusy || !capable();
    $('pending-panel').hidden = !pending;
    if (pending) {
      const issue = HGTarget.problem(pending.target || null);
      $('pending-info').textContent = `${KINDS[pending.kind]} ${pending.id} · 基于 ${pending.base_revision ?? '创建'}\n原操作：${pending.operation_id}\n节点：${pending.target?.node_id || '未绑定'} · ${pending.target?.entry || '未记录入口'}`;
      message('pending-help', issue || (pending.conflict ? '当前版本已变化。草稿与原操作保留；先读最新版本，再显式另开编辑，自行合并。' : pending.refill ? '按原操作重新填写。原 ID、对象与版本不变；内容不同会冲突，应优先查询原操作。' : pending.missing ? '未找到已提交回执，不代表早先请求不会完成。只可显式使用同一原操作接续，不会自动重发。' : '写入结果待核对；保留原节点、对象、版本和操作 ID。先只读查询原操作。'), !!issue);
      $('pending-query').disabled = writeBusy || !capable() || !!issue;
      $('pending-retry').hidden = !pending.body || !pending.missing || pending.refill || pending.conflict;
      $('pending-refill').hidden = !!pending.body || !pending.missing || pending.refill || pending.conflict;
      $('conflict-refresh').hidden = !pending.conflict;
      $('conflict-restart').hidden = !pending.conflict || !conflictLatest;
      for (const id of ['pending-retry', 'pending-refill', 'conflict-restart']) $(id).disabled = !allowed || writeBusy || !!issue;
      $('conflict-refresh').disabled = writeBusy || !!issue;
    }
  }
  async function loadAccess() {
    accessBusy = true; renderControls();
    try { access = await request(`${API}/access`); accessError = ''; $('login-link').hidden = true; }
    catch (error) { access = null; accessError = failText(error); $('login-link').hidden = error.status !== 401; }
    finally { accessBusy = false; if (!disposed) renderControls(); }
  }
  async function setAccess(body, lock = false) {
    if (accessBusy) return; accessBusy = true; renderControls(); $('unlock-submit').disabled = true;
    try { await request(`${API}/steer/${lock ? 'lock' : 'unlock'}`, {body}); $('unlock-form').hidden = true; message('unlock-message', ''); }
    catch (error) { message('unlock-message', failText(error), true); }
    finally { $('steer-password').value = ''; $('unlock-submit').disabled = false; accessBusy = false; await loadAccess(); }
  }
  function changeKind() {
    const kind = $('filter-kind').value;
    $('filter-state').replaceChildren(new Option('全部状态', ''), ...Object.entries(STATES[kind] || {}).map(([value, label]) => new Option(label, value)));
    $('state-filter-label').hidden = kind === 'feedback'; $('request-filter-label').hidden = kind !== 'feedback'; renderControls();
  }
  function renderList() {
    $('objects-heading').textContent = KINDS[listKind]; $('object-count').textContent = `${records.length} 条已载入`;
    $('object-list').replaceChildren(...records.map(item => {
      const li = el('li'), button = el('button', null, 'research-choice'); button.type = 'button'; button.setAttribute('aria-current', String(keyOf(item) === keyOf(selection)));
      button.append(el('strong', item.title || item.id), el('span', `${item.id} · v${item.revision} · ${stateText(item.kind, item.state)}`, 'research-meta'), el('span', `记录于 ${time(item.updated_at)}`, 'research-meta'));
      if (item.kind === 'direction') button.append(el('span', item.metadata_blocked === true ? '存在未解的局部阻断依赖' : item.metadata_blocked === false ? '未解的阻断依赖：无；设备资格另核' : '依赖摘要未提供', 'research-meta'));
      if (item.kind === 'request') button.append(el('span', `${Number.isInteger(item.feedback_count) ? item.feedback_count : '未知'} 条答复；依赖是否解除另记`, 'research-meta'));
      button.addEventListener('click', () => { selectObject(item.kind, item.id); $('detail-heading').focus(); }); li.append(button); return li;
    }));
    $('more-objects').hidden = !listCursor; $('more-objects').disabled = listBusy;
  }
  async function loadList(append = false) {
    if (!capable() || disposed) return;
    const epoch = ++listEpoch, target = HGTarget.binding(); listController?.abort(); listController = new AbortController();
    if (!append) {
      const query = new URLSearchParams(); listKind = $('filter-kind').value;
      for (const [id, key] of [['filter-research', 'research_id'], [listKind === 'feedback' ? 'filter-request' : 'filter-state', listKind === 'feedback' ? 'request_id' : 'state']]) if ($(id).value.trim()) query.set(key, $(id).value.trim());
      listQuery = query.toString(); listCursor = null; records = []; renderList();
    }
    const query = new URLSearchParams(listQuery); query.set('limit', '20'); if (append && listCursor) query.set('cursor', listCursor);
    listBusy = true; $('object-list').setAttribute('aria-busy', 'true'); $('filter-submit').disabled = true; $('more-objects').disabled = true;
    message('list-message', append ? '正在读取同一索引快照的下一页…' : '正在读取记录…');
    try {
      const value = checkedNode(await request(`${API}/research-state/${listKind}?${query}`, {target, signal: listController.signal}), target);
      if (disposed || epoch !== listEpoch) return;
      if (!Array.isArray(value.items)) throw new Error('列表缺少记录数组。');
      records.push(...value.items); listCursor = value.next_cursor || null;
      message('list-message', records.length ? '状态是作者声明；选择记录查看具体版本与依赖。' : '没有匹配记录。可新建研究，或调整筛选。');
    } catch (error) { if (!disposed && epoch === listEpoch) message('list-message', `读取失败：${failText(error)}${records.length ? ' 已载入页保留。' : ''}`, true); }
    finally { if (!disposed && epoch === listEpoch) { listBusy = false; $('filter-submit').disabled = false; $('object-list').setAttribute('aria-busy', 'false'); renderList(); } }
  }
  function contextView(value, atSubmit = false) {
    const box = el('div', null, 'research-derived-item');
    box.append(el('p', `${atSubmit ? '答复提交时' : '当前'}上下文：研究${value?.research === true ? '版本已变化' : value?.research === false ? '版本相同' : '版本差异未提供'}。变化提示不自动否定评价。`));
    for (const item of value?.directions || []) box.append(el('p', `${item.id}：问题 v${item.expected_revision} / ${atSubmit ? '提交时' : '当前'} v${item.current_revision} · ${item.changed ? '已变化' : '相同'}`));
    return box;
  }
  function renderDerived(record) {
    const box = $('derived-view'), value = record.derived || {}; box.replaceChildren();
    if (record.kind === 'research') {
      const entries = Object.entries(value.direction_counts || {});
      box.append(el('p', entries.length ? entries.map(([state, count]) => `${stateText('direction', state)} ${count}`).join(' · ') : '尚无方向状态计数。', 'note'));
    } else if (record.kind === 'direction') {
      box.append(el('p', value.metadata_blocked === true ? '存在未解的阻断依赖，仅影响本方向。其他方向可独立推进。' : value.metadata_blocked === false ? '当前没有未解的阻断依赖；执行与设备资格仍需独立核查。' : '未提供依赖汇总。', 'note'));
      for (const dep of value.unresolved_dependencies || []) {
        const item = el('div', null, 'research-derived-item');
        item.append(objectLink(dep.kind, dep.target_id, `${dep.id} → ${KINDS[dep.kind] || dep.kind} ${dep.target_id} v${dep.target_revision}`, dep.target_revision), el('p', `${dep.blocking ? '阻断' : '不阻断'} · ${text(dep.reason)}`)); box.append(item);
      }
    } else if (record.kind === 'request') {
      box.append(el('p', `答复 ${Number.isInteger(value.feedback_count) ? value.feedback_count : '未知'} 条 · 最近答复 ${time(value.last_feedback_at)}${value.overdue === true ? ' · 已到期（仅提示）' : ''}`, 'note'), contextView(value.context_changed));
      if (!(value.affected_directions || []).length) box.append(el('p', '当前没有方向显式依赖此请求。', 'note'));
      for (const direction of value.affected_directions || []) {
        const item = el('div', null, 'research-derived-item'); item.append(objectLink('direction', direction.id, `${direction.id} v${direction.revision}`, direction.revision), el('p', `依赖 ${direction.dependency_id} · ${direction.blocking ? '阻断声明' : '不阻断'} · ${direction.resolved ? '已显式处理' : '未解除'}`)); box.append(item);
      }
    } else {
      box.append(el('p', `提交时原请求：${stateText('request', value.request_state_at_submit)} · v${text(value.request_revision_at_submit)}`, 'note'), contextView(value.context_changed_at_submit, true));
    }
  }
  function renderEvidence(record) {
    const refs = record.data.evidence_refs || [], items = refs.map(ref => {
      const li = el('li');
      if (ref.kind === 'external') li.append(el('strong', '外部声明（仅文字）'), el('p', `${text(ref.node_id)} · ${text(ref.locator)}`, 'note'));
      else if (ref.node_id === node?.node_id) {
        const link = el('a', ref.kind === 'job' ? `任务 ${ref.job_id}` : `结果 ${ref.result_id}${ref.source_name ? ` · 来源 ${ref.source_name}` : ''}`);
        link.href = HGTarget.url(ref.kind === 'job' ? `/dash/jobs#job=${encodeURIComponent(ref.job_id)}` : `/dash/results#result=${encodeURIComponent(ref.result_id)}`); li.append(link);
      } else li.append(el('span', `${ref.kind} · ${text(ref.node_id)} · ${text(ref.job_id || ref.result_id)}（不自动访问其他节点）`));
      return li;
    });
    if (!items.length) items.push(el('li', '没有声明证据引用。', 'note')); $('evidence-list').replaceChildren(...items);
  }
  function renderDetail(record) {
    $('detail-content').hidden = false; $('object-title').textContent = record.data.title || `答复 ${record.id}`;
    $('object-state').textContent = `${stateText(record.kind, record.data.state)}${record.kind === 'direction' ? '（作者声明）' : ''}`;
    $('object-state').className = 'task-state unknown';
    $('detail-version').textContent = `正在查看 v${record.revision} · 当前 v${record.current_revision} · 事件 #${record.sequence} · ${time(record.updated_at)}`;
    facts($('object-info'), [['对象', `${KINDS[record.kind]} / ${record.id}`], ['研究', record.research_id], ['作者标签', record.audit?.actor_label], ['授权通道', record.audit?.auth_channel], ['修改理由', record.audit?.reason]]);
    $('edit-object').textContent = record.kind === 'feedback' ? '新建修正答复' : '编辑此版本';
    $('new-direction').hidden = record.kind !== 'research'; $('new-request').hidden = !['research', 'direction'].includes(record.kind);
    $('reply-request').hidden = record.kind !== 'request'; $('close-request').hidden = record.kind !== 'request';
    $('close-request').textContent = record.data.state === 'closed' ? '重新开放问题' : '关闭问题';
    $('related-directions').hidden = record.kind !== 'research'; $('related-requests').hidden = !['research', 'direction'].includes(record.kind);
    $('related-feedback').hidden = !['request', 'feedback'].includes(record.kind);
    const body = $('object-body'); body.replaceChildren();
    for (const [key, label, type] of FIELDS[record.kind]) {
      if (['title', 'state'].includes(key)) continue;
      const value = record.data[key]; if (value == null || value === '' || Array.isArray(value) && !value.length) continue;
      if (type === 'json') body.append(jsonDetails(label, value));
      else body.append(el('h4', label), el('p', type === 'lines' ? value.join('\n') : key === 'type' ? TYPES[value] || value : value));
    }
    const ab = !!record.data.evaluation;
    $('evidence-details').open = !ab;
    $('evidence-summary').textContent = ab ? '查看证据引用（会揭示素材标签）' : '查看证据引用';
    $('record-json-details').open = false;
    $('record-json-summary').textContent = ab ? '完整记录与绑定（会揭示素材标签）' : '完整记录、作者来源与引用绑定';
    renderDerived(record); renderEvidence(record); $('object-json').textContent = JSON.stringify(record, null, 2);
    HGResearchEvaluation.renderDetail($('evaluation-detail'), record, {node, target: HGTarget.binding()}); renderControls();
  }
  async function selectObject(kind, id, revision = null) {
    if (!KINDS[kind] || !validID(id) || !capable() || disposed) return;
    selection = {kind, id, revision}; const epoch = ++detailEpoch, target = HGTarget.binding();
    HGResearchEvaluation.unloadAll('记录已切换，媒体已卸下；再次查看需明确加载。'); HGResearchEvaluation.clearDetail();
    detailController?.abort(); detailController = new AbortController(); detailFresh = false;
    ++historyEpoch; historyCursor = null; historyBusy = false; $('history-list').replaceChildren(); $('more-history').hidden = true; $('load-history').disabled = false; message('history-message', '');
    $('detail-content').hidden = true; message('detail-message', '正在读取明确版本…'); renderList(); renderControls();
    try {
      const value = checkedObject(await request(objectPath(kind, id) + (revision ? `?revision=${revision}` : ''), {target, signal: detailController.signal}), kind, id, target, revision);
      if (disposed || epoch !== detailEpoch) return;
      detail = value; detailFresh = true; renderDetail(value);
      message('detail-message', value.revision !== value.current_revision ? '这是历史版本。编辑以此版本为基础，若当前已更新会返回冲突。' : '已读取当前版本。读取不会覆盖打开的草稿。');
    } catch (error) { if (!disposed && epoch === detailEpoch) message('detail-message', failText(error), true); }
    finally { if (!disposed && epoch === detailEpoch) renderControls(); }
  }
  async function loadHistory(append = false) {
    if (!selection || historyBusy || !capable()) return;
    const selected = {...selection}, epoch = ++historyEpoch, target = HGTarget.binding(), query = new URLSearchParams({limit: '20'});
    if (append && historyCursor) query.set('cursor', historyCursor);
    historyBusy = true; $('load-history').disabled = true; $('more-history').disabled = true; message('history-message', '正在读取不可变版本…');
    try {
      const value = checkedNode(await request(`${objectPath(selected.kind, selected.id)}/history?${query}`, {target}), target);
      if (disposed || epoch !== historyEpoch || keyOf(selected) !== keyOf(selection)) return;
      if (!Array.isArray(value.items)) throw new Error('历史列表格式不可读。');
      if (!append) $('history-list').replaceChildren();
      for (const record of value.items) {
        checkedObject(record, selected.kind, selected.id, target);
        const li = el('li'), button = el('button', `查看 v${record.revision}`, 'ghost'); button.type = 'button';
        button.addEventListener('click', () => selectObject(record.kind, record.id, record.revision));
        li.append(button, el('p', `${time(record.updated_at)} · ${text(record.audit?.actor_label)} · ${text(record.audit?.reason)}`, 'note')); $('history-list').append(li);
      }
      historyCursor = value.next_cursor || null; $('more-history').hidden = !historyCursor; message('history-message', '历史作者与理由保留；选择版本不会自动改写当前对象。');
    } catch (error) { if (!disposed && epoch === historyEpoch) message('history-message', failText(error), true); }
    finally { if (!disposed && epoch === historyEpoch) { historyBusy = false; $('load-history').disabled = false; $('more-history').disabled = false; } }
  }
  function defaults(kind) {
    const data = {evidence_refs: []};
    for (const [key, , type] of FIELDS[kind]) data[key] = type === 'lines' || ['budgets', 'coverage', 'dependencies', 'feedback_decisions'].includes(key) ? [] : type === 'state' ? Object.keys(STATES[kind])[0] : type === 'type' ? 'guidance' : null;
    if (kind === 'request') data.context = {research_revision: null, directions: []}; return data;
  }
  function preserveDraft() {
    if (!draft) return;
    const fields = Object.fromEntries([...$('edit-fields').querySelectorAll('[data-field]')].map(field => [field.dataset.field, field.value]));
    $('previous-draft-json').textContent = JSON.stringify({...draft, id: $('edit-id').value, raw_fields: fields, evaluation_draft: HGResearchEvaluation.snapshot(), actor_label: $('edit-actor').value, reason: $('edit-reason').value}, null, 2);
    $('previous-draft').hidden = false;
  }
  function paintDraft() {
    if (!draft) return;
    $('editor').hidden = false; $('edit-form').hidden = false; $('edit-id').value = draft.id; $('edit-actor').value = draft.actor_label || ''; $('edit-reason').value = draft.reason || '';
    $('editor-heading').textContent = `${draft.base_revision ? '编辑' : '新建'}${KINDS[draft.kind]}`;
    $('editor-binding').textContent = `节点 ${draft.target?.node_id || '未绑定'} · 基于 ${draft.base_revision ? `v${draft.base_revision}` : '新对象'}\n操作 ID ${draft.operation_id}`;
    $('editor-help').textContent = draft.kind === 'request' && draft.base_revision ? '问题正文与上下文已固定，只能变更开放/关闭及理由。修正内容请新建问题并填写 supersedes。' : draft.kind === 'feedback' ? '答复绑定原问题版本；不会自动关闭问题、采用意见或解除依赖。修正答复应使用新 ID 与 supersedes。' : '保存只修改研究元数据，不启动任务或释放设备。草稿不写入浏览器存储，请在离开前复制需要保留的内容。';
    $('edit-fields').replaceChildren();
    for (const [key, label, type, required, help] of [...FIELDS[draft.kind], EVIDENCE]) {
      const wrapper = el(type === 'json' ? 'details' : 'label', null, type === 'json' ? 'json-field' : '');
      const field = el(type === 'state' || type === 'type' ? 'select' : ['long', 'lines', 'json'].includes(type) ? 'textarea' : 'input');
      field.id = `field-${key}`; field.dataset.field = key; field.dataset.type = type;
      if (type === 'json') { wrapper.append(el('summary', label)); field.setAttribute('aria-label', label); }
      else wrapper.append(el('span', label));
      if (type === 'state' || type === 'type') for (const [value, title] of Object.entries(type === 'state' ? STATES[draft.kind] : TYPES)) field.append(new Option(title, value));
      else if (type === 'integer' || type === 'number') { field.type = 'number'; field.step = type === 'integer' ? '1' : 'any'; }
      else if (field.tagName === 'INPUT') { field.type = 'text'; field.maxLength = key === 'title' ? 240 : 64; }
      if (field.tagName === 'TEXTAREA') { field.rows = type === 'json' ? 6 : 3; if (type !== 'json') field.maxLength = 16384; }
      field.required = !!required; field.autocomplete = 'off';
      field.value = type === 'json' ? JSON.stringify(draft.data[key] ?? null, null, 2) : type === 'lines' ? (draft.data[key] || []).join('\n') : draft.data[key] ?? '';
      field.dataset.immutable = String(!!draft.base_revision && (draft.kind === 'request' && !['state', 'close_reason'].includes(key) || key === 'research_id'));
      wrapper.append(field); if (help) wrapper.append(el('span', help, 'field-note')); $('edit-fields').append(wrapper);
    }
    const target = draft.target;
    HGResearchEvaluation.mountEditor($('evaluation-editor'), {kind: draft.kind, data: draft.data, baseRevision: draft.base_revision, node, target, resuming: !!pending?.refill,
      readRequest: async (id, revision) => {
        if (!validID(id) || !Number.isInteger(revision) || revision <= 0) throw new Error('请输入有效的原请求 ID 与正整数版本。');
        return checkedObject(await request(`${objectPath('request', id)}?revision=${revision}`, {target}), 'request', id, target, revision);
      }});
    $('edit-reason').required = draft.base_revision != null; renderControls();
  }
  function openDraft(kind, data = {}, record = null) {
    if (!writable() || writeBusy || pending) return;
    preserveDraft(); conflictLatest = null; $('conflict-latest').hidden = true;
    draft = {kind, id: record?.id || crypto.randomUUID(), base_revision: record?.revision || null, operation_id: crypto.randomUUID(), target: HGTarget.binding(), data: {...defaults(kind), ...clone(data)}, actor_label: '', reason: ''};
    message('write-message', ''); $('write-links').replaceChildren(); paintDraft(); $('editor-heading').scrollIntoView({block: 'start'});
  }
  function collectDraft() {
    if (!draft || !validID($('edit-id').value.trim())) throw new Error('对象 ID 需为 1–64 位字母、数字、点、下划线或连字符，不能只有 . 或 ..。');
    const data = clone(draft.data);
    for (const field of $('edit-fields').querySelectorAll('[data-field]')) {
      if (field.dataset.immutable === 'true') continue;
      const {field: key, type} = field.dataset, value = field.value.trim();
      if (type === 'json') { try { data[key] = value ? JSON.parse(value) : null; } catch (_) { throw new Error(`${key} 的 JSON 无效；原内容保留。`); } }
      else if (type === 'lines') data[key] = value ? value.split('\n').map(line => line.trim()).filter(Boolean) : [];
      else if (type === 'integer' || type === 'number') {
        data[key] = value === '' ? null : Number(value);
        if (data[key] != null && (!Number.isFinite(data[key]) || type === 'integer' && !Number.isInteger(data[key]))) throw new Error(`${key} 需为${type === 'integer' ? '整数' : '有限数值'}。`);
      } else data[key] = value || null;
      if (field.required && (data[key] == null || data[key] === '')) throw new Error(`请填写 ${key}。`);
    }
    if (draft.kind === 'request' && data.state === 'open') data.close_reason = null;
    HGResearchEvaluation.collect(data);
    if (new TextEncoder().encode(JSON.stringify(data)).length > 65536) throw new Error('研究记录超过 64 KiB，请减少元数据。');
    const reason = $('edit-reason').value.trim() || null;
    if (draft.base_revision != null && !reason) throw new Error('更新记录需要修改理由。');
    return {schema: 'phonefarm/research-write@1', operation_id: draft.operation_id, base_revision: draft.base_revision, data, actor_label: $('edit-actor').value.trim() || null, reason};
  }
  function receiptLinks(element, receipt, target) {
    element.replaceChildren(objectLink(receipt.object.kind, receipt.object.id, `查看 ${KINDS[receipt.object.kind]} ${receipt.object.id} v${receipt.object.revision}`, receipt.object.revision, target));
  }
  async function completeWrite(receipt, p) {
    checkedReceipt(receipt, p.operation_id, p.target);
    if (receipt.object.kind !== p.kind || receipt.object.id !== p.id) throw new Error('此原操作属于另一对象，不能作为当前草稿保存成功。');
    if (pending !== p || disposed) return;
    receiptLinks($('write-links'), receipt, p.target);
    message('write-message', `已保存 v${receipt.object.revision} · 事件 #${receipt.event_seq} · ${time(receipt.committed_at)}。${p.kind === 'feedback' ? '答复已追加，问题和方向依赖保持原状态。' : '这里只记录研究状态。'}`);
    pending = null; persistPending(); draft = null; conflictLatest = null; $('edit-form').hidden = true; $('pending-panel').hidden = true;
    HGResearchEvaluation.unloadAll('已保存答复或记录，媒体已卸下；再次查看需明确加载。');
    if (keyOf(selection) === `${p.kind}/${p.id}`) await selectObject(p.kind, p.id);
    if (listKind === p.kind) await loadList();
  }
  async function sendPending(p) {
    if (!p?.body || !writable() || writeBusy || HGTarget.problem(p.target || null)) return;
    writeBusy = true; p.refill = false; p.missing = false; persistPending(); renderControls(); message('write-message', '正在保存；原操作 ID 已固定。');
    try { await completeWrite(await request(objectPath(p.kind, p.id), {body: p.body, target: p.target}), p); }
    catch (error) {
      if (disposed || pending !== p) return;
      if (error.code === 'research_revision_conflict') { p.conflict = true; p.current_revision = error.currentRevision; }
      else if (error.status >= 400 && error.status < 500 && error.status !== 409) { pending = null; persistPending(); }
      else if (error.status === 409 && !['research_operation_conflict', 'research_revision_conflict'].includes(error.code)) { pending = null; persistPending(); }
      message('write-message', `${failText(error)}${pending ? ' 原操作与草稿已保留，请先查询或核对版本。' : ' 请求已明确拒绝，草稿仍可修改。'}`, true);
      if ([401, 403].includes(error.status)) { access = null; accessError = '操作权限已失效，请重新解锁。'; }
    } finally { writeBusy = false; if (!disposed) renderControls(); }
  }
  async function submit() {
    if (!writable() || writeBusy || !draft || pending && !pending.refill) return;
    try {
      const body = collectDraft(), id = $('edit-id').value.trim(); HGTarget.assert(draft.target);
      if (pending && (pending.id !== id || pending.operation_id !== body.operation_id || pending.base_revision !== body.base_revision)) throw new Error('未决请求只能使用原对象、版本与操作 ID。');
      if (!pending) pending = {kind: draft.kind, id, operation_id: body.operation_id, base_revision: body.base_revision, target: draft.target};
      pending.body = body; await sendPending(pending);
    } catch (error) { message('write-message', failText(error), true); renderControls(); }
  }
  async function queryPending() {
    const p = pending; if (!p || writeBusy || !capable() || HGTarget.problem(p.target || null)) return;
    writeBusy = true; renderControls(); message('write-message', '正在只读查询原操作…');
    try { await completeWrite(await request(`${API}/research-operations/${encodeURIComponent(p.operation_id)}`, {target: p.target}), p); }
    catch (error) { if (!disposed && pending === p) { p.missing = error.status === 404 && error.code === 'research_operation_not_found'; message('write-message', failText(error) + (p.missing ? ' 不会自动重发。' : ''), true); } }
    finally { writeBusy = false; if (!disposed) renderControls(); }
  }
  async function refillPending() {
    const p = pending; if (!p || p.body || !p.missing || !writable() || writeBusy) return;
    writeBusy = true; renderControls();
    try {
      const record = p.base_revision ? checkedObject(await request(`${objectPath(p.kind, p.id)}?revision=${p.base_revision}`, {target: p.target}), p.kind, p.id, p.target, p.base_revision) : null;
      if (disposed || pending !== p) return;
      p.refill = true; draft = {kind: p.kind, id: p.id, operation_id: p.operation_id, base_revision: p.base_revision, target: p.target, data: record?.data || defaults(p.kind)};
      $('edit-form').hidden = false; paintDraft(); message('write-message', '已恢复原定位。请重填原内容及原作者/理由；不同内容会冲突。');
    } catch (error) { message('write-message', failText(error), true); }
    finally { writeBusy = false; if (!disposed) renderControls(); }
  }
  async function readConflict() {
    const p = pending; if (!p?.conflict || writeBusy) return;
    writeBusy = true; renderControls();
    try {
      const latest = checkedObject(await request(objectPath(p.kind, p.id), {target: p.target}), p.kind, p.id, p.target);
      if (disposed || pending !== p) return; conflictLatest = latest; $('conflict-latest-json').textContent = JSON.stringify(latest, null, 2); $('conflict-latest').hidden = false;
      message('write-message', `已读取当前 v${latest.revision}，原草稿未改。可显式另开草稿，自行合并。`);
    } catch (error) { message('write-message', failText(error), true); }
    finally { writeBusy = false; if (!disposed) renderControls(); }
  }
  function restartConflict() {
    if (!pending?.conflict || !conflictLatest || !writable() || writeBusy) return;
    const old = pending, latest = conflictLatest, oldBody = old.body; preserveDraft();
    if (!draft) { $('previous-draft-json').textContent = JSON.stringify(old, null, 2); $('previous-draft').hidden = false; }
    pending = null; persistPending(); draft = {kind: old.kind, id: old.kind === 'feedback' ? crypto.randomUUID() : old.id, base_revision: old.kind === 'feedback' ? null : latest.revision, operation_id: crypto.randomUUID(), target: old.target,
      data: old.kind === 'request' ? clone(latest.data) : clone(oldBody?.data || latest.data), actor_label: oldBody?.actor_label || '', reason: ''};
    if (old.kind === 'feedback') draft.data.supersedes = latest.id;
    conflictLatest = null; $('conflict-latest').hidden = true; $('edit-form').hidden = false; paintDraft();
    message('write-message', '已显式另开操作。上个草稿和操作 ID 保留在下方；请对照最新版本自行合并后保存。');
  }
  async function relatedDraft(kind) {
    if (!detailFresh || !detail || pending || !writable() || relatedBusy) return;
    const current = detail, epoch = detailEpoch, target = HGTarget.binding();
    if (kind === 'direction') { openDraft(kind, {research_id: current.id}); return; }
    if (kind === 'feedback') { openDraft(kind, {request_id: current.id, request_revision: current.revision}); return; }
    relatedBusy = true; renderControls();
    try {
      const research = current.kind === 'research' ? current : checkedObject(await request(objectPath('research', current.research_id), {target}), 'research', current.research_id, target);
      if (disposed || epoch !== detailEpoch) return;
      openDraft('request', {research_id: research.id, context: {research_revision: research.revision, directions: current.kind === 'direction' ? [{id: current.id, revision: current.revision}] : []}});
    } catch (error) { message('detail-message', failText(error), true); }
    finally { relatedBusy = false; renderControls(); }
  }
  function filterRelated(kind) {
    if (!detailFresh || !detail) return;
    $('filter-kind').value = kind; changeKind(); $('filter-research').value = detail.research_id || detail.id;
    $('filter-request').value = kind === 'feedback' ? detail.kind === 'request' ? detail.id : detail.data.request_id : ''; loadList(); $('objects-heading').focus();
  }
  async function lookupOperation() {
    const id = $('operation-id').value.trim(), target = HGTarget.binding(); if (!validID(id) || lookupBusy || !capable()) { message('operation-message', '请输入有效的原操作 ID。', true); return; }
    if (pending?.operation_id === id) { $('editor').hidden = false; await queryPending(); return; }
    lookupBusy = true; renderControls(); $('operation-links').replaceChildren(); message('operation-message', '正在只读查询…');
    try {
      const value = checkedReceipt(await request(`${API}/research-operations/${encodeURIComponent(id)}`, {target}), id, target);
      if (disposed) return; receiptLinks($('operation-links'), value, target); message('operation-message', `已找到原操作 · 事件 #${value.event_seq} · ${time(value.committed_at)}。没有重发写入。`);
    } catch (error) { if (!disposed) message('operation-message', `${failText(error)}${error.status === 404 ? ' 未找到不证明从未发出；本页不会自动重试。' : ''}`, true); }
    finally { lookupBusy = false; if (!disposed) renderControls(); }
  }
  async function loadEvents() {
    if (eventsBusy || !capable()) return;
    const scope = $('filter-research').value.trim(), target = HGTarget.binding();
    if (eventsScope !== scope) { eventsScope = scope; eventsAfter = 0; $('event-list').replaceChildren(); }
    const query = new URLSearchParams({after: String(eventsAfter), limit: '50'}); if (scope) query.set('research_id', scope);
    eventsBusy = true; $('load-events').disabled = true; $('events-scope').textContent = scope ? `研究 ${scope}` : '此节点全部研究'; message('events-message', '正在读取事件…');
    try {
      const value = checkedNode(await request(`${API}/research-events?${query}`, {target}), target); if (disposed) return;
      if (!Array.isArray(value.items)) throw new Error('事件列表格式不可读。');
      for (const item of value.items) {
        const li = el('li'); li.append(objectLink(item.kind, item.id, `#${item.sequence} · ${KINDS[item.kind]} ${item.id} v${item.revision}`, item.revision), el('p', `${time(item.at)} · 原操作 ${item.operation_id}`, 'note')); $('event-list').append(li);
      }
      eventsAfter = value.next_after; message('events-message', value.items.length ? `已读到事件 #${eventsAfter}${value.has_more ? '，可继续读取。' : '，当前无更多。'}` : '此次没有新增事件。'); $('load-events').textContent = '从原序号继续读取';
    } catch (error) { if (!disposed) message('events-message', failText(error), true); }
    finally { eventsBusy = false; if (!disposed) $('load-events').disabled = false; }
  }
  function capabilityMessage() { message('capability-message', capable() ? '当前后台保存独立研究状态；只读记录不会探测手机。声明状态与任务运行、设备恢复分别核对。' : '此节点未提供 research.state-v1，研究记录不可用；旧目标客户端仍可从概览进入。', !capable()); renderControls(); }
  async function refresh() {
    $('refresh-all').disabled = true;
    try { node = await HGTarget.refresh(); capabilityMessage(); if (capable()) await Promise.all([loadList(), selection ? selectObject(selection.kind, selection.id, selection.revision) : null]); }
    catch (error) { node = null; detailFresh = false; message('capability-message', failText(error), true); }
    finally { $('refresh-all').disabled = false; renderControls(); }
  }
  $('filters').addEventListener('submit', event => { event.preventDefault(); loadList(); });
  $('filter-kind').addEventListener('change', changeKind);
  $('filter-reset').addEventListener('click', () => { $('filters').reset(); changeKind(); loadList(); });
  $('more-objects').addEventListener('click', () => loadList(true));
  $('refresh-detail').addEventListener('click', () => { if (selection) selectObject(selection.kind, selection.id); });
  $('refresh-all').addEventListener('click', () => { refresh(); loadAccess(); });
  $('new-object').addEventListener('click', () => { openDraft($('filter-kind').value, ['direction', 'request'].includes($('filter-kind').value) && $('filter-research').value.trim() ? {research_id: $('filter-research').value.trim()} : {}); $('edit-form').hidden = false; });
  $('edit-object').addEventListener('click', () => {
    if (!detailFresh) return;
    if (detail.kind === 'feedback') openDraft('feedback', {...detail.data, supersedes: detail.id}); else openDraft(detail.kind, detail.data, detail);
    $('edit-form').hidden = false;
  });
  $('new-direction').addEventListener('click', () => relatedDraft('direction'));
  $('new-request').addEventListener('click', () => relatedDraft('request'));
  $('reply-request').addEventListener('click', () => relatedDraft('feedback'));
  $('close-request').addEventListener('click', () => { if (detailFresh && detail?.kind === 'request') openDraft('request', {...detail.data, state: detail.data.state === 'closed' ? 'open' : 'closed', close_reason: null}, detail); });
  $('related-directions').addEventListener('click', () => filterRelated('direction'));
  $('related-requests').addEventListener('click', () => filterRelated('request'));
  $('related-feedback').addEventListener('click', () => filterRelated('feedback'));
  $('load-history').addEventListener('click', () => loadHistory()); $('more-history').addEventListener('click', () => loadHistory(true));
  $('editor-close').addEventListener('click', () => { $('edit-form').hidden = !$('edit-form').hidden; if ($('edit-form').hidden) HGResearchEvaluation.unloadAll('草稿已收起，媒体已卸下。'); renderControls(); });
  $('edit-form').addEventListener('submit', event => { event.preventDefault(); submit(); });
  $('copy-draft').addEventListener('click', async () => { try { const value = {kind: draft.kind, id: $('edit-id').value.trim(), target: draft.target, body: collectDraft()}; await navigator.clipboard.writeText(JSON.stringify(value, null, 2)); message('write-message', '草稿已复制；尚未保存。'); } catch (error) { message('write-message', failText(error), true); } });
  $('pending-query').addEventListener('click', queryPending); $('pending-retry').addEventListener('click', () => { if (pending?.missing) sendPending(pending); });
  $('pending-refill').addEventListener('click', refillPending); $('conflict-refresh').addEventListener('click', readConflict); $('conflict-restart').addEventListener('click', restartConflict);
  $('operation-form').addEventListener('submit', event => { event.preventDefault(); lookupOperation(); }); $('load-events').addEventListener('click', loadEvents);
  $('access-toggle').addEventListener('click', () => { if (access?.steer) setAccess({}, true); else { $('unlock-form').hidden = false; $('steer-password').focus(); } });
  $('unlock-close').addEventListener('click', () => { $('steer-password').value = ''; $('unlock-form').hidden = true; });
  $('unlock-form').addEventListener('submit', event => { event.preventDefault(); setAccess({pw: $('steer-password').value}); });
  for (const id of ['evidence-details', 'record-json-details']) $(id).addEventListener('toggle', () => { if ($(id).open) HGResearchEvaluation.revealDetail(); });
  document.addEventListener('hgamey-target-change', renderControls);
  window.addEventListener('pagehide', () => { disposed = true; listController?.abort(); detailController?.abort(); });
  try {
    const saved = JSON.parse(sessionStorage.getItem(KEY) || 'null');
    if (saved && KINDS[saved.kind] && validID(saved.id) && validID(saved.operation_id) && (saved.base_revision == null || Number.isInteger(saved.base_revision) && saved.base_revision > 0)) pending = {...saved, target: saved.target || null};
  } catch (_) { /* Corrupt browser state establishes no operation identity. */ }
  const query = new URLSearchParams(location.search);
  if (KINDS[query.get('kind')]) $('filter-kind').value = query.get('kind'); changeKind();
  if (query.has('research_id')) $('filter-research').value = query.get('research_id');
  if (pending) { $('editor').hidden = false; $('edit-form').hidden = true; }
  loadAccess();
  HGTarget.ready().then(value => {
    node = value; capabilityMessage(); if (!capable()) return; loadList();
    if (KINDS[query.get('kind')] && validID(query.get('id'))) { const revision = Number(query.get('revision')); selectObject(query.get('kind'), query.get('id'), Number.isInteger(revision) && revision > 0 ? revision : null); }
  }).catch(error => { message('capability-message', failText(error), true); renderControls(); });
})();
