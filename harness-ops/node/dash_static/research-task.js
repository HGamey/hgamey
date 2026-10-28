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
  function imageFigure(ref, alt) {
    const link = el('a'), figure = el('figure', null, 'research-artifact'), image = el('img');
    link.href = HGTarget.url(artifactPath(ref)); link.download = ref.path.split('/').pop();
    image.alt = alt || ref.path; link.append(image); figure.append(link, el('figcaption', `任务 ${ref.job_id} · ${ref.path}`));
    loadImage(image, ref, IMAGE_TYPES[ref.path.split('.').pop().toLowerCase()] || 'application/octet-stream', imageEpoch);
    return figure;
  }
  function evidenceList(refs) {
    const list = el('ul', null, 'research-evidence'), local = HGTarget.binding()?.node_id;
    for (const ref of refs || []) {
      const item = el('li');
      if (ref.kind === 'job' && ref.node_id === local) {
        if (ref.path) {
          const mime = IMAGE_TYPES[ref.path.split('.').pop().toLowerCase()];
          if (mime) item.append(imageFigure(ref, ref.path));
          else {
            const link = el('a', `任务 ${ref.job_id} · ${ref.path}`);
            link.href = HGTarget.url(artifactPath(ref)); link.download = ref.path.split('/').pop(); item.append(link);
          }
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

  function jobImage(value) {
    const match = /^job:([0-9a-f]{12}):(.+)$/.exec(value);
    if (!match) return null;
    const path = match[2];
    if (path.length > 2048 || path.startsWith('/') || path.split('/').some(part => !part || ['.', '..'].includes(part))) return null;
    return {kind: 'job', job_id: match[1], path, node_id: HGTarget.binding()?.node_id};
  }
  function appendInline(parent, value) {
    const pattern = /!\[([^\]]*)\]\((job:[0-9a-f]{12}:[^)]+)\)/g;
    let at = 0, match;
    while ((match = pattern.exec(value))) {
      parent.append(document.createTextNode(value.slice(at, match.index)));
      const ref = jobImage(match[2]);
      if (ref) parent.append(imageFigure(ref, match[1]));
      else parent.append(document.createTextNode(match[0]));
      at = pattern.lastIndex;
    }
    parent.append(document.createTextNode(value.slice(at)));
  }
  const tableCells = line => line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(cell => cell.trim());
  const tableDivider = line => line.includes('|') && tableCells(line).every(cell => /^:?-{3,}:?$/.test(cell.replace(/\s/g, '')));
  const listLine = line => /^\s*([-*+]|\d+\.)\s+(.*)$/.exec(line);
  function markdownReport(source) {
    const article = el('article', null, 'research-report'), lines = String(source).replace(/\r\n?/g, '\n').split('\n');
    let index = 0;
    while (index < lines.length) {
      const line = lines[index];
      if (!line.trim()) { ++index; continue; }
      const heading = /^(#{1,6})\s+(.*)$/.exec(line);
      if (heading) {
        const element = el('h' + heading[1].length); appendInline(element, heading[2]); article.append(element); ++index; continue;
      }
      if (line.trim().startsWith('```')) {
        const code = []; ++index;
        while (index < lines.length && !lines[index].trim().startsWith('```')) code.push(lines[index++]);
        article.append(el('pre', code.join('\n'))); if (index < lines.length) ++index; continue;
      }
      if (index + 1 < lines.length && line.includes('|') && tableDivider(lines[index + 1])) {
        const table = el('table'), head = el('thead'), body = el('tbody'), header = el('tr');
        for (const cell of tableCells(line)) { const th = el('th'); appendInline(th, cell); header.append(th); }
        head.append(header); index += 2;
        while (index < lines.length && lines[index].trim() && lines[index].includes('|')) {
          const row = el('tr');
          for (const cell of tableCells(lines[index])) { const td = el('td'); appendInline(td, cell); row.append(td); }
          body.append(row); ++index;
        }
        table.append(head, body); article.append(table); continue;
      }
      const item = listLine(line);
      if (item) {
        const ordered = /^\d/.test(item[1]), list = el(ordered ? 'ol' : 'ul');
        while (index < lines.length) {
          const current = listLine(lines[index]);
          if (!current || /^\d/.test(current[1]) !== ordered) break;
          const li = el('li'); appendInline(li, current[2]); list.append(li); ++index;
        }
        article.append(list); continue;
      }
      const paragraph = el('p'); appendInline(paragraph, line); article.append(paragraph); ++index;
    }
    return article;
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
    section.id = 'task-report';
    const scope = [value.applies_to.game_version && `游戏版本 ${value.applies_to.game_version}`, ...value.applies_to.devices].filter(Boolean).join(' · ');
    section.append(el('h2', '交付报告'), markdownReport(value.summary), el('h3', '成果文件'), list);
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
    let replyTo = null;
    status.setAttribute('role', 'status');
    section.append(el('h3', '留言'), list);
    async function load() {
      const value = await api(`/research-state/note?research_id=${encodeURIComponent(record.id)}&limit=100`);
      const notes = value.items.slice().reverse(), ids = new Set(notes.map(note => note.id)), children = new Map(), roots = [];
      for (const note of notes) {
        if (note.reply_to && ids.has(note.reply_to)) {
          if (!children.has(note.reply_to)) children.set(note.reply_to, []);
          children.get(note.reply_to).push(note);
        } else roots.push(note);
      }
      function paint(note) {
        const entry = el('div', null, 'discuss-entry'), head = el('div');
        head.append(el('span', speaker(note), 'discuss-from'), el('span', when(note.updated_at), 'discuss-time'));
        if (note.unanswered === true) head.append(el('span', '未回应', 'task-state unknown'));
        if (note.ask) {
          head.append(el('span', '问人', 'discuss-via'));
          if (note.question_unanswered === true) head.append(el('span', '待回复', 'task-state unknown'));
          if (note.question_unanswered === true && ctx.writable()) {
            const reply = el('button', '回复', 'ghost'); reply.type = 'button'; reply.dataset.replyTo = note.id; head.append(reply);
          }
        }
        if (note.reply_to) head.append(el('span', `回复 ${note.reply_to}${note.decision ? ` · ${{adopt: '采纳', defer: '暂缓', reject: '拒绝'}[note.decision]}` : ''}`, 'discuss-via'));
        entry.append(head, el('p', note.text));
        if (note.evidence_refs?.length) entry.append(evidenceList(note.evidence_refs));
        entry.dataset.note = note.id; entry.id = 'note-' + note.id;
        const replies = children.get(note.id) || [];
        if (replies.length) {
          const nested = el('div', null, 'discuss-replies');
          nested.append(...replies.map(paint)); entry.append(nested);
        }
        return entry;
      }
      clearImages();
      list.replaceChildren(...(roots.length ? roots.map(paint) : [el('div', '暂无留言', 'empty')]));
      if (location.hash) document.getElementById(location.hash.slice(1))?.scrollIntoView({block: 'center'});
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
          await write('note', 'n-' + crypto.randomUUID().replace(/-/g, '').slice(0, 12),
                      {research_id: record.id, text, reply_to: replyTo, decision: null, ask: false, evidence_refs: []}, null, null);
          replyTo = null; input.placeholder = '留言…'; input.value = ''; await load();
        } catch (error) { status.textContent = '发送失败：' + failure(error); }
        finally { button.disabled = false; }
      };
      section.append(form);
    }
    section.addEventListener('click', event => {
      const button = event.target.closest('[data-reply-to]');
      if (!button) return;
      replyTo = button.dataset.replyTo;
      const input = document.getElementById('task-note-text');
      if (input) { input.placeholder = '回复 ' + replyTo; input.focus(); }
    });
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
