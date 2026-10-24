// 实时画面：fetch 读 multipart JPEG 流（每帧带 Content-Length），流不通时退回逐帧拉取。
// 只在页面可见且近期有人操作时观看；切走/空闲即断开，服务端随即停止采集。
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const IDLE_MS = 15 * 60 * 1000;   // 这么久没人操作就不再自动续流
  const MODE = { live: "实时", held: "测量中 · 限速", paused: "测量中 · 已暂停", error: "出错，重试中", idle: "未在采集" };
  const S = { serial: new URLSearchParams(location.search).get("serial"), ctl: null, seq: 0, url: null,
              times: [], active: Date.now(), devs: [], policy: null, lastFrame: 0, poll: false };

  const enc = new TextEncoder(), dec = new TextDecoder();
  const SEP = enc.encode("\r\n\r\n");

  function find(buf, pat, from) {
    outer: for (let i = from || 0; i <= buf.length - pat.length; i++) {
      for (let j = 0; j < pat.length; j++) if (buf[i + j] !== pat[j]) continue outer;
      return i;
    }
    return -1;
  }

  function show(bytes, mode) {
    const url = URL.createObjectURL(new Blob([bytes], { type: "image/jpeg" }));
    const img = $("v-img"), old = S.url;
    img.onload = () => { if (old) URL.revokeObjectURL(old); };
    img.src = url; img.hidden = false; S.url = url;
    const now = performance.now();
    S.times.push(now); while (S.times.length && now - S.times[0] > 3000) S.times.shift();
    S.lastFrame = Date.now();
    $("v-msg").textContent = "";
    $("screen").classList.toggle("stale", mode === "paused");
  }

  async function runStream(serial, signal) {
    const r = await fetch(`/dash/api/live/stream?serial=${encodeURIComponent(serial)}`, { signal, credentials: "same-origin", cache: "no-store" });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || `HTTP ${r.status}`);
    const reader = r.body.getReader();
    let buf = new Uint8Array(0), got = false;
    const firstTimer = setTimeout(() => { if (!got) { S.poll = true; reader.cancel().catch(() => {}); } }, 8000);
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) return;
        const nb = new Uint8Array(buf.length + value.length); nb.set(buf); nb.set(value, buf.length); buf = nb;
        for (;;) {
          const h = find(buf, SEP, 0);
          if (h < 0) break;
          const head = dec.decode(buf.subarray(0, h));
          const m = /Content-Length:\s*(\d+)/i.exec(head);
          if (!m) { buf = buf.subarray(h + 4); continue; }
          const len = +m[1], start = h + 4;
          if (buf.length < start + len + 2) break;
          const mode = (/X-Frame-Mode:\s*(\w+)/i.exec(head) || [])[1];
          got = true;
          show(buf.slice(start, start + len), mode);
          buf = buf.slice(start + len + 2);
        }
        if (buf.length > 8 << 20) buf = new Uint8Array(0);
      }
    } finally { clearTimeout(firstTimer); }
  }

  async function runPoll(serial, signal) {
    let after = 0;
    for (;;) {
      const r = await fetch(`/dash/api/live/frame?serial=${encodeURIComponent(serial)}&after=${after}`, { signal, credentials: "same-origin", cache: "no-store" });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || `HTTP ${r.status}`);
      after = +(r.headers.get("X-Frame-Seq") || after);
      show(new Uint8Array(await r.arrayBuffer()), r.headers.get("X-Frame-Mode"));
    }
  }

  function canRun() { return S.serial && document.visibilityState === "visible" && Date.now() - S.active < IDLE_MS; }

  async function play() {
    stop();
    if (!S.serial) return;
    if (!canRun()) { pausedUi(); return; }
    $("v-resume").hidden = true;
    const ctl = new AbortController(); S.ctl = ctl;
    $("v-msg").textContent = "连接中…（第一次打开要 1–2 秒启动采集）";
    let fails = 0;
    while (S.ctl === ctl) {
      try {
        if (S.poll) await runPoll(S.serial, ctl.signal); else await runStream(S.serial, ctl.signal);
        fails = 0;  // 服务端到时关流（或流 8 秒没出帧，已改逐帧拉取）：可见且有人在才续
      } catch (e) {
        if (S.ctl !== ctl) return;
        if (e.name === "AbortError") return;
        fails++;
        $("v-msg").textContent = `连接中断：${e.message}，${Math.min(10, fails * 2)} 秒后重试`;
        await new Promise((ok) => setTimeout(ok, Math.min(10, fails * 2) * 1000));
      }
      if (S.ctl === ctl && !canRun()) { stop(); pausedUi(); return; }
    }
  }

  function stop() { if (S.ctl) { const c = S.ctl; S.ctl = null; c.abort(); } }
  function pausedUi() { if (S.serial) { $("v-resume").hidden = false; $("v-msg").textContent = "已暂停观看（页面在后台或长时间无操作），服务端已停止采集"; } }

  function select(serial) {
    S.serial = serial; S.poll = false; S.times = []; S.active = Date.now();
    history.replaceState(null, "", `/dash/live?serial=${encodeURIComponent(serial)}`);
    renderDevs(); play();
  }

  function renderDevs() {
    $("devs").innerHTML = S.devs.map((d) => {
      const off = d.state !== "device";
      return `<button class="dev${d.serial === S.serial ? " sel" : ""}" type="button" data-s="${esc(d.serial)}"${off ? " disabled" : ""}>
        <span class="m">${esc(d.model || d.serial)}</span><span class="s">${esc(d.serial)} · ${off ? esc(d.state) : esc(MODE[d.mode] || d.mode)}${d.viewers ? ` · ${d.viewers} 人在看` : ""}</span></button>`;
    }).join("") || `<p class="muted">没有接入的 adb 设备</p>`;
  }

  function renderView() {
    const d = S.devs.find((x) => x.serial === S.serial);
    $("v-name").textContent = d ? `${d.model || ""} ${d.serial}`.trim() : (S.serial || "未选择手机");
    const b = $("v-badge");
    if (!d || !S.ctl) { b.hidden = true; $("v-stats").textContent = ""; $("v-label").textContent = ""; return; }
    b.hidden = false; b.className = `badge ${d.mode}`;
    const p = S.policy || {};
    b.textContent = d.mode === "held" ? `测量中 · 限速 ≤${p.held_fps} fps` : (MODE[d.mode] || d.mode);
    const fps = S.times.length > 1 ? ((S.times.length - 1) * 1000 / (S.times[S.times.length - 1] - S.times[0])).toFixed(1) : "0";
    const age = S.lastFrame ? Math.round((Date.now() - S.lastFrame) / 1000) : null;
    $("v-stats").textContent = `本页 ${fps} fps · 采集 ${d.fps ?? 0} fps${age != null && age > 3 ? ` · 画面 ${age} 秒未变` : ""}${S.poll ? " · 逐帧拉取" : ""}`;
    $("v-label").textContent = d.mode === "held" || d.mode === "paused"
      ? `设备被「${d.label || "测量"}」占用：为不干扰测量，停用视频编码，只低频截图${d.mode === "paused" ? "（当前配置为暂停，显示最后一帧）" : ""}。`
      : d.error ? `采集出错：${d.error}` : "";
  }

  async function refresh() {
    try {
      const r = await fetch("/dash/api/live/status", { credentials: "same-origin", cache: "no-store" });
      if (r.status === 401) { location.href = "/dash/login"; return; }
      const j = await r.json();
      S.devs = j.devices || []; S.policy = j.policy;
      const p = j.policy || {};
      $("policy").textContent = `空闲时 ≤${p.live_fps} fps、长边 ${p.live_side}px；设备被测量占用时停用编码器，改为 ≤${p.held_fps} fps、长边 ${p.held_side}px 的截图（0 = 暂停）；锁释放后 ${p.free_grace_s} 秒才恢复全速。没人看就不采集。`;
      renderDevs(); renderView();
    } catch (e) { /* 下次再试 */ }
  }

  $("devs").addEventListener("click", (e) => { const b = e.target.closest(".dev"); if (b && !b.disabled) select(b.dataset.s); });
  $("v-resume").addEventListener("click", () => { S.active = Date.now(); play(); });
  for (const ev of ["pointerdown", "keydown", "touchstart", "wheel"]) addEventListener(ev, () => { S.active = Date.now(); }, { passive: true });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") { S.active = Date.now(); if (S.serial && !S.ctl) play(); } else { stop(); pausedUi(); }
  });
  refresh(); setInterval(refresh, 2000);
  if (S.serial) play();
})();
