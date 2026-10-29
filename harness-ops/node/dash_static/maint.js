// HGamey 维护页：30 分钟芯片温度曲线与实时日志。
(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const num = (v, d = 1) => v == null || Number.isNaN(+v) ? "—" : (+v).toFixed(d);
  const ago = s => s == null ? "—" : s < 60 ? `${Math.round(s)} 秒前` : s < 3600 ? `${Math.round(s / 60)} 分钟前` : `${(s / 3600).toFixed(1)} 小时前`;
  const hm = t => new Date(t * 1000).toLocaleTimeString("zh-CN", {hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false});
  const S = {state: null, selJob: null, fails: 0};

  async function getJSON(url) {
    const response = await HGTarget.fetch(url, {cache: "no-store", credentials: "same-origin"});
    if (response.status === 401) { location.href = "/dash/login"; throw new Error("401"); }
    if (!response.ok) throw new Error(response.status);
    return response.json();
  }

  async function pollState() {
    try {
      const state = await getJSON("/dash/api/state");
      S.state = state; S.fails = 0;
      renderState(state); setLive(true, state);
    } catch (_) { S.fails++; setLive(false); }
  }

  function setLive(ok, state) {
    $("live").className = "live " + (ok ? "ok" : "bad");
    $("live-text").textContent = ok ? `实时 · ${hm(state.now)}` : "连不上节点";
  }

  function renderState(state) {
    const notes = [];
    if (state.fleet_age_s != null) notes.push(`手机数据采于 ${ago(state.fleet_age_s)}`);
    if (state.fleet_skip) notes.push(state.fleet_skip);
    if (state.fleet_err) notes.push(state.fleet_err);
    if (state.fleet_age_s == null && !state.fleet_err) notes.push("第一次读取中…");
    $("phone-note").textContent = notes.join(" · ");
    renderSpark(state.hist || []);
    const jobs = state.jobs || [];
    const selected = jobs.find(job => ["running", "preparing", "queued"].includes(job.state)) || jobs[0];
    S.selJob = selected ? selected.id : null;
    $("log-title").textContent = selected ? `${selected.label || ""} · ${selected.bin} ${(selected.argv || [])[0] || ""} · ${selected.id}` : "";
  }

  function renderSpark(hist) {
    const svg = $("spark"), now = Date.now() / 1000, t0 = now - 1800;
    const pts = hist.filter(h => h.t >= t0 && h.soc != null);
    if (pts.length < 2) { svg.innerHTML = `<text x="150" y="38" text-anchor="middle" font-size="11" fill="var(--muted)">数据积累中（页面开着时才读）</text>`; return; }
    const W = 300, H = 70, pad = 4;
    let lo = Math.min(...pts.map(p => p.soc)), hi = Math.max(...pts.map(p => p.soc));
    if (hi - lo < 4) { const mid = (hi + lo) / 2; lo = mid - 2; hi = mid + 2; }
    const x = t => ((t - t0) / 1800) * W, y = v => H - pad - ((v - lo) / (hi - lo)) * (H - 2 * pad);
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
    svg.onmousemove = ev => {
      const rect = svg.getBoundingClientRect(), t = t0 + ((ev.clientX - rect.left) / rect.width) * 1800;
      let best = pts[0]; for (const point of pts) if (Math.abs(point.t - t) < Math.abs(best.t - t)) best = point;
      showTip(ev, `${hm(best.t)}<br>芯片 ${num(best.soc)}°C · 显卡 ${num(best.gpu)}°C<br>电量 ${best.bat ?? "—"}% · 电池 ${num(best.bat_c)}°C${best.held ? "<br>有人在用手机" : ""}`);
    };
    svg.onmouseleave = hideTip;
  }

  let logSize = -1, logJob = null;
  async function pollLog() {
    if (!S.selJob) return;
    const job = (S.state?.jobs || []).find(item => item.id === S.selJob);
    if (job && job.id === logJob && job.log_bytes === logSize) return;
    try {
      const response = await getJSON(`/dash/api/log?job=${encodeURIComponent(S.selJob)}&bytes=24000`);
      if (response.error) { $("log").textContent = response.error; return; }
      logJob = response.job; logSize = response.size;
      const box = $("log"), atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
      box.textContent = response.text || "（还没有输出）";
      if ($("follow").checked || atBottom) box.scrollTop = box.scrollHeight;
    } catch (_) { /* The next poll retries the same job. */ }
  }

  const tip = $("tip");
  function showTip(ev, html) {
    tip.innerHTML = html; tip.hidden = false;
    const width = tip.offsetWidth, height = tip.offsetHeight;
    tip.style.left = Math.min(window.innerWidth - width - 8, ev.clientX + 12) + "px";
    tip.style.top = Math.max(8, ev.clientY - height - 12) + "px";
  }
  function hideTip() { tip.hidden = true; }

  function every(ms, fn) {
    let timer = null;
    const tick = async () => { if (!document.hidden) await fn(); timer = setTimeout(tick, ms); };
    tick();
    document.addEventListener("visibilitychange", () => { if (!document.hidden) { clearTimeout(timer); tick(); } });
    return timer;
  }
  const timers = [every(6000, pollState), every(4000, pollLog)];
  window.addEventListener("pagehide", () => { for (const timer of timers) clearTimeout(timer); });
})();
