// Command view (concept A v2). One 2D tree per task on a pan/zoom field; phones in a dock; agents move between them.
// Replays the real record (data.json, a read-only snapshot) at speed. Every mark on screen is a recorded event.
const response = await fetch('/dash/api/showcase', {credentials: 'same-origin'});
if (!response.ok) { document.body.innerHTML = '<p class="empty">此节点还没有展示数据快照。</p>'; throw new Error('no showcase data'); }
const data = await response.json();
const Q = new URLSearchParams(location.search);
const PAGES = {story: '/dash/showcase', command: '/dash/showcase/command'};
const leave = url => { document.body.classList.add('leaving'); setTimeout(() => { location.href = url; }, 700); };
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'})[c]);
const el = (tag, cls, html) => { const n = document.createElement(tag); if (cls) n.className = cls; if (html != null) n.innerHTML = html; return n; };
const BRAND = {HONOR: '荣耀', REDMAGIC: '红魔', HUAWEI: '华为'};
const SCREEN = {HONOR: '/dash/api/showcase/screen/HONOR'};
const AGENT = {'kimi-researcher': {c: '#7cc4ff', s: 'K', n: 'kimi'}, 'glm-researcher': {c: '#b49cff', s: 'G', n: 'glm'}, 'claude-lead': {c: '#ff9f7a', s: 'C', n: 'claude'}, 'glm-ops': {c: '#e39ad6', s: 'O', n: 'glm-ops'}};
const OUTCOME = {succeeded: '成功', failed: '失败', cancelled: '取消', interrupted: '中断'};
const DIR_STATE = {verified: '验证有效', rejected: '试过放弃', proposed: '待做', deferred: '暂缓', running: '进行中', active: '进行中'};
const fmt = (s, o) => new Intl.DateTimeFormat('zh-CN', {...o, hour12: false}).format(new Date(s * 1000));
const hm = s => fmt(s, {hour: '2-digit', minute: '2-digit'});
const md = s => fmt(s, {month: 'numeric', day: 'numeric'});
const ease = t => t < .5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
const brand = j => j.device ? BRAND[j.device] || j.device : j.phone ? '真机' : '宿主';
const begun = j => j.claimed || j.started;  // started is the submit time; claimed is when the node started it
const mins = j => j.ended && begun(j) ? Math.max(1, Math.round((j.ended - begun(j)) / 60)) : null;
const WAIT_MIN = 30;  // seconds in the queue before we show an agent waiting
const waited = j => j.phone && j.queued && j.claimed && j.claimed - j.queued > WAIT_MIN;
const agentName = a => AGENT[a]?.n || a || '平台';
function short(t, max = 30) {
  let s = (t || '').replace(/^\[[^\]]*\]\s*/, '');
  const cut = s.search(/[，,；;（(]/); if (cut > 0) s = s.slice(0, cut);
  const colon = s.search(/[：:]/); if (colon >= 6) s = s.slice(0, colon);
  return s.length > max ? s.slice(0, max - 1) + '…' : s;
}

// ── Layout. Trees in creation order, masonry over two columns. Each tree: root orb → direction cards → experiments on an arc.
const SPACE = 30, COL_W = 1180, GAP = 90, CHIP_X = 250, CHIP_W = 236, COLS = 3;
const arcFor = n => { const R = Math.min(440, Math.max(170, n * SPACE / 2.2)), span = n > 1 ? Math.min(2.5, n * SPACE / R) : 0; return {R, span, ext: n > 1 ? 2 * R * Math.sin(span / 2) : 0}; };
const widen = n => [...Array(n).keys()].sort((a, b) => Math.abs(a - (n - 1) / 2) - Math.abs(b - (n - 1) / 2) || a - b);
const tasks = [...data.research].sort((a, b) => a.created_at - b.created_at);
const trees = new Map(), colY = Array(COLS).fill(0);
for (const task of tasks) {
  const dirs = data.directions.filter(d => d.research_id === task.id).sort((a, b) => a.created_at - b.created_at);
  const jobs = data.jobs.filter(j => j.research_id === task.id && j.started).sort((a, b) => (a.ended || 1e12) - (b.ended || 1e12));
  const loose = jobs.filter(j => !dirs.some(d => d.id === j.direction_id));
  const groups = [...(loose.length ? [{key: 'trunk', dir: null, jobs: loose}] : []), ...dirs.map(d => ({key: d.id, dir: d, jobs: jobs.filter(j => j.direction_id === d.id)}))];
  for (const g of groups) { g.arc = arcFor(g.jobs.length); g.band = Math.max(84, g.arc.ext + 46); g.order = widen(g.jobs.length); g.count = 0; }
  const h = Math.max(480, groups.reduce((a, g) => a + g.band, 0)), col = colY.indexOf(Math.min(...colY));
  const t = {task, groups, x: col * COL_W, y: colY[col] + h / 2, h, ok: 0, n: 0, shown: false, busy: 0};
  let y = t.y - groups.reduce((a, g) => a + g.band, 0) / 2;
  for (const g of groups) { g.y = y + g.band / 2; y += g.band; }
  trees.set(task.id, t); colY[col] += h + GAP;
}

// ── World rendering
const world = $('world'), svg = $('links'), NS = 'http://www.w3.org/2000/svg';
let gid = 0;
function link(x0, y0, x1, y1, color, attrs, width = 1.2, draw = false) {
  const g = document.createElementNS(NS, 'linearGradient'), id = 'g' + (++gid);
  g.id = id; g.setAttribute('gradientUnits', 'userSpaceOnUse');
  for (const [k, v] of Object.entries({x1: x0, y1: y0, x2: x1, y2: y1})) g.setAttribute(k, v);
  g.innerHTML = `<stop offset="0" stop-color="${color}" stop-opacity=".9"/><stop offset="1" stop-color="${color}" stop-opacity=".16"/>`;
  svg.append(g);
  const p = document.createElementNS(NS, 'path'), mx = x0 + (x1 - x0) * .5;
  p.setAttribute('d', `M${x0},${y0} C${mx},${y0} ${mx},${y1} ${x1},${y1}`);
  p.setAttribute('stroke', `url(#${id})`); p.setAttribute('stroke-width', width);
  p.setAttribute('vector-effect', 'non-scaling-stroke'); p.setAttribute('pathLength', '1');
  for (const [k, v] of Object.entries(attrs)) p.dataset[k] = v;
  if (draw) { p.setAttribute('class', 'draw'); p.style.strokeDasharray = '1'; }
  svg.append(p); return p;
}
function place(node, x, y, tree) { node.style.left = x + 'px'; node.style.top = y + 'px'; if (tree) node.dataset.tree = tree; world.append(node); return node; }
function ringSvg(t) {
  const jobs = t.groups.flatMap(g => g.jobs.filter(j => j.shown)).sort((a, b) => a.ended - b.ended), N = 60;
  let dots = '';
  for (let k = 0; k < N; k++) { const a = -Math.PI / 2 + k * 2 * Math.PI / N; dots += `<circle cx="${(115 + 100 * Math.cos(a)).toFixed(1)}" cy="${(115 + 100 * Math.sin(a)).toFixed(1)}" r="1.4" fill="rgba(150,175,200,.16)"/>`; }
  jobs.forEach((j, k) => {
    const ring = Math.floor(k / N), a = -Math.PI / 2 + (k % N) * 2 * Math.PI / N, r = 100 + ring * 9, x = (115 + r * Math.cos(a)).toFixed(1), y = (115 + r * Math.sin(a)).toFixed(1);
    dots += j.outcome === 'succeeded' ? `<circle cx="${x}" cy="${y}" r="3.4" fill="#5ccfc0"/>` : `<circle cx="${x}" cy="${y}" r="2.8" fill="none" stroke="#66778a" stroke-width="1.2"/>`;
  });
  return dots;
}
function drawRoot(t, fresh) {
  const st = t.task.state === 'delivered' && t.task.updated_at <= now ? 'delivered' : 'active';
  const r = el('div', 'root' + (fresh ? ' fresh' : ''), `<svg class="ring" viewBox="0 0 230 230"></svg><div class="orb"><div class="count"><b>0</b><small>实验</small></div></div>${fresh ? '<span class="ripple"></span>' : ''}`);
  place(r, t.x, t.y, t.task.id);
  const label = el('div', 'task-label', `<b>${esc(short(t.task.title))}</b><div class="meta"><span class="tag ${st}">${st === 'delivered' ? '已交付' : '进行中'}</span><span class="mono">${esc(agentName(t.task.owner))} · ${md(t.task.created_at)} ${hm(t.task.created_at)}</span></div>`);
  place(label, t.x, t.y + 122, t.task.id); if (fresh) label.classList.add('fresh');
  t.root = r; t.label = label; t.shown = true;
  r.addEventListener('mouseenter', e => { dimExcept(n => n.dataset.tree === t.task.id); tip(e, `<b>${esc(t.task.title)}</b><span class="mono">${t.root.classList.contains('delivered') ? '已交付' : '进行中'} · ${esc(t.task.owner || '无人负责')} · ${t.n} 次实验 · 成功 ${t.ok}</span>`); });
  r.addEventListener('mouseleave', () => { dimExcept(null); tip(); });
  r.addEventListener('click', () => { touched = performance.now(); flyTo(boundsOf([t]), 900); });
  paintRoot(t);
}
function paintRoot(t) {
  if (!t.root) return;
  t.root.querySelector('.count b').textContent = t.n;
  t.root.querySelector('.ring').innerHTML = ringSvg(t);
  const delivered = t.task.state === 'delivered' && t.task.updated_at <= now;
  t.root.classList.toggle('delivered', delivered);
  const tag = t.label.querySelector('.tag'); tag.className = 'tag ' + (delivered ? 'delivered' : 'active'); tag.textContent = delivered ? '已交付' : '进行中';
}
const MARK = {verified: '<svg viewBox="0 0 16 16"><path d="M3.5 8.5l3 3 6-7" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  rejected: '<svg viewBox="0 0 16 16"><path d="M4.5 4.5l7 7M11.5 4.5l-7 7" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>',
  proposed: '<svg viewBox="0 0 16 16"><circle cx="8" cy="8" r="4.5" fill="none" stroke="currentColor" stroke-width="1.5" stroke-dasharray="2.2 2"/></svg>'};
function paintChip(g) {
  const ok = g.jobs.filter(j => j.shown && j.outcome === 'succeeded').length;
  if (!g.dir) { g.chip.querySelector('.n').textContent = g.count; g.chip.querySelector('small').textContent = `成功 ${ok}`; return; }
  g.chip.querySelector('small').textContent = (DIR_STATE[g.dir.state] || g.dir.state) + (g.count ? ` · ${g.count} 次实验` : '');
}
const chipColor = s => ({verified: '#f0c36e', rejected: '#4c5866', proposed: '#8db4f5'})[s] || '#8aa1b5';
function drawGroup(t, g, fresh) {
  if (g.chip) return;
  const state = g.dir ? g.dir.state : 'trunk', x = t.x + CHIP_X;
  const head = g.dir ? `<span class="mark">${MARK[state] || MARK.proposed}</span>` : '<span class="n">0</span>';
  const chip = el('div', `chip ${state}` + (fresh ? ' fresh' : ''), `${head}<div class="t"><span>${esc(g.dir ? short(g.dir.title, 40) : '实验')}</span><small></small></div>`);
  place(chip, x, g.y, t.task.id); chip.dataset.group = g.key;
  link(t.x + 80, t.y, x, g.y, chipColor(state), {tree: t.task.id, group: g.key}, 1.6, fresh);
  g.chip = chip; paintChip(g);
  chip.addEventListener('mouseenter', e => {
    dimExcept(n => n.dataset.tree === t.task.id && (!n.dataset.group || n.dataset.group === g.key));
    tip(e, `<b>${esc(g.dir ? g.dir.title : '未归入方向的实验')}</b><span class="mono">${g.dir ? (DIR_STATE[g.dir.state] || g.dir.state) + ' · ' : ''}${g.count} 次实验</span>`);
  });
  chip.addEventListener('mouseleave', () => { dimExcept(null); tip(); });
}
function addLeaf(t, job, fresh) {
  const g = t.groups.find(x => x.jobs.includes(job)); if (!g) return;
  drawGroup(t, g, fresh);
  const n = g.jobs.length, slot = g.order[g.count], {R, span} = g.arc, th = n > 1 ? -span / 2 + slot * span / (n - 1) : 0;
  const cx = t.x + CHIP_X + CHIP_W, ax = cx + 26, lx = ax + R * Math.cos(th), ly = g.y + R * Math.sin(th);
  const leaf = el('div', `leaf ${job.outcome || ''}` + (fresh ? ' fresh' : ''), `<i></i><span>${esc(job.kind)}</span><em>${brand(job)}</em>${mins(job) ? `<em>${mins(job)}m</em>` : ''}`);
  place(leaf, lx + 2, ly, t.task.id); leaf.dataset.group = g.key;
  link(cx, g.y, lx - 4, ly, job.outcome === 'succeeded' ? '#5ccfc0' : '#66778a', {tree: t.task.id, group: g.key}, 1.1, fresh);
  leaf.addEventListener('mouseenter', e => tip(e, `<b>${esc(job.kind)} · ${OUTCOME[job.outcome] || '未知'}</b>${esc(short(t.task.title))}<br><span class="mono">${esc(agentName(job.agent))} · ${brand(job)} · ${hm(begun(job))}–${hm(job.ended)}${waited(job) ? ` · 排队 ${Math.round((job.claimed - job.queued) / 60)} 分钟` : ''} · ${mins(job) ?? '?'} 分钟</span>`));
  leaf.addEventListener('mouseleave', () => tip());
  job.shown = true; g.count++; t.n++; if (job.outcome === 'succeeded') t.ok++;
  paintChip(g); paintRoot(t);
}
function dimExcept(keep) { for (const n of world.querySelectorAll('[data-tree]')) n.classList.toggle('dim', !!keep && !keep(n)); }
function tip(e, html) {
  const box = $('tip'); if (!html) { box.hidden = true; return; }
  box.innerHTML = html; box.hidden = false;
  box.style.left = Math.min(innerWidth - 360, e.clientX + 16) + 'px'; box.style.top = Math.min(innerHeight - 120, e.clientY + 16) + 'px';
}

// ── Camera: drag to pan, wheel to zoom; flights use smooth zoom-and-pan (van Wijk & Nuij), one target at a time.
const RAIL = 380, DOCK = 168;
const field = () => ({l: RAIL, r: innerWidth, t: 70, b: innerHeight - DOCK});
const cam = {x: 0, y: 0, k: 1};
const apply = () => { world.style.transform = `translate(${cam.x}px,${cam.y}px) scale(${cam.k})`; };
const toScreen = (x, y) => [cam.x + x * cam.k, cam.y + y * cam.k];
function boundsOf(list) {
  const pinned = t => [...pins.values()].some(p => p.tree === t.task.id);
  const xs = list.flatMap(t => [t.x - 130, t.x + 980]), ys = list.flatMap(t => [Math.min(t.y - t.h / 2, pinned(t) ? t.y - 330 : 1e9), t.y + t.h / 2]);
  return {x0: Math.min(...xs), x1: Math.max(...xs), y0: Math.min(...ys), y1: Math.max(...ys)};
}
function viewFor(b, cap = 1.05) {
  const f = field(), k = Math.min(cap, (f.r - f.l - 80) / (b.x1 - b.x0), (f.b - f.t - 40) / (b.y1 - b.y0));
  return {k, cx: (b.x0 + b.x1) / 2, cy: (b.y0 + b.y1) / 2};
}
function setView(v) { const f = field(); cam.k = v.k; cam.x = (f.l + f.r) / 2 - v.cx * v.k; cam.y = (f.t + f.b) / 2 - v.cy * v.k; apply(); }
let flight = null;
function flyTo(b, ms = 1300) {
  const f = field(), W = f.r - f.l, v = viewFor(b), mx = (f.l + f.r) / 2, my = (f.t + f.b) / 2;
  const ux0 = (mx - cam.x) / cam.k, uy0 = (my - cam.y) / cam.k, w0 = W / cam.k, ux1 = v.cx, uy1 = v.cy, w1 = W / v.k;
  const rho = Math.SQRT2, dx = ux1 - ux0, dy = uy1 - uy0, d2 = dx * dx + dy * dy, d1 = Math.sqrt(d2);
  let at;
  if (d1 < 1e-3) { const S = Math.log(w1 / w0) / rho; at = s => [ux0, uy0, w0 * Math.exp(rho * s * S)]; }
  else {
    const b0 = (w1 * w1 - w0 * w0 + rho ** 4 * d2) / (2 * w0 * rho * rho * d1), b1 = (w1 * w1 - w0 * w0 - rho ** 4 * d2) / (2 * w1 * rho * rho * d1);
    const r0 = Math.log(Math.sqrt(b0 * b0 + 1) - b0), r1 = Math.log(Math.sqrt(b1 * b1 + 1) - b1), S = (r1 - r0) / rho;
    at = s => { const u = s * S, c = Math.cosh(r0), q = w0 / (rho * rho * d1) * (c * Math.tanh(rho * u + r0) - Math.sinh(r0)); return [ux0 + q * dx, uy0 + q * dy, w0 * c / Math.cosh(rho * u + r0)]; };
  }
  if (flight) flight.done();
  return new Promise(done => { flight = {at, t0: performance.now(), ms, done, W, mx, my}; });
}
function stepFlight(now_) {
  if (!flight) return;
  const p = Math.min(1, (now_ - flight.t0) / flight.ms), [ux, uy, w] = flight.at(ease(p));
  cam.k = flight.W / w; cam.x = flight.mx - ux * cam.k; cam.y = flight.my - uy * cam.k; apply();
  if (p >= 1) { const d = flight.done; flight = null; d(); }
}
const vp = $('viewport'); let drag = null, touched = -1e9;
vp.addEventListener('pointerdown', e => { drag = {x: e.clientX - cam.x, y: e.clientY - cam.y}; vp.classList.add('dragging'); if (flight) { flight.done(); flight = null; } touched = performance.now(); });
addEventListener('pointermove', e => { if (drag) { cam.x = e.clientX - drag.x; cam.y = e.clientY - drag.y; apply(); touched = performance.now(); } });
addEventListener('pointerup', () => { drag = null; vp.classList.remove('dragging'); });
vp.addEventListener('wheel', e => {
  e.preventDefault(); if (flight) { flight.done(); flight = null; } touched = performance.now();
  const k = Math.min(2.2, Math.max(.12, cam.k * Math.exp(-e.deltaY * .0015)));
  cam.x = e.clientX - (e.clientX - cam.x) * k / cam.k; cam.y = e.clientY - (e.clientY - cam.y) * k / cam.k; cam.k = k; apply();
}, {passive: false});

// ── Phones
const dock = $('dock'), phones = {};
for (const p of data.phones) {
  const key = (p.brand || '').toUpperCase(), temp = p.soc_c || p.temp_c, card = el('div', 'phone' + (temp >= 40 ? ' warm' : ''));
  card.innerHTML = `<div class="screen"${SCREEN[key] ? ` style="background-image:url(${SCREEN[key]})"` : ''}></div><div class="meta"><div class="name">${BRAND[key] || key}<small>${esc(p.model)}</small></div>
    <div class="temp" data-now="${temp.toFixed(1)}">—<small>°C</small></div><div class="state"><i></i><span>空闲</span></div></div><div class="bar"><i></i></div>`;
  card.querySelector('.screen').style.opacity = .35;
  dock.append(card); phones[key] = {card, key, job: null};
}
if (Number(Q.get('t') || 0) >= data.at - 60) for (const t of dock.querySelectorAll('.temp')) t.firstChild.textContent = t.dataset.now;
const phoneOf = j => j.device ? phones[j.device] : null;
function phoneBusy(p, job) {
  p.job = job; p.card.classList.toggle('testing', !!job);
  p.card.querySelector('.screen').style.opacity = job ? 1 : .35;
  p.card.querySelector('.state span').textContent = job ? `${agentName(job.agent)} · ${job.kind}` : '空闲';
  if (!job) p.card.querySelector('.bar i').style.width = '0';
}

// ── Agents (screen space)
const layer = $('agents'), agents = {};
const STAND = [-128, -82, -36, 10].map(d => d * Math.PI / 180);
data.agents.forEach((name, i) => {
  const m = AGENT[name] || {c: '#a3b3c2', s: '?', n: name}, node = el('div', 'agent', `<div class="av">${m.s}</div><div class="nm">${m.n}<small></small></div>`);
  node.style.setProperty('--c', m.c); layer.append(node);
  agents[name] = {name, i, node, at: null, from: null, t0: 0, pos: [RAIL + 60 + i * 56, innerHeight - DOCK - 40], doing: '待命'};
});
function targetOf(a) {
  const f = field();
  if (a.at?.phone) { const r = a.at.phone.card.getBoundingClientRect(); return [r.right - 34, r.top - 36]; }
  if (a.at?.wait) { const r = dock.getBoundingClientRect(), k = Object.values(agents).filter(x => x.at?.wait).indexOf(a); return [r.left - 46 - k * 64, r.top + 44]; }
  const t = a.at?.tree && trees.get(a.at.tree);
  if (!t?.shown) return [f.l + 60 + a.i * 56, f.b - 30];
  const [sx, sy] = toScreen(t.x, t.y), R = Math.max(64, 115 * cam.k + 26), th = STAND[a.i % STAND.length];
  const x = sx + R * Math.cos(th), y = sy + R * Math.sin(th), cx = Math.min(f.r - 40, Math.max(f.l + 30, x)), cy = Math.min(f.b - 40, Math.max(f.t + 30, y));
  return [cx + (cy !== y ? a.i * 64 : 0), cy + (cx !== x ? a.i * 64 : 0)];
}
function go(a, at, doing) {
  if (!a) return;
  a.from = [...a.pos]; a.at = at; a.t0 = performance.now(); a.doing = doing;
}
function stepAgents(t) {
  for (const a of Object.values(agents)) {
    const [tx, ty] = targetOf(a);
    if (a.from) {
      const p = Math.min(1, (t - a.t0) / 650), e = ease(p);
      a.pos = [a.from[0] + (tx - a.from[0]) * e, a.from[1] + (ty - a.from[1]) * e - Math.sin(Math.PI * p) * 36];
      a.node.classList.toggle('moving', p < 1); if (p >= 1) a.from = null;
    } else a.pos = [tx, ty];
    const waiting = !!a.at?.wait && a.waitSince != null;
    a.node.classList.toggle('waiting', waiting);
    a.node.querySelector('small').textContent = waiting ? `等 ${Math.max(1, Math.round((now - a.waitSince) / 60))} 分` : '';
    a.node.style.transform = `translate(${a.pos[0]}px,${a.pos[1]}px) scale(${waiting ? 1 : Math.min(1, Math.max(.72, cam.k * 1.4))})`;
  }
}

// ── Talk: fading bubbles, asks pinned to their tree, a toast when an agent waits on a person.
const bubbles = [], pins = new Map();
function bubble(note) {
  const human = note.who === 'human', a = agents[note.who], t = trees.get(note.research_id);
  if (!human && !a) return;
  const b = el('div', 'bubble' + (human ? ' human' : ''), `<b>${human ? '人' : agentName(note.who)}</b>${esc(note.text.slice(0, 64))}${note.text.length > 64 ? '…' : ''}`);
  b.style.setProperty('--c', AGENT[note.who]?.c || '#a3b3c2'); layer.append(b);
  const anchor = human ? () => { const [x, y] = t?.shown ? toScreen(t.x, t.y) : [innerWidth / 2, 300]; return [x - 90 * cam.k - 10, y - 90 * cam.k, '-100%']; }
                       : () => [a.pos[0] + 12, a.pos[1] - 26, '0'];
  bubbles.push({b, anchor, born: performance.now()});
  while (bubbles.length > 3) bubbles.shift().b.remove();
}
function pin(note, live) {
  const p = el('div', 'pin', `<b>${agentName(note.who)} 在等你回复</b><div class="txt">${esc(note.text)}</div>`);
  layer.append(p); pins.set(note.id, {p, tree: note.research_id});
  if (!live) return;
  const task = data.research.find(r => r.id === note.research_id);
  const toast = $('toast'); toast.innerHTML = `<span class="q">?</span><div><b>${agentName(note.who)} 在等你回复</b><div class="txt">${esc(short(task?.title || ''))}</div></div>`;
  toast.hidden = false; clearTimeout(pin.timer); pin.timer = setTimeout(() => { toast.hidden = true; }, 7000);
}
function unpin(id) { const x = pins.get(id); if (!x) return; x.p.style.opacity = 0; setTimeout(() => x.p.remove(), 500); pins.delete(id); }
function stepTalk(t) {
  // Pins first, then bubbles oldest to newest; a bubble that would cover another moves up above it.
  const placed = [], hit = (l, r, tp, b) => placed.find(q => l < q.r && r > q.l && tp < q.b && b > q.t);
  for (const x of pins.values()) {
    const tr = trees.get(x.tree); x.p.style.display = tr?.shown ? '' : 'none'; if (!tr?.shown) continue;
    const [sx, sy] = toScreen(tr.x, tr.y), mini = cam.k < .5; x.p.classList.toggle('mini', mini);
    const w = x.p.offsetWidth, h = x.p.offsetHeight, l = sx - w / 2, b = sy - 115 * cam.k - (mini ? 8 : 34);
    x.p.style.transform = `translate(${l}px,${b - h}px)`; placed.push({l, r: l + w, t: b - h, b});
  }
  for (const x of [...bubbles]) {
    const [ax, ay, dx] = x.anchor(); x.w ??= x.b.offsetWidth; x.h ??= x.b.offsetHeight;
    const l = dx === '-100%' ? ax - x.w : ax; let b = ay;
    for (let q, n = 0; n < 6 && (q = hit(l, l + x.w, b - x.h, b)); n++) b = q.t - 8;
    placed.push({l, r: l + x.w, t: b - x.h, b});
    x.b.style.transform = `translate(${l}px,${b - x.h}px)`;
    if (t - x.born > 6500) x.b.classList.add('fade');
    if (t - x.born > 7500) { x.b.remove(); bubbles.splice(bubbles.indexOf(x), 1); }
  }
}

// ── Record feed (left rail)
const feed = $('feed');
function log(at, who, html, cls = '') {
  const item = el('div', 'item ' + cls, `<time>${hm(at)}</time><i></i><div class="txt">${html}</div>`);
  item.style.setProperty('--c', who === 'human' ? '#f0c36e' : AGENT[who]?.c || '#8aa1b5');
  feed.prepend(item); while (feed.children.length > 10) feed.lastChild.remove();
}

// ── The record as events, replayed at speed (quiet stretches pass faster).
const events = [];
for (const t of tasks) events.push({at: t.created_at, kind: 'task', t});
for (const d of data.directions) events.push({at: d.created_at, kind: 'dir', d});
for (const j of data.jobs) if (j.research_id && j.started) {
  if (waited(j)) events.push({at: j.queued, kind: 'wait', j});
  events.push({at: begun(j), kind: 'start', j}); if (j.ended) events.push({at: j.ended, kind: 'end', j});
}
for (const n of data.notes) if (n.at && n.research_id) events.push({at: n.at, kind: 'note', n});
for (const r of data.research) if (r.state === 'delivered') events.push({at: r.updated_at, kind: 'deliver', r});
events.sort((a, b) => a.at - b.at);
const T0 = tasks[0].created_at - 600, T1 = data.at;
let now = T0, ei = 0, hold = 0, focus = null;
const SPEED = Number(Q.get('speed') || 420);
const queue = [];

function play(ev, live) {
  const t = trees.get(ev.t?.id || ev.d?.research_id || ev.j?.research_id || ev.n?.research_id || ev.r?.id);
  if (live && ev.kind !== 'task') follow(t);
  if (ev.kind !== 'task') focus = t;
  if (ev.kind === 'task') {
    log(ev.at, null, `<b>新任务</b> ${esc(short(ev.t.title))}`);
    if (live) { hold++; queue.push(t); } else drawRoot(t, false);
    return;
  }
  if (!t?.shown) return;
  if (ev.kind === 'dir') {
    const g = t.groups.find(x => x.key === ev.d.id); if (g) drawGroup(t, g, live);
    log(ev.at, null, `<b>新方向</b> ${esc(short(ev.d.title, 40))}`);
  } else if (ev.kind === 'wait') {
    const a = agents[ev.j.agent]; if (!a) return;
    a.waitSince = ev.at; go(a, {wait: ev.j}, '排队等手机');
    log(ev.at, ev.j.agent, `<b>${agentName(ev.j.agent)}</b> 排队等手机${ev.j.device ? `（${BRAND[ev.j.device] || ev.j.device}）` : ''} · ${esc(ev.j.kind)}`);
  } else if (ev.kind === 'start') {
    const a = agents[ev.j.agent], p = phoneOf(ev.j);
    if (a?.at?.wait === ev.j) { log(ev.at, ev.j.agent, `<b>${agentName(ev.j.agent)}</b> 等了 ${Math.round((ev.at - a.waitSince) / 60)} 分钟，拿到手机`); a.waitSince = null; }
    t.busy++; t.root.classList.add('busy');
    if (p) { phoneBusy(p, ev.j); go(a, {phone: p}, `在${BRAND[p.key]}上 · ${ev.j.kind}`); log(ev.at, ev.j.agent, `<b>${agentName(ev.j.agent)}</b> 在${BRAND[p.key]}上开始${esc(ev.j.kind)}`); }
    else go(a, {tree: t.task.id}, ev.j.kind);
  } else if (ev.kind === 'end') {
    const p = phoneOf(ev.j); if (p?.job === ev.j) phoneBusy(p, null);
    t.busy = Math.max(0, t.busy - 1); if (!t.busy) t.root.classList.remove('busy');
    go(agents[ev.j.agent], {tree: t.task.id}, '写报告');
    addLeaf(t, ev.j, live);
    if (ev.j.phone) log(ev.at, ev.j.agent, `<b>${esc(ev.j.kind)}</b> ${OUTCOME[ev.j.outcome] || '结束'} · ${brand(ev.j)} · ${mins(ev.j)} 分钟`);
  } else if (ev.kind === 'note') {
    const n = ev.n, human = n.who === 'human';
    if (!human) go(agents[n.who], {tree: t.task.id}, n.ask ? '等你回复' : '写记录');
    if (live) bubble(n);
    log(ev.at, n.who, `<b>${human ? '人' : agentName(n.who)}</b> ${esc(n.text.slice(0, 70))}`, human ? 'human' : '');
    if (n.ask && (n.open || data.notes.some(x => x.reply_to === n.id))) pin(n, live);
    if (human && n.reply_to) unpin(n.reply_to);
  } else if (ev.kind === 'deliver') {
    paintRoot(t); log(ev.at, null, `<b>交付</b> ${esc(short(ev.r.title))}`);
  }
}
async function newTree(t) {
  const quiet = performance.now() - touched < 6000;
  if (!quiet) {
    const slot = place(el('div', 'slot'), t.x, t.y);
    await flyTo(boundsOf([t]), 1200);
    slot.remove();
  }
  drawRoot(t, true);
  await new Promise(r => setTimeout(r, quiet ? 200 : 1300));
  hold--; lastFollow = performance.now();
}
let lastFollow = -1e9;
function seen(t) {
  const b = boundsOf([t]), f = field(), [x0, y0] = toScreen(b.x0, b.y0), [x1, y1] = toScreen(b.x1, b.y1);
  return Math.max(0, Math.min(x1, f.r) - Math.max(x0, f.l)) * Math.max(0, Math.min(y1, f.b) - Math.max(y0, f.t)) / ((x1 - x0) * (y1 - y0));
}
function follow(t) {
  const at = performance.now();
  if (!t?.shown || flight || building || queue.length || at - touched < 8000 || at - lastFollow < 6000 || seen(t) > .85) return;
  lastFollow = at; flyTo(boundsOf([t]), 1200);
}
let building = false;
async function runQueue() { if (building || !queue.length) return; building = true; await newTree(queue.shift()); building = false; }

// ── HUD
const fmtN = n => n.toLocaleString('en-US');
function counts() {
  const shown = [...trees.values()].filter(t => t.shown), delivered = shown.filter(t => t.task.state === 'delivered' && t.task.updated_at <= now).length;
  const runs = data.jobs.filter(j => j.shown).length, busy = Object.values(phones).filter(p => p.job).length;
  $('counts').innerHTML = [['任务', shown.length, '#8aa1b5'], ['进行中', shown.length - delivered, '#5ccfc0'], ['已交付', delivered, '#f0c36e'], ['实验', runs, '#e3eaf0'], ['手机在用', `${busy}/${data.phones.length}`, busy ? '#5ccfc0' : '#3d4b59'], ['等手机', Object.values(agents).filter(a => a.at?.wait).length, '#8db4f5']]
    .map(([k, v, c]) => `<span style="--c:${c}"><i></i>${k}<b>${v}</b></span>`).join('');
}
const CELLS = 48, cell = (T1 - T0) / CELLS;
function roster() {
  $('agents-n').textContent = `${data.agents.length} 个`;
  const box = $('roster');
  if (!box.children.length) for (const name of data.agents) {
    const m = AGENT[name] || {c: '#a3b3c2', s: '?', n: name}, row = el('div', 'who', `<span class="av">${m.s}</span><span class="nm">${m.n}</span><span class="n">0<small>次</small></span><span class="doing">待命</span><span></span><div class="heat">${'<i></i>'.repeat(CELLS)}</div>`);
    row.style.setProperty('--c', m.c); row.dataset.name = name; box.append(row);
  }
  for (const row of box.children) {
    const a = agents[row.dataset.name], jobs = data.jobs.filter(j => j.agent === row.dataset.name && j.started && j.started <= now);
    row.querySelector('.n').firstChild.textContent = jobs.filter(j => j.ended && j.ended <= now).length;
    row.querySelector('.doing').textContent = a?.doing || '待命'; row.classList.toggle('busy', !!a?.doing && a.doing !== '待命');
    const cells = row.querySelectorAll('.heat i');
    for (let c = 0; c < CELLS; c++) {
      const c0 = T0 + c * cell, c1 = Math.min(now, c0 + cell); if (c1 <= c0) { cells[c].style.background = ''; continue; }
      const busy = jobs.reduce((s, j) => s + Math.max(0, Math.min(c1, j.ended || now) - Math.max(c0, begun(j))), 0) / cell;
      const notes = data.notes.some(n => n.who === row.dataset.name && n.at >= c0 && n.at < c1);
      const f = Math.min(1, busy + (notes ? .25 : 0));
      cells[c].style.background = f ? `color-mix(in srgb, ${AGENT[row.dataset.name]?.c || '#a3b3c2'} ${Math.round(18 + f * 82)}%, transparent)` : '';
    }
  }
}
function clock() { $('clock').textContent = hm(now); $('date').textContent = fmt(now, {month: 'long', day: 'numeric', weekday: 'short'}); }
$('source').textContent = `${data.source || '本节点'} · 截至 ${md(T1)} ${hm(T1)}`;
function wins() {
  const done = data.research.filter(r => r.state === 'delivered' && r.updated_at <= now).sort((a, b) => b.updated_at - a.updated_at);
  const phone = data.jobs.filter(j => j.phone && j.ended && j.ended <= now).length;
  const humans = data.notes.filter(n => n.who === 'human' && n.at <= now).length, adopted = data.notes.filter(n => n.decision === 'adopt' && n.at <= now).length;
  const verified = data.directions.filter(d => d.state === 'verified' && d.updated_at <= now).length, h = data.history;
  const commits = Object.values(h.commits).reduce((a, b) => a + b, 0), box = $('wins'), had = box.querySelectorAll('li').length;
  box.innerHTML = `<h3>成果<span>截至 ${md(now)} ${hm(now)}</span></h3>
    <div class="hero"><b>${done.length}</b><span>份交付报告</span></div>
    <div class="tiles"><div class="tile gold"><b>${verified}</b><span>验证有效的方向</span></div><div class="tile"><b>${phone}</b><span>次真机实验</span></div>
      <div class="tile"><b>${humans}</b><span>次人的回复</span></div><div class="tile"><b>${adopted}</b><span>条采纳记录</span></div></div>
    <ol>${done.map((r, i) => `<li class="${i < done.length - had ? 'new' : ''}"><span>${esc(short(r.title, 40))}</span><time>${md(r.updated_at)}</time></li>`).join('')}</ol>
    <div class="history"><div><b>${fmtN(commits)}</b>次提交</div><div><b>${h.experiment_lines.length}</b>条实验线</div><div><b>${h.sr_networks_kept}</b>个超分网络</div></div>`;
}
const HOURS = Math.ceil((T1 - T0) / 3600), hourMax = Math.max(1, ...[...Array(HOURS).keys()].map(i => data.jobs.filter(j => j.ended >= T0 + i * 3600 && j.ended < T0 + (i + 1) * 3600).length));
function trend() {
  const X = s => (s - T0) / (T1 - T0) * 1000, bw = 1000 / HOURS, Y = v => 170 - v / hourMax * 130;
  let bars = '';
  for (let i = 0; i < HOURS; i++) {
    const a = T0 + i * 3600, b = Math.min(now, a + 3600); if (b <= a) break;
    const done = data.jobs.filter(j => j.ended >= a && j.ended < b), ok = done.filter(j => j.outcome === 'succeeded').length, bad = done.length - ok;
    const x = (i * bw + bw * .18).toFixed(1), w = (bw * .64).toFixed(1);
    if (ok) bars += `<rect x="${x}" y="${Y(ok)}" width="${w}" height="${170 - Y(ok)}" rx="1.5" fill="#5ccfc0" opacity=".85"/>`;
    if (bad) bars += `<rect x="${x}" y="${Y(ok + bad)}" width="${w}" height="${Y(ok) - Y(ok + bad) - 1}" rx="1.5" fill="#4c5866"/>`;
  }
  const dl = data.research.filter(r => r.state === 'delivered' && r.updated_at <= now).map(r => r.updated_at).sort((a, b) => a - b), total = data.research.filter(r => r.state === 'delivered').length;
  let d = `M0,170`; dl.forEach((s, i) => { d += ` H${X(s).toFixed(1)} V${(170 - (i + 1) / total * 150).toFixed(1)}`; }); d += ` H${X(now).toFixed(1)}`;
  const people = data.notes.filter(n => n.who === 'human' && n.at <= now).map(n => `<line x1="${X(n.at)}" x2="${X(n.at)}" y1="12" y2="170" stroke="#f0c36e" stroke-width="1" stroke-dasharray="2 3" opacity=".6" vector-effect="non-scaling-stroke"/><circle cx="${X(n.at)}" cy="10" r="4" fill="#f0c36e"/>`).join('');
  $('trend-svg').innerHTML = `<line x1="0" x2="1000" y1="170" y2="170" stroke="rgba(150,175,200,.2)" vector-effect="non-scaling-stroke"/>${bars}${people}<path d="${d}" fill="none" stroke="#e3eaf0" stroke-width="1.6" vector-effect="non-scaling-stroke"/>`;
  $('trend-axis').innerHTML = [0, .25, .5, .75, 1].map(f => `<span>${md(T0 + (T1 - T0) * f)} ${hm(T0 + (T1 - T0) * f)}</span>`).join('');
}
for (const [btn, panel, render] of [['t-wins', 'wins', wins], ['t-trend', 'trend', trend]]) {
  const toggle = open => { $(panel).hidden = !open; $(btn).setAttribute('aria-pressed', String(open)); if (open) render(); };
  $(btn).addEventListener('click', () => toggle($(panel).hidden));
  $(btn).addEventListener('mouseenter', () => { if ($(panel).hidden) { toggle(true); $(panel).dataset.peek = '1'; } });
  $(btn).addEventListener('mouseleave', () => { if ($(panel).dataset.peek) { delete $(panel).dataset.peek; toggle(false); } });
  $(btn).addEventListener('click', () => { delete $(panel).dataset.peek; }, true);
}
$('t-story').addEventListener('click', () => leave(PAGES.story));
$('t-fit').addEventListener('click', () => { touched = performance.now(); flyTo(boundsOf([...trees.values()].filter(t => t.shown)), 1200); });

// ── Start: either at the beginning of the record or at ?t=<unix seconds> (for stills).
const jump = Number(Q.get('t') || 0);
if (jump) { now = jump; while (ei < events.length && events[ei].at <= jump) play(events[ei++], false); }
{ const t = focus?.shown ? focus : trees.get(tasks[0].id); if (t.shown) setView(viewFor(boundsOf([t]))); else setView({k: .9, cx: t.x + 400, cy: t.y}); }
if (Q.get('view') === 'all') setView(viewFor(boundsOf([...trees.values()].filter(t => t.shown)), 1));
for (const p of Object.values(phones)) { const live = data.jobs.find(j => phoneOf(j) === p && j.research_id && begun(j) <= now && (!j.ended || j.ended > now)); if (live) phoneBusy(p, live); }
for (const a of Object.values(agents)) {
  const running = data.jobs.filter(j => j.agent === a.name && j.research_id && begun(j) <= now && (!j.ended || j.ended > now)), live = running.find(phoneOf) || running[0];
  const last = events.slice(0, ei).reverse().find(e => (e.j?.agent || e.n?.who) === a.name);
  const queued = data.jobs.find(j => j.agent === a.name && j.research_id && waited(j) && j.queued <= now && j.claimed > now);
  if (queued) { a.at = {wait: queued}; a.waitSince = queued.queued; a.doing = '排队等手机'; }
  else if (live && phoneOf(live)) { a.at = {phone: phoneOf(live)}; a.doing = `在${BRAND[phoneOf(live).key]}上 · ${live.kind}`; }
  else if (last) { a.at = {tree: (last.j || last.n).research_id}; a.doing = last.n ? '写记录' : '写报告'; }
}
if (Q.get('panels')) for (const id of Q.get('panels').split(',')) $('t-' + id)?.click();
counts(); roster(); clock();

let last = performance.now(), paused = Q.get('paused') === '1', tick = 0, finished = false;
addEventListener('keydown', e => { if (e.key === ' ') { paused = !paused; e.preventDefault(); } });
function frame(t) {
  const dt = Math.min(.1, (t - last) / 1000); last = t;
  if (!paused && !hold && now < T1 && document.visibilityState === 'visible') {
    const next = events[ei]?.at ?? T1, gap = next - now > 1800 ? 8 : 1;
    now = Math.min(T1, now + dt * SPEED * gap);
    let changed = false;
    while (!hold && ei < events.length && events[ei].at <= now) { const ev = events[ei++]; play(ev, true); changed = true; if (ev.kind === 'task') now = ev.at; }
    if (changed) counts();
    for (const p of Object.values(phones)) if (p.job?.ended) p.card.querySelector('.bar i').style.width = Math.min(100, (now - begun(p.job)) / (p.job.ended - begun(p.job)) * 100) + '%';
  }
  runQueue(); stepFlight(t); stepAgents(t); stepTalk(t); clock();
  if (++tick % 15 === 0) { roster(); if (!$('wins').hidden) wins(); if (!$('trend').hidden) trend(); }
  if (now >= T1 && !finished) {
    finished = true; window.__done = true;
    for (const t of dock.querySelectorAll('.temp')) t.firstChild.textContent = t.dataset.now;
    if (Q.get('kiosk')) kiosk();
  }
  requestAnimationFrame(frame);
}
async function kiosk() {
  const wait = ms => new Promise(r => setTimeout(r, ms));
  await flyTo(boundsOf([...trees.values()].filter(t => t.shown)), 2000); await wait(3000);
  for (let i = 0; i < 3; i++) dock.append(el('div', 'phone ghost', '<span>+</span>'));
  await wait(2500);
  $('t-trend').click(); await wait(5000); $('t-trend').click();
  $('t-wins').click(); await wait(7000);
  window.__kioskDone = true; parent.postMessage({showcase: 'a-done'}, '*');
  if (Q.get('kiosk') === 'go') leave(`${PAGES.story}?kiosk=loop`);
}
requestAnimationFrame(frame);
setTimeout(() => { window.__ready = true; }, 1500);
