// Shared helpers for the command view: formatting, names and colours, and what a job, direction or task was doing at a time.
export const $ = id => document.getElementById(id);
export const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'})[c]);
export const el = (tag, cls, html) => { const n = document.createElement(tag); if (cls) n.className = cls; if (html != null) n.innerHTML = html; return n; };
export const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
export const ease = t => t < .5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;

const fmt = (s, o) => new Intl.DateTimeFormat('zh-CN', {...o, hour12: false}).format(new Date(s * 1000));
export const hm = s => fmt(s, {hour: '2-digit', minute: '2-digit'});
export const md = s => fmt(s, {month: 'numeric', day: 'numeric'});
export const day = s => fmt(s, {month: 'long', day: 'numeric', weekday: 'short'});
export const stamp = s => `${md(s)} ${hm(s)}`;
export function ago(sec) {
  if (sec < 60) return `${Math.max(0, Math.round(sec))} 秒`;
  if (sec < 3600) return `${Math.round(sec / 60)} 分钟`;
  if (sec < 172800) return `${(sec / 3600).toFixed(sec < 36000 ? 1 : 0)} 小时`;
  return `${Math.round(sec / 86400)} 天`;
}

export const BRAND = {HONOR: '荣耀', REDMAGIC: '红魔', NUBIA: '红魔', HUAWEI: '华为', XIAOMI: '小米', OPPO: 'OPPO', VIVO: 'vivo', SAMSUNG: '三星'};
const SAME = {NUBIA: 'REDMAGIC'};   // a phone's reported brand vs the device id jobs bind to
export const phoneKey = b => { const k = String(b || '').toUpperCase(); return SAME[k] || k; };
const KNOWN = {'kimi-researcher': ['#7cc4ff', 'K', 'kimi'], 'glm-researcher': ['#b49cff', 'G', 'glm'], 'claude-lead': ['#ff9f7a', 'C', 'claude'], 'glm-ops': ['#e39ad6', 'O', 'glm-ops'],
  'codex-researcher': ['#9be38a', 'X', 'codex']};
const SPARE = ['#7fd6e8', '#f2a6a0', '#c8d67a', '#e0b3ff', '#ffc98a'];
const spare = new Map();
export function agentMeta(name) {
  if (KNOWN[name]) { const [c, s, n] = KNOWN[name]; return {c, s, n}; }
  if (!spare.has(name)) spare.set(name, SPARE[spare.size % SPARE.length]);
  const n = String(name || '?').replace(/-(researcher|agent)$/, '');
  return {c: spare.get(name), s: n.slice(0, 1).toUpperCase(), n};
}
export const agentName = a => a === 'human' ? '人' : a ? agentMeta(a).n : '平台';

export const OUTCOME = {succeeded: '成功', failed: '失败', cancelled: '取消', interrupted: '中断'};
export const DIR_STATE = {verified: '验证有效', rejected: '试过放弃', proposed: '待做', deferred: '暂缓', running: '进行中', active: '进行中'};
export const WAIT_KIND = {resource: '等手机', device_lease: '等手机', capacity: '等空位', preparing: '准备中', recovery: '等恢复', durability: '等存储'};

// A title's first clause, unless that clause is too short to tell tasks apart ("照 X 的交付，在 Y 上…" keeps the second).
export function short(t, max = 30) {
  let s = (t || '').replace(/^\[[^\]]*\]\s*/, '');
  const stops = [...s.matchAll(/[，,；;（(]/g)].map(m => m.index), cut = stops.find(i => i >= 16);
  if (cut) s = s.slice(0, cut);
  const colon = s.search(/[：:]/); if (colon >= 6) s = s.slice(0, colon);
  return s.length > max ? s.slice(0, max - 1) + '…' : s;
}

// A job's `started` is when it was submitted; it runs from `claimed`. Phone jobs queued longer than this count as waiting.
export const WAIT_MIN = 30;
export const begun = j => j.claimed || (j.queued ? j.ended : j.started);   // a job cancelled in the queue never ran
export const brandOf = j => j.device ? BRAND[phoneKey(j.device)] || j.device : j.phone ? '真机' : '宿主';
export const mins = j => j.ended && begun(j) ? Math.max(1, Math.round((j.ended - begun(j)) / 60)) : null;
export const waitEnd = j => j.claimed || j.ended || Infinity;
export function jobAt(j, t) {
  const w1 = waitEnd(j);
  if (j.phone && j.queued && j.queued <= t && t < w1 && (w1 === Infinity || w1 - j.queued > WAIT_MIN)) return 'waiting';
  const b = begun(j);
  if (!b || b > t) return null;
  return j.ended && j.ended <= t ? j.outcome || 'failed' : 'running';
}
export const dirAt = (d, t) => d.updated_at <= t ? d.state : 'proposed';
// A task at time t: delivered or closed from its last write on, active before. A chain is active while any of its steps
// is, delivered once one of them delivered, and closed only when every step was closed.
const stepAt = (s, t) => (s.state === 'delivered' || s.state === 'closed') && s.updated_at <= t ? s.state : 'active';
export const taskAt = (task, t) => {
  const states = (task.steps || [task]).filter(s => s.created_at <= t).map(s => stepAt(s, t));
  return states.includes('active') || !states.length ? 'active' : states.includes('delivered') ? 'delivered' : 'closed';
};
export const TASK_STATE = {active: '进行中', delivered: '已交付', closed: '已关闭'};
