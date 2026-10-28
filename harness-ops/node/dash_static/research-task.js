/* Owner, deliverable and discussion thread of a research. */
(() => {
  'use strict';
  const API = '/dash/api';
  let agents = null, shown = null, imageEpoch = 0;
  const imageURLs = new Set(), IMAGE_TYPES = {png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', svg: 'image/svg+xml', webp: 'image/webp'};
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
  const artifactPath = ref => `${API}/jobs/${encodeURIComponent(ref.job_id)}/out?${new URLSearchParams({path: ref.path})}`;
  function clearImages() {
    ++imageEpoch;
    for (const url of imageURLs) URL.revokeObjectURL(url);
    imageURLs.clear();
  }
  async function loadImage(image, ref, mime, epoch) {
    try {
      const response = await HGTarget.fetch(artifactPath(ref), {credentials: 'same-origin', cache: 'no-store'});
      if (!response.ok) throw new Error('HTTP ' + response.status);
      const blob = await response.blob();
      if (epoch !== imageEpoch) return;
      const url = URL.createObjectURL(blob.slice(0, blob.size, mime));
      imageURLs.add(url); image.src = url;
    } catch (_) {
      if (epoch === imageEpoch) image.replaceWith(el('span', '图片读取失败', 'note'));
    }
  }
  function evidenceList(refs) {
    const list = el('ul', null, 'research-evidence'), local = HGTarget.binding()?.node_id;
    for (const ref of refs || []) {
      const item = el('li');
      if (ref.kind === 'job' && ref.node_id === local) {
        if (ref.path) {
          const link = el('a'), mime = IMAGE_TYPES[ref.path.split('.').pop().toLowerCase()];
          link.href = HGTarget.url(artifactPath(ref)); link.download = ref.path.split('/').pop();
          if (mime) {
            const figure = el('figure', null, 'research-artifact'), image = el('img');
            image.alt = ref.path; link.append(image); figure.append(link, el('figcaption', `任务 ${ref.job_id} · ${ref.path}`));
            item.append(figure); loadImage(image, ref, mime, imageEpoch);
          } else { link.textContent = `任务 ${ref.job_id} · ${ref.path}`; item.append(link); }
        } else {
          const link = el('a', `任务 ${ref.job_id}`); link.href = HGTarget.url(`/dash/jobs#job=${encodeURIComponent(ref.job_id)}`); item.append(link);
        }
      } else if (ref.kind === 'external') item.append(el('span', ref.locator));
      else if (['result', 'result_source'].includes(ref.kind) && ref.node_id === local) {
        const link = el('a', `结果 ${ref.result_id}${ref.source_name ? ` · 来源 ${ref.source_name}` : ''}`);
        link.href = HGTarget.url(`/dash/results#result=${encodeURIComponent(ref.result_id)}`); item.append(link);
      } else item.append(el('span', `${ref.kind} · ${ref.node_id || '未绑定节点'} · ${ref.job_id || ref.result_id || ''}`));
      list.append(item);
    }
    return list;
  }

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
    const section = el('section', null, 'research-section'), list = evidenceList(value.artifacts);
    const scope = [value.applies_to.game_version && `游戏版本 ${value.applies_to.game_version}`, ...value.applies_to.devices].filter(Boolean).join(' · ');
    section.append(el('h3', '交付'), el('p', value.summary), list);
    if (scope) section.append(el('p', scope, 'muted'));
    return section;
  }

  const JOB_STATE = {running: '运行中', finished: '已结束', lost: '丢失'};
  async function runsSection(record, section) {
    const list = el('ul', null, 'research-evidence');
    section.append(el('h3', '运行'), list);
    try {
      const value = await api(`/jobs?research=${encodeURIComponent(record.id)}&limit=50`);
      list.replaceChildren(...(value.items.length ? value.items.map(job => {
        const item = el('li'), link = el('a', job.label || `${job.bin} ${(job.argv || [])[0] || ''}`.trim());
        link.href = HGTarget.url(`/dash/jobs#job=${encodeURIComponent(job.id)}`);
        const state = job.outcome && job.state === 'finished' ? job.outcome : JOB_STATE[job.state] || job.state;
        item.dataset.job = job.id;
        const facts = [state, job.actor_id, job.started && when(job.started)].filter(Boolean);
        item.append(link, el('span', facts.length ? ' · ' + facts.join(' · ') : '', 'muted'));
        return item;
      }) : [el('li', '暂无运行', 'empty')]));
    } catch (error) { list.replaceChildren(el('li', '读取运行失败：' + failure(error))); }
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
        if (note.evidence_refs?.length) entry.append(evidenceList(note.evidence_refs));
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
    clearImages(); container.replaceChildren();
    if (record.kind !== 'research') return;
    if (agents === null) agents = (await api('/agents').catch(() => ({agents: []}))).agents.filter(agent => agent.enabled !== false);
    if (shown !== record) return;
    const runs = el('section', null, 'research-section'), thread = el('section', null, 'research-section');
    runs.id = 'task-runs'; thread.id = 'task-thread';
    container.append(...[ownerSection(record, ctx), deliverableSection(record), runs, thread].filter(Boolean));
    await Promise.all([runsSection(record, runs), threadSection(record, ctx, thread)]);
  }

  window.HGResearchTask = {renderDetail, clearDetail: container => { shown = null; clearImages(); container.replaceChildren(); }};
})();
