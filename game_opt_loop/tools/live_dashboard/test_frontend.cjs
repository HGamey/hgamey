'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const elements = {};
const element = id => elements[id] || (elements[id] = { innerHTML: '', textContent: '', querySelectorAll: () => [], classList: { toggle() {}, add() {} } });
const context = { document: { getElementById: element, addEventListener() {}, querySelectorAll: () => [] },
  fetch: () => new Promise(() => {}), setInterval: () => 0, console, window: {} };
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname, '../../src/viz/assets/app.js'), 'utf8'), context);
context.LIVE = { measurement_date: '2026-10-12', device: 'owned fixture', game: '<script>bad</script>', generated_at: '2026-10-12T00:00:00Z',
  main_workflow: 'existing eval', baseline_status: 'ABORT', records: [{ id: 'negative', title: 'failure', kind: 'existing_eval', status: 'ABORT',
    metrics: [{ label: 'FPS proxy', value: null }], sources: [], limitations: ['not display'], restoration: 'NOT_RUN', optimized_effect_proven: false, promotion_allowed: false }] };
context.renderToday();
assert.match(element('page-today').innerHTML, /&lt;script&gt;bad&lt;\/script&gt;/);
assert.doesNotMatch(element('page-today').innerHTML, /<script>bad/);
assert.match(element('page-today').innerHTML, /未知\/未测/);
assert.match(element('page-today').innerHTML, /尚无验证优化收益/);
assert.match(element('page-today').innerHTML, /ABORT/);
context.LIVE_ERROR = 'HTTP 500'; context.renderToday();
assert.match(element('page-today').innerHTML, /刷新失败，正在显示旧快照/);
assert.equal(context.REFRESH_MS, 30000);
context.DASH = { version: 'static-live-export' };
context.PAGE = 'device'; context.render();
assert.match(element('page-device').innerHTML, /手机页不连接设备/);
console.log('frontend: render escaping, null, ABORT, stale snapshot, refresh and offline device guards PASS');
async function legacyRouting() {
  context.renderOverview = () => {};
  for (const version of ['legacy-panel', 'static-live-export']) {
    context.PAGE = 'today'; context.LIVE = null; context.LIVE_ERROR = null;
    context.fetch = url => Promise.resolve(url === '/api/dashboard'
      ? { ok: true, json: () => Promise.resolve({ version, paths: { game_opt_loop: '', phonefarm: '', lock_root: '' }, notices: [] }) }
      : { ok: false, status: 404 });
    context.load();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(context.PAGE, version === 'legacy-panel' ? 'overview' : 'today');
  }
  console.log('frontend: legacy Rust panel retains overview; static export retains Today PASS');
}
legacyRouting().catch(e => { console.error(e); process.exitCode = 1; });
