// 本页订阅共享 JPEG 流；帧身份/采集时间与当前采集状态分开显示。
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const IDLE_MS = 15 * 60 * 1000;
  const MODE = { live: "采样中", held: "设备占用 · 暂缓", blocked: "设备屏障 · 暂缓", unknown: "采集状态未知", deferred: "未获设备 lease · 暂缓", paused: "采集已暂停", error: "采集出错", idle: "未在采集" };
  const S = { serial: new URLSearchParams(location.search).get("serial"), ctl: null, statusCtl: null, url: null,
    times: [], active: Date.now(), devs: [], policy: null, frameKey: null, frameAt: null, poll: false,
    statusError: "尚未取得采集状态", statusAt: null, connection: "", timer: 0,
    scope: new URLSearchParams(location.search).get("scope") === "device" ? "device" : "global",
    deviceBinding: null, target: null, bindingBusy: false, bindingError: "", revision: 0 };
  const enc = new TextEncoder(), dec = new TextDecoder(), SEP = enc.encode("\r\n\r\n");
  const positive = (value) => value != null && value !== "" && Number.isFinite(+value) && +value > 0 ? +value : null;
  function liveURL(kind, serial = S.serial, binding = S.deviceBinding, extra = {}) {
    const query = new URLSearchParams({...extra, ...(serial ? {serial} : {}), ...(binding ? {device_binding: JSON.stringify(binding)} : {})});
    return `/dash/api/live/${kind}?${query}`;
  }
  function bindingProblem() {
    if (S.scope !== "device") return "";
    if (S.policy?.device_binding_v1 !== true) return "节点尚未声明按登记设备观看能力，不会回落全域。";
    return S.deviceBinding?.serial === S.serial ? "" : S.bindingError || "先读取当前序列号的登记绑定，再显式观看；设备目录未采样不阻止此操作。";
  }
  function renderSelection() {
    $("live-resolve").hidden = S.scope !== "device";
    $("live-resolve").disabled = S.bindingBusy || !S.serial || !HGTarget.canWrite() || S.policy?.device_binding_v1 !== true;
    $("live-watch").disabled = S.bindingBusy || !S.serial || !HGTarget.canWrite() || !!bindingProblem();
    $("live-binding-info").textContent = S.bindingBusy ? "正在读取宿主登记…" : bindingProblem() || (S.deviceBinding ? `观看固定登记设备 ${S.deviceBinding.device_id} · revision ${S.deviceBinding.registry_revision}；重连不重新解析绑定。` : "兼容全域观看；与设备任务全域互斥。按登记设备观看可与其他设备任务并行。");
    $("live-binding-json").hidden = !S.deviceBinding; $("live-binding-json").textContent = S.deviceBinding ? JSON.stringify(S.deviceBinding, null, 2) : "";
  }
  function clearView() {
    stop(); const ctl = S.statusCtl; S.statusCtl = null; ctl?.abort();
    if (S.url) URL.revokeObjectURL(S.url);
    S.url = null; S.frameKey = null; S.frameAt = null; S.poll = false; S.times = []; S.connection = "";
    $("v-img").hidden = true; $("v-img").removeAttribute("src");
  }
  async function resolveBinding() {
    if ($("live-resolve").disabled) return;
    clearView(); S.deviceBinding = null; S.bindingError = ""; S.bindingBusy = true;
    const revision = ++S.revision, serial = S.serial, target = HGTarget.binding(); renderView();
    try {
      const response = await HGTarget.fetch(`/dash/api/node/devices/resolve?serial=${encodeURIComponent(serial)}`, {credentials:"same-origin",cache:"no-store"}, {target});
      const value = await response.json();
      if (!response.ok) throw new Error(value.error?.message || value.error || `HTTP ${response.status}`);
      if (revision !== S.revision) return;
      if (value.node_id !== target.node_id || value.binding?.schema !== "phonefarm/device-binding@1" || value.binding.serial !== serial) throw new Error("登记绑定与原节点或序列号不一致");
      S.deviceBinding = Object.freeze({...value.binding}); S.target = target;
    } catch (error) { if (revision === S.revision) S.bindingError = error.message; }
    finally { if (revision === S.revision) { S.bindingBusy = false; renderView(); refresh(); } }
  }

  function find(buf, pat) {
    outer: for (let i = 0; i <= buf.length - pat.length; i++) {
      for (let j = 0; j < pat.length; j++) if (buf[i + j] !== pat[j]) continue outer;
      return i;
    }
    return -1;
  }

  function show(bytes, seqValue, atValue, signal) {
    if (signal.aborted || S.ctl?.signal !== signal) return;
    const seq = positive(seqValue), at = positive(atValue), key = seq != null && at != null ? `${seq}:${at}` : null;
    S.connection = "";
    if (key && key === S.frameKey) { renderView(); return; } // 保活重复帧不刷新年龄或新帧计数。
    const url = URL.createObjectURL(new Blob([bytes], { type: "image/jpeg" })), old = S.url;
    const img = $("v-img");
    img.onload = () => { if (old) URL.revokeObjectURL(old); };
    img.src = url; img.hidden = false; S.url = url;
    S.frameKey = key; S.frameAt = at;
    if (key) S.times.push(performance.now());
    renderView();
  }

  async function runStream(serial, signal, binding, target) {
    const r = await HGTarget.fetch(liveURL("stream", serial, binding), { signal, credentials: "same-origin", cache: "no-store" }, {target});
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || `HTTP ${r.status}`);
    const reader = r.body.getReader();
    let buf = new Uint8Array(0), got = false;
    const firstTimer = setTimeout(() => { if (!got && !signal.aborted) { S.poll = true; reader.cancel().catch(() => {}); } }, 8000);
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done || signal.aborted) return;
        const nb = new Uint8Array(buf.length + value.length); nb.set(buf); nb.set(value, buf.length); buf = nb;
        for (;;) {
          const h = find(buf, SEP);
          if (h < 0) break;
          const head = dec.decode(buf.subarray(0, h)), m = /Content-Length:\s*(\d+)/i.exec(head);
          if (!m) { buf = buf.subarray(h + 4); continue; }
          const len = +m[1], start = h + 4;
          if (buf.length < start + len + 2) break;
          got = true;
          show(buf.slice(start, start + len), (/X-Frame-Seq:\s*(\d+)/i.exec(head) || [])[1], (/X-Frame-At:\s*([\d.]+)/i.exec(head) || [])[1], signal);
          buf = buf.slice(start + len + 2);
        }
        if (buf.length > 8 << 20) buf = new Uint8Array(0);
      }
    } finally { clearTimeout(firstTimer); }
  }

  async function runPoll(serial, signal, binding, target) {
    let after = 0;
    for (;;) {
      const r = await HGTarget.fetch(liveURL("frame", serial, binding, {after}), { signal, credentials: "same-origin", cache: "no-store" }, {target});
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || `HTTP ${r.status}`);
      after = +(r.headers.get("X-Frame-Seq") || after);
      show(new Uint8Array(await r.arrayBuffer()), r.headers.get("X-Frame-Seq"), r.headers.get("X-Frame-At"), signal);
      if (signal.aborted) return;
    }
  }

  function canRun() { return S.serial && !bindingProblem() && document.visibilityState === "visible" && Date.now() - S.active < IDLE_MS; }
  function stop() { const ctl = S.ctl; S.ctl = null; if (ctl) ctl.abort(); }
  async function play() {
    stop();
    if (!canRun()) { renderView(); return; }
    const ctl = new AbortController(), serial = S.serial, binding = S.deviceBinding; S.ctl = ctl;
    try { await HGTarget.ready(); } catch (error) { if (S.ctl === ctl) { S.connection = error.message; stop(); renderView(); } return; }
    if (S.ctl !== ctl) return;
    const target = S.target || HGTarget.binding(); S.target = target;
    S.connection = "正在连接；是否可采样以节点当前状态为准。";
    renderView();
    let fails = 0;
    while (S.ctl === ctl) {
      try {
        if (S.poll) await runPoll(serial, ctl.signal, binding, target); else await runStream(serial, ctl.signal, binding, target);
        fails = 0;
      } catch (e) {
        if (S.ctl !== ctl || e.name === "AbortError") return;
        if (e.target) { S.connection=e.message; stop(); renderView(); return; }
        fails++;
        S.connection = `画面连接中断：${e.message}，${Math.min(10, fails * 2)} 秒后重试。`;
        renderView();
        await new Promise((ok) => setTimeout(ok, Math.min(10, fails * 2) * 1000));
      }
      if (S.ctl === ctl && !canRun()) { stop(); renderView(); return; }
    }
  }

  function select(serial, autoplay = true) {
    clearView(); S.revision++; S.deviceBinding = null; S.bindingBusy = false; S.bindingError = "";
    S.serial = serial; S.active = Date.now(); $("live-serial").value = serial || "";
    history.replaceState(null, "", HGTarget.url(`/dash/live?serial=${encodeURIComponent(serial || "")}${S.scope === "device" ? "&scope=device" : ""}`));
    renderDevs(); renderView(); refresh(); if (autoplay && S.scope === "global") play();
  }

  function renderDevs() {
    $("devs").innerHTML = S.devs.map((d) => {
      const off = d.state !== "device" && S.scope !== "device", mode = S.statusError ? "unknown" : d.mode;
      return `<button class="dev${d.serial === S.serial ? " sel" : ""}" type="button" data-s="${esc(d.serial)}"${off ? " disabled" : ""}>
        <span class="m">${esc(d.model || d.serial)}</span><span class="s">${esc(d.serial)} · ${off ? esc(d.state) : esc(MODE[mode] || "采集状态未知")}${d.viewers ? ` · ${d.viewers} 人在看` : ""}</span></button>`;
    }).join("") || `<p class="muted">${S.statusError ? "设备目录尚未确认" : "当前目录没有 adb 设备"}</p>`;
  }

  function renderView() {
    renderSelection();
    const d = S.devs.find((x) => x.serial === S.serial);
    $("v-name").textContent = d ? `${d.model || ""} ${d.serial}`.trim() : (S.serial || "未选择手机");
    const mode = S.statusError || !d || !MODE[d.mode] ? "unknown" : d.mode;
    const b = $("v-badge"); b.hidden = !S.serial; b.className = `badge ${mode}`; b.textContent = MODE[mode];
    const now = performance.now(); S.times = S.times.filter(t => now - t <= 3000);
    const age = S.frameAt == null ? null : Math.max(0, Math.floor(Date.now() / 1000 - S.frameAt));
    const frame = !S.url ? "尚无画面" : age == null ? "当前画面未提供采集时间" : `原帧采集于 ${new Date(S.frameAt * 1000).toLocaleTimeString()} · 约 ${age} 秒前`;
    const rate = !S.statusError && Number.isFinite(d?.fps) ? `节点采样 ${d.fps} 帧/秒` : "节点采样速率未知";
    $("v-stats").textContent = S.serial ? `${frame} · 本页近 3 秒 ${S.times.length} 个带序号的新帧 · ${rate}（非游戏 FPS）${S.poll ? " · 逐帧接收" : ""}` : "";
    const paused = S.serial && !S.ctl;
    $("v-resume").hidden = !paused; $("v-resume").disabled = !HGTarget.canWrite() || !!bindingProblem();
    $("v-msg").textContent = bindingProblem() || (!S.serial ? "选择设备后观看；采样须取得实际设备 lease。" : paused ? "本页已断开观看，保留最后画面。其他订阅者的采集状态由节点决定。" : S.connection || (mode !== "live" ? `${MODE[mode]}；${S.url ? "保留最后画面" : "尚无可用画面"}。` : ""));
    $("screen").classList.toggle("stale", !!S.url && (paused || mode !== "live" || !!S.connection || age == null || age > 3));
    $("v-label").textContent = [S.statusError ? `当前状态未确认：${S.statusError}` : "", d?.label ? `节点说明：${d.label}` : "", d?.error ? `最近采集错误：${d.error}` : "", S.statusAt ? `状态读取于 ${new Date(S.statusAt).toLocaleTimeString()}` : ""].filter(Boolean).join("；");
  }

  async function refresh() {
    if (document.visibilityState !== "visible" || S.statusCtl) return;
    const ctl = new AbortController(); S.statusCtl = ctl;
    const timeout = setTimeout(() => ctl.abort(), 10000);
    try {
      const r = await HGTarget.fetch(liveURL("status"), { signal: ctl.signal, credentials: "same-origin", cache: "no-store" }, S.target ? {target:S.target} : {});
      if (r.status === 401) { location.href = "/dash/login"; return; }
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const j = await r.json();
      if (S.statusCtl !== ctl) return;
      if (!Array.isArray(j.devices)) throw new Error("设备目录格式不正确");
      S.devs = j.devices; S.policy = j.policy; S.statusError = ""; S.statusAt = Date.now();
    } catch (e) { if (S.statusCtl === ctl) S.statusError = e.name === "AbortError" ? "状态读取超时" : e.message; }
    finally {
      clearTimeout(timeout);
      if (S.statusCtl === ctl) { S.statusCtl = null; renderDevs(); renderView(); }
    }
  }
  function disconnect() {
    stop();
    const ctl = S.statusCtl; S.statusCtl = null; if (ctl) ctl.abort();
    S.statusError = "本页暂停，需重新读取当前状态"; renderView();
  }
  function startTimer() {
    if (!S.timer) S.timer = setInterval(() => {
      if (document.visibilityState !== "visible") return;
      if (S.ctl && !canRun()) stop();
      refresh(); renderView();
    }, 2000);
  }
  $("policy").textContent = "每次设备采样须取得实际 lease。占用、屏障或状态未知时保留旧图并说明原因；旧帧保活不计作新采样。页面展示的是采样与传输状态，不是游戏 FPS。";
  $("live-serial").value = S.serial || ""; $("live-scope").value = S.scope;
  $("live-serial").addEventListener("change", () => select($("live-serial").value.trim(), false));
  $("live-scope").addEventListener("change", () => { S.scope = $("live-scope").value; select($("live-serial").value.trim(), false); });
  $("live-resolve").addEventListener("click", resolveBinding);
  $("live-selection").addEventListener("submit", event => { event.preventDefault(); if (!$("live-watch").disabled) { S.active = Date.now(); refresh(); play(); } });
  $("devs").addEventListener("click", (e) => { const b = e.target.closest(".dev"); if (b && !b.disabled) select(b.dataset.s); });
  $("v-resume").addEventListener("click", () => { S.active = Date.now(); refresh(); play(); });
  for (const ev of ["pointerdown", "keydown", "touchstart", "wheel"]) addEventListener(ev, () => { S.active = Date.now(); }, { passive: true });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") { S.active = Date.now(); refresh(); if (S.serial && !S.ctl) play(); } else disconnect();
  });
  addEventListener("pagehide", () => { disconnect(); clearInterval(S.timer); S.timer = 0; });
  addEventListener("pageshow", () => { startTimer(); refresh(); if (S.serial && !S.ctl) play(); });
  document.addEventListener("hgamey-target-change", () => { if(!HGTarget.canWrite())stop(); renderView(); });
  refresh(); startTimer();
  if (S.serial) play();
})();
