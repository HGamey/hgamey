// HGamey 实时看板首页：「现在」一行 + 手机 + 目标 + 发生了什么 + 掌舵。纯静态，轮询节点上的 JSON 接口。
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const num = (v, d = 1) => (v == null || Number.isNaN(+v) ? "—" : (+v).toFixed(d));
  const toDate = (t) => typeof t === "number" ? new Date(t * 1000) : new Date(t);
  const hm = (t) => { const d = toDate(t); return isNaN(d) ? "" : d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false }); };
  const dt = (t) => { const d = toDate(t); return isNaN(d) ? "" : d.toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false }); };
  const minsAgo = (t) => { const d = toDate(t); return isNaN(d) ? null : Math.max(0, Math.round((Date.now() - d) / 60000)); };

  // 包名 → 大家叫的名字（标题里不出现包名）
  const GAMES = { "com.miHoYo.Yuanshen": "原神", "com.miHoYo.GenshinImpact": "原神", "com.unity.megacity.metro": "Megacity", "com.miHoYo.hkrpg": "星穹铁道",
    "com.miHoYo.Nap": "绝区零", "com.tencent.tmgp.sgame": "王者荣耀", "com.tencent.tmgp.pubgmhd": "和平精英" };
  const gameName = (pkg) => GAMES[pkg] || (pkg ? pkg.split(".").pop() : "游戏");
  // 指标：名字、越低越好还是越高越好、变化时的说法
  const M = {
    power_watt: { name: "整机耗电", lower: true, short: "耗电", dn: "降", up: "升", goal: "省电" },
    frame_p95: { name: "卡顿程度", lower: true, short: "卡顿", dn: "减轻", up: "加重", goal: "减少卡顿", tip: "最慢的 5% 画面帧要多少毫秒（帧时 p95），越小越不卡" },
    fps_mean: { name: "平均帧率", lower: false, short: "帧率", dn: "降", up: "升", goal: "提高帧率" },
    gpu_busy_ms: { name: "显卡忙碌时间", lower: true, short: "显卡忙碌", dn: "降", up: "升", goal: "减轻显卡负担" },
  };
  const ALIAS = { power_w_mean: "power_watt", fps_p95_ms: "frame_p95", frame_p95_ms: "frame_p95" };
  const mkey = (k) => ALIAS[k] || k;
  const minfo = (k) => M[mkey(k)] || { name: k, lower: true, short: k, dn: "降", up: "升", goal: k };
  const reliable = (p) => p != null && p < 0.05;
  const relWord = (p) => p == null ? "" : `<span class="rel" title="统计检验 p=${num(p, 3)}（越小越可靠，小于 0.05 算可靠）">${reliable(p) ? "可靠" : "可能只是波动"}</span>`;
  // 「耗电降 3.1%」「卡顿加重 4.2%」：方向词按指标好坏说，可靠时上色
  function change(k, d, p) {
    if (d == null) return "";
    const m = minfo(k), better = m.lower ? d < 0 : d > 0;
    const word = d === 0 ? "没变" : `${d < 0 ? m.dn : m.up} ${Math.abs(d).toFixed(1)}%`;
    const cls = reliable(p) ? (better ? "good" : "bad") : "";
    return `${m.tip ? `<span title="${esc(m.tip)}">${esc(m.short)}</span>` : esc(m.short)}<span class="${cls}">${word}</span>${p != null ? `（${relWord(p)}）` : ""}`;
  }
  const LAYER = { sysparam: "改系统设置", gray: "精简画面步骤", upop: "改画面算法", shader: "改画面算法" };
  const STOP = { target_reached: "达到目标了", budget_exhausted: "预算用完了", no_gain: "连续几代都没有收益，这条路走不通", human_stop: "你叫停的", error: "出错停下了" };
  const WHO = { judge: "判官测试", onboard: "接入新游戏", core: "核心开发", frame: "画面分析", speed: "提速", open: "开放接口", pair: "结对调试", deploy: "部署" };
  const who = (label) => !label ? "别人" : label.startsWith("goal-") ? "另一个目标" : (WHO[label] ? `${WHO[label]}（${label}）` : label);

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

  // ───────── goal.md 里要用的几项 ─────────
  function parseGoal(md) {
    const g = { game: null, metric: null, direction: null, target: null, maxGen: null, budget: null };
    if (!md) return g;
    const fm = (md.match(/^---\s*\n([\s\S]*?)\n---/) || [])[1] || "";
    g.game = (fm.match(/^game:\s*(\S+)/m) || [])[1] || null;
    const obj = (fm.match(/objective:\s*\n((?:[ \t]+.*\n?)+)/) || [])[1] || "";
    g.metric = (obj.match(/metric:\s*([\w.]+)/) || [])[1] || null;
    g.direction = (obj.match(/direction:\s*(\w+)/) || [])[1] || null;
    const t = (obj.match(/target_pct:\s*([\d.]+)/) || [])[1]; g.target = t ? +t : null;
    const mg = (fm.match(/max_generations:\s*(\d+)/) || [])[1]; g.maxGen = mg ? +mg : null;
    const wm = (fm.match(/wall_minutes:\s*(\d+)/) || [])[1]; g.budget = wm ? +wm : null;
    return g;
  }
  function runInfo(r) {
    const st = (r && r.state) || {}, g = parseGoal(r && r.goal_md);
    const game = g.game || ((st.goal_summary || "").match(/^([\w.]+\.[\w.]+)/) || [])[1];
    const metric = mkey((st.best && st.best.metric) || g.metric || "power_watt");
    const m = minfo(metric);
    const lower = g.direction ? g.direction === "lower" : m.lower;
    return { st, g, game, gname: gameName(game), metric, m, lower, target: g.target,
      short: `${gameName(game)} ${m.goal}`, title: `${gameName(game)}：${m.name}${lower ? "降" : "提高"} ${g.target ?? "?"}%` };
  }

  // 每个方案：提出时的理由、上机、结论
  function candidates(events) {
    const map = new Map();
    const get = (id, gen) => { if (!map.has(id)) map.set(id, { id, gen, layer: null, why: "", verdict: null, metrics: {}, measuring: null, t: null }); return map.get(id); };
    for (const e of events || []) {
      const id = e.candidate_id; if (!id) continue;
      const c = get(id, e.gen);
      if (e.type === "propose") { c.layer = e.layer; c.why = e.why || ""; c.changes = e.changes; }
      if (e.type === "measure") { c.measuring = e; c.t = e.t; }
      if (e.type === "verdict") { c.verdict = e.verdict; c.metrics = e.metrics || {}; c.measuring = null; c.reason = e.reason; }
      if (e.type === "note" && /撤下|不上机|拦下/.test(e.text || e.message || "")) c.dropped = true;
    }
    return [...map.values()];
  }
  const shortWhy = (c) => { const w = (c && c.why || "").split(/[：:，,。；;]/)[0].trim(); return w ? (w.length > 18 ? w.slice(0, 18) + "…" : w) : (c ? c.id : ""); };
  // 系统设置（节点名）→ 大白话；原名放悬浮提示
  const CORE = (n) => n === 0 ? "小核" : n === 7 ? "大核" : "中核";
  function knobName(k) {
    let m;
    if ((m = k.match(/^gpu\.(max|min)_pwrlevel$/))) return `GPU ${m[1] === "max" ? "最高" : "最低"}频率`;
    if ((m = k.match(/^cpu\.policy(\d+)\.scaling_(max|min)_freq$/))) return `${CORE(+m[1])} CPU ${m[2] === "max" ? "最高" : "最低"}频率`;
    if ((m = k.match(/^cpu\.policy(\d+)\.scaling_governor$/))) return `${CORE(+m[1])} CPU 调度方式`;
    if (/^bus\.DDR\./.test(k)) return "内存频率下限";
    if (/^bus\.LLCC\./.test(k)) return "缓存频率下限";
    if (/^gpu[.:]/.test(k)) return "GPU 相关设置";
    if (/^cpu[.:]/.test(k)) return "CPU 相关设置";
    if (/^bus[.:]/.test(k)) return "内存总线设置";
    return "一项系统设置";
  }
  // 自由文本（备注、理由、harness 的理解）里的方案编号和节点名换成中文名，原文进悬浮提示
  function humanize(text, cmap) {
    // 先把要换的东西换成占位符，最后再填回 HTML —— 免得后面的替换改到悬浮提示里的原文
    const parts = [];
    const hold = (html) => `\u0000${parts.push(html) - 1}\u0000`;
    let t = String(text || "");
    const ids = [...(cmap ? cmap.keys() : [])].sort((a, b) => b.length - a.length);
    for (const id of ids) {
      const c = cmap.get(id);
      t = t.split(id).join(hold(`方案<span class="rel" title="${esc(id)}${c.why ? "：" + esc(c.why) : ""}">「${esc(shortWhy(c))}」</span>`));
    }
    t = t.replace(/\b(?:sys|gray|shader)_g\d+c\d+\b/g, (id) => hold(`<span class="rel" title="${esc(id)}">一个方案</span>`));
    t = t.replace(/\b(?:gpu|cpu|bus|kgsl)(?:\.[A-Za-z0-9_]+)+/g, (k) => hold(`<span class="rel" title="${esc(k)}">${esc(knobName(k))}</span>`));
    t = t.replace(/\b(sysparam|gray)\b/g, (w) => hold(esc(LAYER[w])));
    t = t.replace(/goal\.md 已起草[,，]\s*等人确认/g, "目标草稿写好了，等你确认").replace(/\bgoal\.md\b/g, () => hold(`<span class="rel" title="goal.md">目标设置</span>`))
      .replace(/\bhuman\.md\b/g, () => hold(`<span class="rel" title="human.md">你的指令</span>`));
    return esc(t).replace(/\u0000(\d+)\u0000/g, (_, i) => parts[+i]);
  }
  const candName = (c) => `<b class="cn" title="${esc(c.id)}${c.why ? "：" + esc(c.why) : ""}">「${esc(shortWhy(c))}」</b>`;

  // ───────── 首页数据 ─────────
  async function pollHome() {
    try {
      const h = await api("/dash/api/home");
      S.home = h;
      $("live").className = "live ok"; $("live-text").textContent = `实时 · ${hm(h.now)}`;
      renderPhones(h); renderSteerBtn(h); renderRunSel(h); renderNow();
    } catch (e) { $("live").className = "live bad"; $("live-text").textContent = "连不上手机节点"; }
  }

  function renderSteerBtn(h) {
    const b = $("steer-btn");
    b.textContent = h.steer ? "掌舵：已解锁" : "掌舵：未解锁";
    b.title = h.steer ? "点一下重新上锁" : "输入掌舵密码";
    $("steer-lock").hidden = !!h.steer;
  }

  function renderPhones(h) {
    const on = h.slots.filter(Boolean), free = h.slots.length - on.length;
    $("phones").innerHTML = on.map((s) => {
      const cls = !s.online ? "off" : s.busy ? "busy" : "on";
      const st = !s.online ? (s.state || "离线") : s.busy ? "在线 · 正在测量" : "在线 · 空闲";
      const thumb = s.shot_at ? `<img alt="${esc(s.model || s.serial)} 的画面" src="/dash/api/shot?serial=${encodeURIComponent(s.serial)}&t=${s.shot_at}">`
        : `<div class="ph">${s.busy ? "测量中不截图" : "暂无画面"}</div>`;
      const doing = s.goal ? `<div class="doing on">▶ 正在跑：${esc(s.goal.summary)}</div>`
        : s.waiting_goal ? `<div class="doing">⏳ 目标在排队，${esc(who(s.waiting_goal.holder))}正在用这台手机</div>`
        : `<div class="doing">${s.busy ? `${esc(who(h.lock && h.lock.held_by))}正在用这台手机` : "空着，可以跑新目标"}</div>`;
      return `<div class="phone-card"><div class="thumb">${thumb}</div><div class="meta">
        <div class="name" title="${esc(s.serial)}">${esc(s.model || "未知型号")}</div>
        <span class="status ${cls}"><i></i>${esc(st)}</span>
        <div class="nums"><div><b>${s.soc_c == null ? "—" : num(s.soc_c, 0) + "°"}</b><span>芯片温度</span></div>
          <div><b>${s.battery_pct == null ? "—" : s.battery_pct + "%"}</b><span>电量</span></div>
          <div><b>${s.battery_c == null ? "—" : num(s.battery_c, 0) + "°"}</b><span>电池温度</span></div></div>
        ${doing}</div></div>`;
    }).join("") || `<p class="phones-more">${h.fleet_age_s == null ? (h.fleet_err ? "读不到手机：" + esc(h.fleet_err) : "正在读手机状态…") : "现在没有手机接入"}</p>`;
    $("phones-more").textContent = free > 0 ? `还可以接 ${free} 台手机` : "";
  }

  function renderRunSel(h) {
    const runs = h.runs || [];
    const sel = $("run-sel");
    if (!runs.length) {
      sel.hidden = true; S.runId = null; S.run = null;
      $("run-status").textContent = "还没有目标"; $("run-status").className = "spill"; $("run-demo").hidden = true; $("run-when").textContent = "";
      $("run-body").innerHTML = `<p class="run-title">还没有目标</p><p class="run-intent">点右上角「＋ 新目标」，用一句话告诉 harness 你想要什么。</p>`;
      $("tl").innerHTML = ""; return;
    }
    if (!S.userPicked || !runs.find((r) => r.run_id === S.runId)) S.runId = runs[0].run_id;
    sel.hidden = runs.length < 2;
    sel.innerHTML = runs.map((r) => `<option value="${esc(r.run_id)}" ${r.run_id === S.runId ? "selected" : ""}>${esc(r.status_zh)} · ${esc((r.intent || r.state.goal_summary || r.run_id).slice(0, 26))}</option>`).join("");
  }
  $("run-sel").addEventListener("change", (e) => { S.runId = e.target.value; S.userPicked = true; S.humanDirty = false; S.histOpen = null; pollRun(); });

  async function pollRun() {
    if (!S.runId) return;
    try {
      const r = await api(`/dash/api/run?id=${encodeURIComponent(S.runId)}`);
      if (r.run_id !== S.runId) return;
      S.run = r;
      renderRun(r); renderTimeline(r); renderSteer(r); renderNow();
    } catch (e) { }
  }

  // ───────── 「现在」一行：现状 + 下一步 ─────────
  function renderNow() {
    const h = S.home, r = S.run;
    let now = "没有目标在跑。", next = "点右上角「＋ 新目标」，用一句话告诉 harness 你想要什么。";
    if (h && r && r.run_id === S.runId) {
      const I = runInfo(r), st = I.st, name = `${I.short}目标`;
      const lock = h.lock || {}, mine = `goal-${r.run_id}`;
      const cands = candidates(r.events);
      const gen = st.generation || 0, maxGen = st.max_generations || I.g.maxGen;
      if (r.demo) { now = `（示例）${name}，第 ${gen} 代正在测「${esc(shortWhy(cands.find((c) => c.measuring) || cands[cands.length - 1]))}」。`; next = "这是示例数据，等真正的目标跑起来后这里会换成实时情况。"; }
      else if (r.status === "drafting") { now = `正在起草「${esc(I.gname)}」的目标。`; next = "草稿出来后点「查看草稿并开跑」，看一眼、改一改、确认。"; }
      else if (r.status === "awaiting_confirm") { now = `${esc(name)}的草稿等你确认。`; next = "点目标卡上的「查看草稿并开跑」，确认后 harness 去排队拿手机。"; }
      else if (r.status === "paused") { now = `${esc(name)}已暂停，手机已经让出来了。`; next = "在「掌舵」里改一句指令（比如删掉「暂停」）就会继续。"; }
      else if (r.status === "done" || r.status === "failed") {
        now = `没有目标在跑。上一个${esc(name)}${r.status === "failed" ? "出错停了" : "已经结束"}：${esc(STOP[st.stop_reason] || st.stop_reason || "")}。`;
        next = r.report_md ? "总报告在页面下方「总报告」里；也可以点「＋ 新目标」再来一个。" : "可以点「＋ 新目标」再来一个。";
      } else if (r.status === "running") {
        if (lock.held_by !== mine) {
          const pos = (lock.queue || []).indexOf(mine);
          now = `${esc(name)}在排队，等${esc(who(lock.held_by))}用完手机${pos > 0 ? `（前面还有 ${pos} 个）` : ""}。`;
          next = lock.held_by ? `拿到手机后开始第 ${Math.max(1, gen)} 代。` : "手机快空出来了，马上开始。";
        } else {
          const cur = cands.filter((c) => c.measuring).pop();
          const pending = cands.filter((c) => c.gen === gen && !c.verdict && !c.dropped && c !== cur).length;
          if (cur) {
            const m = minsAgo(cur.t);
            now = `第 ${gen}${maxGen ? ` / ${maxGen}` : ""} 代，正在测方案${candName(cur)}${m != null ? `（已测 ${m} 分钟）` : ""}。`;
            next = `测完出结论${pending ? `；这一代还剩 ${pending} 个方案` : "；这是这一代最后一个方案"}。`;
          } else {
            now = `第 ${Math.max(1, gen)}${maxGen ? ` / ${maxGen}` : ""} 代，正在出方案。`;
            next = "方案出来后逐个上手机测。";
          }
        }
      }
    } else if (h && !(h.runs || []).length) {
      now = "没有目标在跑。";
    }
    $("now-text").innerHTML = now; $("next-text").innerHTML = next ? `下一步：${next}` : "";
  }

  // ───────── 目标卡 ─────────
  function renderRun(r) {
    const I = runInfo(r), st = I.st;
    $("run-status").textContent = r.status_zh; $("run-status").className = "spill " + r.status;
    $("run-demo").hidden = !r.demo;
    $("run-when").textContent = r.updated ? `更新于 ${dt(r.updated)}` : "";
    const best = st.best;
    const improve = best && best.delta_pct != null ? (I.lower ? -best.delta_pct : best.delta_pct) : null;
    const prog = Math.max(0, Math.min(1, st.progress_to_target != null ? st.progress_to_target : improve != null && I.target ? improve / I.target : 0));
    // 最好方案的主指标差别可不可靠：从它的结论事件里找 p
    let bestP = null;
    if (best) {
      const v = (r.events || []).filter((e) => e.type === "verdict" && e.candidate_id === best.candidate).pop();
      const mm = v && v.metrics ? Object.entries(v.metrics).find(([k]) => mkey(k) === I.metric) : null;
      bestP = mm ? mm[1].p : null;
    }
    const gen = st.generation || 0, maxGen = st.max_generations || I.g.maxGen || 0;
    const used = st.elapsed_min, budget = st.budget_min || I.g.budget;
    const dir = I.lower ? "降" : "提高";
    const dots = maxGen ? Array.from({ length: maxGen }, (_, i) => `<i class="${i + 1 < gen ? "done" : i + 1 === gen ? (r.status === "running" ? "cur" : "done") : ""}"></i>`).join("") : "";
    const bestCand = best ? candidates(r.events).find((c) => c.id === best.candidate) : null;
    const why = r.status === "paused" ? `已暂停：手机已经让出，在「掌舵」里改一句指令就会继续。`
      : st.stop_reason ? `停下原因：${STOP[st.stop_reason] || st.stop_reason}${st.no_gain_streak ? `（连续 ${st.no_gain_streak} 代没有收益）` : ""}。` : "";
    const barText = r.status === "awaiting_confirm" || r.status === "drafting" ? "草稿等你确认后开跑"
      : improve == null ? `目标${dir} ${I.target ?? "?"}%，还没有有效的方案` : `目标${dir} ${I.target ?? "?"}%，目前最好${improve >= 0 ? dir : "反而变差"} ${Math.abs(improve).toFixed(1)}%`;
    $("run-body").innerHTML = `
      <h2 class="run-title" id="run-title">${esc(I.title)}</h2>
      ${r.intent ? `<p class="run-intent">原话：“${esc(r.intent)}”</p>` : ""}
      <div class="bignums">
        <div class="bn"><div class="k">目标</div><div class="v">${dir} ${I.target ?? "?"}<small>%</small></div><div class="s">${esc(I.m.name)}</div></div>
        <div class="bn"><div class="k">目前最好</div>${improve == null ? `<div class="v">—</div><div class="s">还没有有效的方案</div>`
          : `<div class="v ${improve > 0 ? "good" : ""}">${improve >= 0 ? dir : "反"} ${Math.abs(improve).toFixed(1)}<small>%</small></div><div class="s">${bestP != null ? relWord(bestP) + " · " : ""}${bestCand ? esc(shortWhy(bestCand)) : esc(best.candidate || "")}</div>`}</div>
        <div class="bn"><div class="k">已用时间</div><div class="v">${used != null ? Math.round(used) : "—"}<small> / ${budget ?? "?"} 分钟</small></div></div>
        <div class="bn"><div class="k">第几代</div><div class="v">${gen || "—"}<small> / ${maxGen || "?"} 代</small></div><div class="dots">${dots}</div></div>
      </div>
      <div class="bigbar"><div class="bt"><span>${esc(barText)}</span><b>完成 ${Math.round(prog * 100)}%</b></div>
        <div class="track" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(prog * 100)}" aria-label="离目标的进度"><i style="width:${prog * 100}%"></i></div></div>
      ${why ? `<p class="why">${esc(why)}</p>` : ""}
      ${r.status === "awaiting_confirm" && !r.demo ? `<div class="row-btns"><button class="btn primary" type="button" data-act="review">查看草稿并开跑</button></div>` : ""}
      <div class="cchart" id="cchart"></div>`;
    renderCandChart(candidates(r.events || []), I);
    $("goalmd").textContent = r.goal_md || "（还没有 goal.md）";
    $("rep").hidden = !r.report_md; if (r.report_md) $("reportmd").textContent = r.report_md;
  }

  // 每个方案在主指标上的改善（向上 = 更好）：保留 蓝 / 淘汰 琥珀 / 正在测 绿
  function renderCandChart(cands, I) {
    const box = $("cchart");
    const list = cands.filter((c) => !c.dropped || c.verdict);
    if (!list.length) { box.innerHTML = ""; return; }
    const val = (c) => { const e = Object.entries(c.metrics || {}).find(([k]) => mkey(k) === I.metric); return e && e[1].delta_pct != null ? (I.lower ? -e[1].delta_pct : e[1].delta_pct) : null; };
    const vals = list.map(val);
    const ext = Math.max(2, I.target || 0, ...vals.filter((v) => v != null).map(Math.abs)) * 1.15;
    const W = Math.max(300, Math.round(box.clientWidth || 640)), H = 190, L = 44, B = 26, T = 10, mid = T + (H - T - B) / 2, sc = (H - T - B) / 2 / ext;
    const slot = (W - L) / list.length, bw = Math.max(8, Math.min(36, slot * 0.55));
    const pctTxt = (v) => (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v).toFixed(0) + "%";
    let g = "";
    for (const v of [ext / 1.15, 0, -ext / 1.15]) {
      const y = mid - v * sc;
      g += `<line x1="${L}" x2="${W}" y1="${y}" y2="${y}" stroke="${v === 0 ? "var(--axis)" : "var(--grid)"}" stroke-width="1"/>
        <text x="${L - 6}" y="${y + 5}" font-size="14" text-anchor="end" fill="var(--muted)">${v === 0 ? "0" : pctTxt(v)}</text>`;
    }
    if (I.target) { const y = mid - I.target * sc; g += `<line x1="${L}" x2="${W}" y1="${y}" y2="${y}" stroke="var(--blue)" stroke-dasharray="5 4" stroke-width="1.5"/><text x="${W}" y="${y - 6}" font-size="14" text-anchor="end" fill="var(--blue-ink)">目标 ${I.target}%</text>`; }
    let lastGen = null;
    list.forEach((c, i) => {
      const x = L + i * slot + (slot - bw) / 2, v = vals[i];
      if (c.gen !== lastGen) { g += `<text x="${L + i * slot + 2}" y="${H - 6}" font-size="14" fill="var(--muted)">第${c.gen}代</text>`; if (i) g += `<line x1="${L + i * slot}" x2="${L + i * slot}" y1="${T}" y2="${H - B}" stroke="var(--grid)" stroke-dasharray="2 3"/>`; lastGen = c.gen; }
      const kind = c.verdict ? ({ PASS: "win", KEEP: "win", REJECT: "lose", SLOW: "lose", POOR_QUALITY: "lose", CRASH: "lose" }[c.verdict] || "void") : c.measuring ? "measuring" : "pending";
      const col = { win: "var(--blue)", lose: "var(--amber)", void: "var(--void)", measuring: "var(--teal)", pending: "var(--grid)" }[kind];
      if (v == null) {
        g += `<rect data-i="${i}" x="${x}" y="${mid - 3}" width="${bw}" height="6" rx="3" fill="${col}"/>`;
        if (kind === "measuring") g += `<text x="${x + bw / 2}" y="${mid - 10}" font-size="14" text-anchor="middle" fill="var(--teal-ink)">正在测</text>`;
      } else {
        const hh = Math.max(2, Math.abs(v) * sc), y = v >= 0 ? mid - hh : mid;
        g += `<rect data-i="${i}" x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${bw.toFixed(1)}" height="${hh.toFixed(1)}" rx="4" fill="${col}" ${kind === "void" ? 'fill-opacity=".5"' : ""}/>`;
        if (kind === "win") g += `<text x="${x + bw / 2}" y="${(v >= 0 ? y - 6 : y + hh + 16).toFixed(1)}" font-size="14" text-anchor="middle" fill="var(--ink)" font-weight="700">${pctTxt(v)}</text>`;
      }
      g += `<rect data-i="${i}" x="${L + i * slot}" y="${T}" width="${slot}" height="${H - T - B}" fill="transparent"/>`;
    });
    box.innerHTML = `<div class="cchart-h"><h3>每个方案让「${esc(I.m.name)}」改善了多少（向上 = 更好）</h3>
      <div class="legend2"><span><i style="background:var(--blue)"></i>保留</span><span><i style="background:var(--amber)"></i>淘汰</span><span><i style="background:var(--teal)"></i>正在测</span><span><i style="background:var(--void);opacity:.5"></i>没测准</span></div></div>
      <svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto" role="img" aria-label="每个方案的改善">${g}</svg>`;
    const svg = box.querySelector("svg");
    svg.addEventListener("mousemove", (ev) => {
      const i = ev.target.dataset && ev.target.dataset.i; if (i == null) return hideTip();
      const c = list[+i], ms = Object.entries(c.metrics || {}).map(([k, m]) => change(k, m.delta_pct, m.p)).join("<br>");
      showTip(ev, `<b>${esc(shortWhy(c))}</b>（${esc(c.id)}）<br>${esc(LAYER[c.layer] || c.layer || "")}${ms ? "<br>" + ms : c.measuring ? "<br>正在测" : "<br>等着上手机"}`);
    });
    svg.addEventListener("mouseleave", hideTip);
  }

  // ───────── 发生了什么：一行一件事 ─────────
  const fmtChanges = (c) => !c ? "" : Array.isArray(c) ? c.join(" + ") : typeof c === "object" ? Object.entries(c).map(([k, v]) => `${k}=${v}`).join(", ") : String(c);
  function line(e, cmap) {
    const c = e.candidate_id ? (cmap.get(e.candidate_id) || { id: e.candidate_id, why: "" }) : null;
    switch (e.type) {
      case "start": return ["▶", "", "开始运行"];
      case "plan": {
        if (e.text || e.summary) return ["◆", "", `定好计划：${esc(e.text || e.summary)}`];
        const ly = (e.layers || []).map((l) => LAYER[l] || l).join("、");
        return ["◆", "", `第 ${e.gen} 代开始：试${esc(ly || "—")}${e.per_generation ? `，每类 ${esc(e.per_generation)} 个方案` : ""}${e.budget_left_min != null ? `<span class="q">（还剩 ${Math.round(e.budget_left_min)} 分钟）</span>` : ""}`];
      }
      case "steer": return ["✎", "steer", `读到你的新指令：“${humanize(e.text, cmap)}”${e.interpreted ? ` <span class="q">→ ${humanize(e.interpreted, cmap)}</span>` : ""}`];
      case "propose": return ["＋", "", `提出方案<span title="${esc(fmtChanges(e.changes))}">${candName(c)}</span><span class="q">（${esc(LAYER[e.layer] || e.layer || "")}）</span>`];
      case "gate": { const ok = /pass|allow|放行|ok/i.test(String(e.decision)); return ["⛨", "", `上手机前预审${candName(c)}：${ok ? "通过" : "拦下，不上手机"}`]; }
      case "measure": return ["◐", "", e.arms_done ? `测方案${candName(c)}：${esc(e.arms_done)} / ${esc(e.arms_planned)} 轮` : `开始测方案${candName(c)}<span class="q">（改前、改后轮流测 ${esc(e.arms_planned)} 轮）</span>`];
      case "verdict": {
        const win = ["PASS", "KEEP"].includes(e.verdict), lose = ["REJECT", "SLOW", "POOR_QUALITY", "CRASH"].includes(e.verdict);
        const word = win ? "保留" : lose ? "淘汰" : e.verdict === "ABORT" ? "这次没测准，不算" : esc(e.verdict);
        const ms = Object.entries(e.metrics || {}).filter(([, m]) => m.delta_pct != null)
          .sort(([a], [b]) => (mkey(b) === runInfo(S.run).metric) - (mkey(a) === runInfo(S.run).metric))
          .map(([k, m]) => change(k, m.delta_pct, m.p)).join("，");
        return [win ? "✓" : lose ? "✗" : "∅", win ? "win" : lose ? "lose" : "", `${word}${candName(c)}${ms ? `：${ms}` : e.reason ? `：<span class="q">${humanize(e.reason, cmap)}</span>` : ""}`];
      }
      case "keep": return null; // 「保留」已经在结论那行里
      case "stop": return ["■", "", `停下了：${esc(STOP[e.reason] || e.reason || "")}${e.summary ? ` <span class="q">${humanize(e.summary, cmap)}</span>` : ""}`];
      case "error": return ["!", "lose", `出错：${humanize(e.message || e.text || e.error || "", cmap)}`];
      case "note": return ["·", "", (e.stage === "draft" ? `起草${e.by === "llm" ? "（大模型）" : e.by === "template" ? "（模板）" : ""}：` : "") + humanize(e.text || e.message || "", cmap)];
      default: return ["·", "", esc(e.type)];
    }
  }
  let tlShowAll = false;
  function renderTimeline(r) {
    const cmap = new Map(candidates(r.events).map((c) => [c.id, c]));
    let evs = (r.events || []).slice();
    const lastMeasure = new Map();
    evs.forEach((e, i) => { if (e.type === "measure") lastMeasure.set(e.candidate_id, i); });
    evs = evs.filter((e, i) => (e.type !== "measure" || lastMeasure.get(e.candidate_id) === i) && e.type !== "keep");
    evs.reverse();
    const total = evs.length;
    if (!tlShowAll) evs = evs.slice(0, 30);
    $("tl-note").textContent = total ? "最新的在上面" : "";
    if (!total) { $("tl").innerHTML = `<li class="ev2"><span></span><span class="tx">还没有发生什么</span></li>`; return; }
    let html = "", gen;
    for (const e of evs) {
      const g = e.gen ?? 0;
      if (g !== gen) { html += `<li class="gen">${g ? `第 ${g} 代` : "准备"}</li>`; gen = g; }
      const L = line(e, cmap); if (!L) continue;
      html += `<li class="ev2 ${L[1]}"><span class="ic" aria-hidden="true">${L[0]}</span><span class="tx">${L[2]}</span><time>${esc(hm(e.t))}</time></li>`;
    }
    if (total > 30) html += `<li class="tl-more"><button class="btn sm" type="button" data-act="tl-toggle">${tlShowAll ? "收起" : `显示全部 ${total} 条`}</button></li>`;
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
    if (!editable(r)) $("steer-msg").textContent = "这个目标已经结束，指令只能看不能改";
    $("hist").innerHTML = (r.human_history || []).slice(0, 12).map((h, i) =>
      `<li><span class="t">${esc(h.name.replace(/^(\d{4})(\d\d)(\d\d)-(\d\d)(\d\d)(\d\d)\.md$/, "$2-$3 $4:$5:$6"))}</span><span class="muted">第 ${(r.human_history.length - i)} 版</span>
       <button class="btn sm" type="button" data-hist="${esc(h.name)}">${S.histOpen === h.name ? "收起" : "看看"}</button></li>`).join("") || `<li><span class="muted">还没有改过</span></li>`;
    const a = (r.audit || [])[0];
    $("audit").textContent = a ? `最近一次：${dt(a.t)} 来自 ${a.ip}（${{ edit_human: "改指令", stop: "停止", new_goal: "新目标", confirm: "确认开跑" }[a.action] || a.action}）` : "";
  }
  // 例子按钮：把那句指令加到输入框末尾（不直接保存）
  document.querySelectorAll(".chip-btn").forEach((b) => b.addEventListener("click", () => {
    const ta = $("human"); if (ta.readOnly) { needSteer(); return; }
    const cur = ta.value.replace(/\s+$/, "");
    if (!cur.split(/\n/).some((l) => l.trim() === b.dataset.ins)) ta.value = (cur ? cur + "\n" : "") + b.dataset.ins + "\n";
    S.humanDirty = true; $("steer-msg").textContent = "已填进输入框，点「保存指令」生效"; ta.focus();
  }));
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
  // 图是按容器宽度画的（文字不缩放），窗口宽度变了就重画
  let rsT = null;
  window.addEventListener("resize", () => { clearTimeout(rsT); rsT = setTimeout(() => { if (S.run) renderRun(S.run); }, 150); });
  (async () => { await pollHome(); every(8000, pollHome)(); every(8000, pollRun)(); })();
})();
