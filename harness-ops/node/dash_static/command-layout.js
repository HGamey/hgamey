// Where everything sits in the world. One tree per task (a chain of tasks is one tree): the root orb at the tree's
// origin; each direction a chip that hangs under the direction it is based on (data.based_on), so the tree grows to the
// right one generation at a time. A direction's runs are dots under its chip; when a direction builds on one run of its
// parent, that run's pill sits on the edge between them. A follow-on task without a direction is a small stub off the
// root. Every box is known here, so the map never overlaps itself and the overlay can keep clear of it. Trees are
// packed into columns in creation order.
import {begun, hm} from './command-util.js';

export const CHIP_X = 240, CHIP_W = 236, CHIP_H = 58, GEN_W = CHIP_W + 172, DOT = 12, DOTS_ROW = 18, DOTS_TOP = 10, TAG_H = 18;
export const PILL_H = 22, STUB_H = 26, ORB = 75, LABEL_Y = 100, LABEL_W = 300, LABEL_H = 96, PIN_SPACE = 92;
const NODE_GAP = 22, GAP_X = 140, GAP_Y = 120, PILL_X = 14, PILL_MAX = 138, STUB_MAX = 260;
const textW = (s, px = 14) => [...String(s || '')].reduce((w, c) => w + (c.charCodeAt(0) > 0x2e80 ? px : px * .58), 0);
export const pillW = label => Math.round(Math.min(PILL_MAX, textW(label, 11) + 30));
export const stubW = label => Math.round(Math.min(STUB_MAX, textW(label, 11) + 22));
// A run is named by its label; runs without one by what they did and when they started.
export const runLabel = j => j.label || `${j.kind || '实验'} ${hm(begun(j))}`;
// The chain's tasks in order (①, ② …) and how a follow-on task is named on its directions and stub.
const CIRCLED = '①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳';
export const stepMark = (task, id) => { const i = (task.steps || [task]).findIndex(s => s.id === id); return CIRCLED[i] || `(${i + 1})`; };

function treeShape(task, dirs, jobs) {
  const order = (a, b) => (begun(a) || a.queued || a.started || 0) - (begun(b) || b.queued || b.started || 0) || (a.id < b.id ? -1 : 1);
  const mine = jobs.filter(j => j.research_id === task.id).sort(order);
  const byKey = new Map(), root = {key: 'root', kids: []};
  const node = (key, x) => { const n = {key, kids: [], ...x}; byKey.set(key, n); return n; };
  const loose = mine.filter(j => !dirs.some(d => d.id === j.direction_id));
  if (loose.length) root.kids.push(node('trunk', {dir: null, jobs: loose}));
  // A direction hangs under the one it is based on when that one is on this tree and older (so there is never a cycle);
  // otherwise it hangs off the root as before.
  for (const d of dirs) {
    const on = d.based_on, parent = on && byKey.get(on.direction_id);
    const n = node(d.id, {dir: d, jobs: mine.filter(j => j.direction_id === d.id), tagged: d.task_id && d.task_id !== task.id ? d.task_id : null});
    n.parent = parent && parent.dir ? parent : root;
    n.pick = n.parent !== root && on.job_id && n.parent.jobs.some(j => j.id === on.job_id) ? on.job_id : null;
    n.parent.kids.push(n);
  }
  for (const s of (task.steps || []).slice(1)) if (!dirs.some(d => d.task_id === s.id)) root.kids.push(node('stub:' + s.id, {stub: s}));
  // Each node's extent above and below its line: a chip with its task tag over it and its dots under it, or a stub.
  const nodes = [...byKey.values()];
  for (const n of nodes) {
    n.depth = n.parent && n.parent !== root ? n.parent.depth + 1 : 1;
    if (n.stub) { n.up = n.down = STUB_H / 2; continue; }
    n.rows = Math.ceil(n.jobs.length / DOTS_ROW);
    n.up = CHIP_H / 2 + (n.tagged ? TAG_H : 0); n.down = CHIP_H / 2 + (n.rows ? DOTS_TOP + n.rows * DOT : 0);
  }
  // Leaves are stacked top to bottom; a parent sits level with the middle of its children.
  const place = (n, top) => {
    if (!n.kids.length) { n.y = top + n.up; return n.y + n.down; }
    let at = top;
    for (const k of n.kids) at = place(k, at) + NODE_GAP;
    n.y = Math.max((n.kids[0].y + n.kids[n.kids.length - 1].y) / 2, top + n.up);
    return Math.max(at - NODE_GAP, n.y + n.down);
  };
  let at = 0;
  for (const k of root.kids) at = place(k, at) + NODE_GAP;
  const mid = root.kids.length ? (root.kids[0].y + root.kids[root.kids.length - 1].y) / 2 : 0;
  let right = ORB + 40, top = Infinity, bottom = -Infinity;
  const slots = new Map(), pills = [];
  for (const n of nodes) {
    n.y -= mid; n.x = CHIP_X + (n.depth - 1) * GEN_W;
    top = Math.min(top, n.y - n.up); bottom = Math.max(bottom, n.y + n.down);
    if (n.stub) { right = Math.max(right, n.x + STUB_MAX); continue; }
    right = Math.max(right, n.x + CHIP_W);
    n.jobs.forEach((j, i) => slots.set(j.id, {x: n.x + 6 + (i % DOTS_ROW) * DOT + DOT / 2, y: n.y + CHIP_H / 2 + DOTS_TOP + Math.floor(i / DOTS_ROW) * DOT + DOT / 2, node: n}));
  }
  // One pill per run that children build on, level with those children and kept apart from its neighbours.
  for (const n of nodes) {
    const picked = new Map();
    for (const k of n.kids) if (k.pick) (picked.get(k.pick) || picked.set(k.pick, []).get(k.pick)).push(k);
    let floor = -Infinity;
    for (const [jid, kids] of picked) {
      const j = n.jobs.find(x => x.id === jid), y = Math.max(floor, kids.reduce((a, k) => a + k.y, 0) / kids.length);
      const pill = {key: n.key + ':' + jid, parent: n, job: j, kids, x: n.x + CHIP_W + PILL_X, y, w: pillW(runLabel(j))};
      for (const k of kids) k.via = pill;
      pills.push(pill); floor = y + PILL_H + 6;
    }
  }
  const nodesOut = nodes.filter(n => n !== root);
  const box = {x0: -LABEL_W / 2 - 10, x1: right + 10, y0: Math.min(top, -ORB - PIN_SPACE) - 10, y1: Math.max(bottom, LABEL_Y + LABEL_H) + 10};
  return {id: task.id, task, nodes: nodesOut, pills, slots, box, w: box.x1 - box.x0, h: box.y1 - box.y0};
}

// Greedy masonry with per-column widths; the column count is chosen once so the whole map suits the stage's aspect.
function pack(shapes, cols) {
  const columns = Array.from({length: cols}, () => ({h: 0, w: 0, items: []}));
  for (const s of shapes) {
    const c = columns.reduce((best, col, i) => col.h < columns[best].h - 1 ? i : best, 0);
    columns[c].items.push(s); columns[c].h += s.h + GAP_Y; columns[c].w = Math.max(columns[c].w, s.w);
  }
  let x = 0;
  for (const col of columns) {
    let y = 0;
    for (const s of col.items) { s.x = x - s.box.x0; s.y = y - s.box.y0; y += s.h + GAP_Y; }
    x += col.w + GAP_X;
  }
  return {w: Math.max(0, x - GAP_X), h: Math.max(0, ...columns.map(c => c.h - GAP_Y))};
}

export function layout(model, aspect, cols = 0) {
  const tasks = [...model.tasks].sort((a, b) => a.created_at - b.created_at || (a.id < b.id ? -1 : 1));
  const dirsOf = new Map(tasks.map(t => [t.id, []]));
  for (const d of [...model.dirs].sort((a, b) => a.created_at - b.created_at || (a.id < b.id ? -1 : 1))) dirsOf.get(d.research_id)?.push(d);
  const shapes = tasks.map(t => treeShape(t, dirsOf.get(t.id), model.jobs));
  if (!cols) {
    let best = Infinity;
    for (let c = 1; c <= Math.max(1, Math.min(7, shapes.length)); c++) {
      const size = pack(shapes, c), score = Math.abs(Math.log((size.w / Math.max(1, size.h)) / aspect));
      if (score < best - .05) { best = score; cols = c; }
    }
  }
  const size = pack(shapes, Math.max(1, Math.min(cols || 1, shapes.length || 1)));
  return {trees: new Map(shapes.map(s => [s.id, s])), cols, size};
}

// World-space boxes, for camera framing and overlay avoidance.
export const treeBounds = t => ({x0: t.x + t.box.x0, x1: t.x + t.box.x1, y0: t.y + t.box.y0, y1: t.y + t.box.y1});
export function union(list) {
  if (!list.length) return null;
  return list.reduce((a, b) => ({x0: Math.min(a.x0, b.x0), x1: Math.max(a.x1, b.x1), y0: Math.min(a.y0, b.y0), y1: Math.max(a.y1, b.y1)}));
}
