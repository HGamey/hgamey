// HGamey 首页：任务、手机占用、Agent、待回复问题与交付报告。
(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const num = (v, d = 1) => v == null || Number.isNaN(+v) ? "—" : (+v).toFixed(d);
  const toDate = t => typeof t === "number" ? new Date(t * 1000) : new Date(t);
  const hm = t => { const d = toDate(t); return isNaN(d) ? "" : d.toLocaleTimeString("zh-CN", {hour: "2-digit", minute: "2-digit", hour12: false}); };
  const S = {home: null, researchDraft: null, afterUnlock: null};

  async function api(url, body) {
    const opt = body === undefined ? {cache: "no-store", credentials: "same-origin"}
      : {method: "POST", credentials: "same-origin", headers: {"Content-Type": "application/json", "X-HG": "1"}, body: JSON.stringify(body)};
    const r = await HGTarget.fetch(url, opt);
    if (r.status === 401) { location.href = "/dash/login"; throw new Error("401"); }
    let j = {}; try { j = await r.json(); } catch (_) { /* An unknown write must not be retried automatically. */ }
    if (!r.ok) { const error = new Error(j.error || `HTTP ${r.status}`); error.data = j; error.status = r.status; throw error; }
    return j;
  }
  async function pollHome() {
    try {
      const h = await api("/dash/api/home"); S.home = h;
      $("live").className = "live ok"; $("live-text").textContent = `实时 · ${hm(h.now)}`;
      renderTasks(h); renderPhones(h); renderAgents(h); renderQuestions(h); renderReports(h); renderSteerBtn(h);
    } catch (_) { $("live").className = "live bad"; $("live-text").textContent = "连不上手机节点"; }
  }
  function renderSteerBtn(h) {
    const b = $("steer-btn"); b.textContent = h.steer ? "操作：已解锁" : "操作：未解锁";
    b.title = h.steer ? "点一下重新上锁" : "输入操作密码";
  }

  const taskLink = t => `<a href="${esc(HGTarget.url(t.report_url || `/dash/research?kind=research&id=${encodeURIComponent(t.id)}`))}">${esc(t.title || t.id)}</a>`;
  const taskJobs = t => t.jobs.map(j => `${esc(j.label || j.job_id)}（${esc(HGJobState.label(j.public || j))}）`).join("、");
  function phoneNames(h, t) {
    const ids = new Set(t.jobs.map(j => j.job_id));
    return h.slots.filter(s => s && ids.has(s.holder?.job_id)).map(s => s.serial);
  }
  function renderTasks(h) {
    const tasks = (h.tasks || []).filter(t => t.state !== "delivered");
    $("tasks").innerHTML = tasks.map(t => {
      const phones = phoneNames(h, t);
      return `<li data-task="${esc(t.id)}">${taskLink(t)} · ${esc(t.owner || "无人负责")}${t.jobs.length ? " · " + taskJobs(t) : ""}${phones.length ? " · 手机 " + esc(phones.join("、")) : ""} · ${esc(hm(t.updated_at))}</li>`;
    }).join("") || `<li>没有进行中的任务</li>`;
  }
  function renderReports(h) {
    const reports = (h.tasks || []).filter(t => t.state === "delivered");
    $("reports").innerHTML = reports.map(t => `<li data-report="${esc(t.id)}">${taskLink(t)} · 已交付 · ${esc(t.owner || "无人负责")} · ${esc(hm(t.updated_at))}</li>`).join("")
      || `<li>还没有交付报告</li>`;
  }
  function renderPhones(h) {
    const on = h.slots.filter(Boolean), free = h.slots.length - on.length;
    $("phones").innerHTML = on.map(s => {
      const cls = !s.online ? "off" : s.busy ? "busy" : "on";
      const st = !s.online ? (s.state || "离线") : s.busy ? "在线 · 占用" : "在线 · 锁记录空闲";
      const thumb = s.shot_at && HGTarget.canWrite() ? `<img alt="${esc(s.model || s.serial)} 的画面" src="${esc(HGTarget.url(`/dash/api/shot?serial=${encodeURIComponent(s.serial)}&t=${s.shot_at}`))}">`
        : `<div class="ph">${s.busy ? "占用中，暂缓截图" : "暂无画面"}</div>`;
      const job = ["job", "global"].includes(s.holder?.kind) ? s.holder : null;
      const task = job && (job.research_id
        ? `${esc(job.label || job.job_id)} · <a href="${esc(HGTarget.url(`/dash/research?kind=research&id=${encodeURIComponent(job.research_id)}`))}">${esc(job.research_title || job.research_id)}</a>`
        : `<a href="${esc(HGTarget.url(`/dash/jobs#job=${encodeURIComponent(job.job_id)}`))}">${esc(job.label || job.job_id)}</a>`);
      const doing = job ? `<div class="doing${job.kind === "job" ? " on" : ""}">${job.kind === "job" ? "任务" : job.reserved ? "全局预留" : "全局锁"}：${task}${job.actor_id ? " · " + esc(job.actor_id) : ""}${job.state && job.state !== "running" ? " · " + esc(job.state) : ""}</div>`
        : s.holder?.kind === "external" ? `<div class="doing on">外部占用：${esc(s.holder.label)}</div>`
        : s.goal ? `<div class="doing on">关联目标：${esc(s.goal.summary)}</div>`
        : s.waiting_goal ? `<div class="doing">目标记录：${esc(s.waiting_goal.summary)} · 设备归属未确认</div>`
        : `<div class="doing">${s.busy ? "已占用，未记录占用者" : "未记录占用；运行前仍需调度与租约核验"}</div>`;
      return `<div class="phone-card"><a class="thumb" href="/dash/live?serial=${encodeURIComponent(s.serial)}" title="看实时画面">${thumb}</a><div class="meta">
        <div class="name" title="${esc(s.serial)}">${esc(s.model || "未知型号")}</div><span class="status ${cls}"><i></i>${esc(st)}</span>
        <div class="nums"><div><b>${s.soc_c == null ? "—" : num(s.soc_c, 0) + "°"}</b><span>芯片温度</span></div><div><b>${s.battery_pct == null ? "—" : s.battery_pct + "%"}</b><span>电量</span></div><div><b>${s.battery_c == null ? "—" : num(s.battery_c, 0) + "°"}</b><span>电池温度</span></div></div>
        ${doing}</div></div>`;
    }).join("") || `<p class="phones-more">${h.fleet_age_s == null ? (h.fleet_err ? "读不到手机：" + esc(h.fleet_err) : "正在读手机状态…") : "现在没有手机接入"}</p>`;
    $("phones-more").textContent = free > 0 ? `还可以接 ${free} 台手机` : "";
  }
  function renderQuestions(h) {
    const questions = h.questions || [];
    $("questions").innerHTML = questions.map(q => `<li data-question="${esc(q.id)}"><a href="${esc(HGTarget.url(q.url))}">${esc(q.text)}</a> · ${esc(q.research_title || q.research_id)} · ${esc(q.actor_label || "agent")} · ${esc(hm(q.updated_at))}</li>`).join("")
      || `<li>没有待回复问题</li>`;
  }
  const DUTY = {answer_note: "回应留言", process_feedback: "处理反馈", unblock_job: "放行任务", post_status: "报状态", plan_directions: "写方向", deliver: "交付"};
  const since = (now, at) => {
    if (!at) return "从未签到";
    const m = Math.max(0, Math.round((now - at) / 60));
    return m < 1 ? "刚签到" : m < 60 ? `${m} 分钟前签到` : `${Math.round(m / 60)} 小时前签到`;
  };
  function renderAgents(h) {
    const agents = h.agents || [];
    $("agents").innerHTML = agents.map(a => {
      const owes = a.owes.length ? "欠：" + [...new Set(a.owes)].map(k => DUTY[k] || k).join("、") : "没有待办";
      const doing = a.status ? `${esc(a.status.text)}（${esc(hm(a.status.at))}）` : "没报过状态";
      const mine = a.research.length ? " · 负责 " + a.research.map(id => taskLink({id})).join("、") : "";
      return `<li data-agent="${esc(a.name)}"><b>${esc(a.name)}</b> · ${esc(since(h.now, a.checkin_at))} · ${doing} · ${esc(owes)}${mine}</li>`;
    }).join("") || `<li>还没有注册 agent</li>`;
  }

  async function needSteer(onReady) {
    if (S.home && S.home.steer) return true;
    S.afterUnlock = onReady || null;
    $("unlock-msg").textContent = ""; $("steer-pw").value = ""; $("dlg-unlock").showModal(); $("steer-pw").focus(); return false;
  }
  $("f-unlock").addEventListener("submit", async ev => {
    ev.preventDefault();
    try {
      await api("/dash/api/steer/unlock", {pw: $("steer-pw").value}); $("dlg-unlock").close(); await pollHome();
      const action = S.afterUnlock; S.afterUnlock = null; if (action) await action();
    } catch (e) { $("unlock-msg").textContent = e.message; }
  });
  $("steer-btn").addEventListener("click", async () => {
    if (S.home && S.home.steer) { await api("/dash/api/steer/lock", {}); await pollHome(); } else needSteer();
  });
  async function steerCall(url, body, msgEl) {
    if (!HGTarget.canWrite()) { $(msgEl).textContent = HGTarget.problem() || "此节点未提供目标约束协议，操作已禁用。"; return null; }
    if (!(await needSteer(() => steerCall(url, body, msgEl)))) return null;
    const m = $(msgEl); m.className = "msg"; m.innerHTML = `<span class="spin"></span> 提交中`;
    try { const r = await api(url, body); m.textContent = r.note || "已保存"; return r; }
    catch (e) { if (e.data && e.data.need_steer) { await pollHome(); needSteer(); } m.className = "msg err"; m.textContent = e.message; return null; }
  }
  function openResearch() {
    const slots = (S.home && S.home.slots || []).filter(s => s && s.online);
    $("research-device").innerHTML = `<option value="">不限设备</option>` + slots.map(s => `<option value="${esc(s.serial)}">${esc(s.model || "")} · ${esc(s.serial)}${s.busy ? "（现在有人在用）" : ""}</option>`).join("");
    S.researchDraft = {id: crypto.randomUUID(), operationId: crypto.randomUUID()}; $("research-msg").textContent = "";
    if (!$("dlg-research").open) $("dlg-research").showModal();
  }
  $("new-btn").addEventListener("click", async () => { if (await needSteer(openResearch)) openResearch(); });
  $("research-form").addEventListener("submit", async ev => {
    ev.preventDefault();
    const objective = $("research-objective").value.trim(), draft = S.researchDraft;
    if (!objective) { $("research-msg").textContent = "先写一句话"; return; }
    if (!draft) return;
    const game = $("research-game").value, device = $("research-device").value;
    const data = {title: objective.split("\n", 1)[0].slice(0, 240), objective,
      game: game ? {game_id: game, game_version: null} : null, device_requirements: device ? [device] : []};
    const r = await steerCall(`/dash/api/research-state/research/${encodeURIComponent(draft.id)}`, {
      schema: "phonefarm/research-write@1", operation_id: draft.operationId, base_revision: null,
      data, actor_label: null, reason: null}, "research-msg");
    if (!r) return;
    if (r.object?.id !== draft.id) { $("research-msg").textContent = "回执不属于此任务，请核对后再继续"; return; }
    $("dlg-research").close(); location.href = HGTarget.url(`/dash/research?kind=research&id=${encodeURIComponent(draft.id)}`);
  });
  document.querySelectorAll("dialog [data-close]").forEach(b => b.addEventListener("click", () => b.closest("dialog").close()));
  document.addEventListener("hgamey-target-change", () => { const button = $("research-submit"); if (button) button.disabled = !HGTarget.canWrite(); });
  function every(ms, fn) {
    let timer = null;
    const tick = async () => { if (!document.hidden) await fn(); timer = setTimeout(tick, ms); };
    document.addEventListener("visibilitychange", () => { if (!document.hidden) { clearTimeout(timer); tick(); } }); return tick;
  }
  (async () => { await pollHome(); every(8000, pollHome)(); })();
})();
