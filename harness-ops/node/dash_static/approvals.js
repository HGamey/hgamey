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
        method: 'POST', headers: {'Content-Type': 'application/json', 'X-HG': '1'}, body: JSON.stringify({decision})});
      const body = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
      const h = item.category === 'handoff';
      card.replaceChildren(el('p', decision === 'allow' ? `${h ? '已告知完成' : '已允许'}：${item.action}` : `${h ? '已放弃' : '已拒绝'}：${item.action}`));
    } catch (e) {
      card.append(el('p', `提交失败：${e.message}`, 'doctor-error'));
      card.querySelectorAll('button').forEach(b => { b.disabled = false; });
    } finally { busy.delete(item.id); }
  }
  function card(item) {
    const li = el('li', null, 'ap-card' + (item.critical ? ' critical' : ''));
    li.append(el('div', `${item.category_zh}${item.critical ? '（必须人工确认）' : ''}`, 'ap-tag'));
    li.append(el('div', item.action));
    li.append(el('div', `${item.category === 'handoff' ? '说明' : '理由'}：${item.reason}`));
    if (item.goal) li.append(el('div', `任务目标：${item.goal}`, 'ap-meta'));
    li.append(el('div', `设备 ${item.serial || '?'} · 应用 ${item.pkg || '?'} · 作业 ${item.job_id} · 剩 ${left(item)}`, 'ap-meta'));
    if (item.has_screenshot && steer) {
      const img = el('img'); img.alt = '请求时的屏幕截图'; img.loading = 'lazy';
      img.src = `/dash/api/approvals/${item.job_id}/${encodeURIComponent(item.id)}/screenshot`;
      li.append(img);
    }
    const row = el('div', null, 'ap-actions');
    const handoff = item.category === 'handoff';
    const yes = el('button', handoff ? '我做完了' : '允许', 'primary'), no = el('button', handoff ? '不处理' : '拒绝', 'ghost');
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
  // 运行中的任务: 卡片常驻(输入框里没发出的字不被刷新冲掉), 只更新留言列表
  const jobsShown = new Map();
  function msgLine(m) {
    const read = m.read_at_step == null ? '待读取' : `第 ${m.read_at_step} 步已读`;
    return el('li', `${m.text}（${m.by} · ${read}）`);
  }
  function jobCard(job) {
    const li = el('li', null, 'ap-card');
    li.append(el('div', job.label || job.job_id, 'ap-tag'));
    li.append(el('div', job.argv, 'ap-meta'));
    li.append(el('div', `作业 ${job.job_id}`, 'ap-meta'));
    const msgs = el('ul', null, 'st-msgs');
    const form = el('form', null, 'st-form');
    const input = el('input'); input.maxLength = 500; input.placeholder = '留言…'; input.setAttribute('aria-label', `给 ${job.label || job.job_id} 留言`);
    const send = el('button', '发送', 'primary'); send.type = 'submit';
    const err = el('p', null, 'doctor-error'); err.hidden = true;
    form.append(input, send);
    form.onsubmit = async ev => {
      ev.preventDefault();
      const text = input.value.trim();
      if (!text || !steer) return;
      send.disabled = true; err.hidden = true;
      try {
        const r = await fetch(`/dash/api/steer/${job.job_id}`, {method: 'POST',
          headers: {'Content-Type': 'application/json', 'X-HG': '1'}, body: JSON.stringify({text})});
        const body = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
        input.value = ''; msgs.append(msgLine(body));
      } catch (e) { err.textContent = `发送失败：${e.message}`; err.hidden = false; }
      finally { send.disabled = !steer; }
    };
    li.append(msgs, form, err);
    return {li, msgs, input, send, sig: ''};
  }
  async function loadJobs() {
    try {
      const r = await fetch('/dash/api/steer', {cache: 'no-store'});
      if (!r.ok) return;
      const body = await r.json();
      const items = Array.isArray(body.items) ? body.items : [];
      $('st-status').textContent = items.length ? '' : '现在没有能留言的运行中任务（只有模型驱动的 run 任务会读留言）。';
      const live = new Set(items.map(j => j.job_id));
      for (const [id, c] of jobsShown) if (!live.has(id)) { c.li.remove(); jobsShown.delete(id); }
      for (const job of items) {
        let c = jobsShown.get(job.job_id);
        if (!c) { c = jobCard(job); jobsShown.set(job.job_id, c); $('st-list').append(c.li); }
        c.input.disabled = c.send.disabled = !body.steer;
        const sig = (job.messages || []).map(m => `${m.id}:${m.read_at_step}`).join(',');
        if (sig !== c.sig) { c.sig = sig; c.msgs.replaceChildren(...(job.messages || []).map(msgLine)); }
      }
    } catch (e) { $('st-status').textContent = `读取运行中任务失败：${e.message}`; }
  }
  load(); loadJobs(); setInterval(load, 3000); setInterval(loadJobs, 3000);
})();
