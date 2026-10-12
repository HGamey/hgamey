// HGamey 实时看板首页：手机位 + 目标运行 + 时间线 + 掌舵。纯静态，轮询节点上的 JSON 接口。
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const num = (v, d = 1) => (v == null || Number.isNaN(+v) ? "—" : (+v).toFixed(d));
  const pct = (v, d = 1) => (v == null ? "—" : (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v).toFixed(d) + "%");
  const hm = (iso) => { const d = typeof iso === "number" ? new Date(iso * 1000) : new Date(iso); return isNaN(d) ? "" : d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false }); };
  const dt = (iso) => { const d = typeof iso === "number" ? new Date(iso * 1000) : new Date(iso); return isNaN(d) ? "" : d.toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false }); };

  const METRIC = { power_watt: ["整机功耗", "lower"], power_w_mean: ["整机功耗", "lower"], frame_p95: ["卡顿程度", "lower"], fps_p95_ms: ["卡顿程度", "lower"],
    frame_p95_ms: ["卡顿程度", "lower"], fps_mean: ["平均帧率", "higher"], gpu_busy_ms: ["显卡忙碌时间", "lower"], psnr_db: ["画面和原来的相似度", "higher"], image: ["画面", "higher"] };
  const mname = (k) => (METRIC[k] || [k])[0];
  const MTIP = { frame_p95: "最慢的 5% 画面帧要多少毫秒（帧时 p95），越小越不卡", fps_p95_ms: "最慢的 5% 画面帧要多少毫秒（帧时 p95），越小越不卡",
    frame_p95_ms: "最慢的 5% 画面帧要多少毫秒（帧时 p95），越小越不卡", power_watt: "整台手机的耗电功率（瓦）", power_w_mean: "整台手机的耗电功率（瓦）" };
  const mlabel = (k) => MTIP[k] ? `<span title="${esc(MTIP[k])}">${esc(mname(k))}</span>` : esc(mname(k));
  // 统计术语只进悬浮提示：正文说「差别可靠 / 可能只是波动」
  const rel = (p) => p == null ? "" : `<span class="rel" title="统计检验 p=${num(p, 3)}（越小越可靠，小于 0.05 算可靠）">${p < 0.05 ? "差别可靠" : "可能只是波动"}</span>`;
  // 判定理由里的统计说法翻成大白话（原文留在悬浮提示）
  const plainReason = (r) => {
    if (!r) return "";
    let t = String(r)
      .replace(/存在显著退化[:：]\s*([\w_,\s]+)/g, (_, ks) => "有指标明显变差：" + ks.split(/[,\s]+/).filter(Boolean).map(mname).join("、"))
      .replace(/没有任何指标显著改善/g, "没有哪一项明显变好")
      .replace(/显著改善/g, "明显变好").replace(/显著变差|显著退化/g, "明显变差");
    return t === r ? esc(r) : `<span class="rel" title="${esc(r)}">${esc(t)}</span>`;
  };
  const relTxt = (p) => p == null ? "" : (p < 0.05 ? "差别可靠" : "可能只是波动") + `（p=${num(p, 3)}）`;
  const LAYER = { sysparam: "改系统设置", gray: "精简画面步骤", upop: "改画面算法", shader: "改画面算法" };
  const VERDICT = { PASS: ["有效，保留", "win"], KEEP: ["有效，保留", "win"], REJECT: ["没效果或有变差，淘汰", "lose"], ABORT: ["这次没测准，不算数", "void"],
    SLOW: ["太慢，淘汰", "lose"], POOR_QUALITY: ["画面变差，淘汰", "lose"], CRASH: ["游戏跑崩了，淘汰", "lose"], REVERTED: ["已还原", "void"] };
  const STOP = { target_reached: "达到目标了", budget_exhausted: "预算用完了", no_gain: "连续几代没有收益，这条路走不通", human_stop: "你叫停的", error: "出错停下" };

  const S = { home: null, runId: null, userPicked: false, run: null, humanDirty: false, humanBase: null, histOpen: null, draftId: null };

  async function api(url, body) {
    const opt = body === undefined ? { cache: "no-store", credentials: "same-origin" }
      : { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json", "X-HG": "1" }, body: JSON.stringify(body) };
    const r = await fetch(url, opt);
    if (r.status === 401) { location.href = "/dash/login"; throw new Error("401"); }
    let j = {}; try { j = await r.json(); } catch (e) { }
    if (!r.ok) { const err = new Error(j.error || `HTTP ${r.status}`); err.data = j; err.status = r.status; throw err; }
    return j;
  }

  // ───────── 首页数据（4 秒）─────────
  async function pollHome() {
    try {
      const h = await api("/dash/api/home");
      S.home = h;
      $("live").className = "live ok"; $("live-text").textContent = `实时 · ${hm(h.now)}`;
      renderSlots(h); renderSteerBtn(h); renderRunSel(h);
    } catch (e) { $("live").className = "live bad"; $("live-text").textContent = "连不上节点"; }
  }

  function renderSteerBtn(h) {
    const b = $("steer-btn");
    b.textContent = h.steer ? "掌舵：已解锁" : "掌舵：未解锁";
    b.className = "btn sm" + (h.steer ? "" : "");
    b.title = h.steer ? "点一下重新上锁" : "输入掌舵密码";
    $("steer-lock").hidden = !!h.steer;
  }

  function renderSlots(h) {
    const el = $("slots");
    el.innerHTML = h.slots.map((s, i) => {
      if (!s) return `<div class="slot empty"><div class="plug"><b>手机位 ${i + 1}</b>未接入</div></div>`;
      const on = s.online, cls = !on ? "off" : s.busy ? "busy" : "on";
      const st = !on ? (s.state || "离线") : s.busy ? "在线 · 测量中" : "在线 · 空闲";
      const thumb = s.shot_at ? `<img alt="${esc(s.model || s.serial)} 画面" src="/dash/api/shot?serial=${encodeURIComponent(s.serial)}&t=${s.shot_at}">`
        : `<div class="ph">${s.busy ? "测量中<br>不截图" : "暂无画面"}</div>`;
      const doing = s.goal ? `<div class="doing on" title="${esc(s.goal.summary)}">▶ ${esc(s.goal.summary)}</div>`
        : `<div class="doing">${s.busy ? "开发调试占用中" : "没有在跑目标"}</div>`;
      return `<div class="slot"><div class="thumb">${thumb}</div><div class="meta">
        <div class="name" title="${esc(s.model || "")}">${esc(s.model || "未知型号")}</div>
        <div class="serial">${esc(s.serial)}</div>
        <span class="status ${cls}"><i></i>${esc(st)}</span>
        <div class="nums"><div><b>${s.soc_c == null ? "—" : num(s.soc_c, 0) + "°"}</b><span>芯片温度</span></div>
          <div><b>${s.battery_pct == null ? "—" : s.battery_pct + "%"}</b><span>电量</span></div>
          <div><b>${s.battery_c == null ? "—" : num(s.battery_c, 0) + "°"}</b><span>电池</span></div></div>
        ${doing}</div></div>`;
    }).join("");
    const n = h.slots.filter(Boolean).length;
    $("slots-note").textContent = (h.fleet_age_s == null ? (h.fleet_err || "首次读取中…") : `${n} 台接入 · 数据 ${h.fleet_age_s}s 前`);
  }

  function renderRunSel(h) {
    const runs = h.runs || [];
    const sel = $("run-sel");
    if (!runs.length) {
      sel.hidden = true; S.runId = null;
      $("run-status").textContent = "还没有目标"; $("run-status").className = "spill"; $("run-demo").hidden = true; $("run-when").textContent = "";
      $("run-body").innerHTML = `<div class="empty-run"><b>还没有目标运行</b>点右上角「＋ 新目标」，用一句话告诉 harness 你想要什么。</div>`;
      $("tl").innerHTML = ""; return;
    }
    if (!S.userPicked || !runs.find((r) => r.run_id === S.runId)) S.runId = runs[0].run_id;
    sel.hidden = runs.length < 2;
    sel.innerHTML = runs.map((r) => `<option value="${esc(r.run_id)}" ${r.run_id === S.runId ? "selected" : ""}>${esc(r.status_zh)} · ${esc((r.state.goal_summary || r.intent || r.run_id).slice(0, 28))}</option>`).join("");
  }
  $("run-sel").addEventListener("change", (e) => { S.runId = e.target.value; S.userPicked = true; S.humanDirty = false; S.histOpen = null; pollRun(); });

  // ───────── 选中的目标运行（4 秒）─────────
  async function pollRun() {
    if (!S.runId) return;
    try {
      const r = await api(`/dash/api/run?id=${encodeURIComponent(S.runId)}`);
      if (r.run_id !== S.runId) return;
      S.run = r;
      renderRun(r); renderTimeline(r); renderSteer(r);
    } catch (e) { }
  }

  function parseGoal(md) {
    const g = { metric: null, direction: null, target: null, maxGen: null, budget: null, layers: null };
    if (!md) return g;
    const fm = (md.match(/^---\s*\n([\s\S]*?)\n---/) || [])[1] || "";
    const obj = (fm.match(/objective:\s*\n((?:[ \t]+.*\n?)+)/) || [])[1] || "";
    g.metric = (obj.match(/metric:\s*([\w.]+)/) || [])[1] || null;
    g.direction = (obj.match(/direction:\s*(\w+)/) || [])[1] || null;
    const t = (obj.match(/target_pct:\s*([\d.]+)/) || [])[1]; g.target = t ? +t : null;
    const mg = (fm.match(/max_generations:\s*(\d+)/) || [])[1]; g.maxGen = mg ? +mg : null;
    const wm = (fm.match(/wall_minutes:\s*(\d+)/) || [])[1]; g.budget = wm ? +wm : null;
    const ly = (fm.match(/layers:\s*\[([^\]]*)\]/) || [])[1]; g.layers = ly ? ly.split(",").map((x) => x.trim()).filter(Boolean) : null;
    return g;
  }

  function candidates(events) {
    const m = new Map();
    const get = (id, gen) => { if (!m.has(id)) m.set(id, { id, gen, layer: null, why: "", verdict: null, metrics: {}, measuring: null, kept: false }); return m.get(id); };
    for (const e of events) {
      const id = e.candidate_id; if (!id) continue;
      const c = get(id, e.gen);
      if (e.type === "propose") { c.layer = e.layer; c.why = e.why; c.changes = e.changes; }
      if (e.type === "measure") c.measuring = [e.arms_done, e.arms_planned];
      if (e.type === "verdict") { c.verdict = e.verdict; c.metrics = e.metrics || {}; c.measuring = null; c.reason = e.reason; }
      if (e.type === "keep") c.kept = true;
      if (e.type === "note" && /撤下|不上机|拦下/.test(e.text || "")) c.dropped = true;
      if (e.type === "gate" && /block|reject|拦/.test(String(e.decision))) c.dropped = true;
    }
    return [...m.values()];
  }

  function renderRun(r) {
    const st = r.state || {}, g = parseGoal(r.goal_md);
    $("run-status").textContent = r.status_zh; $("run-status").className = "spill " + r.status;
    $("run-demo").hidden = !r.demo;
    $("run-when").textContent = r.updated ? `更新于 ${dt(r.updated)}` : "";
    const metric = (st.best && st.best.metric) || g.metric || "power_watt";
    const dir = g.direction || (METRIC[metric] || [])[1] || "lower";
    const target = g.target;
    const best = st.best;
    const improve = best && best.delta_pct != null ? (dir === "lower" ? -best.delta_pct : best.delta_pct) : null;
    const prog = st.progress_to_target != null ? st.progress_to_target : improve != null && target ? improve / target : 0;
    const gen = st.generation || 0, maxGen = st.max_generations || g.maxGen || 0;
    const used = st.elapsed_min, budget = st.budget_min || g.budget;
    const want = dir === "lower" ? "降" : "提";
    let progText;
    if (r.status === "awaiting_confirm" || r.status === "drafting") progText = r.status === "drafting" ? "harness 正在起草目标…" : "草稿等你确认后开跑";
    else if (improve == null) progText = `目标：${mname(metric)}${want} ${target ?? "?"}% · 还没有有效的方案`;
    else progText = `目标${want} ${target ?? "?"}%，目前最好${improve >= 0 ? want : "反而变差"}了 ${Math.abs(improve).toFixed(1)}%`;
    const dots = maxGen ? Array.from({ length: maxGen }, (_, i) => `<i class="${i + 1 < gen ? "done" : i + 1 === gen ? (r.status === "running" ? "cur" : "done") : ""}"></i>`).join("") : "";
    const bestHtml = best ? `<div class="v ${improve > 0 ? "good-txt" : ""}">${pct(best.delta_pct)} <small>${esc(mname(best.metric || metric))}</small></div>
        <div class="s">${esc(best.candidate || "")}${best.change ? " · " + esc(best.change) : ""}<br>${esc((VERDICT[best.verdict] || [best.verdict || ""])[0])}</div>`
      : `<div class="v">—</div><div class="s">还没有有效的方案</div>`;
    const why = r.status === "paused" ? `<p class="note2" style="margin-top:12px">已暂停：手机锁已放掉，改「掌舵」里的指令后 harness 会重新排队继续。</p>`
      : st.stop_reason ? `<p class="note2" style="margin-top:12px">停下原因：${esc(STOP[st.stop_reason] || st.stop_reason)}${r.report_md ? "。总报告在右边「总报告」里。" : ""}</p>` : "";
    const action = r.status === "awaiting_confirm" && !r.demo ? `<div class="row-btns"><button class="btn primary" type="button" data-act="review">查看草稿并开跑</button></div>` : "";
    $("run-body").innerHTML = `
      <div class="goal-title">${esc(st.goal_summary || r.intent || r.run_id)}</div>
      ${r.intent ? `<p class="intent">${esc(r.intent)}</p>` : ""}
      <div class="prog"><div class="prog-h"><span>${esc(progText)}</span><b>${Math.round(Math.max(0, Math.min(1, prog)) * 100)}%</b></div>
        <div class="bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(prog * 100)}"><i style="width:${Math.max(0, Math.min(1, prog)) * 100}%"></i></div></div>
      <div class="stats">
        <div class="stat"><div class="k">第几代</div><div class="v">${gen || "—"}<small> / ${maxGen || "?"} 代</small></div><div class="gens-dots">${dots}</div>${st.no_gain_streak ? `<div class="s">已连续 ${st.no_gain_streak} 代没收益</div>` : ""}</div>
        <div class="stat"><div class="k">时间预算</div><div class="v">${used ?? "—"}<small> / ${budget ?? "?"} 分钟</small></div>
          <div class="bar thin amber" style="margin-top:10px"><i style="width:${used != null && budget ? Math.min(100, used / budget * 100) : 0}%"></i></div></div>
        <div class="stat"><div class="k">目前最好的方案</div>${bestHtml}</div>
      </div>${why}${action}
      <div class="cchart" id="cchart"></div>`;
    renderCandChart(candidates(r.events || []), metric, dir, target);
    $("goalmd").textContent = r.goal_md || "（还没有 goal.md）";
    $("rep").hidden = !r.report_md; if (r.report_md) $("reportmd").textContent = r.report_md;
  }

  // 每个方案在主指标上的改善（向上 = 更好），颜色 = 结局：保留 蓝 / 淘汰 琥珀 / 测量中 绿
  function renderCandChart(cands, metric, dir, target) {
    const box = $("cchart");
    const list = cands.filter((c) => !c.dropped || c.verdict);
    if (!list.length) { box.innerHTML = ""; return; }
    const val = (c) => { const m = c.metrics[metric] || c.metrics[metric === "power_watt" ? "power_w_mean" : metric]; return m && m.delta_pct != null ? (dir === "lower" ? -m.delta_pct : m.delta_pct) : null; };
    const vals = list.map(val);
    const ext = Math.max(2, target || 0, ...vals.filter((v) => v != null).map(Math.abs)) * 1.15;
    const W = Math.max(280, Math.round(box.clientWidth || 640)), H = 170, L = 36, B = 22, T = 8, mid = T + (H - T - B) / 2, sc = (H - T - B) / 2 / ext;
    const slot = (W - L) / list.length, bw = Math.max(6, Math.min(34, slot * 0.55));
    let g = "";
    for (const v of [ext / 1.15, 0, -ext / 1.15]) {
      const y = mid - v * sc;
      g += `<line x1="${L}" x2="${W}" y1="${y}" y2="${y}" stroke="${v === 0 ? "var(--axis)" : "var(--grid)"}" stroke-width="1"/>
        <text x="${L - 6}" y="${y + 4}" font-size="10" text-anchor="end" fill="var(--muted)">${v === 0 ? "0" : pct(v, 0)}</text>`;
    }
    if (target) { const y = mid - target * sc; g += `<line x1="${L}" x2="${W}" y1="${y}" y2="${y}" stroke="var(--blue)" stroke-dasharray="4 4" stroke-width="1"/><text x="${W}" y="${y - 4}" font-size="10" text-anchor="end" fill="var(--blue)">目标 ${target}%</text>`; }
    let lastGen = null;
    list.forEach((c, i) => {
      const x = L + i * slot + (slot - bw) / 2, v = vals[i];
      if (c.gen !== lastGen) { g += `<text x="${L + i * slot + 2}" y="${H - 6}" font-size="10" fill="var(--muted)">第${c.gen}代</text>`; if (i) g += `<line x1="${L + i * slot}" x2="${L + i * slot}" y1="${T}" y2="${H - B}" stroke="var(--grid)" stroke-dasharray="2 3"/>`; lastGen = c.gen; }
      const kind = c.verdict ? (VERDICT[c.verdict] || ["", "void"])[1] : c.measuring ? "measuring" : "pending";
      const col = { win: "var(--blue)", lose: "var(--amber)", void: "var(--muted)", measuring: "var(--teal)", pending: "var(--grid)" }[kind];
      if (v == null) {
        g += `<rect data-i="${i}" x="${x}" y="${mid - 3}" width="${bw}" height="6" rx="3" fill="${col}" ${kind === "pending" ? 'stroke="var(--axis)" fill-opacity=".6"' : ""}/>`;
        if (kind === "measuring") g += `<text x="${x + bw / 2}" y="${mid - 8}" font-size="10" text-anchor="middle" fill="var(--teal)">正在测</text>`;
      } else {
        const h = Math.max(2, Math.abs(v) * sc), y = v >= 0 ? mid - h : mid;
        g += `<rect data-i="${i}" x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${bw.toFixed(1)}" height="${h.toFixed(1)}" rx="4" fill="${col}" ${kind === "void" ? 'fill-opacity=".5"' : ""}/>`;
        if (c.kept || kind === "win") g += `<text x="${x + bw / 2}" y="${(v >= 0 ? y - 4 : y + h + 12).toFixed(1)}" font-size="10" text-anchor="middle" fill="var(--ink)" font-weight="700">${pct(v)}</text>`;
      }
      g += `<rect data-i="${i}" x="${L + i * slot}" y="${T}" width="${slot}" height="${H - T - B}" fill="transparent"/>`;
    });
    box.innerHTML = `<div class="cchart-h"><h3>每个方案的「${esc(mname(metric))}」改善（向上 = 更好）</h3>
      <div class="legend2"><span><i style="background:var(--blue)"></i>有效，保留</span><span><i style="background:var(--amber)"></i>淘汰</span><span><i style="background:var(--teal)"></i>正在测</span><span><i style="background:var(--muted);opacity:.5"></i>没测准</span></div></div>
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="height:${H}px" role="img" aria-label="每个方案的改善">${g}</svg>`;
    const svg = box.querySelector("svg");
    svg.addEventListener("mousemove", (ev) => {
      const i = ev.target.dataset && ev.target.dataset.i; if (i == null) return hideTip();
      const c = list[+i], ms = Object.entries(c.metrics || {}).map(([k, m]) => `${esc(mname(k))} ${pct(m.delta_pct)}${m.p != null ? "，" + relTxt(m.p) : ""}`).join("<br>");
      showTip(ev, `<b>${esc(c.id)}</b> · ${esc(LAYER[c.layer] || c.layer || "")}<br>${esc(c.why || "")}<br>${c.verdict ? esc((VERDICT[c.verdict] || [c.verdict])[0]) : c.measuring ? `测量中 ${c.measuring[0]}/${c.measuring[1]} 轮` : "等着上机"}${ms ? "<br>" + ms : ""}`);
    });
    svg.addEventListener("mouseleave", hideTip);
  }

  // ───────── 时间线：events.jsonl → 大白话 ─────────
  const fmtChanges = (c) => !c ? "" : Array.isArray(c) ? c.join(" + ") : typeof c === "object" ? Object.entries(c).map(([k, v]) => `${k}=${v}`).join(", ") : String(c);
  function sentence(e) {
    const cid = e.candidate_id ? `<code>${esc(e.candidate_id)}</code>` : "";
    switch (e.type) {
      case "start": return ["▶", "blue", `开始运行${e.summary ? "：" + esc(e.summary) : ""}${e.device ? `<span class="sub2">手机 ${esc(e.device)}</span>` : ""}`];
      case "plan": {
        if (e.text || e.summary) return ["◆", "blue", `定好计划：${esc(e.text || e.summary)}`];
        const ly = (e.layers || []).map((l) => LAYER[l] || l).join("、");
        const fb = (e.forbid || []).length ? `<span class="sub2">不碰这些设置：<code>${esc(e.forbid.join(", "))}</code></span>` : "";
        return ["◆", "blue", `这一代要试：${esc(ly || "—")}${e.per_generation ? `，每类 ${esc(e.per_generation)} 个方案` : ""}${e.budget_left_min != null ? `；还剩 ${Math.round(e.budget_left_min)} 分钟预算` : ""}${fb}`];
      }
      case "steer": return ["✎", "brown", `读到你的新指令（第 ${esc(e.human_md_rev ?? "?")} 版）：“${esc(e.text || "")}”${e.interpreted ? `<span class="sub2">harness 是这么理解的：${esc(e.interpreted)}</span>` : ""}`];
      case "propose": return ["＋", "", `提出方案 ${cid}（${esc(LAYER[e.layer] || e.layer || "")}）：${esc(e.why || "")}${e.changes ? `<span class="sub2">改动：<code>${esc(fmtChanges(e.changes))}</code></span>` : ""}`];
      case "gate": { const ok = /pass|allow|放行|ok/i.test(String(e.decision)); return ["⛨", ok ? "teal" : "amber", `上手机前先预审 ${cid}：打了 ${num(e.score, 2)} 分 → ${ok ? "通过" : "拦下，不上手机"}`]; }
      case "measure": return ["◐", "teal", e.arms_done ? `在真手机上测 ${cid}：第 ${esc(e.arms_done)} / ${esc(e.arms_planned)} 轮（改前、改后轮流）`
        : `开始在真手机上测 ${cid}：<span title="对照臂 / 方案臂交替">改前、改后轮流测</span>，共 ${esc(e.arms_planned)} 轮，测完才出结论`];
      case "verdict": {
        const v = VERDICT[e.verdict] || [e.verdict, "void"];
        const ms = Object.entries(e.metrics || {}).map(([k, m]) => `${mlabel(k)} ${pct(m.delta_pct)}${m.p != null ? "（" + rel(m.p) + "）" : ""}`).join("，");
        const extra = [e.image_psnr != null ? `画面相似度 ${num(e.image_psnr)} dB` : "", e.contested_arms ? `<span title="contested arms">${e.contested_arms} 轮被手机厂商的性能服务干扰</span>` : "", e.reason ? `原因：${plainReason(e.reason)}` : ""].filter(Boolean).join("；");
        return [v[1] === "win" ? "✓" : v[1] === "lose" ? "✗" : "∅", v[1] === "win" ? "blue" : v[1] === "lose" ? "amber" : "", `结论：${cid} ${esc(v[0])}${ms ? `<span class="sub2">${ms}</span>` : ""}${extra ? `<span class="sub2">${extra}</span>` : ""}`];
      }
      case "keep": return ["★", "blue", `${cid} 进入保留名单${e.delta_pct != null ? `（${esc(mname(e.metric || ""))} ${pct(e.delta_pct)}）` : ""}，后面的方案在它基础上继续`];
      case "stop": return ["■", e.reason === "error" ? "red" : "brown", `停下了：${esc(STOP[e.reason] || e.reason || "")}${e.summary ? `<span class="sub2">${esc(e.summary)}</span>` : ""}`];
      case "error": return ["!", "red", `出错：${esc(e.message || e.text || e.error || "")}`];
      case "note": return ["·", e.stage === "draft" ? "brown" : "", (e.stage === "draft" ? `起草：${e.by === "llm" ? "大模型" : e.by === "template" ? "模板" : esc(e.by || "")} · ` : "") + esc(e.text || e.message || "")];
      default: return ["·", "", `${esc(e.type)} ${esc(JSON.stringify(e).slice(0, 160))}`];
    }
  }
  let tlShowAll = false;
  function renderTimeline(r) {
    let evs = (r.events || []).slice();
    // 同一个方案的多条「测量中」只留最新一条
    const lastMeasure = new Map();
    evs.forEach((e, i) => { if (e.type === "measure") lastMeasure.set(e.candidate_id, i); });
    evs = evs.filter((e, i) => e.type !== "measure" || lastMeasure.get(e.candidate_id) === i);
    evs.reverse();
    const total = evs.length;
    if (!tlShowAll) evs = evs.slice(0, 40);
    $("tl-note").textContent = total ? `共 ${total} 条，最新的在上面` : "";
    if (!total) { $("tl").innerHTML = `<li class="empty">还没有事件</li>`; return; }
    let html = "", gen = undefined;
    for (const e of evs) {
      const g = e.gen ?? 0;
      if (g !== gen) { html += `<li class="tl-gen">${g ? `第 ${g} 代` : "准备"}</li>`; gen = g; }
      const [ic, col, tx] = sentence(e);
      html += `<li class="ev ${esc(e.type)}"><span class="t">${esc(hm(e.t))}</span><span class="ic ${col}" aria-hidden="true">${ic}</span><span class="tx">${tx}</span></li>`;
    }
    if (total > 40) html += `<li class="tl-more"><button class="btn sm" type="button" data-act="tl-toggle">${tlShowAll ? "收起" : `显示全部 ${total} 条`}</button></li>`;
    $("tl").innerHTML = html;
  }

  // ───────── 掌舵 ─────────
  const editable = (r) => r && ["running", "paused", "awaiting_confirm", "drafting"].includes(r.status);
  function renderSteer(r) {
    const ta = $("human");
    if (!S.humanDirty || S.humanBase === null) { ta.value = r.human_md || ""; S.humanBase = r.human_mtime; }
    const can = editable(r) && S.home && S.home.steer;
    ta.readOnly = !can;
    $("save-human").disabled = !can;
    $("stop-run").disabled = !can || !(["running", "paused"].includes(r.status) || r.run_alive);
    if (!editable(r)) $("steer-msg").textContent = "这个目标已经结束，指令只读";
    $("hist").innerHTML = (r.human_history || []).slice(0, 12).map((h, i) =>
      `<li><span class="t">${esc(h.name.replace(/^(\d{4})(\d\d)(\d\d)-(\d\d)(\d\d)(\d\d)\.md$/, "$2-$3 $4:$5:$6"))}</span><span class="muted">第 ${(r.human_history.length - i)} 版</span>
       <button class="btn sm" type="button" data-hist="${esc(h.name)}">${S.histOpen === h.name ? "收起" : "看看"}</button></li>`).join("") || `<li><span class="muted">还没有改过</span></li>`;
    const a = (r.audit || [])[0];
    $("audit").textContent = a ? `最近一次：${dt(a.t)} 来自 ${a.ip}（${{ edit_human: "改指令", stop: "停止", new_goal: "新目标", confirm: "确认开跑" }[a.action] || a.action}）` : "";
  }
  $("human").addEventListener("input", () => { S.humanDirty = true; $("steer-msg").textContent = "有未保存的修改"; $("steer-msg").className = "msg"; });
  $("hist").addEventListener("click", async (ev) => {
    const b = ev.target.closest("[data-hist]"); if (!b) return;
    const name = b.dataset.hist;
    if (S.histOpen === name) { S.histOpen = null; $("hist-view").hidden = true; renderSteer(S.run); return; }
    try {
      const r = await api(`/dash/api/run/human_rev?id=${encodeURIComponent(S.runId)}&name=${encodeURIComponent(name)}`);
      S.histOpen = name;
      const v = $("hist-view"); v.hidden = false;
      v.innerHTML = `${esc(r.text) || "（空）"}<div class="row-btns"><button class="btn sm" type="button" data-act="restore">放回编辑框</button></div>`;
      v.dataset.text = r.text; renderSteer(S.run);
    } catch (e) { }
  });

  async function needSteer() {
    if (S.home && S.home.steer) return true;
    $("unlock-msg").textContent = ""; $("steer-pw").value = "";
    $("dlg-unlock").showModal(); $("steer-pw").focus();
    return false;
  }
  $("f-unlock").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    try { await api("/dash/api/steer/unlock", { pw: $("steer-pw").value }); $("dlg-unlock").close(); await pollHome(); if (S.run) renderSteer(S.run); }
    catch (e) { $("unlock-msg").textContent = e.message; }
  });
  $("steer-btn").addEventListener("click", async () => {
    if (S.home && S.home.steer) { await api("/dash/api/steer/lock", {}); await pollHome(); if (S.run) renderSteer(S.run); }
    else needSteer();
  });

  async function steerCall(url, body, msgEl) {
    if (!(await needSteer())) return null;
    const m = $(msgEl); m.className = "msg"; m.innerHTML = `<span class="spin"></span> 提交中`;
    try { const r = await api(url, body); m.textContent = r.note || "已保存"; return r; }
    catch (e) { if (e.data && e.data.need_steer) { await pollHome(); needSteer(); } m.className = "msg err"; m.textContent = e.message; return null; }
  }
  $("save-human").addEventListener("click", async () => {
    const r = await steerCall("/dash/api/run/human", { id: S.runId, text: $("human").value, base_mtime: S.humanBase }, "steer-msg");
    if (r) { S.humanDirty = false; S.humanBase = null; if (r.saved) $("steer-msg").textContent = "已保存，harness 下一代开始前会读到"; pollRun(); }
  });
  $("stop-run").addEventListener("click", async () => {
    if (!confirm("让 harness 立刻停下来？\n正在测的那一轮会先把手机还原，然后写总报告。")) return;
    const r = await steerCall("/dash/api/run/stop", { id: S.runId }, "steer-msg");
    if (r) { S.humanDirty = false; S.humanBase = null; pollRun(); }
  });

  // ───────── 新目标 ─────────
  function openNew(step) {
    const dlg = $("dlg-new");
    $("new-1").hidden = step !== 1; $("new-2").hidden = step !== 2;
    ["st1", "st2", "st3"].forEach((id, i) => $(id).className = i + 1 === step ? "on" : "");
    if (step === 1) {
      const slots = (S.home && S.home.slots || []).filter((s) => s && s.online);
      $("dev-sel").innerHTML = `<option value="any">任一台空闲的同型号手机</option>` + slots.map((s) => `<option value="${esc(s.serial)}">${esc(s.model || "")} · ${esc(s.serial)}${s.busy ? "（现在有人在用，会排队）" : ""}</option>`).join("");
      $("draft-msg").textContent = "";
    }
    if (!dlg.open) dlg.showModal();
  }
  $("new-btn").addEventListener("click", async () => { if (await needSteer()) openNew(1); });
  $("do-draft").addEventListener("click", async () => {
    const intent = $("intent").value.trim();
    if (!intent) { $("draft-msg").textContent = "先写一句话"; return; }
    const r = await steerCall("/dash/api/goal/draft", { intent, game: $("game").value.trim(), device: $("dev-sel").value }, "draft-msg");
    if (!r) return;
    S.draftId = r.run_id; S.runId = r.run_id; S.userPicked = true;
    $("draft-msg").innerHTML = `<span class="spin"></span> harness 正在起草…`;
    waitDraft(r.run_id, r.mode);
  });
  async function waitDraft(id, mode) {
    for (let i = 0; i < 120; i++) {
      try {
        const r = await api(`/dash/api/run?id=${encodeURIComponent(id)}`);
        if (r.goal_md && r.status === "awaiting_confirm") { showDraft(r, mode); return; }
        if (r.status === "failed") { $("draft-msg").textContent = "起草失败，去维护页看日志"; return; }
      } catch (e) { }
      await new Promise((ok) => setTimeout(ok, 2000));
    }
    $("draft-msg").textContent = "起草超时，去维护页看日志";
  }
  function showDraft(r, mode) {
    S.draftId = r.run_id;
    let md = r.goal_md || "";
    const want = r.dash && r.dash.device;
    if (want && want !== "any") md = md.replace(/^device:.*$/m, `device: ${want}`);
    $("goalmd-edit").value = md;
    const byTpl = (r.events || []).some((e) => e.type === "note" && e.stage === "draft" && e.by === "template") || mode === "template";
    $("draft-note").textContent = (byTpl ? "这份草稿是模板填的（大模型没起草成功），请重点检查主指标和目标幅度。" : "这是 harness 根据你的话让大模型起草的。")
      + "可以直接改：主要看哪个数、想改善多少、时间预算、每代试几个方案、允许哪几类改动、哪些设置不许碰。点「确认并开始」后 goal.md 就冻结，运行中想调整请改「掌舵」里的指令。";
    $("confirm-msg").textContent = "";
    openNew(2);
  }
  $("do-confirm").addEventListener("click", async () => {
    const r = await steerCall("/dash/api/goal/confirm", { id: S.draftId, goal_md: $("goalmd-edit").value }, "confirm-msg");
    if (!r) return;
    ["st1", "st2", "st3"].forEach((id, i) => $(id).className = i === 2 ? "on" : "");
    if (r.started) { $("confirm-msg").textContent = "已开跑，harness 会去排队拿手机"; setTimeout(() => $("dlg-new").close(), 1200); }
    pollHome(); pollRun();
  });
  document.querySelectorAll("dialog [data-close]").forEach((b) => b.addEventListener("click", () => b.closest("dialog").close()));

  document.addEventListener("click", (ev) => {
    const a = ev.target.closest("[data-act]"); if (!a) return;
    if (a.dataset.act === "unlock") needSteer();
    if (a.dataset.act === "tl-toggle") { tlShowAll = !tlShowAll; renderTimeline(S.run); }
    if (a.dataset.act === "restore") { $("human").value = $("hist-view").dataset.text || ""; S.humanDirty = true; $("steer-msg").textContent = "已放回编辑框，点「保存指令」生效"; }
    if (a.dataset.act === "review" && S.run) showDraft(S.run, S.run.harness_ready ? "harness" : "template");
  });

  // ───────── 提示框 ─────────
  const tip = $("tip");
  function showTip(ev, html) {
    tip.innerHTML = html; tip.hidden = false;
    tip.style.left = Math.min(window.innerWidth - tip.offsetWidth - 8, ev.clientX + 12) + "px";
    tip.style.top = Math.max(8, ev.clientY - tip.offsetHeight - 12) + "px";
  }
  function hideTip() { tip.hidden = true; }

  function every(ms, fn) {
    let timer = null;
    const tick = async () => { if (!document.hidden) await fn(); timer = setTimeout(tick, ms); };
    document.addEventListener("visibilitychange", () => { if (!document.hidden) { clearTimeout(timer); tick(); } });
    return tick;
  }
  (async () => { await pollHome(); every(8000, pollHome)(); every(8000, pollRun)(); })();
})();
