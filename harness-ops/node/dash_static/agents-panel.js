// Agent 页的已注册 Agent 区（FRONT-05）：注册表 + 各 agent 当前运行任务。
// 3 天没有活动、手动归档或已停用的收进「已归档 N 个」；有新活动自动回来。归档、停用、启用和重新获取标识要掌舵权限。
// Every part is built element by element, never parsed from a string: text stays text, and each piece of the page
// (a row, a cell, a dialog) is its own small function.
(function () {
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
  // An element with its attributes and children. `class` and `data` (its data-* attributes) are set as such; hidden,
  // open, disabled, readOnly and value as properties; the rest as attributes. Empty children are left out.
  const PROPERTIES = new Set(["hidden", "open", "disabled", "readOnly", "value"]);
  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value == null || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "data") Object.assign(node.dataset, value);
      else if (PROPERTIES.has(key)) node[key] = value;
      else node.setAttribute(key, value);
    }
    node.append(...children.flat(Infinity).filter(child => child != null && child !== false && child !== ""));
    return node;
  }
  const spaced = items => items.flatMap((item, i) => i ? [" ", item] : [item]);
  const active = HGJobState.active;
  const ARCHIVED = { idle: "3 天没有活动", manual: "手动归档", disabled: "已停用", tour: "体验身份" };
  const SOURCE = { registered: "注册", checkin: "签到", status: "报状态", job: "提交作业", research: "写研究记录" };
  const stamp = at => at ? new Date(at * 1000).toLocaleString("zh-CN", { hour12: false }) : "从未";
  function ago(at) {
    if (!at) return "没有活动记录";
    const s = Math.max(0, Date.now() / 1000 - at);
    return s < 3600 ? `${Math.max(1, Math.round(s / 60))} 分钟前` : s < 86400 ? `${Math.round(s / 3600)} 小时前` : `${Math.round(s / 86400)} 天前`;
  }
  // Who it is, the same line as its card on the home page: the first clause of its note, the whole note on hover.
  const role = note => note ? el("div", { class: "agent-role", title: note }, note.split(/[；;。\n]/)[0]) : null;
  // Its zone (a `zone:<name>` tag) by name alone; other tags (the model's maker, how it runs) are technical details.
  const ZONE = "zone:";
  function tags(list) {
    const zones = list.filter(t => t.startsWith(ZONE)).map(t => el("span", { class: "tag" }, t.slice(ZONE.length)));
    return [...(zones.length ? spaced(zones) : ["–"]), ...list.filter(t => !t.startsWith(ZONE)).flatMap(t => [" ", el("span", { class: "tag tech" }, t)])];
  }
  const sub = (text, title) => el("div", { class: "agent-sub muted", title }, text);
  let agents = [], foldOpen = false;

  // ── The table: one row per agent ──
  function actions(a) {
    const act = (kind, label, danger) => el("button", { class: danger ? "btn sm danger" : "btn sm", type: "button", data: { act: kind, agent: a.name } }, label);
    if (a.enabled === false) return [act("enable", "重新启用")];
    return spaced([a.archived === "manual" ? act("unarchive", "取消归档") : !a.archived ? act("archive", "归档") : null,
                   act("disable", "停用", true), act("token", "重新获取标识")].filter(Boolean));
  }
  function row(a, runningByAgent, owed) {
    const checkin = a.checkin?.at, activity = a.activity?.at;
    return el("tr", { data: { agentRow: a.name } },
      el("td", { class: "mono agent-who" }, el("button", { class: "agent-name", type: "button", data: { agent: a.name } }, a.name), role(a.note),
        a.archived ? sub(ARCHIVED[a.archived] || a.archived) : null),
      el("td", { class: "agent-zone" }, tags(a.tags || [])),
      el("td", { class: "num agent-running" }, String(runningByAgent[a.name] || 0)),
      el("td", { class: "agent-checkin", title: checkin ? stamp(checkin) : "" }, checkin ? ago(checkin) : "从未"),
      el("td", { class: "agent-activity", title: activity ? stamp(activity) : "" }, ago(activity), a.activity?.source ? sub(SOURCE[a.activity.source] || a.activity.source) : null),
      el("td", { class: "num agent-owed" }, String(owed[a.name] ?? "–")),
      el("td", { class: "agent-status" }, a.status ? a.status.text : "–", a.status ? sub(ago(a.status.at), stamp(a.status.at)) : null),
      el("td", { class: "agent-actions" }, actions(a)));
  }
  function table(items, runningByAgent, owed) {
    const heads = ["名称", "标签", "运行中作业", "上次签到", "最近活动", "待办", "当前状态"].map(text => el("th", null, text));
    return el("table", { class: "tbl agents-table" },
      el("thead", null, el("tr", null, heads, el("th", { class: "agent-actions" }, "操作"))),
      el("tbody", null, items.map(a => row(a, runningByAgent, owed))));
  }
  // Archiving, disabling and new tokens are for whoever holds the steer password: until it is unlocked their column is
  // not shown, and 管理 unlocks it.
  function manage(access) {
    const card = document.getElementById("agents-card"), head = card?.querySelector(".card-h");
    if (!head) return;
    let button = document.getElementById("agents-manage");
    if (!button) {
      button = el("button", { id: "agents-manage", type: "button", class: "btn sm" }, "管理");
      button.addEventListener("click", async () => { if (await steer()) { if (dialog?.open) dialog.close(); await load(); } });
      head.append(button);
    }
    card.classList.toggle("can-manage", !!access?.steer);
    button.hidden = !!access?.steer || !access?.steer_configured;
  }
  async function load() {
    const list = document.getElementById("agents-list");
    if (!list) return;
    const [registry, jobs, access] = await Promise.all([
      api("/dash/api/agents").catch(() => ({ agents: [] })),
      api("/dash/api/jobs?limit=50").catch(() => ({ items: [] })),
      api("/dash/api/access").catch(() => null),
    ]);
    manage(access);
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
    list.replaceChildren(...[
      shown.length ? table(shown, runningByAgent, owed) : agents.length ? null : el("div", { class: "empty" }, "还没有注册 Agent"),
      folded.length ? el("details", { id: "agents-archived", class: "agents-archived", open: foldOpen },
        el("summary", null, `已归档 ${folded.length} 个`), table(folded, runningByAgent, owed)) : null,
    ].filter(Boolean));
  }

  // ── One dialog for the changes: unlock first when needed, confirm, then the outcome ──
  let dialog = null;
  function box() {
    if (dialog) return dialog;
    dialog = el("dialog", { id: "agent-dialog", class: "agent-dialog" });
    dialog.addEventListener("close", () => { dialog.replaceChildren(); });   // a token shown here goes with it
    document.body.append(dialog);
    return dialog;
  }
  function show(...parts) { const d = box(); d.replaceChildren(...parts.flat(Infinity).filter(Boolean)); if (!d.open) d.showModal(); return d; }
  const said = text => el("p", { class: "agent-dialog-text" }, text);
  const buttons = (...items) => el("div", { class: "agent-dialog-actions" }, items);
  const closer = label => el("button", { class: "btn", type: "button", data: { close: "" } }, label);
  function done(text) { show(said(text), buttons(closer("关闭"))); }
  async function steer() {
    const access = await api("/dash/api/access").catch(() => null);
    if (access?.steer) return true;
    if (!access?.steer_configured) { done("本节点未配置操作口令，Agent 只能查看。"); return false; }
    return new Promise(resolve => {
      const input = el("input", { id: "agent-steer", type: "password", autocomplete: "current-password", placeholder: "向管理员要" });
      const msg = el("p", { id: "agent-steer-msg", class: "agent-dialog-msg", role: "status" });
      const go = el("button", { class: "btn primary", type: "button", id: "agent-unlock" }, "解锁");
      const d = show(el("h2", null, "需要操作口令"), said("归档、停用、启用和重新获取标识需要先解锁。"),
        el("label", { for: "agent-steer" }, "操作口令"), input, msg, buttons(closer("取消"), go));
      d.addEventListener("close", () => resolve(false), { once: true });
      const unlock = async () => {
        const pw = input.value; input.value = "";
        try { await api("/dash/api/steer/unlock", { pw }); resolve(true); }
        catch (e) { msg.textContent = "解锁失败：" + e.message; }
      };
      go.onclick = unlock;
      input.addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); unlock(); } });
      input.focus();
    });
  }
  function confirmWith(parts, label, danger, check) {
    return new Promise(resolve => {
      const go = el("button", { class: `btn ${danger ? "danger" : "primary"}`, type: "button", id: "agent-go" }, label);
      const d = show(parts, buttons(closer("取消"), go));
      const settle = value => { d.removeEventListener("close", closed); resolve(value); }, closed = () => resolve(false);
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
        if (!(await confirmWith([el("h2", null, `停用 ${name}？`), said("停用后它不能再连中心，现在用的标识立即作废；历史保留，它的作业、留言里的名字和链接都不变。之后可以重新启用，但要重新获取标识。")], "停用", true))) return;
        await api(path + "disable", { revision: agent.revision });
        done(`${name} 已停用并归档。`);
      } else if (act === "enable") {
        if (!(await confirmWith([el("h2", null, `重新启用 ${name}？`), said("启用后它仍然没有可用的标识：要用「重新获取标识」给它一个新的，或者重新发邀请，它才能再连中心。")], "重新启用", false))) return;
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
    const warning = el("div", { class: running ? "agent-warning is-running" : "agent-warning" },
      running ? el("p", null, el("b", null, "它看起来还在运行。")) : null,
      el("ul", null, facts.map(fact => el("li", null, fact))),
      el("p", null, "重新获取后，它现在用的标识立即失效；如果它其实还在运行，会马上断开。"));
    const typed = running ? [el("label", { for: "agent-confirm-name" }, "输入 agent 名 ", el("b", null, name), " 确认"),
      el("input", { id: "agent-confirm-name", autocomplete: "off" })] : [];
    if (!(await confirmWith([el("h2", null, `重新获取 ${name} 的标识`), warning, typed], "重新获取标识", true,
      running ? d => d.querySelector("#agent-confirm-name").value.trim() === name : null))) return;
    const issued = await api(path + "token", {});
    const url = location.origin, file = "$HOME/.config/hgamey/agent.token", quote = v => `'${String(v).replace(/'/g, `'\\''`)}'`;
    const info = [`中心地址：${url}`, `节点 ID：${issued.node_id}`, `Agent：${name}`, `标识（token）：${issued.token}`, "",
      "写进 token 文件（pf-node 的做法）：", `umask 077; mkdir -p "$HOME/.config/hgamey"; printf '%s\\n' ${quote(issued.token)} > "${file}"`,
      `export PF_NODE_URL=${quote(url)}`, `export PF_NODE_ID=${quote(issued.node_id)}`, `export PF_NODE_TOKEN_FILE="${file}"`, "",
      `或者不用文件：export PF_NODE_TOKEN=${quote(issued.token)}（不要和 PF_NODE_TOKEN_FILE 同时设置）`].join("\n");
    const copier = (id, label) => el("button", { class: "btn sm", type: "button", data: { copy: id } }, label);
    const field = el("input", { id: "agent-token", class: "mono", readOnly: true, value: issued.token });
    show(el("h2", null, `${name} 的新标识`), said("只显示这一次，关掉或刷新页面就看不到了。旧标识已经失效。"),
      el("label", { for: "agent-token" }, "标识"), el("div", { class: "agent-copy" }, field, copier("agent-token", "复制")),
      el("label", { for: "agent-connect" }, "接回中心需要的全部信息"),
      el("textarea", { id: "agent-connect", class: "mono", readOnly: true, rows: "11", value: info }),
      buttons(copier("agent-connect", "复制全部"), closer("关闭")),
      el("p", { id: "agent-copy-msg", class: "agent-dialog-msg", role: "status" }));
    field.select();
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
    // On a phone an agent's status shows two lines; a tap shows the rest.
    ev.target.closest("#agents-list .agent-status")?.classList.toggle("open");
  });

  // 点击 agent 名展开详情：滚到详情卡，写明它是谁、在负责哪些任务、最近交付了什么，再是运行中作业和状态历史。
  const TASK_STATE = { active: "推进中", idle: "暂空闲", paused: "已暂停" };
  const taskLink = (href, title, ...extra) => el("div", { class: "row" }, el("a", { href: window.HGTarget ? HGTarget.url(href) : href }, title), extra);
  const fill = (id, rows, empty) => document.getElementById(id).replaceChildren(...(rows.length ? rows : [el("div", { class: "empty" }, empty)]));
  function detailParts() {
    if (document.getElementById("agent-detail-work")) return;
    const body = document.querySelector("#agent-detail .card-body");
    body.prepend(el("p", { id: "agent-detail-who", class: "agent-detail-who" }));
    body.querySelector("h3").before(el("div", { id: "agent-detail-work" }, el("h3", null, "负责的任务"), el("div", { id: "agent-detail-owned" }),
      el("h3", null, "最近交付"), el("div", { id: "agent-detail-delivered" })));
  }
  let opened = null;
  document.addEventListener("click", async (ev) => {
    const btn = ev.target.closest(".agent-name");
    if (!btn) return;
    const name = btn.dataset.agent;
    const card = document.getElementById("agent-detail");
    if (!card) return;
    const mine = opened = {};
    detailParts();
    card.hidden = false;
    document.getElementById("agent-detail-name").textContent = name;
    const note = agents.find(a => a.name === name)?.note || "";
    document.getElementById("agent-detail-who").textContent = note;
    document.getElementById("agent-detail-who").hidden = !note;
    for (const id of ["agent-detail-owned", "agent-detail-delivered", "agent-detail-tasks", "agent-detail-history"]) fill(id, [], "读取中…");
    card.scrollIntoView({ block: "start" });
    // 加载详情：它负责的任务 + 状态历史 + 当前作业
    const [agent, jobs, tasks] = await Promise.all([
      api(`/dash/api/agents/${encodeURIComponent(name)}`).catch(() => null),
      api("/dash/api/jobs?limit=50").catch(() => ({ items: [] })),
      api("/dash/api/tasks?all=1").catch(() => ({ items: [] })),
    ]);
    if (opened !== mine) return;
    const owned = (tasks.items || []).filter(t => t.owner === name), recent = t => -(t.active_at || t.updated_at || 0);
    const open = owned.filter(t => TASK_STATE[t.state]).sort((a, b) => recent(a) - recent(b));
    const delivered = owned.filter(t => t.state === "delivered").sort((a, b) => recent(a) - recent(b)).slice(0, 5);
    fill("agent-detail-owned", open.map(t => taskLink(`/dash/research?kind=research&id=${encodeURIComponent(t.id)}`, t.title || t.id,
      " ", el("span", { class: "muted" }, TASK_STATE[t.state]))), "没有在负责的任务");
    fill("agent-detail-delivered", delivered.map(t => taskLink(`/dash/research/report?id=${encodeURIComponent(t.id)}`, t.title || t.id,
      " ", el("span", { class: "muted", title: stamp(t.updated_at) }, ago(t.updated_at)))), "还没有交付");
    const running = (jobs.items || []).filter(j => active(j) && j.actor_id === name);
    const history = (agent && agent.status_history) || [];
    document.getElementById("agent-detail-status").textContent = agent && agent.status ? agent.status.text : "–";
    fill("agent-detail-tasks", running.map(j => el("div", { class: "row" }, el("span", { class: "mono" }, j.id), " ", j.label || j.bin)), "没有运行中的作业");
    fill("agent-detail-history", history.slice(-10).reverse().map(h => el("div", { class: "row" },
      el("span", { class: "muted" }, new Date(h.at * 1000).toLocaleString("zh-CN", { hour12: false })), " ", h.text)), "暂无状态历史");
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
