// Concept B: keynote-style story. One persistent 3D world; each chapter moves the camera, dims what is not the
// subject and shows one line in large type. ?ch=1..6 holds a chapter (stills); ?auto=1 plays 1→6 and reports done
// to the page that framed it. Every number comes from data.json.
import {THREE, loadData, stage, phone, orb, beam, starfield, textureFrom, statusScreen, rng, fmt, CSS2DObject} from './showcase-parts.js';

const data = await loadData();
const Q = new URLSearchParams(location.search);
const PAGES = {story: '/dash/showcase', command: '/dash/showcase/command'};
const leave = url => { document.body.classList.add('leaving'); setTimeout(() => { location.href = url; }, 700); };
const BG = 0x151d26; // reads as #0c1218 after tone mapping, matching the command view
const S = stage(document.getElementById('c'), {bloom: 0.45, radius: 0.35, threshold: 0.9, background: BG, fog: {color: BG, density: 0.018}});
const {scene, camera} = S;
scene.add(new THREE.AmbientLight(0x9aa8b8, 0.75));
const sun = new THREE.DirectionalLight(0xffffff, 1.1); sun.position.set(5, 14, 8); scene.add(sun);
{ const stars = starfield(420, 140); stars.material.opacity = 0.3; stars.material.color.set(0x5d6f84); scene.add(stars); }

const GOLD = new THREE.Color(0xf0c36e), STEEL = new THREE.Color(0x8aa1b5), ASH = new THREE.Color(0x3d4854), RUN = new THREE.Color(0x5d6f80), TEAL = new THREE.Color(0x5ccfc0);
const AGENT = {'kimi-researcher': 0x7cc4ff, 'glm-researcher': 0xb49cff, 'claude-lead': 0xff9f7a, 'glm-ops': 0xe39ad6};
const tag = (html, cls, pos, parent = scene) => { const d = document.createElement('div'); d.className = cls; d.innerHTML = html; const o = new CSS2DObject(d); o.position.copy(pos); parent.add(o); return o; };

// ── The tree of everything tried: eras from the repos (real commit counts), then the platform's tasks.
const r = rng(21), tree = new THREE.Group(); tree.position.set(-9, 0, -2); scene.add(tree);
const branch = (from, to, color, width, gain = 1) => {
  const mid = from.clone().lerp(to, 0.5).add(new THREE.Vector3((r() - 0.5) * 0.8, 0.4, (r() - 0.5) * 0.8));
  tree.add(new THREE.Mesh(new THREE.TubeGeometry(new THREE.QuadraticBezierCurve3(from, mid, to), 20, width, 6, false),
    new THREE.MeshBasicMaterial({color: color.clone().multiplyScalar(gain), toneMapped: gain <= 1})));
};
const leaves = [];
const leaf = (p, color, size, gain = 1) => leaves.push({p, color: color.clone().multiplyScalar(gain), size});
const trunkTop = new THREE.Vector3(0, 3.2, 0);
branch(new THREE.Vector3(0, 0, 0), trunkTop, STEEL, 0.13, 0.75);
const H = data.history;
const eras = [
  {name: 'phonefarm 真机底座', n: 615, since: '8/29', twigs: 40},
  {name: 'game_opt_loop 优化闭环', n: 168, since: '9/21', twigs: 24},
  {name: 'sr_loop 超分进化', n: H.sr_networks, since: '9/09', unit: '个网络', twigs: H.sr_networks, kept: H.sr_networks_kept},
  {name: 'experiments 研究线', n: 131, since: '10/21', twigs: 30},
  {name: 'HGamey 平台', n: 291, since: '10/11', twigs: 18},
];
const eraTips = [];
eras.forEach((era, i) => {
  const a = -Math.PI * 0.95 + (i / (eras.length - 1)) * Math.PI * 0.9 + (r() - 0.5) * 0.2;
  const base = new THREE.Vector3(0, 1.0 + i * 0.5, 0), end = new THREE.Vector3(Math.cos(a) * 4.2, 3.0 + i * 0.9 + r(), Math.sin(a) * 2.2 - 0.5);
  branch(base, end, STEEL, 0.025 + Math.log10(era.n) * 0.02, 0.7);
  for (let k = 0; k < era.twigs; k++) {
    const b = a + (r() - 0.5) * 1.6, len = 0.7 + r() * 1.6;
    const tip = end.clone().add(new THREE.Vector3(Math.cos(b) * len, r() * 1.4 - 0.2, Math.sin(b) * len * 0.6));
    const kept = era.kept ? k < era.kept : r() < 0.18;
    branch(end, tip, kept ? GOLD : ASH, 0.007, kept ? 1.15 : 1);
    leaf(tip, kept ? GOLD : ASH, kept ? 0.34 : 0.18, kept ? 1.2 : 1);
  }
  eraTips.push({era, end});
});
// The platform's tasks grow from the top: verified directions gold, rejected ash, to-do steel.
const top = new THREE.Vector3(0, 7.2, 0); branch(trunkTop, top, STEEL, 0.1, 0.75);
const tasks = [...data.research].sort((a, b) => a.updated_at - b.updated_at);
tasks.forEach((task, i) => {
  const a = (i / tasks.length) * Math.PI * 2, end = top.clone().add(new THREE.Vector3(Math.cos(a) * 2.6, 1.6 + r() * 1.4, Math.sin(a) * 1.6));
  branch(top, end, task.state === 'delivered' ? GOLD : STEEL, 0.04, task.state === 'delivered' ? 0.9 : 0.8);
  for (const d of data.directions.filter(x => x.research_id === task.id)) {
    const b = a + (r() - 0.5) * 1.2, tip = end.clone().add(new THREE.Vector3(Math.cos(b) * 1.4, 1 + r(), Math.sin(b) * 0.9));
    const c = d.state === 'verified' ? GOLD : d.state === 'rejected' ? ASH : STEEL;
    branch(end, tip, c, 0.026, d.state === 'verified' ? 1.25 : 0.9);
    leaf(tip, c, d.state === 'verified' ? 0.6 : 0.45, d.state === 'verified' ? 1.3 : 1);
  }
  for (const j of data.jobs.filter(x => x.research_id === task.id)) {
    const b = a + (r() - 0.5) * 2, tip = end.clone().add(new THREE.Vector3(Math.cos(b) * (0.5 + r()), r() * 1.1, Math.sin(b) * 0.6));
    const ok = j.outcome === 'succeeded';
    branch(end, tip, ok ? RUN : ASH, 0.009, 1); leaf(tip, ok ? TEAL : ASH, 0.16, ok ? 0.9 : 1);
  }
});
{
  const g = new THREE.BufferGeometry(), pos = [], col = [], size = [];
  for (const l of leaves) { pos.push(l.p.x, l.p.y, l.p.z); col.push(l.color.r, l.color.g, l.color.b); size.push(l.size); }
  g.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3)); g.setAttribute('color', new THREE.Float32BufferAttribute(col, 3)); g.setAttribute('size', new THREE.Float32BufferAttribute(size, 1));
  tree.add(new THREE.Points(g, new THREE.ShaderMaterial({transparent: true, depthWrite: false, vertexColors: true,
    vertexShader: 'attribute float size; varying vec3 vColor; void main(){ vColor = color; vec4 mv = modelViewMatrix * vec4(position,1.0); gl_PointSize = size * (320.0 / -mv.z); gl_Position = projectionMatrix * mv; }',
    fragmentShader: 'varying vec3 vColor; void main(){ float d = length(gl_PointCoord - 0.5) * 2.0; float a = smoothstep(1.0, 0.55, d); gl_FragColor = vec4(vColor, a); }'})));
}

// ── Phones on a low shelf, gold when warm, and the agents above them.
const names = {HONOR: '荣耀', REDMAGIC: '红魔', HUAWEI: '华为'};
const latest = {}; for (const j of data.jobs) if (j.device && j.agent && (!latest[j.agent] || j.started > latest[j.agent].started)) latest[j.agent] = j;
const holder = {}; for (const j of Object.values(latest)) holder[j.device] = j;
const shelf = new THREE.Mesh(new THREE.BoxGeometry(9, 0.12, 2.2), new THREE.MeshStandardMaterial({color: 0x141c25, metalness: 0.6, roughness: 0.45}));
shelf.position.set(5, 2.6, 2); scene.add(shelf);
const honorShot = await textureFrom('/dash/api/showcase/screen/HONOR');
const phones = {};
data.phones.forEach((p, i) => {
  const key = p.brand.toUpperCase(), name = `${names[key]} ${p.model}`, temp = p.soc_c || p.temp_c, busy = holder[key];
  const task = busy ? (data.byResearch(busy.research_id)?.title || '').replace(/[，,；;].*$/, '') : '';
  const m = phone({texture: key === 'HONOR' ? honorShot : statusScreen({name, state: busy ? 'testing' : 'idle', temp, task}), temp, state: busy ? 'testing' : 'cooling'});
  m.userData.aura.material.uniforms.color.value.set(temp >= 40 ? 0xf0c36e : 0x6f8aa3);
  m.userData.aura.material.uniforms.opacity.value = 0.22;
  m.scale.setScalar(0.8); m.position.set(2.2 + i * 2.8, 3.95, 2); m.rotation.y = -0.12 + i * 0.06; scene.add(m);
  tag(`${name}<small>${busy ? '实验中' : '空闲'} · ${temp.toFixed(1)}°C</small>`, 'b-phone', new THREE.Vector3(0, -2.3, 0), m);
  phones[key] = m;
});
const agentAt = {'glm-researcher': new THREE.Vector3(3.2, 8.4, 1), 'kimi-researcher': new THREE.Vector3(7.6, 8.8, 1), 'claude-lead': new THREE.Vector3(-1.6, 9.6, 0), 'glm-ops': new THREE.Vector3(10.6, 9.4, 0)};
const flowing = [], orbs = [];
for (const name of data.agents) {
  if (!agentAt[name]) continue;
  const o = orb(AGENT[name] || 0xa3b3c2, 0.2, {glowSize: 3, bright: 1.05}); o.position.copy(agentAt[name]); scene.add(o); orbs.push(o);
  const j = latest[name], doing = j && phones[j.device] ? `在${names[j.device]}上实验` : '写报告';
  tag(`${name}<small>${doing}</small>`, 'b-agent', new THREE.Vector3(0, 0.75, 0), o);
  if (j && phones[j.device]) {
    const b = beam(o.position, phones[j.device].position.clone().add(new THREE.Vector3(0, 1.3, 0.2)), 0x5ccfc0, {width: 0.016});
    b.userData.material.uniforms.color.value.set(0x5ccfc0).multiplyScalar(0.9); scene.add(b); flowing.push(b.userData.material);
  }
}
const ghosts = [];
for (let i = 0; i < 3; i++) {
  const ghost = new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.BoxGeometry(1.2, 2.48, 0.13)), new THREE.LineDashedMaterial({color: 0xf0c36e, dashSize: 0.12, gapSize: 0.1, transparent: true, opacity: 0.75}));
  ghost.computeLineDistances(); ghost.position.set(10.6 + i * 2.6, 3.95, 2); ghost.rotation.y = 0.05 * i; ghost.visible = false; scene.add(ghost); ghosts.push(ghost);
}
eraTips.forEach(({era, end}) => tag(`${era.name}<small>${era.since} 起 · ${fmt(era.n)} ${era.unit || '次提交'}${era.kept ? ` · 留下 ${era.kept} 个` : ''}</small>`, 'b-era', end.clone().add(new THREE.Vector3(0, 0.5, 0)), tree));

// ── Chapters: camera, what stays lit, one line of fact.
const begun = j => j.claimed || j.started;  // started is the submit time
const runH = data.jobs.filter(j => j.started && j.ended).reduce((s, j) => s + j.ended - begun(j), 0) / 3600;
const day = data.jobs.filter(j => j.phone && j.started > data.at - 86400), dayH = day.filter(j => j.ended).reduce((s, j) => s + j.ended - begun(j), 0) / 3600;
const waits = data.jobs.filter(j => j.phone && j.queued && j.claimed && j.claimed - j.queued > 30).map(j => (j.claimed - j.queued) / 60);
const commits = Object.values(H.commits).reduce((a, b) => a + b, 0);
const humans = data.notes.filter(n => n.who === 'human'), adopts = data.notes.filter(n => n.decision === 'adopt').length;
const delivered = data.research.filter(x => x.state === 'delivered').length, verified = data.directions.filter(d => d.state === 'verified').length;
const phoneLine = data.phones.map(p => `${names[p.brand.toUpperCase()]} ${p.model}`).join(' · ');
const CH = {
  1: {cam: [4.6, 6.4, 23], look: [4.6, 5.0, 1], kicker: '此刻', title: `${data.agents.length} 个 Agent，${data.phones.length} 台手机`, sub: `过去 24 小时 · ${day.length} 次真机实验 · 共 ${dayH.toFixed(1)} 小时`},
  2: {cam: [-7.5, 7.5, 13], look: [-8.6, 5.4, -2], kicker: '来时路', title: `${fmt(commits)} 次提交`, sub: `8 月 29 日起 · ${H.experiment_lines.length} 条实验线 · ${data.jobs.length} 次平台实验`},
  3: {cam: [0, 6, 22], look: [0, 5, 0], kicker: '人和 Agent', title: `${humans.length} 次人的回复，${adopts} 条采纳记录`, sub: '', chart: true},
  4: {cam: [5.4, 4.2, 12.5], look: [5.4, 2.6, 2], kicker: '手机', title: `${data.phones.length} 台手机`,
      sub: `${phoneLine}\n${waits.length} 次实验排队等手机，最长 ${Math.round(Math.max(0, ...waits))} 分钟`},
  5: {cam: [0, 6, 22], look: [0, 5, 0], kicker: '成果', title: `${delivered} 份交付报告`, sub: '', wins: true},
  6: {cam: [9, 4.4, 18], look: [8.4, 2.8, 1], kicker: '下一步', title: '手机翻倍，实验线翻倍', sub: '', more: true},
};
const N = Object.keys(CH).length;

// Chapter 3 chart: hourly runs as bars, deliveries as a white step line, people's replies as gold pins.
{
  const box = document.getElementById('chart');
  const t0 = Math.min(...humans.map(n => n.at)) - 6 * 3600, t1 = data.at, X = s => (s - t0) / (t1 - t0) * 1600;
  const hours = Math.ceil((t1 - t0) / 3600), per = [...Array(hours).keys()].map(i => data.jobs.filter(j => j.ended >= t0 + i * 3600 && j.ended < t0 + (i + 1) * 3600));
  const max = Math.max(1, ...per.map(p => p.length)), bw = 1600 / hours;
  const bars = per.map((p, i) => { const ok = p.filter(j => j.outcome === 'succeeded').length, h1 = ok / max * 150, h2 = (p.length - ok) / max * 150;
    return (ok ? `<rect x="${(i * bw + bw * .2).toFixed(1)}" y="${(430 - h1).toFixed(1)}" width="${(bw * .6).toFixed(1)}" height="${h1.toFixed(1)}" rx="2" fill="#5ccfc0" opacity=".8"/>` : '') +
      (p.length - ok ? `<rect x="${(i * bw + bw * .2).toFixed(1)}" y="${(430 - h1 - h2 - 2).toFixed(1)}" width="${(bw * .6).toFixed(1)}" height="${h2.toFixed(1)}" rx="2" fill="#3d4854"/>` : ''); }).join('');
  const dl = data.research.filter(x => x.state === 'delivered').map(x => x.updated_at).sort((a, b) => a - b);
  let d = 'M0,430 '; dl.forEach((s, i) => { d += `H${X(s).toFixed(1)} V${(430 - (i + 1) / dl.length * 380).toFixed(1)} `; }); d += 'H1600';
  const pins = humans.map(n => `<g transform="translate(${X(n.at).toFixed(1)},0)"><line y1="34" y2="430" stroke="#f0c36e" stroke-width="1.5" stroke-dasharray="3 6" opacity=".6"/><circle cy="30" r="7" fill="#f0c36e"/></g>`).join('');
  box.querySelector('svg').innerHTML = `<line x1="0" x2="1600" y1="430" y2="430" stroke="rgba(150,175,200,.25)"/>${bars}${pins}<path d="${d}" fill="none" stroke="#e3eaf0" stroke-width="3" stroke-linejoin="round"/>`;
  const note = humans.find(n => n.id === 'n-08e1844b4e04') || humans.find(n => n.text.startsWith('指导')) || humans[0];
  box.querySelector('.quote').innerHTML = `<b>人</b>${note.text.length > 60 ? note.text.slice(0, 60) + '…' : note.text}`;
  box.querySelector('.legend').innerHTML = `<span><i style="--c:#5ccfc0"></i>成功的实验</span><span><i style="--c:#3d4854"></i>未成功</span><span><i style="--c:#e3eaf0"></i>交付报告</span><span><i style="--c:#f0c36e"></i>人的回复</span>`;
}
document.getElementById('wins').innerHTML = [[delivered, '份', '交付报告'], [verified, '条', '验证有效的方向'], [H.sr_networks_kept, '个', `超分网络（从 ${H.sr_networks} 个里留下）`],
  [data.jobs.filter(j => j.phone).length, '次', `真机实验 · 共 ${data.jobs.length} 次 · ${runH.toFixed(1)} 小时`], [humans.length, '次', '人的回复'], [fmt(commits), '次', '代码、实验与文档提交']]
  .map(([n, u, t], i) => `<div class="${i === 0 ? 'gold' : ''}"><b>${n}<small>${u}</small></b><span>${t}</span></div>`).join('');

// ── Showing a chapter: camera glides (or jumps for stills), text crossfades, overlays fade in over a dimmed world.
const cam = {from: null, to: null, t0: 0, ms: 1800};
const look = new THREE.Vector3();
let current = 0;
function show(n, instant = false) {
  const c = CH[n]; current = n;
  const target = {p: new THREE.Vector3(...c.cam), l: new THREE.Vector3(...c.look)};
  if (instant || !cam.to) { camera.position.copy(target.p); look.copy(target.l); camera.lookAt(look); cam.to = target; cam.from = null; }
  else { cam.from = {p: camera.position.clone(), l: look.clone()}; cam.to = target; cam.t0 = performance.now(); }
  const cap = document.querySelector('.caption');
  cap.classList.add('out');
  setTimeout(() => {
    document.getElementById('kicker').textContent = c.kicker;
    document.getElementById('title').textContent = c.title;
    document.getElementById('sub').textContent = c.sub;
    cap.classList.remove('out');
  }, instant ? 0 : 300);
  document.querySelectorAll('.dots i').forEach((d, i) => d.classList.toggle('on', i + 1 === n));
  document.body.className = `ch${n}` + (c.chart || c.wins ? ' overlay' : '');
  document.getElementById('chart').hidden = !c.chart; document.getElementById('wins').hidden = !c.wins;
  for (const g of ghosts) g.visible = !!c.more;
}
const ease = t => t < .5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
const timer = new THREE.Timer();
let running = true, token = 0;
(function tick(now) {
  if (!running) { requestAnimationFrame(tick); return; }
  timer.update(now); const e = timer.getElapsed();
  if (cam.from) {
    const p = Math.min(1, (performance.now() - cam.t0) / cam.ms), k = ease(p);
    camera.position.lerpVectors(cam.from.p, cam.to.p, k); look.lerpVectors(cam.from.l, cam.to.l, k);
    if (p >= 1) cam.from = null;
  } else if (cam.to) {
    // A slow drift keeps the held shot alive.
    camera.position.copy(cam.to.p).add(new THREE.Vector3(Math.sin(e * .12) * .5, Math.sin(e * .09) * .2, 0)); look.copy(cam.to.l);
  }
  camera.lookAt(look);
  for (const m of flowing) m.uniforms.time.value = e;
  for (const p of Object.values(phones)) p.userData.aura.material.uniforms.time.value = e;
  for (const o of orbs) o.userData.ring.rotation.z = e * .6;
  S.frame(); requestAnimationFrame(tick);
})(performance.now());

const start = Number(Q.get('ch') || 1);
show(start, true);
// Playback: ?auto=1 plays from the start chapter on load; a framing page can also send {cmd: 'play' | 'pause'}.
// Paused means no rendering at all, so a hidden story costs nothing while the command view runs.
async function playAll(from) {
  const mine = ++token, wait = ms => new Promise(r => setTimeout(r, ms)), HOLD = Number(Q.get('hold') || 7000);
  for (let n = from; n <= N; n++) {
    if (mine !== token) return;
    if (n !== from || current !== n) show(n);
    await wait(HOLD + (CH[n].chart || CH[n].wins ? 2500 : 0));
  }
  if (mine !== token) return;
  window.__done = true; parent.postMessage({showcase: 'b-done'}, '*');
  // ?kiosk=go: the story once, then the command view; ?kiosk=loop: the story over and over.
  if (Q.get('kiosk') === 'go') leave(`${PAGES.command}?kiosk=go`);
  if (Q.get('kiosk') === 'loop') { document.body.classList.add('leaving'); await wait(800); show(1, true); document.body.classList.remove('leaving'); playAll(1); }
}
addEventListener('message', e => {
  if (e.data?.cmd === 'play') { running = true; show(1, true); playAll(1); }
  if (e.data?.cmd === 'pause') { running = false; token++; }
});
if (Q.get('auto') || Q.get('kiosk')) playAll(start);
addEventListener('keydown', e => {
  if (e.key === 'ArrowRight' && current < N) show(current + 1);
  if (e.key === 'ArrowLeft' && current > 1) show(current - 1);
  if (e.key === 'a' || e.key === 'A') leave(PAGES.command);
});
document.querySelector('.brand small').textContent = `${data.source || '本节点'} · 截至 ${new Intl.DateTimeFormat('zh-CN', {month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false}).format(new Date(data.at * 1000))}`;
setTimeout(() => { window.__ready = true; }, 1500);
