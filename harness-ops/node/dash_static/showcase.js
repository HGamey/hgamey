// Story page: six steps, each a fact in large type beside one subject in a single 3D world (phones and agents, the
// commits behind them, the record of people and agents, waiting for phones, results, what more phones would add).
// The camera frames each subject inside the area beside the text. → / Space / PageDown advance; #N names a step;
// ?kiosk=go|loop plays through and hands over to the command view. Data: the node's snapshot, else its live model.
import {THREE, BG, makeStage, label, phone, ghostPhone, statusScreen, textureFrom, dots, framing, fmt, RoundedBoxGeometry} from './showcase-parts.js';

const Q = new URLSearchParams(location.search);
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'})[c]);
const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
const BRAND = {HONOR: '荣耀', REDMAGIC: '红魔', NUBIA: '红魔', HUAWEI: '华为'};
const SAME = {NUBIA: 'REDMAGIC'}, key = b => { const k = String(b || '').toUpperCase(); return SAME[k] || k; };
const AGENT = {'kimi-researcher': [0x7cc4ff, 'kimi'], 'glm-researcher': [0xb49cff, 'glm'], 'claude-lead': [0xff9f7a, 'claude'], 'glm-ops': [0xe39ad6, 'glm-ops']};
const agentOf = a => AGENT[a] || [0xa3b3c2, String(a).replace(/-(researcher|agent)$/, '')];
const COLOR = {gold: 0xf0c36e, teal: 0x5ccfc0, ash: 0x4c5866, steel: 0x8aa1b5, ice: 0x8db4f5, white: 0xe3eaf0};
const stampFmt = new Intl.DateTimeFormat('zh-CN', {month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false});
const stamp = s => stampFmt.format(new Date(s * 1000));

// ── Data
async function load() {
  for (const url of ['/dash/api/showcase', '/dash/api/command']) {
    const r = await fetch(url, {credentials: 'same-origin'}).catch(() => null);
    if (r?.status === 401) { location.href = '/dash/login?next=' + encodeURIComponent(location.pathname + location.search); return null; }
    if (r?.ok) return {...await r.json(), from: url.endsWith('showcase') ? 'snapshot' : 'live'};
  }
  return null;
}
const data = await load();
if (!data || !(data.research || []).length) {
  $('empty').innerHTML = `<span>${data ? '这个节点还没有任务。' : '读不到数据。'}<button type="button" id="retry">重试</button></span>`;
  $('empty').hidden = false; $('retry').addEventListener('click', () => location.reload()); await new Promise(() => {});
}
const at = data.at, H = data.history;
const ids = new Set(data.research.map(r => r.id));
const jobs = data.jobs || [], notes = (data.notes || []).filter(n => ids.has(n.research_id)), phones = (data.phones || []).map(p => ({...p, key: key(p.brand)}));
const begun = j => j.claimed || (j.queued ? j.ended : j.started);
// Every count on this page is of the same thing: runs filed under a task. Totals count finished runs.
const taskJobs = jobs.filter(j => ids.has(j.research_id)), finished = taskJobs.filter(j => j.ended);
const phoneJobs = taskJobs.filter(j => j.phone && begun(j)), phoneDone = phoneJobs.filter(j => j.ended);
const day = phoneDone.filter(j => begun(j) >= at - 86400), dayH = day.reduce((s, j) => s + j.ended - begun(j), 0) / 3600;
const runningNow = phoneJobs.filter(j => !j.ended && begun(j) <= at);
const waits = phoneJobs.filter(j => j.queued && j.claimed && j.claimed - j.queued > 30);
const longest = Math.round(Math.max(0, ...waits.map(j => (j.claimed - j.queued) / 60)));
const humans = notes.filter(n => n.who === 'human'), adopts = notes.filter(n => n.decision === 'adopt').length;
const delivered = data.research.filter(r => r.state === 'delivered'), verified = (data.directions || []).filter(d => d.state === 'verified').length;
const ok = finished.filter(j => j.outcome === 'succeeded').length, done = finished.length;
const commits = H ? Object.values(H.commits).reduce((a, b) => a + b, 0) : 0;
$('src').textContent = `${data.source || '本节点'} · ${data.from === 'snapshot' ? '快照' : '实时'} · 截至 ${stamp(at)}`;

// ── World
let S = null;
try { S = makeStage($('c'), $('stage')); } catch (error) { document.body.classList.add('flat'); }
const W = S && S.scene, groups = {};
const area = (name, x, z) => { const g = new THREE.Group(); g.position.set(x, 0, z); if (W) W.add(g); groups[name] = g; return g; };
const tags = [];   // [object, steps it shows on]
const tagFor = (steps, ...args) => { if (!W) return null; const t = label(...args); tags.push([t, steps]); return t; };
const shadow = (w, d) => {
  const c = document.createElement('canvas'); c.width = c.height = 128; const g = c.getContext('2d'), grad = g.createRadialGradient(64, 64, 4, 64, 64, 64);
  grad.addColorStop(0, 'rgba(0,0,0,.55)'); grad.addColorStop(1, 'rgba(0,0,0,0)'); g.fillStyle = grad; g.fillRect(0, 0, 128, 128);
  const m = new THREE.Mesh(new THREE.PlaneGeometry(w, d), new THREE.MeshBasicMaterial({map: new THREE.CanvasTexture(c), transparent: true, depthWrite: false}));
  m.rotation.x = -Math.PI / 2; m.position.y = .005; return m;
};
const moved = (m, x, y, z) => { m.position.set(x, y, z); return m; };
const animate = [];   // per-frame updates (only for what is live)
const sub = {};       // named subjects → objects whose box a step frames

if (W) {
  W.add(new THREE.HemisphereLight(0xbfd2e6, 0x0c1218, .5));
  const sun = new THREE.DirectionalLight(0xffffff, 1.6); sun.position.set(-6, 12, 10); W.add(sun);
  const floor = new THREE.Mesh(new THREE.CircleGeometry(140, 64), new THREE.MeshStandardMaterial({color: BG, roughness: .92, metalness: 0}));
  floor.rotation.x = -Math.PI / 2; floor.position.y = -.01; W.add(floor);

  // Phones on a plinth, the agents using them above (live: glow and a flowing beam), idle agents further back.
  const deck = area('deck', 0, 0), gap = 2.4, n = phones.length, plinthW = Math.max(4, n * gap + 1.2);
  const plinth = new THREE.Mesh(new RoundedBoxGeometry(plinthW, .26, 2.4, 4, .08), new THREE.MeshPhysicalMaterial({color: 0x131b23, metalness: .35, roughness: .45, clearcoat: .3}));
  plinth.position.y = .13; deck.add(plinth, shadow(plinthW + 3, 5));
  const shot = data.from === 'snapshot' ? await textureFrom('/dash/api/showcase/screen/HONOR') : null;
  sub.phones = []; sub.agents = []; sub.queue = []; sub.ghosts = [];
  const phoneAt = {};
  phones.forEach((p, i) => {
    const running = runningNow.find(j => key(j.device) === p.key), temp = p.temp_c ?? p.soc_c, name = `${BRAND[p.key] || p.brand} ${p.model || ''}`.trim();
    const m = phone(p.key === 'HONOR' && shot ? shot : statusScreen({name, busy: !!running, temp, doing: running ? `${agentOf(running.agent)[1]} · ${running.kind}` : ''}));
    const x = (i - (n - 1) / 2) * gap; m.position.set(x, 1.82, 0); m.rotation.set(-.06, (i - (n - 1) / 2) * -.06, 0); deck.add(m);
    phoneAt[p.key] = m; sub.phones.push(m);
    tagFor([1], `${esc(name)}<small>${running ? '实验中' : '空闲'}${temp ? ` · 电池 ${temp.toFixed(0)}°C` : ''}</small>`, 'big', new THREE.Vector3(x, -.15, 1.3), deck);
  });
  // One bead per time a run waited for a phone, queued in front of the plinth (longer waits stand taller).
  waits.sort((a, b) => a.queued - b.queued).forEach((j, k) => {
    const mins = (j.claimed - j.queued) / 60, len = Math.min(1.2, .08 + Math.sqrt(mins) * .1);
    const bead = new THREE.Mesh(new THREE.CapsuleGeometry(.1, len, 4, 12), new THREE.MeshStandardMaterial({color: COLOR.ice, roughness: .4, metalness: .1}));
    bead.position.set((k - (waits.length - 1) / 2) * .36, .1 + len / 2 + .1, 3.1); bead.visible = false; deck.add(bead); sub.queue.push(bead);
  });
  if (waits.length) tagFor([4], `排队等手机<small class="mono">${waits.length} 次 · 最长 ${longest} 分钟</small>`, 'big', new THREE.Vector3(0, -.2, 3.75), deck);
  const live = new Set(runningNow.map(j => j.agent)), idle = (data.agents || []).filter(a => !live.has(a));
  for (const j of runningNow) {
    const ph = phoneAt[key(j.device)]; if (!ph) continue;
    const [c, short] = agentOf(j.agent), pos = new THREE.Vector3(ph.position.x + .2, 4.35, .5);
    const orb = new THREE.Mesh(new THREE.SphereGeometry(.17, 32, 16), new THREE.MeshBasicMaterial({color: new THREE.Color(c).multiplyScalar(2.4), toneMapped: false}));
    orb.position.copy(pos); deck.add(orb); sub.agents.push(orb);
    const curve = new THREE.QuadraticBezierCurve3(pos.clone(), new THREE.Vector3(pos.x - .1, 4.0, .4), new THREE.Vector3(ph.position.x, 3.42, .1));
    const mat = new THREE.ShaderMaterial({transparent: true, depthWrite: false, blending: THREE.AdditiveBlending, uniforms: {color: {value: new THREE.Color(c).multiplyScalar(1.6)}, time: {value: 0}},
      vertexShader: 'varying vec2 vUv; void main(){ vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position,1.0); }',
      fragmentShader: 'uniform vec3 color; uniform float time; varying vec2 vUv; void main(){ float d = smoothstep(.3,.5,fract(vUv.x*6.-time*1.2)); float a = (.25+.75*d); gl_FragColor = vec4(color*a, a); }'});
    deck.add(new THREE.Mesh(new THREE.TubeGeometry(curve, 24, .018, 6, false), mat));
    animate.push(t => { mat.uniforms.time.value = t; orb.position.y = pos.y + Math.sin(t * 1.3 + pos.x) * .05; });
    tagFor([1], `<span class="mono">${esc(short)}</span><small>${esc(j.kind)}</small>`, '', new THREE.Vector3(pos.x, pos.y + .42, pos.z), deck);
  }
  idle.forEach((a, i) => {
    const [c, short] = agentOf(a), pos = new THREE.Vector3((i - (idle.length - 1) / 2) * 1.5, 4.9, -1.6);
    const orb = new THREE.Mesh(new THREE.SphereGeometry(.14, 24, 12), new THREE.MeshStandardMaterial({color: c, roughness: .5, metalness: .1}));
    orb.position.copy(pos); deck.add(orb); sub.agents.push(orb);
    tagFor([1], `<span class="mono">${esc(short)}</span>`, 'dim', new THREE.Vector3(pos.x, pos.y + .36, pos.z), deck);
  });
  for (let i = 0; i < Math.max(1, n); i++) {
    const g = ghostPhone(), x = plinthW / 2 + .9 + i * gap; g.position.set(x, 1.82, 0); g.visible = false; deck.add(g); sub.ghosts.push(g);
  }
  const ext = new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.BoxGeometry(Math.max(1, n) * gap, .26, 2.4)), new THREE.LineDashedMaterial({color: COLOR.gold, dashSize: .12, gapSize: .1, transparent: true, opacity: .6}));
  ext.computeLineDistances(); ext.position.set(plinthW / 2 + Math.max(1, n) * gap / 2 + .1, .13, 0); ext.visible = false; deck.add(ext); sub.ghosts.push(ext);

  // Commits since the start, one dot each, a block per repository.
  if (H) {
    const hist = area('history', -38, -8); sub.history = [];
    const repos = Object.entries(H.commits).sort((a, b) => b[1] - a[1]), NAMES = {hgamey: '平台', docs: '记录', experiments: '实验线', sr_loop: '超分进化'};
    let x = 0;
    for (const [repo, count] of repos) {
      const side = 6, s = .22, pts = [], tone = new THREE.Color(repo === 'sr_loop' ? COLOR.gold : COLOR.steel).lerp(new THREE.Color(COLOR.white), .25);
      for (let i = 0; i < count; i++) {
        const a = i % side, b = Math.floor(i / side) % side, c = Math.floor(i / side / side);
        pts.push({x: x + a * s, y: .14 + c * s, z: b * s - side * s / 2, color: tone});
      }
      const block = dots(pts, {size: .17}); hist.add(block); sub.history.push(block);
      hist.add(moved(shadow(side * s + 1.4, side * s + 1.4), x + side * s / 2, .005, 0));
      tagFor([2], `${esc(NAMES[repo] || repo)}<small class="mono">${fmt(count)} 次提交</small>`, 'big', new THREE.Vector3(x + side * s / 2, -.2, side * s / 2 + .5), hist);
      x += side * s + 1.4;
    }
    groups.history.position.x -= x / 2;
  }

  // The record: finished runs per hour (teal succeeded, grey not), gold pillars where a person replied, a white line
  // stepping up at each delivery.
  {
    const rec = area('record', 0, -40); sub.record = [];
    const t0 = Math.min(...data.research.map(r => r.created_at)) - 1800, hours = Math.max(1, Math.ceil((at - t0) / 3600)), step = .46, X = t => (t - t0) / 3600 * step;
    const per = Array.from({length: hours}, (_, i) => jobs.filter(j => ids.has(j.research_id) && j.ended >= t0 + i * 3600 && j.ended < t0 + (i + 1) * 3600));
    const top = Math.max(1, ...per.map(p => p.length)), unit = 3.6 / top, box = new THREE.BoxGeometry(.3, 1, .3);
    const bars = (color, count) => new THREE.InstancedMesh(box, new THREE.MeshStandardMaterial({color, roughness: .45, metalness: .15}), Math.max(1, count));
    const good = bars(COLOR.teal, hours), bad = bars(0x3a4552, hours), M = new THREE.Matrix4();
    let gi = 0, bi = 0;
    per.forEach((p, i) => {
      const k = p.filter(j => j.outcome === 'succeeded').length, f = p.length - k;
      if (k) good.setMatrixAt(gi++, M.compose(new THREE.Vector3(i * step, k * unit / 2, 0), new THREE.Quaternion(), new THREE.Vector3(1, k * unit, 1)));
      if (f) bad.setMatrixAt(bi++, M.compose(new THREE.Vector3(i * step, k * unit + f * unit / 2 + .02, 0), new THREE.Quaternion(), new THREE.Vector3(1, f * unit, 1)));
    });
    good.count = gi; bad.count = bi; rec.add(good, bad); sub.record.push(good, bad);
    const base = new THREE.Mesh(new THREE.BoxGeometry(hours * step + .6, .03, .9), new THREE.MeshStandardMaterial({color: 0x1a232d, roughness: .8}));
    base.position.set((hours - 1) * step / 2, .015, 0); rec.add(base, moved(shadow(hours * step + 4, 6), (hours - 1) * step / 2, .004, 0));
    const pillar = new THREE.CylinderGeometry(.025, .025, 1, 8), gold = new THREE.MeshStandardMaterial({color: COLOR.gold, roughness: .55, metalness: .2});
    for (const n of humans) {
      const x = X(n.at) - step / 2, p = new THREE.Mesh(pillar, gold); p.scale.y = 4.6; p.position.set(x, 2.3, .55); rec.add(p);
      const cap = new THREE.Mesh(new THREE.SphereGeometry(.09, 16, 8), gold); cap.position.set(x, 4.65, .55); rec.add(cap);
    }
    const dl = delivered.map(r => r.updated_at).sort((a, b) => a - b), pts = [new THREE.Vector3(-step / 2, .05, -.55)];
    dl.forEach((t, i) => { const x = X(t) - step / 2, y = .05 + (i + 1) / dl.length * 4.3; pts.push(new THREE.Vector3(x, pts.at(-1).y, -.55), new THREE.Vector3(x, y, -.55)); });
    pts.push(new THREE.Vector3(hours * step - step / 2, pts.at(-1).y, -.55));
    rec.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts), new THREE.LineBasicMaterial({color: COLOR.white})));
    const tickStep = hours > 36 ? 12 : 6, first = Math.ceil(t0 / (tickStep * 3600)) * tickStep * 3600;
    for (let t = first; t < at; t += tickStep * 3600) tagFor([3], `<span class="mono">${stamp(t)}</span>`, 'dim', new THREE.Vector3(X(t) - step / 2, -.1, 1.7), rec);
  }

  // Results: a gold card per delivered report, and the super-resolution networks tried (gold: kept).
  {
    const res = area('results', 38, -8); sub.results = [];
    const card = new RoundedBoxGeometry(1.7, 1.1, .05, 3, .024), mat = new THREE.MeshStandardMaterial({color: COLOR.gold, metalness: .45, roughness: .5});   // history: no glow
    const face = new THREE.PlaneGeometry(1.6, 1.0), N = delivered.length, cols = Math.min(4, N), rows = Math.ceil(N / cols);
    [...delivered].sort((a, b) => a.updated_at - b.updated_at).forEach((r, i) => {
      const c = i % cols, row = Math.floor(i / cols), m = new THREE.Mesh(card, mat), a = (c - (cols - 1) / 2) * .26;
      const front = new THREE.Mesh(face, new THREE.MeshStandardMaterial({map: reportFace(r), roughness: .45, metalness: .25})); front.position.z = .027; m.add(front);
      m.position.set(Math.sin(a) * 7, .75 + (rows - 1 - row) * 1.28, -Math.cos(a) * 7 + 7); m.rotation.y = -a; res.add(m); sub.results.push(m);
    });
    if (H?.sr_networks) {
      const pts = [];
      for (let i = 0; i < H.sr_networks; i++) { const a = -Math.PI / 2 + i / H.sr_networks * Math.PI * 2; pts.push({x: Math.cos(a) * 1.9, y: .14, z: 3 + Math.sin(a) * 1.9, color: i < H.sr_networks_kept ? COLOR.gold : 0x6b7c8e}); }
      const ring = dots(pts, {size: .13}); res.add(ring); sub.results.push(ring);
      tagFor([5], `超分网络<small class="mono">留下 ${H.sr_networks_kept} / ${H.sr_networks}</small>`, '', new THREE.Vector3(0, -.2, 5.3), res);
    }
    tagFor([5], `交付报告<small class="mono">${N} 份</small>`, 'gold', new THREE.Vector3(0, rows * 1.28 + .5, 0), res);
    res.add(moved(shadow(10, 8), 0, .004, 1));
  }
}

// A delivered report's card face: its title's first clause and the delivery date.
function reportFace(r) {
  const c = document.createElement('canvas'); c.width = 544; c.height = 340; const g = c.getContext('2d');
  const grad = g.createLinearGradient(0, 0, 544, 340); grad.addColorStop(0, '#f3cf86'); grad.addColorStop(1, '#c99a45'); g.fillStyle = grad; g.fillRect(0, 0, 544, 340);
  const font = w => `${w} "PingFang SC","HarmonyOS Sans SC","Noto Sans CJK SC",sans-serif`;
  let title = (r.title || '').replace(/^\[[^\]]*\]\s*/, ''); const cut = [...title.matchAll(/[，,；;（(：:]/g)].map(m => m.index).find(i => i >= 10); if (cut) title = title.slice(0, cut);
  g.fillStyle = '#2b1e07'; g.font = font('700 46px');
  const lines = []; let line = '';
  for (const ch of title) { if (g.measureText(line + ch).width > 470) { lines.push(line); line = ''; } line += ch; }
  lines.push(line);
  if (lines.length > 3) { lines.length = 3; lines[2] = lines[2].slice(0, -1) + '…'; }
  lines.forEach((l, i) => g.fillText(l, 36, 84 + i * 62));
  g.fillStyle = 'rgba(43,30,7,.7)'; g.font = font('500 30px'); g.fillText(`交付 · ${stamp(r.updated_at)}`, 36, 304);
  const t = new THREE.CanvasTexture(c); t.colorSpace = THREE.SRGBColorSpace; t.anisotropy = 8; return t;
}

// ── Steps: what each says and what it frames
const v = (x, y, z) => new THREE.Vector3(x, y, z);
const AREA = {now: 'deck', history: 'history', record: 'record', phones: 'deck', results: 'results', next: 'deck'}, KEYS = Object.keys(AREA);
const STEPS = [
  {key: 'now', kicker: data.from === 'snapshot' ? stamp(at) : '此刻', title: `${(data.agents || []).length} 个 Agent，${phones.length} 台手机`, lead: '此前 24 小时',
    facts: [[day.length, '次', '完成的真机实验', 'teal'], [dayH.toFixed(1), '小时', '真机实验时长'], [runningNow.length, '个', '正在用手机的 Agent']],
    frame: () => [...sub.phones, ...sub.agents], dir: v(.12, .34, 1)},
  H && {key: 'history', kicker: '来时路', title: `${fmt(commits)} 次提交`, lead: `${H.since.replace(/^\d{4}-0?(\d+)-0?(\d+)$/, '$1 月 $2 日')}起`,
    facts: [[H.experiment_lines.length, '条', '实验线'], [H.sr_loop_generations, '代', '超分进化'], [data.research.length, '个', '平台上的任务']],
    frame: () => sub.history, dir: v(.55, .5, 1)},
  {key: 'record', kicker: '人和 Agent', title: `${humans.length} 次人的回复，${adopts} 条采纳记录`, lead: '',
    facts: [[ok, '次', '成功的实验', 'teal', 'sw-bar'], [done - ok, '次', '未成功的实验', '', 'sw-bar sw-ash'], [humans.length, '次', '人的回复', 'gold', 'sw-pin'], [delivered.length, '份', '交付报告（累计）', '', 'sw-line']],
    quote: humans.find(n => /^指导/.test(n.text)) || humans[0], frame: () => sub.record, dir: v(-.3, .42, 1)},
  {key: 'phones', kicker: '手机', title: waits.length ? `${waits.length} 次实验排队等手机` : `${phones.length} 台手机`, lead: phones.map(p => `${BRAND[p.key] || p.brand} ${p.model || ''}`.trim()).join(' · '),
    facts: [...(waits.length ? [[longest, '分钟', '最长一次等待', 'ice']] : []), [phoneDone.length, '次', '完成的真机实验'], [done, '次', '完成的实验（含宿主）']],
    frame: () => [...sub.phones, ...sub.agents, ...sub.queue], dir: v(.1, .62, 1), show: on => sub.queue.forEach(b => { b.visible = on; })},
  {key: 'results', kicker: '成果', title: `${delivered.length} 份交付报告`, lead: '',
    facts: [[verified, '条', '验证有效的方向', 'gold'], ...(H ? [[H.sr_networks_kept, '个', `超分网络（从 ${H.sr_networks} 个里留下）`, 'gold']] : []), [phoneDone.length, '次', `完成的真机实验 · 共 ${done} 次实验`], [humans.length, '次', '人的回复']],
    frame: () => sub.results, dir: v(0, .32, 1)},
  {key: 'next', kicker: '下一步', title: '手机翻倍，实验线翻倍', lead: `现在 ${phones.length} 台手机${H ? ` · ${H.experiment_lines.length} 条实验线` : ''}`,
    facts: [], frame: () => [...sub.phones, ...sub.ghosts], dir: v(.18, .3, 1), show: on => sub.ghosts.forEach(g => { g.visible = on; })},
].filter(Boolean).filter(s => !W || s.frame().length);

// ── Text
const dotsBox = $('dots');
STEPS.forEach((s, i) => { const b = document.createElement('button'); b.type = 'button'; b.setAttribute('aria-label', `第 ${i + 1} 步：${s.kicker}`); b.addEventListener('click', () => go(i)); const li = document.createElement('li'); li.append(b); dotsBox.append(li); });
function writeCopy(s) {
  $('kicker').textContent = s.kicker; $('title').textContent = s.title; $('lead').textContent = s.lead;
  $('facts').innerHTML = s.facts.map(([n, unit, what, tone, mark]) => `<div class="${tone || ''}"><dt>${esc(n)}<small>${esc(unit)}</small></dt><dd>${mark ? `<i class="sw ${mark}" aria-hidden="true"></i>` : ''}${esc(what)}</dd></div>`).join('');
  const q = s.quote; $('quote').hidden = !q;
  if (q) $('quote').innerHTML = `<b>人 · ${stamp(q.at)}</b>${esc(q.text.length > 90 ? q.text.slice(0, 89) + '…' : q.text)}`;
}

// ── Camera
const cam = {from: null, to: null, t0: 0, ms: 1300};
function region() {
  const w = innerWidth, h = innerHeight;
  const pad = $('copy').getBoundingClientRect().left || 48, col = $('copy').getBoundingClientRect().width;
  if (matchMedia('(max-aspect-ratio: 6/5)').matches) return {x: pad, y: 76, w: w - 2 * pad, h: Math.max(160, h * .52 - 76)};
  const x = pad + col + Math.max(32, pad * .8);
  return {x, y: 76, w: Math.max(200, w - x - pad * .6), h: h - 76 - 70};
}
function shot(i) {
  const s = STEPS[i], box = new THREE.Box3();
  for (const o of s.frame()) { o.updateWorldMatrix(true, true); box.expandByObject(o); }
  box.expandByVector(new THREE.Vector3(.3, .5, .3));
  return framing(S.camera, box, s.dir, region(), innerWidth, innerHeight);
}
function place(p) {
  S.camera.position.copy(p.position); S.camera.lookAt(p.target);
  S.camera.setViewOffset(innerWidth, innerHeight, p.offset[0], p.offset[1], innerWidth, innerHeight);
}
const easeIO = t => t < .5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;

// ── Navigation
let current = -1, swap = 0;
function go(i, instant = false) {
  i = Math.max(0, Math.min(STEPS.length - 1, i));
  if (i === current && !instant) return;
  const prev = current; current = i;
  const s = STEPS[i];
  for (const [k, step] of STEPS.entries()) step.show?.(k === i);
  // Only the subject on screen (and, while flying, the one being left) is drawn.
  const keep = new Set([AREA[s.key], prev >= 0 && !instant ? AREA[STEPS[prev].key] : null]);
  for (const [name, g] of Object.entries(groups)) g.visible = keep.has(name);
  clearTimeout(go.hide); if (keep.size > 1) go.hide = setTimeout(() => { for (const [name, g] of Object.entries(groups)) g.visible = name === AREA[STEPS[current].key]; }, cam.ms + 50);
  tagsFor(i);
  const copy = $('copy');
  clearTimeout(swap);
  if (instant || prev < 0) writeCopy(s);
  else { copy.classList.add('out'); swap = setTimeout(() => { writeCopy(s); copy.classList.remove('out'); }, 230); }
  for (const [k, li] of [...dotsBox.children].entries()) { const b = li.firstChild; if (k === i) b.setAttribute('aria-current', 'step'); else b.removeAttribute('aria-current'); }
  $('count').textContent = `${String(i + 1).padStart(2, '0')} / ${String(STEPS.length).padStart(2, '0')}`;
  $('prev').disabled = i === 0; $('next').disabled = i === STEPS.length - 1;
  if (location.hash !== `#${i + 1}`) history.replaceState(null, '', `${location.pathname}${location.search}#${i + 1}`);
  if (!S) return;
  const to = shot(i);
  if (instant || prev < 0 || reduced) { cam.from = null; cam.to = to; place(to); }
  else { cam.from = {position: S.camera.position.clone(), target: cam.to.target.clone(), offset: [...cam.to.offset]}; cam.to = to; cam.t0 = performance.now(); }
}
// Tags name the step they belong to by its place in the full story, so a story without history still matches them.
const ORDER = STEPS.map(s => KEYS.indexOf(s.key) + 1);
function tagsFor(i) { const n = ORDER[i]; for (const [t, steps] of tags) t.element.classList.toggle('off', !steps.includes(n)); }

$('prev').addEventListener('click', () => { stopAuto(); go(current - 1); });
$('next').addEventListener('click', () => { stopAuto(); go(current + 1); });
$('full').addEventListener('click', () => fullscreen());
function fullscreen() { if (document.fullscreenElement) document.exitFullscreen(); else document.documentElement.requestFullscreen?.().catch(() => {}); }
addEventListener('keydown', e => {
  if (e.isComposing || e.ctrlKey || e.metaKey || e.altKey) return;
  const next = ['ArrowRight', 'ArrowDown', 'PageDown', ' '].includes(e.key), back = ['ArrowLeft', 'ArrowUp', 'PageUp'].includes(e.key);
  if ((next || back) && e.target.closest?.('button, a') && e.key === ' ') return;
  if (next) { e.preventDefault(); stopAuto(); go(current + 1); }
  else if (back) { e.preventDefault(); stopAuto(); go(current - 1); }
  else if (e.key === 'Home') { stopAuto(); go(0); } else if (e.key === 'End') { stopAuto(); go(STEPS.length - 1); }
  else if (/^[1-9]$/.test(e.key)) { stopAuto(); go(Number(e.key) - 1); }
  else if (e.key === 'f' || e.key === 'F') fullscreen();
});
addEventListener('hashchange', () => { const n = Number(location.hash.slice(1)); if (n) go(n - 1); });
addEventListener('resize', () => { if (!S) return; S.resize(); if (current >= 0) { cam.from = null; cam.to = shot(current); place(cam.to); } });

// ── Frame loop (idle when the tab is hidden)
const clock = new THREE.Timer();
function frame(now) {
  requestAnimationFrame(frame);
  if (!S || document.visibilityState !== 'visible') return;
  clock.update(now); const t = clock.getElapsed();
  if (cam.from) {
    const p = Math.min(1, (performance.now() - cam.t0) / cam.ms), k = easeIO(p), to = cam.to, from = cam.from;
    const pos = from.position.clone().lerp(to.position, k), lift = from.position.distanceTo(to.position) * .12 * Math.sin(Math.PI * p);
    pos.y += lift;
    S.camera.position.copy(pos); S.camera.lookAt(from.target.clone().lerp(to.target, k));
    S.camera.setViewOffset(innerWidth, innerHeight, from.offset[0] + (to.offset[0] - from.offset[0]) * k, from.offset[1] + (to.offset[1] - from.offset[1]) * k, innerWidth, innerHeight);
    if (p >= 1) cam.from = null;
  } else if (cam.to && !reduced) {
    // A slow sway keeps a held shot alive without moving the subject out of its place.
    const rel = cam.to.position.clone().sub(cam.to.target), a = Math.sin(t * .13) * .035;
    rel.applyAxisAngle(new THREE.Vector3(0, 1, 0), a);
    S.camera.position.copy(cam.to.target).add(rel); S.camera.lookAt(cam.to.target);
  }
  for (const f of animate) f(t);
  S.render();
}

// ── Auto play (kiosk): hold each step, then hand over to the command view.
let auto = 0;
function stopAuto() { auto++; }
async function play(from) {
  const mine = ++auto, wait = ms => new Promise(r => setTimeout(r, ms)), HOLD = Number(Q.get('hold') || 9000);
  for (let i = from; i < STEPS.length; i++) {
    if (mine !== auto) return;
    go(i); await wait(HOLD + (STEPS[i].quote || STEPS[i].facts.length > 3 ? 2500 : 0));
  }
  if (mine !== auto) return;
  window.__done = true;
  if (Q.get('kiosk')) { document.body.style.transition = 'opacity .7s'; document.body.style.opacity = 0; await wait(700); location.href = '/dash/showcase/command?kiosk=loop'; }
}

// ── Start
const start = Number(location.hash.slice(1) || Q.get('ch') || 1) - 1;
if (S) S.resize();   // now that every dot field exists
go(start, true);
if (S) { S.render(); $('c').classList.add('on'); }
requestAnimationFrame(frame);
if (Q.get('kiosk') || Q.get('auto')) play(start);
window.__story = {go, steps: STEPS.length, get current() { return current; }};
setTimeout(() => { window.__ready = true; }, 400);
