// The trees as DOM: one positioned container per task, built once and updated in place. show(t) puts every tree in its
// state at time t; when `animate` is set (forward play), things that just appeared draw in, otherwise they simply appear.
import {CHIP_W, CHIP_X, LABEL_Y, ORB, leafW} from './command-layout.js';
import {DIR_STATE, TASK_STATE, agentName, brandOf, dirAt, el, esc, hm, jobAt, md, mins, short, taskAt} from './command-util.js';

const NS = 'http://www.w3.org/2000/svg';
export const FAR = .68;    // below this zoom: no speech bubbles, pins shrink to a mark, task titles grow to stay readable
export const TINY = .2;    // below this zoom (past fit-all on a 1280×720 window) small text is a smear: runs become dots and chips keep only their mark
const MARK = {verified: '<svg viewBox="0 0 16 16"><path d="M3.5 8.5l3 3 6-7" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  rejected: '<svg viewBox="0 0 16 16"><path d="M4.5 4.5l7 7M11.5 4.5l-7 7" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>',
  deferred: '<svg viewBox="0 0 16 16"><path d="M5 4v8M11 4v8" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>',
  proposed: '<svg viewBox="0 0 16 16"><circle cx="8" cy="8" r="4.5" fill="none" stroke="currentColor" stroke-width="1.5" stroke-dasharray="2.2 2"/></svg>',
  trunk: '<svg viewBox="0 0 16 16"><path d="M4 5h8M4 8h8M4 11h5" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>'};
const LINK = {verified: '#f0c36e', rejected: '#4c5866', deferred: '#66778a', proposed: '#8db4f5', trunk: '#8aa1b5'};
const LEAF_LINK = {succeeded: '#5ccfc0', running: '#5ccfc0', waiting: '#8db4f5'};

function curve(x0, y0, x1, y1) { const mx = x0 + (x1 - x0) * .5; return `M${x0},${y0} C${mx},${y0} ${mx},${y1} ${x1},${y1}`; }
function path(svg, d, color, width) {
  const p = document.createElementNS(NS, 'path');
  p.setAttribute('d', d); p.setAttribute('stroke', color); p.setAttribute('stroke-width', width); p.setAttribute('pathLength', '1');
  p.setAttribute('vector-effect', 'non-scaling-stroke'); svg.append(p); return p;
}
function fresh(node, animate) {
  if (!animate) return;
  node.classList.remove('fresh'); void node.offsetWidth; node.classList.add('fresh');
  node.addEventListener('animationend', () => node.classList.remove('fresh'), {once: true});
}

export class World {
  constructor(root) { this.root = root; this.trees = new Map(); this.t = -Infinity; }

  // (Re)build from a layout. Existing trees keep their nodes and move (CSS transition); new ones are created hidden.
  build(lay) {
    for (const [id, tr] of this.trees) if (!lay.trees.has(id)) { tr.node.remove(); this.trees.delete(id); }
    for (const [id, shape] of lay.trees) {
      let tr = this.trees.get(id);
      if (!tr) { tr = {id, node: el('div', 'tree'), parts: new Map()}; tr.node.dataset.tree = id; tr.node.style.display = 'none'; this.root.append(tr.node); this.trees.set(id, tr); }
      tr.shape = shape;
      tr.node.style.transform = `translate(${shape.x}px,${shape.y}px)`;
      this.draw(tr);
    }
    this.t = -Infinity;
  }

  draw(tr) {
    const {task, groups} = tr.shape, node = tr.node;
    if (!tr.root) {
      tr.links = document.createElementNS(NS, 'svg'); tr.links.setAttribute('class', 'links'); tr.links.setAttribute('width', 1); tr.links.setAttribute('height', 1);
      tr.root = el('div', 'root', '<svg class="ring" viewBox="0 0 230 230"></svg><div class="orb"><div class="count"><b>0</b><small>实验</small></div></div>');
      tr.label = el('div', 'task-label', '<b></b><div class="meta"><span class="tag"></span><span class="mono"></span></div>');
      for (const n of [tr.root, tr.label]) { n.dataset.tree = tr.id; n.dataset.kind = 'root'; }
      tr.root.tabIndex = 0; tr.root.setAttribute('role', 'button');
      tr.root.style.left = '0px'; tr.root.style.top = '0px'; tr.label.style.left = '0px'; tr.label.style.top = LABEL_Y + 'px';
      node.append(tr.links, tr.root, tr.label);
    }
    tr.label.querySelector('b').textContent = short(task.title, 34); tr.root.setAttribute('aria-label', task.title);
    tr.label.querySelector('.mono').textContent = `${agentName(task.owner)} · ${md(task.created_at)} ${hm(task.created_at)}`;
    const keep = new Set();
    for (const g of groups) {
      const key = 'g:' + g.key; keep.add(key);
      let part = tr.parts.get(key);
      if (!part) {
        part = {kind: 'chip', g, node: el('div', 'chip', '<span class="mark"></span><div class="t"><span></span><small></small></div>')};
        part.node.dataset.tree = tr.id; part.node.dataset.kind = 'chip'; part.node.dataset.key = g.key;
        part.link = path(tr.links, '', LINK.trunk, 1.5); node.append(part.node); tr.parts.set(key, part);
      }
      part.g = g;
      part.node.style.left = CHIP_X + 'px'; part.node.style.top = g.y + 'px';
      part.link.setAttribute('d', curve(ORB, 0, CHIP_X, g.y));
      part.node.querySelector('.t span').textContent = g.dir ? short(g.dir.title, 40) : '未归入方向的实验';
      for (const j of g.jobs) {
        const k = 'j:' + j.id, slot = g.slots.get(j.id); keep.add(k);
        let leaf = tr.parts.get(k);
        if (!leaf) {
          leaf = {kind: 'leaf', node: el('div', 'leaf', '<i></i><span></span><em class="b"></em><em class="m"></em>')};
          leaf.node.dataset.tree = tr.id; leaf.node.dataset.kind = 'leaf'; leaf.node.dataset.key = j.id;
          leaf.link = path(tr.links, '', '#66778a', 1); node.append(leaf.node); tr.parts.set(k, leaf);
        }
        leaf.j = j; leaf.g = g;
        leaf.node.style.left = slot.x + 'px'; leaf.node.style.top = slot.y + 'px';
        leaf.node.querySelector('span').textContent = j.kind;
        leaf.node.querySelector('.b').textContent = brandOf(j);
        leaf.link.setAttribute('d', slot.col ? '' : curve(CHIP_X + CHIP_W, g.y, slot.x - 6, slot.y));
      }
    }
    for (const [k, part] of tr.parts) if (!keep.has(k)) { part.node.remove(); part.link.remove(); tr.parts.delete(k); }
    tr.s = null;   // counts and ring are recomputed on the next show()
  }

  // Put every tree in its state at t. Cheap when nothing crossed a boundary: each node is touched only if its state changed.
  show(t, animate) {
    const out = {busy: new Set(), shown: 0};
    for (const tr of this.trees.values()) {
      const {task} = tr.shape, here = task.created_at <= t;
      if (tr.here !== here) { tr.node.style.display = here ? '' : 'none'; tr.here = here; if (here) fresh(tr.root, animate); }
      if (!here) continue;
      out.shown++;
      let done = 0, ok = 0, busy = false, running = 0;
      const ring = [];
      for (const part of tr.parts.values()) {
        if (part.kind !== 'leaf') continue;
        const s = jobAt(part.j, t);
        if (s === 'running') { busy = true; running++; }
        if (s && s !== 'running' && s !== 'waiting') { done++; if (s === 'succeeded') ok++; ring.push([part.j.ended, s === 'succeeded']); }
        if (part.s === s) continue;
        const was = part.s; part.s = s;
        part.node.style.display = s && s !== 'waiting' ? '' : 'none'; part.link.style.display = s && s !== 'waiting' ? '' : 'none';
        part.node.className = 'leaf ' + (s || '');
        part.link.setAttribute('stroke', LEAF_LINK[s] || '#66778a'); part.link.setAttribute('stroke-opacity', s === 'running' ? '.9' : s === 'succeeded' ? '.5' : '.45');
        const m = mins(part.j); part.node.querySelector('.m').textContent = s !== 'running' && m ? `${m}m` : '';
        if (animate && s && s !== 'waiting' && (!was || was === 'waiting')) { fresh(part.node, true); part.link.classList.remove('draw'); void part.link.getBBox(); part.link.classList.add('draw'); }
      }
      if (busy) out.busy.add(tr.id);
      for (const part of tr.parts.values()) {
        if (part.kind !== 'chip') continue;
        const g = part.g, shown = [...g.jobs].filter(j => { const s = jobAt(j, t); return s && s !== 'waiting'; }).length;
        const here2 = g.dir ? g.dir.created_at <= t : shown > 0, state = g.dir ? dirAt(g.dir, t) : 'trunk', s = here2 ? `${state}|${shown}` : null;
        if (part.s === s) continue;
        const was = part.s; part.s = s;
        part.node.style.display = here2 ? '' : 'none'; part.link.style.display = here2 ? '' : 'none';
        if (!here2) continue;
        part.node.className = 'chip ' + state;
        part.node.querySelector('.mark').innerHTML = g.dir ? MARK[state] || MARK.proposed : MARK.trunk;   // the count is in the line below
        part.node.querySelector('small').textContent = g.dir ? (DIR_STATE[state] || state) + (shown ? ` · ${shown} 次实验` : '') : `${shown} 次实验`;
        part.link.setAttribute('stroke', LINK[state] || LINK.trunk); part.link.setAttribute('stroke-opacity', '.7');
        if (animate && !was) { fresh(part.node, true); part.link.classList.remove('draw'); void part.link.getBBox(); part.link.classList.add('draw'); }
      }
      const state = taskAt(task, t), delivered = state === 'delivered', s = `${done}|${ok}|${running}|${state}`;
      if (animate && delivered && tr.delivered === false) { const r = el('span', 'gold-ripple'); tr.root.append(r); r.addEventListener('animationend', () => r.remove()); }
      if (tr.s !== s) {
        tr.s = s;
        tr.root.classList.toggle('busy', busy); tr.root.classList.toggle('delivered', delivered); tr.root.classList.toggle('closed', state === 'closed');
        tr.root.querySelector('.count b').textContent = done + running;
        const tag = tr.label.querySelector('.tag'); tag.className = 'tag ' + state; tag.textContent = TASK_STATE[state];
        tr.root.querySelector('.ring').innerHTML = ringSvg(ring.sort((a, b) => a[0] - b[0]));
        tr.done = done; tr.ok = ok;
      }
      tr.busy = busy; tr.delivered = delivered; tr.state = state;
    }
    this.t = t;
    return out;
  }

  // Screen boxes of what is drawn, for the overlay to keep clear of (only trees on screen are measured).
  boxes(cam) {
    const v = cam.view(), out = [], far = cam.k < TINY;
    for (const tr of this.trees.values()) {
      if (!tr.here) continue;
      const {x, y, box} = tr.shape;
      if (x + box.x1 < v.x0 || x + box.x0 > v.x1 || y + box.y1 < v.y0 || y + box.y0 > v.y1) continue;
      const add = (x0, y0, x1, y1) => { const [a, b] = cam.toScreen(x + x0, y + y0), [c, d] = cam.toScreen(x + x1, y + y1); out.push({l: a, t: b, r: c, b: d}); };
      add(-ORB - 6, -ORB - 6, ORB + 6, ORB + 6); add(-220, LABEL_Y, 220, LABEL_Y + Math.max(80, 2 * 1.35 * 13 / cam.k));
      for (const part of tr.parts.values()) {
        if (!part.s) continue;
        if (part.kind === 'chip') add(CHIP_X, part.g.y - 29, CHIP_X + CHIP_W, part.g.y + 29);
        else if (part.s !== 'waiting') { const sl = part.g.slots.get(part.j.id); add(sl.x - 4, sl.y - 12, sl.x + (far ? 12 : leafW(part.j)), sl.y + 12); }
      }
    }
    return out;
  }

  highlight(id) { for (const tr of this.trees.values()) tr.node.classList.toggle('dim', !!id && tr.id !== id); }
  select(id) { for (const tr of this.trees.values()) tr.node.classList.toggle('selected', tr.id === id); }
  // The run or direction picked on the map stays marked while its row is highlighted in the panel.
  mark(id, key) { for (const tr of this.trees.values()) for (const [k, part] of tr.parts) part.node.classList.toggle('marked', tr.id === id && k === key); }
}

function ringSvg(done) {
  const N = 60; let dots = '';
  for (let k = 0; k < N; k++) { const a = -Math.PI / 2 + k * 2 * Math.PI / N; dots += `<circle cx="${(115 + 100 * Math.cos(a)).toFixed(1)}" cy="${(115 + 100 * Math.sin(a)).toFixed(1)}" r="1.4" fill="rgba(150,175,200,.16)"/>`; }
  done.forEach(([, good], k) => {
    const ring = Math.floor(k / N), a = -Math.PI / 2 + (k % N) * 2 * Math.PI / N, r = 100 + ring * 9, x = (115 + r * Math.cos(a)).toFixed(1), y = (115 + r * Math.sin(a)).toFixed(1);
    dots += good ? `<circle cx="${x}" cy="${y}" r="3.4" fill="#5ccfc0"/>` : `<circle cx="${x}" cy="${y}" r="2.8" fill="none" stroke="#66778a" stroke-width="1.2"/>`;
  });
  return dots;
}

export const tipFor = {
  root: tr => `<b>${esc(tr.shape.task.title)}</b><span class="mono">${TASK_STATE[tr.state] || TASK_STATE.active} · ${esc(agentName(tr.shape.task.owner))} · ${tr.done || 0} 次实验 · 成功 ${tr.ok || 0}</span>`,
  chip: (tr, part) => `<b>${esc(part.g.dir ? part.g.dir.title : '未归入方向的实验')}</b><span class="mono">${part.g.dir ? esc(DIR_STATE[dirAt(part.g.dir, Infinity)] || part.g.dir.state) + ' · ' : ''}${part.g.jobs.length} 次实验</span>`,
};
