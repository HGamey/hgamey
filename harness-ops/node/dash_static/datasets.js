/* Datasets: results registered with a `dataset` field, a name's versions together, newest first. How the low-res side
   was made shows on every row: a simulated pairing must never read as a real one. A row opens in place, and only then
   are its previews, manifest and QA report read. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id), API = '/dash/api', DASH = '—';
  const PAIRING = {simulated: ['模拟低清', 'warn'], real: ['真实配对', 'ok'], unpaired: ['无配对', 'off'], mixed: ['混合（含模拟）', 'warn']};
  const COLUMNS = [['版本'], ['配对'], ['游戏'], ['设备'], ['采集方法'], ['帧数', 'num'], ['场景', 'num'], ['训练 / 验证 / 测试', 'num'], ['大小', 'num']];
  const PREVIEW = /\.(png|jpe?g|webp|gif|avif)$/i, MIME = {png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', webp: 'image/webp', gif: 'image/gif', avif: 'image/avif'};
  const REPORTS = ['qa_report.json', 'manifest.json', 'split.json'], FIRST_PREVIEWS = 8, TEXT_LIMIT = 1 << 20;
  const opened = new Set();   // result IDs open across a refresh
  let computers = new Map(), models = new Map();   // node ID → name; phone serial or alias → model

  // The device a person reads: phone serials and commits are technical details, folded with the separator before them;
  // a serial the phones on this center know becomes its model when the label does not name it already.
  const SERIAL = /^(?=.*\d)(?=.*[A-Z])[A-Z0-9]{8,20}$/, HASH = /^(?=.*\d)(?=.*[a-f])[0-9a-f]{7,40}$/;
  function device(label) {
    if (!label) return DASH;
    const box = el('span', null, 'ds-device'), parts = String(label).split(/(\s*[\/;,]\s*|\s+)/);
    for (let i = 0; i < parts.length; i += 2) {
      const word = parts[i], before = i ? parts[i - 1] : '', model = models.get(word);
      if (!word) { box.append(before); continue; }
      if (model || SERIAL.test(word) || HASH.test(word)) {
        const named = model && !label.includes(model.split(' ').pop());
        if (named) box.append(before, model);
        box.append(el('span', (named ? ' ' : before) + word, 'tech'));
      } else box.append(before, word);
    }
    return box;
  }

  function el(tag, value, className) { const item = document.createElement(tag); if (value != null) item.textContent = String(value); if (className) item.className = className; return item; }
  const count = n => n == null ? DASH : n.toLocaleString('zh-CN');
  function size(bytes) {
    if (bytes == null) return DASH;
    const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'], at = Math.min(units.length - 1, Math.floor(Math.log(Math.max(bytes, 1)) / Math.log(1024)));
    return `${at ? (bytes / 1024 ** at).toFixed(bytes / 1024 ** at < 100 ? 1 : 0) : bytes} ${units[at]}`;
  }
  const splits = s => s ? [s.train, s.val, s.test].map(count).join(' / ') : DASH;
  const base = path => String(path || '').split('/').pop();
  const fileName = source => base(source.path || source.name);

  async function read(path, init) {
    const response = await HGTarget.fetch(API + path, {credentials: 'same-origin', cache: 'no-store', ...init});
    if (response.ok) return response;
    const body = await response.json().catch(() => null), reason = typeof body?.error === 'string' ? body.error : body?.error?.message;
    throw new Error(reason || `HTTP ${response.status}`);
  }
  const json = async path => (await read(path)).json();
  async function all() {
    const items = [];
    for (let cursor = null; ;) {
      const query = new URLSearchParams({datasets: '1', limit: '100'});
      if (cursor) query.set('cursor', cursor);
      const page = await json(`/results?${query}`);
      items.push(...page.items); cursor = page.next_cursor;
      if (!cursor) return items;
    }
  }

  // Each name once, the one with the newest registration first; current versions before superseded ones.
  function grouped(items) {
    const names = new Map();
    for (const item of items) { const name = item.dataset.name || item.title || item.result_id; if (!names.has(name)) names.set(name, []); names.get(name).push(item); }
    const newest = list => Math.max(...list.map(item => item.registered_at)), obsolete = item => item.superseded_by ? 1 : 0;
    return [...names].sort(([, a], [, b]) => newest(b) - newest(a)).map(([name, list]) => [name, list.sort((a, b) =>
      obsolete(a) - obsolete(b) || (a.dataset.version == null) - (b.dataset.version == null) || String(b.dataset.version).localeCompare(String(a.dataset.version), undefined, {numeric: true}) || b.registered_at - a.registered_at)]);
  }

  function row(item) {
    const d = item.dataset, tr = el('tr', null, 'ds-version'), [word, tone] = PAIRING[d.pairing] || [d.pairing, 'warn'], open = el('button', (d.version ?? '未写版本') + (item.superseded_by ? '（已替代）' : ''), 'ds-open');
    const panel = el('tr', null, 'ds-detail'), cell = el('td'); cell.colSpan = COLUMNS.length; panel.append(cell); panel.hidden = true;
    open.type = 'button'; open.setAttribute('aria-expanded', 'false');
    tr.dataset.result = item.result_id; tr.dataset.pairing = d.pairing;
    const cells = [open, el('span', word, `tag ${tone} ds-pairing`), [item.labels?.game_id, item.labels?.game_version].filter(Boolean).join(' ') || DASH, device(item.labels?.device),
      d.capture_method || DASH, count(d.frames), count(d.scenes), splits(d.splits), size(d.size_bytes)];
    cells.forEach((value, i) => { const td = el('td', value instanceof Node ? null : value, COLUMNS[i][1]); if (value instanceof Node) td.append(value); tr.append(td); });
    let built = false;
    const toggle = () => {
      panel.hidden = !panel.hidden; open.setAttribute('aria-expanded', String(!panel.hidden));
      if (panel.hidden) opened.delete(item.result_id); else opened.add(item.result_id);
      if (!panel.hidden && !built) { built = true; detail(cell, item); }
    };
    tr.addEventListener('click', toggle);
    if (opened.has(item.result_id)) { opened.delete(item.result_id); toggle(); }
    return [tr, panel];
  }
  function render(items) {
    const box = $('datasets'), empty = !items.length;
    box.hidden = empty; showMessage(empty ? '还没有登记过数据集。' : '');
    if (empty) return box.replaceChildren();
    const table = el('table', null, 'ds-table'), head = el('tr');
    head.append(...COLUMNS.map(([label, kind]) => el('th', label, kind)));
    const thead = el('thead'); thead.append(head); table.append(thead);
    for (const [name, list] of grouped(items)) {
      const body = el('tbody', null, 'ds-group'), title = el('tr', null, 'ds-name'), th = el('th'); th.colSpan = COLUMNS.length; th.scope = 'rowgroup';
      th.append(el('span', name), el('small', `${list.length} 个版本`));
      const purpose = list.find(item => item.dataset.purpose)?.dataset.purpose;   // the newest version that says it
      if (purpose) th.append(el('span', purpose, 'ds-purpose'));
      const samples = el('span', null, 'ds-samples'); samples.hidden = true; samples.dataset.results = list.map(item => item.result_id).join(' ');
      th.append(samples);
      title.append(th); body.append(title);
      for (const item of list) body.append(...row(item));
      table.append(body);
    }
    box.replaceChildren(table);
    showSamples(table);
  }
  // Under each name, a sample from its newest version that has one: a low-res / high-res pair, or one picture, from
  // files the center holds. A picture that does not load is left out.
  async function showSamples(table) {
    let found;
    try { found = (await json('/datasets/samples')).items || {}; } catch (_) { return; }
    for (const box of table.querySelectorAll('.ds-samples')) {
      const chosen = box.dataset.results.split(' ').map(rid => found[rid]).find(list => list?.length);
      if (!chosen) continue;
      for (const sample of chosen) {
        const figure = el('figure', null, 'ds-sample'), image = el('img');
        Object.assign(image, {alt: sample.label, loading: 'lazy', src: HGTarget.url(sample.url)});
        image.onerror = () => { figure.remove(); box.hidden = !box.children.length; };
        image.onclick = () => { $('zoom').querySelector('img').src = image.src; $('zoom').querySelector('img').alt = sample.label; $('zoom').showModal(); };
        figure.append(image, el('figcaption', sample.label));
        box.append(figure);
      }
      box.hidden = false;
    }
  }
  function showMessage(text, retry) {
    const box = $('message'); box.hidden = !text; box.textContent = text || ''; box.classList.toggle('task-error', !!retry);
    if (retry) { const again = el('button', '重试', 'ghost'); again.type = 'button'; again.onclick = retry; box.append(' ', again); }
  }
  async function load() {
    $('refresh').disabled = true;
    try {
      const node = await HGTarget.ready();
      if (!node.capabilities?.includes('results.index-v1')) return showMessage('这个节点读不了数据集。');
      const [items] = await Promise.all([all(), json('/nodes').then(value => { computers = new Map((value.items || []).map(item => [item.node_id, item.name])); }, () => {}),
        json('/phones').then(value => {
          models = new Map();
          for (const phone of value.items || []) {
            const seen = phone.observation || {}, model = [seen.brand, seen.model || phone.model].filter(Boolean).join(' ');
            if (model) for (const name of [phone.serial, phone.device_id, ...(phone.aliases || [])]) if (name) models.set(name, model);
          }
        }, () => {})]);
      render(items);
    } catch (error) { showMessage(`数据集没读出来：${error.message}`, load); }
    finally { $('refresh').disabled = false; }
  }

  // ── An opened row
  function section(title) { const box = el('section', null, 'ds-section'); box.append(el('h3', title)); return box; }
  async function detail(cell, item) {
    cell.replaceChildren(el('p', '正在读取…', 'note'));
    let value;
    try { value = await json(`/results/${encodeURIComponent(item.result_id)}`); }
    catch (error) {
      const line = el('p', `没读出来：${error.message} `, 'task-error'), again = el('button', '重试', 'ghost');
      again.type = 'button'; again.onclick = () => detail(cell, item); line.append(again); cell.replaceChildren(line); return;
    }
    const m = value.manifest, sources = m.sources || [], current = new Map((value.current?.sources || []).map(s => [s.name, s]));
    const pictures = sources.filter(s => PREVIEW.test(fileName(s))), reports = REPORTS.map(file => [file, sources.find(s => fileName(s) === file)]);
    const others = sources.filter(s => !pictures.includes(s) && !reports.some(([, r]) => r === s));
    const head = el('p', null, 'ds-record'), ran = value.provenance?.execution_node_id;
    head.append(el('span', m.title), el('span', item.result_id, 'mono'), el('span', new Date(value.registered_at * 1000).toLocaleString()));
    if (ran && ran !== value.node_id) head.append(el('span', `在 ${computers.get(ran) || `电脑 ${ran.slice(0, 8)}`} 上采集`));
    cell.replaceChildren(head);
    if (pictures.length) cell.append(previews(pictures, current));
    cell.append(whereFrom(item, value, sources, others, current), files(reports, current));
  }
  function previews(pictures, current) {
    const box = section(`预览 · ${pictures.length} 张`), grid = el('div', null, 'ds-previews');
    let shown = 0;
    const more = el('button', '', 'ghost'); more.type = 'button';
    async function next() {
      const batch = pictures.slice(shown, shown + FIRST_PREVIEWS); shown += batch.length;
      more.hidden = shown >= pictures.length; more.textContent = `再看 ${pictures.length - shown} 张`;
      const tiles = batch.map(source => { const tile = el('button', null, 'ds-preview'); tile.type = 'button'; tile.title = source.name; grid.append(tile); return [tile, source]; });
      for (const [tile, source] of tiles) await picture(tile, source, current.get(source.name));   // one at a time, in order
    }
    more.onclick = next; box.append(grid, more); next();
    return box;
  }
  async function picture(tile, source, state) {
    tile.classList.remove('failed'); tile.textContent = ''; tile.onclick = null;
    if (!state?.url) { tile.textContent = '原件读不到'; tile.classList.add('failed'); return; }
    try {
      const bytes = await (await read(state.url, {signal: AbortSignal.timeout(60000)})).arrayBuffer();
      const image = el('img'); image.alt = source.name;
      image.src = URL.createObjectURL(new Blob([bytes], {type: MIME[fileName(source).split('.').pop().toLowerCase()]}));
      tile.append(image);
      tile.onclick = () => { $('zoom').querySelector('img').src = image.src; $('zoom').querySelector('img').alt = source.name; $('zoom').showModal(); };
    } catch (_) { tile.textContent = '没读到，点这里重试'; tile.classList.add('failed'); tile.onclick = () => picture(tile, source, state); }
  }
  function whereFrom(item, value, sources, others, current) {
    const box = section('原件和来源'), d = item.dataset, labels = value.manifest.labels || {}, links = el('p', null, 'ds-links');
    box.append(el('p', d.location || '没有写原件放在哪', d.location ? 'ds-location' : 'note'));
    const add = (label, text, href) => { const a = el('a', text); a.href = HGTarget.url(href); links.append(el('span', label, 'note'), a); };
    if (labels.research_id) add('研究', labels.research_id, `/dash/research?kind=research&id=${encodeURIComponent(labels.research_id)}`);
    if (labels.direction_id) add('方向', labels.direction_id, `/dash/research?kind=direction&id=${encodeURIComponent(labels.direction_id)}`);
    for (const job of new Set([value.manifest.job_id, ...sources.map(s => s.job_id)].filter(Boolean))) add('任务', job, `/dash/jobs#job=${encodeURIComponent(job)}`);
    add('完整记录', item.result_id, `/dash/results#result=${encodeURIComponent(item.result_id)}`);
    for (const source of others) {
      const state = current.get(source.name);
      if (state?.url) { const a = el('a', source.name); a.href = HGTarget.url(API + state.url); links.append(el('span', '其他文件', 'note'), a); }
      else links.append(el('span', '其他文件', 'note'), el('span', source.name));
    }
    box.append(links);
    return box;
  }
  function files(reports, current) {
    const box = section('清单和 QA 报告');
    reports.forEach(([file, source], i) => {
      if (!source) return box.append(el('p', `${file}：没有登记`, 'note ds-missing'));
      const fold = el('details', null, 'ds-file'), pre = el('pre', null, 'command-box'); fold.append(el('summary', file), pre);
      let loaded = false;
      fold.addEventListener('toggle', () => { if (fold.open && !loaded) { loaded = true; report(pre, current.get(source.name)); } });
      if (i === 0) fold.open = true;
      box.append(fold);
    });
    return box;
  }
  // A file too long to show stops at the limit, with the way to the whole of it.
  async function report(pre, state) {
    pre.textContent = '正在读取…';
    if (!state?.url) { pre.textContent = '原件读不到'; return; }
    try {
      const response = await read(state.url, {headers: {Range: `bytes=0-${TEXT_LIMIT - 1}`}}), text = await response.text(), total = Number(response.headers.get('X-Total-Size')) || 0;
      if (total > TEXT_LIMIT) {
        const note = el('p', `文件 ${size(total)}，这里只显示开头 ${size(TEXT_LIMIT)}。`, 'note'), whole = el('a', '下载完整文件');
        whole.href = HGTarget.url(API + state.url); note.append(' ', whole);
        pre.textContent = text; pre.before(note);
        return;
      }
      try { pre.textContent = JSON.stringify(JSON.parse(text), null, 2); } catch (_) { pre.textContent = text; }
    } catch (error) {
      pre.textContent = `没读到：${error.message}\n`;
      const again = el('button', '重试', 'ghost'); again.type = 'button'; again.onclick = () => report(pre, state); pre.append(again);
    }
  }

  $('zoom').addEventListener('click', () => $('zoom').close());
  $('refresh').addEventListener('click', load);
  load();
})();
