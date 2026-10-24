// 把实时画面嵌进别的页面 (远程调试控制台)：实现 control.js 约定的
// window.HGLive.attach(wrap, serial, devSize) → {el, size(), detach()}。实时帧是缩放过的 (长边 960),
// size() 必须给设备像素: devSize() 给自然方向的设备尺寸 (wm size), 按当前帧横竖决定是否交换宽高。流协议与 live.js 相同:
// /dash/api/live/stream 的 multipart JPEG (每帧带 Content-Length), 流 8 秒不出帧就改逐帧拉 /frame。
(() => {
  "use strict";
  const enc = new TextEncoder(), dec = new TextDecoder();
  const SEP = enc.encode("\r\n\r\n");

  function find(buf, pat) {
    outer: for (let i = 0; i <= buf.length - pat.length; i++) {
      for (let j = 0; j < pat.length; j++) if (buf[i + j] !== pat[j]) continue outer;
      return i;
    }
    return -1;
  }

  function attach(wrap, serial, devSize) {
    const img = document.createElement("img");
    img.className = "hg-live-img";
    img.alt = "实时画面";
    img.draggable = false;
    const badge = document.createElement("div");
    badge.className = "hg-live-badge";
    wrap.appendChild(img);
    wrap.appendChild(badge);
    const st = { ctl: new AbortController(), url: null, poll: false, w: 0, h: 0 };

    function show(bytes, mode) {
      const url = URL.createObjectURL(new Blob([bytes], { type: "image/jpeg" }));
      const old = st.url;
      img.onload = () => { st.w = img.naturalWidth; st.h = img.naturalHeight; if (old) URL.revokeObjectURL(old); };
      img.src = url; st.url = url;
      badge.textContent = mode === "held" ? "测量中 · 限速" : mode === "paused" ? "测量中 · 已暂停" : "";
      badge.hidden = !badge.textContent;
    }

    async function stream(signal) {
      const r = await fetch(`/dash/api/live/stream?serial=${encodeURIComponent(serial)}`, { signal, credentials: "same-origin", cache: "no-store" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const reader = r.body.getReader();
      let buf = new Uint8Array(0), got = false;
      const t = setTimeout(() => { if (!got) { st.poll = true; reader.cancel().catch(() => {}); } }, 8000);
      try {
        for (;;) {
          const { done, value } = await reader.read();
          if (done) return;
          const nb = new Uint8Array(buf.length + value.length); nb.set(buf); nb.set(value, buf.length); buf = nb;
          for (;;) {
            const h = find(buf, SEP);
            if (h < 0) break;
            const head = dec.decode(buf.subarray(0, h));
            const m = /Content-Length:\s*(\d+)/i.exec(head);
            if (!m) { buf = buf.subarray(h + 4); continue; }
            const len = +m[1], start = h + 4;
            if (buf.length < start + len + 2) break;
            got = true;
            show(buf.slice(start, start + len), (/X-Frame-Mode:\s*(\w+)/i.exec(head) || [])[1]);
            buf = buf.slice(start + len + 2);
          }
          if (buf.length > 8 << 20) buf = new Uint8Array(0);
        }
      } finally { clearTimeout(t); }
    }

    async function poll(signal) {
      let after = 0;
      for (;;) {
        const r = await fetch(`/dash/api/live/frame?serial=${encodeURIComponent(serial)}&after=${after}`, { signal, credentials: "same-origin", cache: "no-store" });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        after = +(r.headers.get("X-Frame-Seq") || after);
        show(new Uint8Array(await r.arrayBuffer()), r.headers.get("X-Frame-Mode"));
      }
    }

    (async () => {
      const ctl = st.ctl;
      let fails = 0;
      while (!ctl.signal.aborted) {
        try { await (st.poll ? poll(ctl.signal) : stream(ctl.signal)); fails = 0; }
        catch (e) {
          if (ctl.signal.aborted || e.name === "AbortError") return;
          fails++;
          await new Promise((ok) => setTimeout(ok, Math.min(10, fails * 2) * 1000));
        }
      }
    })();

    return {
      el: img,
      size: () => {
        const d = typeof devSize === "function" ? devSize() : null;
        if (!d || !st.w || !st.h) return null;   // 控制台会退回会话里的尺寸
        const [a, b] = d, frameLandscape = st.w > st.h;
        return frameLandscape === (a > b) ? [a, b] : [b, a];
      },
      detach: () => {
        st.ctl.abort();
        if (st.url) URL.revokeObjectURL(st.url);
        img.remove(); badge.remove();
      },
    };
  }

  window.HGLive = { attach };
})();
