// The time bar under the map: finished runs per bucket (teal succeeded, grey not), gold ticks where a person spoke and
// where a task was delivered, the playhead, and the part of the range that has not happened yet. Drag or click to seek;
// the wheel over the bar narrows or widens the range around the cursor; double-click shows the whole record again.
import {$, clamp, hm, md, stamp} from './command-util.js';

const STEPS = [60, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 604800];
const H = 54, BASE = 50;

export class Timeline {
  constructor({onSeek, onRange}) {
    Object.assign(this, {onSeek, onRange, d0: 0, d1: 1, t0: 0, t1: 1, now: 0, items: null});
    this.track = $('t-track'); this.svg = $('t-chart'); this.head = $('t-head'); this.headLabel = $('t-head-label');
    this.hover = $('t-hover'); this.future = $('t-window'); this.axis = $('t-axis');
    new ResizeObserver(() => this.render()).observe(this.track);
    this.bind();
  }

  setData(items, t0, t1) { this.items = items; this.t0 = t0; this.t1 = t1; this.render(); }
  setRange(d0, d1) {
    const span = Math.max(600, d1 - d0);
    this.d1 = Math.min(this.t1, d1); this.d0 = Math.max(this.t0, this.d1 - span);
    if (this.d1 - this.d0 < 600) this.d0 = this.d1 - 600;
    this.render();
  }
  x(t) { return (t - this.d0) / (this.d1 - this.d0) * this.track.clientWidth; }
  timeAt(px) { return clamp(this.d0 + px / Math.max(1, this.track.clientWidth) * (this.d1 - this.d0), this.d0, this.d1); }

  setNow(now, live) {
    this.now = now;
    const w = this.track.clientWidth, x = clamp(this.x(now), 0, w);
    this.head.style.transform = `translateX(${x}px)`;
    this.head.classList.toggle('right', x > w - 110);
    this.headLabel.textContent = live ? `直播 ${hm(now)}` : stamp(now);
    this.future.style.left = x + 'px'; this.future.style.width = Math.max(0, w - x) + 'px';
  }

  render() {
    const w = this.track.clientWidth; if (!w || !this.items) return;
    const span = this.d1 - this.d0, bucket = STEPS.find(s => span / s <= w / 7) || STEPS.at(-1), first = Math.floor(this.d0 / bucket) * bucket;
    const n = Math.ceil((this.d1 - first) / bucket), ok = new Array(n).fill(0), bad = new Array(n).fill(0);
    for (const j of this.items.ends) {
      if (j.at < this.d0 || j.at > this.d1) continue;
      const i = Math.floor((j.at - first) / bucket); if (i < 0 || i >= n) continue;
      if (j.ok) ok[i]++; else bad[i]++;
    }
    const top = Math.max(1, ...ok.map((v, i) => v + bad[i])), bw = bucket / span * w, Y = v => BASE - v / top * 34;
    let out = `<line x1="0" x2="${w}" y1="${BASE + .5}" y2="${BASE + .5}" stroke="rgba(150,175,200,.22)"/>`;
    for (let i = 0; i < n; i++) {
      if (!ok[i] && !bad[i]) continue;
      const x = this.x(first + i * bucket) + bw * .14, bwid = Math.max(1, bw * .72);
      if (ok[i]) out += `<rect x="${x.toFixed(1)}" y="${Y(ok[i]).toFixed(1)}" width="${bwid.toFixed(1)}" height="${(BASE - Y(ok[i])).toFixed(1)}" rx="1" fill="#5ccfc0" opacity=".85"/>`;
      if (bad[i]) out += `<rect x="${x.toFixed(1)}" y="${Y(ok[i] + bad[i]).toFixed(1)}" width="${bwid.toFixed(1)}" height="${(Y(ok[i]) - Y(ok[i] + bad[i]) - (ok[i] ? 1 : 0)).toFixed(1)}" rx="1" fill="#4c5866"/>`;
    }
    for (const t of this.items.people) if (t >= this.d0 && t <= this.d1) out += `<circle cx="${this.x(t).toFixed(1)}" cy="6" r="2.6" fill="#f0c36e"/>`;
    for (const t of this.items.delivered) if (t >= this.d0 && t <= this.d1) {
      const x = this.x(t).toFixed(1);
      out += `<line x1="${x}" x2="${x}" y1="10" y2="${BASE}" stroke="#f0c36e" stroke-width="1" stroke-dasharray="2 3" opacity=".55"/><path d="M${x},9 l4,-5 h-8 z" fill="#f0c36e"/>`;
    }
    this.svg.setAttribute('viewBox', `0 0 ${w} ${H}`);
    this.svg.innerHTML = out;
    const tick = STEPS.find(s => span / s <= w / 110) || STEPS.at(-1), offset = new Date().getTimezoneOffset() * 60;
    let labels = '';
    for (let t = Math.ceil((this.d0 - offset) / tick) * tick + offset; t <= this.d1; t += tick) {
      const x = this.x(t); if (x < 24 || x > w - 24) continue;
      labels += `<span style="left:${x.toFixed(0)}px">${tick >= 86400 || hm(t) === '00:00' ? md(t) : hm(t)}</span>`;
    }
    this.axis.innerHTML = labels;
    this.setNow(this.now, this.live);
  }

  bind() {
    const tr = this.track, px = e => e.clientX - tr.getBoundingClientRect().left;
    let scrubbing = false;
    tr.addEventListener('pointerdown', e => {
      if (e.button !== 0) return;
      scrubbing = true; tr.setPointerCapture(e.pointerId); this.onSeek(this.timeAt(px(e)), false);
    });
    tr.addEventListener('pointermove', e => {
      const t = this.timeAt(px(e));
      this.hover.hidden = false; this.hover.style.left = clamp(px(e), 0, tr.clientWidth) + 'px'; this.hover.dataset.t = stamp(t);
      if (scrubbing) this.onSeek(t, false);
    });
    const end = e => { if (!scrubbing) return; scrubbing = false; this.onSeek(this.timeAt(px(e)), true); };
    tr.addEventListener('pointerup', end); tr.addEventListener('pointercancel', end);
    tr.addEventListener('pointerleave', () => { if (!scrubbing) this.hover.hidden = true; });
    tr.addEventListener('wheel', e => {
      e.preventDefault();
      const at = this.timeAt(px(e)), f = Math.pow(2, e.deltaY * (e.deltaMode ? .05 : .002)), span = clamp((this.d1 - this.d0) * f, 600, this.t1 - this.t0);
      const r = (at - this.d0) / (this.d1 - this.d0), d0 = clamp(at - r * span, this.t0, this.t1 - span);
      this.setRange(d0, d0 + span); this.onRange(this.d0, this.d1, 'custom');
    }, {passive: false});
    tr.addEventListener('dblclick', () => { this.setRange(this.t0, this.t1); this.onRange(this.d0, this.d1, 'all'); });
  }
}
