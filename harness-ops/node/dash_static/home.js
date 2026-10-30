// HGamey 首页：待你回复、进行中任务（含最新进展）、已交付报告、手机占用、Agent。
(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const num = (v, d = 1) => v == null || Number.isNaN(+v) ? "—" : (+v).toFixed(d);
  const toDate = t => typeof t === "number" ? new Date(t * 1000) : new Date(t);
  const HM = new Intl.DateTimeFormat("zh-CN", {hour: "2-digit", minute: "2-digit", hour12: false});
  const MD = new Intl.DateTimeFormat("zh-CN", {month: "numeric", day: "numeric"});
  const FULL = new Intl.DateTimeFormat("zh-CN", {dateStyle: "medium", timeStyle: "short", hour12: false});
  const hm = t => { const d = toDate(t); return isNaN(d) ? "" : HM.format(d); };
  const full = t => { const d = toDate(t); return isNaN(d) ? "" : FULL.format(d); };
  const S = {home: null, researchDraft: null, afterUnlock: null, busy: false};

  // "3 分钟前"; older than a day shows the date. The exact time stays in the title.
  function ago(now, t) {
    if (!t) return "";
    const m = Math.max(0, Math.round((now - t) / 60));
    if (m < 1) return "刚刚";
    if (m < 60) return `${m} 分钟前`;
    if (m < 1440) return `${Math.round(m / 60)} 小时前`;
    return MD.format(toDate(t));
  }
  const when = (now, t) => t ? `<time datetime="${esc(toDate(t).toISOString())}" title="${esc(full(t))}">${esc(ago(now, t))}</time>` : "";
  const initial = name => esc((Array.from(String(name || "?").replace(/^[^\p{L}\p{N}]+/u, ""))[0] || "?").toUpperCase());
  const avatar = (name, human = false) => `<span class="av${human ? " human" : ""}" aria-hidden="true">${human ? "人" : initial(name)}</span>`;
  const speaker = note => note.auth_channel === "dashboard_steer" ? "人工" : note.auth_channel === "admin_token" ? "管理员" : note.actor_label || "agent";
  const model = s => s ? (s.model || s.serial) : "";

  async function api(url, body) {
    const opt = body === undefined ? {cache: "no-store", credentials: "same-origin"}
      : {method: "POST", credentials: "same-origin", headers: {"Content-Type": "application/json", "X-HG": "1"}, body: JSON.stringify(body)};
    const r = await HGTarget.fetch(url, opt);
    if (r.status === 401) { location.href = "/dash/login"; throw new Error("401"); }
    let j = {}; try { j = await r.json(); } catch (_) { /* An unknown write must not be retried automatically. */ }
    if (!r.ok) { const error = new Error(j.error || `HTTP ${r.status}`); error.data = j; error.status = r.status; throw error; }
    return j;
  }
  let polling = null;
  async function pollHome() {
    if (polling) return polling;
    polling = (async () => {
      try {
        const h = await api("/dash/api/home"); S.home = h;
        $("live").className = "live ok"; $("live-text").textContent = `实时 · ${hm(h.now)}`;
        $("live").title = `最近更新 ${full(h.now)}`;
        renderQuestions(h); renderReviews(h); renderTasks(h); renderReports(h); renderPhones(h); renderAgents(h); renderSteerBtn(h);
        document.querySelector("main.home").setAttribute("aria-busy", "false");
      } catch (_) {
        $("live").className = "live bad"; $("live-text").textContent = "连不上节点";
        $("live").title = S.home ? `显示的是 ${full(S.home.now)} 的数据` : "还没读到数据";
        if (!S.home) $("tasks").innerHTML = `<li class="empty-row">读不到首页数据。<button class="link-btn" type="button" data-retry>重试</button></li>`;
      } finally { polling = null; }
    })();
    return polling;
  }
  function renderSteerBtn(h) {
    const b = $("steer-btn");
    b.querySelector(".steer-label").textContent = h.steer ? "可操作" : "只读";
    b.setAttribute("aria-pressed", String(!!h.steer));
    b.classList.toggle("on", !!h.steer);
    b.title = h.steer ? "已解锁，点一下重新上锁" : "只读；点一下输入掌舵密码解锁";
    b.setAttribute("aria-label", h.steer ? "操作已解锁，点一下上锁" : "只读，点一下解锁操作");
  }

  const taskURL = t => HGTarget.url(t.report_url || `/dash/research?kind=research&id=${encodeURIComponent(t.id)}`);
  const ownerLine = t => t.owner ? `<span class="who">${avatar(t.owner)}${esc(t.owner)}</span>` : `<span class="tag warn">无人负责</span>`;
  function phoneOf(h, jobId) {
    return h.slots.find(s => s && (s.holder?.job_id === jobId || s.global_holder?.job_id === jobId));
  }
  function jobChip(h, j) {
    const state = j.public || j, phone = phoneOf(h, j.job_id);
    return `<span class="run"><span class="tag ${HGJobState.tone(state)}">${esc(HGJobState.label(state))}</span><span class="run-label">${esc(j.label || j.job_id)}</span>${phone ? `<span class="run-phone" title="${esc(phone.serial)}">${esc(model(phone))}</span>` : ""}</span>`;
  }
  function renderTasks(h) {
    const tasks = (h.tasks || []).filter(t => t.state !== "delivered");
    $("tasks-count").textContent = tasks.length ? String(tasks.length) : "";
    $("tasks").innerHTML = tasks.map(t => {
      const note = t.latest_note;
      return `<li data-task="${esc(t.id)}" class="task-row">
        <a class="task-title" href="${esc(taskURL(t))}" title="${esc(t.title || t.id)}">${esc(t.title || t.id)}</a>
        <div class="task-meta">${ownerLine(t)}${t.jobs.map(j => jobChip(h, j)).join("")}<span class="task-time">${when(h.now, note?.updated_at || t.updated_at)}</span></div>
        ${note ? `<p class="task-note"><span><b>${esc(speaker(note))}</b>${esc(note.text)}</span></p>` : ""}
      </li>`;
    }).join("") || `<li class="empty-row">没有进行中的任务。<button class="link-btn" type="button" data-new>新建任务</button></li>`;
  }
  function renderReports(h) {
    const reports = (h.tasks || []).filter(t => t.state === "delivered");
    $("reports-count").textContent = reports.length ? String(reports.length) : "";
    $("reports").innerHTML = reports.map(t => `<li data-report="${esc(t.id)}"><a href="${esc(taskURL(t))}" title="${esc(t.title || t.id)}">${esc(t.title || t.id)}</a><span class="report-meta">${esc(t.owner || "无人负责")} · ${when(h.now, t.updated_at)}</span></li>`).join("")
      || `<li class="empty-row">还没有交付报告</li>`;
  }

  // Who holds a phone. A research job names its label, its task and its agent.
  const taskFor = entry => entry.research_id
    ? `${esc(entry.label || entry.job_id)} · <a href="${esc(HGTarget.url(`/dash/research?kind=research&id=${encodeURIComponent(entry.research_id)}`))}" title="${esc(entry.research_title || entry.research_id)}">${esc(entry.research_title || entry.research_id)}</a>`
    : `<a href="${esc(HGTarget.url(`/dash/jobs#job=${encodeURIComponent(entry.job_id)}`))}">${esc(entry.label || entry.job_id)}</a>`;
  function holderLine(kind, entry) {
    const state = entry.state && entry.state !== "running" ? `<span class="tag off">${esc(HGJobState.label({state: entry.state}))}</span>` : "";
    return `<div class="doing" data-kind="${kind}"><span class="doing-k">${kind}</span>${entry.actor_id ? `<span class="who">${avatar(entry.actor_id)}${esc(entry.actor_id)}</span>` : ""}${state}<span class="doing-what">${taskFor(entry)}</span></div>`;
  }
  function phoneStatus(s, job, global) {
    if (!s.online) return `<span class="tag bad">${esc(s.state || "离线")}</span>`;
    if (job) return `<span class="tag acc">${job.reserved ? "已预留" : "使用中"}</span>`;
    if (global) return `<span class="tag acc">${global.reserved ? "全局预留" : "全局锁"}</span>`;
    if (s.holder?.kind === "external") return `<span class="tag warn">外部占用</span>`;
    return s.busy ? `<span class="tag warn">占用</span>` : `<span class="tag ok">空闲</span>`;
  }
  function renderPhones(h) {
    const on = h.slots.filter(Boolean), idle = on.filter(s => s.online !== false && !s.busy).length;
    $("phones").innerHTML = on.map(s => {
      const job = s.holder?.kind === "job" ? s.holder : null, global = s.global_holder;
      const thumb = s.shot_at && HGTarget.canWrite()
        ? `<img alt="${esc(model(s))} 的画面" width="56" height="120" loading="lazy" src="${esc(HGTarget.url(`/dash/api/shot?serial=${encodeURIComponent(s.serial)}&t=${s.shot_at}`))}">`
        : `<span class="ph">${s.busy ? "占用中" : "无画面"}</span>`;
      const doing = (job ? holderLine(job.reserved ? "预留" : "任务", job) : "")
        + (global ? holderLine(global.reserved ? "全局预留" : "全局锁", global) : "")
        + (s.holder?.kind === "external" ? `<div class="doing"><span class="doing-k">外部</span><span class="doing-what">${esc(s.holder.label)}</span></div>`
          : !job && s.goal ? `<div class="doing"><span class="doing-k">目标</span><span class="doing-what">${esc(s.goal.summary)}</span></div>`
          : !job && s.waiting_goal ? `<div class="doing"><span class="doing-k">目标记录</span><span class="doing-what">${esc(s.waiting_goal.summary)} · 设备归属未确认</span></div>`
          : !job && !global && s.busy ? `<div class="doing muted">已占用，未记录占用者</div>` : "");
      const stat = (v, unit, label, hot) => `<div class="pstat${hot ? " hot" : ""}"><b>${v == null ? "—" : v + unit}</b><span>${label}</span></div>`;
      return `<div class="phone-card"><a class="thumb" href="${esc(HGTarget.url(`/dash/live?serial=${encodeURIComponent(s.serial)}`))}" aria-label="看 ${esc(model(s))} 的实时画面">${thumb}</a><div class="meta">
        <div class="phone-head"><span class="name" title="${esc(s.serial)}">${esc(model(s) || "未知型号")}</span>${phoneStatus(s, job, global)}</div>
        <div class="stats">${stat(s.soc_c == null ? null : num(s.soc_c, 0), "°", "芯片", s.soc_c >= 45)}${stat(s.battery_c == null ? null : num(s.battery_c, 0), "°", "电池", s.battery_c >= 40)}${stat(s.battery_pct, "%", "电量", false)}</div>
        ${doing}</div></div>`;
    }).join("") || `<p class="empty-row">${h.fleet_age_s == null ? (h.fleet_err ? "读不到手机：" + esc(h.fleet_err) : "正在读手机状态…") : "现在没有手机接入"}</p>`;
    $("phones-more").textContent = on.length ? `${on.length} 台 · ${idle ? `${idle} 台空闲` : "都在用"}` : "";
  }
  function renderQuestions(h) {
    const questions = h.questions || [];
    $("ask-sec").hidden = !questions.length;
    $("questions-count").textContent = questions.length > 1 ? String(questions.length) : "";
    $("questions").innerHTML = questions.map(q => `<li data-question="${esc(q.id)}" class="q-row">
      <a class="q-text" href="${esc(HGTarget.url(q.url))}">${esc(q.text)}</a>
      <div class="q-meta"><span class="who">${avatar(q.actor_label || "agent")}${esc(q.actor_label || "agent")}</span><span class="q-task" title="${esc(q.research_title || q.research_id)}">${esc(q.research_title || q.research_id)}</span>${when(h.now, q.updated_at)}<a class="btn sm" href="${esc(HGTarget.url(q.url))}">回复</a></div>
    </li>`).join("");
  }
  // Comparisons nobody has judged yet: the agent's question, how many items, and a way to the block on the task page.
  function renderReviews(h) {
    const reviews = h.reviews || [];
    $("review-sec").hidden = !reviews.length;
    $("reviews-count").textContent = reviews.length > 1 ? String(reviews.length) : "";
    $("reviews").innerHTML = reviews.map(r => `<li data-review="${esc(r.id)}" class="q-row">
      <a class="q-text" href="${esc(HGTarget.url(r.url))}"><b>${esc(r.title)}</b>${r.need ? `<span class="q-need">${esc(r.need)}</span>` : ""}</a>
      <div class="q-meta"><span class="who">${avatar(r.actor_label || "agent")}${esc(r.actor_label || "agent")}</span><span class="q-task" title="${esc(r.research_title || r.research_id)}">${esc(r.research_title || r.research_id)}</span>${when(h.now, r.updated_at)}<a class="btn sm" href="${esc(HGTarget.url(r.url))}">去评 · ${r.items} 个</a></div>
    </li>`).join("");
  }
  const DUTY = {answer_note: "回应留言", process_feedback: "处理反馈", unblock_job: "放行任务", post_status: "报状态", plan_directions: "写方向", deliver: "交付"};
  function freshness(now, at) {
    if (!at) return {cls: "off", text: "从未签到"};
    const m = (now - at) / 60;
    return {cls: m < 30 ? "ok" : m < 240 ? "warn" : "off", text: `${ago(now, at)}签到`};
  }
  function renderAgents(h) {
    const agents = h.agents || [];
    $("agents-count").textContent = agents.length ? String(agents.length) : "";
    $("agents").innerHTML = agents.map(a => {
      const f = freshness(h.now, a.checkin_at);
      const owes = [...new Set(a.owes)].map(k => `<span class="tag warn">${esc(DUTY[k] || k)}</span>`).join("");
      const mine = a.research.length ? `<span class="agent-tasks">负责 ${a.research.length} 个任务</span>` : "";
      return `<li data-agent="${esc(a.name)}" class="agent-row">${avatar(a.name)}<div class="agent-body">
        <div class="agent-head"><b class="agent-name" title="${esc(a.name)}">${esc(a.name)}</b><span class="fresh ${f.cls}" title="${esc(a.checkin_at ? full(a.checkin_at) : "")}"><i aria-hidden="true"></i>${esc(f.text)}</span></div>
        ${a.status ? `<p class="agent-status" title="${esc(a.status.text)}">${esc(a.status.text)}</p>` : `<p class="agent-status muted">没报过状态</p>`}
        ${owes || mine ? `<div class="agent-foot">${owes}${mine}</div>` : ""}</div></li>`;
    }).join("") || `<li class="empty-row">还没有注册 agent</li>`;
  }

  // 接入新 agent: one click makes a one-use invitation and shows the whole first prompt to paste into the agent;
  // unused invitations are listed with their expiry and can be revoked.
  async function invites() {
    const list = $("invite-list");
    try {
      const value = await api("/dash/api/agent-invites"), now = Date.now() / 1000, items = value.invites || [];
      $("invite-count").textContent = items.length ? String(items.length) : "";
      list.innerHTML = items.length ? items.sort((a, b) => b.created_at - a.created_at).map(i => `<li data-invite="${esc(i.id)}">
        <span class="mono">${esc(i.id)}</span><span class="muted">${esc(full(i.created_at))} 生成 · ${i.expires_at <= now ? `<b class="expired">已过期</b>` : `${esc(left(i.expires_at - now))}后过期`}${i.note ? " · " + esc(i.note) : ""}</span>
        <button class="btn sm" type="button" data-revoke="${esc(i.id)}">撤销</button></li>`).join("") : `<li class="empty-row">没有</li>`;
    } catch (e) { list.innerHTML = `<li class="empty-row">读不到：${esc(e.message)}</li>`; }
  }
  const left = s => s > 129600 ? `${Math.round(s / 86400)} 天` : s >= 3600 ? `${Math.round(s / 3600)} 小时` : `${Math.max(1, Math.round(s / 60))} 分钟`;
  async function makeInvite() {
    const b = $("invite-make"), m = $("invite-msg");
    if (!(await needSteer(makeInvite))) return;
    b.disabled = true; m.className = "msg"; m.textContent = "生成中…";
    try {
      const v = await api("/dash/api/agent-invites", {hours: 24});
      $("invite-prompt").textContent = v.prompt; $("invite-expires").textContent = `${left(v.expires_at - v.created_at)}内有效`;
      $("invite-copied").textContent = ""; $("invite-new").hidden = false; m.textContent = "";
      if (!$("dlg-invite").open) $("dlg-invite").showModal();
      invites();
    } catch (e) { m.className = "msg err"; m.textContent = "没有生成：" + e.message; }
    finally { b.disabled = false; }
  }
  $("invite-open").addEventListener("click", makeInvite);
  $("invite-make").addEventListener("click", makeInvite);
  $("invite-copy").addEventListener("click", async () => {
    const text = $("invite-prompt").textContent, out = $("invite-copied");
    try { await navigator.clipboard.writeText(text); out.textContent = "已复制"; }
    catch (_) {   // no clipboard (http, older browsers): select it for Ctrl/⌘+C
      const range = document.createRange(); range.selectNodeContents($("invite-prompt"));
      const sel = getSelection(); sel.removeAllRanges(); sel.addRange(range); out.textContent = "已选中，按 Ctrl/⌘+C 复制";
    }
  });
  $("invite-list").addEventListener("click", async ev => {
    const b = ev.target.closest("[data-revoke]"); if (!b) return;
    b.disabled = true; b.textContent = "撤销中…";
    try {
      const r = await HGTarget.fetch("/dash/api/agent-invites/" + encodeURIComponent(b.dataset.revoke), {method: "DELETE", credentials: "same-origin", headers: {"X-HG": "1"}});
      if (!r.ok) { let j = {}; try { j = await r.json(); } catch (_) {} throw new Error(j.error || `HTTP ${r.status}`); }
      await invites();
    } catch (e) { b.disabled = false; b.textContent = "撤销"; b.title = "没有撤销：" + e.message; $("invite-msg").className = "msg err"; $("invite-msg").textContent = "没有撤销：" + e.message; }
  });
  async function needSteer(onReady) {
    if (S.home && S.home.steer) return true;
    S.afterUnlock = onReady || null;
    $("unlock-msg").textContent = ""; $("steer-pw").value = ""; $("dlg-unlock").showModal(); $("steer-pw").focus(); return false;
  }
  $("f-unlock").addEventListener("submit", async ev => {
    ev.preventDefault();
    const button = $("unlock-submit-home");
    if (button.disabled) return;
    button.disabled = true; button.textContent = "解锁中…"; $("unlock-msg").textContent = "";
    try {
      await api("/dash/api/steer/unlock", {pw: $("steer-pw").value}); $("dlg-unlock").close(); await pollHome();
      const action = S.afterUnlock; S.afterUnlock = null; if (action) await action();
    } catch (e) { $("unlock-msg").textContent = e.message; $("steer-pw").select(); }
    finally { button.disabled = false; button.textContent = "解锁"; }
  });
  $("dlg-unlock").addEventListener("close", () => { $("steer-pw").value = ""; });
  $("steer-btn").addEventListener("click", async () => {
    if (!(S.home && S.home.steer)) return needSteer();
    const b = $("steer-btn"); b.disabled = true;
    try { await api("/dash/api/steer/lock", {}); await pollHome(); }
    catch (e) { b.title = "上锁失败：" + e.message; }
    finally { b.disabled = false; }
  });
  async function steerCall(url, body, msgEl) {
    if (!HGTarget.canWrite()) { $(msgEl).textContent = HGTarget.problem() || "此节点未提供目标约束协议，操作已禁用。"; return null; }
    if (!(await needSteer(() => steerCall(url, body, msgEl)))) return null;
    const m = $(msgEl); m.className = "msg"; m.innerHTML = `<span class="spin" aria-hidden="true"></span> 提交中…`;
    try { const r = await api(url, body); m.textContent = r.note || "已保存"; return r; }
    catch (e) { if (e.data && e.data.need_steer) { await pollHome(); needSteer(); } m.className = "msg err"; m.textContent = e.message; return null; }
  }
  function openResearch() {
    const slots = (S.home && S.home.slots || []).filter(s => s && s.online);
    const keep = $("research-device").value;
    $("research-device").innerHTML = `<option value="">不限设备</option>` + slots.map(s => `<option value="${esc(s.serial)}">${esc(s.model || "")} · ${esc(s.serial)}${s.busy ? "（在用）" : ""}</option>`).join("");
    if (keep && slots.some(s => s.serial === keep)) $("research-device").value = keep;
    if (!S.researchDraft) S.researchDraft = {id: crypto.randomUUID(), operationId: crypto.randomUUID()};
    $("research-msg").textContent = ""; $("research-msg").className = "msg";
    if (!$("dlg-research").open) $("dlg-research").showModal();
    $("research-objective").focus();
  }
  $("new-btn").addEventListener("click", async () => { if (await needSteer(openResearch)) openResearch(); });
  $("research-form").addEventListener("submit", async ev => {
    ev.preventDefault();
    if (S.busy) return;
    const objective = $("research-objective").value.trim(), draft = S.researchDraft;
    if (!objective) { $("research-msg").className = "msg err"; $("research-msg").textContent = "先写一句话目标"; $("research-objective").focus(); return; }
    if (!draft) return;
    const game = $("research-game").value, device = $("research-device").value;
    const data = {title: objective.split("\n", 1)[0].slice(0, 240), objective,
      game: game ? {game_id: game, game_version: null} : null, device_requirements: device ? [device] : []};
    S.busy = true; $("research-submit").disabled = true; $("research-submit").textContent = "创建中…";
    try {
      const r = await steerCall(`/dash/api/research-state/research/${encodeURIComponent(draft.id)}`, {
        schema: "phonefarm/research-write@1", operation_id: draft.operationId, base_revision: null,
        data, actor_label: null, reason: null}, "research-msg");
      if (!r) return;
      if (r.object?.id !== draft.id) { $("research-msg").className = "msg err"; $("research-msg").textContent = "回执不属于此任务，请核对后再继续"; return; }
      S.researchDraft = null;
      $("dlg-research").close(); location.href = HGTarget.url(`/dash/research?kind=research&id=${encodeURIComponent(draft.id)}`);
    } finally { S.busy = false; $("research-submit").disabled = !HGTarget.canWrite(); $("research-submit").textContent = "创建任务"; }
  });
  // 中文输入法选词时的回车不提交；Ctrl/⌘ + 回车提交目标
  $("research-objective").addEventListener("keydown", ev => {
    if (ev.isComposing || ev.keyCode === 229) return;
    if (ev.key === "Enter" && (ev.metaKey || ev.ctrlKey)) { ev.preventDefault(); $("research-form").requestSubmit(); }
  });
  document.querySelectorAll("dialog [data-close]").forEach(b => b.addEventListener("click", () => b.closest("dialog").close()));
  document.addEventListener("click", ev => {
    if (ev.target.closest("[data-new]")) $("new-btn").click();
    if (ev.target.closest("[data-retry]")) pollHome();
  });
  document.addEventListener("hgamey-target-change", () => { const button = $("research-submit"); if (button && !S.busy) button.disabled = !HGTarget.canWrite(); });
  function every(ms, fn) {
    let timer = null;
    const tick = async () => { clearTimeout(timer); if (!document.hidden) await fn(); timer = setTimeout(tick, ms); };
    document.addEventListener("visibilitychange", () => { if (!document.hidden) tick(); });
    window.addEventListener("pagehide", () => clearTimeout(timer));
    return tick;
  }
  every(8000, pollHome)();
})();
