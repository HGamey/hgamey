// The trees as DOM: one positioned container per task, built once and updated in place. show(t) puts every tree in its
// state at time t; when `animate` is set (forward play), things that just appeared draw in, otherwise they simply appear.
import {CHIP_W, LABEL_Y, ORB, PILL_H, STUB_H, runLabel, stepMark, stubW} from './command-layout.js';
import {DIR_STATE, TASK_STATE, agentName, brandOf, dirAt, el, esc, hm, jobAt, md, mins, short, taskAt} from './command-util.js';

const NS = 'http://www.w3.org/2000/svg';
export const FAR = .68;    // below this zoom: no speech bubbles, pins shrink to a mark, task titles grow to stay readable
export const TINY = .2;    // below this zoom (past fit-all on a 1280×720 window) small text is a smear: chips keep only their mark and run dots grow
const MARK = {verified: '<svg viewBox="0 0 16 16"><path d="M3.5 8.5l3 3 6-7" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  rejected: '<svg viewBox="0 0 16 16"><path d="M4.5 4.5l7 7M11.5 4.5l-7 7" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>',
  deferred: '<svg viewBox="0 0 16 16"><path d="M5 4v8M11 4v8" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>',
  proposed: '<svg viewBox="0 0 16 16"><circle cx="8" cy="8" r="4.5" fill="none" stroke="currentColor" stroke-width="1.5" stroke-dasharray="2.2 2"/></svg>',
  trunk: '<svg viewBox="0 0 16 16"><path d="M4 5h8M4 8h8M4 11h5" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>'};
const LINK = {main: '#f0c36e', side: '#8aa1b5'};

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
    const {task, nodes, pills, slots} = tr.shape, node = tr.node;
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
    const keep = new Set(), part = (key, kind, make) => {
      keep.add(key);
      let p = tr.parts.get(key);
      if (!p) { p = {kind, node: make()}; p.node.dataset.tree = tr.id; node.append(p.node); tr.parts.set(key, p); }
      return p;
    };
    const into = n => n.via ? curve(n.via.x + n.via.w, n.via.y, n.x, n.y) : n.parent?.dir ? curve(n.parent.x + CHIP_W, n.parent.y, n.x, n.y) : curve(ORB, 0, n.x, n.y);
    const owner = (id, name) => { const s = (task.steps || []).find(x => x.id === id); return s ? `${stepMark(task, id)} ${short(s.title, 12)}${s.owner && s.owner !== (task.steps[0].owner) ? ' · ' + agentName(s.owner) : ''}` : name; };
    for (const n of nodes) {
      if (n.stub) {
        const p = part('s:' + n.stub.id, 'stub', () => el('div', 'stub'));
        p.n = n; p.node.dataset.kind = 'stub'; p.node.dataset.key = n.stub.id;
        p.link ||= path(tr.links, '', LINK.side, 1);
        p.node.style.left = n.x + 'px'; p.node.style.top = n.y + 'px';
        p.link.setAttribute('d', curve(ORB, 0, n.x, n.y)); p.link.setAttribute('stroke-dasharray', '.012 .012');
        continue;
      }
      const p = part('g:' + n.key, 'chip', () => el('div', 'chip', '<span class="mark"></span><div class="t"><span></span><small></small></div>'));
      p.n = n; p.g = n; p.node.dataset.kind = 'chip'; p.node.dataset.key = n.key;
      p.link ||= path(tr.links, '', LINK.side, 1.2);
      p.node.style.left = n.x + 'px'; p.node.style.top = n.y + 'px';
      p.link.setAttribute('d', into(n));
      p.node.querySelector('.t span').textContent = n.dir ? short(n.dir.title, 40) : '未归入方向的实验';
      let tag = p.node.querySelector('.step');
      if (n.tagged && !tag) { tag = el('span', 'step'); p.node.append(tag); }
      if (tag) { tag.textContent = n.tagged ? owner(n.tagged, '') : ''; tag.hidden = !n.tagged; }
      for (const j of n.jobs) {
        const leaf = part('j:' + j.id, 'leaf', () => el('div', 'leaf', '<i></i><span></span><em class="b"></em><em class="m"></em>')), slot = slots.get(j.id);
        leaf.j = j; leaf.n = n; leaf.node.dataset.kind = 'leaf'; leaf.node.dataset.key = j.id;
        leaf.node.style.left = slot.x + 'px'; leaf.node.style.top = slot.y + 'px';
        leaf.node.querySelector('span').textContent = j.kind;
        leaf.node.querySelector('.b').textContent = brandOf(j);
      }
    }
    for (const pill of pills) {
      const p = part('p:' + pill.key, 'pill', () => el('div', 'pill', '<i></i><span></span>'));
      p.p = pill; p.node.dataset.kind = 'leaf'; p.node.dataset.key = pill.job.id;
      p.link ||= path(tr.links, '', LINK.side, 1.2);
      p.node.style.left = pill.x + 'px'; p.node.style.top = pill.y + 'px'; p.node.style.width = pill.w + 'px';
      p.node.querySelector('span').textContent = runLabel(pill.job);
      p.link.setAttribute('d', curve(pill.parent.x + CHIP_W, pill.parent.y, pill.x, pill.y));
    }
    for (const [k, p] of tr.parts) if (!keep.has(k)) { p.node.remove(); p.link?.remove(); tr.parts.delete(k); }
    tr.picked = new Set(pills.map(p => p.job.id));
    tr.s = null; tr.main = null;   // counts, ring and the main line are recomputed on the next show()
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
      const ring = [], runningOn = new Set();
      for (const part of tr.parts.values()) {
        if (part.kind !== 'leaf') continue;
        const s = jobAt(part.j, t);
        if (s === 'running') { busy = true; running++; runningOn.add(part.n.key); }
        if (s && s !== 'running' && s !== 'waiting') { done++; if (s === 'succeeded') ok++; ring.push([part.j.ended, s === 'succeeded']); }
        if (part.s === s) continue;
        const was = part.s; part.s = s;
        part.node.style.display = s && s !== 'waiting' ? '' : 'none';
        part.node.className = 'leaf ' + (s || '') + (tr.picked.has(part.j.id) ? ' picked' : '');
        const m = mins(part.j); part.node.querySelector('.m').textContent = s !== 'running' && m ? `${m}m` : '';
        if (animate && s && s !== 'waiting' && (!was || was === 'waiting')) fresh(part.node, true);
      }
      if (busy) out.busy.add(tr.id);
      const seen = new Set();
      for (const part of tr.parts.values()) {
        if (part.kind !== 'chip') continue;
        const n = part.n, shown = n.jobs.filter(j => { const s = jobAt(j, t); return s && s !== 'waiting'; }).length;
        const here2 = n.dir ? n.dir.created_at <= t : shown > 0, state = n.dir ? dirAt(n.dir, t) : 'trunk', s = here2 ? `${state}|${shown}` : null;
        if (here2) seen.add(n.key);
        if (part.s === s) continue;
        const was = part.s; part.s = s;
        part.node.style.display = here2 ? '' : 'none'; part.link.style.display = here2 ? '' : 'none';
        if (!here2) continue;
        part.node.className = 'chip ' + state + (part.main ? ' main' : '');
        part.node.querySelector('.mark').innerHTML = n.dir ? MARK[state] || MARK.proposed : MARK.trunk;   // the count is in the line below
        part.node.querySelector('small').textContent = n.dir ? (DIR_STATE[state] || state) + (shown ? ` · ${shown} 次实验` : '') : `${shown} 次实验`;
        if (animate && !was) { fresh(part.node, true); part.link.classList.remove('draw'); void part.link.getBBox(); part.link.classList.add('draw'); }
      }
      for (const part of tr.parts.values()) {
        const here2 = part.kind === 'pill' ? part.p.kids.some(k => seen.has(k.key)) : part.kind === 'stub' ? part.n.stub.created_at <= t : null;
        if (here2 === null || part.s === here2) continue;
        part.s = here2; part.node.style.display = here2 ? '' : 'none'; part.link.style.display = here2 ? '' : 'none';
        if (part.kind === 'stub' && here2) part.node.textContent = `${stepMark(task, part.n.stub.id)} ${short(part.n.stub.title, 12)} · ${TASK_STATE[taskAt(part.n.stub, t)] || ''} · 无方向`;
        if (animate && here2) fresh(part.node, true);
      }
      // The main line: from the root to the direction being worked on now, else the newest one still standing.
      const visible = [...seen].map(key => tr.parts.get('g:' + key).n).filter(n => n.dir), newest = list => list.sort((a, b) => b.dir.created_at - a.dir.created_at)[0];
      const target = newest(visible.filter(n => runningOn.has(n.key))) || newest(visible.filter(n => !['rejected', 'deferred'].includes(dirAt(n.dir, t)))) || newest(visible);
      const line = new Set();
      for (let n = target; n?.dir; n = n.parent) line.add(n.key);
      const mainKey = [...line].join('|');
      if (tr.main !== mainKey) {
        tr.main = mainKey;
        for (const part of tr.parts.values()) {
          if (part.kind !== 'chip' && part.kind !== 'pill') continue;
          const on = part.kind === 'chip' ? line.has(part.n.key) : part.p.kids.some(k => line.has(k.key));
          part.main = on; part.node.classList.toggle('main', on);
          part.link.setAttribute('stroke', on ? LINK.main : LINK.side); part.link.setAttribute('stroke-width', on ? 2 : 1.2); part.link.setAttribute('stroke-opacity', on ? '.95' : '.45');
        }
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
    const v = cam.view(), out = [];
    for (const tr of this.trees.values()) {
      if (!tr.here) continue;
      const {x, y, box} = tr.shape;
      if (x + box.x1 < v.x0 || x + box.x0 > v.x1 || y + box.y1 < v.y0 || y + box.y0 > v.y1) continue;
      const add = (x0, y0, x1, y1) => { const [a, b] = cam.toScreen(x + x0, y + y0), [c, d] = cam.toScreen(x + x1, y + y1); out.push({l: a, t: b, r: c, b: d}); };
      add(-ORB - 6, -ORB - 6, ORB + 6, ORB + 6); add(-220, LABEL_Y, 220, LABEL_Y + Math.max(80, 2 * 1.35 * 13 / cam.k));
      for (const part of tr.parts.values()) {
        if (!part.s) continue;
        if (part.kind === 'chip') { const n = part.n; add(n.x, n.y - n.up, n.x + CHIP_W, n.y + n.down); }
        else if (part.kind === 'pill') add(part.p.x, part.p.y - PILL_H / 2, part.p.x + part.p.w, part.p.y + PILL_H / 2);
        else if (part.kind === 'stub') add(part.n.x, part.n.y - STUB_H / 2, part.n.x + stubW(part.node.textContent), part.n.y + STUB_H / 2);
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
  chip: (tr, part) => `<b>${esc(part.n.dir ? part.n.dir.title : '未归入方向的实验')}</b><span class="mono">${part.n.dir ? esc(DIR_STATE[dirAt(part.n.dir, Infinity)] || part.n.dir.state) + ' · ' : ''}${part.n.jobs.length} 次实验</span>`,
  stub: (tr, part) => `<b>${esc(part.n.stub.title)}</b><span class="mono">${TASK_STATE[part.n.stub.state] || part.n.stub.state} · ${esc(agentName(part.n.stub.owner))} · 还没有方向</span>`,
};
