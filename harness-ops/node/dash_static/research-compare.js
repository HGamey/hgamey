/* Comparisons an agent asks a human to judge: N images or videos shown as A, B, C…, of which the human picks k
   (the agent sets both) and says how large the difference is. Which source sits behind each letter is shown only
   once the comparison has an answer. Reads phonefarm/ab-evaluation@1 (two slots, pick one) and phonefarm/compare@1. */
(() => {
  'use strict';
  const LETTERS = 'ABCDEFGH';
  const DIFF = {small: '小', moderate: '中', large: '大'};

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
      return value.unable ? {unable: true} : value.picked?.length ? {picked: value.picked, difference: value.difference} : {same: true, difference: value.difference};
    return null;
  }
  function verdictText(v, pick) {
    if (!v) return '';
    if (v.unable) return '无法判断';
    if (v.same) return '差不多';
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

  // A task's (or a whole chain's) comparisons in one read: each full record with its newest answer, page by page,
  // in chain order as the per-task lists gave them.
  async function chainRequests(ids, U) {
    const query = new URLSearchParams(ids.map(id => ['research_id', id])), items = [];
    query.set('full', '1'); query.set('feedback', 'latest'); query.set('limit', '100');
    for (let cursor = null; ; ) {
      if (cursor) query.set('cursor', cursor);
      const page = await U.api(`/research-state/request?${query}`);
      items.push(...page.items); cursor = page.next_cursor;
      if (!cursor) break;
    }
    const step = new Map(ids.map((id, i) => [id, i]));
    return items.map((item, i) => [item, i]).sort(([a, i], [b, j]) => (step.get(a.research_id) ?? 0) - (step.get(b.research_id) ?? 0) || i - j).map(([item]) => item);
  }

  // Videos and type probes wait until the block is near the screen (the task page's reader when it has one).
  let near = (node, run) => run();
  // A slot whose kind the record does not state is asked for its type with a one-byte read before an element is chosen.
  function media(record, item, state, done) {
    const path = `/dash/api/research-state/request/${encodeURIComponent(record.id)}/media/${item.slot}?revision=${record.revision}`;
    const holder = document.createElement('div'), size = document.createElement('span');
    holder.className = 'compare-media'; state[item.slot] = 'not_loaded';
    const kind = item.kind ? Promise.resolve(item.kind) : new Promise(resolve => near(holder, () => resolve(
      HGTarget.fetch(path, {credentials: 'same-origin', cache: 'no-store', headers: {Range: 'bytes=0-0'}})
        .then(r => { r.body?.cancel(); return /^video\//.test(r.headers.get('Content-Type') || '') ? 'video' : 'image'; }, () => 'image'))));
    const ready = kind.then(k => { holder.append(element(HGTarget.url(path), k, item, state, size)); done(); return {slot: item.slot, url: HGTarget.url(path), kind: k}; });
    return {holder, size, ready};
  }
  function element(url, kind, item, state, size) {
    const node = document.createElement(kind === 'video' ? 'video' : 'img');
    const shown = () => {
      state[item.slot] = 'decoded';
      const w = node.videoWidth || node.naturalWidth, h = node.videoHeight || node.naturalHeight;
      size.textContent = [w && h ? `${w}×${h}` : '', Number.isFinite(node.duration) ? `${node.duration.toFixed(1)} 秒` : ''].filter(Boolean).join(' · ');
    };
    node.onerror = () => { state[item.slot] = 'failed'; size.textContent = '读取失败'; };
    if (node.tagName === 'VIDEO') {
      Object.assign(node, {controls: true, preload: 'metadata', playsInline: true, muted: true});
      node.onloadstart = () => { if (state[item.slot] === 'not_loaded') state[item.slot] = 'unknown'; };
      node.onloadedmetadata = node.onloadeddata = shown;
      near(node, () => { node.src = url + '#t=0.1'; });   // paints the first frame without playing
      return node;
    }
    node.alt = item.slot; node.decoding = 'async'; node.loading = 'lazy';
    node.onload = () => node.decode().then(shown, node.onerror);
    node.src = url;
    return node;
  }

  // The in-page viewer: every slot stacked in one box at the same size, so switching (arrow keys, A–D or the letter
  // buttons) only changes which one shows — nothing reloads, moves or flashes. Videos keep one clock: playing,
  // pausing and seeking the shown one moves them all, so a switch shows the same moment. Only letters are shown.
  function viewer(article, slots, start, U) {
    const box = U.el('dialog', null, 'compare-viewer'), stage = U.el('div', null, 'viewer-stage'), rail = U.el('div', null, 'viewer-rail');
    const close = U.el('button', '×', 'viewer-close'), hint = U.el('p', '← →\nA–D\n切换\n\nEsc\n关闭', 'viewer-hint');
    close.type = 'button'; close.setAttribute('aria-label', '关闭'); box.setAttribute('aria-label', '放大对比');
    let index = 0;
    const items = slots.map(({slot, url, kind}) => {
      const node = document.createElement(kind === 'video' ? 'video' : 'img'), button = U.el('button', slot, 'viewer-slot');
      node.className = 'viewer-media'; node.dataset.slot = slot; button.type = 'button'; button.dataset.slot = slot;
      if (kind === 'video') Object.assign(node, {muted: true, playsInline: true, preload: 'auto', loop: true});
      else { node.alt = slot; node.decoding = 'sync'; }
      node.src = kind === 'video' ? url + '#t=0.1' : url;
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
      return source?.job_id ? {job: source.job_id, path: source.path, name: ref.source_name} : {name: ref.source_name || ref.result_id};
    }));
  }

  function block(record, answer, ctx, U) {
    const s = spec(record), v = verdict(answer?.data.evaluation), state = {};
    const article = U.el('article', null, 'compare' + (answer ? ' answered' : ''));
    article.id = 'cmp-' + record.id; article.dataset.request = record.id;
    const head = U.el('div', null, 'compare-head');
    head.append(U.el('strong', record.data.title, 'compare-title'));
    if (answer) head.append(U.tag('已评', 'ok'));
    else if (record.data.state === 'open') head.append(U.tag('待评', 'warn'));
    else head.append(U.tag('已关闭', 'off'));
    head.append(U.el('span', record.audit?.actor_label || '', 'muted'), U.timeEl(record.created_at));
    article.append(head, U.el('p', record.data.need, 'compare-need'));
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
    const placed = () => { together.hidden = players().length < 2; };
    const ready = [];
    // A picture (or a slot's ⤢) opens the viewer on that slot, inside this page.
    const zoom = async slot => viewer(article, await Promise.all(ready), slot, U);
    for (const item of s.slots) {
      const card = U.el('figure', null, 'compare-item'), cap = U.el('figcaption'), m = media(record, item, state, placed), open = U.el('button', '⤢', 'compare-zoom');
      card.dataset.slot = item.slot; open.type = 'button'; open.title = '放大对比'; open.setAttribute('aria-label', `放大对比 ${item.slot}`); open.onclick = () => zoom(item.slot);
      cap.append(U.el('b', item.slot, 'compare-letter'), m.size, open);
      card.append(cap, m.holder); grid.append(card); cards.set(item.slot, card); ready.push(m.ready);
    }
    grid.addEventListener('click', event => { const image = event.target.closest('.compare-media img'); if (image) zoom(image.closest('.compare-item').dataset.slot); });
    article.append(grid, together);
    if (answer) {
      for (const letter of v?.picked || []) cards.get(letter)?.classList.add('picked');
      const line = U.el('p', null, 'compare-verdict');
      line.append(U.el('span', U.speaker(answer.audit || {}), 'compare-who'), U.el('strong', verdictText(v, s.pick) || '已答复'));
      if (answer.data.text && answer.data.text !== verdictText(v, s.pick)) line.append(U.el('span', answer.data.text, 'compare-comment'));
      line.append(U.timeEl(answer.created_at));
      grid.before(line);
      reveal(s.slots, U).then(sources => sources.forEach((source, i) => {
        if (!source) return;
        const where = U.el('span', null, 'compare-source'), card = cards.get(s.slots[i].slot);
        if (source.job) {
          const link = U.el('a', `${source.path}`); link.href = HGTarget.url(`/dash/jobs#job=${encodeURIComponent(source.job)}`);
          where.append(link, U.el('span', ` · 任务 ${source.job}`, 'muted'));
        } else where.append(source.name);
        card.querySelector('figcaption').append(where);
      }));
    } else if (record.data.state === 'open') article.append(form(record, s, state, ctx, U));
    return article;
  }

  function form(record, s, state, ctx, U) {
    const box = U.el('form', null, 'compare-answer'), picks = U.el('div', null, 'compare-picks'), diff = U.el('div', null, 'compare-diff');
    const comment = U.el('textarea'), send = U.el('button', '提交', 'ghost primary'), status = U.el('p', null, 'note');
    let picked = [], other = null, degree = null;
    picks.setAttribute('role', 'group'); picks.setAttribute('aria-label', `选 ${s.pick} 个`);
    diff.setAttribute('role', 'group'); diff.setAttribute('aria-label', '差别');
    status.setAttribute('role', 'status');
    comment.rows = 1; comment.maxLength = 16384; comment.placeholder = '补充一句（可选）'; comment.setAttribute('aria-label', '补充');
    send.type = 'submit';
    const choice = (label, onClick, cls = '') => { const b = U.el('button', label, 'chip-btn ' + cls); b.type = 'button'; b.setAttribute('aria-pressed', 'false'); b.onclick = onClick; return b; };
    picks.append(U.el('span', `选 ${s.pick} 个`, 'compare-hint'));
    const letters = s.slots.map(({slot}) => choice(slot, () => {
      other = null;
      picked = picked.includes(slot) ? picked.filter(x => x !== slot) : [...(s.pick === 1 ? [] : picked), slot].slice(-s.pick);
      paint();
    }, 'letter'));
    const same = choice('差不多', () => { other = other === 'same' ? null : 'same'; picked = []; paint(); });
    const unable = choice('无法判断', () => { other = other === 'unable' ? null : 'unable'; picked = []; paint(); });
    picks.append(...letters, U.el('span', null, 'compare-sep'), same, unable);
    diff.append(U.el('span', '差别', 'compare-hint'), ...Object.entries(DIFF).map(([key, label]) => {
      const b = choice(label, () => { degree = degree === key ? null : key; paint(); }); b.dataset.diff = key; return b;
    }));
    const foot = U.el('div', null, 'compare-foot'); foot.append(comment, send);
    box.append(picks, diff, foot, status);
    function ready() { return other !== null || picked.length === s.pick && degree !== null; }
    function paint() {
      letters.forEach((b, i) => b.setAttribute('aria-pressed', String(picked.includes(s.slots[i].slot))));
      same.setAttribute('aria-pressed', String(other === 'same')); unable.setAttribute('aria-pressed', String(other === 'unable'));
      diff.hidden = picked.length === 0;
      for (const b of diff.querySelectorAll('[data-diff]')) b.setAttribute('aria-pressed', String(b.dataset.diff === degree));
      send.disabled = !ready();
    }
    paint();
    comment.addEventListener('input', () => { comment.style.height = 'auto'; comment.style.height = Math.min(comment.scrollHeight + 2, 240) + 'px'; });
    box.onsubmit = async event => {
      event.preventDefault();
      if (!ready() || send.dataset.busy) return;
      if (!ctx.requireWrite(() => box.requestSubmit())) return;
      const display = Object.fromEntries(s.slots.map(({slot}) => [slot, state[slot]]));
      const v = other === 'unable' ? {unable: true} : other === 'same' ? {same: true} : {picked: picked.slice().sort(), difference: degree};
      const evaluation = s.legacy
        ? {schema: 'phonefarm/ab-feedback@1', preference: v.unable ? 'unable_to_judge' : v.same ? 'no_preference' : v.picked[0],
           difference: v.unable ? 'unknown' : v.same ? 'none' : degree,
           reviewed: v.unable ? {A: null, B: null} : {A: true, B: true}, client_display: display, identity_revealed: false}
        : {schema: 'phonefarm/compare-feedback@1', picked: v.picked || [], unable: !!v.unable, difference: v.unable ? null : v.same ? 'none' : degree,
           client_display: display, identity_revealed: false};
      const text = comment.value.trim() || verdictText(v, s.pick);
      send.dataset.busy = '1'; send.disabled = true; send.textContent = '提交中…'; status.textContent = ''; status.classList.remove('task-error');
      try {
        await U.write('feedback', 'fb-' + crypto.randomUUID().replace(/-/g, '').slice(0, 12),
                      {request_id: record.id, request_revision: record.revision, text, supersedes: null, evidence_refs: [], evaluation}, null, null);
        await ctx.reload();
      } catch (error) {
        status.textContent = '没有提交：' + U.failure(error); status.classList.add('task-error');
        delete send.dataset.busy; send.textContent = '提交'; paint();
      }
    };
    return box;
  }

  // The research's comparisons: those waiting for a judgement first (the caller puts them at the top), answered ones apart.
  // On a chain of tasks (`scope`), every task's comparisons, each tagged with its task.
  async function sections(record, ctx, U, scope) {
    if (U.near) near = U.near;
    const requests = (await chainRequests(scope?.ids || [record.id], U)).filter(isComparison);
    // The newest answer is shown when it carries a judgement; otherwise the answers are read one by one as before.
    const answers = await Promise.all(requests.map(r => r.latest_feedback === null ? null
      : verdict(r.latest_feedback?.data?.evaluation) ? r.latest_feedback : load(r, U).catch(() => null)));
    const open = [], done = [];
    requests.forEach((r, i) => {
      const item = block(r, answers[i], ctx, U), mark = scope?.tag(r.research_id);
      if (mark) { item.querySelector('.compare-head').prepend(mark); item.dataset.task = r.research_id; }
      (answers[i] || r.data.state !== 'open' ? done : open).push(item);
    });
    const section = (items, title, id) => {
      if (!items.length) return null;
      const box = U.el('section', null, 'research-section compare-section'), head = U.el('div', null, 'section-head');
      box.id = id; head.append(U.el('h3', title), U.el('span', items.length > 1 ? String(items.length) : '', 'count'));
      box.append(head, ...items); return box;
    };
    return {open: section(open, '待你评', 'task-compare-open'), done: section(done, '对比', 'task-compare-done')};
  }

  async function renderRequest(container, record, ctx, U) {
    if (!isComparison(record)) return;
    const answer = await load(record, U).catch(() => null), box = U.el('section', null, 'research-section compare-section');
    box.append(block(record, answer, ctx, U)); container.append(box);
  }

  window.HGResearchCompare = {sections, renderRequest, isComparison};
})();
