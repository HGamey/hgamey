/* Comparisons an agent asks a human to judge: N images or videos shown as A, B, C…, of which the human picks k
   (the agent sets both) and says how large the difference is; when k is N the human ranks them all, best first,
   and says how much better each is than the next. Which source sits behind each letter is shown only
   once the comparison has an answer. Reads phonefarm/ab-evaluation@1 (two slots, pick one) and phonefarm/compare@1. */
(() => {
  'use strict';
  const LETTERS = 'ABCDEFGH';
  const DIFF = {small: '小', moderate: '中', large: '大'}, GAP = {none: '差不多', ...DIFF}, RANK = '①②③④⑤⑥⑦⑧';
  // A comparison that asks for every item is a ranking.
  const ranks = s => !s.legacy && s.slots.length > 1 && s.pick === s.slots.length;

  // One shape for both schemas: the slots in order, how many to pick, each slot's evidence ref and media kind.
  function spec(record) {
    const value = record.data.evaluation, refs = record.data.evidence_refs || [];
    if (value?.schema === 'phonefarm/ab-evaluation@1')
      return {legacy: true, pick: 1, slots: ['A', 'B'].map(slot => ({slot, ref: refs[value.slots[slot]], kind: value.media_kind}))};
    if (value?.schema === 'phonefarm/compare@1')
      return {legacy: false, pick: value.pick, slots: value.items.map((item, i) => {
        const index = typeof item === 'number' ? item : item.ref, ref = refs[index];
        return {slot: LETTERS[i], ref, kind: item.media_kind || null};
      })};
    return null;
  }
  const isComparison = record => record?.kind === 'request' && record.data?.type === 'evaluation' && !!spec(record);

  // What an answer says, in the same words for both schemas: the picked letters, or that it looked the same / could not be judged.
  function verdict(value) {
    if (!value) return null;
    if (value.schema === 'phonefarm/ab-feedback@1') {
      if (value.preference === 'A' || value.preference === 'B') return {picked: [value.preference], difference: value.difference};
      return {same: value.preference === 'no_preference', unable: value.preference !== 'no_preference', difference: value.difference};
    }
    if (value.schema === 'phonefarm/compare-feedback@1')
      return value.unable ? {unable: true} : value.picked?.length ? {picked: value.picked, difference: value.difference, gaps: value.gaps?.length ? value.gaps : null}
        : {same: true, difference: value.difference};
    return null;
  }
  function verdictText(v, pick) {
    if (!v) return '';
    if (v.unable) return '无法判断';
    if (v.same) return '差不多';
    // A ranking reads best to worst with how far apart each neighbour is: B ›小› D ≈ A ›大› C.
    if (v.gaps) return v.picked[0] + v.gaps.map((gap, i) => (gap === 'none' ? ' ≈ ' : ` ›${DIFF[gap]}› `) + v.picked[i + 1]).join('');
    const chosen = v.picked.join('、') + (pick === 1 ? ' 更好' : ' 入选');
    return DIFF[v.difference] ? `${chosen} · 差别${DIFF[v.difference]}` : chosen;
  }

  async function load(record, U) {
    const answers = await U.api(`/research-state/feedback?request_id=${encodeURIComponent(record.id)}&limit=20`);
    // Answers are newest first; the newest one with a structured judgement is the one shown.
    for (const item of answers.items) {
      const full = await U.api(`/research-state/feedback/${encodeURIComponent(item.id)}`);
      if (verdict(full.data.evaluation)) return full;
    }
    return answers.items.length ? await U.api(`/research-state/feedback/${encodeURIComponent(answers.items[0].id)}`) : null;
  }

  // A task's comparisons in one read: each full record with its newest answer, page by page.
  async function taskRequests(id, U) {
    const query = new URLSearchParams({research_id: id}), items = [];
    query.set('full', '1'); query.set('feedback', 'latest'); query.set('limit', '100');
    for (let cursor = null; ; ) {
      if (cursor) query.set('cursor', cursor);
      const page = await U.api(`/research-state/request?${query}`);
      items.push(...page.items); cursor = page.next_cursor;
      if (!cursor) break;
    }
    return items;
  }

  // A block's files wait until it is near the screen (the task page's reader when it has one).
  let near = (node, run) => run();
  function media(record, item, state, done, clips) {
    const path = `/dash/api/research-state/request/${encodeURIComponent(record.id)}/media/${item.slot}?revision=${record.revision}`;
    const holder = document.createElement('div'), size = document.createElement('span');
    holder.className = 'compare-media waiting'; state[item.slot] = 'not_loaded';
    const file = fileFor(`${record.id}@${record.revision}/${item.slot}`, path);
    const entry = clips.add({slot: item.slot, file, size, kind: item.kind});
    // A slot whose kind the record does not state takes it from the file's first read.
    const kind = item.kind ? Promise.resolve(item.kind) : new Promise(resolve => near(holder, () => {
      const seen = () => { if (!file.total && !file.error) return; file.watch.delete(seen); resolve(/^video\//.test(file.type) ? 'video' : 'image'); };
      file.watch.add(seen); ask(file, false); seen();
    }));
    const ready = kind.then(async k => {
      entry.kind = k;
      const node = element(k, item, state, size);
      holder.append(node); done();
      const copy = await entry.copy;
      size.textContent = `${mb(copy.bytes)} MB`; node.src = copy.url;
      if (k === 'video') node.controls = true;
      holder.classList.remove('waiting'); done();
      return {slot: item.slot, url: copy.url, kind: k};
    });
    return {holder, size, ready, kind};
  }
  // The element shows the local copy; its source is set when the copy is in.
  function element(kind, item, state, size) {
    const node = document.createElement(kind === 'video' ? 'video' : 'img');
    const shown = () => {
      state[item.slot] = 'decoded';
      const w = node.videoWidth || node.naturalWidth, h = node.videoHeight || node.naturalHeight;
      size.textContent = [w && h ? `${w}×${h}` : '', Number.isFinite(node.duration) ? `${node.duration.toFixed(1)} 秒` : ''].filter(Boolean).join(' · ');
    };
    node.onerror = () => { state[item.slot] = 'failed'; size.textContent = '下载完了，但打不开'; };
    if (node.tagName === 'VIDEO') {
      Object.assign(node, {controls: false, preload: 'auto', playsInline: true, muted: true});
      node.onloadstart = () => { if (state[item.slot] === 'not_loaded') state[item.slot] = 'unknown'; };
      node.onloadedmetadata = node.onloadeddata = shown;
      return node;
    }
    node.decoding = 'async';
    node.onload = () => { node.alt = item.slot; node.decode().then(shown, node.onerror); };
    return node;
  }

  // Every file of a comparison comes through here, one read at a time across the page, and the grid and the viewer show
  // the local copies: a switch never waits on the network and every clip shows the same moment. Nothing is re-encoded.
  // What has arrived stays: when the link cuts a read off or refuses it, the next read a few seconds later asks for the
  // bytes still missing, with nobody clicking. A file's first read is its first byte, which gives the size and type of
  // everything asked for before the long reads begin. A file is kept while its request is shown.
  const QUIET = 30000;   // a read with no bytes for this long has been cut off, whatever the browser thinks
  const files = new Map(), queue = new Set();   // `${request}@${revision}/${slot}` → what is held of that file
  let reading = null;
  const mb = n => (n / 1048576).toFixed(1);
  const tell = file => file.watch.forEach(see => see());
  const needed = file => !file.url && !file.gone && !file.error && (file.whole || !file.total);
  const cutOff = error => error instanceof TypeError || error.name === 'AbortError' || [408, 429, 502, 503, 504].includes(error.status) || error.status >= 520 && error.status <= 530;
  const rest = (file, seconds) => new Promise(done => { const timer = setTimeout(done, seconds * 1000); file.stop = () => { clearTimeout(timer); done(); }; });
  function fileFor(key, path) {
    if (!files.has(key)) files.set(key, {path, parts: [], bytes: 0, total: 0, type: '', tag: '', whole: false, down: false, error: null, url: null, gone: false, stop: null, watch: new Set()});
    return files.get(key);
  }
  function keep(requests) {
    const shown = new Set(requests.map(r => `${r.id}@${r.revision}/`));
    for (const [key, file] of files) if (!shown.has(key.slice(0, key.lastIndexOf('/') + 1))) {
      file.gone = true; file.stop?.(); queue.delete(file); files.delete(key);
      if (file.url) URL.revokeObjectURL(file.url);
    }
  }
  // Asks for a file's first read, or for all of it.
  function ask(file, whole) {
    file.whole ||= whole; file.error = null;
    if (needed(file)) queue.add(file);
    pump(); tell(file);
  }
  async function pump() {
    if (reading) return;
    const asked = [...queue], file = reading = asked.find(f => !f.total) || asked[0] || null;
    if (!file) return;
    await carryOn(file);
    if (!needed(file)) queue.delete(file);
    reading = null; pump();
  }
  // Reads until the file is whole, its size is what was asked for, or the node refuses it for a reason waiting will not cure.
  async function carryOn(file) {
    tell(file);
    for (let pause = 2; ;) {
      const sizing = !file.total, held = file.bytes;
      let failed = null;
      try { await read(file); } catch (error) { failed = error; }
      if (file.gone) return;
      if (failed && !cutOff(failed)) { file.error = failed; file.down = false; return tell(file); }
      if (file.total && file.bytes >= file.total) {
        file.url = URL.createObjectURL(new Blob(file.parts, {type: file.type})); file.parts = [];
        return tell(file);
      }
      if (!failed && sizing && file.total) return;
      if (file.bytes > held) { pause = 2; if (!failed) continue; }
      file.down = true; tell(file);
      await rest(file, pause); pause = Math.min(15, pause * 2);
      if (file.gone) return;
    }
  }
  async function read(file) {
    const control = new AbortController();
    let quiet;
    const alive = () => { clearTimeout(quiet); quiet = setTimeout(() => control.abort(), QUIET); };
    file.stop = () => control.abort(); alive();
    try {
      const headers = {Range: file.total ? `bytes=${file.bytes}-` : 'bytes=0-0'};
      if (file.tag) headers['If-Range'] = file.tag;
      const response = await HGTarget.fetch(file.path, {credentials: 'same-origin', cache: 'no-store', signal: control.signal, headers});
      if (!response.ok) { response.body?.cancel(); throw Object.assign(new Error(`HTTP ${response.status}`), {status: response.status}); }
      if (response.status !== 206) { file.parts = []; file.bytes = 0; }   // the range was not honoured or the file changed: all of it follows
      file.total = Number(response.headers.get('X-Total-Size')); file.type = response.headers.get('Content-Type') || ''; file.tag = response.headers.get('ETag') || '';
      file.down = false; tell(file);
      const reader = response.body.getReader();
      for (;;) {
        const {done, value} = await reader.read();
        if (done) return;
        alive(); file.parts.push(value); file.bytes += value.length; tell(file);
      }
    } finally { clearTimeout(quiet); }
  }
  // One block's files on one line, which keeps its place so nothing below moves when the download ends. Pictures show as
  // each arrives; videos get their copies together, once every video of the block is in. A file the node refused is
  // retried from the line.
  function loader(U, onDone) {
    const line = U.el('div', null, 'compare-load'), bar = U.el('progress'), text = U.el('span'), go = U.el('button', '下载视频后播放', 'ghost'), again = U.el('button', '重试', 'ghost');
    go.type = again.type = 'button'; line.append(bar, text, go, again);
    const list = [], isVideo = c => c.kind === 'video';
    const what = items => !items.every(c => c.kind) ? `${items.length} 个文件`
      : [[items.filter(isVideo).length, '段视频'], [items.filter(c => !isVideo(c)).length, '张图片']].filter(([n]) => n).map(([n, unit]) => `${n} ${unit}`).join('和 ');
    let finished = false;
    const paint = () => {
      const wanted = list.filter(c => c.file.whole), failed = list.filter(c => c.file.error), left = wanted.filter(c => !c.file.url && !c.file.error);
      const got = wanted.reduce((a, c) => a + c.file.bytes, 0), total = wanted.every(c => c.file.total) ? wanted.reduce((a, c) => a + c.file.total, 0) : 0;
      const amount = `${mb(got)}${total ? ` / ${mb(total)}` : ''} MB`;
      bar.hidden = !left.length || !!failed.length; go.hidden = !list.some(c => isVideo(c) && !c.file.whole); again.hidden = !failed.length;
      bar.max = total || 1; bar.value = total ? got : 0;
      text.textContent = list.every(c => c.file.url) ? `已下载 ${what(list)} · ${mb(got)} MB`
        : failed.length ? `${failed.map(c => c.slot).join('、')} 没下载下来：${U.failure(failed[0].file.error)}`
        : list.some(c => c.file.down) ? `连接断了，几秒后接着下${total ? ` · ${amount}` : ''}`
        : !left.length ? `${what(list.filter(c => !c.file.url))}，下载到本地后再播放和切换`
        : reading && !list.some(c => c.file === reading) ? `排队等前面的下完 · ${what(left)}`
        : `正在下载 ${what(wanted)} · ${amount}`;
      for (const {file, size} of list) if (!file.url) size.textContent = file.bytes > 1 ? `${mb(file.bytes)} / ${mb(file.total)} MB` : file.total ? `${mb(file.total)} MB` : '';
      if (!list.every(c => c.kind)) return;
      for (const c of list) if (c.give && c.file.url && (!isVideo(c) || list.filter(isVideo).every(v => v.file.url))) { c.give(c.file); c.give = null; }
      if (!finished && list.every(c => c.file.url)) { finished = true; onDone(); }
    };
    const start = all => list.forEach(c => { if (all || !isVideo(c)) ask(c.file, true); });
    go.onclick = () => start(true); again.onclick = () => list.filter(c => c.file.error).forEach(c => ask(c.file, true));
    return {line, start, paint,
      add(c) { c.copy = new Promise(give => { c.give = give; }); c.file.watch.add(paint); list.push(c); return c; }};
  }

  // The in-page viewer: every slot stacked in one box at the same size, so switching (arrow keys, A–D or the letter
  // buttons) only changes which one shows — nothing reloads, moves or flashes. Videos keep one clock: playing,
  // pausing and seeking the shown one moves them all, so a switch shows the same moment. Only letters are shown.
  // One picture (a report's image) has nothing to switch to, and a click beside it closes the viewer too.
  function viewer(article, slots, start, U, {label = '放大对比'} = {}) {
    const box = U.el('dialog', null, 'compare-viewer'), stage = U.el('div', null, 'viewer-stage'), rail = U.el('div', null, 'viewer-rail');
    const single = slots.length === 1;
    const close = U.el('button', '×', 'viewer-close'), hint = U.el('p', single ? 'Esc\n关闭' : '← →\nA–D\n切换\n\nEsc\n关闭', 'viewer-hint');
    close.type = 'button'; close.setAttribute('aria-label', '关闭'); box.setAttribute('aria-label', label);
    rail.hidden = single;
    let index = 0;
    const items = slots.map(({slot, url, kind}) => {
      const node = document.createElement(kind === 'video' ? 'video' : 'img'), button = U.el('button', slot, 'viewer-slot');
      node.className = 'viewer-media'; node.dataset.slot = slot; button.type = 'button'; button.dataset.slot = slot;
      if (kind === 'video') Object.assign(node, {muted: true, playsInline: true, preload: 'auto', loop: true});
      else { node.alt = slot; node.decoding = 'sync'; }
      node.src = url;
      stage.append(node); rail.append(button);
      return {slot, node, button, video: kind === 'video'};
    });
    const shown = () => items[index];
    const others = () => items.filter((item, i) => i !== index && item.video);
    // The shown video leads; the others follow its play, pause and position.
    const follow = event => {
      const lead = shown(); if (event.target !== lead.node) return;
      for (const item of others()) {
        if (event.type === 'play') item.node.play().catch(() => {});
        if (event.type === 'pause') item.node.pause();
        if (Math.abs(item.node.currentTime - lead.node.currentTime) > 0.05) item.node.currentTime = lead.node.currentTime;
      }
    };
    for (const type of ['play', 'pause', 'seeked']) stage.addEventListener(type, follow, true);
    const settle = node => new Promise(done => node.seeking ? node.addEventListener('seeked', done, {once: true}) : done());
    async function go(next) {
      next = (next + items.length) % items.length;
      if (next === index && box.open) return paint();
      const from = shown(), to = items[next];
      if (from?.video && to.video) {
        if (Math.abs(to.node.currentTime - from.node.currentTime) > (from.node.paused ? 0.01 : 0.15)) to.node.currentTime = from.node.currentTime;
        if (from.node.paused) { to.node.pause(); await settle(to.node); } else to.node.play().catch(() => {});
      }
      index = next; paint();
    }
    function paint() {
      items.forEach((item, i) => {
        item.node.classList.toggle('shown', i === index); item.button.setAttribute('aria-pressed', String(i === index));
        if (item.video) item.node.controls = i === index;
      });
      box.dataset.slot = shown().slot;
    }
    rail.addEventListener('click', event => { const button = event.target.closest('.viewer-slot'); if (button) go(items.findIndex(item => item.slot === button.dataset.slot)); });
    box.addEventListener('keydown', event => {
      if (event.target.closest('video') && [' ', 'Enter'].includes(event.key)) return;
      const letter = items.findIndex(item => item.slot.toLowerCase() === event.key.toLowerCase());
      if (['ArrowRight', 'ArrowDown'].includes(event.key)) go(index + 1);
      else if (['ArrowLeft', 'ArrowUp'].includes(event.key)) go(index - 1);
      else if (letter >= 0) go(letter);
      else return;
      event.preventDefault(); event.stopPropagation();
    }, true);   // before a focused video's own seek keys
    close.onclick = () => box.close();
    if (single) box.addEventListener('click', event => {
      // The picture fills the stage at its own shape (object-fit: contain); only a click on the picture itself keeps it open.
      const node = items[0].node, r = node.getBoundingClientRect(), w = node.naturalWidth, h = node.naturalHeight;
      if (event.target === close) return;
      const scale = w && h ? Math.min(r.width / w, r.height / h) : 0, x = r.left + (r.width - w * scale) / 2, y = r.top + (r.height - h * scale) / 2;
      if (event.target !== node || event.clientX < x || event.clientX > x + w * scale || event.clientY < y || event.clientY > y + h * scale) box.close();
    });
    box.addEventListener('close', () => { items.forEach(item => item.video && item.node.pause()); box.remove(); });
    box.append(stage, rail, close, hint); article.append(box);
    index = Math.max(0, items.findIndex(item => item.slot === start)); paint();
    box.showModal(); box.focus();
    return box;
  }

  // The letters' sources, once answered: result source → the run and file it came from.
  async function reveal(items, U) {
    const results = new Map();
    for (const {ref} of items) if (ref?.result_id && !results.has(ref.result_id))
      results.set(ref.result_id, U.api(`/results/${encodeURIComponent(ref.result_id)}`).catch(() => null));
    return Promise.all(items.map(async ({ref}) => {
      if (!ref) return null;
      const result = ref.result_id ? await results.get(ref.result_id) : null;
      const source = result?.manifest?.sources?.find(s => s.name === ref.source_name);
      return source?.job_id ? {job: source.job_id, path: source.path, name: ref.source_name, label: source.label}
        : {name: source?.filename || ref.source_name || ref.result_id, label: source?.label};
    }));
  }
  // What a person reads for each revealed source: the name the agent gave it, else the short file name.
  const readable = sources => {
    const short = names(sources.map(source => source?.path || source?.name || ''));
    return sources.map((source, i) => source?.label || short[i]);
  };
  // Only a judgement a person gave on this page counts as one; an answer sent with a token (admin or agent) names nobody.
  const byPerson = answer => answer?.audit?.auth_channel === 'dashboard_steer';

  function block(record, answer, ctx, U, fold) {
    const folded = fold && !answer && record.data.state !== 'open';
    const article = U.el(folded ? 'details' : 'article', null, 'compare' + (answer ? ' answered' : folded ? ' compare-closed' : ''));
    article.id = 'cmp-' + record.id; article.dataset.request = record.id;
    const head = U.el(folded ? 'summary' : 'div', null, 'compare-head');
    head.append(U.el('strong', record.data.title, 'compare-title'));
    if (answer) head.append(U.tag('已评', 'ok'));
    else if (record.data.state === 'open') head.append(U.tag('待评', 'warn'));
    else head.append(U.tag('已关闭', 'off'));
    const fill = () => { head.append(U.el('span', record.audit?.actor_label || '', 'muted'), U.timeEl(record.created_at)); contents(article, record, answer, ctx, U); };
    article.append(head);
    if (!folded) fill();
    // A closed comparison nobody answered is one line: its question and files are read only if someone opens it.
    else article.addEventListener('toggle', function opened() { if (article.open) { article.removeEventListener('toggle', opened); fill(); } });
    return article;
  }
  function contents(article, record, answer, ctx, U) {
    const s = spec(record), v = verdict(answer?.data.evaluation), state = {};
    article.append(U.el('p', record.data.need, 'compare-need'));
    const grid = U.el('div', null, 'compare-grid'), cards = new Map(), n = s.slots.length;
    grid.style.setProperty('--cols', n === 4 ? 2 : Math.min(n, 3));
    // With two or more videos, one button starts them all from the beginning (and pauses them all while any plays).
    const together = U.el('button', '一起从头播放', 'ghost compare-together'), players = () => [...grid.querySelectorAll('video')];
    together.type = 'button'; together.hidden = true;
    const sync = () => { together.textContent = players().some(p => !p.paused) ? '全部暂停' : '一起从头播放'; };
    together.onclick = () => {
      if (players().some(p => !p.paused)) players().forEach(p => p.pause());
      else players().forEach(p => { p.currentTime = 0; p.play().catch(() => {}); });
    };
    grid.addEventListener('play', sync, true); grid.addEventListener('pause', sync, true);
    const placed = () => { together.hidden = players().length < 2; together.disabled = players().filter(p => p.src).length < 2; };
    // No play, zoom or switch before every file of the block is in.
    const clips = loader(U, () => grid.querySelectorAll('.compare-zoom').forEach(b => { b.disabled = false; }));
    const ready = [], kinds = [];
    // A picture (or a slot's ⤢) opens the viewer on that slot, inside this page.
    const zoom = async slot => { clips.start(true); viewer(article, await Promise.all(ready), slot, U); };
    for (const item of s.slots) {
      const card = U.el('figure', null, 'compare-item'), cap = U.el('figcaption'), m = media(record, item, state, placed, clips), open = U.el('button', '⤢', 'compare-zoom');
      card.dataset.slot = item.slot; open.type = 'button'; open.disabled = true; open.title = '放大对比'; open.setAttribute('aria-label', `放大对比 ${item.slot}`); open.onclick = () => zoom(item.slot);
      cap.append(U.el('b', item.slot, 'compare-letter'), m.size, open);
      card.append(cap, m.holder); grid.append(card); cards.set(item.slot, card); ready.push(m.ready); kinds.push(m.kind);
    }
    const counted = found => {
      const n = found.filter(k => k === 'video').length;
      together.hidden = n < 2; together.disabled = true;   // in place from the start, usable once the copies are in
      clips.paint();
      // Near the screen pictures download, and clips too when waiting to be judged; otherwise clips wait for the line's button.
      near(grid, () => clips.start(!answer && record.data.state === 'open'));
    };
    // Kinds the record states are counted at once, so the line holds its place before the page lands anywhere.
    if (s.slots.every(item => item.kind)) counted(s.slots.map(item => item.kind)); else Promise.all(kinds).then(counted);
    grid.addEventListener('click', event => { const image = event.target.closest('.compare-media img'); if (image) zoom(image.closest('.compare-item').dataset.slot); });
    article.append(clips.line, grid, together);
    if (answer) {
      if (v?.gaps) v.picked.forEach((letter, i) => {   // a ranking: each card carries its place; the best is marked
        const card = cards.get(letter); card?.querySelector('figcaption').prepend(U.el('span', RANK[i] || `(${i + 1})`, 'compare-rank'));
        if (i === 0) card?.classList.add('picked');
      });
      else for (const letter of v?.picked || []) cards.get(letter)?.classList.add('picked');
      const line = U.el('p', null, 'compare-verdict');
      line.append(U.el('span', U.speaker(answer.audit || {}), 'compare-who'));
      if (!byPerson(answer)) line.append(U.tag('评判人不明', 'warn'));
      line.append(U.el('strong', verdictText(v, s.pick) || '已答复'));
      if (answer.data.text && answer.data.text !== verdictText(v, s.pick)) line.append(U.el('span', answer.data.text, 'compare-comment'));
      line.append(U.timeEl(answer.created_at));
      grid.before(line);
      reveal(s.slots, U).then(sources => {
        const shown = readable(sources);
        sources.forEach((source, i) => {
          if (!source) return;
          const where = U.el('span', null, 'compare-source'), card = cards.get(s.slots[i].slot);
          where.append(U.el('b', shown[i], 'compare-source-name'));
          if (source.job) {   // the file and its run, for whoever checks
            const link = U.el('a', `${source.path}`, 'tech'); link.href = HGTarget.url(`/dash/jobs#job=${encodeURIComponent(source.job)}`);
            where.append(link, U.el('span', ` · 作业 ${source.job}`, 'muted tech'));
          }
          card.querySelector('figcaption').append(where);
        });
      });
    } else if (record.data.state === 'open') article.append(form(record, s, state, ctx, U));
  }

  function form(record, s, state, ctx, U) {
    const box = U.el('form', null, 'compare-answer'), picks = U.el('div', null, 'compare-picks'), diff = U.el('div', null, 'compare-diff');
    const comment = U.el('textarea'), send = U.el('button', '提交', 'ghost primary'), status = U.el('p', null, 'compare-status');
    // Locked, the steer password is asked for right here, beside 提交, so nothing chosen is lost and no one is sent away.
    const password = U.el('input'), locked = () => !ctx.writable();
    Object.assign(password, {type: 'password', autocomplete: 'off', placeholder: '掌舵口令'}); password.setAttribute('aria-label', '掌舵口令');
    // Ranking, letters are clicked best to worst; each neighbouring pair then gets how far apart they are (gaps).
    const ranking = ranks(s), order = U.el('div', null, 'compare-order');
    let picked = [], gaps = [], other = null, degree = null;
    const ask = ranking ? '从好到差依次点' : `选 ${s.pick} 个`;
    picks.setAttribute('role', 'group'); picks.setAttribute('aria-label', ask);
    diff.setAttribute('role', 'group'); diff.setAttribute('aria-label', '差别');
    status.setAttribute('role', 'status');
    comment.rows = 1; comment.maxLength = 16384; comment.placeholder = '补充一句（可选）'; comment.setAttribute('aria-label', '补充');
    send.type = 'submit';
    const choice = (label, onClick, cls = '') => { const b = U.el('button', label, 'chip-btn ' + cls); b.type = 'button'; b.setAttribute('aria-pressed', 'false'); b.onclick = () => { failed = ''; onClick(); }; return b; };
    picks.append(U.el('span', ask, 'compare-hint'));
    const letters = s.slots.map(({slot}) => choice(slot, () => {
      other = null;
      if (ranking) {   // a ranked letter takes itself and every place after it back
        const at = picked.indexOf(slot);
        picked = at >= 0 ? picked.slice(0, at) : [...picked, slot];
        gaps = picked.slice(1).map((_, i) => gaps[i] ?? null);
      } else picked = picked.includes(slot) ? picked.filter(x => x !== slot) : [...(s.pick === 1 ? [] : picked), slot].slice(-s.pick);
      paint();
    }, 'letter'));
    const same = choice('差不多', () => { other = other === 'same' ? null : 'same'; picked = []; gaps = []; paint(); });
    const unable = choice('无法判断', () => { other = other === 'unable' ? null : 'unable'; picked = []; gaps = []; paint(); });
    picks.append(...letters, U.el('span', null, 'compare-sep'), same, unable);
    diff.append(U.el('span', '差别', 'compare-hint'), ...Object.entries(DIFF).map(([key, label]) => {
      const b = choice(label, () => { degree = degree === key ? null : key; paint(); }); b.dataset.diff = key; return b;
    }));
    const foot = U.el('div', null, 'compare-foot'); foot.append(comment, password, send);
    box.append(picks, order, diff, foot, status);
    order.setAttribute('aria-label', '名次');
    // The places so far, ① B ‹差不多 小 中 大› ② D …, each gap a choice of how much better the one before is.
    function places() {
      // Rebuilt on every choice: a focused gap button hands its focus to the same button in the same place.
      const focused = order.contains(document.activeElement) && document.activeElement.dataset.gap
        ? [[...order.querySelectorAll('.compare-gap')].indexOf(document.activeElement.closest('.compare-gap')), document.activeElement.dataset.gap] : null;
      order.replaceChildren(...picked.flatMap((letter, i) => {
        const place = U.el('span', null, 'compare-place'); place.append(U.el('b', RANK[i] || `(${i + 1})`), ` ${letter}`);
        if (i === 0) return [place];
        const group = U.el('span', null, 'compare-gap'); group.setAttribute('role', 'group'); group.setAttribute('aria-label', `${picked[i - 1]} 比 ${letter} 好多少`);
        group.append(...Object.entries(GAP).map(([key, label]) => {
          const b = choice(label, () => { gaps[i - 1] = gaps[i - 1] === key ? null : key; paint(); });
          b.dataset.gap = key; b.setAttribute('aria-pressed', String(gaps[i - 1] === key)); return b;
        }));
        return [group, place];
      }));
      if (focused) order.querySelectorAll('.compare-gap')[focused[0]]?.querySelector(`[data-gap="${focused[1]}"]`)?.focus();
    }
    function ready() {
      if (other !== null) return true;
      return ranking ? picked.length === s.slots.length && gaps.every(Boolean) : picked.length === s.pick && degree !== null;
    }
    // Until the answer is saved the form says so, and what is still missing.
    let failed = '';
    function paint() {
      letters.forEach((b, i) => {
        const at = picked.indexOf(s.slots[i].slot);
        b.setAttribute('aria-pressed', String(at >= 0)); b.textContent = ranking && at >= 0 ? `${s.slots[i].slot} ${RANK[at] || ''}` : s.slots[i].slot;
      });
      same.setAttribute('aria-pressed', String(other === 'same')); unable.setAttribute('aria-pressed', String(other === 'unable'));
      diff.hidden = ranking || picked.length === 0; order.hidden = !ranking || picked.length === 0; if (ranking) places();
      for (const b of diff.querySelectorAll('[data-diff]')) b.setAttribute('aria-pressed', String(b.dataset.diff === degree));
      password.hidden = !locked(); send.textContent = send.dataset.busy ? '提交中…' : locked() ? '解锁并提交' : '提交';
      send.disabled = !ready() || !!send.dataset.busy || locked() && !password.value;
      const open = gaps.findIndex(gap => !gap);
      const next = ranking && other === null && picked.length < s.slots.length
          ? (picked.length ? `再点第 ${picked.length + 1} 名（还剩 ${s.slots.length - picked.length} 个）` : `从好到差依次点 ${s.slots.length} 个`)
        : ranking && other === null && open >= 0 ? `再选 ${picked[open]} 和 ${picked[open + 1]} 差多少`
        : other === null && picked.length < s.pick ? `先选 ${s.pick} 个` : !ready() ? '再选差别大小' : locked() && !password.value ? '填掌舵口令后提交' : '可以提交';
      status.textContent = send.dataset.busy ? '提交中…' : failed ? `没有提交：${failed}` : `还没提交 · ${next}`;
      status.classList.toggle('task-error', !!failed && !send.dataset.busy);
    }
    paint();
    password.addEventListener('input', () => { failed = ''; paint(); });
    comment.addEventListener('input', () => { comment.style.height = 'auto'; comment.style.height = Math.min(comment.scrollHeight + 2, 240) + 'px'; });
    box.onsubmit = async event => {
      event.preventDefault();
      if (!ready() || send.dataset.busy) return;
      const display = Object.fromEntries(s.slots.map(({slot}) => [slot, state[slot]]));
      // Picks keep the order they were clicked in: for a ranking that order is the answer.
      const v = other === 'unable' ? {unable: true} : other === 'same' ? {same: true}
        : ranking ? {picked: picked.slice(), gaps: gaps.slice(), difference: null} : {picked: picked.slice(), difference: degree};
      const evaluation = s.legacy
        ? {schema: 'phonefarm/ab-feedback@1', preference: v.unable ? 'unable_to_judge' : v.same ? 'no_preference' : v.picked[0],
           difference: v.unable ? 'unknown' : v.same ? 'none' : degree,
           reviewed: v.unable ? {A: null, B: null} : {A: true, B: true}, client_display: display, identity_revealed: false}
        : {schema: 'phonefarm/compare-feedback@1', picked: v.picked || [], unable: !!v.unable, difference: v.unable || v.gaps ? null : v.same ? 'none' : degree,
           ...(v.gaps ? {gaps: v.gaps} : {}), client_display: display, identity_revealed: false};
      const text = comment.value.trim() || verdictText(v, s.pick);
      send.dataset.busy = '1'; failed = ''; paint();
      try {
        if (locked()) { await ctx.unlock(password.value); password.value = ''; }
        await U.write('feedback', 'fb-' + crypto.randomUUID().replace(/-/g, '').slice(0, 12),
                      {request_id: record.id, request_revision: record.revision, text, supersedes: null, evidence_refs: [], evaluation}, null, null);
        await ctx.reload();
      } catch (error) {
        failed = U.failure(error); delete send.dataset.busy; paint();
      }
    };
    return box;
  }

  // The research's comparisons: those waiting for a judgement first (the caller puts them at the top), answered ones apart.
  async function sections(record, ctx, U) {
    if (U.near) near = U.near;
    const requests = (await taskRequests(record.id, U)).filter(isComparison);
    keep(requests);
    // The newest answer is shown when it carries a judgement; otherwise the answers are read one by one as before.
    const answers = await Promise.all(requests.map(r => r.latest_feedback === null ? null
      : verdict(r.latest_feedback?.data?.evaluation) ? r.latest_feedback : load(r, U).catch(() => null)));
    const open = [], done = [], closed = [];
    requests.forEach((r, i) => {
      const item = block(r, answers[i], ctx, U, true);
      (answers[i] ? done : r.data.state !== 'open' ? closed : open).push(item);
    });
    // The comparison a task's top shows: the newest ranking a person gave on the page, else their newest answered comparison.
    const answered = requests.map((r, i) => [r, answers[i], verdict(answers[i]?.data.evaluation)])
      .filter(([, a, v]) => v?.picked?.length && byPerson(a))
      .sort(([, a], [, b]) => b.created_at - a.created_at);
    const [kr, ka, kv] = answered.find(([, , v]) => v.gaps) || answered[0] || [];
    const section = (items, title, id) => {
      if (!items.length) return null;
      const box = U.el('section', null, 'research-section compare-section'), head = U.el('div', null, 'section-head');
      box.id = id; head.append(U.el('h3', title), U.el('span', items.length > 1 ? String(items.length) : '', 'count'));
      box.append(head, ...items); return box;
    };
    return {open: section(open, '待你评', 'task-compare-open'), done: section([...done, ...closed], '对比', 'task-compare-done'),
            key: kr ? key(kr, ka, kv, U) : null};
  }
  // B ›大› D ›中› A: each letter with the file it turned out to be; the best one marked. Links to the full block below.
  function key(record, answer, v, U) {
    const s = spec(record), box = U.el('div', null, 'summary-block summary-compare'), head = U.el('div', null, 'section-head');
    const jump = U.el('a', '看对比', 'summary-link');
    jump.href = '#cmp-' + record.id;
    head.append(U.el('h2', '关键对比'), jump);
    const order = U.el('p', null, 'summary-rank'), chips = new Map();
    v.picked.forEach((letter, i) => {
      if (i) order.append(U.el('span', v.gaps ? (v.gaps[i - 1] === 'none' ? '≈' : `›${DIFF[v.gaps[i - 1]]}›`) : '、', 'summary-gap'));
      const chip = U.el('span', null, 'summary-chip' + (i === 0 ? ' best' : ''));
      chip.append(U.el('b', letter));
      chips.set(letter, chip); order.append(chip);
    });
    if (!v.gaps) order.append(U.el('span', s.pick === 1 ? '更好' : '入选', 'summary-gap'));
    box.append(head, U.el('p', record.data.title, 'summary-title'), order);
    reveal(s.slots, U).then(sources => {
      const named = readable(sources);
      sources.forEach((source, i) => {
        const chip = chips.get(s.slots[i].slot);
        if (!chip || !named[i]) return;
        chip.append(U.el('span', named[i]));
        chip.title = source?.path || source?.name || '';
      });
    });
    return box;
  }
  // Short names for files that differ only partly: the path segments that tell them apart (counted from the file end),
  // dropping outer ones while the names stay distinct. sr_lowres/traces/A1_video.mp4 → sr_lowres/A1_video.
  function names(paths) {
    const split = paths.map(path => path.replace(/\.[^./]+$/, '').split('/')), depth = Math.min(...split.map(s => s.length));
    const name = ks => split.map(s => ks.map(k => s[s.length - k]).join('/')), distinct = list => new Set(list).size === list.length;
    let keep = [];
    for (let k = 1; k <= depth; ++k) if (new Set(split.map(s => s[s.length - k])).size > 1) keep.unshift(k);
    while (keep.length > 1 && distinct(name(keep.slice(1)))) keep = keep.slice(1);
    return keep.length ? name(keep) : split.map(s => s[s.length - 1]);
  }

  async function renderRequest(container, record, ctx, U) {
    if (!isComparison(record)) return;
    keep([record]);
    const answer = await load(record, U).catch(() => null), box = U.el('section', null, 'research-section compare-section');
    box.append(block(record, answer, ctx, U)); container.append(box);
  }

  window.HGResearchCompare = {sections, renderRequest, isComparison, viewer};
})();
