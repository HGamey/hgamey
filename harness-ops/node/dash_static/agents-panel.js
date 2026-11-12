// Agent 页的已注册 Agent 区（FRONT-05）：注册表 + 各 agent 当前运行任务。
// 3 天没有活动、手动归档或已停用的收进「已归档 N 个」；有新活动自动回来。归档、停用、启用和重新获取标识要掌舵权限。
(function () {
  const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  async function api(path, body) {
    const init = { credentials: "same-origin", cache: "no-store" };
    if (body !== undefined) Object.assign(init, { method: "POST", headers: { "Content-Type": "application/json", "X-HG": "1" }, body: JSON.stringify(body) });
    const r = await (window.HGTarget ? HGTarget.fetch(path, init) : fetch(path, init));
    if (!r.ok) {
      const value = await r.json().catch(() => null), error = new Error(value?.error || path + " → HTTP " + r.status);
      error.status = r.status; error.steer = !!value?.need_steer;
      throw error;
    }
    return r.json();
  }
  const active = HGJobState.active;
  const ARCHIVED = { idle: "3 天没有活动", manual: "手动归档", disabled: "已停用", tour: "体验身份" };
  const SOURCE = { registered: "注册", checkin: "签到", status: "报状态", job: "提交作业", research: "写研究记录" };
  const stamp = at => at ? new Date(at * 1000).toLocaleString("zh-CN", { hour12: false }) : "从未";
  function ago(at) {
    if (!at) return "没有活动记录";
    const s = Math.max(0, Date.now() / 1000 - at);
    return s < 3600 ? `${Math.max(1, Math.round(s / 60))} 分钟前` : s < 86400 ? `${Math.round(s / 3600)} 小时前` : `${Math.round(s / 86400)} 天前`;
  }
  let agents = [], foldOpen = false;

  function actions(a) {
    const act = (kind, label, cls = "") => `<button class="btn sm${cls}" type="button" data-act="${kind}" data-agent="${esc(a.name)}">${label}</button>`;
    if (a.enabled === false) return act("enable", "重新启用");
    return [a.archived === "manual" ? act("unarchive", "取消归档") : !a.archived ? act("archive", "归档") : "",
            act("disable", "停用", " danger"), act("token", "重新获取标识")].join(" ");
  }
  function table(items, runningByAgent, owed) {
    return `<table class="tbl" style="width:100%"><thead><tr><th>名称</th><th>标签</th><th>运行中作业</th><th>上次签到</th><th>最近活动</th><th>待办</th><th>当前状态</th><th>操作</th></tr></thead><tbody>${
      items.map(a => `<tr data-agent-row="${esc(a.name)}">
        <td class="mono"><button class="agent-name" data-agent="${esc(a.name)}" type="button" style="background:none;border:none;color:var(--acc);cursor:pointer;font:inherit;padding:0;text-decoration:underline">${esc(a.name)}</button>${a.archived ? `<div class="muted" style="font-size:12px">${esc(ARCHIVED[a.archived] || a.archived)}</div>` : ""}</td>
        <td>${(a.tags || []).map(t => `<span class="tag">${esc(t)}</span>`).join(" ") || "–"}</td>
        <td class="num">${runningByAgent[a.name] || 0}</td>
        <td title="${esc(a.checkin?.at ? stamp(a.checkin.at) : "")}">${a.checkin?.at ? esc(ago(a.checkin.at)) : "从未"}</td>
        <td title="${esc(a.activity?.at ? stamp(a.activity.at) : "")}">${esc(ago(a.activity?.at))}${a.activity?.source ? `<div class="muted" style="font-size:12px">${esc(SOURCE[a.activity.source] || a.activity.source)}</div>` : ""}</td>
        <td class="num">${owed[a.name] ?? "–"}</td>
        <td>${a.status ? esc(a.status.text) : "–"}${a.status ? `<div class="muted" style="font-size:12px" title="${esc(stamp(a.status.at))}">${esc(ago(a.status.at))}</div>` : ""}</td>
        <td class="agent-actions">${actions(a)}</td>
      </tr>`).join("")}</tbody></table>`;
  }
  async function load() {
    const list = document.getElementById("agents-list");
    if (!list) return;
    const [registry, jobs] = await Promise.all([
      api("/dash/api/agents").catch(() => ({ agents: [] })),
      api("/dash/api/jobs?limit=50").catch(() => ({ items: [] })),
    ]);
    const runningByAgent = {};
    for (const j of jobs.items || []) {
      if (active(j) && j.actor_id) runningByAgent[j.actor_id] = (runningByAgent[j.actor_id] || 0) + 1;
    }
    agents = registry.agents || [];
    const owed = Object.fromEntries(await Promise.all(agents.filter(a => a.enabled !== false).map(a =>
      api(`/dash/api/agents/${encodeURIComponent(a.name)}/duties`).then(d => [a.name, d.duties.length]).catch(() => [a.name, null]))));
    const shown = agents.filter(a => !a.archived), folded = agents.filter(a => a.archived);
    foldOpen = document.getElementById("agents-archived")?.open ?? foldOpen;
    document.getElementById("agents-count").textContent = shown.length ? shown.length + " 个" : "";
    list.innerHTML = (shown.length ? table(shown, runningByAgent, owed) : agents.length ? "" : `<div class="empty">还没有注册 Agent</div>`)
      + (folded.length ? `<details id="agents-archived" class="agents-archived"${foldOpen ? " open" : ""}><summary>已归档 ${folded.length} 个</summary>${table(folded, runningByAgent, owed)}</details>` : "");
  }

  // ── One dialog for the changes: unlock first when needed, confirm, then the outcome ──
  let dialog = null;
  function box() {
    if (dialog) return dialog;
    dialog = document.createElement("dialog");
    dialog.id = "agent-dialog"; dialog.className = "agent-dialog";
    dialog.addEventListener("close", () => { dialog.replaceChildren(); });   // a token shown here goes with it
    document.body.append(dialog);
    return dialog;
  }
  function show(html) { const d = box(); d.innerHTML = html; if (!d.open) d.showModal(); return d; }
  function done(text) { show(`<p class="agent-dialog-text">${esc(text)}</p><div class="agent-dialog-actions"><button class="btn" type="button" data-close>关闭</button></div>`); }
  async function steer() {
    const access = await api("/dash/api/access").catch(() => null);
    if (access?.steer) return true;
    if (!access?.steer_configured) { done("本节点未配置掌舵密码，Agent 只能查看。"); return false; }
    return new Promise(resolve => {
      const d = show(`<h2>需要掌舵权限</h2><p class="agent-dialog-text">归档、停用、启用和重新获取标识需要先解锁。</p>
        <label for="agent-steer">掌舵密码</label><input id="agent-steer" type="password" autocomplete="current-password">
        <p id="agent-steer-msg" class="agent-dialog-msg" role="status"></p>
        <div class="agent-dialog-actions"><button class="btn" type="button" data-close>取消</button><button class="btn primary" type="button" id="agent-unlock">解锁</button></div>`);
      d.addEventListener("close", () => resolve(false), { once: true });
      const unlock = async () => {
        const pw = d.querySelector("#agent-steer").value; d.querySelector("#agent-steer").value = "";
        try { await api("/dash/api/steer/unlock", { pw }); resolve(true); }
        catch (e) { d.querySelector("#agent-steer-msg").textContent = "解锁失败：" + e.message; }
      };
      d.querySelector("#agent-unlock").onclick = unlock;
      d.querySelector("#agent-steer").addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); unlock(); } });
      d.querySelector("#agent-steer").focus();
    });
  }
  function confirmWith(html, label, danger, check) {
    return new Promise(resolve => {
      const d = show(`${html}<div class="agent-dialog-actions"><button class="btn" type="button" data-close>取消</button><button class="btn ${danger ? "danger" : "primary"}" type="button" id="agent-go">${label}</button></div>`);
      const go = d.querySelector("#agent-go"), settle = value => { d.removeEventListener("close", closed); resolve(value); }, closed = () => resolve(false);
      d.addEventListener("close", closed);
      if (check) { const sync = () => { go.disabled = !check(d); }; d.addEventListener("input", sync); sync(); }
      go.onclick = () => { if (!go.disabled) settle(true); };
    });
  }
  async function change(name, act) {
    const agent = agents.find(a => a.name === name);
    if (!agent || !(await steer())) return;
    const path = `/dash/api/agents/${encodeURIComponent(name)}/`;
    try {
      if (act === "archive" || act === "unarchive") {
        await api(path + "archive", { revision: agent.revision, archived: act === "archive" });
        if (dialog?.open) dialog.close();
      } else if (act === "disable") {
        if (!(await confirmWith(`<h2>停用 ${esc(name)}？</h2><p class="agent-dialog-text">停用后它不能再连中心，现在用的标识立即作废；历史保留，它的作业、留言里的名字和链接都不变。之后可以重新启用，但要重新获取标识。</p>`, "停用", true))) return;
        await api(path + "disable", { revision: agent.revision });
        done(`${name} 已停用并归档。`);
      } else if (act === "enable") {
        if (!(await confirmWith(`<h2>重新启用 ${esc(name)}？</h2><p class="agent-dialog-text">启用后它仍然没有可用的标识：要用「重新获取标识」给它一个新的，或者重新发邀请，它才能再连中心。</p>`, "重新启用", false))) return;
        await api(path + "enable", { revision: agent.revision });
        done(`${name} 已重新启用。它原来的标识已作废，用「重新获取标识」给它一个新的，它才能再连中心。`);
      } else if (act === "token") await token(name, path);
    } catch (e) {
      done(e.status === 409 ? `没有保存：${e.message}。刷新后再试。` : `没有保存：${e.message}`);
    }
    await load().catch(() => {});
  }

  // A new token replaces the one the agent holds now, at once. What it is doing now is read first; one that looks
  // to be running asks for its name before going on. The token is shown here once and kept nowhere.
  async function token(name, path) {
    const detail = await api(`/dash/api/agents/${encodeURIComponent(name)}`);
    const at = detail.activity?.at, jobs = detail.work?.jobs || [], sessions = detail.work?.sessions || [];
    const recent = at && Date.now() / 1000 - at < (detail.active_within_s || 1800);
    const running = recent || jobs.length || sessions.length;
    const facts = [`最近活动：${at ? `${stamp(at)}（${ago(at)}，${SOURCE[detail.activity.source] || detail.activity.source}）` : "没有活动记录"}`,
      `未结束的作业：${jobs.length ? jobs.join("、") : "没有"}`, `远程会话 / 设备占用：${sessions.length ? sessions.join("、") : "没有"}`];
    const html = `<h2>重新获取 ${esc(name)} 的标识</h2>
      <div class="agent-warning${running ? " is-running" : ""}">${running ? "<p><b>它看起来还在运行。</b></p>" : ""}
        <ul>${facts.map(f => `<li>${esc(f)}</li>`).join("")}</ul>
        <p>重新获取后，它现在用的标识立即失效；如果它其实还在运行，会马上断开。</p></div>
      ${running ? `<label for="agent-confirm-name">输入 agent 名 <b>${esc(name)}</b> 确认</label><input id="agent-confirm-name" autocomplete="off">` : ""}`;
    if (!(await confirmWith(html, "重新获取标识", true, running ? d => d.querySelector("#agent-confirm-name").value.trim() === name : null))) return;
    const issued = await api(path + "token", {});
    const url = location.origin, file = "$HOME/.config/hgamey/agent.token", quote = v => `'${String(v).replace(/'/g, `'\\''`)}'`;
    const info = [`中心地址：${url}`, `节点 ID：${issued.node_id}`, `Agent：${name}`, `标识（token）：${issued.token}`, "",
      "写进 token 文件（pf-node 的做法）：", `umask 077; mkdir -p "$HOME/.config/hgamey"; printf '%s\\n' ${quote(issued.token)} > "${file}"`,
      `export PF_NODE_URL=${quote(url)}`, `export PF_NODE_ID=${quote(issued.node_id)}`, `export PF_NODE_TOKEN_FILE="${file}"`, "",
      `或者不用文件：export PF_NODE_TOKEN=${quote(issued.token)}（不要和 PF_NODE_TOKEN_FILE 同时设置）`].join("\n");
    const d = show(`<h2>${esc(name)} 的新标识</h2><p class="agent-dialog-text">只显示这一次，关掉或刷新页面就看不到了。旧标识已经失效。</p>
      <label for="agent-token">标识</label><div class="agent-copy"><input id="agent-token" class="mono" readonly value="${esc(issued.token)}"><button class="btn sm" type="button" data-copy="agent-token">复制</button></div>
      <label for="agent-connect">接回中心需要的全部信息</label><textarea id="agent-connect" class="mono" readonly rows="11">${esc(info)}</textarea>
      <div class="agent-dialog-actions"><button class="btn sm" type="button" data-copy="agent-connect">复制全部</button><button class="btn" type="button" data-close>关闭</button></div>
      <p id="agent-copy-msg" class="agent-dialog-msg" role="status"></p>`);
    d.querySelector("#agent-token").select();
  }
  // Copying works over plain HTTP too, where the browser has no clipboard API.
  async function copy(id) {
    const field = document.getElementById(id), msg = document.getElementById("agent-copy-msg");
    try { if (navigator.clipboard && isSecureContext) await navigator.clipboard.writeText(field.value); else { field.select(); if (!document.execCommand("copy")) throw new Error(); } msg.textContent = "已复制"; }
    catch (_) { field.select(); msg.textContent = "没能自动复制，已选中，请手动复制"; }
  }

  document.addEventListener("click", ev => {
    const action = ev.target.closest("[data-act]");
    if (action) { change(action.dataset.agent, action.dataset.act); return; }
    if (ev.target.closest("[data-close]")) { dialog?.close(); return; }
    const copier = ev.target.closest("[data-copy]");
    if (copier) copy(copier.dataset.copy);
  });

  // 点击 agent 名展开详情
  document.addEventListener("click", async (ev) => {
    const btn = ev.target.closest(".agent-name");
    if (!btn) return;
    const name = btn.dataset.agent;
    const card = document.getElementById("agent-detail");
    if (!card) return;
    card.hidden = false;
    document.getElementById("agent-detail-name").textContent = name;
    // 加载详情：状态历史 + 当前任务
    const [agent, jobs] = await Promise.all([
      api(`/dash/api/agents/${name}`).catch(() => null),
      api("/dash/api/jobs?limit=50").catch(() => ({ items: [] })),
    ]);
    const running = (jobs.items || []).filter(j => active(j) && j.actor_id === name);
    const history = (agent && agent.status_history) || [];
    document.getElementById("agent-detail-status").textContent = agent && agent.status ? agent.status.text : "–";
    document.getElementById("agent-detail-tasks").innerHTML = running.length
      ? running.map(j => `<div class="row"><span class="mono">${esc(j.id)}</span> ${esc(j.label || j.bin)}</div>`).join("")
      : '<div class="empty">没有运行中的作业</div>';
    document.getElementById("agent-detail-history").innerHTML = history.length
      ? history.slice(-10).reverse().map(h => `<div class="row"><span class="muted">${new Date(h.at * 1000).toLocaleString("zh-CN", { hour12: false })}</span> ${esc(h.text)}</div>`).join("")
      : '<div class="empty">暂无状态历史</div>';
  });

  // On a task page the directory is folded: read it once it is opened (the Agent tab opens it).
  const directory = document.getElementById("agents-directory");
  let started = false;
  const start = () => {
    if (started || (directory && !directory.open)) return;
    started = true; load().catch(e => { started = false; console.error(e); });
  };
  directory?.addEventListener("toggle", start);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
