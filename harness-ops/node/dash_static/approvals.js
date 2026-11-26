/* Pending sensitive-action approvals raised by phonefarm jobs. Deciding needs the steer password. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const el = (tag, text, cls) => { const e = document.createElement(tag); if (text != null) e.textContent = text; if (cls) e.className = cls; return e; };
  let steer = false, busy = new Set(), shown = '';
  function left(item) {
    const s = Math.max(0, Math.round((item.expires_at || 0) - Date.now() / 1000));
    return s >= 60 ? `${Math.floor(s / 60)} 分 ${s % 60} 秒` : `${s} 秒`;
  }
  async function decide(item, decision, card) {
    if (busy.has(item.id)) return;
    busy.add(item.id);
    card.querySelectorAll('button').forEach(b => { b.disabled = true; });
    try {
      const r = await fetch(`/dash/api/approvals/${item.job_id}/${encodeURIComponent(item.id)}`, {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({decision})});
      const body = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
      card.replaceChildren(el('p', decision === 'allow' ? `已允许：${item.action}` : `已拒绝：${item.action}`));
    } catch (e) {
      card.append(el('p', `提交失败：${e.message}`, 'doctor-error'));
      card.querySelectorAll('button').forEach(b => { b.disabled = false; });
    } finally { busy.delete(item.id); }
  }
  function card(item) {
    const li = el('li', null, 'ap-card' + (item.critical ? ' critical' : ''));
    li.append(el('div', `${item.category_zh}${item.critical ? '（必须人工确认）' : ''}`, 'ap-tag'));
    li.append(el('div', item.action));
    li.append(el('div', `理由：${item.reason}`));
    if (item.goal) li.append(el('div', `任务目标：${item.goal}`, 'ap-meta'));
    li.append(el('div', `设备 ${item.serial || '?'} · 应用 ${item.pkg || '?'} · 作业 ${item.job_id} · 剩 ${left(item)}`, 'ap-meta'));
    if (item.has_screenshot) {
      const img = el('img'); img.alt = '请求时的屏幕截图'; img.loading = 'lazy';
      img.src = `/dash/api/approvals/${item.job_id}/${encodeURIComponent(item.id)}/screenshot`;
      li.append(img);
    }
    const row = el('div', null, 'ap-actions');
    const yes = el('button', '允许', 'primary'), no = el('button', '拒绝', 'ghost');
    yes.type = no.type = 'button';
    yes.disabled = no.disabled = !steer;
    yes.onclick = () => decide(item, 'allow', li);
    no.onclick = () => decide(item, 'deny', li);
    row.append(yes, no); li.append(row);
    return li;
  }
  async function load() {
    if (busy.size) return;
    try {
      const r = await fetch('/dash/api/approvals', {cache: 'no-store'});
      if (r.status === 401) { location.href = '/dash/login'; return; }
      const body = await r.json();
      steer = !!body.steer;
      $('ap-steer').hidden = steer;
      const items = Array.isArray(body.items) ? body.items : [];
      $('ap-status').textContent = items.length ? `${items.length} 条待审批` : '现在没有待审批的动作。';
      const sig = steer + '|' + items.map(i => i.id).join(',');
      if (sig !== shown) { shown = sig; $('ap-list').replaceChildren(...items.map(card)); }
    } catch (e) { $('ap-status').textContent = `读取失败：${e.message}`; }
  }
  load(); setInterval(load, 3000);
})();
