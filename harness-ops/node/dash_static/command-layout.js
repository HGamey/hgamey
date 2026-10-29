// Where everything sits in the world. One tree per task: the root orb at the tree's origin, its directions as chips to the
// right, and each direction's runs stacked beside its chip (in columns when there are many). Every box is known here, so
// the map never overlaps itself and the overlay can keep clear of it. Trees are packed into columns in creation order.
import {begun} from './command-util.js';

export const ROW = 30, CHIP_X = 240, CHIP_W = 236, CHIP_H = 58, LEAF_X = CHIP_X + CHIP_W + 46, COL_W = 236, MAX_ROWS = 18;
export const ORB = 75, LABEL_Y = 100, LABEL_W = 300, LABEL_H = 96, PIN_SPACE = 92;
const GROUP_GAP = 20, GAP_X = 140, GAP_Y = 120;
const textW = s => [...String(s || '')].reduce((w, c) => w + (c.charCodeAt(0) > 0x2e80 ? 14 : 8), 0);
export const leafW = j => textW(j.kind) + 104;

function treeShape(task, dirs, jobs) {
  const order = (a, b) => (begun(a) || a.queued || a.started || 0) - (begun(b) || b.queued || b.started || 0) || (a.id < b.id ? -1 : 1);
  const mine = jobs.filter(j => j.research_id === task.id).sort(order);
  const known = new Set(dirs.map(d => d.id));
  const loose = mine.filter(j => !known.has(j.direction_id));
  const groups = [...(loose.length ? [{key: 'trunk', dir: null, jobs: loose}] : []),
    ...dirs.map(d => ({key: d.id, dir: d, jobs: mine.filter(j => j.direction_id === d.id)}))];
  let right = ORB + 40;
  for (const g of groups) {
    const n = g.jobs.length, cols = Math.ceil(n / MAX_ROWS), rows = Math.max(1, Math.ceil(n / Math.max(1, cols)));   // balanced columns
    g.rows = rows; g.cols = cols; g.band = Math.max(CHIP_H + 18, rows * ROW + 16);
    const bow = cols === 1 && n > 2 ? Math.min(80, 10 + rows * 4) : 0, half = Math.max(1, (rows - 1) / 2 * ROW);
    g.slots = new Map();
    g.jobs.forEach((j, i) => {
      const c = Math.floor(i / rows), r = i % rows, dy = (r - (rows - 1) / 2) * ROW;
      const x = LEAF_X + c * COL_W + bow * (1 - (dy / half) ** 2);
      g.slots.set(j.id, {x, dy, w: leafW(j), col: c});
      right = Math.max(right, x + leafW(j));
    });
    right = Math.max(right, CHIP_X + CHIP_W);
  }
  const total = groups.reduce((a, g) => a + g.band, 0) + GROUP_GAP * Math.max(0, groups.length - 1);
  let y = -total / 2;
  for (const g of groups) { g.y = y + g.band / 2; y += g.band + GROUP_GAP; for (const s of g.slots.values()) s.y = g.y + s.dy; }
  const box = {x0: -LABEL_W / 2 - 10, x1: right + 10, y0: Math.min(-total / 2, -ORB - PIN_SPACE) - 10, y1: Math.max(total / 2, LABEL_Y + LABEL_H) + 10};
  return {id: task.id, task, groups, box, w: box.x1 - box.x0, h: box.y1 - box.y0};
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
  for (const d of [...model.dirs].sort((a, b) => a.created_at - b.created_at)) dirsOf.get(d.research_id)?.push(d);
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
