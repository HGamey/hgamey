// Screen-space marks over the map: agents beside the task they are on, what they just said, and open questions pinned to
// their task. These may not cover the map's own content: a bubble or pin tries eight sides of its anchor at growing
// distances (a thin line leads back when it moves away) and keeps its place unless another is clearly better; when no
// place is clear it is not shown (the rail's record still has it). Zoomed out, there are no bubbles at all.
import {ORB} from './command-layout.js';
import {FAR} from './command-world.js';
import {agentMeta, agentName, clamp, el, esc} from './command-util.js';

const STAND = [180, 212, 244, 276, 308].map(d => d * Math.PI / 180);   // left of and above the orb, never below: the title is there
const PLACES = ['ne', 'nw', 'e', 'w', 'se', 'sw', 'n', 's'], REACH = [0, 48, 110, 190];
const NS = 'http://www.w3.org/2000/svg';
const area = (a, b) => Math.max(0, Math.min(a.r, b.r) - Math.max(a.l, b.l)) * Math.max(0, Math.min(a.b, b.b) - Math.max(a.t, b.t));

function boxAt(place, ax, ay, w, h, gap) {
  const x = {e: ax + gap, ne: ax + gap * .6, se: ax + gap * .6, w: ax - gap - w, nw: ax - gap * .6 - w, sw: ax - gap * .6 - w, n: ax - w / 2, s: ax - w / 2}[place];
  const y = {n: ay - gap - h, ne: ay - gap * .6 - h, nw: ay - gap * .6 - h, s: ay + gap, se: ay + gap * .6, sw: ay + gap * .6, e: ay - h / 2, w: ay - h / 2}[place];
  return {l: x, t: y, r: x + w, b: y + h};
}

export class Overlay {
  constructor(layer, cam, world) {
    Object.assign(this, {layer, cam, world, agents: new Map(), bubbles: [], pins: new Map()});
    this.leads = document.createElementNS(NS, 'svg'); this.leads.setAttribute('class', 'leads'); layer.append(this.leads);
  }

  setAgents(names) {
    for (const [name, a] of this.agents) if (!names.includes(name)) { a.node.remove(); this.agents.delete(name); }
    names.forEach((name, i) => {
      if (this.agents.has(name)) { this.agents.get(name).i = i; return; }
      const m = agentMeta(name), node = el('div', 'agent', `<div class="av">${esc(m.s)}</div><div class="nm">${esc(m.n)}</div>`);
      node.style.setProperty('--c', m.c); node.dataset.agent = name; node.hidden = true; this.layer.append(node);
      this.agents.set(name, {name, i, node, tree: null, pos: null, from: null, t0: 0});
    });
  }

  // Where each agent is at the current time: the tree it is working on, or nowhere (idle).
  place(where) {
    for (const a of this.agents.values()) {
      const tree = where.get(a.name) || null;
      if (tree === a.tree) continue;
      a.from = a.pos && tree && a.tree ? [...a.pos] : null; a.t0 = performance.now(); a.tree = tree;
    }
  }

  // Screen position of an agent standing at its tree, or null when the tree is not drawn.
  stand(a) {
    const tr = a.tree && this.world.trees.get(a.tree);
    if (!tr?.here) return null;
    const {x, y} = tr.shape, [sx, sy] = this.cam.toScreen(x, y), together = [...this.agents.values()].filter(b => b.tree === a.tree);
    const R = Math.max(34, 112 * this.cam.k + 18), th = STAND[together.indexOf(a) % STAND.length];   // outside the ring of finished runs
    return [sx + R * Math.cos(th), sy + R * Math.sin(th)];
  }

  // Per frame: agents glide to their stand; agents whose tree is off screen wait at the nearest edge.
  step(t) {
    const W = this.cam.w, H = this.cam.h;
    let moving = false;
    for (const a of this.agents.values()) {
      const target = this.stand(a), out = !target || target[0] < -20 || target[0] > W + 20 || target[1] < -20 || target[1] > H + 20;
      a.node.hidden = out; if (out) { a.pos = null; a.from = null; continue; }   // the rail says what an off-screen agent is doing
      const [tx, ty] = target;
      let [x, y] = [tx, ty];
      if (a.from) {
        const p = Math.min(1, (t - a.t0) / 700), e = 1 - Math.pow(1 - p, 3);
        x = a.from[0] + (tx - a.from[0]) * e; y = a.from[1] + (ty - a.from[1]) * e - Math.sin(Math.PI * p) * 30;
        a.node.classList.toggle('moving', p < 1); if (p >= 1) a.from = null;
      }
      a.pos = [x, y];
      moving ||= !!a.from;
      a.node.style.transform = `translate(${x}px,${y}px) scale(${clamp(.62 + this.cam.k * .5, .7, 1)})`;
    }
    this.moving = moving;
  }

  // A note said at the current time: a bubble by its agent (or by the task, for a person). At most two at once.
  say(note, tree) {
    const human = note.who === 'human';
    if (!human && !this.agents.has(note.who)) return;
    const b = el('div', 'bubble' + (human ? ' human' : ''), `<b>${human ? '人' : esc(agentName(note.who))}</b>${esc(note.text.slice(0, 70))}${note.text.length > 70 ? '…' : ''}`);
    b.style.setProperty('--c', agentMeta(note.who).c); this.layer.append(b);
    for (const x of this.bubbles.filter(x => x.who === note.who && (x.who !== 'human' || x.tree === tree))) { x.b.remove(); this.bubbles.splice(this.bubbles.indexOf(x), 1); }
    this.bubbles.push({b, who: note.who, tree, born: performance.now(), place: null}); this.dirty = true;
    while (this.bubbles.length > 2) this.bubbles.shift().b.remove();
  }
  hush() { for (const x of this.bubbles) x.b.remove(); this.bubbles = []; }

  // Open questions at the current time, pinned to their task.
  setPins(open, onClick) {
    for (const [id, p] of this.pins) if (!open.some(n => n.id === id)) { p.node.remove(); this.pins.delete(id); }
    for (const n of open) {
      if (this.pins.has(n.id)) continue;
      const node = el('button', 'pin', `<b>${esc(agentName(n.who))} 在等你回复</b><div class="txt">${esc(n.text)}</div>`);
      node.type = 'button'; node.title = n.text; node.addEventListener('click', () => onClick(n));
      this.layer.append(node); this.pins.set(n.id, {node, note: n, place: null});
    }
    this.dirty = true;
  }

  // Lay out pins, then bubbles, avoiding the map's drawn boxes, the agents, each other, and the given screen rects.
  layout(avoid) {
    const W = this.cam.w, H = this.cam.h, taken = [...this.world.boxes(this.cam), ...avoid], far = this.cam.k < FAR;
    for (const a of this.agents.values()) if (a.pos) taken.push({l: a.pos[0] - 16, t: a.pos[1] - 16, r: a.pos[0] + 16, b: a.pos[1] + 16});
    const leads = [];
    const pick = (x, ax, ay, w, h, gap, places) => {
      let best = null;
      for (const [step, reach] of REACH.entries()) for (const place of places) {
        const box = boxAt(place, ax, ay, w, h, gap + reach);
        if (box.l < 6 || box.t < 6 || box.r > W - 6 || box.b > H - 6) continue;
        let cover = 0; for (const o of taken) cover += area(box, o);
        if (cover > w * h * .01) continue;   // covering the map is never the answer
        const score = cover + step * 900 + (place === x.place && step === x.step ? -1200 : 0);
        if (!best || score < best.score) best = {place, step, box, score};
      }
      return best;
    };
    const lead = (box, ax, ay, r) => {
      const x = clamp(ax, box.l, box.r), y = clamp(ay, box.t, box.b), d = Math.hypot(x - ax, y - ay);
      if (d > r + 14) leads.push(`M${(ax + (x - ax) * r / d).toFixed(1)},${(ay + (y - ay) * r / d).toFixed(1)} L${x.toFixed(1)},${y.toFixed(1)}`);
    };
    for (const p of this.pins.values()) {
      const tr = this.world.trees.get(p.note.research_id);
      p.node.hidden = !tr?.here; if (!tr?.here) continue;
      p.node.classList.toggle('mini', far);
      const [sx, sy] = this.cam.toScreen(tr.shape.x, tr.shape.y), w = far ? 26 : 280, h = far ? 26 : p.node.offsetHeight || 64, r = ORB * this.cam.k;
      const best = pick(p, sx, sy, w, h, r + 10, ['n', 'nw', 'ne', 'w', 'sw', 'e']);
      if (!best) { p.node.hidden = true; continue; }
      p.place = best.place; p.step = best.step; taken.push(best.box); lead(best.box, sx, sy, r + 4);
      p.node.style.transform = `translate(${best.box.l}px,${best.box.t}px)`;
    }
    const now = performance.now();
    for (const x of [...this.bubbles]) {
      if (now - x.born > 6500) x.b.classList.add('fade');
      if (now - x.born > 7400) { x.b.remove(); this.bubbles.splice(this.bubbles.indexOf(x), 1); continue; }
      let anchor = null;
      if (far) anchor = null;
      else if (x.who === 'human') { const tr = this.world.trees.get(x.tree); if (tr?.here) anchor = [...this.cam.toScreen(tr.shape.x, tr.shape.y), ORB * this.cam.k + 8]; }
      else { const a = this.agents.get(x.who); if (a?.pos) anchor = [...a.pos, 18]; }
      x.b.hidden = !anchor; if (!anchor) continue;
      x.w ??= x.b.offsetWidth; x.h ??= x.b.offsetHeight;
      const best = pick(x, anchor[0], anchor[1], x.w, x.h, anchor[2], PLACES);
      if (!best) { x.b.hidden = true; continue; }   // nowhere to say it without covering the map
      x.place = best.place; x.step = best.step; taken.push(best.box); lead(best.box, anchor[0], anchor[1], anchor[2] - 4);
      x.b.style.transform = `translate(${best.box.l}px,${best.box.t}px)`;
    }
    const d = leads.join(' ');
    if (this.leads.dataset.d !== d) { this.leads.dataset.d = d; this.leads.innerHTML = d ? `<path d="${d}"/>` : ''; }
  }
}
