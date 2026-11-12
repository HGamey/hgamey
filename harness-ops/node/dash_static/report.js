/* 交付报告: one task's delivery on its own page. The newest version reads in full; the versions delivered before it
   fold below and are drawn when opened. A version is a revision of the task whose deliverable differs from the one
   before it. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id), U = HGResearchTask.util, {el} = U;
  const id = new URLSearchParams(location.search).get('id') || '';
  const STATE = {active: ['推进中', 'acc'], idle: ['暂空闲', 'off'], paused: ['已暂停', 'warn'], closed: ['已关闭', 'off'], delivered: ['已交付', 'ok']};
  const DATE = new Intl.DateTimeFormat('zh-CN', {dateStyle: 'medium', timeStyle: 'short', hour12: false});
  const when = at => DATE.format(new Date(at * 1000));
  const by = revision => revision.audit?.auth_channel === 'dashboard_steer' ? '人工' : revision.audit?.actor_label || '管理员';
  const task = encodeURIComponent(id);

  // A picture opens large on this page, in the viewer comparisons use.
  const show = (url, name) => HGResearchCompare.viewer(document.body, [{slot: name, url, kind: 'image'}], name, U, {label: '放大查看'});
  const outPath = (job, path) => `/dash/api/jobs/${encodeURIComponent(job)}/out?${new URLSearchParams({path})}`;
  const preview = file => U.picture(file.path) ? U.thumbnail(outPath(file.job, file.path), file.path, show) : window.HGMedia?.preview(outPath(file.job, file.path), file.path) || null;
  // A picture the report names as job:<job>:<path> opens in the viewer where it is named; it stays a download link.
  const REF = /^job:([0-9a-f]{12}):(.+)$/;
  function pictureRefs(article) {
    for (const code of article.querySelectorAll('code')) {
      const [, job, path] = REF.exec(code.textContent.trim()) || [];
      if (!job || !U.picture(path)) continue;
      const link = el('a', null, 'report-picture'), source = outPath(job, path);
      link.href = HGTarget.url(source); link.download = path.split('/').pop(); link.title = '放大查看';
      code.replaceWith(link); link.append(code);
      link.addEventListener('click', async event => {
        event.preventDefault();
        try { show(await U.readPicture(source, path), path.split('/').pop()); }
        catch (_) { if (!link.nextElementSibling?.matches('.image-unread')) link.after(el('span', '无法预览', 'note image-unread')); }
      });
    }
    return article;
  }

  // The newest version lists what it produced by kind (成果); an earlier one keeps the references it was delivered with.
  function body(deliverable, outputs) {
    const parts = [], applies = deliverable.applies_to;
    const scope = [applies.game_version && `游戏版本 ${applies.game_version}`, ...applies.devices].filter(Boolean).join(' · ');
    if (scope) parts.push(el('p', '适用：' + scope, 'report-scope'));
    parts.push(pictureRefs(U.markdownReport(deliverable.summary)));
    parts.push(...(outputs ? [outputs] : [el('h2', `成果文件 · ${deliverable.artifacts.length}`, 'report-files'), U.evidenceList(deliverable.artifacts)]));
    return parts;
  }
  async function outputs(record, slot) {
    try {
      const jobs = await U.api(`/jobs?research=${task}&limit=50`);
      slot.replaceWith(HGTaskOutputs.full(await HGTaskOutputs.read(record, jobs.items), preview));
      if (location.hash === '#outputs') document.getElementById('outputs')?.scrollIntoView({block: 'start'});
    } catch (error) { slot.replaceChildren(el('h2', '成果', 'report-files'), el('p', `没读到成果：${error.message}`, 'note task-error')); }
  }
  // Every revision, newest first, reduced to those that changed the deliverable.
  async function versions() {
    const all = [];
    for (let cursor = null; ;) {
      const page = await U.api(`/research-state/research/${task}/history?limit=100${cursor ? '&cursor=' + encodeURIComponent(cursor) : ''}`);
      all.push(...page.items);
      cursor = page.next_cursor;
      if (!cursor) break;
    }
    return all.filter((revision, i) => revision.data.deliverable && JSON.stringify(revision.data.deliverable) !== JSON.stringify(all[i + 1]?.data.deliverable));
  }
  // A version's reason says what changed in it; the plain 交付 adds nothing.
  const label = (revision, n) => [`第 ${n} 版`, when(revision.updated_at), by(revision), revision.audit?.reason !== '交付' && revision.audit?.reason].filter(Boolean).join(' · ');

  async function load() {
    if (!id) { $('message').textContent = '没有指定任务。'; return; }
    const [record, delivered] = await Promise.all([U.api(`/research-state/research/${task}`), versions()]);
    const back = $('report-back');
    back.href = HGTarget.url(`/dash/research?kind=research&id=${task}`);
    back.querySelector('span').textContent = record.data.title;
    document.title = `交付报告 · ${record.data.title} · HGamey`;
    $('report-title').textContent = record.data.title;
    const [stateText, tone] = STATE[record.data.state] || [record.data.state, 'off'];
    if (!delivered.length) {
      $('report-kicker').replaceChildren(el('span', '交付报告'), U.tag(stateText, tone));
      $('message').textContent = '这个任务还没有交付报告。';
      return;
    }
    const [newest, ...older] = delivered;
    $('report-kicker').replaceChildren(el('span', '交付报告 · ' + label(newest, delivered.length)), U.tag(stateText, tone));
    const slot = el('section', null, 'report-outputs');
    slot.id = 'outputs';
    slot.append(el('h2', '成果', 'report-files'), el('p', '正在读取成果…', 'note'));
    $('report').replaceChildren(...body(newest.data.deliverable, slot));
    $('report').hidden = false;
    $('message').hidden = true;
    outputs({...record, data: {...record.data, deliverable: newest.data.deliverable}}, slot);
    if (!older.length) return;
    $('report-history').querySelector('summary').textContent = `历史版本 · ${older.length}`;
    $('report-versions').replaceChildren(...older.map((revision, i) => {
      const item = el('li'), box = el('details', null, 'report-version');
      box.append(el('summary', label(revision, older.length - i)));
      box.addEventListener('toggle', () => { if (box.open && box.children.length === 1) box.append(...body(revision.data.deliverable)); });
      item.append(box);
      return item;
    }));
    $('report-history').hidden = false;
  }
  load().catch(error => {
    $('message').textContent = error.code === 'research_not_found' ? '没有这个任务。' : `没读到交付报告：${error.message}`;
    $('message').classList.add('task-error');
  });
})();
