// 任务详情页的讨论区（AGENT-05）：任务级讨论，不影响执行。
// 页面发言一律记为 human（服务端按认证通道决定）；token 发言显示其自报的 agent 名。
(function () {
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  let currentJid = null;

  async function api(path, body) {
    const init = body === undefined ? { credentials: "same-origin", cache: "no-store" }
      : { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json", "X-HG": "1" }, body: JSON.stringify(body) };
    const r = await (window.HGTarget ? HGTarget.fetch(path, init) : fetch(path, init));
    const value = await r.json().catch(() => ({}));
    if (!r.ok) throw Object.assign(new Error(value.error || "HTTP " + r.status), { status: r.status, needSteer: value.need_steer === true });
    return value;
  }

  function speaker(e) {
    const via = e.auth_channel === "admin_token" ? '<span class="discuss-via">token</span>' : "";
    return `<span class="discuss-from">${esc(e.from)}</span>${via}`;
  }

  function render(entries) {
    const list = $("discuss-list");
    $("discuss-count").textContent = entries.length ? entries.length + " 条" : "";
    list.innerHTML = entries.length
      ? entries.map(e => `<div class="discuss-entry">${speaker(e)}<span class="discuss-time">${e.at ? new Date(e.at * 1000).toLocaleString("zh-CN", { hour12: false }) : ""}</span><p>${esc(e.text)}</p></div>`).join("")
      : `<div class="empty"><b>暂无讨论</b>人工和 agent 的留言会显示在这里</div>`;
  }

  function showForm() {
    $("discuss-form").hidden = !(currentJid && window.HGTarget && HGTarget.canWrite());
  }

  async function load(jid) {
    currentJid = jid;
    $("discuss-message").textContent = "";
    showForm();
    try {
      render((await api(`/dash/api/jobs/${jid}/discuss`)).entries || []);
    } catch (e) {
      if (jid === currentJid) $("discuss-message").textContent = "读取讨论失败: " + e.message;
    }
  }

  document.addEventListener("hgamey-job-selected", ev => { if (ev.detail && ev.detail.id) load(ev.detail.id); });
  document.addEventListener("hgamey-target-change", showForm);

  $("discuss-form")?.addEventListener("submit", async ev => {
    ev.preventDefault();
    const text = $("discuss-text").value.trim();
    if (!text || !currentJid) return;
    const button = $("discuss-submit");
    button.disabled = true;
    $("discuss-message").textContent = "";
    try {
      await api(`/dash/api/jobs/${currentJid}/discuss`, { text });
      $("discuss-text").value = "";
      await load(currentJid);
    } catch (e) {
      $("discuss-message").textContent = e.needSteer ? "需要先解锁任务操作（掌舵密码）再发言" : "发送失败: " + e.message;
    } finally {
      button.disabled = false;
    }
  });
})();
