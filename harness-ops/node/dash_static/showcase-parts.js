// Rendering pieces for the story page (three.js r186): the renderer and its passes, a studio light environment,
// phones, dot fields, and the camera fit that frames a subject inside the part of the window beside the text.
import * as THREE from './three.module.js';
import {EffectComposer} from './three-effect-composer.js';
import {RenderPass} from './three-render-pass.js';
import {UnrealBloomPass} from './three-unreal-bloom-pass.js';
import {OutputPass} from './three-output-pass.js';
import {RoundedBoxGeometry} from './three-rounded-box-geometry.js';
import {CSS2DRenderer, CSS2DObject} from './three-css2d-renderer.js';

export {THREE, CSS2DObject, RoundedBoxGeometry};
export const BG = 0x0c1218;
const dotMaterials = new Set();   // point sizes are in world units; they need the drawing buffer's scale

export function makeStage(canvas, host) {
  const renderer = new THREE.WebGLRenderer({canvas, antialias: true, powerPreference: 'high-performance'});
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  renderer.toneMapping = THREE.NeutralToneMapping; renderer.toneMappingExposure = .95;
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(BG); scene.fog = new THREE.Fog(BG, 38, 95);
  const pmrem = new THREE.PMREMGenerator(renderer);
  scene.environment = pmrem.fromScene(studio(), .04).texture; pmrem.dispose();
  const camera = new THREE.PerspectiveCamera(30, 1, .1, 300);
  const target = new THREE.WebGLRenderTarget(1, 1, {type: THREE.HalfFloatType, samples: 4});
  const composer = new EffectComposer(renderer, target);
  composer.addPass(new RenderPass(scene, camera));
  const bloom = new UnrealBloomPass(new THREE.Vector2(1, 1), .38, .45, .92);   // only what is live is bright enough to bloom
  composer.addPass(bloom); composer.addPass(new OutputPass());
  const labels = new CSS2DRenderer();
  Object.assign(labels.domElement.style, {position: 'absolute', inset: '0', pointerEvents: 'none'});
  host.append(labels.domElement);
  const resize = () => {
    const w = host.clientWidth, h = host.clientHeight;
    renderer.setSize(w, h, false); composer.setPixelRatio(renderer.getPixelRatio()); composer.setSize(w, h); labels.setSize(w, h);
    camera.aspect = w / h; camera.updateProjectionMatrix();
    const scale = renderer.domElement.height / (2 * Math.tan(THREE.MathUtils.degToRad(camera.fov) / 2));
    for (const m of dotMaterials) m.uniforms.scale.value = scale;
  };
  resize();
  return {renderer, scene, camera, composer, labels, resize, render() { composer.render(); labels.render(scene, camera); }};
}

// A dim room with three soft panels: enough for metal and glass to read as objects, no visible light sources.
function studio() {
  const room = new THREE.Scene(), box = new THREE.BoxGeometry(1, 1, 1);
  const walls = new THREE.Mesh(box, new THREE.MeshBasicMaterial({color: 0x10161d, side: THREE.BackSide}));
  walls.scale.set(30, 16, 30); room.add(walls);
  for (const [x, y, z, sx, sy, sz, v] of [[0, 7.5, 0, 14, .2, 8, 3.2], [-14, 3, 2, .2, 6, 10, 1.4], [13, 2, -6, .2, 5, 7, .9]]) {
    const panel = new THREE.Mesh(box, new THREE.MeshBasicMaterial({color: new THREE.Color(.9, .95, 1).multiplyScalar(v)}));
    panel.position.set(x, y, z); panel.scale.set(sx, sy, sz); room.add(panel);
  }
  return room;
}

export function label(html, cls, position, parent) {
  const div = document.createElement('div'); div.className = 'tag ' + cls; div.innerHTML = html;
  const obj = new CSS2DObject(div); obj.position.copy(position); parent.add(obj); return obj;
}

function roundedRect(w, h, r) {
  const s = new THREE.Shape(), x = -w / 2, y = -h / 2;
  s.moveTo(x + r, y); s.lineTo(x + w - r, y); s.quadraticCurveTo(x + w, y, x + w, y + r); s.lineTo(x + w, y + h - r); s.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
  s.lineTo(x + r, y + h); s.quadraticCurveTo(x, y + h, x, y + h - r); s.lineTo(x, y + r); s.quadraticCurveTo(x, y, x + r, y);
  return s;
}
const bodyGeo = new THREE.ExtrudeGeometry(roundedRect(1.44, 3.04, .2), {depth: .1, bevelEnabled: true, bevelThickness: .03, bevelSize: .03, bevelSegments: 4, curveSegments: 10}).translate(0, 0, -.05);
const screenGeo = new THREE.PlaneGeometry(1.34, 2.9);
export function phone(texture) {
  const g = new THREE.Group();
  g.add(new THREE.Mesh(bodyGeo, new THREE.MeshPhysicalMaterial({color: 0x1b222b, metalness: .65, roughness: .3, clearcoat: .7, clearcoatRoughness: .25})));
  const screen = new THREE.Mesh(screenGeo, new THREE.MeshBasicMaterial({map: texture, color: new THREE.Color(.82, .82, .82), toneMapped: false}));
  screen.position.z = .081; g.add(screen);
  return g;
}
export function ghostPhone() {
  const pts = roundedRect(1.5, 3.1, .22).getPoints(12);
  const line = new THREE.LineLoop(new THREE.BufferGeometry().setFromPoints(pts), new THREE.LineDashedMaterial({color: 0xf0c36e, dashSize: .1, gapSize: .08, transparent: true, opacity: .9}));
  line.computeLineDistances(); return line;
}

// What a phone's screen shows when there is no screenshot: its state and temperature at the snapshot.
export function statusScreen({name, busy, temp, doing}) {
  const c = document.createElement('canvas'); c.width = 340; c.height = 736; const g = c.getContext('2d');
  const grad = g.createLinearGradient(0, 0, 0, 736); grad.addColorStop(0, busy ? '#0f2a2b' : '#131c26'); grad.addColorStop(1, '#090e13');
  g.fillStyle = grad; g.fillRect(0, 0, 340, 736);
  const font = w => `${w} "PingFang SC","HarmonyOS Sans SC","Noto Sans CJK SC",sans-serif`;
  g.fillStyle = busy ? '#5ccfc0' : '#8aa1b5'; g.font = font('600 30px'); g.fillText(busy ? '● 实验中' : '○ 空闲', 30, 96);
  if (temp) { g.fillStyle = '#eef2f6'; g.font = font('700 128px'); g.fillText(`${Math.round(temp)}°`, 26, 300); }
  g.fillStyle = '#a3b3c2'; g.font = font('500 30px'); g.fillText(name, 30, 360);
  if (doing) {
    g.fillStyle = '#d5dee7'; g.font = font('500 28px');
    let line = '', y = 470;
    for (const ch of doing) { if (g.measureText(line + ch).width > 280) { g.fillText(line, 30, y); line = ''; y += 40; if (y > 640) break; } line += ch; }
    if (y <= 640) g.fillText(line, 30, y);
  }
  const t = new THREE.CanvasTexture(c); t.colorSpace = THREE.SRGBColorSpace; t.anisotropy = 4; return t;
}
export function textureFrom(url) {
  return new Promise(resolve => new THREE.TextureLoader().load(url, t => { t.colorSpace = THREE.SRGBColorSpace; resolve(t); }, undefined, () => resolve(null)));
}

// Many small round dots in one draw call; size in world units.
export function dots(points, {size = .12, opacity = 1} = {}) {
  const pos = [], col = [];
  for (const p of points) { pos.push(p.x, p.y, p.z); const c = new THREE.Color(p.color); col.push(c.r, c.g, c.b); }
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3)); g.setAttribute('color', new THREE.Float32BufferAttribute(col, 3));
  const m = new THREE.ShaderMaterial({transparent: true, depthWrite: false, vertexColors: true, fog: true,
    uniforms: {...THREE.UniformsLib.fog, size: {value: size}, scale: {value: 1}, opacity: {value: opacity}},
    vertexShader: `uniform float size; uniform float scale; varying vec3 vColor;
      #include <fog_pars_vertex>
      void main(){ vColor = color; vec4 mvPosition = modelViewMatrix * vec4(position, 1.0); gl_PointSize = size * scale / -mvPosition.z; gl_Position = projectionMatrix * mvPosition;
      #include <fog_vertex>
      }`,
    fragmentShader: `uniform float opacity; varying vec3 vColor;
      #include <fog_pars_fragment>
      void main(){ float d = length(gl_PointCoord - .5) * 2.; float a = smoothstep(1., .7, d) * opacity; gl_FragColor = vec4(vColor, a);
      #include <tonemapping_fragment>
      #include <colorspace_fragment>
      #include <fog_fragment>
      }`});
  dotMaterials.add(m);
  const pts = new THREE.Points(g, m); pts.userData.dots = m; return pts;
}

// Frame a box from a direction so that it fills the region {x, y, w, h} (pixels) of a W×H view: the view is shifted
// with setViewOffset so the subject sits in the middle of the region, and the distance comes from the box's corners.
export function framing(camera, box, dir, region, W, H, margin = 1.08) {
  const center = box.getCenter(new THREE.Vector3()), d = dir.clone().normalize();
  const f = d.clone().negate(), r = new THREE.Vector3().crossVectors(f, new THREE.Vector3(0, 1, 0)).normalize(), u = new THREE.Vector3().crossVectors(r, f);
  const tanV = Math.tan(THREE.MathUtils.degToRad(camera.fov) / 2), tanY = tanV * region.h / H, tanX = tanV * region.w / H;
  let dist = 0;
  for (let i = 0; i < 8; i++) {
    const c = new THREE.Vector3(i & 1 ? box.max.x : box.min.x, i & 2 ? box.max.y : box.min.y, i & 4 ? box.max.z : box.min.z).sub(center);
    dist = Math.max(dist, Math.abs(c.dot(r)) * margin / tanX + c.dot(d), Math.abs(c.dot(u)) * margin / tanY + c.dot(d));
  }
  return {position: center.clone().addScaledVector(d, dist), target: center, offset: [W / 2 - (region.x + region.w / 2), H / 2 - (region.y + region.h / 2)]};
}

export const fmt = n => new Intl.NumberFormat('zh-CN').format(n);
