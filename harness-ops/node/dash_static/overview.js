// 总览页（FRONT-02）：现在发生了什么——运行中任务、待人处理、设备、最近事件。
(function () {
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const t = v => v ? new Date(v * 1000).toLocaleTimeString("zh-CN", { hour12: false }) : "–";

  async function api(path) {
    const r = await fetch(path);
    if (!r.ok) throw new Error(path + " → HTTP " + r.status);
    return r.json();
  }

  function row(title, sub, right, cls) {
    return `<div class="row"><div class="what"><b>${title}</b>${sub ? `<span>${sub}</span>` : ""}</div>${right || ""}</div>`;
  }
  function empty(text, sub) {
    return `<div class="empty"><b>${esc(text)}</b>${esc(sub || "")}</div>`;
  }

  async function load() {
    const [queue, jobs, requests, node] = await Promise.all([
      api("/dash/api/queue").catch(() => null),
      api("/dash/api/jobs?limit=8").catch(() => ({ items: [] })),
      api("/dash/api/requests?state=open&limit=6").catch(() => null),
      api("/dash/api/node").catch(() => null),
    ]);
    $("read-at").textContent = "读取于 " + new Date().toLocaleTimeString("zh-CN", { hour12: false });

    const running = (jobs.items || []).filter(j => j.state === "running");
    const queuedCount = queue ? queue.queued_count ?? 0 : null;
    const waitingItems = (requests && requests.items) ? requests.items.filter(r => (r.data || {}).state === "open") : [];

    // KPI：字不是卡片
    $("kpis").innerHTML = [
      ["正在运行", running.length, running.length ? "个任务" : "无"],
      ["排队中", queuedCount === null ? "–" : queuedCount, queuedCount ? "个任务" : ""],
      ["待人处理", waitingItems.length, waitingItems.length ? "条请求" : "无"],
      ["节点", node ? "在线" : "未知", node ? (node.host || {}).hostname || "" : ""],
    ].map(([label, num, sub]) =>
      `<div class="kpi"><span class="kpi-num">${num}</span><span class="kpi-label">${esc(label)}</span>${sub ? `<span class="kpi-sub">${esc(sub)}</span>` : ""}</div>`).join("");

    // 正在运行
    $("running-sub").textContent = running.length ? running.length + " 个" : "";
    $("running").innerHTML = running.length ? running.map(j =>
      row(`<span class="mono">${esc(j.id)}</span> ${esc(j.label || j.bin)}`,
          `${esc(j.bin)} · ${j.actor_id ? "agent " + esc(j.actor_id) + " · " : ""}开始于 ${t(j.started)}`,
          `<span class="tag ok">运行中</span>`)).join("")
      : empty("没有运行中的任务", "提交任务后会显示在这里");

    // 待人处理
    $("waiting-sub").textContent = waitingItems.length ? waitingItems.length + " 条" : "";
    $("waiting").innerHTML = waitingItems.length ? waitingItems.map(r =>
      row(esc((r.data || {}).title || r.id),
          esc((r.data || {}).type || "请求") + " · " + t(r.created_at),
          `<span class="tag warn">待处理</span>`)).join("")
      : empty("没有待处理的请求", "Agent 的请求会显示在这里");

    // 设备
    const devices = node && node.devices;
    if (devices && devices.status === "not_probed") {
      $("devices").innerHTML = empty("节点不主动探测手机", "设备经登记与任务 owner 流程出现");
    } else {
      $("devices").innerHTML = empty("未读取到设备清单", "到设备页查看登记与占用");
    }

    // 最近事件：最近结束的 4 个任务
    const recent = (jobs.items || []).filter(j => j.state === "finished").slice(0, 4);
    $("events").innerHTML = recent.length ? recent.map(j =>
      row(`<span class="mono">${esc(j.id)}</span> ${esc(j.label || j.bin)}`,
          `${esc(j.bin)} · ${t(j.ended || j.started)}`,
          j.outcome === "succeeded" ? `<span class="tag ok">完成</span>` :
          j.outcome === "cancelled" ? `<span class="tag off">已取消</span>` :
          `<span class="tag bad">${esc(j.outcome || "结束")}</span>`)).join("")
      : empty("暂无事件");
  }

  $("refresh").addEventListener("click", () => load().catch(e => console.error(e)));
  load().catch(e => console.error(e));
})();
