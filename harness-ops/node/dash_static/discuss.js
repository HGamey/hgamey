// 任务详情页的讨论区（AGENT-05）：任务级讨论，不影响执行。
(function () {
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  let currentJid = null;

  async function api(path, body) {
    const opt = body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {};
    const r = await fetch(path, opt);
    if (!r.ok) throw new Error(path + " → HTTP " + r.status);
    return r.json();
  }

  function render(entries) {
    const list = $("discuss-list");
    $("discuss-count").textContent = entries.length ? entries.length + " 条" : "";
    list.innerHTML = entries.length
      ? entries.map(e => `<div class="discuss-entry"><span class="discuss-from">${esc(e.from)}</span><span class="discuss-time">${e.at ? new Date(e.at * 1000).toLocaleString("zh-CN", { hour12: false }) : ""}</span><p>${esc(e.text)}</p></div>`).join("")
      : `<div class="empty"><b>暂无讨论</b>人工和 agent 的留言会显示在这里</div>`;
  }

  async function load(jid) {
    currentJid = jid;
    const result = await api(`/dash/api/jobs/${jid}/discuss`);
    render(result.entries || []);
    const canWrite = window.HGTarget && HGTarget.canWrite && HGTarget.canWrite();
    $("discuss-form").hidden = !canWrite;
  }

  document.addEventListener("hgamey-job-selected", (ev) => {
    if (ev.detail && ev.detail.id) load(ev.detail.id).catch(e => console.error(e));
  });

  document.getElementById("discuss-form")?.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const text = $("discuss-text").value.trim();
    if (!text || !currentJid) return;
    try {
      await api(`/dash/api/jobs/${currentJid}/discuss`, { text, from: "human" });
      $("discuss-text").value = "";
      await load(currentJid);
    } catch (e) {
      $("discuss-message").textContent = "发送失败: " + e.message;
    }
  });
})();
