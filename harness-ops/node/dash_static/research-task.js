/* Owner, deliverable and discussion thread of a research. */
(() => {
  'use strict';
  const API = '/dash/api';
  let agents = null, shown = null, assigned = null, imageEpoch = 0;
  const imageURLs = new Set(), IMAGE_TYPES = {png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', svg: 'image/svg+xml', webp: 'image/webp'};
  const VIDEO_TYPES = {mp4: 'video/mp4', webm: 'video/webm'}, extension = path => path.split('.').pop().toLowerCase();
  const el = (tag, value, className) => { const item = document.createElement(tag); if (value != null) item.textContent = String(value); if (className) item.className = className; return item; };
  const FULL = new Intl.DateTimeFormat('zh-CN', {dateStyle: 'medium', timeStyle: 'medium', hour12: false});
  const SHORT = new Intl.DateTimeFormat('zh-CN', {month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false});
  const when = at => FULL.format(new Date(at * 1000)), short = at => SHORT.format(new Date(at * 1000));
  const speaker = note => note.auth_channel === 'dashboard_steer' ? '人工' : note.auth_channel === 'admin_token' ? note.actor_label || '管理员' : note.actor_label || note.auth_channel;

  async function api(path, body) {
    // A write keeps going if the page is left while it is in flight (the browser caps keepalive bodies at 64 KiB).
    const data = body === undefined ? null : JSON.stringify(body);
    const init = data === null ? {credentials: 'same-origin', cache: 'no-store'}
      : {method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json', 'X-HG': '1'}, body: data,
         keepalive: new TextEncoder().encode(data).length < 60000};
    const response = await HGTarget.fetch(API + path, init);
    const value = await response.json().catch(() => ({}));
    if (!response.ok) throw Object.assign(new Error(value.error || 'HTTP ' + response.status), {needSteer: value.need_steer === true, code: value.code});
    return value;
  }
  const write = (kind, id, data, base, reason) => api(`/research-state/${kind}/${encodeURIComponent(id)}`,
    {schema: 'phonefarm/research-write@1', operation_id: crypto.randomUUID(), base_revision: base, data, actor_label: null, reason});
  const failure = error => error.needSteer ? '需要先解锁研究操作（掌舵密码）' : error.message;
  const artifactPath = ref => `${API}/jobs/${encodeURIComponent(ref.job_id)}/out?${new URLSearchParams({path: ref.path})}`;
  const attachmentPath = ref => `${API}/research-attachments/${encodeURIComponent(ref.attachment_id)}`;
  function clearImages() {
    ++imageEpoch;
    for (const url of imageURLs) URL.revokeObjectURL(url);
    imageURLs.clear();
  }
  async function loadImage(image, ref, mime, epoch, sourcePath = artifactPath(ref)) {
    try {
      const response = await HGTarget.fetch(sourcePath, {credentials: 'same-origin', cache: 'no-store'});
      if (!response.ok) throw new Error('HTTP ' + response.status);
      const blob = await response.blob();
      if (epoch !== imageEpoch) return;
      const url = URL.createObjectURL(blob.slice(0, blob.size, mime));
      imageURLs.add(url); image.src = url;
    } catch (_) {
      if (epoch === imageEpoch) image.replaceWith(el('span', '图片读取失败', 'note'));
    }
  }
  function imageFigure(ref, alt, sourcePath = artifactPath(ref)) {
    const link = el('a'), figure = el('figure', null, 'research-artifact'), image = el('img');
    const filename = ref.filename || ref.path.split('/').pop();
    link.href = HGTarget.url(sourcePath); link.download = filename;
    image.alt = alt || filename; link.append(image); figure.append(link, el('figcaption', ref.kind === 'attachment' ? filename : `任务 ${ref.job_id} · ${ref.path}`));
    const mime = ref.content_type || IMAGE_TYPES[ref.path.split('.').pop().toLowerCase()] || 'application/octet-stream';
    loadImage(image, ref, mime, imageEpoch, sourcePath);
    return figure;
  }
  // Videos stream from their URL (the player seeks with ranges); nothing is read until the player asks for it.
  function videoFigure(ref, sourcePath = artifactPath(ref)) {
    const figure = el('figure', null, 'research-artifact video'), video = el('video'), link = el('a');
    const filename = ref.filename || ref.path.split('/').pop();
    Object.assign(video, {controls: true, preload: 'metadata', playsInline: true});
    video.src = HGTarget.url(sourcePath);
    video.onerror = () => video.replaceWith(el('span', '视频读取失败', 'note'));
    link.textContent = ref.kind === 'attachment' ? filename : `任务 ${ref.job_id} · ${ref.path}`;
    link.href = HGTarget.url(sourcePath); link.download = filename;
    const caption = el('figcaption'); caption.append(link);
    figure.append(video, caption);
    return figure;
  }
  function evidenceList(refs) {
    const list = el('ul', null, 'research-evidence'), local = HGTarget.binding()?.node_id;
    for (const ref of refs || []) {
      const item = el('li');
      if (ref.kind === 'job' && ref.node_id === local) {
        if (ref.path) {
          const mime = IMAGE_TYPES[extension(ref.path)];
          if (mime) item.append(imageFigure(ref, ref.path));
          else if (VIDEO_TYPES[extension(ref.path)]) item.append(videoFigure(ref));
          else {
            const link = el('a', `任务 ${ref.job_id} · ${ref.path}`);
            link.href = HGTarget.url(artifactPath(ref)); link.download = ref.path.split('/').pop(); item.append(link);
          }
        } else {
          const link = el('a', `任务 ${ref.job_id}`); link.href = HGTarget.url(`/dash/jobs#job=${encodeURIComponent(ref.job_id)}`); item.append(link);
        }
      } else if (ref.kind === 'attachment' && ref.node_id === local) {
        item.append(/^video\//.test(ref.content_type || '') ? videoFigure(ref, attachmentPath(ref)) : imageFigure(ref, ref.filename, attachmentPath(ref)));
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
  // `code` and **bold** as elements built from text nodes; nothing from the report becomes markup.
  function appendText(parent, value) {
    const pattern = /`([^`]+)`|\*\*([^*]+)\*\*/g;
    let at = 0, match;
    while ((match = pattern.exec(value))) {
      parent.append(document.createTextNode(value.slice(at, match.index)));
      parent.append(match[1] != null ? el('code', match[1]) : el('strong', match[2]));
      at = pattern.lastIndex;
    }
    parent.append(document.createTextNode(value.slice(at)));
  }
  function appendInline(parent, value) {
    const pattern = /!\[([^\]]*)\]\((job:[0-9a-f]{12}:[^)]+)\)/g;
    let at = 0, match;
    while ((match = pattern.exec(value))) {
      appendText(parent, value.slice(at, match.index));
      const ref = jobImage(match[2]);
      if (ref) parent.append(VIDEO_TYPES[extension(ref.path)] ? videoFigure(ref) : imageFigure(ref, match[1]));
      else parent.append(document.createTextNode(match[0]));
      at = pattern.lastIndex;
    }
    appendText(parent, value.slice(at));
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

  const avatar = (name, human = false) => {
    const item = el('span', human ? '人' : (Array.from(String(name || '?').replace(/^[^\p{L}\p{N}]+/u, ''))[0] || '?').toUpperCase(), human ? 'av human' : 'av');
    item.setAttribute('aria-hidden', 'true'); return item;
  };
  const tag = (text, tone = '') => el('span', text, ('tag ' + tone).trim());
  const icon = d => { const span = el('span', null, 'ic'); span.innerHTML = `<svg aria-hidden="true" viewBox="0 0 16 16" width="14" height="14"><path d="${d}" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>`; return span; };
  const timeEl = at => { const t = el('time', at ? short(at) : '', 'muted'); if (at) { t.dateTime = new Date(at * 1000).toISOString(); t.title = when(at); } return t; };

  function ownerSection(record, ctx) {
    const section = el('section', null, 'research-section task-owner'), row = el('div', null, 'owner-row'), status = el('p', null, 'note');
    status.setAttribute('role', 'status');
    section.append(el('h3', '负责人'), row, status);
    const owner = record.data.owner, who = el('span', null, owner ? 'who' : 'who nobody');
    who.append(avatar(owner), el('span', owner || '无人负责'));
    row.append(who);
    if (record.data.deliverable) {
      const jump = el('a', '查看交付报告', 'owner-jump'); jump.href = '#task-report'; row.append(jump);
    }
    // The outcome of the last click outlives the repaint that follows it, and is judged on the owner just read back.
    const outcome = assigned?.id === record.id && record.revision >= assigned.revision ? assigned : null;
    if (outcome) {
      assigned = null;
      const ok = outcome.chosen === undefined || outcome.chosen === (owner || null);
      status.textContent = outcome.text || (ok ? (owner ? `已改派给 ${owner}` : '已改为无人负责') : `负责人现在是 ${owner || '无人负责'}，请再确认一次`);
      status.classList.toggle('task-error', !ok || !!outcome.bad);
    }
    if (!ctx.writable()) return section;
    const select = el('select'), button = el('button', '改派', 'ghost');
    select.id = 'task-owner'; select.setAttribute('aria-label', '改派给'); button.type = 'button'; button.id = 'task-assign';
    select.append(new Option('无人负责', ''), ...(agents || []).map(agent => new Option(agent.name, agent.name, false, agent.name === owner)));
    if (owner && select.value !== owner) select.append(new Option(owner, owner, false, true));
    // Only a choice that differs from the saved owner can be sent, so a changed select never passes for a saved one.
    const sync = () => { button.disabled = (select.value || null) === (owner || null); };
    select.onchange = sync; sync();
    button.onclick = async () => {
      const chosen = select.value || null;
      select.disabled = button.disabled = true; button.textContent = '改派中…'; status.textContent = ''; status.classList.remove('task-error');
      try {
        const saved = await write('research', record.id, {...record.data, owner: chosen}, record.revision, `改派给 ${chosen || '无人'}`);
        assigned = {id: record.id, revision: saved.object.revision, chosen};
      } catch (error) {
        if (error.code !== 'research_revision_conflict') {
          status.textContent = '没有改派：' + failure(error); status.classList.add('task-error');
          select.disabled = false; button.textContent = '改派'; sync(); return;
        }
        assigned = {id: record.id, revision: record.revision + 1, text: '没有改派：任务刚被别人改过，已重新读取，请再选一次', bad: true};
      }
      if (shown?.record.id === record.id) await ctx.reload();
    };
    const assign = el('div', null, 'owner-assign'); assign.append(select, button); row.append(assign);
    return section;
  }

  function deliverableSection(record) {
    const value = record.data.deliverable;
    if (!value) return null;
    const section = el('section', null, 'research-section task-report'), list = evidenceList(value.artifacts);
    section.id = 'task-report';
    const head = el('div', null, 'section-head');
    head.append(el('h2', '交付报告'), tag('已交付', 'ok'));
    const scope = [value.applies_to.game_version && `游戏版本 ${value.applies_to.game_version}`, ...value.applies_to.devices].filter(Boolean).join(' · ');
    section.append(head);
    if (scope) section.append(el('p', '适用：' + scope, 'report-scope'));
    section.append(markdownReport(value.summary), el('h3', `成果文件 · ${value.artifacts.length}`), list);
    return section;
  }

  const DIRECTION_STATE = {proposed: '待探索', preparing: '准备中', ready: '可开展', running: '进行中', waiting_device: '等待设备',
    waiting_human: '等待协助', waiting_material: '等待素材', awaiting_evaluation: '等待评价', verified: '已验证', no_gain: '无收益', rejected: '已排除', deferred: '已暂缓'};
  const DIRECTION_TONE = {running: 'acc', ready: 'acc', preparing: 'acc', waiting_device: 'warn', waiting_human: 'warn', waiting_material: 'warn',
    awaiting_evaluation: 'warn', verified: 'ok', no_gain: 'off', rejected: 'off', deferred: 'off', proposed: 'off'};
  const RESULT_OUTCOME = {gain: ['有收益', 'ok'], no_gain: ['无收益', 'off'], regression: ['退步', 'bad'], failed: ['失败', 'bad'], not_run: ['未运行', 'off'], unknown: ['未知', 'warn']};
  function runRow(job) {
    const item = el('li', null, 'run-row'), link = el('a', job.label || `${job.bin} ${(job.argv || [])[0] || ''}`.trim());
    link.href = HGTarget.url(`/dash/jobs#job=${encodeURIComponent(job.id)}`);
    item.dataset.job = job.id;
    const facts = el('span', null, 'run-facts');
    if (job.actor_id) facts.append(el('span', job.actor_id));
    if (job.started) facts.append(timeEl(job.started));
    item.append(tag(HGJobState.label(job), HGJobState.tone(job)), link, facts);
    return item;
  }
  async function directionsSection(record, section) {
    const list = el('div', null, 'research-directions'), head = el('div', null, 'section-head'), count = el('span', null, 'count');
    head.append(el('h3', '探索过程'), count);
    section.id = 'task-directions'; section.append(head, list);
    list.append(el('p', '读取中…', 'empty'));
    try {
      const [directions, jobs, results] = await Promise.all([
        api(`/research-state/direction?research_id=${encodeURIComponent(record.id)}&limit=100`),
        api(`/jobs?research=${encodeURIComponent(record.id)}&limit=50`),
        api(`/results?research_id=${encodeURIComponent(record.id)}&limit=100`)]);
      const entries = directions.items.slice().reverse();
      count.textContent = entries.length ? String(entries.length) : '';
      list.replaceChildren(...(entries.length ? entries.map(direction => {
        const item = el('article', null, 'research-direction'), top = el('div', null, 'direction-head');
        item.dataset.direction = direction.id;
        const link = el('a', direction.title || direction.id, 'direction-title');
        link.href = HGTarget.url(`/dash/research?kind=direction&id=${encodeURIComponent(direction.id)}`);
        top.append(link, tag(DIRECTION_STATE[direction.state] || direction.state, DIRECTION_TONE[direction.state] || 'off'));
        const hypothesis = el('p', direction.hypothesis, 'direction-hypothesis');
        item.append(top, hypothesis);
        if ((direction.hypothesis || '').length > 140) {
          hypothesis.classList.add('clamped');
          const more = el('button', '展开', 'link-btn'); more.type = 'button'; more.setAttribute('aria-expanded', 'false');
          more.onclick = () => { const open = hypothesis.classList.toggle('clamped') === false; more.textContent = open ? '收起' : '展开'; more.setAttribute('aria-expanded', String(open)); };
          item.append(more);
        }
        const related = el('ul', null, 'direction-runs');
        for (const job of jobs.items.filter(job => job.direction_id === direction.id || results.items.some(result => result.labels?.direction_id === direction.id && result.job_id === job.id))) related.append(runRow(job));
        for (const result of results.items.filter(result => result.labels?.direction_id === direction.id)) {
          const row = el('li', null, 'run-row'), link = el('a', result.title || result.result_id), [label, tone] = RESULT_OUTCOME[result.outcome] || [result.outcome, 'off'];
          link.href = HGTarget.url(`/dash/results#result=${encodeURIComponent(result.result_id)}`);
          row.append(tag('结果 · ' + label, tone), link); related.append(row);
        }
        if (related.childElementCount) item.append(related);
        return item;
      }) : [el('p', '还没有方向', 'empty')]));
    } catch (error) { list.replaceChildren(el('p', '读取方向失败：' + failure(error), 'note')); }
  }

  const RUNS_SHOWN = 8;
  async function runsSection(record, section) {
    const list = el('ul', null, 'run-list'), head = el('div', null, 'section-head'), count = el('span', null, 'count');
    head.append(el('h3', '运行'), count);
    section.append(head, list);
    list.append(el('li', '读取中…', 'empty'));
    try {
      const value = await api(`/jobs?research=${encodeURIComponent(record.id)}&limit=50`);
      count.textContent = value.items.length ? String(value.items.length) : '';
      const rows = value.items.map(runRow);
      rows.slice(RUNS_SHOWN).forEach(row => { row.hidden = true; });
      list.replaceChildren(...(rows.length ? rows : [el('li', '暂无运行', 'empty')]));
      if (rows.length > RUNS_SHOWN) {
        const more = el('button', `显示全部 ${rows.length} 次`, 'link-btn runs-more'); more.type = 'button';
        more.onclick = () => { rows.forEach(row => { row.hidden = false; }); more.remove(); };
        section.append(more);
      }
    } catch (error) { list.replaceChildren(el('li', '读取运行失败：' + failure(error))); }
  }

  const DECISION = {adopt: ['采纳', 'ok'], defer: ['暂缓', 'off'], reject: ['拒绝', 'bad']};
  async function threadSection(record, ctx, section) {
    const list = el('div', null, 'discuss-list'), status = el('p', null, 'note'), head = el('div', null, 'section-head'), count = el('span', null, 'count');
    let replyTo = null, byId = new Map();
    status.setAttribute('role', 'status');
    const jump = el('button', '写留言', 'link-btn'); jump.type = 'button';
    head.append(el('h3', '留言'), count, jump);
    section.append(head, list);
    list.append(el('div', '读取中…', 'empty'));
    async function load() {
      const value = await api(`/research-state/note?research_id=${encodeURIComponent(record.id)}&limit=100`);
      const notes = value.items.slice().reverse(), ids = new Set(notes.map(note => note.id)), children = new Map(), roots = [];
      byId = new Map(notes.map(note => [note.id, note]));
      count.textContent = notes.length ? String(notes.length) : '';
      for (const note of notes) {
        if (note.reply_to && ids.has(note.reply_to)) {
          if (!children.has(note.reply_to)) children.set(note.reply_to, []);
          children.get(note.reply_to).push(note);
        } else roots.push(note);
      }
      function paint(note) {
        const human = note.auth_channel === 'dashboard_steer';
        const entry = el('div', null, 'discuss-entry' + (human ? ' human' : '') + (note.question_unanswered === true ? ' asking' : ''));
        const main = el('div', null, 'discuss-main'), top = el('div', null, 'discuss-head');
        top.append(el('span', speaker(note), 'discuss-from'));
        if (human) top.append(tag('指导', 'acc plain'));
        if (note.unanswered === true) top.append(tag('agent 未回应', 'warn'));
        if (note.ask) top.append(tag(note.question_unanswered === true ? '问人 · 待回复' : '问人', note.question_unanswered === true ? 'warn' : 'plain'));
        if (note.decision && DECISION[note.decision]) top.append(tag(...DECISION[note.decision]));
        if (note.reply_to && !ids.has(note.reply_to)) top.append(el('span', `回复 ${note.reply_to}`, 'discuss-via'));
        top.append(timeEl(note.updated_at));
        const reply = el('button', '回复', note.question_unanswered === true ? 'ghost primary reply-btn' : 'link-btn reply-btn');
        reply.type = 'button'; reply.dataset.replyTo = note.id; top.append(reply);
        main.append(top, el('p', note.text, 'discuss-text'));
        if (note.evidence_refs?.length) main.append(evidenceList(note.evidence_refs));
        entry.dataset.note = note.id; entry.id = 'note-' + note.id;
        const replies = children.get(note.id) || [];
        if (replies.length) {
          const nested = el('div', null, 'discuss-replies');
          nested.append(...replies.map(paint)); main.append(nested);
        }
        entry.append(avatar(speaker(note), human), main);
        return entry;
      }
      list.replaceChildren(...(roots.length ? roots.map(paint) : [el('div', '还没有留言', 'empty')]));
      if (location.hash) document.getElementById(location.hash.slice(1))?.scrollIntoView({block: 'center'});
    }
    try { await load(); } catch (error) { list.replaceChildren(); status.textContent = '读取留言失败：' + failure(error); }
    {
      const form = el('form', null, 'composer'), input = el('textarea'), button = el('button', '发送', 'ghost primary');
      const chip = el('div', null, 'reply-chip'), chipText = el('span'), cancel = el('button', null, 'chip-x'), foot = el('div', null, 'composer-foot');
      form.id = 'task-note-form'; input.id = 'task-note-text'; input.maxLength = 16384; input.autocomplete = 'off'; input.rows = 2;
      input.placeholder = record.data.owner ? `写给 ${record.data.owner} 的指导…` : '留言…'; input.setAttribute('aria-label', '留言内容');
      button.type = 'submit'; button.id = 'task-note-submit';
      cancel.type = 'button'; cancel.setAttribute('aria-label', '取消回复'); cancel.append(icon('M4 4l8 8M12 4l-8 8'));
      chip.hidden = true; chip.append(chipText, cancel);
      foot.append(el('span', 'Enter 发送 · Shift+Enter 换行', 'composer-hint'), button);
      form.append(chip, input, foot);
      const setReply = id => {
        replyTo = id; chip.hidden = !id;
        if (id) { const note = byId.get(id); chipText.textContent = `回复 ${note ? speaker(note) : id}：${(note?.text || '').slice(0, 60)}`; }
      };
      cancel.onclick = () => { setReply(null); input.focus(); };
      const grow = () => { input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight + 2, 320) + 'px'; };
      input.addEventListener('input', grow);
      // 中文输入法选词的回车不发送
      input.addEventListener('keydown', event => {
        if (event.isComposing || event.keyCode === 229) return;
        if (event.key === 'Enter' && !event.shiftKey && !event.altKey) { event.preventDefault(); form.requestSubmit(); }
        if (event.key === 'Escape' && replyTo) { event.preventDefault(); setReply(null); }
      });
      form.onsubmit = async event => {
        event.preventDefault();
        const text = input.value.trim();
        if (!text || button.disabled) return;
        if (!ctx.requireWrite(() => form.requestSubmit())) return;
        button.disabled = true; button.textContent = '发送中…'; status.textContent = '';
        try {
          await write('note', 'n-' + crypto.randomUUID().replace(/-/g, '').slice(0, 12),
                      {research_id: record.id, text, reply_to: replyTo, decision: null, ask: false, evidence_refs: []}, null, null);
          setReply(null); input.value = ''; grow(); await load();
        } catch (error) { status.textContent = '发送失败：' + failure(error) + '。内容还在输入框里。'; }
        finally { button.disabled = false; button.textContent = '发送'; }
      };
      jump.onclick = () => { input.focus(); input.scrollIntoView({block: 'center'}); };
      section.append(form);
      section.addEventListener('click', event => {
        const target = event.target.closest('[data-reply-to]');
        if (!target || !ctx.requireWrite(() => { target.click(); return false; })) return;
        setReply(target.dataset.replyTo);
        input.focus();
      });
    }
    section.append(status);
  }

  async function renderDetail(container, record, ctx) {
    // Unlock and selection can both repaint the same record; only the latest paint may fill the container.
    const paint = shown = {record};
    clearImages(); container.replaceChildren();
    if (record.kind !== 'research') return;
    agents = (await api('/agents').catch(() => ({agents: []}))).agents.filter(agent => agent.enabled !== false);
    if (shown !== paint) return;
    const directions = el('section', null, 'research-section'), runs = el('section', null, 'research-section'), thread = el('section', null, 'research-section');
    runs.id = 'task-runs'; thread.id = 'task-thread';
    container.append(...[ownerSection(record, ctx), directions, runs, thread, deliverableSection(record)].filter(Boolean));
    await Promise.all([directionsSection(record, directions), runsSection(record, runs), threadSection(record, ctx, thread)]);
  }

  window.HGResearchTask = {renderDetail, clearDetail: container => { shown = null; clearImages(); container.replaceChildren(); }};
})();
