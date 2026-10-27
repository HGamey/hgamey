// HGamey 工作台外壳（2026-10-28 重构）：统一 5-tab 导航，固定尺寸不跳动，active 中性灰 pill。
// 每页引入：在 <body> 末尾 <script src="/dash/static/shell.js"></script>；外壳替换旧 header.top。
(function () {
  const TABS = [
    ["/dash", "main", "总览"],
    ["/dash/fleet", "device", "设备"],
    ["/dash/jobs", "task", "任务"],
    ["/dash/research", "agent", "Agent"],
    ["/dash/maint", "option", "设置"],
  ];
  const path = location.pathname.replace(/\/+$/, "") || "/dash";
  function current(p) {
    if (p === "/dash") return path === "/dash" || path === "/dash/";
    return path === p || path.startsWith(p + "/");
  }
  function build() {
    const old = document.querySelector("header.top");
    const sub = old?.querySelector(".brand .sub")?.textContent || "";
    // 收割旧顶栏动作区（如 home 的 live/干预/新目标），并入外壳右侧
    const actions = old ? Array.from(old.querySelector(".top-right")?.children || [])
        .filter(el => el.tagName !== "FORM" || !el.querySelector('form[action="/dash/logout"]')) : [];
    const top = document.createElement("header");
    top.className = "shell-top";
    const brand = document.createElement("a");
    brand.className = "shell-brand";
    brand.href = "/dash";
    brand.innerHTML = `<span class="shell-mark">HG</span><span class="shell-name">HGamey</span><span class="shell-node" id="shell-node"></span>`;
    const tabs = document.createElement("nav");
    tabs.className = "shell-tabs";
    tabs.setAttribute("aria-label", "主导航");
    for (const [href, key, label] of TABS) {
      const a = document.createElement("a");
      a.className = "shell-tab";
      a.href = href;
      a.textContent = label;
      a.dataset.tab = key;
      if (current(href)) a.setAttribute("aria-current", "page");
      tabs.append(a);
    }
    const right = document.createElement("div");
    right.className = "shell-right";
    const hasLive = actions.some(el => el.id === "live");
    right.innerHTML = `${hasLive ? "" : '<span class="shell-clock" id="shell-clock"></span>'}
      <a class="shell-tab" id="shell-logout" href="/dash/logout" style="min-width:0">退出</a>`;
    const actBox = document.createElement("div");
    actBox.className = "shell-right";
    actBox.style.marginRight = "-4px";
    for (const el of actions) {
      if (el.matches('form[action="/dash/logout"]')) continue;
      actBox.append(el);
    }
    top.append(brand, tabs, actBox, right);
    if (old) old.replaceWith(top); else document.body.prepend(top);
    // 时钟（每 30s 刷新）
    const clock = top.querySelector("#shell-clock");
    if (clock) {
      const tick = () => { const d = new Date(); clock.textContent =
        `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`; };
      tick(); setInterval(tick, 30000);
    }
    // 节点名（异步，不阻塞渲染）
    fetch("/dash/api/node").then(r => r.ok ? r.json() : null).then(d => {
      if (d && d.host) top.querySelector("#shell-node").textContent = "· " + d.host.hostname;
    }).catch(() => {});
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", build);
  else build();
})();
