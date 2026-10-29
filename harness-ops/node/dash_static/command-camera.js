// The map camera. Screen = world * k + (x, y), in the stage's own pixels; the stage is the only area the map uses, so
// framing never puts anything under the chrome. Wheel zooms toward the cursor, drag pans with inertia, two fingers pinch,
// and flights follow the smooth zoom-and-pan path of van Wijk & Nuij (the one d3.interpolateZoom uses).
import {clamp, ease} from './command-util.js';

const RHO = Math.SQRT2;

function zoomPath(a, b) {   // a, b: [cx, cy, w] (centre and visible width in world units)
  const [ux0, uy0, w0] = a, [ux1, uy1, w1] = b, dx = ux1 - ux0, dy = uy1 - uy0, d2 = dx * dx + dy * dy;
  if (d2 < 1e-6) {
    const S = Math.log(w1 / w0) / RHO;
    return {S: Math.abs(S), at: s => [ux0 + s * dx, uy0 + s * dy, w0 * Math.exp(RHO * s * S)]};
  }
  const d1 = Math.sqrt(d2), b0 = (w1 * w1 - w0 * w0 + RHO ** 4 * d2) / (2 * w0 * RHO ** 2 * d1), b1 = (w1 * w1 - w0 * w0 - RHO ** 4 * d2) / (2 * w1 * RHO ** 2 * d1);
  const r0 = Math.log(Math.sqrt(b0 * b0 + 1) - b0), r1 = Math.log(Math.sqrt(b1 * b1 + 1) - b1), S = (r1 - r0) / RHO;
  return {S, at: s => {
    const u = s * S, c = Math.cosh(r0), q = w0 / (RHO ** 2 * d1) * (c * Math.tanh(RHO * u + r0) - Math.sinh(r0));
    return [ux0 + q * dx, uy0 + q * dy, w0 * c / Math.cosh(RHO * u + r0)];
  }};
}

export class Camera {
  constructor(stage, world, {onChange = () => {}, onUser = () => {}} = {}) {
    Object.assign(this, {stage, world, onChange, onUser, x: 0, y: 0, k: 1, kMin: .06, kMax: 2.4, extent: null, flight: null, glide: null});
    this.w = stage.clientWidth; this.h = stage.clientHeight;
    new ResizeObserver(() => this.resize()).observe(stage);
    this.bind();
  }

  resize() {
    const w = this.stage.clientWidth, h = this.stage.clientHeight;
    if (!w || !h || (w === this.w && h === this.h)) return;
    // Keep the world point at the centre where it was; the owner may re-frame after this.
    const cx = (this.w / 2 - this.x) / this.k, cy = (this.h / 2 - this.y) / this.k;
    this.w = w; this.h = h; this.x = w / 2 - cx * this.k; this.y = h / 2 - cy * this.k;
    this.apply(); this.onResize?.();
  }

  apply() {
    this.constrain();
    this.world.style.transform = `translate(${this.x}px,${this.y}px) scale(${this.k})`;
    this.onChange();
  }

  // Keep some of the map in view: the visible rectangle may not leave the content's extent by more than 40% of itself.
  constrain() {
    const e = this.extent; if (!e) return;
    const mx = this.w * .4 / this.k, my = this.h * .4 / this.k;
    const vx0 = -this.x / this.k, vx1 = (this.w - this.x) / this.k, vy0 = -this.y / this.k, vy1 = (this.h - this.y) / this.k;
    const ex0 = e.x0 - mx, ex1 = e.x1 + mx, ey0 = e.y0 - my, ey1 = e.y1 + my;
    const sx = vx1 - vx0 > ex1 - ex0 ? (ex0 + ex1) / 2 - (vx0 + vx1) / 2 : Math.max(0, ex0 - vx0) + Math.min(0, ex1 - vx1);
    const sy = vy1 - vy0 > ey1 - ey0 ? (ey0 + ey1) / 2 - (vy0 + vy1) / 2 : Math.max(0, ey0 - vy0) + Math.min(0, ey1 - vy1);
    this.x -= sx * this.k; this.y -= sy * this.k;
  }

  toScreen(wx, wy) { return [this.x + wx * this.k, this.y + wy * this.k]; }
  toWorld(sx, sy) { return [(sx - this.x) / this.k, (sy - this.y) / this.k]; }
  view() { return {x0: -this.x / this.k, y0: -this.y / this.k, x1: (this.w - this.x) / this.k, y1: (this.h - this.y) / this.k}; }

  // The view that shows bounds b inside the stage with a margin; maxK keeps a single small tree from filling the screen.
  viewFor(b, {pad = 56, maxK = 1.15} = {}) {
    const k = clamp(Math.min((this.w - 2 * pad) / Math.max(1, b.x1 - b.x0), (this.h - 2 * pad) / Math.max(1, b.y1 - b.y0)), this.kMin, maxK);
    return {k, cx: (b.x0 + b.x1) / 2, cy: (b.y0 + b.y1) / 2};
  }
  set(v) { this.stop(); this.k = v.k; this.x = this.w / 2 - v.cx * v.k; this.y = this.h / 2 - v.cy * v.k; this.apply(); }
  // How much of b is on screen (0..1) and how far the zoom is from the one that frames it (ratio, >= 1).
  seen(b, opts) {
    const v = this.view(), inner = .06 * (v.x1 - v.x0);
    const ix = Math.max(0, Math.min(b.x1, v.x1 - inner) - Math.max(b.x0, v.x0 + inner)), iy = Math.max(0, Math.min(b.y1, v.y1 - inner) - Math.max(b.y0, v.y0 + inner));
    const want = this.viewFor(b, opts).k;
    return {inside: ix * iy / Math.max(1, (b.x1 - b.x0) * (b.y1 - b.y0)), zoom: Math.max(want / this.k, this.k / want)};
  }

  fly(target, {ms, ...opts} = {}) {
    const v = 'k' in target ? target : this.viewFor(target, opts);
    this.stop();
    const from = [(this.w / 2 - this.x) / this.k, (this.h / 2 - this.y) / this.k, this.w / this.k], to = [v.cx, v.cy, this.w / v.k];
    const path = zoomPath(from, to), dur = ms ?? clamp(path.S * 700, 450, 1500);
    if (matchMedia('(prefers-reduced-motion: reduce)').matches) { this.set(v); return Promise.resolve(); }
    return new Promise(done => { this.flight = {path, t0: performance.now(), dur, done}; });
  }
  stop() { if (this.flight) { const d = this.flight.done; this.flight = null; d(); } this.glide = null; }
  get moving() { return !!(this.flight || this.glide || this.drag); }

  step(t) {
    if (this.flight) {
      const f = this.flight, p = Math.min(1, (t - f.t0) / f.dur), [cx, cy, w] = f.path.at(ease(p));
      this.k = this.w / w; this.x = this.w / 2 - cx * this.k; this.y = this.h / 2 - cy * this.k; this.apply();
      if (p >= 1) { this.flight = null; f.done(); }
    } else if (this.glide) {
      const g = this.glide, dt = Math.min(48, t - g.t); g.t = t;
      this.x += g.vx * dt; this.y += g.vy * dt; this.apply();
      const decay = Math.exp(-dt / 220); g.vx *= decay; g.vy *= decay;
      if (Math.hypot(g.vx, g.vy) < .015) this.glide = null;
    }
  }

  zoomAt(sx, sy, factor) {
    const k = clamp(this.k * factor, this.kMin, this.kMax);
    this.x = sx - (sx - this.x) * k / this.k; this.y = sy - (sy - this.y) * k / this.k; this.k = k; this.apply();
  }
  panBy(dx, dy) { this.x += dx; this.y += dy; this.apply(); }
  zoomBy(factor, animate = true) {
    const [cx, cy] = this.toWorld(this.w / 2, this.h / 2), k = clamp(this.k * factor, this.kMin, this.kMax);
    if (animate) return this.fly({k, cx, cy}, {ms: 260});
    this.zoomAt(this.w / 2, this.h / 2, factor);
  }

  bind() {
    const s = this.stage, pointers = new Map(), local = e => { const r = s.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
    let pinch = null, samples = [];
    s.addEventListener('wheel', e => {
      e.preventDefault(); this.stop(); this.onUser('wheel');
      const [sx, sy] = local(e);
      if (!e.ctrlKey && e.deltaMode === 0 && Math.abs(e.deltaX) > Math.abs(e.deltaY) * .5) { this.panBy(-e.deltaX, -e.deltaY); return; }   // trackpad two-finger scroll
      this.zoomAt(sx, sy, Math.pow(2, -e.deltaY * (e.deltaMode === 1 ? .05 : e.deltaMode ? 1 : .002) * (e.ctrlKey ? 10 : 1)));
    }, {passive: false});
    s.addEventListener('pointerdown', e => {
      if (e.button !== 0 || e.target.closest('button, a, .pin, .toast, .minimap, .agent')) return;
      pointers.set(e.pointerId, local(e));
      this.stop();
      if (pointers.size === 2) {
        const [a, b] = [...pointers.values()];
        pinch = {d: Math.hypot(a[0] - b[0], a[1] - b[1]), m: [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2]};
      } else this.drag = {id: e.pointerId, x0: e.clientX, y0: e.clientY, on: false};
      samples = [];
    });
    s.addEventListener('pointermove', e => {
      if (!pointers.has(e.pointerId)) return;
      const p = local(e), prev = pointers.get(e.pointerId); pointers.set(e.pointerId, p);
      if (pinch && pointers.size === 2) {
        const [a, b] = [...pointers.values()], d = Math.hypot(a[0] - b[0], a[1] - b[1]), m = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
        this.panBy(m[0] - pinch.m[0], m[1] - pinch.m[1]); this.zoomAt(m[0], m[1], d / pinch.d); pinch = {d, m}; this.onUser('pinch');
        return;
      }
      const g = this.drag; if (!g || g.id !== e.pointerId) return;
      if (!g.on) {
        if (Math.hypot(e.clientX - g.x0, e.clientY - g.y0) < 5) return;
        g.on = true; s.setPointerCapture(e.pointerId); s.classList.add('dragging'); this.onUser('drag');
      }
      this.panBy(p[0] - prev[0], p[1] - prev[1]);
      const t = performance.now(); samples.push([t, p[0], p[1]]); while (samples.length && t - samples[0][0] > 60) samples.shift();
    });
    const up = e => {
      if (!pointers.delete(e.pointerId)) return;
      if (pinch) { pinch = null; this.drag = null; return; }
      const g = this.drag; this.drag = null; s.classList.remove('dragging');
      if (!g?.on) return;
      this.dragged = performance.now();
      if (samples.length > 1 && !matchMedia('(prefers-reduced-motion: reduce)').matches) {
        const [t0, x0, y0] = samples[0], [t1, x1, y1] = samples.at(-1), dt = Math.max(1, t1 - t0);
        const vx = (x1 - x0) / dt, vy = (y1 - y0) / dt, v = Math.hypot(vx, vy), cap = Math.min(1, 1.4 / Math.max(v, 1e-6));   // at most 1400 px/s
        if (performance.now() - t1 < 60) this.glide = {vx: vx * cap, vy: vy * cap, t: performance.now()};
      }
    };
    s.addEventListener('pointerup', up); s.addEventListener('pointercancel', up);
    // A drag that ends over a tree is not a click on it.
    s.addEventListener('click', e => { if (performance.now() - (this.dragged || 0) < 60) { e.stopPropagation(); e.preventDefault(); } }, true);
  }
}
