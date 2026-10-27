// Agent 页的已注册 Agent 区（FRONT-05）：注册表 + 各 agent 当前运行任务。
(function () {
  const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  async function api(path) {
    const r = await fetch(path);
    if (!r.ok) throw new Error(path + " → HTTP " + r.status);
    return r.json();
  }
  async function load() {
    const list = document.getElementById("agents-list");
    if (!list) return;
    const [agents, jobs] = await Promise.all([
      api("/dash/api/agents").catch(() => ({ agents: [] })),
      api("/dash/api/jobs?limit=50").catch(() => ({ items: [] })),
    ]);
    const runningByAgent = {};
    for (const j of jobs.items || []) {
      if (j.state === "running" && j.actor_id) runningByAgent[j.actor_id] = (runningByAgent[j.actor_id] || 0) + 1;
    }
    const items = agents.agents || [];
    document.getElementById("agents-count").textContent = items.length ? items.length + " 个" : "";
    list.innerHTML = items.length
      ? `<table class="tbl" style="width:100%"><thead><tr><th>名称</th><th>标签</th><th>运行中任务</th><th>注册时间</th></tr></thead><tbody>${
          items.map(a => `<tr>
            <td class="mono">${esc(a.name)}</td>
            <td>${(a.tags || []).map(t => `<span class="tag">${esc(t)}</span>`).join(" ") || "–"}</td>
            <td class="num">${runningByAgent[a.name] || 0}</td>
            <td>${a.status ? esc(a.status.text) : "–"}${a.status ? `<div class="muted" style="font-size:12px">${new Date(a.status.at * 1000).toLocaleString("zh-CN", { hour12: false })}</div>` : ""}</td>
          </tr>`).join("")}</tbody></table>`
      : `<div class="empty"><b>还没有注册 Agent</b>用 pf-node agents register 注册；任务提交时带 --actor-id 关联</div>`;
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => load().catch(e => console.error(e)));
  else load().catch(e => console.error(e));
})();
