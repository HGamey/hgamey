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
  // Media are read once they come near the screen: a long chain's notes, judged comparisons and folded reports cost
  // nothing until a person scrolls to (or opens) them.
  const mediaRuns = new WeakMap(), media = 'IntersectionObserver' in window ? new IntersectionObserver(entries => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      media.unobserve(entry.target); const run = mediaRuns.get(entry.target); mediaRuns.delete(entry.target); run?.();
    }
  }, {rootMargin: '600px 0px'}) : null;
  function near(node, run) { if (!media) return run(); mediaRuns.set(node, run); media.observe(node); }
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
    image.alt = alt || filename; link.append(image); figure.append(link, el('figcaption', ref.kind === 'attachment' ? filename : `作业 ${ref.job_id} · ${ref.path}`));
    const mime = ref.content_type || IMAGE_TYPES[ref.path.split('.').pop().toLowerCase()] || 'application/octet-stream';
    const epoch = imageEpoch; near(figure, () => loadImage(image, ref, mime, epoch, sourcePath));
    return figure;
  }
  // Videos stream from their URL (the player seeks with ranges); nothing is read until the player asks for it.
  function videoFigure(ref, sourcePath = artifactPath(ref)) {
    const figure = el('figure', null, 'research-artifact video'), video = el('video'), link = el('a');
    const filename = ref.filename || ref.path.split('/').pop();
    Object.assign(video, {controls: true, preload: 'metadata', playsInline: true});
    near(video, () => { video.src = HGTarget.url(sourcePath) + '#t=0.1'; });   // paints the first frame without playing
    video.onerror = () => video.replaceWith(el('span', '视频读取失败', 'note'));
    link.textContent = ref.kind === 'attachment' ? filename : `作业 ${ref.job_id} · ${ref.path}`;
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
            const link = el('a', `作业 ${ref.job_id} · ${ref.path}`);
            link.href = HGTarget.url(artifactPath(ref)); link.download = ref.path.split('/').pop(); item.append(link);
          }
        } else {
          const link = el('a', `作业 ${ref.job_id}`); link.href = HGTarget.url(`/dash/jobs#job=${encodeURIComponent(ref.job_id)}`); item.append(link);
        }
      } else if (ref.kind === 'attachment' && ref.node_id === local) {
        if (/^video\//.test(ref.content_type || '')) item.append(videoFigure(ref, attachmentPath(ref)));
        else if (/^image\//.test(ref.content_type || '')) item.append(imageFigure(ref, ref.filename, attachmentPath(ref)));
        else {
          const link = el('a', ref.filename);
          link.href = HGTarget.url(attachmentPath(ref)); link.download = ref.filename; item.append(link);
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
      const jump = el('a', '查看交付报告', 'owner-jump'); jump.href = HGTarget.url(`/dash/research/report?id=${encodeURIComponent(record.id)}`); row.append(jump);
    }
    const flow = el('a', '数据流向', 'owner-jump'); flow.href = HGTarget.url(`/dash/research/flow?id=${encodeURIComponent(record.id)}`); row.append(flow);
    // The outcome of the last click outlives the repaint that follows it, and is judged on the owner just read back.
    const outcome = assigned?.id === record.id && record.revision >= assigned.revision ? assigned : null;
    if (outcome) {
      assigned = null;
      const ok = outcome.chosen === undefined || outcome.chosen === (owner || null);
      status.textContent = outcome.text || (ok ? (owner ? `已改派给 ${owner}` : '已改为无人负责') : `负责人现在是 ${owner || '无人负责'}，请再确认一次`);
      status.classList.toggle('task-error', !ok || !!outcome.bad);
    }
    if (record.data.state === 'closed') {
      const closed = el('p', null, 'task-closed');
      closed.append(el('b', '已关闭'), el('span', record.data.state_reason || ''), timeEl(record.updated_at));
      section.append(closed);
    }
    const stateBox = stateControl(record, ctx, status);
    if (!ctx.writable()) { row.append(stateBox); return section; }
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
    const assign = el('div', null, 'owner-assign'); assign.append(select, button); row.append(assign, stateBox);
    return section;
  }

  // 关闭 asks why; a closed or delivered task can be reopened (and delivered again later). Either is an ordinary revision
  // of the task, as the CLI's research close/reopen write it.
  function stateControl(record, ctx, status) {
    const closing = !['closed', 'delivered'].includes(record.data.state), box = el('div', null, 'task-state');
    const open = el('button', closing ? '关闭任务' : '重新打开', 'link-btn'), form = el('form', null, 'task-state-form');
    const why = el('input'), ok = el('button', closing ? '关闭' : '重新打开', 'ghost primary'), no = el('button', '取消', 'ghost');
    open.type = no.type = 'button'; ok.type = 'submit'; open.id = 'task-state-open'; why.id = 'task-state-reason'; form.hidden = true;
    why.maxLength = 16384; why.autocomplete = 'off'; why.placeholder = closing ? '关闭原因' : '原因（可不填）'; why.setAttribute('aria-label', closing ? '关闭原因' : '重新打开的原因');
    open.onclick = () => { if (!ctx.requireWrite()) return; open.hidden = true; form.hidden = false; why.focus(); };
    no.onclick = () => { form.hidden = true; open.hidden = false; why.value = ''; };
    form.onsubmit = async event => {
      event.preventDefault();
      const reason = why.value.trim() || (closing ? '' : '网页上重新打开');
      if (!reason) { why.focus(); status.textContent = '写一句关闭原因'; status.classList.add('task-error'); return; }
      if (!ctx.requireWrite(() => form.requestSubmit())) return;
      ok.disabled = no.disabled = true; ok.textContent = closing ? '关闭中…' : '打开中…'; status.textContent = ''; status.classList.remove('task-error');
      try {
        await write('research', record.id, {...record.data, state: closing ? 'closed' : 'active', state_reason: reason}, record.revision, reason);
      } catch (error) {
        status.textContent = (closing ? '没有关闭：' : '没有重新打开：') + failure(error); status.classList.add('task-error');
        ok.disabled = no.disabled = false; ok.textContent = closing ? '关闭' : '重新打开'; return;
      }
      if (shown?.record.id === record.id) await ctx.reload();
    };
    form.append(why, ok, no); box.append(open, form);
    return box;
  }
  const reportUrl = record => HGTarget.url(`/dash/research/report?id=${encodeURIComponent(record.id)}`);
  // The delivery reads in full on its own page; its conclusion is at the top of this one.
  function deliverableSection(record) {
    if (!record.data.deliverable) return null;
    const section = el('section', null, 'research-section task-report'), head = el('div', null, 'section-head'), link = el('a', '完整交付报告', 'summary-link');
    section.id = 'task-report'; link.href = reportUrl(record);
    head.append(el('h2', '交付报告'), tag('已交付', 'ok'), link);
    section.append(head);
    return section;
  }

  // The report's 结论 section (up to the next heading of its level or above); without one, its first paragraph.
  function conclusion(text) {
    const lines = String(text).replace(/\r\n?/g, '\n').split('\n'), at = lines.findIndex(line => /^#{1,6}\s*结论/.test(line));
    if (at >= 0) {
      const level = /^#+/.exec(lines[at])[0].length, rest = lines.slice(at + 1);
      const end = rest.findIndex(line => { const head = /^(#+)\s/.exec(line); return head && head[1].length <= level; });
      return (end < 0 ? rest : rest.slice(0, end)).join('\n').trim();
    }
    return lines.join('\n').split(/\n\s*\n/).map(block => block.split('\n').filter(line => !/^#/.test(line)).join('\n').trim()).find(Boolean) || '';
  }
  // What a person opens a task for, before its working sections: the task it follows, the conclusion of its delivery,
  // the key comparison (filled in once the comparisons are read, in `slot`) and the tasks that follow it.
  function summarySection(record, chain, slot, outputs) {
    const section = el('section', null, 'research-section task-summary');
    section.id = 'task-summary';
    const parent = chain.find(task => task.id === record.data.follows);
    if (parent) {
      const line = el('p', '接续自 ', 'summary-parent'), link = el('a', parent.title);
      link.href = HGTarget.url(`/dash/research?kind=research&id=${encodeURIComponent(parent.id)}`);
      line.append(link); section.append(line);
    }
    const report = record.data.deliverable && conclusion(record.data.deliverable.summary);
    if (report) {
      const box = el('div', null, 'summary-block summary-conclusion'), head = el('div', null, 'section-head'), link = el('a', '完整交付报告', 'summary-link');
      link.href = reportUrl(record);
      head.append(el('h2', '结论'), link);
      const text = markdownReport(report), more = el('button', '展开', 'ghost summary-more');
      more.type = 'button';
      more.onclick = () => { box.classList.remove('clamped'); more.remove(); };
      box.classList.add('clamped');
      box.append(head, text, more);
      // Only a conclusion taller than the clamp keeps it.
      requestAnimationFrame(() => { if (text.scrollHeight <= text.clientHeight + 2) { box.classList.remove('clamped'); more.remove(); } });
      section.append(box);
    }
    section.append(outputs, slot);
    const depth = new Map([[record.id, 0]]), children = [];
    for (const task of chain) if (depth.has(task.follows) && task.id !== record.id) { depth.set(task.id, depth.get(task.follows) + 1); children.push(task); }
    if (children.length) {
      const box = el('div', null, 'summary-block summary-children'), list = el('ul', null, 'summary-tasks');
      box.append(el('div', null, 'section-head'));
      box.firstChild.append(el('h2', '子任务'), el('span', String(children.length), 'count'));
      for (const task of children) {
        const item = el('li'), link = el('a', null, 'summary-task'), [stateText, tone] = TASK_TONE[task.state] || [task.state, 'off'];
        link.href = HGTarget.url(`/dash/research?kind=research&id=${encodeURIComponent(task.id)}`);
        link.style.setProperty('--depth', depth.get(task.id) - 1);
        link.append(el('span', task.title, 'summary-task-title'), el('span', task.owner || '无人负责', 'muted'), tag(stateText, tone));
        item.append(link); list.append(item);
      }
      box.append(list); section.append(box);
    }
    return section;
  }
  const TASK_TONE = {active: ['推进中', 'acc'], idle: ['暂空闲', 'off'], paused: ['已暂停', 'warn'], closed: ['已关闭', 'off'], delivered: ['已交付', 'ok']};

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
  // Directions as they grew: each under the one it is based on (data.based_on), children in list order. The line from
  // the top to the direction being worked on now (else the newest one still standing) is the main line; a
  // rejected or deferred direction that nothing builds on is a dead end.
  function lineage(entries, jobs) {
    const byId = new Map(entries.map(d => [d.id, d])), kids = new Map(), parent = new Map(), out = [], seen = new Set();
    for (const d of entries) {
      const p = d.based_on && byId.get(d.based_on.direction_id);
      if (p && p !== d) { parent.set(d.id, p.id); (kids.get(p.id) || kids.set(p.id, []).get(p.id)).push(d); }
    }
    const walk = (d, depth) => {
      if (seen.has(d.id)) return;
      const above = [];
      for (let id = parent.get(d.id); id && !above.includes(id); id = parent.get(id)) above.push(id);
      seen.add(d.id); out.push({d, depth, parent: parent.get(d.id) || null, above: above.map(id => byId.get(id).research_id)});
      for (const k of kids.get(d.id) || []) walk(k, depth + 1);
    };
    for (const d of entries) if (!parent.has(d.id)) walk(d, 0);
    for (const d of entries) walk(d, 0);   // a link that loops leaves its directions at the top level
    const standing = d => !['rejected', 'deferred'].includes(d.state), latest = list => list.sort((a, b) => b.created_at - a.created_at)[0];
    const busy = new Set(jobs.filter(job => window.HGJobState?.active(job)).map(job => job.direction_id));
    const target = latest(entries.filter(d => busy.has(d.id))) || latest(entries.filter(standing)) || latest([...entries]);
    const line = new Set();
    for (let id = target?.id; id && !line.has(id); id = parent.get(id)) line.add(id);
    for (const row of out) { row.main = line.has(row.d.id); row.dead = !standing(row.d) && !kids.has(row.d.id) && !row.main; }
    return out;
  }
  const runName = job => `${job.label || (job.argv || [])[0] || job.bin || job.id}${job.started ? ' · ' + short(job.started) : ''}`;
  // A working section folded to its heading line and a count: what agents did is there when asked for.
  function folded(section, title) {
    const box = el('details', null, 'task-fold'), head = el('summary', null, 'section-head'), count = el('span', null, 'count'), brief = el('span', null, 'fold-brief');
    head.append(el('h3', title), count, brief);
    box.append(head); section.append(box);
    return {box, count, brief};
  }
  const tally = (items, key, names) => Object.entries(items.reduce((n, item) => { const k = key(item); if (names[k]) n[k] = (n[k] || 0) + 1; return n; }, {}))
    .map(([k, n]) => `${names[k]} ${n}`).join(' · ');

  async function directionsSection(record, section, jobs) {
    const list = el('div', null, 'research-directions'), {box, count, brief} = folded(section, '探索过程');
    section.id = 'task-directions'; box.append(list);
    list.append(el('p', '读取中…', 'empty'));
    try {
      const [directions, runs, results] = await Promise.all([
        api(`/research-state/direction?research_id=${encodeURIComponent(record.id)}&limit=100`), jobs(),
        api(`/results?research_id=${encodeURIComponent(record.id)}&limit=100`)]);
      const entries = directions.items.slice().reverse();   // oldest first
      count.textContent = entries.length ? String(entries.length) : '';
      brief.textContent = tally(entries, d => d.state, {running: '进行中', verified: '已验证', no_gain: '无收益', rejected: '已排除'});
      // A direction may build on one in the task this one follows: that one is read to name it.
      const outside = [...new Set(entries.map(d => d.based_on?.direction_id).filter(id => id && !entries.some(d => d.id === id)))];
      const bases = await Promise.all(outside.map(id => api(`/research-state/direction/${encodeURIComponent(id)}`).then(r => ({...r.data, id: r.id}), () => null)));
      const grown = lineage(entries, runs.items), byId = new Map([...bases.filter(Boolean), ...entries].map(d => [d.id, d]));
      const picked = new Set(entries.map(d => d.based_on?.job_id).filter(Boolean));
      const article = ({d: direction, depth, main}) => {
        const item = el('article', null, 'research-direction' + (main ? ' on-main' : '')), top = el('div', null, 'direction-head');
        item.dataset.direction = direction.id; item.style.setProperty('--depth', depth);
        const link = el('a', direction.title || direction.id, 'direction-title');
        link.href = HGTarget.url(`/dash/research?kind=direction&id=${encodeURIComponent(direction.id)}`);
        const title = el('div', null, 'direction-name'); title.append(link);
        top.append(title, tag(DIRECTION_STATE[direction.state] || direction.state, DIRECTION_TONE[direction.state] || 'off'));
        item.append(top);
        // What it builds on: the run picked from its parent, named; the parent too when that is another task's.
        const base = direction.based_on && byId.get(direction.based_on.direction_id);
        if (base) {
          const from = el('p', null, 'direction-base'), run = direction.based_on.job_id && runs.items.find(job => job.id === direction.based_on.job_id);
          from.append('基于 ');
          if (base.research_id !== direction.research_id || !direction.based_on.job_id) from.append(el('b', `「${clip(base.title || base.id, 24)}」`));
          if (direction.based_on.job_id) {
            const name = el('a', run ? runName(run) : `作业 ${direction.based_on.job_id}`); name.href = HGTarget.url(`/dash/jobs#job=${encodeURIComponent(direction.based_on.job_id)}`);
            from.append(...(base.research_id !== direction.research_id ? [' 里的 '] : []), name, ' 那次作业');
          }
          item.append(from);
        }
        const hypothesis = el('p', direction.hypothesis, 'direction-hypothesis');
        item.append(hypothesis);
        if ((direction.hypothesis || '').length > 140) {
          hypothesis.classList.add('clamped');
          const more = el('button', '展开', 'link-btn'); more.type = 'button'; more.setAttribute('aria-expanded', 'false');
          more.onclick = () => { const open = hypothesis.classList.toggle('clamped') === false; more.textContent = open ? '收起' : '展开'; more.setAttribute('aria-expanded', String(open)); };
          item.append(more);
        }
        const related = el('ul', null, 'direction-runs');
        for (const job of runs.items.filter(job => job.direction_id === direction.id || results.items.some(result => result.labels?.direction_id === direction.id && result.job_id === job.id))) {
          const row = runRow(job); if (picked.has(job.id)) { row.classList.add('picked'); row.title = '后面的方向基于这次作业'; } related.append(row);
        }
        for (const result of results.items.filter(result => result.labels?.direction_id === direction.id)) {
          const row = el('li', null, 'run-row'), link = el('a', result.title || result.result_id), [label, tone] = RESULT_OUTCOME[result.outcome] || [result.outcome, 'off'];
          link.href = HGTarget.url(`/dash/results#result=${encodeURIComponent(result.result_id)}`);
          row.append(tag('结果 · ' + label, tone), link); related.append(row);
        }
        if (related.childElementCount) item.append(related);
        return item;
      };
      // Dead ends that sit together (same parent) fold into one row that opens in place.
      const shown = [];
      for (let i = 0; i < grown.length; i++) {
        if (!grown[i].dead) { shown.push(article(grown[i])); continue; }
        const run = [grown[i]];
        while (grown[i + 1]?.dead && grown[i + 1].parent === grown[i].parent) run.push(grown[++i]);
        const items = run.map(article), fold = el('button', null, 'direction-fold');
        fold.type = 'button'; fold.style.setProperty('--depth', run[0].depth); fold.setAttribute('aria-expanded', 'false');
        fold.textContent = `▸ ${run.length} 条没往下走的分支：${run.map(x => `${clip(x.d.title || x.d.id, 18)}（${DIRECTION_STATE[x.d.state] || x.d.state}）`).join('、')}`;
        items.forEach(item => item.classList.add('folded'));
        fold.onclick = () => { fold.remove(); items.forEach(item => item.classList.remove('folded')); };
        shown.push(fold, ...items);
      }
      list.replaceChildren(...(entries.length ? shown : [el('p', '还没有方向', 'empty')]));
    } catch (error) { list.replaceChildren(el('p', '读取方向失败：' + failure(error), 'note')); }
  }

  const RUNS_SHOWN = 8;
  const MOSAIC_TILES = 12;
  // One job's screenshots, straight on the task page: each distinct image is a tile, byte-identical
  // screenshots collapse into one tile labelled with their count ("8 张完全相同" reads as "nothing changed").
  function mosaic(box, job) {
    near(box, async () => {
      try {
        const page = await api(`/jobs/${encodeURIComponent(job.id)}/images`);
        const groups = new Map();
        for (const image of page.images) {
          const group = groups.get(image.sha256);
          if (group) group.count++;
          else groups.set(image.sha256, {image, count: 1});
        }
        const tiles = [...groups.values()], extra = tiles.length - MOSAIC_TILES;
        for (const {image, count} of tiles.slice(0, MOSAIC_TILES)) {
          const tile = el('figure', null, 'mosaic-tile'), link = el('a'), img = el('img');
          link.href = HGTarget.url(artifactPath({job_id: job.id, path: image.path}));
          link.target = '_blank'; link.rel = 'noopener';
          img.alt = image.path; img.loading = 'lazy';
          const epoch = imageEpoch;
          near(tile, () => loadImage(img, {job_id: job.id, path: image.path}, IMAGE_TYPES[extension(image.path)], epoch));
          link.append(img); tile.append(link);
          if (count > 1) tile.append(el('span', `${count} 张完全相同`, 'dup'));
          box.append(tile);
        }
        if (extra > 0) box.append(el('span', `+${extra} 张`, 'dup more'));
      } catch (error) { /* a job without readable images keeps its plain row */ }
    });
  }
  // 作业: what agents ran for this task, newest first, folded to a count and how they went.
  async function runsSection(section, jobs) {
    const list = el('ul', null, 'run-list'), {box, count, brief} = folded(section, '作业');
    box.append(list);
    list.append(el('li', '读取中…', 'empty'));
    try {
      const items = (await jobs()).items.slice().sort((a, b) => (b.started || 0) - (a.started || 0));
      count.textContent = items.length ? String(items.length) : '';
      brief.textContent = tally(items, job => HGJobState.tone(job), {acc: '运行中', ok: '成功', bad: '失败'});
      const rows = items.map(runRow);
      rows.forEach((row, index) => mosaic(row, items[index]));
      rows.slice(RUNS_SHOWN).forEach(row => { row.hidden = true; });
      list.replaceChildren(...(rows.length ? rows : [el('li', '暂无作业', 'empty')]));
      if (rows.length > RUNS_SHOWN) {
        const more = el('button', `显示全部 ${rows.length} 次`, 'link-btn runs-more'); more.type = 'button';
        more.onclick = () => { rows.forEach(row => { row.hidden = false; }); more.remove(); };
        box.append(more);
      }
    } catch (error) { list.replaceChildren(el('li', '读取作业失败：' + failure(error))); }
  }

  const DECISION = {adopt: ['采纳', 'ok'], defer: ['暂缓', 'off'], reject: ['拒绝', 'bad']};
  const NOTES_SHOWN = 5;
  async function threadSection(record, ctx, section) {
    const list = el('div', null, 'discuss-list'), status = el('p', null, 'note'), head = el('div', null, 'section-head'), count = el('span', null, 'count');
    let replyTo = null, byId = new Map();
    status.setAttribute('role', 'status');
    const jump = el('button', '写留言', 'link-btn'); jump.type = 'button';
    head.append(el('h3', '留言'), count, jump);
    section.append(head, list);
    list.append(el('div', '读取中…', 'empty'));
    async function load() {
      const page = await api(`/research-state/note?research_id=${encodeURIComponent(record.id)}&limit=100`);
      const notes = page.items.slice().sort((a, b) => a.updated_at - b.updated_at), ids = new Set(notes.map(note => note.id)), children = new Map(), roots = [];
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
        if (note.to) top.append(el('span', '→ ' + note.to, 'discuss-to'));
        if (note.urgent) top.append(tag('紧急', 'bad'));
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
      // The newest few threads show; older ones fold, except one still waiting for a person or the one a link opens.
      const thread = note => [note, ...(children.get(note.id) || []).flatMap(thread)];
      const linked = location.hash.startsWith('#note-') ? location.hash.slice(6) : '';
      const older = roots.slice(0, -NOTES_SHOWN).filter(root => !thread(root).some(note => note.question_unanswered === true || note.id === linked));
      if (older.length) {
        const hidden = older.map(root => document.getElementById('note-' + root.id)), n = older.flatMap(thread).length;
        const more = el('button', `更早的 ${n} 条留言`, 'link-btn notes-more'); more.type = 'button';
        hidden.forEach(entry => { entry.hidden = true; });
        more.onclick = () => { hidden.forEach(entry => { entry.hidden = false; }); more.remove(); };
        list.prepend(more);
      }
      // Each section answers only its own anchor: a note link lands here; #cmp- and #task-report land on theirs.
      // A note taller than half the screen lands with its author line at the top, not its middle at the centre.
      const note = location.hash.startsWith('#note-') && document.getElementById(location.hash.slice(1));
      if (note) note.scrollIntoView({block: note.offsetHeight > innerHeight / 2 ? 'start' : 'center'});
    }
    try { await load(); } catch (error) { list.replaceChildren(); status.textContent = '读取留言失败：' + failure(error); }
    {
      const form = el('form', null, 'composer'), input = el('textarea'), button = el('button', '发送', 'ghost primary');
      const chip = el('div', null, 'reply-chip'), chipText = el('span'), cancel = el('button', null, 'chip-x'), foot = el('div', null, 'composer-foot');
      form.id = 'task-note-form'; input.id = 'task-note-text'; input.maxLength = 16384; input.autocomplete = 'off'; input.rows = 2;
      input.setAttribute('aria-label', '留言内容');
      button.type = 'submit'; button.id = 'task-note-submit';
      cancel.type = 'button'; cancel.setAttribute('aria-label', '取消回复'); cancel.append(icon('M4 4l8 8M12 4l-8 8'));
      chip.hidden = true; chip.append(chipText, cancel);
      // 点名: a note addressed to an agent reaches it at check-in even if it does not own this task; the owner is the
      // default reader.
      const to = el('select', null, 'composer-to'), owner = record.data.owner;
      to.id = 'task-note-to'; to.setAttribute('aria-label', '点名');
      // 紧急: the note lands first in the assignee's next check-in duties, above the ordinary queue.
      const urgent = el('input'); urgent.type = 'checkbox'; urgent.id = 'task-note-urgent';
      const urgentLabel = el('label', null, 'composer-urgent'); urgentLabel.setAttribute('for', 'task-note-urgent');
      urgentLabel.setAttribute('aria-label', '标为紧急'); urgentLabel.append(urgent, '紧急');
      to.onchange = () => { input.placeholder = to.value ? `写给 ${to.value}…` : owner ? `写给 ${owner} 的指导…` : '留言…'; };
      to.replaceChildren(new Option(owner ? `给负责人 ${owner}` : '不点名', ''),
                         ...(agents || []).filter(agent => agent.name !== owner).map(agent => new Option(`点名 ${agent.name}`, agent.name)));
      to.onchange();
      foot.append(to, el('span', 'Enter 发送 · Shift+Enter 换行', 'composer-hint'), urgentLabel, button);
      form.append(chip, input, foot);
      const setReply = id => {
        replyTo = id; chip.hidden = !id;
        if (id) { const note = byId.get(id); chipText.textContent = `回复 ${note ? speaker(note) : id}：${(note?.text || '').slice(0, 60)}`;
          // Answering another agent's note points the reply at that agent.
          const who = note?.auth_channel === 'agent_token' ? note.actor_label : null;
          if (who && [...to.options].some(option => option.value === who)) { to.value = who; to.onchange(); } }
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
                      {research_id: record.id, text, reply_to: replyTo, decision: null, ask: false, evidence_refs: [],
                       to: to.value || null, urgent: urgent.checked}, null, null);
          setReply(null); input.value = ''; to.value = ''; to.onchange(); urgent.checked = false; grow(); await load();
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

  // 最近失败的作业放在任务页顶部：作业号、一行失败原因和时间；任务没有失败过的作业就不显示。
  async function failedSection(section, jobs) {
    try {
      const items = (await jobs()).items;
      const bad = items.filter(job => job.outcome === 'failed')
        .sort((a, b) => (b.ended || b.started || 0) - (a.ended || a.started || 0))[0];
      if (!bad) { section.remove(); return; }
      const why = typeof bad.error === 'string' ? bad.error : bad.error?.message || bad.note
        || (Number.isInteger(bad.exit) ? `exit ${bad.exit}` : '原因未记录');
      const link = el('a', `作业 ${bad.id}`);
      link.href = HGTarget.url(`/dash/jobs#job=${encodeURIComponent(bad.id)}`);
      const line = el('p', null, 'failed-line');
      line.append(tag('最近失败', 'bad'), link, el('span', clip(why, 120), 'failed-why'), timeEl(bad.ended || bad.started));
      section.append(line);
    } catch (error) { section.remove(); }
  }

  async function renderDetail(container, record, ctx) {
    // Unlock and selection can both repaint the same record; only the latest paint may fill the container.
    const paint = shown = {record};
    // Arriving from 去评 (#cmp-<id>), say at once that the comparison is on its way.
    const target = record.kind === 'research' && location.hash.startsWith('#cmp-') ? location.hash.slice(1) : '';
    const waiting = target ? compareNote('正在读取待你评的对比…') : el('div');
    clearImages(); dropChain(); container.replaceChildren(...(target ? [waiting] : []));
    if (record.kind !== 'research') return;
    if (location.hash === '#task-report' && record.data.deliverable) { location.replace(reportUrl(record)); return; }
    // Only this task's own records are read. The tasks it follows or that follow it (its chain) are links: a tree in
    // the task list, and 接续自 / 子任务 at the top. The chain is asked for while the owner list is read.
    const chained = api(`/tasks?chain=${encodeURIComponent(record.id)}`).then(value => value.items, () => []);
    agents = (await api('/agents').catch(() => ({agents: []}))).agents.filter(agent => agent.enabled !== false);
    if (shown !== paint) return;
    const chain = await chained;
    if (shown !== paint) return;
    let read = null;   // this task's jobs, read once for both 探索过程 and 作业
    const jobs = () => read || (read = api(`/jobs?research=${encodeURIComponent(record.id)}&limit=50`));
    const directions = el('section', null, 'research-section'), runs = el('section', null, 'research-section'), thread = el('section', null, 'research-section');
    runs.id = 'task-runs'; thread.id = 'task-thread';
    const judged = el('div'), keySlot = el('div'), outputSlot = el('div'), summary = summarySection(record, chain, keySlot, outputSlot);
    const failed = el('section', null, 'research-section failed-job'); failed.id = 'task-failed';
    container.append(...[ownerSection(record, ctx), failed, waiting, summary, directions, runs, thread, judged, deliverableSection(record)].filter(Boolean));
    failedSection(failed, jobs);
    if (chain.length > 1) chainTree(chain, record, container);
    // Comparisons are what a person comes here to answer, so they go first and the rest starts once they are in place.
    await comparisons(record, ctx, container, waiting, judged, target, () => shown === paint, keySlot);
    if (shown !== paint) return;
    await Promise.all([directionsSection(record, directions, jobs), runsSection(runs, jobs), threadSection(record, ctx, thread)]);
    await outputsBrief(record, outputSlot, jobs, () => shown === paint);
  }
  // 成果 at the top: what the delivery produced, read once the working sections are in.
  async function outputsBrief(record, slot, jobs, current) {
    if (!record.data.deliverable || !window.HGTaskOutputs) { slot.remove(); return; }
    try {
      const outputs = await HGTaskOutputs.read(record, (await jobs()).items);
      if (!current()) return;
      const any = outputs.files.length || outputs.commits.length || outputs.results.length || outputs.links.length;
      slot.replaceWith(any ? HGTaskOutputs.brief(outputs, reportUrl(record)) : '');
    } catch (_) { if (current()) slot.remove(); }
  }
  function compareNote(text, retry) {
    const box = el('section', null, 'research-section compare-section'), head = el('div', null, 'section-head');
    box.id = 'task-compare-open'; head.append(el('h3', '待你评')); box.append(head, el('p', text, retry ? 'compare-status task-error' : 'compare-status'));
    if (retry) { const again = el('button', '重试', 'ghost'); again.type = 'button'; again.addEventListener('click', retry); box.append(again); }
    return box;
  }
  async function comparisons(record, ctx, container, waiting, judged, target, current, keySlot) {
    if (!window.HGResearchCompare) { waiting.remove(); judged.remove(); keySlot.remove(); return; }
    let found;
    try { found = await window.HGResearchCompare.sections(record, ctx, util); }
    catch (error) {
      if (!current()) return;
      // Never drop the section silently: a person sent here to judge must see that the read failed.
      const failed = compareNote(`对比没读出来：${error.message || '连接中断'}`, () => {
        const again = compareNote('正在重新读取对比…'); failed.replaceWith(again);
        comparisons(record, ctx, container, again, judged, target, current, keySlot);
      });
      waiting.replaceWith(failed); return;
    }
    if (!current()) return;
    if (target && !found.open) found.open = compareNote('没有待你评的对比了：可能已经评过或已关闭，评过的在下方「对比」里。');
    waiting.replaceWith(found.open || ''); judged.replaceWith(found.done || ''); keySlot.replaceWith(found.key || '');
    const block = target && document.getElementById(target);
    if (block) { block.scrollIntoView({block: 'start'}); block.classList.add('compare-target'); setTimeout(() => block.classList.remove('compare-target'), 2400); }
  }

  // ── Chains of tasks (research.follows)
  const CIRCLED = '①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳';
  const TASK_STATE = {active: '推进中', idle: '暂空闲', paused: '已暂停', closed: '已关闭', delivered: '已交付'};
  const clip = (value, n) => { const chars = Array.from(value || ''); return chars.length > n ? chars.slice(0, n).join('') + '…' : value || ''; };
  // The chain as a tree in the task list, under its root's row: each task indented under the one it follows, a link to
  // its own page, this one marked. When the root is not in the list (filtered out, or the list is not in yet) it stands
  // above the task instead.
  let chainNav = null;
  function chainTree(chain, record, container) {
    const nav = el('nav', null, 'chain-strip'), depth = new Map();
    nav.setAttribute('aria-label', '接续任务');
    chain.forEach((task, i) => {
      const seg = el('a', null, 'chain-seg');
      depth.set(task.id, depth.has(task.follows) ? depth.get(task.follows) + 1 : 0);
      seg.href = HGTarget.url(`/dash/research?kind=research&id=${encodeURIComponent(task.id)}`);
      seg.dataset.seg = task.id; seg.title = task.title; seg.style.setProperty('--depth', depth.get(task.id));
      if (depth.get(task.id)) seg.classList.add('nested');
      if (task.id === record.id) seg.setAttribute('aria-current', 'page');
      seg.append(el('b', CIRCLED[i] || `(${i + 1})`), el('span', clip(task.title, 18), 'chain-title'), el('small', `${task.owner || '无人负责'} · ${TASK_STATE[task.state] || task.state}`));
      nav.append(seg);
    });
    chainNav = {root: chain[0].id, node: nav, container};
    placeChain();
  }
  function placeChain() {
    if (!chainNav) return;
    const row = [...document.querySelectorAll('#object-list > li[data-id]')].find(li => li.dataset.id === chainNav.root);
    if (row && chainNav.node.parentNode !== row) row.append(chainNav.node);
    else if (!row && chainNav.node.parentNode !== chainNav.container) chainNav.container.prepend(chainNav.node);
  }
  function dropChain() { chainNav?.node.remove(); chainNav = null; }
  const util = {api, write, el, tag, timeEl, speaker, failure, near, markdownReport, evidenceList};

  window.HGResearchTask = {renderDetail, util, placeChain, clearDetail: container => { shown = null; clearImages(); dropChain(); container.replaceChildren(); }};
})();
