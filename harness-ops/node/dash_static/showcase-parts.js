// Shared building blocks for the showcase prototypes (three.js r186). Real data only: data.json comes from a
// read-only production snapshot.
import * as THREE from './three.module.js';
import {EffectComposer} from './three-effect-composer.js';
import {RenderPass} from './three-render-pass.js';
import {UnrealBloomPass} from './three-unreal-bloom-pass.js';
import {OutputPass} from './three-output-pass.js';
import {RoundedBoxGeometry} from './three-rounded-box-geometry.js';
import {CSS2DRenderer, CSS2DObject} from './three-css2d-renderer.js';

export {THREE, CSS2DObject};

export async function loadData() {
  const response = await fetch('/dash/api/showcase', {credentials: 'same-origin'});
  if (!response.ok) { document.body.innerHTML = '<p class="empty">此节点还没有展示数据快照。</p>'; await new Promise(() => {}); }  // stop here quietly
  const data = await response.json();
  data.byResearch = id => data.research.find(r => r.id === id);
  return data;
}

// Seeded random so a still and a recording show the same tree.
export function rng(seed = 7) {
  let s = seed >>> 0;
  return () => { s = (s * 1664525 + 1013904223) >>> 0; return s / 4294967296; };
}

export function stage(canvas, {bloom = 0.9, radius = 0.6, threshold = 0.15, background = 0x05070d, fog = null} = {}) {
  const renderer = new THREE.WebGLRenderer({canvas, antialias: true, powerPreference: 'high-performance'});
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  renderer.setSize(canvas.clientWidth, canvas.clientHeight, false);
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.0;
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(background);
  if (fog) scene.fog = new THREE.FogExp2(fog.color, fog.density);
  const camera = new THREE.PerspectiveCamera(38, canvas.clientWidth / canvas.clientHeight, 0.1, 400);
  const composer = new EffectComposer(renderer);
  composer.addPass(new RenderPass(scene, camera));
  const bloomPass = new UnrealBloomPass(new THREE.Vector2(canvas.clientWidth, canvas.clientHeight), bloom, radius, threshold);
  composer.addPass(bloomPass);
  composer.addPass(new OutputPass());
  const labels = new CSS2DRenderer();
  labels.setSize(canvas.clientWidth, canvas.clientHeight);
  Object.assign(labels.domElement.style, {position: 'absolute', inset: '0', pointerEvents: 'none'});
  canvas.parentElement.append(labels.domElement);
  return {renderer, scene, camera, composer, labels, bloomPass,
    frame() { composer.render(); labels.render(scene, camera); }};
}

export function label(text, className, position) {
  const div = document.createElement('div');
  div.className = className; div.textContent = text;
  const obj = new CSS2DObject(div); if (position) obj.position.copy(position);
  return obj;
}

// Temperature → color: cool cyan at 30 °C, amber at 40, red at 46+.
export function heatColor(c) {
  const t = THREE.MathUtils.clamp((c - 30) / 16, 0, 1);
  const cool = new THREE.Color(0x35c6ff), warm = new THREE.Color(0xffa63d), hot = new THREE.Color(0xff3b30);
  return t < 0.6 ? cool.clone().lerp(warm, t / 0.6) : warm.clone().lerp(hot, (t - 0.6) / 0.4);
}

// A soft additive halo (used for heat aura and agent glow).
export function halo(color, size, opacity = 0.6) {
  const material = new THREE.ShaderMaterial({
    transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
    uniforms: {color: {value: new THREE.Color(color)}, opacity: {value: opacity}, time: {value: 0}},
    vertexShader: 'varying vec2 vUv; void main(){ vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position,1.0); }',
    fragmentShader: `uniform vec3 color; uniform float opacity; uniform float time; varying vec2 vUv;
      void main(){ vec2 p = vUv - 0.5; float d = length(p) * 2.0;
        float shimmer = 0.85 + 0.15 * sin(time * 2.0 + p.y * 18.0);
        float a = smoothstep(1.0, 0.0, d); a = a * a * opacity * shimmer; gl_FragColor = vec4(color * a, a); }`,
  });
  const mesh = new THREE.Mesh(new THREE.PlaneGeometry(size, size), material);
  mesh.renderOrder = -1;
  return mesh;
}

export function phone({texture, temp = 36, name = '', state = 'testing'}) {
  const group = new THREE.Group();
  const body = new THREE.Mesh(new RoundedBoxGeometry(1.5, 3.1, 0.16, 6, 0.16),
    new THREE.MeshStandardMaterial({color: 0x161a22, metalness: 0.7, roughness: 0.35}));
  group.add(body);
  const screenMat = new THREE.MeshBasicMaterial({color: 0xd8d8d8, map: texture || null});
  if (!texture) screenMat.color.set(0x0c1220);
  const screen = new THREE.Mesh(new THREE.PlaneGeometry(1.36, 2.92), screenMat);
  screen.position.z = 0.082; group.add(screen);
  const aura = halo(heatColor(temp), 5.2, state === 'cooling' ? 0.35 : 0.55);
  aura.position.z = -0.25; group.add(aura);
  group.userData = {aura, screen, temp, name, state};
  return group;
}

export function orb(color, radius = 0.22, {glowSize = 9, bright = 2.2} = {}) {
  const group = new THREE.Group();
  const core = new THREE.Mesh(new THREE.SphereGeometry(radius, 32, 16), new THREE.MeshBasicMaterial({color: new THREE.Color(color).multiplyScalar(bright), toneMapped: false}));
  group.add(core);
  const ring = new THREE.Mesh(new THREE.TorusGeometry(radius * 1.9, radius * 0.08, 8, 64), new THREE.MeshBasicMaterial({color, transparent: true, opacity: 0.7}));
  ring.rotation.x = Math.PI / 2.4; group.add(ring);
  const glow = halo(color, radius * glowSize, 0.7); group.add(glow);
  group.userData = {core, ring, glow};
  return group;
}

// A light beam from a to b with dashes flowing toward b (agent → phone: commands going in, data coming back).
export function beam(a, b, color, {width = 0.035, lift = 0.8} = {}) {
  const mid = a.clone().lerp(b, 0.5); mid.y += lift;
  const curve = new THREE.QuadraticBezierCurve3(a.clone(), mid, b.clone());
  const material = new THREE.ShaderMaterial({
    transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
    uniforms: {color: {value: new THREE.Color(color).multiplyScalar(1.8)}, time: {value: 0}},
    vertexShader: 'varying vec2 vUv; void main(){ vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position,1.0); }',
    fragmentShader: `uniform vec3 color; uniform float time; varying vec2 vUv;
      void main(){ float dash = smoothstep(0.35, 0.5, fract(vUv.x * 14.0 - time * 1.6));
        float edge = 1.0 - abs(vUv.y - 0.5) * 2.0; float a = (0.35 + 0.65 * dash) * edge;
        gl_FragColor = vec4(color * a, a); }`,
  });
  const mesh = new THREE.Mesh(new THREE.TubeGeometry(curve, 64, width, 8, false), material);
  mesh.userData = {material, curve};
  return mesh;
}

// The exploration tree: root goal → tasks → directions → runs. Real counts and states decide every branch.
// Branch colors: verified = gold, delivered task = warm white, rejected = ash, proposed (to do) = cyan buds.
export const TREE_COLORS = {verified: 0xffc857, delivered: 0xf3efe6, active: 0x7cf0c8, rejected: 0x59606c, proposed: 0x4fc3ff, run: 0x9fb4d8, failed: 0x6b4a4a};

export function tree(data, {seed = 11, height = 9, spread = 7} = {}) {
  const r = rng(seed), group = new THREE.Group(), tips = [], segments = [];
  const add = (from, to, color, width, kind) => {
    const mid = from.clone().lerp(to, 0.5).add(new THREE.Vector3((r() - 0.5) * 0.6, 0.3, (r() - 0.5) * 0.6));
    const curve = new THREE.QuadraticBezierCurve3(from, mid, to);
    const mat = new THREE.MeshBasicMaterial({color: new THREE.Color(color).multiplyScalar(kind === 'rejected' ? 0.55 : kind === 'verified' ? 2.2 : kind === 'proposed' ? 1.8 : 0.9), toneMapped: false, transparent: true, opacity: kind === 'rejected' ? 0.55 : 1});
    const mesh = new THREE.Mesh(new THREE.TubeGeometry(curve, 24, width, 6, false), mat);
    mesh.userData = {kind}; group.add(mesh); segments.push(mesh);
    return curve;
  };
  const root = new THREE.Vector3(0, 0, 0), trunkTop = new THREE.Vector3(0, height * 0.32, 0);
  add(root, trunkTop, 0xe8e2d4, 0.12, 'trunk');
  const tasks = [...data.research].sort((a, b) => (a.updated_at || 0) - (b.updated_at || 0));
  tasks.forEach((task, i) => {
    const angle = (i / tasks.length) * Math.PI * 2 + r() * 0.4, lean = 0.45 + r() * 0.5;
    const end = new THREE.Vector3(Math.cos(angle) * spread * lean * 0.55, height * (0.55 + r() * 0.2), Math.sin(angle) * spread * lean * 0.55);
    const taskColor = task.state === 'delivered' ? TREE_COLORS.delivered : TREE_COLORS.active;
    add(trunkTop, end, taskColor, 0.06, task.state);
    const dirs = data.directions.filter(d => d.research_id === task.id);
    const runs = data.jobs.filter(j => j.research_id === task.id);
    dirs.forEach((d, k) => {
      const a2 = angle + (k - (dirs.length - 1) / 2) * 0.5 + (r() - 0.5) * 0.3;
      const tip = end.clone().add(new THREE.Vector3(Math.cos(a2) * 1.6, 1.2 + r() * 1.2, Math.sin(a2) * 1.6));
      add(end, tip, TREE_COLORS[d.state] || TREE_COLORS.run, 0.04, d.state);
      tips.push({position: tip, state: d.state, title: d.title, research: task.title});
    });
    runs.forEach(job => {
      const a3 = angle + (r() - 0.5) * 1.8, len = 0.6 + r() * 1.1;
      const tip = end.clone().add(new THREE.Vector3(Math.cos(a3) * len, 0.3 + r() * 1.1, Math.sin(a3) * len));
      add(end, tip, job.outcome === 'succeeded' ? TREE_COLORS.run : TREE_COLORS.failed, 0.022, job.outcome === 'succeeded' ? 'run' : 'failed-run');
      tips.push({position: tip, state: job.outcome === 'succeeded' ? 'run' : 'failed', run: true});
    });
  });
  // Leaves: a point per tip, bright for verified, hollow-ish pulse for to-do, ash for rejected.
  const pos = [], col = [], size = [];
  for (const t of tips) {
    pos.push(t.position.x, t.position.y, t.position.z);
    const c = new THREE.Color(t.state === 'verified' ? TREE_COLORS.verified : t.state === 'proposed' ? TREE_COLORS.proposed : t.state === 'rejected' ? TREE_COLORS.rejected : t.state === 'failed' ? TREE_COLORS.failed : TREE_COLORS.run);
    col.push(c.r * 2, c.g * 2, c.b * 2); size.push(t.run ? 0.32 : 0.7);
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
  g.setAttribute('color', new THREE.Float32BufferAttribute(col, 3));
  g.setAttribute('size', new THREE.Float32BufferAttribute(size, 1));
  const leaves = new THREE.Points(g, new THREE.ShaderMaterial({
    transparent: true, depthWrite: false, blending: THREE.AdditiveBlending, vertexColors: true, uniforms: {time: {value: 0}},
    vertexShader: `attribute float size; varying vec3 vColor; uniform float time;
      void main(){ vColor = color; vec4 mv = modelViewMatrix * vec4(position,1.0); gl_PointSize = size * (300.0 / -mv.z) * (1.0 + 0.15 * sin(time * 2.0 + position.x * 3.0)); gl_Position = projectionMatrix * mv; }`,
    fragmentShader: `varying vec3 vColor; void main(){ float d = length(gl_PointCoord - 0.5) * 2.0; float a = smoothstep(1.0, 0.0, d); gl_FragColor = vec4(vColor * a, a); }`,
  }));
  group.add(leaves);
  group.userData = {tips, leaves, segments};
  return group;
}

export function starfield(count = 1600, radius = 120, seed = 3) {
  const r = rng(seed), pos = [];
  for (let i = 0; i < count; i++) {
    const v = new THREE.Vector3(r() - 0.5, r() - 0.5, r() - 0.5).normalize().multiplyScalar(radius * (0.4 + r() * 0.6));
    pos.push(v.x, v.y, v.z);
  }
  const g = new THREE.BufferGeometry(); g.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
  return new THREE.Points(g, new THREE.PointsMaterial({color: 0x6a7ba8, size: 0.25, sizeAttenuation: true, transparent: true, opacity: 0.55}));
}

// A phone screen drawn from real state when there is no usable screenshot (screen off, or none taken).
export function statusScreen({name, state, temp, task}) {
  const c = document.createElement('canvas'); c.width = 336; c.height = 720; const g = c.getContext('2d');
  const testing = state === 'testing', col = testing ? '#5ccfc0' : '#8aa1b5';
  const grad = g.createLinearGradient(0, 0, 0, 720); grad.addColorStop(0, testing ? '#10262a' : '#121b25'); grad.addColorStop(1, '#0a0f14');
  g.fillStyle = grad; g.fillRect(0, 0, 336, 720);
  g.fillStyle = col; g.font = '600 30px "PingFang SC","Noto Sans CJK SC",sans-serif'; g.fillText(testing ? '● 实验中' : '○ 空闲', 28, 90);
  g.fillStyle = '#eef2fb'; g.font = '700 120px "PingFang SC","Noto Sans CJK SC",sans-serif'; g.fillText(Math.round(temp) + '°', 28, 300);
  g.fillStyle = '#9fb0cc'; g.font = '500 28px "PingFang SC","Noto Sans CJK SC",sans-serif'; g.fillText(name, 28, 360);
  if (task) { g.fillStyle = '#c8d3e8'; g.font = '500 26px "PingFang SC","Noto Sans CJK SC",sans-serif';
    let line = '', y = 470; for (const ch of task) { if (g.measureText(line + ch).width > 280) { g.fillText(line, 28, y); line = ''; y += 38; if (y > 620) break; } line += ch; } if (y <= 620) g.fillText(line, 28, y); }
  const t = new THREE.CanvasTexture(c); t.colorSpace = THREE.SRGBColorSpace; return t;
}

export function textureFrom(url) {
  return new Promise(resolve => new THREE.TextureLoader().load(url, t => { t.colorSpace = THREE.SRGBColorSpace; resolve(t); }, undefined, () => resolve(null)));
}

export const AGENT_COLORS = {'glm-researcher': 0x7c5cff, 'kimi-researcher': 0x2fe0b0, 'claude-lead': 0xff8a3d};

export function fmt(n) { return new Intl.NumberFormat('zh-CN').format(n); }
