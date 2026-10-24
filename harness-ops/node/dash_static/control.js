// 远程调试控制台：/dash/api/remote/*（查看密码 cookie + 操作员会话 cookie + X-HG 头）。
// 与实时画面的合同：若 window.HGLive.attach(wrap, serial) 存在，返回 {el, size(), detach()}；
// size() 给当前方向下的设备像素 [w, h]。没有实时画面时用截图兜底。
(() => {
  "use strict";
  const API = "/dash/api/remote";
  const $ = (id) => document.getElementById(id);
  const state = { op: undefined, devices: [], serial: null, session: null, live: null, size: null,
                  shotTimer: 0, shotBusy: false, beatTimer: 0, url: null };

  function msg(text, err) { const m = $("msg"); m.textContent = text || ""; m.classList.toggle("err", !!err); }

  async function call(method, path, body, raw) {
    const headers = { "X-HG": "1" };
    let payload;
    if (raw) { headers["Content-Type"] = "application/octet-stream"; payload = raw; }
    else if (method !== "GET") { headers["Content-Type"] = "application/json"; payload = JSON.stringify(body || {}); }
    const r = await fetch(API + path, { method, headers, body: payload, credentials: "same-origin" });
    if (r.status === 401) {
      const j = await r.json().catch(() => ({}));
      if (j.need_operator) { setOp(null); }
      else { location.href = "/dash/login"; }
      throw new Error(j.error || "需要登录");
    }
    const ct = r.headers.get("Content-Type") || "";
    if (!ct.startsWith("application/json")) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return { blob: await r.blob(), w: +r.headers.get("X-Device-W"), h: +r.headers.get("X-Device-H") };
    }
    const j = await r.json();
    if (!r.ok) { const e = new Error(j.error || ("HTTP " + r.status)); e.data = j; throw e; }
    return j;
  }

  function setOp(name) {
    state.op = name;
    $("op-login").hidden = !!name;
    $("op-name").hidden = !name;
    $("op-logout").hidden = !name;
    $("op-name").textContent = name ? "操作员：" + name : "";
    if (!name) msg("用节点主人签发给你的操作员令牌登录后才能接管设备。");
    render();
  }

  function holderText(d) {
    const h = d.holder;
    if (!h) return d.enabled ? "空闲" : "未对远程开放";
    if (h.kind === "remote") return (h.operator === state.op ? "我在用" : h.operator + " 在用") + "，剩 " + fmt(h.expires_in_s);
    if (h.kind === "experiment") return "实验占用：" + (h.label || "未知") + (h.since ? " 自 " + h.since : "") + (h.global ? "（全局锁）" : "");
    return "phonefarm lease 占用";
  }
  function fmt(s) { s = Math.max(0, s | 0); return (s >= 60 ? Math.floor(s / 60) + "分" : "") + (s % 60) + "秒"; }

  function render() {
    const ul = $("devices");
    ul.replaceChildren(...state.devices.map((d) => {
      const li = document.createElement("li");
      const b = document.createElement("button");
      b.type = "button";
      b.setAttribute("aria-pressed", String(d.serial === state.serial));
      const s = document.createElement("span"); s.className = "dev-serial"; s.textContent = d.serial;
      const sub = document.createElement("span"); sub.className = "dev-sub";
      sub.textContent = (d.model || d.transport) + " · " + holderText(d);
      if (d.holder && d.holder.kind === "remote" && d.holder.operator === state.op) sub.classList.add("dev-mine");
      else if (d.holder) sub.classList.add("dev-held");
      b.append(s, sub);
      b.disabled = !d.enabled;
      b.addEventListener("click", () => select(d.serial));
      li.append(b);
      return li;
    }));
    const d = state.devices.find((x) => x.serial === state.serial);
    const mine = !!(d && d.holder && d.holder.kind === "remote" && d.holder.operator === state.op);
    $("cur-serial").textContent = state.serial || "";
    $("take").disabled = !state.op || !d || !d.enabled || mine || (d.holder && d.holder.kind !== "remote");
    $("release").disabled = !mine;
    $("ops").disabled = !mine;
    $("lease-info").textContent = !d ? "先在左边选设备" : mine
      ? "已接管，剩 " + fmt(d.holder.expires_in_s) + "（最晚 " + d.holder.deadline + "，" + d.holder.heartbeat_ttl_s + "s 无操作自动释放）"
      : holderText(d);
    if (mine && !state.session) startSession(d.holder);
    if (!mine && state.session) stopSession();
  }

  async function refresh() {
    try {
      const who = await call("GET", "/whoami");
      if (who.operator !== state.op) setOp(who.operator);
      if (!state.op) { state.devices = []; render(); return; }
      const r = await call("GET", "/devices");
      state.devices = r.devices;
      if (r.lock_storage_error) msg("锁目录不可用：" + r.lock_storage_error, true);
      render();
    } catch (e) { msg(e.message, true); }
  }

  function select(serial) {
    if (state.session && state.serial !== serial) { msg("先释放当前设备再换"); return; }
    state.serial = serial; render();
  }

  function startSession(holder) {
    state.session = holder;
    const wrap = $("hg-screen-wrap");
    state.live = null;
    if (window.HGLive && typeof window.HGLive.attach === "function") {
      try { state.live = window.HGLive.attach(wrap, state.serial); } catch (e) { state.live = null; }
    }
    const target = state.live && state.live.el ? state.live.el : $("hg-screen");
    $("screen-empty").hidden = true;
    if (!state.live) { $("hg-screen").hidden = false; shotLoop(); }
    bindPointer(target);
    clearInterval(state.beatTimer);
    state.beatTimer = setInterval(() => { if (!document.hidden) call("POST", dev("renew"), {}).catch(() => {}); }, 60000);
  }

  function stopSession() {
    state.session = null;
    clearTimeout(state.shotTimer); clearInterval(state.beatTimer);
    if (state.live && state.live.detach) { try { state.live.detach(); } catch (e) { /* ignore */ } }
    state.live = null;
    $("hg-screen").hidden = true; $("screen-empty").hidden = false;
  }

  const dev = (a) => "/devices/" + encodeURIComponent(state.serial) + "/" + a;

  async function shot() {
    if (state.shotBusy || !state.session) return;
    state.shotBusy = true;
    try {
      const r = await call("GET", dev("screenshot") + "?max=900");
      if (r.w && r.h) state.size = [r.w, r.h];
      const url = URL.createObjectURL(r.blob);
      $("hg-screen").src = url;
      if (state.url) URL.revokeObjectURL(state.url);
      state.url = url;
    } catch (e) { msg("截图：" + e.message, true); }
    finally { state.shotBusy = false; }
  }
  function shotLoop() {
    clearTimeout(state.shotTimer);
    if (!state.session || state.live) return;
    const go = document.hidden ? Promise.resolve() : shot();
    go.finally(() => { state.shotTimer = setTimeout(shotLoop, 1500); });
  }

  function deviceSize() {
    if (state.live && state.live.size) { const s = state.live.size(); if (s) return s; }
    return state.size || (state.session && state.session.screen_size);
  }

  function bindPointer(el) {
    if (el.dataset.hgBound) return;
    el.dataset.hgBound = "1";
    let down = null;
    const pos = (ev) => {
      const r = el.getBoundingClientRect(); const s = deviceSize();
      if (!s || !r.width) return null;
      return [Math.round((ev.clientX - r.left) / r.width * s[0]), Math.round((ev.clientY - r.top) / r.height * s[1]),
              ev.clientX, ev.clientY];
    };
    el.addEventListener("pointerdown", (ev) => { if (!state.session) return; down = { p: pos(ev), t: Date.now() };
      el.setPointerCapture(ev.pointerId); ev.preventDefault(); });
    el.addEventListener("pointerup", (ev) => {
      if (!down || !down.p) { down = null; return; }
      const p = pos(ev); const a = down.p; const ms = Math.min(5000, Math.max(80, Date.now() - down.t)); down = null;
      if (!p) return;
      const moved = Math.hypot(p[2] - a[2], p[3] - a[3]) > 8;
      const req = moved ? call("POST", dev("swipe"), { x1: a[0], y1: a[1], x2: p[0], y2: p[1], ms })
                        : call("POST", dev("tap"), { x: a[0], y: a[1] });
      req.then(() => { if (!state.live) setTimeout(shot, 250); }).catch((e) => msg(e.message, true));
    });
  }

  function show(r) {
    if (!r) return;
    const parts = [];
    if (r.stdout) parts.push(r.stdout);
    if (r.stderr) parts.push("[stderr]\n" + r.stderr);
    parts.push("[exit " + r.exit + (r.timed_out ? "，超时" : "") + (r.truncated ? "，输出已截断" : "") + "]");
    $("out").textContent = parts.join("\n");
  }
  async function act(name, body) {
    try { const r = await call("POST", dev(name), body); show(r); if (!state.live) setTimeout(shot, 300); return r; }
    catch (e) { msg(e.message, true); }
  }

  $("op-login").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    try { const r = await call("POST", "/login", { token: $("op-token").value.trim() }); $("op-token").value = "";
      setOp(r.operator); msg("已登录"); refresh(); }
    catch (e) { msg(e.message, true); }
  });
  $("op-logout").addEventListener("click", async () => { await call("POST", "/logout", {}).catch(() => {}); stopSession(); setOp(null); });
  $("take").addEventListener("click", async () => {
    try { const r = await call("POST", dev("session"), {}); msg("已接管 " + r.serial); await refresh(); }
    catch (e) { msg(e.message, true); refresh(); }
  });
  $("release").addEventListener("click", async () => {
    try { await call("DELETE", dev("session"), {}); stopSession(); msg("已释放"); refresh(); }
    catch (e) { msg(e.message, true); }
  });
  document.querySelectorAll("[data-key]").forEach((b) => b.addEventListener("click", () => act("key", { key: b.dataset.key })));
  $("shot-now").addEventListener("click", shot);
  $("text-form").addEventListener("submit", (ev) => { ev.preventDefault(); const t = $("text").value; if (t) act("text", { text: t }).then(() => { $("text").value = ""; }); });
  $("app-form").addEventListener("submit", (ev) => { ev.preventDefault(); act("app_start", { package: $("pkg").value.trim() }); });
  $("app-stop").addEventListener("click", () => act("app_stop", { package: $("pkg").value.trim() }));
  $("shell-form").addEventListener("submit", (ev) => { ev.preventDefault(); const c = $("shell").value.trim(); if (c) { $("out").textContent = "$ " + c + "\n…"; act("shell", { cmd: c }); } });
  $("logcat").addEventListener("click", async () => {
    try { const r = await call("GET", dev("logcat") + "?lines=200"); show(r); } catch (e) { msg(e.message, true); }
  });
  $("apk").addEventListener("change", async () => {
    const f = $("apk").files[0]; if (!f) return;
    msg("正在上传并安装 " + f.name + "（" + (f.size >> 20) + " MiB）…");
    try { const r = await call("PUT", dev("install"), null, f); show(r); msg(r.exit === 0 ? "安装完成" : "安装失败", r.exit !== 0); }
    catch (e) { msg(e.message, true); }
    $("apk").value = "";
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
  refresh();
  setInterval(() => { if (!document.hidden) refresh(); }, 5000);
})();
