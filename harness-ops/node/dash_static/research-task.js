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
  // A chain's sections below the comparisons are read once they reach the screen (the media above prefetch further).
  const sectionRuns = new WeakMap(), sections = media && new IntersectionObserver(entries => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      sections.unobserve(entry.target); const run = sectionRuns.get(entry.target); sectionRuns.delete(entry.target); run?.();
    }
  }, {rootMargin: '100px 0px'});
  const reached = node => new Promise(resolve => { if (!sections) return resolve(); sectionRuns.set(node, resolve); sections.observe(node); });
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
  // Directions as they grew: each under the one it is based on (data.based_on), children in list order. The line from
  // the top to the direction being worked on now (else the latest touched one still standing) is the main line; a
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
    const standing = d => !['rejected', 'deferred'].includes(d.state), latest = list => list.sort((a, b) => b.updated_at - a.updated_at)[0];
    const busy = new Set(jobs.filter(job => window.HGJobState?.active(job)).map(job => job.direction_id));
    const target = latest(entries.filter(d => busy.has(d.id))) || latest(entries.filter(standing)) || latest([...entries]);
    const line = new Set();
    for (let id = target?.id; id && !line.has(id); id = parent.get(id)) line.add(id);
    for (const row of out) { row.main = line.has(row.d.id); row.dead = !standing(row.d) && !kids.has(row.d.id) && !row.main; }
    return out;
  }
  const runName = job => `${job.label || (job.argv || [])[0] || job.bin || job.id}${job.started ? ' · ' + short(job.started) : ''}`;
  async function directionsSection(scope, section) {
    const list = el('div', null, 'research-directions'), head = el('div', null, 'section-head'), count = el('span', null, 'count');
    head.append(el('h3', '探索过程'), count);
    section.id = 'task-directions'; section.append(head, list);
    list.append(el('p', '读取中…', 'empty'));
    try {
      const per = await Promise.all(scope.ids.map(id => Promise.all([
        api(`/research-state/direction?research_id=${encodeURIComponent(id)}&limit=100`),
        scope.jobs(id),
        api(`/results?research_id=${encodeURIComponent(id)}&limit=100`)])));
      // A chain lists its tasks' directions in chain order, each task's oldest first.
      const entries = per.flatMap(([directions]) => directions.items.slice().reverse());
      const jobs = {items: per.flatMap(([, value]) => value.items)}, results = {items: per.flatMap(([, , value]) => value.items)};
      count.textContent = entries.length ? String(entries.length) : '';
      const grown = lineage(entries, jobs.items), byId = new Map(entries.map(d => [d.id, d])), picked = new Set(entries.map(d => d.based_on?.job_id).filter(Boolean));
      const article = ({d: direction, depth, main, above}) => {
        const item = el('article', null, 'research-direction' + (main ? ' on-main' : '')), top = el('div', null, 'direction-head');
        item.dataset.direction = direction.id; item.dataset.task = direction.research_id; item.style.setProperty('--depth', depth);
        indent.push([item, depth, above]);
        const link = el('a', direction.title || direction.id, 'direction-title');
        link.href = HGTarget.url(`/dash/research?kind=direction&id=${encodeURIComponent(direction.id)}`);
        const title = el('div', null, 'direction-name'); title.append(...[scope.tag(direction.research_id), link].filter(Boolean));
        top.append(title, tag(DIRECTION_STATE[direction.state] || direction.state, DIRECTION_TONE[direction.state] || 'off'));
        item.append(top);
        // What it builds on: the run picked from its parent, named; the parent too when that is another task's.
        const base = direction.based_on && byId.get(direction.based_on.direction_id);
        if (base) {
          const from = el('p', null, 'direction-base'), run = direction.based_on.job_id && jobs.items.find(job => job.id === direction.based_on.job_id);
          from.append('基于 ');
          if (base.research_id !== direction.research_id || !direction.based_on.job_id) from.append(el('b', `「${clip(base.title || base.id, 24)}」`));
          if (direction.based_on.job_id) {
            const name = el('a', run ? runName(run) : `任务 ${direction.based_on.job_id}`); name.href = HGTarget.url(`/dash/jobs#job=${encodeURIComponent(direction.based_on.job_id)}`);
            from.append(...(base.research_id !== direction.research_id ? [' 里的 '] : []), name, ' 那次运行');
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
        for (const job of jobs.items.filter(job => job.direction_id === direction.id || results.items.some(result => result.labels?.direction_id === direction.id && result.job_id === job.id))) {
          const row = runRow(job); if (picked.has(job.id)) { row.classList.add('picked'); row.title = '后面的方向基于这次运行'; } related.append(row);
        }
        for (const result of results.items.filter(result => result.labels?.direction_id === direction.id)) {
          const row = el('li', null, 'run-row'), link = el('a', result.title || result.result_id), [label, tone] = RESULT_OUTCOME[result.outcome] || [result.outcome, 'off'];
          link.href = HGTarget.url(`/dash/results#result=${encodeURIComponent(result.result_id)}`);
          row.append(tag('结果 · ' + label, tone), link); related.append(row);
        }
        if (related.childElementCount) item.append(related);
        return item;
      };
      const indent = [];
      // Dead ends that sit together (same parent, same task) fold into one row that opens in place.
      const shown = [];
      for (let i = 0; i < grown.length; i++) {
        if (!grown[i].dead) { shown.push(article(grown[i])); continue; }
        const run = [grown[i]];
        while (grown[i + 1]?.dead && grown[i + 1].parent === grown[i].parent && grown[i + 1].d.research_id === grown[i].d.research_id) run.push(grown[++i]);
        const items = run.map(article), fold = el('button', null, 'direction-fold');
        fold.type = 'button'; fold.dataset.task = run[0].d.research_id; fold.style.setProperty('--depth', run[0].depth); fold.setAttribute('aria-expanded', 'false');
        indent.push([fold, run[0].depth, run[0].above]);
        fold.textContent = `▸ ${run.length} 条没往下走的分支：${run.map(x => `${clip(x.d.title || x.d.id, 18)}（${DIRECTION_STATE[x.d.state] || x.d.state}）`).join('、')}`;
        items.forEach(item => item.classList.add('folded'));
        fold.onclick = () => { fold.remove(); items.forEach(item => item.classList.remove('folded')); };
        shown.push(fold, ...items);
      }
      list.replaceChildren(...(entries.length ? shown : [el('p', '还没有方向', 'empty')]));
      // Looking at one task, a direction is indented only under what that task shows.
      const relayer = () => { for (const [item, depth, above] of indent) item.style.setProperty('--depth', scope.segment ? above.filter(id => id === scope.segment).length : depth); };
      scope.onPick.push(relayer); relayer();
      scope.apply(list);
    } catch (error) { list.replaceChildren(el('p', '读取方向失败：' + failure(error), 'note')); }
  }

  const RUNS_SHOWN = 8;
  async function runsSection(scope, section) {
    const list = el('ul', null, 'run-list'), head = el('div', null, 'section-head'), count = el('span', null, 'count');
    head.append(el('h3', '运行'), count);
    section.append(head, list);
    list.append(el('li', '读取中…', 'empty'));
    try {
      const value = {items: (await Promise.all(scope.ids.map(id => scope.jobs(id))))
        .flatMap(page => page.items).sort((a, b) => (b.started || 0) - (a.started || 0))};
      count.textContent = value.items.length ? String(value.items.length) : '';
      const rows = value.items.map(job => {
        const row = runRow(job), mark = scope.tag(job.research_id);
        row.dataset.task = job.research_id; if (mark) row.querySelector('.run-facts').prepend(mark);
        return row;
      });
      rows.slice(RUNS_SHOWN).forEach(row => { row.hidden = true; });
      list.replaceChildren(...(rows.length ? rows : [el('li', '暂无运行', 'empty')]));
      scope.apply(list);
      if (rows.length > RUNS_SHOWN) {
        const more = el('button', `显示全部 ${rows.length} 次`, 'link-btn runs-more'); more.type = 'button';
        more.onclick = () => { rows.forEach(row => { row.hidden = false; }); more.remove(); };
        section.append(more);
      }
    } catch (error) { list.replaceChildren(el('li', '读取运行失败：' + failure(error))); }
  }

  const DECISION = {adopt: ['采纳', 'ok'], defer: ['暂缓', 'off'], reject: ['拒绝', 'bad']};
  async function threadSection(record, ctx, section, scope) {
    const list = el('div', null, 'discuss-list'), status = el('p', null, 'note'), head = el('div', null, 'section-head'), count = el('span', null, 'count');
    let replyTo = null, byId = new Map();
    status.setAttribute('role', 'status');
    const jump = el('button', '写留言', 'link-btn'); jump.type = 'button';
    head.append(el('h3', '留言'), count, jump);
    section.append(head, list);
    list.append(el('div', '读取中…', 'empty'));
    async function load() {
      const pages = await Promise.all(scope.ids.map(id => api(`/research-state/note?research_id=${encodeURIComponent(id)}&limit=100`)));
      const notes = pages.flatMap(page => page.items).sort((a, b) => a.updated_at - b.updated_at), ids = new Set(notes.map(note => note.id)), children = new Map(), roots = [];
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
        const mark = !note.reply_to || !ids.has(note.reply_to) ? scope.tag(note.research_id) : null;
        if (mark) top.append(mark);
        top.append(el('span', speaker(note), 'discuss-from'));
        if (note.to) top.append(el('span', '→ ' + note.to, 'discuss-to'));
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
        if (mark) entry.dataset.task = note.research_id;
        const replies = children.get(note.id) || [];
        if (replies.length) {
          const nested = el('div', null, 'discuss-replies');
          nested.append(...replies.map(paint)); main.append(nested);
        }
        entry.append(avatar(speaker(note), human), main);
        return entry;
      }
      list.replaceChildren(...(roots.length ? roots.map(paint) : [el('div', '还没有留言', 'empty')]));
      scope.apply(list);
      // Each section answers only its own anchor: a note link lands here; #cmp- and #task-report land on theirs.
      if (location.hash.startsWith('#note-')) document.getElementById(location.hash.slice(1))?.scrollIntoView({block: 'center'});
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
      // 点名: a note addressed to an agent reaches it at check-in even if it does not own this task.
      // On a chain the note goes to the selected task (this page's task under 全部), and its owner is the default reader.
      const to = el('select', null, 'composer-to');
      to.id = 'task-note-to'; to.setAttribute('aria-label', '点名');
      to.onchange = () => {
        const owner = scope.owner(), where = scope.chain ? `${scope.members.get(scope.target())?.n || ''} ` : '';
        input.placeholder = to.value ? `${where}写给 ${to.value}…` : owner ? `${where}写给 ${owner} 的指导…` : `${where}留言…`;
      };
      const fillTo = () => {
        const owner = scope.owner();
        to.replaceChildren(new Option(owner ? `给负责人 ${owner}` : '不点名', ''),
                           ...(agents || []).filter(agent => agent.name !== owner).map(agent => new Option(`点名 ${agent.name}`, agent.name)));
        to.onchange();
      };
      fillTo(); scope.onPick.push(fillTo);
      foot.append(to, el('span', 'Enter 发送 · Shift+Enter 换行', 'composer-hint'), button);
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
                      {research_id: replyTo ? byId.get(replyTo)?.research_id || scope.target() : scope.target(), text, reply_to: replyTo, decision: null, ask: false, evidence_refs: [], to: to.value || null}, null, null);
          setReply(null); input.value = ''; to.value = ''; to.onchange(); grow(); await load();
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
    // Arriving from 去评 (#cmp-<id>), say at once that the comparison is on its way.
    const target = record.kind === 'research' && location.hash.startsWith('#cmp-') ? location.hash.slice(1) : '';
    const waiting = target ? compareNote('正在读取待你评的对比…') : el('div');
    clearImages(); dropChain(); container.replaceChildren(...(target ? [waiting] : []));
    if (record.kind !== 'research') return;
    // A task that others follow (or that follows one) shows the whole chain: a strip of its tasks, every section merged.
    // The chain is asked for while the owner list is read.
    const chained = api(`/tasks?chain=${encodeURIComponent(record.id)}`).then(value => value.items, () => []);
    agents = (await api('/agents').catch(() => ({agents: []}))).agents.filter(agent => agent.enabled !== false);
    if (shown !== paint) return;
    const chain = await chained;
    if (shown !== paint) return;
    const scope = chainScope(record, chain);
    const directions = el('section', null, 'research-section'), runs = el('section', null, 'research-section'), thread = el('section', null, 'research-section');
    runs.id = 'task-runs'; thread.id = 'task-thread';
    const judged = el('div'), reports = el('div');
    container.append(...[ownerSection(record, ctx), waiting, directions, runs, thread, judged, reports].filter(Boolean));
    if (scope.chain) { chainTree(scope, container); scope.pick(chain[0].id === record.id ? null : record.id); }
    // Comparisons are what a person comes here to answer. On a long chain the other sections fire dozens of reads and
    // media loads, so the comparisons go first and the rest of the page starts once they are in place.
    await comparisons(record, ctx, scope, container, waiting, judged, target, () => shown === paint);
    if (shown !== paint) return;
    const delivered = deliverables(scope, record).then(found => { if (shown === paint) { reports.replaceWith(...found); scope.apply(container); } });
    const steps = [[directions, () => directionsSection(scope, directions)], [runs, () => runsSection(scope, runs)],
      [thread, () => threadSection(record, ctx, thread, scope), location.hash.startsWith('#note-')]];
    if (!scope.chain) await Promise.all([...steps.map(([, load]) => load()), delivered]);
    else {
      // A chain reads its sections in page order as each reaches the screen, so what is below the fold waits for a
      // scroll; a note link reads the thread at once.
      const now = steps.filter(([, , at]) => at).map(([, load]) => load());
      (async () => { for (const [section, load, at] of steps) { if (at) continue; await reached(section); if (shown !== paint) return; await load(); } })();
      await Promise.all([...now, delivered]);
    }
    // The report is last, so a 交付报告 link lands once everything above it that will be read now is in.
    if (shown === paint && location.hash === '#task-report') document.getElementById('task-report')?.scrollIntoView({block: 'start'});
  }
  function compareNote(text, retry) {
    const box = el('section', null, 'research-section compare-section'), head = el('div', null, 'section-head');
    box.id = 'task-compare-open'; head.append(el('h3', '待你评')); box.append(head, el('p', text, retry ? 'compare-status task-error' : 'compare-status'));
    if (retry) { const again = el('button', '重试', 'ghost'); again.type = 'button'; again.addEventListener('click', retry); box.append(again); }
    return box;
  }
  async function comparisons(record, ctx, scope, container, waiting, judged, target, current) {
    if (!window.HGResearchCompare) { waiting.remove(); judged.remove(); return; }
    let found;
    try { found = await window.HGResearchCompare.sections(record, ctx, util, scope); }
    catch (error) {
      if (!current()) return;
      // Never drop the section silently: a person sent here to judge must see that the read failed.
      const failed = compareNote(`对比没读出来：${error.message || '连接中断'}`, () => {
        const again = compareNote('正在重新读取对比…'); failed.replaceWith(again);
        comparisons(record, ctx, scope, container, again, judged, target, current);
      });
      waiting.replaceWith(failed); return;
    }
    if (!current()) return;
    if (target && !found.open) found.open = compareNote('没有待你评的对比了：可能已经评过或已关闭，评过的在下方「对比」里。');
    waiting.replaceWith(found.open || ''); judged.replaceWith(found.done || ''); scope.apply(container);
    const block = target && document.getElementById(target);
    if (block) { block.scrollIntoView({block: 'start'}); block.classList.add('compare-target'); setTimeout(() => block.classList.remove('compare-target'), 2400); }
  }

  // ── Chains of tasks (research.follows)
  const CIRCLED = '①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳';
  const TASK_STATE = {active: '推进中', idle: '暂空闲', paused: '已暂停', closed: '已关闭', delivered: '已交付'};
  const clip = (value, n) => { const chars = Array.from(value || ''); return chars.length > n ? chars.slice(0, n).join('') + '…' : value || ''; };
  function chainScope(record, chain) {
    const linked = chain.length > 1, members = new Map(chain.map((task, i) => [task.id, {...task, n: CIRCLED[i] || `(${i + 1})`}]));
    const runs = new Map();
    const scope = {chain: linked, ids: linked ? chain.map(task => task.id) : [record.id], members, segment: null, onPick: [],
      // Each task's runs, read once for both 探索过程 and 运行.
      jobs: id => runs.get(id) || runs.set(id, api(`/jobs?research=${encodeURIComponent(id)}&limit=50`)).get(id),
      // The task an entry came from, on a merged chain page only.
      tag(id) {
        const m = linked && members.get(id); if (!m) return null;
        const mark = el('span', `${m.n} ${clip(m.title, 10)}`, 'chain-tag'); mark.title = `${m.n} ${m.title} · ${m.owner || '无人负责'}`; return mark;
      },
      target: () => scope.segment || record.id,
      owner: () => scope.segment && scope.segment !== record.id ? members.get(scope.segment)?.owner : record.data.owner,
      apply(root) { for (const item of root.querySelectorAll('[data-task]')) item.classList.toggle('seg-out', !!scope.segment && item.dataset.task !== scope.segment); },
      pick() {}};
    return scope;
  }
  // The chain as a tree in the task list, under its root's row: 全部, then each task indented under the one it follows.
  // When the root is not in the list (filtered out, or the list is not in yet) it stands above the task instead.
  let chainNav = null;
  function chainTree(scope, container) {
    const nav = el('nav', null, 'chain-strip'), all = el('button', '全部', 'chain-seg');
    nav.setAttribute('aria-label', '接续任务'); all.type = 'button'; all.onclick = () => scope.pick(null); nav.append(all);
    const depth = new Map();
    for (const id of scope.ids) {
      const m = scope.members.get(id), seg = el('button', null, 'chain-seg');
      depth.set(id, depth.has(m.follows) ? depth.get(m.follows) + 1 : 0);
      seg.type = 'button'; seg.dataset.seg = id; seg.title = m.title; seg.style.setProperty('--depth', depth.get(id)); seg.onclick = () => scope.pick(id);
      if (depth.get(id)) seg.classList.add('nested');
      seg.append(el('b', m.n), el('span', clip(m.title, 18), 'chain-title'), el('small', `${m.owner || '无人负责'} · ${TASK_STATE[m.state] || m.state}`));
      nav.append(seg);
    }
    scope.pick = id => {
      scope.segment = id;
      for (const seg of nav.querySelectorAll('.chain-seg')) seg.setAttribute('aria-pressed', String((seg.dataset.seg || null) === id));
      scope.apply(container); scope.onPick.forEach(fn => fn());
    };
    chainNav = {root: scope.ids[0], node: nav, container};
    placeChain();
  }
  function placeChain() {
    if (!chainNav) return;
    const row = [...document.querySelectorAll('#object-list > li[data-id]')].find(li => li.dataset.id === chainNav.root);
    if (row && chainNav.node.parentNode !== row) row.append(chainNav.node);
    else if (!row && chainNav.node.parentNode !== chainNav.container) chainNav.container.prepend(chainNav.node);
  }
  function dropChain() { chainNav?.node.remove(); chainNav = null; }
  // The newest delivery reads in full; older ones in the chain fold under it and are read when opened.
  async function deliverables(scope, record) {
    if (!scope.chain) return [deliverableSection(record)].filter(Boolean);
    const read = m => m.id === record.id ? Promise.resolve(record) : api(`/research-state/research/${encodeURIComponent(m.id)}`).catch(() => null);
    const done = [...scope.members.values()].filter(m => m.state === 'delivered' || m.id === record.id && record.data.deliverable)
      .sort((a, b) => (b.id === record.id ? record.updated_at : b.updated_at) - (a.id === record.id ? record.updated_at : a.updated_at));
    let newest = null;
    while (done.length && !newest) { const r = await read(done.shift()); if (r?.data.deliverable) newest = r; }
    const tagged = r => { const section = deliverableSection(r), mark = scope.tag(r.id); if (mark) section.querySelector('.section-head').prepend(mark); return section; };
    const older = done.map(m => {
      const box = el('details', null, 'research-section report-older');
      box.dataset.task = m.id; box.append(el('summary', `${m.n} ${m.title} 的交付报告 · ${short(m.id === record.id ? record.updated_at : m.updated_at)}`));
      box.addEventListener('toggle', async () => {
        if (!box.open || box.dataset.read) return;
        box.dataset.read = '1'; const status = el('p', '读取中…', 'empty'); box.append(status);
        const r = await read(m);
        if (!r?.data.deliverable) { status.textContent = '没读到这份交付报告。'; delete box.dataset.read; return; }
        const section = tagged(r); section.removeAttribute('id'); status.replaceWith(section);
      });
      return box;
    });
    if (!newest) return older;
    const section = tagged(newest); section.dataset.task = newest.id;
    return [section, ...older];
  }
  const util = {api, write, el, tag, timeEl, speaker, failure, near};

  window.HGResearchTask = {renderDetail, util, placeChain, clearDetail: container => { shown = null; clearImages(); dropChain(); container.replaceChildren(); }};
})();
