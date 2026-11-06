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

  function body(deliverable) {
    const parts = [], applies = deliverable.applies_to;
    const scope = [applies.game_version && `游戏版本 ${applies.game_version}`, ...applies.devices].filter(Boolean).join(' · ');
    if (scope) parts.push(el('p', '适用：' + scope, 'report-scope'));
    parts.push(U.markdownReport(deliverable.summary), el('h2', `成果文件 · ${deliverable.artifacts.length}`, 'report-files'), U.evidenceList(deliverable.artifacts));
    return parts;
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
  const label = (revision, n) => [`第 ${n} 版`, when(revision.updated_at), by(revision)].join(' · ');

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
    $('report').replaceChildren(...body(newest.data.deliverable));
    $('report').hidden = false;
    $('message').hidden = true;
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
