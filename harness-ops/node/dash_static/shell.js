// HGamey 工作台外壳：主导航 8 项 + 「更多」菜单；节点身份收进节点名按钮；窄屏两行，标签可横向滚动。
// 每页引入：紧跟旧 header.top 之后放 <script src="/dash/static/shell.js"></script>，外壳随首屏一起画出、替换旧顶栏；
// 执行目标框由页尾的 target.js 生成，生成时再接上。
// crypto.randomUUID 只在安全上下文（HTTPS、localhost）里有；明文入口（frp、局域网地址）上用 getRandomValues 补一个
// 标准 v4 UUID。shell.js 在每页的页面脚本之前加载，页面里照常调用 crypto.randomUUID()。
if (globalThis.crypto && typeof crypto.randomUUID !== "function") {
  crypto.randomUUID = function randomUUID() {
    const b = crypto.getRandomValues(new Uint8Array(16));
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    const h = Array.from(b, x => x.toString(16).padStart(2, "0")).join("");
    return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
  };
}
(function () {
  const TABS = [
    ["/dash", "main", "总览"],
    ["/dash/command", "command", "指挥中心"],
    ["/dash/structure", "structure", "结构"],
    ["/dash/research", "task", "任务"],
    ["/dash/research#agents-directory", "agent", "Agent"],
    ["/dash/fleet", "device", "设备"],
    ["/dash/jobs", "runs", "作业"],
    ["/dash/datasets", "data", "数据集"],
  ];
  // 「更多」按谁用分两组：看结果的在前，工程师的工具在后。
  const MORE = [
    ["/dash/results", "研究结果", "看结果"],
    ["/dash/live", "实时画面", "看结果"],
    ["/dash/games", "游戏与资源", "看结果"],
    ["/dash/gol", "GOL 目标", "工程师用"],
    ["/dash/control", "远程调试", "工程师用"],
    ["/dash/doctor", "环境诊断", "工程师用"],
    ["/dash/maint", "曲线与日志", "工程师用"],
  ];
  const path = location.pathname.replace(/\/+$/, "") || "/dash";
  function current(p) {
    if (p === "/dash") return path === "/dash" || path === "/dash/";
    if (p === "/dash/research#agents-directory") return path === "/dash/research" && location.hash === "#agents-directory";
    if (p === "/dash/research") return (path === p || path.startsWith(p + "/")) && location.hash !== "#agents-directory";
    return path === p || path.startsWith(p + "/");
  }
  // 技术细节（各种 ID、hash、命令、原始记录）默认收起：页面上标了 .tech 的地方只在「更多 → 显示技术细节」打开时出现。
  // 记在这个浏览器上；先于页面内容设好，不闪。
  const TECH = "hgamey-tech";
  const techShown = () => { try { return localStorage.getItem(TECH) === "1"; } catch (_) { return false; } };
  document.documentElement.classList.toggle("show-tech", techShown());
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };

  // A disclosure that closes on Esc, outside click and focus leaving it; Esc returns focus to its trigger.
  function disclosure(button, panel, keep) {
    const set = open => {
      button.setAttribute("aria-expanded", String(open));
      panel.hidden = !open && !(keep && keep());
    };
    button.addEventListener("click", () => set(button.getAttribute("aria-expanded") !== "true"));
    document.addEventListener("keydown", e => {
      if (e.key === "Escape" && button.getAttribute("aria-expanded") === "true") { set(false); button.focus(); }
    });
    document.addEventListener("pointerdown", e => {
      if (button.getAttribute("aria-expanded") === "true" && !panel.contains(e.target) && !button.contains(e.target)) set(false);
    });
    panel.addEventListener("focusout", e => {
      if (e.relatedTarget && !panel.contains(e.relatedTarget) && !button.contains(e.relatedTarget)) set(false);
    });
    return set;
  }

  function build() {
    const old = document.querySelector("header.top");
    // 收割旧顶栏动作区（如首页的实时、操作权限、新任务），并入外壳右侧；退出表单改放进「更多」菜单
    const actions = old ? Array.from(old.querySelector(".top-right")?.children || [])
      .filter(n => !n.matches('form[action="/dash/logout"]')) : [];
    const top = el("header", "shell-top");
    const brand = el("a", "shell-brand");
    brand.href = "/dash";
    brand.setAttribute("aria-label", "HGamey 总览");
    brand.innerHTML = '<span class="shell-mark" aria-hidden="true">HG</span><span class="shell-name">HGamey</span>';

    const nav = el("nav", "shell-nav");
    nav.setAttribute("aria-label", "主导航");
    const tabs = el("div", "shell-tabs");
    for (const [href, key, label] of TABS) {
      const a = el("a", "shell-tab", label);
      a.href = href;
      a.dataset.tab = key;
      a.dataset.href = href;
      if (current(href)) a.setAttribute("aria-current", "page");
      tabs.append(a);
    }
    // 任务 ↔ Agent 只差一个 #，切换时不重新加载页面
    window.addEventListener("hashchange", () => {
      for (const a of tabs.querySelectorAll(".shell-tab")) {
        if (current(a.dataset.href)) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
      }
    });
    const here = MORE.find(([href]) => current(href));
    const moreWrap = el("div", "shell-more");
    const moreBtn = el("button", "shell-tab shell-more-btn");
    moreBtn.type = "button";
    moreBtn.setAttribute("aria-haspopup", "true");
    moreBtn.setAttribute("aria-controls", "shell-more-menu");
    moreBtn.innerHTML = `<span>${here ? here[1] : "更多"}</span><svg aria-hidden="true" viewBox="0 0 12 12" width="12" height="12"><path d="M3 4.5 6 7.5 9 4.5" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
    if (here) moreBtn.setAttribute("aria-current", "page");
    const menu = el("div", "shell-menu");
    menu.id = "shell-more-menu";
    menu.hidden = true;
    const list = el("ul");
    let group = null;
    for (const [href, label, kind] of MORE) {
      if (kind !== group) { group = kind; const head = el("li", "shell-menu-label", kind); head.setAttribute("role", "presentation"); list.append(head); }
      const li = el("li"), a = el("a", null, label);
      a.href = href;
      if (current(href)) a.setAttribute("aria-current", "page");
      li.append(a);
      list.append(li);
    }
    const tech = el("button", "shell-tech");
    tech.type = "button"; tech.id = "shell-tech";
    const label = () => {
      const on = document.documentElement.classList.contains("show-tech");
      tech.textContent = on ? "隐藏技术细节" : "显示技术细节"; tech.setAttribute("aria-pressed", String(on));
    };
    tech.addEventListener("click", () => {
      const on = document.documentElement.classList.toggle("show-tech");
      try { localStorage.setItem(TECH, on ? "1" : "0"); } catch (_) { /* 存不下就只对这一页生效 */ }
      label();
    });
    label();
    const logout = el("form", "shell-logout");
    logout.method = "post";
    logout.action = "/dash/logout";
    logout.innerHTML = '<button type="submit" id="shell-logout">退出登录</button>';
    menu.append(list, tech, logout);
    moreWrap.append(moreBtn, menu);
    nav.append(tabs, moreWrap);
    disclosure(moreBtn, menu);

    const actBox = el("div", "shell-actions");
    for (const n of actions) actBox.append(n);

    // 节点名按钮：圆点表示目标身份状态，点开看完整的执行目标（target.js 的 #connection-target）
    const nodeBtn = el("button", "shell-node");
    nodeBtn.type = "button";
    nodeBtn.setAttribute("aria-controls", "connection-target");
    nodeBtn.setAttribute("aria-expanded", "false");
    nodeBtn.innerHTML = '<i class="shell-node-dot" aria-hidden="true"></i><span class="shell-node-name"></span>';
    nodeBtn.title = "执行目标";
    nodeBtn.dataset.state = "wait";
    // 上次读到的主机名先顶上，读回后再更新：切页时节点名和后面的标签不跟着变宽变窄
    const NAME_KEY = "hgamey-shell-node-name";
    const nameEl = nodeBtn.querySelector(".shell-node-name");
    try { nameEl.textContent = localStorage.getItem(NAME_KEY) || "节点"; } catch (_) { nameEl.textContent = "节点"; }

    // 中心节点：同一中心的各个对外入口，选中后在那个入口打开当前这页
    const CENTERS = [
      {label: "gameharness · Cloudflare", origin: "https://gameharness.liyao.space"},
      {label: "office · frp", origin: "http://office.liyao.space:40765"},
    ];
    const centerSel = el("select", "shell-center");
    centerSel.setAttribute("aria-label", "中心节点");
    centerSel.title = "中心节点";
    const centerAt = CENTERS.findIndex(c => c.origin === location.origin);
    if (centerAt < 0) centerSel.append(new Option(location.host, "", true, true));
    CENTERS.forEach((c, i) => centerSel.append(new Option(c.label, c.origin, i === centerAt, i === centerAt)));
    centerSel.addEventListener("change", () => {
      if (centerSel.value) location.href = centerSel.value + location.pathname + location.search + location.hash;
    });
    const nodeWrap = el("div", "shell-nodewrap");
    nodeWrap.append(nodeBtn, centerSel);
    // 窄屏上顶栏放不下这个下拉：中心入口改列在「更多」菜单里，同样在选中的入口打开当前这页
    const centers = el("div", "shell-centers");
    const centerList = el("ul");
    centerList.setAttribute("aria-label", "中心节点");
    // 和下拉一样点了才跳：页面上不出现指向别处的链接
    CENTERS.forEach((c, i) => {
      const li = el("li"), b = el("button", null, c.label);
      b.type = "button";
      if (i === centerAt) b.setAttribute("aria-current", "page");
      b.addEventListener("click", () => { location.href = c.origin + location.pathname + location.search + location.hash; });
      li.append(b);
      centerList.append(li);
    });
    centers.append(el("p", "shell-menu-label", "中心节点"), centerList);
    menu.insertBefore(centers, logout);

    top.append(brand, nodeWrap, nav, actBox);
    if (old) old.replaceWith(top); else document.body.prepend(top);
    document.body.classList.add("has-shell");
    // 动作放不进品牌和节点旁边时（例如手机上的 GOL 页）单独占一行；动作本身变宽（状态文字）或窗口变化时重新量
    const fit = () => {
      top.classList.remove("shell-crowded");
      const shown = [...actBox.children].filter(n => n.offsetWidth);
      const gap = parseFloat(getComputedStyle(actBox).columnGap) || 0;
      const need = shown.reduce((sum, n) => sum + n.offsetWidth, 0) + gap * Math.max(0, shown.length - 1);
      top.classList.toggle("shell-crowded", need > actBox.clientWidth + 1);
    };
    fit();
    addEventListener("resize", fit);
    if (window.ResizeObserver) { const ro = new ResizeObserver(fit); for (const n of actBox.children) ro.observe(n); }
    // 顶栏的实际高度（窄屏两三行时会变），页内锚点按它留出位置
    const height = () => document.documentElement.style.setProperty("--shell-h", top.offsetHeight + "px");
    height();
    addEventListener("resize", height);
    if (window.ResizeObserver) new ResizeObserver(height).observe(top);
    const tab = tabs.querySelector('[aria-current="page"]');
    if (tab && tabs.scrollWidth > tabs.clientWidth) tab.scrollIntoView({block: "nearest", inline: "center"});
    fetch("/dash/api/node").then(r => r.ok ? r.json() : null).then(d => {
      if (!d || !d.host) return;
      nameEl.textContent = d.host.hostname;
      try { localStorage.setItem(NAME_KEY, d.host.hostname); } catch (_) { /* private mode: just no head start next time */ }
    }).catch(() => {});
    // target.js 生成执行目标框时会发 hgamey-target-change；框一出现就接上（同一任务内，不会先露出来）
    let wired = false;
    const attach = () => { if (!wired && document.getElementById("connection-target")) { wired = true; wire(nodeBtn); } };
    document.addEventListener("hgamey-target-change", attach);
    attach();
  }

  function wire(nodeBtn) {
    const box = document.getElementById("connection-target");
    if (box) {
      box.classList.remove("collapsed");
      disclosure(nodeBtn, box, () => box.classList.contains("inline"));
      // target.js 在标题行上切换折叠；外壳里由节点按钮开关，这里吞掉那次切换
      box.addEventListener("click", e => { if (!e.target.closest("button,a,summary")) box.classList.remove("collapsed"); });
      const sync = () => {
        const problem = window.HGTarget?.problem?.() || "";
        const ok = !problem && window.HGTarget?.canWrite?.(), checking = /尚未核对|正在核对/.test(problem);
        nodeBtn.dataset.state = checking ? "wait" : problem ? "bad" : ok ? "ok" : "wait";
        box.classList.toggle("inline", !!problem && !checking);
        if (box.classList.contains("inline")) box.hidden = false;
        else if (nodeBtn.getAttribute("aria-expanded") !== "true") box.hidden = true;
        nodeBtn.title = problem || (ok ? "执行目标已核对" : "执行目标：只读");
      };
      document.addEventListener("hgamey-target-change", sync);
      box.hidden = true;
      sync();
    }
  }
  // 「重新登录」回到当前这页（连同任务、#位置），而不是总览
  document.addEventListener("click", (e) => {
    const a = e.target.closest && e.target.closest('a[href="/dash/login"]');
    if (a) a.href = "/dash/login?next=" + encodeURIComponent(location.pathname + location.search + location.hash);
  }, true);
  // 放在 body 里时立即画；万一被放进 head，等 body 出现再画
  if (document.body) build();
  else document.addEventListener("DOMContentLoaded", build, {once: true});
})();
