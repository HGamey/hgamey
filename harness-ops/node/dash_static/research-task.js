/* Owner, deliverable and discussion thread of a research. */
(() => {
  'use strict';
  const API = '/dash/api';
  let agents = null, shown = null;
  const el = (tag, value, className) => { const item = document.createElement(tag); if (value != null) item.textContent = String(value); if (className) item.className = className; return item; };
  const when = at => new Date(at * 1000).toLocaleString('zh-CN', {hour12: false});
  const speaker = note => note.auth_channel === 'dashboard_steer' ? 'human' : note.actor_label || note.auth_channel;

  async function api(path, body) {
    const init = body === undefined ? {credentials: 'same-origin', cache: 'no-store'}
      : {method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json', 'X-HG': '1'}, body: JSON.stringify(body)};
    const response = await HGTarget.fetch(API + path, init);
    const value = await response.json().catch(() => ({}));
    if (!response.ok) throw Object.assign(new Error(value.error || 'HTTP ' + response.status), {needSteer: value.need_steer === true});
    return value;
  }
  const write = (kind, id, data, base, reason) => api(`/research-state/${kind}/${encodeURIComponent(id)}`,
    {schema: 'phonefarm/research-write@1', operation_id: crypto.randomUUID(), base_revision: base, data, actor_label: null, reason});
  const failure = error => error.needSteer ? '需要先解锁研究操作（掌舵密码）' : error.message;

  function ownerSection(record, ctx) {
    const section = el('section', null, 'research-section'), row = el('div', null, 'research-actions'), status = el('p', null, 'note');
    status.setAttribute('role', 'status');
    section.append(el('h3', '负责人'), row, status);
    row.append(el('span', record.data.owner || '无人负责'));
    if (!ctx.writable()) return section;
    const select = el('select'), button = el('button', '改派', 'ghost');
    select.id = 'task-owner'; button.type = 'button'; button.id = 'task-assign';
    select.append(new Option('无人负责', ''), ...(agents || []).map(agent => new Option(agent.name, agent.name, false, agent.name === record.data.owner)));
    button.onclick = async () => {
      button.disabled = true; status.textContent = '';
      try {
        await write('research', record.id, {...record.data, owner: select.value || null}, record.revision, `改派给 ${select.value || '无人'}`);
        await ctx.reload();
      } catch (error) { status.textContent = failure(error); button.disabled = false; }
    };
    row.append(select, button);
    return section;
  }

  function deliverableSection(record) {
    const value = record.data.deliverable;
    if (!value) return null;
    const section = el('section', null, 'research-section'), list = el('ul', null, 'research-evidence');
    for (const ref of value.artifacts) {
      const item = el('li');
      if (ref.kind === 'job') { const link = el('a', `任务 ${ref.job_id}`); link.href = HGTarget.url(`/dash/jobs#job=${encodeURIComponent(ref.job_id)}`); item.append(link); }
      else if (ref.kind === 'external') item.append(el('span', ref.locator));
      else { const link = el('a', `结果 ${ref.result_id}`); link.href = HGTarget.url(`/dash/results#result=${encodeURIComponent(ref.result_id)}`); item.append(link); }
      list.append(item);
    }
    const scope = [value.applies_to.game_version && `游戏版本 ${value.applies_to.game_version}`, ...value.applies_to.devices].filter(Boolean).join(' · ');
    section.append(el('h3', '交付'), el('p', value.summary), list);
    if (scope) section.append(el('p', scope, 'muted'));
    return section;
  }

  async function threadSection(record, ctx, section) {
    const list = el('div', null, 'discuss-list'), status = el('p', null, 'note');
    status.setAttribute('role', 'status');
    section.append(el('h3', '留言'), list);
    async function load() {
      const value = await api(`/research-state/note?research_id=${encodeURIComponent(record.id)}&limit=100`);
      const notes = value.items.slice().reverse();
      list.replaceChildren(...(notes.length ? notes.map(note => {
        const entry = el('div', null, 'discuss-entry'), head = el('div');
        head.append(el('span', speaker(note), 'discuss-from'), el('span', when(note.updated_at), 'discuss-time'));
        if (note.reply_to) head.append(el('span', `回复 ${note.reply_to}${note.decision ? ` · ${{adopt: '采纳', defer: '暂缓', reject: '不采纳'}[note.decision]}` : ''}`, 'discuss-via'));
        entry.append(head, el('p', note.text));
        entry.dataset.note = note.id;
        return entry;
      }) : [el('div', '暂无留言', 'empty')]));
    }
    try { await load(); } catch (error) { status.textContent = '读取留言失败：' + failure(error); }
    if (ctx.writable()) {
      const form = el('form'), input = el('input'), button = el('button', '发送', 'ghost primary');
      form.id = 'task-note-form'; input.id = 'task-note-text'; input.maxLength = 16384; input.autocomplete = 'off'; input.placeholder = '留言…';
      button.type = 'submit'; button.id = 'task-note-submit';
      form.append(input, button);
      form.onsubmit = async event => {
        event.preventDefault();
        const text = input.value.trim();
        if (!text) return;
        button.disabled = true; status.textContent = '';
        try {
          await write('note', 'n-' + crypto.randomUUID().replace(/-/g, '').slice(0, 12), {research_id: record.id, text, reply_to: null, decision: null, evidence_refs: []}, null, null);
          input.value = ''; await load();
        } catch (error) { status.textContent = '发送失败：' + failure(error); }
        finally { button.disabled = false; }
      };
      section.append(form);
    }
    section.append(status);
  }

  async function renderDetail(container, record, ctx) {
    shown = record;
    container.replaceChildren();
    if (record.kind !== 'research') return;
    if (agents === null) agents = (await api('/agents').catch(() => ({agents: []}))).agents.filter(agent => agent.enabled !== false);
    if (shown !== record) return;
    const thread = el('section', null, 'research-section');
    thread.id = 'task-thread';
    container.append(...[ownerSection(record, ctx), deliverableSection(record), thread].filter(Boolean));
    await threadSection(record, ctx, thread);
  }

  window.HGResearchTask = {renderDetail, clearDetail: container => { shown = null; container.replaceChildren(); }};
})();
