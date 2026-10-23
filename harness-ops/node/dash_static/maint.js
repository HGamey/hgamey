// HGamey 实时看板：纯静态页面，轮询节点上的只读 JSON 接口。
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const num = (v, d = 1) => (v == null || Number.isNaN(+v) ? "—" : (+v).toFixed(d));
  const ago = (s) => s == null ? "—" : s < 60 ? `${Math.round(s)} 秒前` : s < 3600 ? `${Math.round(s / 60)} 分钟前` : `${(s / 3600).toFixed(1)} 小时前`;
  const dur = (s) => s < 60 ? `${Math.round(s)}s` : s < 3600 ? `${Math.floor(s / 60)}m${String(Math.round(s % 60)).padStart(2, "0")}s` : `${Math.floor(s / 3600)}h${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}m`;
  const hm = (t) => new Date(t * 1000).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });

  const S = { state: null, exps: [], expsVer: "", selJob: null, userPickedJob: false, selExp: null, userPickedExp: false, logText: "", fails: 0, shotAt: 0 };

  async function getJSON(url) {
    const r = await fetch(url, { cache: "no-store", credentials: "same-origin" });
    if (r.status === 401) { location.href = "/dash/login"; throw new Error("401"); }
    if (!r.ok) throw new Error(r.status);
    return r.json();
  }

  // ───────── 状态（3 秒） ─────────
  async function pollState() {
    try {
      const st = await getJSON("/dash/api/state");
      S.state = st; S.fails = 0;
      renderState(st);
      setLive(true, st);
    } catch (e) { S.fails++; setLive(false); }
  }

  function setLive(ok, st) {
    const el = $("live");
    el.className = "live " + (ok ? "ok" : "bad");
    $("live-text").textContent = ok ? `实时 · ${hm(st.now)}` : "连不上节点";
  }

  function mainDevice(st) { return (st.devices || []).find((d) => d.online) || (st.devices || [])[0]; }

  function renderState(st) {
    const L = st.lock || {}, d = mainDevice(st);
    const blocked = L.lease?.status === "blocked", unknown = L.status === "unknown" || L.lease?.status === "unknown";
    const held = L.status === "held" && !blocked && !unknown;
    const free = L.status === "free" && !blocked && !unknown;
    // 顶栏锁状态
    const pill = $("lock-pill");
    pill.className = "pill " + (free ? "free" : "held");
    pill.textContent = blocked ? "设备租约需要恢复" : held ? (L.stale ? "心跳已过期，需核查租约状态" : "设备锁占用") : free ? "锁记录空闲" : "设备锁身份或状态未知";
    $("dev-name").textContent = d ? `${d.brand || ""} ${d.model || d.serial}`.trim() : "没有设备";

    // 手机
    if (d) {
      $("t-online").innerHTML = `<span class="dot ${d.online ? "on" : "off"}"></span>${d.online ? "在线" : esc(d.state)}`;
      $("t-os").textContent = [d.os, d.root ? "root" : null].filter(Boolean).join(" · ");
      const b = d.battery || {}, t = d.thermal || {};
      $("t-bat").textContent = b.level_pct == null ? "—" : `${b.level_pct}%`;
      const bst = { Charging: "充电中", Discharging: "放电中", "Not charging": "未充电", Full: "已充满" }[b.status] || b.status || "";
      $("t-batst").textContent = [bst, b.on_battery === true ? "电池供电" : b.on_battery === false ? "外部供电" : null].filter(Boolean).join(" · ");
      $("t-soc").textContent = t.soc_max_c == null ? "—" : `${num(t.soc_max_c)}°C`;
      $("t-socz").textContent = t.soc_zone || "";
      $("t-gpu").textContent = `${t.gpu_c == null ? "—" : num(t.gpu_c) + "°"} / ${b.temp_c == null ? (t.battery_c == null ? "—" : num(t.battery_c) + "°") : num(b.temp_c) + "°"}`;
      const f = d.fan || {};
      $("t-fan").textContent = f.supported ? `风扇 ${f.enabled ? "开" : "关"}${f.speed_level != null ? " · 档 " + f.speed_level : ""}` : "";
      $("t-fg").textContent = d.foreground || (d.probe_depth === "light" ? "（本次快照未读取）" : "桌面 / 未知");
      const pc = $("probe");
      pc.textContent = d.probe_depth === "light" ? "轻量读取" : d.probe_depth === "full" ? "完整读取" : "离线";
      pc.title = d.probe_depth === "light" ? "本次为轻量快照，不代表当前设备空闲或正在测量" : "本次快照的采样方式，不代表当前设备可用性";
    } else {
      $("t-online").innerHTML = `<span class="dot off"></span>没看到手机`;
    }
    const notes = [];
    if (st.fleet_age_s != null) notes.push(`手机数据采于 ${ago(st.fleet_age_s)}`);
    if (st.fleet_skip) notes.push(st.fleet_skip);
    if (st.fleet_err) notes.push(st.fleet_err);
    if (st.fleet_age_s == null && !st.fleet_err) notes.push("第一次读取中…");
    $("phone-note").textContent = notes.join(" · ");
    renderSpark(st.hist || []);

    // 设备锁
    if (blocked || unknown || !held && !free) {
      $("lock-holder").innerHTML = `${blocked ? "需要恢复" : "身份或状态未知"}<span class="s">${esc(L.lease?.error || L.error || "保持阻断；标签不授予持锁资格")}${L.held_by ? ` · 标签：${esc(L.held_by)}` : ""}</span>`;
    } else if (L.held_by) {
      const since = L.since ? `从 ${L.since.slice(11) || L.since} 起` : "";
      $("lock-holder").innerHTML = `标签：${esc(L.held_by)}<span class="s">${esc(since)} · 心跳距今 ${L.heartbeat_age_s ?? "?"} 秒；标签仅展示${L.stale ? " · 心跳已过期，需核查租约状态" : ""}</span>`;
    } else if (free) {
      $("lock-holder").innerHTML = `空闲<span class="s">${L.free_for_s != null ? "已空闲 " + dur(L.free_for_s) : ""}</span>`;
    } else $("lock-holder").textContent = "设备锁占用，未记录展示标签";
    $("lock-age").textContent = "";
    $("queue").innerHTML = (L.queue || []).length ? L.queue.map((q) => `<li>${esc(q)}</li>`).join("") : `<li class="empty">没人排队</li>`;
    $("lock-events").innerHTML = (L.recent || []).map((e) =>
      `<li><span class="t">${esc(e.at.slice(11))}</span><span class="a-${esc(e.action)}">${{ acquire: "获取锁", release: "释放锁", steal: "接管记录" }[e.action] || esc(e.action)}</span><b>${esc(e.label)}</b></li>`).join("");

    // 画面
    const box = document.querySelector(".shot-box");
    if (st.shot) {
      if (st.shot.at !== S.shotAt) { S.shotAt = st.shot.at; $("shot").src = `/dash/api/shot?t=${st.shot.at}`; }
      $("shot").hidden = false; $("shot-empty").hidden = true;
      $("shot-age").textContent = `截于 ${ago(st.shot.age_s)}`;
    } else { $("shot").hidden = true; $("shot-empty").hidden = false; $("shot-age").textContent = ""; }
    $("shot-veil").hidden = free;
    box.classList.toggle("stale", !free);
    const P = st.policy || {};
    $("shot-rule").textContent = `锁空闲满 ${P.free_grace_s} 秒才允许截图，最多每 ${P.shot_every_s} 秒一张；占用、排队或待核查时暂缓。`;

    renderJobs(st);
  }

  // ───────── SoC 温度走势（单序列，持锁时段打底色） ─────────
  function renderSpark(hist) {
    const svg = $("spark");
    const now = Date.now() / 1000, t0 = now - 1800;
    const pts = hist.filter((h) => h.t >= t0 && h.soc != null);
    if (pts.length < 2) { svg.innerHTML = `<text x="150" y="38" text-anchor="middle" font-size="11" fill="var(--muted)">数据积累中（页面开着时才读）</text>`; return; }
    const W = 300, H = 70, pad = 4;
    let lo = Math.min(...pts.map((p) => p.soc)), hi = Math.max(...pts.map((p) => p.soc));
    if (hi - lo < 4) { const m = (hi + lo) / 2; lo = m - 2; hi = m + 2; }
    const x = (t) => ((t - t0) / 1800) * W, y = (v) => H - pad - ((v - lo) / (hi - lo)) * (H - 2 * pad);
    let bands = "";
    for (let i = 0; i < pts.length; i++) if (pts[i].held) {
      const x1 = x(pts[i].t), x2 = i + 1 < pts.length ? x(pts[i + 1].t) : x1 + 3;
      bands += `<rect x="${x1.toFixed(1)}" y="0" width="${Math.max(1, x2 - x1).toFixed(1)}" height="${H}" fill="var(--held)"/>`;
    }
    const d = pts.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.soc).toFixed(1)}`).join("");
    const last = pts[pts.length - 1];
    svg.innerHTML = `${bands}<line x1="0" x2="${W}" y1="${H - 0.5}" y2="${H - 0.5}" stroke="var(--axis)" stroke-width="1"/>
      <text x="2" y="10" font-size="9" fill="var(--muted)">${hi.toFixed(0)}°</text><text x="2" y="${H - 6}" font-size="9" fill="var(--muted)">${lo.toFixed(0)}°</text>
      <path d="${d}" fill="none" stroke="var(--series)" stroke-width="2" vector-effect="non-scaling-stroke" stroke-linejoin="round"/>
      <circle cx="${x(last.t).toFixed(1)}" cy="${y(last.soc).toFixed(1)}" r="3" fill="var(--series)" stroke="var(--card)" stroke-width="1.5" vector-effect="non-scaling-stroke"/>`;
    svg.onmousemove = (ev) => {
      const r = svg.getBoundingClientRect(), t = t0 + ((ev.clientX - r.left) / r.width) * 1800;
      let best = pts[0]; for (const p of pts) if (Math.abs(p.t - t) < Math.abs(best.t - t)) best = p;
      showTip(ev, `${hm(best.t)}<br>芯片 ${num(best.soc)}°C · 显卡 ${num(best.gpu)}°C<br>电量 ${best.bat ?? "—"}% · 电池 ${num(best.bat_c)}°C${best.held ? "<br>有人在用手机" : ""}`);
    };
    svg.onmouseleave = hideTip;
  }

  // ───────── 任务 ─────────
  function renderJobs(st) {
    const jobs = st.jobs || [], L = st.lock || {};
    const associated = new Set((L.lease?.entries || []).map(entry => entry.job_id).filter(Boolean));
    const running = jobs.filter((j) => j.state === "running"), done = jobs.filter((j) => j.state !== "running").slice(0, 12);
    $("jobs-count").textContent = `${running.length} 个在跑`;
    if (!S.userPickedJob || !jobs.find((j) => j.id === S.selJob)) {
      const pick = running.find((j) => associated.has(j.id)) || running[0] || done[0];
      S.selJob = pick ? pick.id : null;
    }
    const row = (j) => {
      const now = st.now, el = j.state === "running" ? now - j.started : (j.ended || now) - j.started;
      const review = j.phase === "interrupted" || j.outcome === "interrupted" || j.recovery?.status === "required";
      const stc = review ? "lost" : j.state === "running" ? "running" : j.state === "lost" ? "lost" : j.exit === 0 ? "ok" : "fail";
      const stt = review ? "待核查" : j.state === "running" ? "在跑" : j.state === "lost" ? "失联" : Number.isInteger(j.exit) ? `命令已退出 · ${j.exit}` : "退出码未知";
      const lockTag = associated.has(j.id) ? `<span class="st lockholder" title="lease 记录明确关联此任务；不代表正在测量或恢复完成">关联租约记录</span>` : "";
      return `<li class="job ${j.id === S.selJob ? "sel" : ""}" data-id="${esc(j.id)}" tabindex="0">
        <div class="row1"><span class="lbl">${esc(j.label || "—")}</span><span class="st ${stc}">${stt}</span>${lockTag}
        <span class="when">${j.state === "running" ? "已跑 " + dur(el) : hm(j.ended || j.started) + " · " + dur(el)}</span></div>
        <div class="cmd">${esc(j.bin)} ${esc((j.argv || []).join(" "))}</div></li>`;
    };
    $("jobs").innerHTML = (running.length ? running.map(row).join("") : `<li class="empty">现在没有任务在跑</li>`) +
      (done.length ? `<li class="sep">最近结束</li>` + done.map(row).join("") : "");
    const j = jobs.find((x) => x.id === S.selJob);
    $("log-title").textContent = j ? `${j.label || ""} · ${j.bin} ${(j.argv || [])[0] || ""} · ${j.id}` : "";
  }
  $("jobs").addEventListener("click", (ev) => {
    const li = ev.target.closest(".job"); if (!li) return;
    S.selJob = li.dataset.id; S.userPickedJob = true; S.logText = "";
    if (S.state) renderJobs(S.state);
    pollLog();
  });
  $("jobs").addEventListener("keydown", (ev) => { if (ev.key === "Enter") ev.target.click(); });

  // ───────── 日志（2 秒） ─────────
  let logSize = -1, logJob = null;
  async function pollLog() {
    if (!S.selJob) return;
    const j = (S.state?.jobs || []).find((x) => x.id === S.selJob);
    if (j && j.id === logJob && j.log_bytes === logSize) return; // 没变化不拉
    try {
      const r = await getJSON(`/dash/api/log?job=${encodeURIComponent(S.selJob)}&bytes=24000`);
      if (r.error) { $("log").textContent = r.error; return; }
      logJob = r.job; logSize = r.size;
      const box = $("log"), atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
      box.textContent = r.text || "（还没有输出）";
      if ($("follow").checked || atBottom) box.scrollTop = box.scrollHeight;
    } catch (e) { /* 下一轮再试 */ }
  }

  // ───────── 实验（10 秒） ─────────
  const OUT = { win: ["有效", "✓"], lose: ["淘汰", "✗"], void: ["没测准", "∅"], measuring: ["正在测", "◐"], pending: ["待测", "…"], other: ["其他", "?"] };
  const MET = [
    { k: "fps", name: "平均帧率", unit: "fps", better: 1 },
    { k: "frame_p95", name: "卡顿程度", unit: "ms", better: -1 },
    { k: "power", name: "整机功耗", unit: "W", better: -1 },
  ];

  async function pollExps() {
    try {
      const r = await getJSON(`/dash/api/exps?ver=${encodeURIComponent(S.expsVer)}`);
      if (r.same) return;
      S.expsVer = r.ver; S.exps = r.exps || [];
      renderExps();
    } catch (e) { }
  }

  function renderExps() {
    const sel = $("exp-sel"), exps = S.exps;
    if (!exps.length) {
      sel.innerHTML = ""; sel.hidden = true;
      $("exp-head").innerHTML = `<div class="empty">手机节点上还没有实验记录（只看最近 3 天和最近的任务）。</div>`;
      $("exp-chart").innerHTML = ""; $("exp-gens").innerHTML = ""; return;
    }
    sel.hidden = false;
    if (!S.userPickedExp || !exps.find((e) => e.id === S.selExp)) S.selExp = (exps.find((e) => e.running) || exps[0]).id;
    sel.innerHTML = exps.map((e) => `<option value="${esc(e.id)}" ${e.id === S.selExp ? "selected" : ""}>${e.running ? "● " : ""}${esc(e.title)}${e.job ? " · " + esc(e.job.label || "") : ""} · ${hm(e.updated)}</option>`).join("");
    renderExp(exps.find((e) => e.id === S.selExp));
  }
  $("exp-sel").addEventListener("change", (ev) => { S.selExp = ev.target.value; S.userPickedExp = true; renderExps(); });

  function renderExp(e) {
    const m = e.meta || {}, c = e.counts || {};
    const counts = ["win", "lose", "void", "measuring", "pending"].filter((k) => c[k]).map((k) => `<span class="oc ${k}"><i>${OUT[k][1]}</i>${OUT[k][0]} ${c[k]}</span>`).join("");
    const bits = [
      e.running ? `<span class="st running">进行中</span>` : m.done ? `<span class="st ok">已完成</span>` : "",
      e.job ? `任务 ${esc(e.job.label || "")} · ${esc(e.job.id)}` : "",
      m.device ? `设备 ${esc(m.device)}` : "", m.proposer ? `出方案：${esc(m.proposer)}` : "",
      `<span title="${esc(e.path)}">${esc(e.path.split("/").slice(-2).join("/"))}</span>`,
    ].filter(Boolean).map((b) => `<span class="meta">${b}</span>`).join("");
    const layers = (m.layers || []).map((l) => `<span class="chip" title="${esc(l.reason || "")}">${esc(l.name)}${l.used ? "" : "（未用）"}</span>`).join(" ");
    $("exp-head").innerHTML = `<div class="headline">${esc(m.headline || (e.running ? "实验进行中，每一代的方案和结论会陆续出现" : e.title))}</div>
      <div class="counts">${counts}</div>${bits}${layers ? `<span class="meta">${layers}</span>` : ""}
      ${m.rule ? `<span class="meta">怎样算有效：${esc(m.rule)}</span>` : ""}${m.error ? `<span class="meta">解析出错：${esc(m.error)}</span>` : ""}`;
    renderChart(e.cands || []);
    renderGens(e);
  }

  // 三张小图：每个候选相对对照臂的「改善 %」（向上 = 更好），颜色 = 判官结论
  function renderChart(cands) {
    const box = $("exp-chart");
    const have = MET.filter((mt) => cands.some((c) => c.metrics?.[mt.k]?.pct != null));
    if (!have.length) { box.innerHTML = ""; return; }
    box.innerHTML = have.map((mt) => `<div class="mchart"><h3>${mt.name} 改善 %</h3><div class="hint">向上 = 更好 · 每根柱 = 一个方案</div><svg data-k="${mt.k}" viewBox="0 0 320 130" preserveAspectRatio="none"></svg></div>`).join("");
    for (const mt of have) {
      const svg = box.querySelector(`svg[data-k="${mt.k}"]`);
      const vals = cands.map((c) => { const v = c.metrics?.[mt.k]; return v?.pct == null ? null : v.pct * mt.better; });
      const ext = Math.max(2, ...vals.filter((v) => v != null).map(Math.abs));
      const W = 320, H = 130, top = 8, bot = 18, mid = top + (H - top - bot) / 2, sc = (H - top - bot) / 2 / ext;
      const n = cands.length, slot = W / Math.max(n, 1), bw = Math.max(3, Math.min(22, slot - 2));
      let g = `<line x1="0" x2="${W}" y1="${mid}" y2="${mid}" stroke="var(--axis)" stroke-width="1" vector-effect="non-scaling-stroke"/>
        <text x="2" y="${top + 8}" font-size="9" fill="var(--muted)">+${ext.toFixed(1)}%</text><text x="2" y="${H - bot - 2}" font-size="9" fill="var(--muted)">−${ext.toFixed(1)}%</text>`;
      let lastGen = null;
      cands.forEach((c, i) => {
        const x = i * slot + (slot - bw) / 2;
        if (c.gen !== lastGen) {
          if (i) g += `<line x1="${i * slot}" x2="${i * slot}" y1="${top}" y2="${H - bot}" stroke="var(--grid)" stroke-dasharray="2 2" vector-effect="non-scaling-stroke"/>`;
          g += `<text x="${i * slot + 2}" y="${H - 4}" font-size="9" fill="var(--muted)">第${esc(c.gen ?? "?")}代</text>`;
          lastGen = c.gen;
        }
        const v = vals[i];
        const col = { win: "var(--good)", lose: "var(--bad)", void: "var(--void)", measuring: "var(--acc)" }[c.outcome] || "var(--muted)";
        if (v == null) { g += `<rect class="bar" data-i="${i}" x="${x}" y="${mid - 1}" width="${bw}" height="2" fill="var(--grid)"/>`; return; }
        const h = Math.max(2, Math.abs(v) * sc), y = v >= 0 ? mid - h : mid;
        g += `<rect class="bar" data-i="${i}" x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${bw.toFixed(1)}" height="${h.toFixed(1)}" rx="2" fill="${col}" ${c.outcome === "void" ? 'fill-opacity=".45"' : ""}/>`;
      });
      svg.innerHTML = g;
      svg.onmousemove = (ev) => {
        const i = Math.floor(((ev.clientX - svg.getBoundingClientRect().left) / svg.getBoundingClientRect().width) * n);
        const c = cands[i]; if (!c) return hideTip();
        const v = c.metrics?.[mt.k] || {};
        showTip(ev, `<b>${esc(c.id)}</b> · ${OUT[c.outcome]?.[0] || ""}<br>${esc(c.change).slice(0, 120)}<br>${mt.name}：${num(v.a, 2)} → ${num(v.b, 2)} ${mt.unit}（${v.pct == null ? "—" : (v.pct > 0 ? "+" : "") + num(v.pct, 2) + "%"}${v.p != null ? "，" + (v.p < 0.05 ? "差别可靠" : "可能只是波动") + "（p=" + num(v.p, 3) + "）" : ""}）`);
      };
      svg.onmouseleave = hideTip;
    }
  }

  function metricTable(c) {
    const rows = MET.map((mt) => {
      const v = c.metrics?.[mt.k];
      if (!v || (v.a == null && v.b == null && v.pct == null)) {
        const ab = c.abs?.[mt.k];
        return ab != null ? `<tr><td>${mt.name}</td><td>—</td><td>${num(ab, 2)}</td><td>—</td><td>—</td></tr>` : "";
      }
      const good = v.pct != null && v.pct * mt.better > 0, sig = v.sig === true || (v.p != null && v.p < 0.05);
      const cls = v.pct == null ? "" : (sig ? (good ? "better" : "worse") : "") + (sig ? " sig" : "");
      return `<tr title="${esc(v.status || "")}"><td>${mt.name}</td><td>${num(v.a, 2)}</td><td>${num(v.b, 2)}</td><td class="${cls}">${v.pct == null ? "—" : (v.pct > 0 ? "+" : "") + num(v.pct, 1) + "%"}</td><td title="${v.p == null ? "" : "p=" + num(v.p, 3)}">${v.p == null ? "—" : v.p < 0.05 ? "可靠" : "像是波动"}</td></tr>`;
    }).join("");
    const temp = c.temp_max_c != null ? `<tr><td>最高温度</td><td></td><td>${num(c.temp_max_c)}°C</td><td></td><td></td></tr>` : "";
    if (!rows && !temp) return "";
    return `<table class="mt"><thead><tr><th>指标</th><th title="对照臂">改前</th><th title="方案臂">改后</th><th>变化</th><th title="统计检验 p 值：小于 0.05 算可靠">可靠吗</th></tr></thead><tbody>${rows}${temp}</tbody></table>`;
  }

  function renderGens(e) {
    const groups = new Map();
    for (const c of e.cands || []) { const k = c.gen ?? "?"; if (!groups.has(k)) groups.set(k, []); groups.get(k).push(c); }
    for (const k of Object.keys(e.gens || {})) if (!groups.has(+k) && !groups.has(k)) groups.set(+k || k, []);
    if (!groups.size) { $("exp-gens").innerHTML = `<div class="empty">${e.running ? "第一代方案还在出…" : "这个实验没有方案记录"}</div>`; return; }
    const keys = [...groups.keys()].sort((a, b) => (+b || 0) - (+a || 0)); // 最新一代在上
    $("exp-gens").innerHTML = keys.map((k) => {
      const gm = (e.gens || {})[String(k)] || {}, cs = groups.get(k);
      const rej = (gm.rejected || []).length ? `<details class="rejected"><summary>没通过检查、没上手机的 ${gm.rejected.length} 个</summary>${gm.rejected.map((r) => `<div>${esc(r)}</div>`).join("")}</details>` : "";
      return `<div class="gen"><h3>第 ${esc(k)} 代 <span class="muted">${gm.origin ? "出方案的是：" + esc(gm.origin) : ""}${gm.proposed != null ? ` · 提了 ${gm.proposed} 个` : ""}${gm.error ? " · 出岔子：" + esc(gm.error) : ""}</span></h3>
        <div class="cands">${cs.map((c) => `<div class="cand ${c.outcome}">
          <div class="row1"><span class="cid">${esc(c.id)}</span>${c.layer ? `<span class="chip">${esc(c.layer)}</span>` : ""}<span class="badge ${c.outcome}">${OUT[c.outcome]?.[1] || ""} ${OUT[c.outcome]?.[0] || esc(c.status)}${c.status && !["PASS", "KEEP", "REJECT", "ABORT"].includes(c.status) ? " · " + esc(c.status) : ""}</span></div>
          ${c.why ? `<div class="why">${esc(c.why)}</div>` : ""}
          ${c.change ? `<div class="chg">${esc(c.change)}</div>` : ""}
          ${metricTable(c)}
          ${c.reason ? `<div class="reason">结论：${esc(c.reason)}${c.p != null ? `（<span class="rel" title="p=${num(c.p, 3)}">${c.p < 0.05 ? "差别可靠" : "可能只是波动"}</span>）` : ""}</div>` : ""}
        </div>`).join("") || `<div class="empty">这一代没有通过检查的方案</div>`}</div>${rej}</div>`;
    }).join("");
  }

  // ───────── 提示框 ─────────
  const tip = $("tip");
  function showTip(ev, html) {
    tip.innerHTML = html; tip.hidden = false;
    const w = tip.offsetWidth, h = tip.offsetHeight;
    tip.style.left = Math.min(window.innerWidth - w - 8, ev.clientX + 12) + "px";
    tip.style.top = Math.max(8, ev.clientY - h - 12) + "px";
  }
  function hideTip() { tip.hidden = true; }

  // ───────── 轮询调度：页面在后台时停 ─────────
  function every(ms, fn) {
    let timer = null;
    const tick = async () => { if (!document.hidden) await fn(); timer = setTimeout(tick, ms); };
    tick();
    document.addEventListener("visibilitychange", () => { if (!document.hidden) { clearTimeout(timer); tick(); } });
  }
  every(6000, pollState);
  every(4000, pollLog);
  every(30000, pollExps);
})();
