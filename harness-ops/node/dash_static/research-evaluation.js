/* Explicit A/B media and subjective feedback; the existing research write owns persistence. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id), SLOTS = ['A', 'B'];
  const PREF = {'': '未作偏好判断', A: '偏好 A', B: '偏好 B', no_preference: '无偏好', unable_to_judge: '无法判断'};
  const DIFF = {'': '未判断差异', none: '没有可见差异', small: '小', moderate: '中等', large: '大', unknown: '无法判断差异'};
  const DISPLAY = {decoded: '本地画面已解码', failed: '读取或解码失败', not_loaded: '未加载', unknown: '尚未确认画面'};
  const views = new Set(), revealedRequests = new Set();
  let detailView = null, editor = null, locked = true;
  const supported = node => node?.capabilities?.includes('research.evaluation-v1') === true;
  const sameRequest = (view, record) => !!view && !!record && requestKey(view.record) === requestKey(record);
  const el = (tag, value, className) => { const node = document.createElement(tag); if (value != null) node.textContent = String(value); if (className) node.className = className; return node; };
  const note = value => el('p', value, 'note');
  const copy = value => JSON.parse(JSON.stringify(value));
  function button(label, id) { const item = el('button', label, 'ghost'); item.type = 'button'; if (id) item.id = id; return item; }
  function label(title, input) { const box = el('label'); box.append(el('span', title), input); return box; }
  function select(options, id) { const item = el('select'); item.id = id; for (const [value, title] of Object.entries(options)) item.append(new Option(title, value)); return item; }
  function checkbox(title, id) { const input = el('input'); input.type = 'checkbox'; input.id = id; const box = el('label', null, 'evaluation-check'); box.append(input, el('span', title)); return {box, input}; }
  function showStatus(node, value, failed = false) { node.textContent = value; node.classList.toggle('task-error', failed); }
  function isAB(record) { return record?.kind === 'request' && record.data?.evaluation?.schema === 'phonefarm/ab-evaluation@1'; }
  function requestKey(record) { return `${record.node_id}/${record.id}/${record.revision}`; }
  function markRevealed(view) {
    const key = requestKey(view.record); revealedRequests.add(key);
    for (const item of views) if (requestKey(item.record) === key) item.revealed = true;
    notify();
  }
  function mediaURL(view, slot) {
    HGTarget.assert(view.target);
    if (!supported(view.node) || !HGTarget.canWrite()) throw new Error('节点未提供 A/B 媒体或显式目标能力，不能加载。');
    return HGTarget.url(`/dash/api/research-state/request/${encodeURIComponent(view.record.id)}/media/${slot}?revision=${view.record.revision}`, view.target);
  }
  function notify() { editor?.refresh?.(); }
  function unloadView(view, reason) {
    for (const item of Object.values(view.items)) {
      ++item.epoch; item.diagnostic?.abort(); item.busy = false;
      if (item.media.tagName === 'VIDEO') item.media.pause();
      item.media.removeAttribute('src');
      if (item.media.tagName === 'VIDEO') item.media.load();
      item.display = 'not_loaded'; item.media.hidden = true; item.load.disabled = !supported(view.node) || !!HGTarget.problem(view.target);
      showStatus(item.status, reason || '未加载；点击后才读取原件。'); item.size.textContent = '原尺寸尚未读取。';
    }
    notify();
  }
  function disposeView(view) {
    if (!view) return; unloadView(view); for (const item of Object.values(view.items)) item.resize?.disconnect(); views.delete(view);
    if (detailView === view) detailView = null;
  }
  function unloadAll(reason = '已卸下媒体；再次查看需明确加载。') { for (const view of views) unloadView(view, reason); }
  function dimensions(item) {
    const video = item.media.tagName === 'VIDEO', width = video ? item.media.videoWidth : item.media.naturalWidth, height = video ? item.media.videoHeight : item.media.naturalHeight;
    if (!width || !height) return '原尺寸尚未读取。';
    const scale = item.media.clientWidth > 0 ? ` · 显示缩放约 ${Math.round(item.media.clientWidth / width * 100)}%` : '';
    const duration = video ? ` · 时长 ${Number.isFinite(item.media.duration) ? `${item.media.duration.toFixed(1)} 秒` : '未知'}` : '';
    return `${width} × ${height}${duration}${scale}；未配准或同步。`;
  }
  async function explainFailure(view, item, epoch) {
    const generic = item.media.tagName === 'VIDEO' ? '浏览器未能解码视频画面，可能格式、编解码器或来源不可用。' : '原图未能完整读取或解码。';
    let reason = generic;
    // Only after an explicit load failed, read one byte for the server's structured error.
    // Success bodies are cancelled immediately; no media Blob or whole-file fetch is made.
    try {
      item.diagnostic = new AbortController();
      const response = await HGTarget.fetch(mediaURL(view, item.slot), {credentials: 'same-origin', cache: 'no-store', headers: {Range: 'bytes=0-0'}, signal: item.diagnostic.signal}, {target: view.target});
      if (!response.ok) { let body; try { body = await response.json(); } catch (_) {} reason = typeof body?.error === 'string' ? body.error : body?.error?.message || `原件读取失败（HTTP ${response.status}）。`; }
      else await response.body?.cancel();
    } catch (error) { if (error.name !== 'AbortError') reason = `${generic} ${error.message}`; }
    if (item.epoch !== epoch || !views.has(view)) return;
    item.busy = false; item.display = 'failed'; item.load.disabled = false; showStatus(item.status, reason, true); notify();
  }
  function loadSlot(view, item) {
    if (document.hidden || item.busy) return;
    let url; try { url = mediaURL(view, item.slot); } catch (error) { showStatus(item.status, error.message, true); return; }
    const epoch = ++item.epoch, video = item.media.tagName === 'VIDEO'; item.diagnostic?.abort();
    item.display = 'unknown'; item.busy = !video; item.load.disabled = item.busy; item.load.textContent = video ? `重新加载 ${item.slot} 视频` : `重新加载 ${item.slot} 原图`;
    item.media.hidden = false; item.size.textContent = '正在读取；还不能声明画面显示成功。';
    showStatus(item.status, video ? '原视频已指定，请手动播放；没有自动播放，也不据此声明已看完整段。' : '正在读取原图…');
    const decoded = () => {
      if (item.epoch !== epoch || !views.has(view) || document.hidden) return;
      const width = video ? item.media.videoWidth : item.media.naturalWidth, height = video ? item.media.videoHeight : item.media.naturalHeight;
      if (!width || !height || !video && (width > 8192 || height > 8192 || width * height > 16777216)) { explainFailure(view, item, epoch); return; }
      item.busy = false; item.load.disabled = false; item.display = 'decoded'; item.size.textContent = dimensions(item);
      showStatus(item.status, video ? '已解码视频画面；是否看过完整内容需由你明确声明。' : '原图已完整解码；请自行判断内容是否可比。'); notify();
    };
    item.media.onerror = () => { if (item.epoch === epoch) { item.display = 'failed'; notify(); explainFailure(view, item, epoch); } };
    if (video) {
      item.media.onloadeddata = decoded;
      item.media.onloadedmetadata = () => { if (item.epoch === epoch) { item.size.textContent = dimensions(item); if (item.display !== 'decoded') showStatus(item.status, '已读到容器信息，尚未确认视频画面；请手动播放。'); } };
    } else item.media.onload = async () => { try { await item.media.decode(); decoded(); } catch (_) { if (item.epoch === epoch) explainFailure(view, item, epoch); } };
    item.media.src = url; if (video) item.media.load(); notify();
  }
  function createView(container, record, context) {
    const view = {record, node: context.node, target: context.target, items: {}, revealed: revealedRequests.has(requestKey(record)), element: el('section', null, 'evaluation-view research-section')};
    const evaluation = record.data.evaluation, grid = el('div', null, 'evaluation-grid');
    view.element.dataset.evaluationRequest = requestKey(record);
    view.element.append(el('h3', '固定 A/B 对照'), note(`请求 ${record.id} · v${record.revision} · A 为左侧／窄屏上方，B 为右侧／下方。排列已保存，读取时不会重新随机。`),
      note(evaluation.media_kind === 'video' ? 'MP4 / WebM，每份上限 128 MiB。完整原视频独立手动播放，不裁片、不同步。' : 'PNG / JPEG，每份上限 16 MiB、单边 8192、总像素 16,777,216。原图等比显示，不配准。'),
      note('此处只隐藏界面中的素材标签，不是保密盲化。问题说明也可能透露身份；显示成功不代表同状态或评价有效。'));
    if (!supported(context.node)) view.element.append(note('此节点未声明 research.evaluation-v1，保留绑定资料，媒体加载不可用。'));
    for (const slot of SLOTS) {
      const card = el('section', null, 'evaluation-slot'), media = el(evaluation.media_kind === 'video' ? 'video' : 'img'), load = button(`加载 ${slot} ${evaluation.media_kind === 'video' ? '视频' : '原图'}`);
      media.hidden = true; media.className = 'evaluation-media'; media.dataset.slot = slot;
      if (media.tagName === 'VIDEO') { media.preload = 'none'; media.autoplay = false; media.controls = true; media.muted = true; media.playsInline = true; }
      else media.alt = `固定槽位 ${slot} 原图`;
      const item = {slot, media, load, status: note('未加载；点击后才读取原件。'), size: note('原尺寸尚未读取。'), epoch: 0, display: 'not_loaded', busy: false};
      item.status.setAttribute('role', 'status'); load.dataset.slot = slot; load.disabled = !supported(context.node) || !!HGTarget.problem(context.target);
      load.addEventListener('click', () => loadSlot(view, item));
      item.resize = new ResizeObserver(() => { if (item.display === 'decoded') item.size.textContent = dimensions(item); }); item.resize.observe(media);
      card.append(el('h4', slot), load, item.status, media, item.size); grid.append(card); view.items[slot] = item;
    }
    const identity = el('details', null, 'evaluation-identity'), original = {};
    for (const slot of SLOTS) original[slot] = {index: evaluation.slots[slot], reference: record.data.evidence_refs[evaluation.slots[slot]], binding: record.evidence_bindings?.[evaluation.slots[slot]] || null};
    identity.append(el('summary', '查看素材身份（会揭示标签）'), el('pre', JSON.stringify(original, null, 2), 'command-box'));
    identity.addEventListener('toggle', () => { if (identity.open) markRevealed(view); });
    view.element.append(grid, identity); container.append(view.element); views.add(view); return view;
  }
  function clearDetail() { disposeView(detailView); $('evaluation-detail').replaceChildren(); notify(); }
  function renderDetail(container, record, context) {
    clearDetail();
    if (isAB(record)) detailView = createView(container, record, context);
    else if (record.kind === 'feedback' && record.data.evaluation) {
      const value = record.data.evaluation, panel = el('section', null, 'research-section'); panel.append(el('h3', 'A/B 主观答复'));
      const entries = [['偏好', PREF[value.preference ?? ''] || '未知'], ['可见差异', DIFF[value.difference ?? ''] || '未知'],
        ...SLOTS.map(slot => [`${slot} 已查看声明`, value.reviewed?.[slot] === true ? '是' : value.reviewed?.[slot] === false ? '否' : '未声明']),
        ...SLOTS.map(slot => [`${slot} 客户端报告`, DISPLAY[value.client_display?.[slot]] || '未知']), ['主动展开身份', value.identity_revealed === true ? '是' : value.identity_revealed === false ? '否（仅本客户端声明）' : '未声明']];
      const facts = el('dl', null, 'facts'); for (const [name, text] of entries) facts.append(el('dt', name), el('dd', text));
      panel.append(facts, note('这是原问题版本上的主观声明。差异程度不是质量分数、胜率或自动采用决定。'));
      if (record.derived?.evaluation_binding) { const binding = el('details'); binding.append(el('summary', '查看提交时素材绑定（会揭示标签）'), el('pre', JSON.stringify(record.derived.evaluation_binding, null, 2), 'command-box')); panel.append(binding); }
      container.append(panel);
    }
    notify();
  }
  function revealDetail() { if (detailView) markRevealed(detailView); }
  function mountEditor(container, context) {
    if (editor?.ownedView) disposeView(editor.ownedView); editor?.cleanup?.(); editor = null; container.replaceChildren();
    if (!['request', 'feedback'].includes(context.kind)) return;
    const state = {context, container, refresh: () => {}, collect: () => {}, ownedView: null}; editor = state;
    if (context.kind === 'request') mountRequest(state); else mountFeedback(state);
    state.refresh();
  }
  function mountRequest(state) {
    const {context, container} = state, old = context.data.evaluation;
    if (context.baseRevision) { if (old) container.append(note('A/B 类型、原件索引与固定排列已保存，状态更新不会修改。')); return; }
    const panel = el('section', null, 'evaluation-editor'), enable = checkbox('附加固定图片／视频 A/B 对照', 'evaluation-enable'); enable.input.checked = !!old;
    const kind = select({image: '两张图片', video: '两份完整视频'}, 'evaluation-media-kind'), first = select({}, 'evaluation-source-one'), second = select({}, 'evaluation-source-two');
    kind.value = old?.media_kind || 'image';
    const controls = el('div', null, 'evaluation-create-controls'), choices = el('div', null, 'research-form-grid'), actions = el('div', null, 'research-actions');
    const random = button('随机排列一次', 'evaluation-random'), fixed = button('固定：来源 1 为 A', 'evaluation-fixed'), reverse = button('固定：来源 2 为 A', 'evaluation-reverse'), mapping = note('尚未固定排列。'), status = note('');
    choices.append(label('来源 1（证据引用索引）', first), label('来源 2（证据引用索引）', second)); actions.append(random, fixed, reverse);
    controls.append(label('媒体类型', kind), choices, actions, mapping, note('先在上方「证据引用」填入至少两个同节点 result_source。创建仅保存注册绑定，不证明原件当前可显示。请使用中性标题和问题措辞。'), status);
    panel.append(el('h3', 'A/B 评价设置'), enable.box, controls); container.append(panel);
    let refsKey = '', pair = old ? copy(old.slots) : null, randomUsed = !!old, initial = !!old, pairRefs = null;
    function refs() { try { const value = JSON.parse($('field-evidence_refs').value); return Array.isArray(value) ? value : []; } catch (_) { return []; } }
    function updateSources() {
      const values = refs(), encoded = JSON.stringify(values); if (refsKey === encoded) return;
      const previous = [first.value, second.value]; refsKey = encoded;
      const items = values.map((ref, index) => ({ref, index})).filter(item => item.ref?.kind === 'result_source' && item.ref.node_id === context.target?.node_id);
      for (const [position, field] of [first, second].entries()) {
        field.replaceChildren(new Option('选择已登记来源', ''), ...items.map(({ref, index}) => new Option(`${index} · ${ref.result_id} / ${ref.source_name}`, String(index))));
        const selected = initial && old ? String(position ? old.slots.B : old.slots.A) : previous[position];
        if ([...field.options].some(option => option.value === selected)) field.value = selected;
      }
      if (initial && old) pairRefs = encoded; else if (pairRefs !== encoded) pair = null;
      initial = false;
    }
    function choose(reverseOrder, randomized = false) {
      if (locked || !supported(context.node)) return;
      if (first.value === '' || second.value === '' || first.value === second.value) { showStatus(status, '请选择两个不同的证据引用索引；两项仍可注册同件原件。', true); return; }
      const a = Number(first.value), b = Number(second.value); pair = reverseOrder ? {A: b, B: a} : {A: a, B: b}; pairRefs = JSON.stringify(refs());
      if (randomized) randomUsed = true; showStatus(status, randomized ? '随机已执行一次；之后的提交和原 ID 接续使用此固定排列。' : '固定排列已明确选择。'); state.refresh();
    }
    random.addEventListener('click', () => { if (!randomUsed && !context.resuming) choose((crypto.getRandomValues(new Uint8Array(1))[0] & 1) === 1, true); });
    fixed.addEventListener('click', () => choose(false)); reverse.addEventListener('click', () => choose(true));
    for (const field of [first, second]) field.addEventListener('change', () => { pair = null; state.refresh(); });
    const onRefs = () => { updateSources(); state.refresh(); }, onType = () => state.refresh();
    $('field-evidence_refs').addEventListener('input', onRefs); $('field-type').addEventListener('change', onType); enable.input.addEventListener('change', onType);
    state.cleanup = () => { $('field-evidence_refs')?.removeEventListener('input', onRefs); $('field-type')?.removeEventListener('change', onType); };
    state.refresh = () => {
      updateSources(); const eligible = $('field-type')?.value === 'evaluation', allowed = supported(context.node);
      panel.hidden = !eligible && !enable.input.checked; controls.hidden = !enable.input.checked;
      enable.input.disabled = locked || !allowed || !eligible;
      for (const control of [kind, first, second, fixed, reverse]) control.disabled = locked || !allowed || !eligible || !enable.input.checked;
      random.disabled = fixed.disabled || randomUsed || context.resuming;
      mapping.textContent = pair ? `固定排列：A → 证据 ${pair.A}，B → 证据 ${pair.B}。` : '尚未固定排列；不会在提交时替你随机。';
      if (!allowed) showStatus(status, '此节点未提供 research.evaluation-v1；可使用原有文字评价。', true);
      else if (context.resuming) showStatus(status, '原操作重填不能重新随机。请从原草稿恢复相同来源和固定排列。');
      else if (!eligible && enable.input.checked) showStatus(status, 'A/B 只适用于评价请求；请选择评价类型或取消附加。', true);
    };
    state.collect = data => {
      if (!enable.input.checked) { delete data.evaluation; return; }
      if (!supported(context.node)) throw new Error('节点未提供 A/B 评价能力。');
      if (data.type !== 'evaluation') throw new Error('A/B 对照只能用于评价请求。');
      if (!pair || pairRefs !== JSON.stringify(data.evidence_refs)) throw new Error('请显式固定两个原件索引的 A/B 排列。');
      data.evaluation = {schema: 'phonefarm/ab-evaluation@1', media_kind: kind.value, slots: copy(pair)};
    };
    state.snapshot = () => ({enabled: enable.input.checked, media_kind: kind.value, slots: pair && copy(pair), randomized_once: randomUsed});
  }
  function mountFeedback(state) {
    const {context, container} = state, old = context.data.evaluation, panel = el('section', null, 'evaluation-editor');
    const load = button('读取原问题的 A/B 设置', 'evaluation-read-request'), heading = el('h3', 'A/B 主观答复'), status = note('如原问题有固定 A/B，可明确读取该版本后填写。文字评论仍使用上方答复内容。');
    const enable = checkbox('附加结构化 A/B 评价', 'evaluation-enable'), fields = el('div', null, 'evaluation-feedback-fields'), media = el('div'), source = note(''), goto = button('查看固定 A/B 素材', 'evaluation-view-media');
    const preference = select(PREF, 'evaluation-preference'), difference = select(DIFF, 'evaluation-difference'), checked = {};
    preference.value = old?.preference || ''; difference.value = old?.difference || '';
    enable.input.checked = !!old;
    fields.append(label('偏好', preference), label('可见差异程度（不是质量分数）', difference));
    for (const slot of SLOTS) { checked[slot] = checkbox(`我已查看 ${slot}（视频由我自行判断观看范围）`, `evaluation-reviewed-${slot}`); fields.append(checked[slot].box); }
    const display = note(''), identity = note(''); fields.append(display, identity, note('有偏好或无偏好要求两边在本页显示成功，并由你勾选已查看。失败仍可选择无法判断，保留评论；不会自动关闭请求或处理依赖。'));
    panel.append(heading, load, status, enable.box, fields, source, goto, media); container.append(panel);
    let original = null, loading = false, epoch = 0;
    const inputsMatch = record => record && $('field-request_id')?.value.trim() === record.id && Number($('field-request_revision')?.value) === record.revision;
    const activeView = () => inputsMatch(original) ? sameRequest(detailView, original) ? detailView : sameRequest(state.ownedView, original) ? state.ownedView : null : null;
    function useRequest(record) { original = record; if (state.ownedView) { disposeView(state.ownedView); state.ownedView = null; } media.replaceChildren();
      if (isAB(record) && !sameRequest(detailView, record)) state.ownedView = createView(media, record, context);
      showStatus(status, isAB(record) ? `已固定请求 ${record.id} v${record.revision}。${record.data.state === 'closed' ? '问题已关闭，仍可追加迟到答复。' : ''}` : '此版本没有 A/B 设置，可继续提交文字答复。'); state.refresh();
    }
    load.addEventListener('click', async () => {
      if (loading || locked) return; const id = $('field-request_id').value.trim(), revision = Number($('field-request_revision').value), current = ++epoch;
      loading = true; state.refresh();
      try { const record = await context.readRequest(id, revision); if (editor !== state || current !== epoch || $('field-request_id').value.trim() !== id || Number($('field-request_revision').value) !== revision) return; useRequest(record); }
      catch (error) { if (editor === state && current === epoch) showStatus(status, error.message, true); }
      finally { loading = false; if (editor === state) state.refresh(); }
    });
    goto.addEventListener('click', () => activeView()?.element.scrollIntoView({block: 'start'}));
    for (const field of [enable.input, preference, difference, checked.A.input, checked.B.input]) field.addEventListener('change', () => state.refresh());
    const changed = () => { ++epoch; if (state.ownedView) { disposeView(state.ownedView); state.ownedView = null; } original = null; media.replaceChildren(); state.refresh(); };
    $('field-request_id').addEventListener('input', changed); $('field-request_revision').addEventListener('input', changed);
    state.cleanup = () => { ++epoch; $('field-request_id')?.removeEventListener('input', changed); $('field-request_revision')?.removeEventListener('input', changed); };
    state.refresh = () => {
      const view = activeView(), available = inputsMatch(original) && isAB(original), decoded = SLOTS.every(slot => view?.items[slot].display === 'decoded');
      load.disabled = locked || loading || !supported(context.node); load.textContent = loading ? '正在读取固定版本…' : '读取原问题的 A/B 设置';
      enable.input.disabled = locked || !available || !supported(context.node); fields.hidden = !enable.input.checked;
      for (const control of [preference, difference, checked.A.input, checked.B.input]) control.disabled = locked || !available || !enable.input.checked;
      for (const option of preference.options) option.disabled = ['A', 'B', 'no_preference'].includes(option.value) && !decoded;
      for (const option of difference.options) option.disabled = ['', 'unable_to_judge'].includes(preference.value) && !['', 'unknown'].includes(option.value);
      display.textContent = SLOTS.map(slot => `${slot}：${DISPLAY[view?.items[slot].display || 'not_loaded']}`).join(' · ');
      identity.textContent = `此显示区主动展开素材身份：${view?.revealed ? '是' : '否'}（不证明此前不知道身份）。`;
      source.textContent = available ? `答复绑定 ${original.id} v${original.revision}；关闭或上下文变化不会自动否定评价。` : '原问题尚未核对，不能填写结构化 A/B 判断。';
      goto.hidden = !view; if (!supported(context.node)) showStatus(status, '节点未提供 research.evaluation-v1；保留文字答复入口。', true);
    };
    state.collect = data => {
      if (!enable.input.checked) { delete data.evaluation; return; }
      const view = activeView();
      if (!supported(context.node) || !inputsMatch(original) || !isAB(original)) throw new Error('先明确读取原问题的 A/B 版本；原请求 ID/版本必须保持一致。');
      const preferred = preference.value || null, differenceValue = difference.value || null, isPreference = ['A', 'B', 'no_preference'].includes(preferred);
      if (isPreference && (!SLOTS.every(slot => view?.items[slot].display === 'decoded') || !SLOTS.every(slot => checked[slot].input.checked))) throw new Error('偏好判断要求两边本地显示成功，并由你明确勾选已查看。');
      if (!isPreference && ![null, 'unknown'].includes(differenceValue)) throw new Error('未作判断或无法判断时，差异只能为未知或未判断。');
      data.evaluation = {schema: 'phonefarm/ab-feedback@1', preference: preferred, difference: differenceValue, reviewed: {A: checked.A.input.checked, B: checked.B.input.checked}, client_display: {A: view?.items.A.display || 'not_loaded', B: view?.items.B.display || 'not_loaded'}, identity_revealed: view ? view.revealed : null};
    };
    state.snapshot = () => ({enabled: enable.input.checked, preference: preference.value || null, difference: difference.value || null, reviewed: {A: checked.A.input.checked, B: checked.B.input.checked}});
    if (sameRequest(detailView, {node_id: context.target?.node_id, id: context.data.request_id, revision: context.data.request_revision})) useRequest(detailView.record);
  }
  function setLocked(value) { locked = value; editor?.refresh?.(); }
  function collect(data) { editor?.collect?.(data); }
  function snapshot() { return editor?.snapshot?.(); }
  document.addEventListener('visibilitychange', () => { if (document.hidden) unloadAll('页面已隐藏，媒体已卸下；回来后请明确重新加载。'); });
  document.addEventListener('hgamey-target-change', () => { for (const view of views) if (HGTarget.problem(view.target)) unloadView(view, '目标身份未确认，已卸下媒体。'); editor?.refresh?.(); });
  window.addEventListener('pagehide', () => unloadAll('已离开页面，媒体已卸下。'));
  window.HGResearchEvaluation = {mountEditor, setLocked, collect, snapshot, renderDetail, clearDetail, revealDetail, unloadAll};
})();
