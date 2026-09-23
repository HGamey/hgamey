'use strict';
/* 面板前端。零依赖、零外网资源: 图是手写 SVG, 没有任何图表库。
   一条原则: 后端给 null 就显示「未知/未测」, 前端绝不替数据编一个值。 */

var DASH = null;
var PAGE = 'overview';
var SELECTED_ROUND = null;
var REFRESH_MS = 30000;
var timer = null;

/* ── 小工具 ───────────────────────────────────────── */
function esc(s) {
  return String(s === null || s === undefined ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
function el(id) { return document.getElementById(id); }
/* 缺值统一走这里 —— 面板里每一个「未知/未测」都是它印出来的。 */
function val(v, unit) {
  if (v === null || v === undefined || v === '') return '<span class="unknown">未知/未测</span>';
  return esc(v) + (unit ? '<span class="u">' + esc(unit) + '</span>' : '');
}
function num(v, digits, unit) {
  if (v === null || v === undefined) return '<span class="unknown">未测</span>';
  return esc(Number(v).toFixed(digits === undefined ? 3 : digits)) + (unit ? ' ' + esc(unit) : '');
}
function kb(n) {
  if (n === null || n === undefined) return '';
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(0) + ' KB';
  return (n / 1048576).toFixed(1) + ' MB';
}
function shortTime(s) {
  if (!s) return '';
  return String(s).replace('T', ' ').replace(/(\+|-)\d\d:\d\d$/, '').replace(/Z$/, '');
}

/* ── 取数 ─────────────────────────────────────────── */
function load() {
  el('refresh-state').textContent = '读取中…';
  fetch('/api/dashboard', { cache: 'no-store' })
    .then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    })
    .then(function (d) {
      DASH = d;
      el('refresh-state').textContent = '数据时间 ' + shortTime(d.generated_at);
      el('footer-paths').innerHTML =
        '层① ' + esc(d.paths.game_opt_loop) + ' · 层②③ ' + esc(d.paths.phonefarm) +
        (d.paths.phonefarm_present ? '' : ' (没找到)') +
        ' · 设备锁 ' + esc(d.paths.lock_root) + ' · v' + esc(d.version);
      render();
    })
    .catch(function (e) {
      el('refresh-state').innerHTML = '<span class="err">读取失败: ' + esc(e.message) + '</span>';
    });
}

/* ── 路由 ─────────────────────────────────────────── */
function showPage(p) {
  PAGE = p;
  var buttons = document.querySelectorAll('#nav button');
  for (var i = 0; i < buttons.length; i++) {
    buttons[i].className = buttons[i].dataset.page === p ? 'on' : '';
  }
  ['overview', 'rounds', 'charts', 'device'].forEach(function (name) {
    el('page-' + name).classList.toggle('hidden', name !== p);
  });
  render();
}

function render() {
  if (!DASH) return;
  renderNotices();
  if (PAGE === 'overview') renderOverview();
  if (PAGE === 'rounds') renderRounds();
  if (PAGE === 'charts') renderCharts();
  if (PAGE === 'device') renderDevice();
}

/* ── 提示条 ───────────────────────────────────────── */
function renderNotices() {
  var html = DASH.notices.map(function (n) {
    var file = n.evidence
      ? ' <span class="file" data-path="' + esc(n.evidence.path) + '">' + esc(n.evidence.label) + '</span>'
      : '';
    return '<div class="notice ' + esc(n.level) + '"><h4>' + esc(n.title) + '</h4><p>' +
      esc(n.body) + file + '</p></div>';
  }).join('');
  el('notices').innerHTML = html;
}

/* ── 总览 ─────────────────────────────────────────── */
function renderOverview() {
  var cards = DASH.layers.map(function (L) {
    var extra = [];
    if (L.evidence_only) extra.push(L.evidence_only + ' 份附加证据');
    if (L.problems && L.problems.length) extra.push('读取问题 ' + L.problems.length + ' 条');
    return '' +
      '<div class="card layer-card" data-layer="' + esc(L.id) + '">' +
        '<div class="layer-head"><span class="layer-idx">' + L.index + '</span>' +
        '<span class="layer-name">' + esc(L.name) + '</span></div>' +
        '<div class="layer-sub">' + esc(L.subtitle) + '</div>' +
        '<div class="counts">' +
          '<div class="count plain"><div class="n">' + L.rounds_total + '</div><div class="t">轮</div></div>' +
          '<div class="count good"><div class="n">' + L.qualified + '</div><div class="t">合格</div></div>' +
          '<div class="count bad"><div class="n">' + L.eliminated + '</div><div class="t">淘汰</div></div>' +
          '<div class="count warn"><div class="n">' + L.invalid + '</div><div class="t">无效</div></div>' +
        '</div>' +
        (extra.length ? '<div class="layer-extra">' + esc(extra.join(' · ')) + '</div>' : '') +
      '</div>';
  }).join('');

  var problems = [];
  DASH.layers.forEach(function (L) {
    (L.problems || []).forEach(function (p) { problems.push(L.index + ' ' + L.name + ': ' + p); });
  });

  el('page-overview').innerHTML =
    '<h2 class="section">三层级 · 每层多少轮, 结果怎么分</h2>' +
    '<div class="grid3">' + cards + '</div>' +
    '<div class="card" style="margin-top:16px"><div class="dim" style="font-size:13px">' +
      '合格 = 判据齐全且全部通过 &nbsp;·&nbsp; 淘汰 = 判据齐全但有未通过 &nbsp;·&nbsp; ' +
      '无效 = 判据不齐 / 没测上, <b>下不出结论</b> (不是判负)' +
    '</div></div>' +
    '<h2 class="section">载体可复现性 · 同一载体连跑 N 轮, 帧时散得有多开</h2>' +
    carrierTable() +
    (problems.length
      ? '<h2 class="section">读取问题</h2><div class="card"><ul class="notes">' +
        problems.map(function (p) { return '<li>' + esc(p) + '</li>'; }).join('') + '</ul></div>'
      : '');
}

function carrierTable() {
  if (!DASH.carriers.length) {
    return '<div class="card"><span class="unknown">没读到任何载体数据 (phonefarm 目录不在?)</span></div>';
  }
  var rows = DASH.carriers.map(function (c) {
    var statusText = c.status === 'pass' ? '合格' : (c.status === 'fail' ? '偏大' : '未定阈值');
    return '<tr>' +
      '<td><b>' + esc(c.carrier) + '</b></td>' +
      '<td>' + esc(c.window) + '</td>' +
      '<td class="num">' + c.n_runs + '</td>' +
      '<td class="num"><b style="font-size:19px">' + num(c.p95_dispersion_pct, 2) + '</b> %</td>' +
      '<td class="num">' + num(c.p50_dispersion_pct, 2) + ' %</td>' +
      '<td class="num">' + num(c.p95_median_ms, 2) + ' ms</td>' +
      '<td class="num">' + num(c.fps_median, 1) + '</td>' +
      '<td class="num">' + (c.gate_pct === null ? '<span class="unknown">无</span>'
        : c.gate_pct + ' %<br><span class="dim" style="font-size:11px">' + esc(c.gate_metric || '') + '</span>') + '</td>' +
      '<td><span class="dot ' + esc(c.status) + '"></span>' + esc(statusText) + '</td>' +
      '<td class="dim">' + esc(c.note || '') +
        ((c.problems && c.problems.length)
          ? '<br><span class="err">' + esc(c.problems.join('; ')) + '</span>' : '') +
      '</td></tr>';
  }).join('');
  return '<div class="card"><table><thead><tr>' +
    '<th>载体</th><th>窗口</th><th>轮数</th><th>p95 离散度</th><th>p50 离散度</th>' +
    '<th>p95 中位</th><th>帧率中位</th><th>阈值</th><th>判定</th><th></th>' +
    '</tr></thead><tbody>' + rows + '</tbody></table></div>';
}

/* ── 轮次 ─────────────────────────────────────────── */
function allRounds() {
  var out = [];
  DASH.layers.forEach(function (L) {
    L.rounds.forEach(function (r) { out.push({ layer: L, round: r }); });
  });
  return out;
}

function findRound(id) {
  var all = allRounds();
  for (var i = 0; i < all.length; i++) if (all[i].round.id === id) return all[i];
  return all.length ? all[0] : null;
}

function renderRounds() {
  var all = allRounds();
  if (!all.length) {
    el('page-rounds').innerHTML = '<div class="card"><span class="unknown">还没有任何一轮数据</span></div>';
    return;
  }
  if (!SELECTED_ROUND || !findRoundExact(SELECTED_ROUND)) SELECTED_ROUND = all[0].round.id;

  var listHtml = '';
  var lastLayer = null;
  all.forEach(function (x) {
    if (x.layer.id !== lastLayer) {
      lastLayer = x.layer.id;
      listHtml += '<div class="round-group">' + x.layer.index + ' ' + esc(x.layer.name) + '</div>';
    }
    listHtml += '<div class="round-item' + (x.round.id === SELECTED_ROUND ? ' on' : '') +
      '" data-round="' + esc(x.round.id) + '">' +
      '<div class="t">' + esc(x.round.title) + '</div>' +
      '<div class="s"><span class="badge ' + esc(x.round.verdict) + '">' + verdictLabel(x.round.verdict) +
      '</span> ' + esc(shortTime(x.round.when)) + '</div></div>';
  });

  el('page-rounds').innerHTML =
    '<div class="round-layout">' +
      '<div class="card round-list">' + listHtml + '</div>' +
      '<div class="card">' + roundDetail(findRound(SELECTED_ROUND)) + '</div>' +
    '</div>';
}

function findRoundExact(id) {
  var all = allRounds();
  for (var i = 0; i < all.length; i++) if (all[i].round.id === id) return all[i];
  return null;
}

function verdictLabel(v) {
  return { qualified: '合格', eliminated: '淘汰', invalid: '无效', evidence: '仅证据' }[v] || v;
}

function roundDetail(x) {
  if (!x) return '<span class="unknown">没有选中任何一轮</span>';
  var r = x.round;
  var html = '' +
    '<div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap">' +
      '<div style="font-size:24px;font-weight:700">' + esc(r.title) + '</div>' +
      '<span class="badge big ' + esc(r.verdict) + '">' + verdictLabel(r.verdict) + '</span>' +
      '<span class="pill">' + esc(r.metric_source) + '</span>' +
      '<span class="dim">' + esc(shortTime(r.when)) + '</span>' +
    '</div>' +
    '<div style="margin-top:6px;font-size:16px">' + esc(r.verdict_text) + '</div>' +
    statsHtml(r.headline);

  if (r.criteria && r.criteria.length) {
    html += '<h2 class="section">判据</h2><table><tbody>' + r.criteria.map(function (c) {
      var mark = c.pass === true ? '<span class="dot pass"></span>通过'
        : (c.pass === false ? '<span class="dot fail"></span>未通过'
        : '<span class="dot unknown"></span><span class="unknown">未评</span>');
      return '<tr><td style="width:150px"><b>' + esc(c.name) + '</b></td><td style="width:110px">' + mark +
        '</td><td class="dim">' + esc(c.detail || '') + '</td></tr>';
    }).join('') + '</tbody></table>';
  }

  if (r.candidates && r.candidates.length) {
    html += '<h2 class="section">候选算子</h2><table><thead><tr>' +
      '<th>候选</th><th>来源</th><th>判定</th><th>耗时 ms</th><th>帧时 p95 ms</th>' +
      '<th>功耗 W</th><th>画质 dB</th><th>p 值</th><th>说明</th></tr></thead><tbody>' +
      r.candidates.map(function (c) {
        return '<tr>' +
          '<td><b>' + esc(c.id) + '</b>' + (c.on_front ? ' <span class="badge qualified">前沿</span>' : '') + '</td>' +
          '<td>' + esc(c.origin) + '</td>' +
          '<td><span class="badge ' + esc(c.verdict) + '">' + verdictLabel(c.verdict) + '</span></td>' +
          '<td class="num">' + num(c.latency_ms, 4) + '</td>' +
          '<td class="num">' + num(c.frame_p95_ms, 3) + '</td>' +
          '<td class="num">' + num(c.power_watt, 3) + '</td>' +
          '<td class="num">' + num(c.psnr_db, 2) + '</td>' +
          '<td class="num">' + (c.p_value === null || c.p_value === undefined
            ? '<span class="unknown">未测</span>' : esc(Number(c.p_value).toExponential(1))) + '</td>' +
          '<td class="dim">' + esc(c.verdict_text) +
            (c.violations && c.violations.length ? ' [' + esc(c.violations.join(',')) + ']' : '') +
            (c.notes ? '<br>' + esc(c.notes) : '') +
            (c.power_excluded_reason ? '<br>功耗未参与排序: ' + esc(c.power_excluded_reason) : '') +
          '</td></tr>';
      }).join('') + '</tbody></table>';
  }

  if (r.arms) {
    html += '<h2 class="section">两臂逐轮数值 · ' + esc(r.arms.metric) + '</h2>' + abSvg(r.arms, 980, 260) +
      '<div class="legend">' +
        '<span><i style="background:var(--dim)"></i>' + esc(r.arms.a_label) + '</span>' +
        '<span><i style="background:var(--accent)"></i>' + esc(r.arms.b_label) + '</span>' +
      '</div>';
  }

  html += '<h2 class="section">测试条件</h2><div class="kv">' +
    r.conditions.map(function (c) {
      return '<div class="k">' + esc(c.label) + '</div><div>' + val(c.value) +
        (c.from ? '<span class="from">' + esc(c.from) + '</span>' : '') + '</div>';
    }).join('') + '</div>';

  if (r.notes && r.notes.length) {
    html += '<ul class="notes">' + r.notes.map(function (n) { return '<li>' + esc(n) + '</li>'; }).join('') + '</ul>';
  }

  html += '<h2 class="section">证据文件</h2><div class="files">' +
    (r.evidence.length ? r.evidence.map(function (f) {
      return '<span class="file" data-path="' + esc(f.path) + '">' + esc(f.label) +
        '<span class="sz">' + esc(kb(f.size_bytes)) + '</span></span>';
    }).join('') : '<span class="unknown">没有可点开的文件</span>') + '</div>';

  return html;
}

function statsHtml(stats) {
  if (!stats || !stats.length) return '';
  return '<div class="stats">' + stats.map(function (s) {
    return '<div class="stat ' + esc(s.tone) + '">' +
      '<div class="l">' + esc(s.label) + '</div>' +
      '<div class="v">' + val(s.value, s.unit) + '</div>' +
      (s.hint ? '<div class="h">' + esc(s.hint) + '</div>' : '') +
      '</div>';
  }).join('') + '</div>';
}

/* ── 图 ───────────────────────────────────────────── */
function renderCharts() {
  var paretoRounds = allRounds().filter(function (x) { return x.round.pareto; });
  var abRounds = allRounds().filter(function (x) { return x.round.arms; });

  var html = '<h2 class="section">Pareto 散点 · 耗时 vs 画质</h2>';
  if (!paretoRounds.length) {
    html += '<div class="card"><span class="unknown">没有可画的档案数据</span></div>';
  } else {
    var pSel = window.__paretoSel || paretoRounds[0].round.id;
    if (!paretoRounds.some(function (x) { return x.round.id === pSel; })) pSel = paretoRounds[0].round.id;
    window.__paretoSel = pSel;
    var pr = paretoRounds.filter(function (x) { return x.round.id === pSel; })[0].round;
    html += '<div class="card">' +
      '<div class="chart-head"><select id="pareto-sel">' +
        paretoRounds.map(function (x) {
          return '<option value="' + esc(x.round.id) + '"' + (x.round.id === pSel ? ' selected' : '') +
            '>' + esc(x.round.title) + '</option>';
        }).join('') +
      '</select><span class="dim">画质真值: ' + val(pr.pareto.quality_reference) +
      ' — 绝对 dB 只在同一组参考图内可比</span></div>' +
      paretoSvg(pr.pareto, 980, 420) +
      '<div class="legend">' +
        '<span><i style="background:var(--good)"></i>支配前沿</span>' +
        '<span><i style="background:var(--dim)"></i>被支配</span>' +
        '<span>点上的字是候选 id, 悬停看详情</span>' +
      '</div></div>';
  }

  html += '<h2 class="section">A/B 两臂每轮数值对比</h2>';
  if (!abRounds.length) {
    html += '<div class="card"><span class="unknown">没有可画的 A/B 数据</span></div>';
  } else {
    var aSel = window.__abSel || abRounds[0].round.id;
    if (!abRounds.some(function (x) { return x.round.id === aSel; })) aSel = abRounds[0].round.id;
    window.__abSel = aSel;
    var ar = abRounds.filter(function (x) { return x.round.id === aSel; })[0].round;
    var metrics = ar.arms.others && ar.arms.others.length
      ? ar.arms.others : [{ metric: ar.arms.metric, unit: ar.arms.unit,
                            a_values: ar.arms.a_values, b_values: ar.arms.b_values }];
    var mSel = window.__abMetric;
    if (!metrics.some(function (m) { return m.metric === mSel; })) mSel = ar.arms.metric;
    window.__abMetric = mSel;
    var m = metrics.filter(function (x) { return x.metric === mSel; })[0] || metrics[0];
    var view = {
      metric: m.metric, unit: m.unit, a_values: m.a_values, b_values: m.b_values,
      a_label: ar.arms.a_label, b_label: ar.arms.b_label,
      a_runs: ar.arms.a_runs, b_runs: ar.arms.b_runs
    };
    html += '<div class="card">' +
      '<div class="chart-head">' +
        '<select id="ab-sel">' + abRounds.map(function (x) {
          return '<option value="' + esc(x.round.id) + '"' + (x.round.id === aSel ? ' selected' : '') +
            '>' + esc(x.round.title) + '</option>';
        }).join('') + '</select>' +
        '<select id="ab-metric">' + metrics.map(function (x) {
          return '<option value="' + esc(x.metric) + '"' + (x.metric === mSel ? ' selected' : '') +
            '>' + esc(x.metric) + '</option>';
        }).join('') + '</select>' +
        (m.diff_pct === undefined || m.diff_pct === null ? '' :
          '<span class="pill">差异 ' + esc(Number(m.diff_pct).toFixed(2)) + '%</span>') +
        (m.p_value === undefined || m.p_value === null ? '' :
          '<span class="pill">p = ' + esc(Number(m.p_value).toFixed(4)) + '</span>') +
        (m.significant === undefined || m.significant === null ? '' :
          '<span class="badge ' + (m.significant ? 'qualified' : 'eliminated') + '">' +
          (m.significant ? '显著' : '未达显著') + '</span>') +
      '</div>' +
      abSvg(view, 980, 320) +
      '<div class="legend">' +
        '<span><i style="background:var(--dim)"></i>' + esc(view.a_label) + '</span>' +
        '<span><i style="background:var(--accent)"></i>' + esc(view.b_label) + '</span>' +
        '<span>虚线 = 各臂均值</span>' +
      '</div></div>';
  }
  el('page-charts').innerHTML = html;
}

/* Pareto 散点。原生 SVG, 不引任何图表库。 */
function paretoSvg(p, w, h) {
  var pts = p.points.filter(function (d) { return d.y !== null && d.y !== undefined; });
  if (!pts.length) return '<span class="unknown">这一轮的档案里没有可画的点 (画质没量到)</span>';
  var m = { l: 62, r: 20, t: 34, b: 46 };
  var xs = pts.map(function (d) { return d.x; });
  var ys = pts.map(function (d) { return d.y; });
  var x0 = Math.min.apply(null, xs), x1 = Math.max.apply(null, xs);
  var y0 = Math.min.apply(null, ys), y1 = Math.max.apply(null, ys);
  var padX = (x1 - x0) * 0.12 || 0.1, padY = (y1 - y0) * 0.12 || 0.5;
  x0 -= padX; x1 += padX; y0 -= padY; y1 += padY;
  function sx(v) { return m.l + (v - x0) / (x1 - x0) * (w - m.l - m.r); }
  function sy(v) { return h - m.b - (v - y0) / (y1 - y0) * (h - m.t - m.b); }

  var g = '';
  for (var i = 0; i <= 4; i++) {
    var gy = m.t + i * (h - m.t - m.b) / 4;
    var gv = y1 - i * (y1 - y0) / 4;
    g += '<line class="grid" x1="' + m.l + '" y1="' + gy + '" x2="' + (w - m.r) + '" y2="' + gy + '"/>' +
         '<text x="' + (m.l - 8) + '" y="' + (gy + 4) + '" text-anchor="end">' + gv.toFixed(1) + '</text>';
    var gx = m.l + i * (w - m.l - m.r) / 4;
    var xv = x0 + i * (x1 - x0) / 4;
    g += '<line class="grid" x1="' + gx + '" y1="' + m.t + '" x2="' + gx + '" y2="' + (h - m.b) + '"/>' +
         '<text x="' + gx + '" y="' + (h - m.b + 18) + '" text-anchor="middle">' + xv.toFixed(2) + '</text>';
  }

  var front = pts.filter(function (d) { return d.on_front; })
    .sort(function (a, b) { return a.x - b.x; });
  var line = front.length > 1
    ? '<polyline fill="none" stroke="var(--good)" stroke-width="2" stroke-dasharray="5 4" points="' +
      front.map(function (d) { return sx(d.x) + ',' + sy(d.y); }).join(' ') + '"/>'
    : '';

  var dots = pts.map(function (d) {
    var c = d.on_front ? 'var(--good)' : 'var(--dim)';
    var rr = d.on_front ? 8 : 5;
    return '<g><circle cx="' + sx(d.x) + '" cy="' + sy(d.y) + '" r="' + rr + '" fill="' + c +
      '" fill-opacity="' + (d.on_front ? 1 : 0.55) + '"><title>' + esc(d.id) + ' · 第' + d.generation +
      '代 · ' + esc(d.origin) + '\n耗时 ' + d.x + ' ms · 画质 ' + d.y + ' dB' +
      (d.on_front ? '\n在支配前沿上' : '') + '</title></circle>' +
      '<text x="' + sx(d.x) + '" y="' + (sy(d.y) - rr - 5) + '" font-size="11" text-anchor="middle">' + esc(d.id) + '</text></g>';
  }).join('');

  return '<svg viewBox="0 0 ' + w + ' ' + h + '" role="img">' + g + line + dots +
    '<line class="axis" x1="' + m.l + '" y1="' + (h - m.b) + '" x2="' + (w - m.r) + '" y2="' + (h - m.b) + '"/>' +
    '<line class="axis" x1="' + m.l + '" y1="' + m.t + '" x2="' + m.l + '" y2="' + (h - m.b) + '"/>' +
    '<text x="' + (w / 2) + '" y="' + (h - 6) + '" text-anchor="middle">' + esc(p.x_label) + ' → 越左越快</text>' +
    '<text x="14" y="16">' + esc(p.y_label) + ' ↑ 越上越清晰</text>' +
    '</svg>';
}

/* A/B 两臂逐轮柱状图 + 均值虚线。 */
function abSvg(arms, w, h) {
  var a = arms.a_values || [], b = arms.b_values || [];
  var n = Math.max(a.length, b.length);
  if (!n) return '<span class="unknown">这一轮没有逐轮数值</span>';
  var m = { l: 62, r: 20, t: 34, b: 44 };
  var all = a.concat(b);
  var lo = Math.min.apply(null, all), hi = Math.max.apply(null, all);
  var span = (hi - lo) || Math.abs(hi) || 1;
  /* 纵轴不从 0 起: 两臂差 0.5% 时从 0 起画等于什么都看不见。
     所以必须把"轴不从零开始"直接写在图上, 免得把小差异看成大差异。 */
  lo -= span * 0.35; hi += span * 0.25;
  function sy(v) { return h - m.b - (v - lo) / (hi - lo) * (h - m.t - m.b); }

  var slot = (w - m.l - m.r) / n;
  var bw = Math.min(26, slot / 2.6);
  var g = '';
  for (var i = 0; i <= 4; i++) {
    var gy = m.t + i * (h - m.t - m.b) / 4;
    var gv = hi - i * (hi - lo) / 4;
    g += '<line class="grid" x1="' + m.l + '" y1="' + gy + '" x2="' + (w - m.r) + '" y2="' + gy + '"/>' +
         '<text x="' + (m.l - 8) + '" y="' + (gy + 4) + '" text-anchor="end">' + gv.toFixed(2) + '</text>';
  }

  var bars = '';
  for (var k = 0; k < n; k++) {
    var cx = m.l + slot * (k + 0.5);
    if (a[k] !== undefined) {
      bars += bar(cx - bw - 2, sy(a[k]), bw, h - m.b - sy(a[k]), 'var(--dim)',
        (arms.a_runs && arms.a_runs[k] ? arms.a_runs[k] : arms.a_label + ' 第' + (k + 1) + '轮') +
        ': ' + a[k] + ' ' + (arms.unit || ''));
    }
    if (b[k] !== undefined) {
      bars += bar(cx + 2, sy(b[k]), bw, h - m.b - sy(b[k]), 'var(--accent)',
        (arms.b_runs && arms.b_runs[k] ? arms.b_runs[k] : arms.b_label + ' 第' + (k + 1) + '轮') +
        ': ' + b[k] + ' ' + (arms.unit || ''));
    }
    g += '<text x="' + cx + '" y="' + (h - m.b + 18) + '" text-anchor="middle">第' + (k + 1) + '轮</text>';
  }

  function meanOf(arr) {
    if (!arr.length) return null;
    var s = 0; for (var i2 = 0; i2 < arr.length; i2++) s += arr[i2];
    return s / arr.length;
  }
  var lines = '';
  [[meanOf(a), 'var(--dim)'], [meanOf(b), 'var(--accent)']].forEach(function (p) {
    if (p[0] === null) return;
    lines += '<line x1="' + m.l + '" y1="' + sy(p[0]) + '" x2="' + (w - m.r) + '" y2="' + sy(p[0]) +
      '" stroke="' + p[1] + '" stroke-width="1.5" stroke-dasharray="6 5" opacity=".9"/>';
  });

  return '<svg viewBox="0 0 ' + w + ' ' + h + '" role="img">' + g + bars + lines +
    '<line class="axis" x1="' + m.l + '" y1="' + (h - m.b) + '" x2="' + (w - m.r) + '" y2="' + (h - m.b) + '"/>' +
    '<text x="14" y="16">' + esc(arms.metric) + ' (' + esc(arms.unit || '') + ')  纵轴不从 0 起</text>' +
    '</svg>';
}

function bar(x, y, w, h, fill, tip) {
  return '<rect x="' + x + '" y="' + y + '" width="' + w + '" height="' + Math.max(h, 1) +
    '" fill="' + fill + '" rx="2"><title>' + esc(tip) + '</title></rect>';
}

/* ── 设备 ─────────────────────────────────────────── */
function renderDevice() {
  var d = DASH.device;
  var cls = d.holder ? (d.holder.stale ? 'stale' : 'busy') : 'free';
  var who = d.holder ? d.holder.label : '空闲';
  var hb = d.holder && d.holder.heartbeat_age_s !== null && d.holder.heartbeat_age_s !== undefined
    ? d.holder.heartbeat_age_s + ' 秒前' : null;

  var html = '<h2 class="section">谁在用手机</h2><div class="card">' +
    '<div class="holder ' + cls + '">' +
      '<div><div class="dim" style="font-size:13px">当前持有者</div><div class="who">' + esc(who) + '</div></div>' +
      '<div class="kv" style="grid-template-columns:110px 1fr">' +
        '<div class="k">取锁时间</div><div>' + val(d.holder ? d.holder.since : null) + '</div>' +
        '<div class="k">心跳</div><div>' + val(hb) + (d.holder && d.holder.stale
          ? ' <span class="badge invalid">已超期, 可被接管</span>' : '') + '</div>' +
        '<div class="k">排队</div><div>' + (d.queue.length ? d.queue.length + ' 个' : '无') + '</div>' +
      '</div></div>' +
    (d.queue.length ? '<div class="queue">' + d.queue.map(function (q, i) {
      return '<span class="q' + (i === 0 ? ' first' : '') + '">' + (i + 1) + '. ' + esc(q) + '</span>';
    }).join('') + '</div>' : '') +
    '</div>';

  html += '<div class="grid2" style="margin-top:16px">';
  html += '<div class="card"><h2 class="section">最近的取锁 / 释放</h2>' +
    (d.history.length ? '<table><tbody>' + d.history.map(function (e) {
      var color = e.action === 'acquire' ? 'var(--warn)' : (e.action === 'steal' ? 'var(--bad)' : 'var(--good)');
      return '<tr><td class="dim" style="white-space:nowrap">' + esc(e.at) + '</td>' +
        '<td style="color:' + color + ';font-weight:600">' + esc(e.action) + '</td>' +
        '<td>' + esc(e.label) + '</td></tr>';
    }).join('') + '</tbody></table>' : '<span class="unknown">没有锁日志</span>') + '</div>';

  html += '<div class="card"><h2 class="section">最近的真机任务</h2>' +
    (d.recent_tasks.length ? '<table><thead><tr><th>任务</th><th>批次</th><th>目标</th><th>时间</th></tr></thead><tbody>' +
      d.recent_tasks.map(function (t) {
        return '<tr><td><b>' + esc(t.task) + '</b></td><td class="dim">' + esc(t.run) + '</td>' +
          '<td>' + val(t.goal) + '</td>' +
          '<td class="dim" style="white-space:nowrap">' + esc(shortTime(t.modified)) + '</td></tr>';
      }).join('') + '</tbody></table>' : '<span class="unknown">没读到任务记录</span>') + '</div>';
  html += '</div>';

  if (d.problems && d.problems.length) {
    html += '<div class="card" style="margin-top:16px"><ul class="notes">' +
      d.problems.map(function (p) { return '<li class="err">' + esc(p) + '</li>'; }).join('') + '</ul></div>';
  }
  el('page-device').innerHTML = html;
}

/* ── 文件抽屉 ─────────────────────────────────────── */
function openFile(path) {
  el('filedrawer').classList.remove('hidden');
  el('filedrawer-title').textContent = path;
  el('filedrawer-body').innerHTML = '<span class="dim">读取中…</span>';
  var isImg = /\.(png|jpe?g)$/i.test(path);
  if (isImg) {
    el('filedrawer-body').innerHTML = '<img alt="" src="/api/file?path=' + encodeURIComponent(path) + '">';
    return;
  }
  fetch('/api/file?path=' + encodeURIComponent(path), { cache: 'no-store' })
    .then(function (r) { return r.text().then(function (t) { return { ok: r.ok, t: t }; }); })
    .then(function (res) {
      var text = res.t;
      if (res.ok && /\.json$/i.test(path)) {
        try { text = JSON.stringify(JSON.parse(text), null, 1); } catch (e) { /* 原样显示 */ }
      }
      el('filedrawer-body').innerHTML = (res.ok ? '<pre>' : '<pre class="err">') + esc(text) + '</pre>';
    })
    .catch(function (e) {
      el('filedrawer-body').innerHTML = '<pre class="err">' + esc(e.message) + '</pre>';
    });
}

/* ── 事件 ─────────────────────────────────────────── */
document.addEventListener('click', function (ev) {
  var t = ev.target;
  var navBtn = t.closest ? t.closest('#nav button') : null;
  if (navBtn) { showPage(navBtn.dataset.page); return; }
  if (t.id === 'refresh-now') { load(); return; }
  if (t.id === 'filedrawer-close') { el('filedrawer').classList.add('hidden'); return; }

  var file = t.closest ? t.closest('.file') : null;
  if (file) { openFile(file.dataset.path); return; }

  var layer = t.closest ? t.closest('.layer-card') : null;
  if (layer) {
    var first = allRounds().filter(function (x) { return x.layer.id === layer.dataset.layer; })[0];
    if (first) SELECTED_ROUND = first.round.id;
    showPage('rounds');
    return;
  }
  var item = t.closest ? t.closest('.round-item') : null;
  if (item) { SELECTED_ROUND = item.dataset.round; renderRounds(); return; }
});

document.addEventListener('change', function (ev) {
  if (ev.target.id === 'pareto-sel') { window.__paretoSel = ev.target.value; renderCharts(); }
  if (ev.target.id === 'ab-sel') { window.__abSel = ev.target.value; window.__abMetric = null; renderCharts(); }
  if (ev.target.id === 'ab-metric') { window.__abMetric = ev.target.value; renderCharts(); }
});

document.addEventListener('keydown', function (ev) {
  if (ev.key === 'Escape') el('filedrawer').classList.add('hidden');
});

load();
timer = setInterval(load, REFRESH_MS);
