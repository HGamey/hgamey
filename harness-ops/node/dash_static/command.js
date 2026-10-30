// Command view: this node's tasks as trees on a map, its phones and agents in the rail, and a time bar that replays the
// record or follows it live. Data: /dash/api/command (this node, live) or /dash/api/showcase (a read-only snapshot).
import {$, BRAND, DIR_STATE, OUTCOME, WAIT_KIND, agentMeta, agentName, ago, begun, brandOf, clamp, day, deliveredAt, dirAt, el, esc, hm, jobAt, md, mins, phoneKey, short, stamp, waitEnd} from './command-util.js';
import {layout, treeBounds, union} from './command-layout.js';
import {Camera} from './command-camera.js';
import {Timeline} from './command-timeline.js';
import {FAR, TINY, World, tipFor} from './command-world.js';
import {Overlay} from './command-overlay.js';

const Q = new URLSearchParams(location.search);
const app = $('app'), stage = $('stage'), empty = $('empty');
const SOURCES = {live: '/dash/api/command', snapshot: '/dash/api/showcase'};
const POLL_MS = 10000, IDLE_AFTER = 2 * 3600, SPEEDS = [1, 60, 300, 900, 3600];
const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;

// ── Loading
function showEmpty(html) { empty.innerHTML = html; empty.hidden = !html; }
const slow = setTimeout(() => showEmpty('<div><div class="spinner"></div>读取中…</div>'), 300);
async function fetchModel(src) {
  const r = await fetch(SOURCES[src], {credentials: 'same-origin', cache: 'no-store'});
  if (r.status === 401) { location.href = '/dash/login?next=' + encodeURIComponent(location.pathname + location.search); throw new Error('需要登录'); }
  if (!r.ok) { let msg = `HTTP ${r.status}`; try { msg = (await r.json()).error || msg; } catch (_) {} const e = new Error(msg); e.status = r.status; throw e; }
  return r.json();
}
// A chain of tasks (each `follows` the one before) is one tree under its first task: its runs, directions and notes
// hang there, and the latest step carries the state and the owner. Every step's delivery stays on the record.
function normalize(raw, src) {
  const all = raw.research || [], byId = new Map(all.map(t => [t.id, t])), rootOf = new Map();
  const earlier = (a, b) => (byId.get(a).created_at || 0) - (byId.get(b).created_at || 0) || (a < b ? -1 : 1);
  const root = id => {
    const seen = [];
    while (!seen.includes(id)) { seen.push(id); if (!byId.has(byId.get(id).follows)) return id; id = byId.get(id).follows; }
    return seen.slice(seen.indexOf(id)).sort(earlier)[0];   // a cycle: its earliest task stands as the root
  };
  for (const t of all) rootOf.set(t.id, root(t.id));
  const tasks = all.filter(t => rootOf.get(t.id) === t.id).map(t => {
    const steps = all.filter(m => rootOf.get(m.id) === t.id).sort((a, b) => earlier(a.id, b.id)), last = steps[steps.length - 1];
    return steps.length > 1 ? {...t, state: last.state, updated_at: last.updated_at, owner: last.owner, steps} : t;
  });
  const onTree = x => rootOf.has(x.research_id), lift = x => ({...x, research_id: rootOf.get(x.research_id)});
  const jobs = (raw.jobs || []).filter(onTree).map(lift);
  const agents = [...new Set([...(raw.agents || []), ...jobs.map(j => j.agent).filter(Boolean)])];
  return {src, at: raw.at, source: raw.source, history: raw.history || null, agents, tasks, jobs,
    dirs: (raw.directions || []).filter(onTree).map(lift), notes: (raw.notes || []).filter(n => onTree(n) && n.at).map(lift),
    phones: (raw.phones || []).map(p => ({...p, key: phoneKey(p.brand)})), phonesAt: raw.phones_sampled_at || raw.at};
}

// ── State
const S = {src: null, M: null, lay: null, events: [], times: [], T0: 0, T1: 0, now: 0, idx: -1, playing: true, live: false, speed: 900,
  follow: true, range: Q.get('range') || 'all', selected: null, mark: null, panel: null, offscreen: 0, skew: 0, lastOk: 0, misses: 0, hasSnapshot: false};
const cam = new Camera(stage, $('world'), {onChange: () => { minimap(); zoomClass(); S.camMoved = true; }, onUser: () => setFollow(false)});
function zoomClass() {
  const w = $('world'), k = Math.round(cam.k * 100) / 100;
  if (w.dataset.k === String(k)) return;
  w.dataset.k = k; w.style.setProperty('--inv', (1 / k).toFixed(3)); w.classList.toggle('far', k < FAR); w.classList.toggle('tiny', k < TINY);
}
const world = new World($('world'));
const overlay = new Overlay($('overlay'), cam, world);
const timeline = new Timeline({onSeek: (t, final) => { setLive(false); seek(t); if (final) syncUrl(); }, onRange: (d0, d1, mode) => { S.range = mode; $('t-range').value = mode === 'custom' ? 'custom' : mode; afterRange(); }});

function buildEvents(M) {
  const ev = [], add = (at, type, x) => { if (at) ev.push({at, type, ...x}); };
  for (const t of M.tasks) { add(t.created_at, 'task', {tree: t.id, t}); for (const s of t.steps || [t]) if (s.state === 'delivered') add(s.updated_at, 'deliver', {tree: t.id, t}); }
  for (const d of M.dirs) { add(d.created_at, 'dir', {tree: d.research_id, d}); if (['verified', 'rejected'].includes(d.state) && d.updated_at > d.created_at + 1) add(d.updated_at, 'dirstate', {tree: d.research_id, d}); }
  for (const j of M.jobs) {
    if (j.phone && j.queued && waitEnd(j) - j.queued > 30) add(j.queued, 'wait', {tree: j.research_id, j, who: j.agent});
    add(begun(j), 'start', {tree: j.research_id, j, who: j.agent});
    add(j.ended, 'end', {tree: j.research_id, j, who: j.agent});
  }
  for (const n of M.notes) add(n.at, 'note', {tree: n.research_id, n, who: n.who});
  return ev.sort((a, b) => a.at - b.at);
}
const upper = (arr, t) => { let lo = 0, hi = arr.length; while (lo < hi) { const m = (lo + hi) >> 1; if (arr[m] <= t) lo = m + 1; else hi = m; } return lo; };

function adopt(raw, src, keepCols) {
  const M = normalize(raw, src), first = S.M === null || S.src !== src, prevAt = first ? null : S.M.at;
  S.M = M; S.src = src;
  S.lay = layout(M, Math.max(.6, cam.w / Math.max(1, cam.h)), first || !keepCols ? 0 : S.lay.cols);
  world.build(S.lay);
  S.events = buildEvents(M); S.times = S.events.map(e => e.at);
  // What arrived since the last model counts as new (it may be a few seconds older than now).
  S.idx = prevAt ? upper(S.times, prevAt) : -1; S.feedIdx = prevAt ? S.idx : null; overlay.dirty = true;
  const firstAt = Math.min(...M.tasks.map(t => t.created_at), raw.at);
  S.T0 = firstAt - 600; S.T1 = src === 'live' ? Math.max(raw.at, Date.now() / 1000 - S.skew) : raw.at;
  overlay.setAgents(M.agents);
  cam.extent = union([...S.lay.trees.values()].map(treeBounds)) || {x0: 0, y0: 0, x1: 1000, y1: 600};
  cam.kMin = Math.min(.5, cam.viewFor(cam.extent, {maxK: 1}).k * .8);
  timeline.setData({ends: M.jobs.filter(j => j.ended).map(j => ({at: j.ended, ok: j.outcome === 'succeeded'})), people: M.notes.filter(n => n.who === 'human').map(n => n.at),
    delivered: M.tasks.flatMap(t => t.steps || [t]).filter(t => t.state === 'delivered').map(t => t.updated_at)}, S.T0, S.T1);
  rosterRows(); phoneCards();
  $('source').textContent = src === 'live' ? `${M.source || '本节点'} · 实时数据` : `快照 · ${M.source || '?'} · 截至 ${stamp(raw.at)}`;
  $('t-live').disabled = src !== 'live'; $('t-live').title = src === 'live' ? '直播：跟着本节点实时走（L）' : '快照不是实时数据；切到「本节点」可以看直播';
  sourceSwitch();
  if (!M.tasks.length) showEmpty(`<div>${src === 'live' ? '这个节点还没有任务。' : '快照里没有任务。'}</div>`); else showEmpty('');
}

// ── Time
function rangeBounds(mode) {
  const T1 = S.T1;
  if (mode === '24') return [T1 - 86400, T1];
  if (mode === '6') return [T1 - 21600, T1];
  if (mode === 'today') { const d = new Date(T1 * 1000); d.setHours(0, 0, 0, 0); return [d.getTime() / 1000, T1]; }
  return [S.T0, T1];
}
function afterRange() {
  if (S.range !== 'custom') timeline.setRange(...rangeBounds(S.range));
  if (S.now < timeline.d0 || S.now > timeline.d1) { setLive(false); seek(timeline.d0); }
  rosterHeat(); syncUrl();
}
function seek(t) {
  S.now = clamp(t, S.T0, S.T1);
  overlay.hush(); refresh(false); timeline.setNow(S.now, S.live); status();
}
function setPlaying(on) {
  if (on && !S.live && S.now >= timeline.d1 - 1) seek(timeline.d0);
  S.playing = on; const b = $('t-play'); b.textContent = on ? '❚❚' : '▶'; b.setAttribute('aria-label', on ? '暂停' : '播放');
  status();
}
function setLive(on) {
  if (on && S.src !== 'live') return;
  if (S.live === on) return;
  S.live = on; $('t-live').setAttribute('aria-pressed', String(on));
  if (!on) setPlaying(false);   // leaving live holds the moment; play replays forward from there
  if (on) {
    S.T1 = Math.max(S.T1, wallNow()); if (S.range === 'custom') { S.range = 'all'; $('t-range').value = 'all'; }
    timeline.setRange(...rangeBounds(S.range)); seek(S.T1); setPlaying(true); setFollow(true); poll(true);
  }
  status();
}
const wallNow = () => Date.now() / 1000 - S.skew;

// ── Applying the time: trees, rail, counts, pins. `animate` only for forward play.
let lastEvents = [];
function refresh(animate) {
  const t = S.now, idx = upper(S.times, t), fresh = animate && S.idx >= 0 && idx > S.idx ? S.events.slice(S.idx, idx) : [];
  const out = world.show(t, animate);
  S.idx = idx; S.busy = out.busy;
  const M = S.M, where = new Map(), doing = new Map(), waiting = [];
  for (const a of M.agents) {
    let best = null;
    for (const j of M.jobs) {
      if (j.agent !== a) continue;
      const s = jobAt(j, t);
      if (s === 'running' && (!best || best.s !== 'running' || begun(j) > begun(best.j))) best = {s, j};
      else if (s === 'waiting' && (!best || best.s !== 'running')) best = {s, j};
    }
    if (best?.s === 'running') { where.set(a, best.j.research_id); doing.set(a, best.j.phone ? `在${brandOf(best.j)}上 · ${best.j.kind}` : best.j.kind); }
    else if (best?.s === 'waiting') { where.set(a, best.j.research_id); doing.set(a, `等手机 · ${ago(t - best.j.queued)}`); waiting.push({a, j: best.j}); }
    else {
      const last = S.events.slice(Math.max(0, idx - 400), idx).reverse().find(e => e.who === a && e.tree);
      if (last && t - last.at < IDLE_AFTER) { where.set(a, last.tree); doing.set(a, last.type === 'note' ? '写记录' : '整理结果'); }
    }
  }
  overlay.place(where);
  S.where = where; S.doing = doing; S.waiting = waiting;
  S.asks = M.notes.filter(n => n.ask && n.at <= t && (() => { const r = M.notes.find(x => x.reply_to === n.id && x.who === 'human'); return r ? r.at > t : n.open; })());
  overlay.setPins(S.asks, n => select(n.research_id, {notes: true}));
  overlay.dirty = true;
  for (const e of fresh) {
    if (e.type === 'note') overlay.say(e.n, e.tree);
    if (e.tree && e.type !== 'end') S.focus = e.tree;
    if (!S.follow && e.tree && !onScreen(e.tree)) S.offscreen++;
  }
  if (!animate && !S.focus) S.focus = lastTreeBefore(idx);
  counts(out); roster(); phones(); asks(); feed(idx, fresh.length && fresh.length < 20); clock(); followButton(); minimap();
  if (M.tasks.length) showEmpty(out.shown || (S.playing && !S.live) ? '' : `<div>${stamp(t)} 还没有任务<small>${stamp(M.tasks.reduce((a, x) => Math.min(a, x.created_at), Infinity))} 出现第一个任务</small></div>`);
  if (S.panel === 'task') panelTask(true); else if (S.panel === 'wins') panelWins();
}
const lastTreeBefore = idx => S.events.slice(0, idx).reverse().find(e => e.tree && e.type !== 'end')?.tree;
function onScreen(id) { const tr = S.lay.trees.get(id); return tr ? cam.seen(treeBounds(tr)).inside > .5 : true; }

// ── Rail
function rosterRows() {
  const box = $('roster'); box.innerHTML = '';
  $('agents-n').textContent = `${S.M.agents.length} 个`;
  for (const name of S.M.agents) {
    const m = agentMeta(name), li = el('li', '', `<span class="av">${esc(m.s)}</span><div class="who"><b>${esc(m.n)}</b><span>待命</span></div><span class="n">0<small>次</small></span><div class="heat">${'<i></i>'.repeat(48)}</div>`);
    li.style.setProperty('--c', m.c); li.dataset.agent = name; box.append(li);
  }
  rosterHeat();
}
function roster() {
  for (const li of $('roster').children) {
    const a = li.dataset.agent, n = S.M.jobs.filter(j => j.agent === a && j.ended && j.ended <= S.now).length;
    li.querySelector('.who span').textContent = S.doing?.get(a) || '待命';
    li.querySelector('.n').firstChild.textContent = n;
    li.classList.toggle('busy', S.doing?.has(a));
  }
  if (Math.abs((S.heatAt || 0) - S.now) > (timeline.d1 - timeline.d0) / 48) rosterHeat();
}
function rosterHeat() {
  S.heatAt = S.now;
  const d0 = timeline.d0, d1 = timeline.d1, cell = (d1 - d0) / 48;
  for (const li of $('roster').children) {
    const a = li.dataset.agent, c = agentMeta(a).c, cells = li.querySelectorAll('.heat i');
    const jobs = S.M.jobs.filter(j => j.agent === a && begun(j)), notes = S.M.notes.filter(n => n.who === a);
    for (let i = 0; i < 48; i++) {
      const c0 = d0 + i * cell, c1 = Math.min(S.now, c0 + cell);
      if (c1 <= c0) { cells[i].style.background = ''; continue; }
      const busy = jobs.reduce((s, j) => s + Math.max(0, Math.min(c1, j.ended || S.now) - Math.max(c0, begun(j))), 0) / cell;
      const f = Math.min(1, busy + (notes.some(n => n.at >= c0 && n.at < c1) ? .25 : 0));
      cells[i].style.background = f ? `color-mix(in srgb, ${c} ${Math.round(18 + f * 82)}%, transparent)` : '';
    }
  }
}
const phoneOf = j => j.device && S.M.phones.find(p => p.key === phoneKey(j.device) || p.serial === j.device);
function phoneCards() {
  const box = $('phones'); box.innerHTML = '';
  $('phones-n').textContent = `${S.M.phones.length} 台`;
  for (const p of S.M.phones) {
    const li = el('li', '', `<b>${esc(BRAND[p.key] || p.brand || '?')}<small>${esc(p.model || '')}</small></b><span class="temp"></span><span class="doing">空闲</span><span class="bar"><i></i></span>`);
    li.dataset.key = p.key; box.append(li);
  }
  const ghost = el('li', 'ghost', '<span>＋</span>'); ghost.hidden = true; ghost.id = 'ghost'; box.append(ghost);
}
function phones() {
  const t = S.now, fresh = S.live || t >= S.T1 - 900;
  let busy = 0;
  for (const li of $('phones').querySelectorAll('li[data-key]')) {
    const p = S.M.phones.find(x => x.key === li.dataset.key), job = S.M.jobs.find(j => j.phone && phoneOf(j) === p && jobAt(j, t) === 'running');
    const temp = p.temp_c ?? p.soc_c;   // battery temperature for every phone (SoC is missing on some)
    const sampled = S.live ? Date.now() / 1000 - (S.M.phonesAt || S.M.at) : S.T1 - (S.M.phonesAt || S.M.at), old = sampled > 600;
    li.querySelector('.temp').innerHTML = fresh && temp ? `${temp.toFixed(0)}°C${old ? `<small>${esc(ago(sampled))}前</small>` : ''}` : '';
    li.classList.toggle('old', fresh && old);
    li.classList.toggle('warm', fresh && temp >= 40);
    li.classList.toggle('busy', !!job); if (job) busy++;
    li.querySelector('.doing').textContent = job ? `${agentName(job.agent)} · ${job.kind}` : '空闲';
    li.querySelector('.bar i').style.width = job?.ended ? clamp((t - begun(job)) / (job.ended - begun(job)) * 100, 0, 100) + '%' : job ? '100%' : '0';
    li.title = job ? `${agentName(job.agent)} · ${job.kind} · 从 ${hm(begun(job))} 开始` : '空闲';
  }
  S.phonesBusy = busy;
  const q = $('queue'), w = S.waiting || [];
  q.hidden = !w.length; $('ghost').hidden = !w.length;
  q.innerHTML = w.map(({a, j}) => {
    const m = agentMeta(a), held = (j.waits || []).filter(x => x.at <= t).at(-1), by = held?.blocking_jobs?.map(id => S.M.jobs.find(x => x.id === id)).filter(Boolean)[0];
    return `<span class="av" style="--c:${m.c}" title="${esc(m.n)}">${esc(m.s)}</span><span>${esc(m.n)} ${esc(WAIT_KIND[held?.kind] || '等手机')}${by ? `（${esc(agentName(by.agent))} 的${esc(by.kind)}在用）` : ''} · ${ago(t - j.queued)}</span>`;
  }).join('');
}
function asks() {
  const box = $('asks'), list = S.asks || [];
  box.closest('.block').hidden = !list.length;
  $('asks-n').textContent = list.length ? `${list.length} 条` : '';
  const key = list.map(n => n.id).join();
  if (box.dataset.key === key) return;
  box.dataset.key = key;
  box.innerHTML = '';
  for (const n of list) {
    const m = agentMeta(n.who), li = el('li', '', `<span class="av" style="--c:${m.c}">${esc(m.s)}</span><div><b>${esc(m.n)}</b><time>${hm(n.at)}</time><div class="txt">${esc(n.text)}</div></div>`);
    li.tabIndex = 0; li.addEventListener('click', () => select(n.research_id, {notes: true})); li.addEventListener('keydown', e => { if (e.key === 'Enter') select(n.research_id, {notes: true}); });
    box.append(li);
  }
}
const FEED_N = 40;
function feedLine(e) {
  const who = e.who === 'human' ? '人' : agentName(e.who);
  switch (e.type) {
    case 'task': return `<b>新任务</b> ${esc(short(e.t.title))}`;
    case 'deliver': return `<b>交付</b> ${esc(short(e.t.title))}`;
    case 'dir': return `<b>新方向</b> ${esc(short(e.d.title, 40))}`;
    case 'dirstate': return `<b>${esc(DIR_STATE[e.d.state])}</b> ${esc(short(e.d.title, 40))}`;
    case 'wait': return `<b>${esc(who)}</b> 排队等手机${e.j.device ? `（${esc(brandOf(e.j))}）` : ''} · ${esc(e.j.kind)}`;
    case 'start': return e.j.phone ? `<b>${esc(who)}</b> 在${esc(brandOf(e.j))}上开始${esc(e.j.kind)}` : null;
    case 'end': return e.j.phone ? `<b>${esc(e.j.kind)}</b> ${OUTCOME[e.j.outcome] || '结束'} · ${esc(brandOf(e.j))} · ${mins(e.j) ?? '?'} 分钟` : null;
    case 'note': return `<b>${esc(who)}</b> ${esc(e.n.text.slice(0, 90))}`;
  }
  return null;
}
function feedItem(e, isNew) {
  const html = feedLine(e); if (!html) return null;
  const li = el('li', (e.who === 'human' ? 'human' : '') + (isNew ? ' new' : ''), `<time>${hm(e.at)}</time><i></i><div class="txt">${html}</div>`);
  li.style.setProperty('--c', e.who === 'human' ? '#f0c36e' : e.who ? agentMeta(e.who).c : '#8aa1b5');
  if (e.tree) { li.dataset.tree = e.tree; li.tabIndex = 0; }
  return li;
}
function feed(idx, incremental) {
  const box = $('feed');
  if (incremental && S.feedIdx != null && idx >= S.feedIdx) {
    for (const e of S.events.slice(S.feedIdx, idx)) { const li = feedItem(e, true); if (li) box.prepend(li); }
    while (box.children.length > FEED_N) box.lastChild.remove();
  } else if (S.feedIdx !== idx) {
    box.innerHTML = '';
    for (let i = idx - 1, n = 0; i >= 0 && n < FEED_N; i--) { const li = feedItem(S.events[i], false); if (li) { box.append(li); n++; } }
  }
  S.feedIdx = idx;
}
$('feed').addEventListener('click', e => { const li = e.target.closest('li[data-tree]'); if (li) select(li.dataset.tree); });
function clock() {
  const sec = Math.floor(S.now);
  if (sec === S.clockAt) return; S.clockAt = sec;
  $('clock').textContent = hm(S.now); $('date').textContent = day(S.now);
}

// ── Top bar
function counts(out) {
  const M = S.M, t = S.now, shown = M.tasks.filter(x => x.created_at <= t), delivered = shown.filter(x => deliveredAt(x, t)).length;
  const runs = M.jobs.filter(j => j.ended && j.ended <= t).length;
  const wait = (S.waiting || []).length, ask = (S.asks || []).length;
  const items = [['tasks', '任务', shown.length, '#8aa1b5'], ['active', '进行中', shown.length - delivered, '#5ccfc0'], ['done', '已交付', delivered, '#f0c36e'], ['runs', '实验', runs, '#e3eaf0'],
    ...(wait ? [['wait', '等手机', wait, '#8db4f5']] : []), ...(ask ? [['ask', '等你回复', ask, '#f0c36e']] : [])];
  const html = items.map(([key, k, v, c]) => `<span data-k="${key}" class="${key === 'wait' || key === 'ask' ? 'alert' : ''}" style="--c:${c}"><i></i>${k}<b>${v}</b></span>`).join('');
  if ($('counts').innerHTML !== html) $('counts').innerHTML = html;
}
function status() {
  const box = $('status'), age = (Date.now() - S.lastOk) / 1000, stale = S.src === 'live' && S.misses > 0 && age > 25;
  let text, warn = false;
  if (S.error) { text = S.error; warn = true; }
  else if (S.live) { text = S.lastOk ? `直播 · ${age < 15 ? '刚刚更新' : `${ago(age)}前更新`}` : '直播 · 连接中'; warn = stale; }
  else {
    const behind = S.T1 - S.now, end = S.src === 'live' ? '现在' : '快照末尾';
    text = behind <= 60 && !S.playing ? `回放已到${end}` : `${S.playing ? `回放 ${S.speed}×` : '已暂停'}${behind > 60 ? ` · 比${end}早 ${ago(behind)}` : ''}`;
  }
  if (box.textContent !== text) box.textContent = text;
  box.classList.toggle('warn', warn);
  // The top bar always says what is on screen and how old it is, also with the rail collapsed.
  const mode = $('mode');
  let cls, head, tail;
  if (stale) { cls = 'stale'; head = '连接中断'; tail = `显示 ${S.lastOk ? new Date(S.lastOk).toLocaleTimeString('zh-CN', {hour12: false}) : '之前'} 的数据 · 重试中`; }
  else if (S.live) { cls = 'live'; head = '直播'; tail = age < 15 ? '刚刚更新' : `${ago(age)}前更新`; }
  else if (S.src === 'snapshot') { cls = 'snap'; head = '快照'; tail = `${S.M.source || ''} · ${ago(Date.now() / 1000 - S.M.at)}前的数据`; }
  else { cls = 'replay'; head = '回放'; tail = S.T1 - S.now > 60 ? `比现在早 ${ago(S.T1 - S.now)}` : '已到现在'; }
  const html = `<i></i><b>${head}</b><small>${esc(tail)}</small>`;
  if (mode.innerHTML !== html) mode.innerHTML = html;
  mode.className = 'mode ' + cls;
  $('t-live').classList.toggle('stale', stale);
}
function followButton() {
  const b = $('b-follow');
  b.setAttribute('aria-pressed', String(S.follow));
  b.textContent = S.follow ? '跟随中' : S.offscreen ? `恢复跟随 · ${S.offscreen}` : '恢复跟随';
}
function setFollow(on) {
  if (S.follow === on) return;
  S.follow = on; S.offscreen = 0; S.followAt = 0; followButton(); minimap();
}
function sourceSwitch() {
  const box = $('src');
  box.hidden = !S.hasSnapshot;
  for (const b of box.querySelectorAll('button')) b.setAttribute('aria-pressed', String(b.dataset.src === S.src));
}

// ── Camera framing
const allBounds = () => union([...world.trees.values()].filter(t => t.here).map(t => treeBounds(t.shape))) || cam.extent;
function fitAll(ms) { const b = allBounds(); if (b) return ms === 0 ? cam.set(cam.viewFor(b, {maxK: 1})) : cam.fly(b, {maxK: 1, ms}); }
function frameTree(id, ms) { const tr = S.lay.trees.get(id); if (tr) return ms === 0 ? cam.set(cam.viewFor(treeBounds(tr))) : cam.fly(treeBounds(tr), {ms}); }
function followStep() {
  if (!S.follow || cam.moving || !S.focus || performance.now() - (S.followAt || 0) < 3500) return;
  const tr = S.lay.trees.get(S.focus); if (!tr || !world.trees.get(S.focus)?.here) return;
  let b = treeBounds(tr);
  const busy = [...(S.busy || [])].map(id => S.lay.trees.get(id)).filter(Boolean).map(treeBounds), together = union([b, ...busy]);
  if (busy.length && cam.viewFor(together, {maxK: 1}).k >= .42) b = together;
  const s = cam.seen(b, {maxK: 1});
  if (s.inside > .92 && s.zoom < 1.9) return;
  S.followAt = performance.now(); cam.fly(b, {maxK: 1});
}
function minimap() {
  const svg = $('minimap'), e = cam.extent; if (!e || !S.lay) return;
  const pad = 200, vb = [e.x0 - pad, e.y0 - pad, e.x1 - e.x0 + 2 * pad, e.y1 - e.y0 + 2 * pad], v = cam.view();
  const whole = v.x0 <= e.x0 && v.y0 <= e.y0 && v.x1 >= e.x1 && v.y1 >= e.y1;
  svg.classList.toggle('off', whole || S.follow);   // a navigation aid for free movement; following needs none
  if (whole) return;
  const aspect = vb[2] / vb[3]; svg.style.width = (aspect >= 1.4 ? 168 : Math.round(108 * aspect)) + 'px'; svg.style.height = (aspect >= 1.4 ? Math.round(168 / aspect) : 108) + 'px';
  svg.setAttribute('viewBox', vb.join(' '));
  let out = '';
  for (const tr of world.trees.values()) {
    if (!tr.here) continue;
    const b = treeBounds(tr.shape), cls = `t${tr.busy ? ' busy' : tr.delivered ? ' delivered' : ''}${tr.id === S.selected ? ' selected' : ''}`;
    out += `<rect class="${cls}" x="${b.x0}" y="${b.y0}" width="${b.x1 - b.x0}" height="${b.y1 - b.y0}" rx="40"/>`;
  }
  out += `<rect class="view" x="${v.x0}" y="${v.y0}" width="${v.x1 - v.x0}" height="${v.y1 - v.y0}"/>`;
  svg.innerHTML = out;
}
{
  const svg = $('minimap'); let dragging = false;
  const go = (e, animate) => {
    const r = svg.getBoundingClientRect(), vb = svg.viewBox.baseVal; if (!vb?.width) return;
    const s = Math.max(vb.width / r.width, vb.height / r.height), ox = (r.width - vb.width / s) / 2, oy = (r.height - vb.height / s) / 2;
    const wx = vb.x + (e.clientX - r.left - ox) * s, wy = vb.y + (e.clientY - r.top - oy) * s;
    setFollow(false);
    if (animate) cam.fly({k: cam.k, cx: wx, cy: wy}, {ms: 400}); else cam.set({k: cam.k, cx: wx, cy: wy});
  };
  svg.addEventListener('pointerdown', e => { dragging = true; svg.setPointerCapture(e.pointerId); go(e, true); e.stopPropagation(); });
  svg.addEventListener('pointermove', e => { if (dragging) go(e, false); });
  svg.addEventListener('pointerup', () => { dragging = false; });
}

// ── Selection and the detail panel
// `mark` is what was clicked inside the tree ('j:<job>' a run, 'g:<direction>' or 'g:trunk' a chip): its row in the
// panel is scrolled to and highlighted, and it stays marked on the map.
function select(id, {notes = false, fly = true, mark = null} = {}) {
  if (!S.lay.trees.has(id)) return;
  if (!S.selected && S.panel !== 'task') S.camBefore = {k: cam.k, cx: (cam.w / 2 - cam.x) / cam.k, cy: (cam.h / 2 - cam.y) / cam.k};
  S.selected = id; S.mark = mark; world.select(id); world.mark(id, mark); setFollow(false);
  openPanel('task');
  if (notes) requestAnimationFrame(() => $('panel-notes')?.scrollIntoView({block: 'start'}));
  else if (mark) requestAnimationFrame(() => {
    const row = $('panel-body').querySelector('.hit'); if (!row) return;
    row.scrollIntoView({block: 'center', behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'});
    row.classList.remove('flash'); void row.offsetWidth; row.classList.add('flash');
  });
  if (fly) requestAnimationFrame(() => requestAnimationFrame(() => { const tr = S.lay.trees.get(id), s = cam.seen(treeBounds(tr)); if (s.inside < .9 || s.zoom > 2.5) frameTree(id); }));
  syncUrl();
}
function openPanel(kind) {
  // On narrower screens the panel takes the rail's room rather than the map's; the rail comes back when it closes.
  if (!S.panel && innerWidth < 1600 && !app.classList.contains('rail-collapsed')) { toggleRail(false); S.railAuto = true; }
  S.panel = kind; $('panel').hidden = false; $('b-wins').setAttribute('aria-pressed', String(kind === 'wins'));
  if (kind === 'task') panelTask(); else panelWins();
}
function closePanel() {
  if (!S.panel) return false;
  const was = S.panel; S.panel = null; $('panel').hidden = true; $('b-wins').setAttribute('aria-pressed', 'false');
  if (S.railAuto) { S.railAuto = false; toggleRail(true); }
  if (was === 'task') { S.selected = S.mark = null; world.select(null); world.mark(null); if (S.camBefore) { const v = S.camBefore; S.camBefore = null; requestAnimationFrame(() => cam.fly(v, {ms: 500})); } }
  syncUrl(); minimap();
  return true;
}
function panelTask(update) {
  const id = S.selected, tr = S.lay.trees.get(id); if (!tr) return closePanel();
  const t = S.now, task = tr.task, M = S.M, live = S.src === 'live';
  const jobs = M.jobs.filter(j => j.research_id === id && jobAt(j, t) && jobAt(j, t) !== 'waiting').sort((a, b) => begun(b) - begun(a));
  const ok = jobs.filter(j => jobAt(j, t) === 'succeeded').length, dirs = M.dirs.filter(d => d.research_id === id && d.created_at <= t);
  const notes = M.notes.filter(n => n.research_id === id && n.at <= t).sort((a, b) => b.at - a.at), delivered = deliveredAt(task, t);
  const key = `${id}|${S.idx}|${S.mark}`; if (update && $('panel-body').dataset.key === key) return;
  // The 30 newest runs show; a marked run further down shows them all.
  const marked = S.mark?.startsWith('j:') ? jobs.findIndex(j => 'j:' + j.id === S.mark) : -1, upto = marked >= 30 ? jobs.length : 30;
  $('panel-body').dataset.key = key;
  $('panel-title').textContent = short(task.title, 24);
  const jobRow = j => {
    const s = jobAt(j, t), name = `${esc(j.kind)} · ${esc(brandOf(j))}`;
    return `<li class="${s === 'succeeded' ? 'ok' : ''}${S.mark === 'j:' + j.id ? ' hit' : ''}" data-job="${esc(j.id)}">${live ? `<a href="/dash/jobs#job=${encodeURIComponent(j.id)}">${name}</a>` : `<span>${name}</span>`}<span class="state">${s === 'running' ? '进行中' : OUTCOME[s] || s}</span><small>${esc(agentName(j.agent))} · ${stamp(begun(j))}${s !== 'running' && mins(j) ? ` · ${mins(j)} 分钟` : ''}${j.claimed && j.queued && j.claimed - j.queued > 30 ? ` · 排队 ${ago(j.claimed - j.queued)}` : ''}</small></li>`;
  };
  $('panel-body').innerHTML = `
    <div class="title">${esc(task.title)}</div>
    <div class="meta"><span class="tag ${delivered ? 'delivered' : 'active'}">${delivered ? '已交付' : '进行中'}</span><span>${esc(agentName(task.owner))}</span><span class="mono">${stamp(task.created_at)} 起</span></div>
    ${live ? `<a class="button open" href="/dash/research?kind=research&id=${encodeURIComponent(id)}">打开任务</a>` : ''}
    <h3>概况</h3>
    <div class="stats"><div><b>${jobs.length}</b><span>次实验</span></div><div><b>${ok}</b><span>成功</span></div>
      <div class="${dirs.some(d => dirAt(d, t) === 'verified') ? 'gold' : ''}"><b>${dirs.filter(d => dirAt(d, t) === 'verified').length}/${dirs.length}</b><span>方向验证有效</span></div><div><b>${jobs.filter(j => j.phone).length}</b><span>次真机</span></div></div>
    ${dirs.length ? `<h3>方向<span>${dirs.length}</span></h3><ul class="list">${dirs.map(d => { const s = dirAt(d, t), n = M.jobs.filter(j => j.direction_id === d.id && jobAt(j, t) && jobAt(j, t) !== 'waiting').length; return `<li class="${s === 'verified' ? 'verified' : ''}${S.mark === 'g:' + d.id ? ' hit' : ''}" data-dir="${esc(d.id)}"><span>${esc(d.title)}</span><span class="state">${esc(DIR_STATE[s] || s)}</span><small>${n} 次实验 · ${stamp(d.created_at)}</small></li>`; }).join('')}</ul>` : ''}
    ${jobs.length ? `<h3 class="${S.mark === 'g:trunk' ? 'hit' : ''}">实验<span>${jobs.length > upto ? `最近 ${upto} / ${jobs.length}` : jobs.length}</span></h3><ul class="list">${jobs.slice(0, upto).map(jobRow).join('')}</ul>` : ''}
    ${notes.length ? `<h3 id="panel-notes">记录<span>${notes.length}</span></h3><ul class="list">${notes.slice(0, 40).map(n => `<li class="note${n.who === 'human' ? ' human' : ''}${(S.asks || []).some(a => a.id === n.id) ? ' ask' : ''}"><div><b>${esc(agentName(n.who))}</b><span class="mono">${stamp(n.at)}</span></div><div>${esc(n.text)}</div></li>`).join('')}</ul>` : ''}`;
}
function panelWins() {
  const M = S.M, t = S.now, done = M.tasks.filter(r => deliveredAt(r, t)).sort((a, b) => b.updated_at - a.updated_at);
  const key = `wins|${S.idx}`; if ($('panel-body').dataset.key === key) return; $('panel-body').dataset.key = key;
  const phone = M.jobs.filter(j => j.phone && j.ended && j.ended <= t).length, humans = M.notes.filter(n => n.who === 'human' && n.at <= t).length;
  const adopted = M.notes.filter(n => n.decision === 'adopt' && n.at <= t).length, verified = M.dirs.filter(d => d.state === 'verified' && d.updated_at <= t).length, h = M.history;
  $('panel-title').textContent = `成果 · 截至 ${stamp(t)}`;
  $('panel-body').innerHTML = `<div class="hero"><b>${done.length}</b><span>份交付报告</span></div>
    <h3>过程</h3><div class="stats"><div class="gold"><b>${verified}</b><span>验证有效的方向</span></div><div><b>${phone}</b><span>次真机实验</span></div>
      <div><b>${humans}</b><span>次人的回复</span></div><div><b>${adopted}</b><span>条采纳记录</span></div></div>
    ${done.length ? `<h3>交付<span>${done.length}</span></h3><ul class="list">${done.map(r => `<li data-tree="${esc(r.id)}"><a href="#" data-tree="${esc(r.id)}">${esc(short(r.title, 44))}</a><span class="state">${md(r.updated_at)}</span></li>`).join('')}</ul>` : ''}
    ${h ? `<h3>来历<span>${h.since ? `自 ${esc(h.since)}` : ''}</span></h3><div class="stats"><div><b>${Object.values(h.commits || {}).reduce((a, b) => a + b, 0).toLocaleString('en-US')}</b><span>次提交</span></div>
      <div><b>${(h.experiment_lines || []).length}</b><span>条实验线</span></div><div class="gold"><b>${h.sr_networks_kept ?? '?'}</b><span>个超分网络（从 ${h.sr_networks ?? '?'} 个里留下）</span></div></div>` : ''}`;
}
$('panel-body').addEventListener('click', e => { const a = e.target.closest('a[data-tree]'); if (a) { e.preventDefault(); select(a.dataset.tree); } });
$('panel-close').addEventListener('click', closePanel);
$('b-wins').addEventListener('click', () => { if (S.panel === 'wins') closePanel(); else { if (S.panel === 'task') { S.selected = S.mark = null; world.select(null); world.mark(null); } openPanel('wins'); } });

// ── Pointer on the map: hover tips and selection
const tip = $('tip');
function showTip(e, html) {
  if (!html) { tip.hidden = true; return; }
  tip.innerHTML = html; tip.hidden = false;
  // Above the pointer by default: a task's title sits below its orb, and the tip should not hide it.
  const r = e.target?.getBoundingClientRect?.(), cx = e.clientX ?? (r ? r.left + r.width / 2 : 0), cy = e.clientY ?? (r ? r.top : 0);
  const w = tip.offsetWidth, h = tip.offsetHeight, x = clamp(cx + 14, 8, innerWidth - w - 8), y = cy - h - 14 >= 8 ? cy - h - 14 : Math.min(innerHeight - h - 8, cy + 18);
  tip.style.left = x + 'px'; tip.style.top = y + 'px';
}
function tipHtml(node) {
  const tr = world.trees.get(node.dataset.tree); if (!tr) return '';
  if (node.dataset.kind === 'root') return tipFor.root(tr);
  const part = tr.parts.get((node.dataset.kind === 'chip' ? 'g:' : 'j:') + node.dataset.key); if (!part) return '';
  if (node.dataset.kind === 'chip') return tipFor.chip(tr, part);
  const j = part.j, s = jobAt(j, S.now), waited = j.claimed && j.queued && j.claimed - j.queued > 30;
  return `<b>${esc(j.kind)} · ${s === 'running' ? '进行中' : OUTCOME[s] || s}</b>${esc(short(tr.shape.task.title))}<br><span class="mono">${esc(agentName(j.agent))} · ${esc(brandOf(j))} · ${hm(begun(j))}${j.ended && s !== 'running' ? `–${hm(j.ended)} · ${mins(j)} 分钟` : ''}${waited ? ` · 排队 ${ago(j.claimed - j.queued)}` : ''}</span>`;
}
// Tabbing to a task brings it into view through the camera (the stage itself never scrolls).
stage.addEventListener('focusin', e => {
  stage.scrollTop = stage.scrollLeft = 0;
  const n = e.target.closest('.root[data-tree]');
  if (n && !onScreen(n.dataset.tree)) { setFollow(false); frameTree(n.dataset.tree); }
});
stage.addEventListener('pointerover', e => {
  const n = e.target.closest('[data-kind]'); if (!n || cam.drag?.on) return;
  showTip(e, tipHtml(n)); if (n.dataset.kind === 'root') world.highlight(n.dataset.tree);
});
stage.addEventListener('pointermove', e => { if (!tip.hidden && !cam.drag?.on) { const n = e.target.closest('[data-kind]'); if (n) showTip(e, tipHtml(n)); else showTip(e); } });
stage.addEventListener('pointerout', e => { const n = e.target.closest('[data-kind]'); if (n && !n.contains(e.relatedTarget)) { showTip(e); world.highlight(null); } });
stage.addEventListener('click', e => {
  const n = e.target.closest('[data-kind]'); if (!n) return;
  select(n.dataset.tree, {mark: n.dataset.kind === 'leaf' ? 'j:' + n.dataset.key : n.dataset.kind === 'chip' ? 'g:' + n.dataset.key : null});
});
stage.addEventListener('dblclick', e => {
  if (e.target.closest('[data-kind], .pin, .toast, .minimap, .agent')) return;
  const r = stage.getBoundingClientRect(), [wx, wy] = cam.toWorld(e.clientX - r.left, e.clientY - r.top), k = clamp(cam.k * (e.shiftKey ? .5 : 2), cam.kMin, cam.kMax);
  setFollow(false); cam.fly({k, cx: wx + (cam.w / 2 - (e.clientX - r.left)) / k, cy: wy + (cam.h / 2 - (e.clientY - r.top)) / k}, {ms: 300});
});
$('overlay').addEventListener('pointerover', e => { const a = e.target.closest('.agent'); if (a) showTip(e, `<b>${esc(agentName(a.dataset.agent))}</b>${esc(S.doing?.get(a.dataset.agent) || '待命')}`); });
$('overlay').addEventListener('pointerout', e => { if (e.target.closest('.agent')) showTip(e); });

// ── Controls
$('t-play').addEventListener('click', () => { if (S.live) return setLive(false); setPlaying(!S.playing); });   // pausing live holds now
$('t-speed').addEventListener('change', e => { S.speed = Number(e.target.value); if (S.live) setLive(false); status(); });
$('t-range').addEventListener('change', e => { S.range = e.target.value; afterRange(); });
$('t-live').addEventListener('click', () => setLive(!S.live));
$('b-follow').addEventListener('click', () => { setFollow(true); S.followAt = 0; if (!S.focus) fitAll(); });
$('b-fit').addEventListener('click', () => { setFollow(false); fitAll(); });
$('b-help').addEventListener('click', () => $('help').showModal());
$('rail-toggle').addEventListener('click', () => { S.railAuto = false; toggleRail(); });
for (const b of $('src').querySelectorAll('button')) b.addEventListener('click', () => switchSource(b.dataset.src));
function toggleRail(open = app.classList.contains('rail-collapsed')) {
  app.classList.toggle('rail-collapsed', !open);
  $('rail-toggle').setAttribute('aria-expanded', String(open)); $('rail-toggle').textContent = open ? '⟨' : '⟩';
  $('rail-toggle').setAttribute('aria-label', open ? '收起侧栏' : '展开侧栏');
}
addEventListener('keydown', e => {
  if (e.isComposing || e.ctrlKey || e.metaKey || e.altKey || e.target.closest?.('input, select, textarea, [contenteditable]') || $('help').open) return;
  // A focused task or record line opens with Enter (or Space); the focused time bar moves with arrows, Home and End.
  const focused = e.target.closest?.('.root[data-tree], .feed li[data-tree]');
  if (focused && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); return select(focused.dataset.tree); }
  if (e.target === $('t-track') && ['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) {
    e.preventDefault(); setLive(false);
    const span = timeline.d1 - timeline.d0, step = span / (e.shiftKey ? 10 : 50);
    return seek(e.key === 'Home' ? timeline.d0 : e.key === 'End' ? timeline.d1 : S.now + (e.key === 'ArrowLeft' ? -step : step));
  }
  const k = e.key, pan = 120;
  const act = {
    ' ': () => $('t-play').click(), k: () => $('t-play').click(), l: () => setLive(!S.live), c: () => $('b-follow').click(), f: () => $('b-fit').click(), '!': () => $('b-fit').click(),
    w: () => $('b-wins').click(), '[': () => { S.railAuto = false; toggleRail(); }, '?': () => $('help').showModal(),
    Escape: () => { if (!closePanel()) { setFollow(false); world.highlight(null); } },
    ArrowLeft: () => { setFollow(false); cam.panBy(pan, 0); }, ArrowRight: () => { setFollow(false); cam.panBy(-pan, 0); },
    ArrowUp: () => { setFollow(false); cam.panBy(0, pan); }, ArrowDown: () => { setFollow(false); cam.panBy(0, -pan); },
    '+': () => { setFollow(false); cam.zoomBy(1.4); }, '=': () => { setFollow(false); cam.zoomBy(1.4); }, '-': () => { setFollow(false); cam.zoomBy(1 / 1.4); },
    ',': () => { setLive(false); seek(S.now - (e.shiftKey ? 3600 : 600)); }, '.': () => { setLive(false); seek(S.now + (e.shiftKey ? 3600 : 600)); },
    '<': () => speedStep(-1), '>': () => speedStep(1),
  }[k.length === 1 ? k.toLowerCase() : k] || (k === ' ' ? null : null);
  if (!act || (e.target.closest?.('button, a') && (k === ' ' || k === 'Enter'))) return;
  e.preventDefault(); act();
});
function speedStep(d) { const i = clamp(SPEEDS.indexOf(S.speed) + d, 0, SPEEDS.length - 1); S.speed = SPEEDS[i]; $('t-speed').value = String(S.speed); if (S.live) setLive(false); status(); }

// ── URL (what is selected and which data; ?t= opens paused at a time, for stills)
function syncUrl() {
  const q = new URLSearchParams(location.search);
  const set = (k, v) => v ? q.set(k, v) : q.delete(k);
  set('src', S.hasSnapshot ? S.src : null); set('range', S.range !== 'all' && S.range !== 'custom' ? S.range : null); set('task', S.selected);
  const s = q.toString(); if (s !== location.search.slice(1)) history.replaceState(null, '', location.pathname + (s ? '?' + s : ''));
}

// ── Live polling, with backoff on failure; the view keeps its last good model.
let pollTimer = null;
async function poll(now) {
  clearTimeout(pollTimer);
  if (S.src !== 'live') return;
  if (!now && document.visibilityState !== 'visible') { pollTimer = setTimeout(poll, POLL_MS); return; }
  try {
    const raw = await fetchModel('live');
    S.skew = Date.now() / 1000 - raw.at; S.lastOk = Date.now(); S.misses = 0; S.error = null;
    const sig = JSON.stringify([raw.research?.length, raw.directions?.length, raw.notes?.length, raw.jobs?.length, raw.jobs?.map(j => [j.ended, j.claimed, j.queued]).flat().reduce((a, b) => a + (b || 0), 0), raw.research?.map(r => r.updated_at).reduce((a, b) => a + b, 0), raw.directions?.map(r => r.updated_at).reduce((a, b) => a + b, 0)]);
    if (sig !== S.sig && S.src === 'live') { S.sig = sig; adopt(raw, 'live', true); if (S.live) { S.T1 = wallNow(); refresh(true); } else refresh(false); }
    else if (S.src === 'live') { S.M.phones = normalize(raw, 'live').phones; }
    pollTimer = setTimeout(poll, POLL_MS);
  } catch (err) {
    S.misses++; S.error = `连接中断（${err.message}）· 显示 ${S.lastOk ? hm(S.lastOk / 1000) : '之前'} 的数据，${Math.min(30, 2 ** S.misses)} 秒后重试`;
    pollTimer = setTimeout(poll, Math.min(30000, 1000 * 2 ** S.misses) * (.8 + Math.random() * .4));
  }
  status();
}
async function switchSource(src) {
  if (src === S.src) return;
  setLive(false); clearTimeout(pollTimer);
  try {
    const raw = await fetchModel(src);
    if (src === 'live') { S.skew = Date.now() / 1000 - raw.at; S.lastOk = Date.now(); }
    S.idx = -1; S.feedIdx = null; S.focus = null; closePanel(); adopt(raw, src, false);
    S.range = 'all'; $('t-range').value = 'all'; timeline.setRange(S.T0, S.T1);
    if (src === 'live') setLive(true); else { seek(S.T0); setPlaying(true); }
    fitAll(0); syncUrl();
  } catch (err) { S.error = `读取失败：${err.message}`; status(); }
}

// ── The frame loop: advance time, apply it, move the camera and the overlay.
let last = performance.now(), statusAt = 0;
function frame(t) {
  const dt = Math.min(.1, (t - last) / 1000); last = t;
  if (S.M && document.visibilityState === 'visible') {
    if (S.src === 'live') {   // a live source's record ends now, live or not
      const wall = wallNow();
      if (wall - S.T1 > 1) { S.T1 = wall; timeline.t1 = wall; if (S.range !== 'custom') timeline.setRange(...rangeBounds(S.range)); }
    }
    if (S.live) {
      S.now = S.T1; refreshIfCrossed(true);
    } else if (S.playing && S.now < timeline.d1) {
      const next = S.times[S.idx] ?? Infinity, gap = next - S.now > 1800 ? 8 : 1;
      S.now = Math.min(timeline.d1, S.now + dt * S.speed * gap); refreshIfCrossed(true);
      if (S.now >= timeline.d1) { setPlaying(false); window.__done = true; onEnd(); }
    }
    timeline.setNow(S.now, S.live); clock();
    followStep(); cam.step(t); overlay.step(t);
    if (S.camMoved || overlay.dirty || overlay.bubbles.length || overlay.moving) { overlay.layout(avoidRects()); S.camMoved = false; overlay.dirty = false; }
    if (t - statusAt > 1000) { statusAt = t; status(); phones(); }
  }
  requestAnimationFrame(frame);
}
function refreshIfCrossed(animate) { if (upper(S.times, S.now) !== S.idx) refresh(animate); }
function avoidRects() {
  const r = stage.getBoundingClientRect(), out = [];
  for (const n of [$('minimap')]) {
    if (n.classList?.contains('off')) continue;
    const b = n.getBoundingClientRect(); if (b.width) out.push({l: b.left - r.left, t: b.top - r.top, r: b.right - r.left, b: b.bottom - r.top});
  }
  return out;
}
cam.onResize = () => {
  // The stage changed size (window, rail, panel): keep the subject framed rather than whatever world point was central.
  clearTimeout(S.resizeT);
  cam.kMin = Math.min(.5, cam.viewFor(cam.extent, {maxK: 1}).k * .8);
  S.resizeT = setTimeout(() => { if (S.selected) frameTree(S.selected, 0); else if (S.follow && S.focus) frameTree(S.focus, 0); minimap(); }, 120);
};

// ── Kiosk: replay once, show the whole map and the results, then hand over to the story page.
async function onEnd() {
  if (S.src === 'live' && !Q.get('kiosk') && timeline.d1 >= S.T1 - 1) return setLive(true);   // caught up with now
  if (!Q.get('kiosk')) return;
  const wait = ms => new Promise(r => setTimeout(r, ms));
  setFollow(false); await fitAll(1800); await wait(3000);
  openPanel('wins'); await wait(8000);
  window.__kioskDone = true;
  if (Q.get('kiosk') === 'go' || Q.get('kiosk') === 'loop') location.href = '/dash/showcase?kiosk=loop';
}

// ── Start
(async () => {
  const want = Q.get('src') || (location.pathname.startsWith('/dash/showcase/') ? 'snapshot' : null);   // the showcase address keeps showing the snapshot
  const [liveR, snapR] = await Promise.allSettled([fetchModel('live'), fetchModel('snapshot')]);
  clearTimeout(slow);
  S.hasSnapshot = snapR.status === 'fulfilled';
  const liveOk = liveR.status === 'fulfilled', explicit = want === 'live' || want === 'snapshot';
  const pick = explicit ? (want === 'live' ? (liveOk ? 'live' : null) : (S.hasSnapshot ? 'snapshot' : null))
    : Q.get('kiosk') && S.hasSnapshot ? 'snapshot' : liveOk ? 'live' : S.hasSnapshot ? 'snapshot' : null;
  if (!pick) {
    // The chosen data could not be read: say so and offer a retry (and the other source if there is one); never switch silently.
    const other = want === 'live' && S.hasSnapshot ? 'snapshot' : want === 'snapshot' && liveOk ? 'live' : null;
    const why = (want === 'snapshot' ? snapR : liveR).reason?.message || '';
    showEmpty(`<div>${want === 'snapshot' ? '读不到快照' : '读不到本节点的数据'}<small>${esc(why)}</small><button type="button" id="retry">重试</button>${other ? `<button type="button" id="other">${other === 'snapshot' ? '看快照' : '看本节点'}</button>` : ''}</div>`);
    $('mode').className = 'mode stale'; $('mode').innerHTML = '<i></i><b>读取失败</b>';
    $('retry').addEventListener('click', () => location.reload());
    $('other')?.addEventListener('click', () => { const q = new URLSearchParams(location.search); q.set('src', other); location.search = q.toString(); });
    return;
  }
  if (!explicit && pick === 'snapshot' && !liveOk) S.error = `本节点数据读取失败（${liveR.reason?.message || ''}），显示的是快照`;
  const raw = pick === 'live' ? liveR.value : snapR.value;
  if (pick === 'live') { S.skew = Date.now() / 1000 - raw.at; S.lastOk = Date.now(); }
  if (innerWidth < 1180) toggleRail(false);
  adopt(raw, pick, false); app.classList.remove('booting');
  S.speed = SPEEDS.includes(Number(Q.get('speed'))) ? Number(Q.get('speed')) : 900; $('t-speed').value = String(S.speed);
  $('t-range').value = ['all', '24', '6', 'today'].includes(S.range) ? S.range : (S.range = 'all');
  timeline.setRange(...rangeBounds(S.range));
  const at = Number(Q.get('t') || 0);
  if (at) { seek(at); setPlaying(false); }
  else if (pick === 'live' && !Q.get('kiosk')) { S.now = S.T1; refresh(false); setLive(true); }
  else { seek(timeline.d0); setPlaying(Q.get('paused') !== '1'); }
  S.focus ||= lastTreeBefore(S.idx) || S.events.find(e => e.type === 'task')?.tree;   // a replay opens on its first task
  const task = S.lay.trees.has(Q.get('task')) ? Q.get('task') : null;
  if (task) select(task, {fly: false}); else if (Q.get('panel') === 'wins') openPanel('wins');
  await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));   // the panel has taken its width
  if (task) frameTree(task, 0); else if (Q.get('view') === 'all' || !S.focus) fitAll(0); else frameTree(S.focus, 0);
  if (S.src === 'live' && !S.live) poll();
  status(); followButton(); syncUrl();
  requestAnimationFrame(frame);
  setTimeout(() => { window.__ready = true; }, 300);
  window.__command = {S, cam, world, overlay, timeline, seek, select, setLive, setFollow};
})();
