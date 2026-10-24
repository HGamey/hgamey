// 嵌入共享画面；保留 control.js 的 attach → {el, size(), detach()} 接口。
// 当前状态独立读取；保活重发不改变原帧采集时间。隐藏页面只断开本订阅。
(() => {
  "use strict";
  const enc = new TextEncoder(), dec = new TextDecoder(), SEP = enc.encode("\r\n\r\n");
  const MODE = { live: "采样中", held: "设备占用 · 暂缓", blocked: "设备屏障 · 暂缓", unknown: "采集状态未知", deferred: "未获设备 lease · 暂缓", paused: "采集已暂停", error: "采集出错", idle: "未在采集" };
  const positive = (value) => value != null && value !== "" && Number.isFinite(+value) && +value > 0 ? +value : null;
  function find(buf, pat) {
    outer: for (let i = 0; i <= buf.length - pat.length; i++) {
      for (let j = 0; j < pat.length; j++) if (buf[i + j] !== pat[j]) continue outer;
      return i;
    }
    return -1;
  }

  function attach(wrap, serial, devSize) {
    const img = document.createElement("img");
    img.className = "hg-live-img"; img.alt = "设备最近一次采集的画面"; img.draggable = false;
    const badge = document.createElement("div");
    badge.className = "hg-live-badge";
    badge.style.right = "8px"; badge.style.whiteSpace = "pre-wrap"; badge.style.overflowWrap = "anywhere"; badge.style.pointerEvents = "none";
    wrap.appendChild(img); wrap.appendChild(badge);
    const st = { ctl: null, statusCtl: null, url: null, poll: false, w: 0, h: 0, frameKey: null, frameAt: null,
      device: null, statusError: "尚未取得采集状态", connection: "", detached: false, timer: 0 };

    function render() {
      const mode = st.statusError || !MODE[st.device?.mode] ? "unknown" : st.device.mode;
      const age = st.frameAt == null ? null : Math.max(0, Math.floor(Date.now() / 1000 - st.frameAt));
      const frame = !st.url ? "尚无画面" : age == null ? "当前画面未提供采集时间" : `原帧 ${new Date(st.frameAt * 1000).toLocaleTimeString()} · 约 ${age} 秒前`;
      const state = !st.ctl ? "本页已断开观看；保留旧图，其他订阅由节点管理。" : MODE[mode];
      badge.textContent = [state, frame, st.statusError || st.device?.label || "", st.connection || st.device?.error || ""].filter(Boolean).join("\n");
      badge.hidden = false;
      img.style.filter = st.url && (!st.ctl || mode !== "live" || !!st.connection || age == null || age > 3) ? "grayscale(.5) opacity(.8)" : "";
    }
    function show(bytes, seqValue, atValue, signal) {
      if (st.detached || signal.aborted || st.ctl?.signal !== signal) return;
      const seq = positive(seqValue), at = positive(atValue), key = seq != null && at != null ? `${seq}:${at}` : null;
      st.connection = "";
      if (key && key === st.frameKey) { render(); return; }
      const url = URL.createObjectURL(new Blob([bytes], { type: "image/jpeg" })), old = st.url;
      img.onload = () => { if (st.url === url && !st.detached) { st.w = img.naturalWidth; st.h = img.naturalHeight; } if (old) URL.revokeObjectURL(old); };
      img.src = url; st.url = url; st.frameKey = key; st.frameAt = at;
      render();
    }

    async function stream(signal) {
      const r = await fetch(`/dash/api/live/stream?serial=${encodeURIComponent(serial)}`, { signal, credentials: "same-origin", cache: "no-store" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const reader = r.body.getReader();
      let buf = new Uint8Array(0), got = false;
      const timer = setTimeout(() => { if (!got && !signal.aborted) { st.poll = true; reader.cancel().catch(() => {}); } }, 8000);
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
      } finally { clearTimeout(timer); }
    }
    async function poll(signal) {
      let after = 0;
      for (;;) {
        const r = await fetch(`/dash/api/live/frame?serial=${encodeURIComponent(serial)}&after=${after}`, { signal, credentials: "same-origin", cache: "no-store" });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        after = +(r.headers.get("X-Frame-Seq") || after);
        show(new Uint8Array(await r.arrayBuffer()), r.headers.get("X-Frame-Seq"), r.headers.get("X-Frame-At"), signal);
        if (signal.aborted) return;
      }
    }
    function stop() { const ctl = st.ctl; st.ctl = null; if (ctl) ctl.abort(); }
    async function play() {
      if (st.detached || document.visibilityState !== "visible" || st.ctl) return;
      const ctl = new AbortController(); st.ctl = ctl;
      st.connection = "正在连接；是否可采样以节点当前状态为准。"; render();
      let fails = 0;
      while (st.ctl === ctl && !st.detached) {
        try { await (st.poll ? poll(ctl.signal) : stream(ctl.signal)); fails = 0; }
        catch (e) {
          if (st.ctl !== ctl || ctl.signal.aborted || e.name === "AbortError") return;
          fails++;
          st.connection = `画面连接中断：${e.message}，${Math.min(10, fails * 2)} 秒后重试。`; render();
          await new Promise((ok) => setTimeout(ok, Math.min(10, fails * 2) * 1000));
        }
      }
    }
    async function refresh() {
      if (st.detached || document.visibilityState !== "visible" || st.statusCtl) return;
      const ctl = new AbortController(); st.statusCtl = ctl;
      const timeout = setTimeout(() => ctl.abort(), 10000);
      try {
        const r = await fetch("/dash/api/live/status", { signal: ctl.signal, credentials: "same-origin", cache: "no-store" });
        if (!r.ok) throw new Error(r.status === 401 ? "查看登录已失效" : `HTTP ${r.status}`);
        const result = await r.json();
        if (st.statusCtl !== ctl || st.detached) return;
        if (!Array.isArray(result.devices)) throw new Error("设备目录格式不正确");
        st.device = result.devices.find(d => d.serial === serial) || null;
        st.statusError = st.device ? "" : "当前目录未确认此设备";
      } catch (e) { if (st.statusCtl === ctl) st.statusError = e.name === "AbortError" ? "状态读取超时" : e.message; }
      finally { clearTimeout(timeout); if (st.statusCtl === ctl) { st.statusCtl = null; render(); } }
    }
    function disconnect() {
      stop();
      const ctl = st.statusCtl; st.statusCtl = null; if (ctl) ctl.abort();
      st.statusError = "本页暂停，需重新读取当前状态"; render();
    }
    function startTimer() {
      if (!st.timer && !st.detached) st.timer = setInterval(() => { if (document.visibilityState === "visible") { refresh(); render(); } }, 2000);
    }
    function visibility() { if (document.visibilityState === "visible") { refresh(); play(); } else disconnect(); }
    function hide() { disconnect(); clearInterval(st.timer); st.timer = 0; }
    function resume() { if (!st.detached) { startTimer(); visibility(); } }
    document.addEventListener("visibilitychange", visibility);
    window.addEventListener("pagehide", hide); window.addEventListener("pageshow", resume);
    startTimer(); refresh(); play(); render();

    return {
      el: img,
      size: () => {
        const d = typeof devSize === "function" ? devSize() : null;
        if (!d || !st.w || !st.h) return null;
        const [a, b] = d, frameLandscape = st.w > st.h;
        return frameLandscape === (a > b) ? [a, b] : [b, a];
      },
      detach: () => {
        st.detached = true; hide();
        document.removeEventListener("visibilitychange", visibility);
        window.removeEventListener("pagehide", hide); window.removeEventListener("pageshow", resume);
        if (st.url) URL.revokeObjectURL(st.url);
        img.remove(); badge.remove();
      },
    };
  }
  window.HGLive = { attach };
})();
