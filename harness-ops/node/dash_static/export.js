/* 数据导出: pick whole tasks (a follow-on chain at once, or the data in no task), have the center write one zip, then
   download it. Tasks still running jobs, or whose originals are on another computer, say so as soon as they are
   picked. Everything here needs the steer password. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id), API = '/dash/api', DASH = '—', DAY = 86400;
  const STATE = {active: '推进中', idle: '暂空闲', paused: '已暂停', closed: '已关闭', delivered: '已交付'};
  const PHASE = {collect: '正在收回从节点上的原件', write: '正在写入'};
  const UNASSIGNED = '*unassigned';   // task IDs have no '*'
  let listing = null, exports = [], timer = null;
  const picked = new Set();

  function el(tag, value, className) { const item = document.createElement(tag); if (value != null) item.textContent = String(value); if (className) item.className = className; return item; }
  function size(bytes) {
    if (bytes == null) return DASH;
    const units = ['B', 'KB', 'MB', 'GB', 'TB'], at = Math.min(units.length - 1, Math.floor(Math.log(Math.max(bytes, 1)) / Math.log(1024)));
    return `${at ? (bytes / 1024 ** at).toFixed(bytes / 1024 ** at < 100 ? 1 : 0) : bytes} ${units[at]}`;
  }
  const time = at => at ? new Date(at * 1000).toLocaleString('zh-CN', {hour12: false, month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit'}) : DASH;

  async function call(path, init = {}) {
    const response = await HGTarget.fetch(API + path, {credentials: 'same-origin', cache: 'no-store', ...init,
      headers: init.body ? {'Content-Type': 'application/json', 'X-HG': '1'} : init.method ? {'X-HG': '1'} : {}});
    const body = await response.json().catch(() => null);
    if (response.ok) return body;
    const error = new Error(body?.error || `HTTP ${response.status}`);
    error.status = response.status; error.steer = !!body?.need_steer;
    throw error;
  }

  // ── what can be picked ──
  const tasks = () => listing?.tasks || [];
  const byId = id => tasks().find(task => task.id === id);
  function chainOf(id) { return (listing?.chains || []).find(chain => chain.tasks.includes(id)); }
  function matches(task) {
    const text = $('f-text').value.trim().toLowerCase(), state = $('f-state').value, owner = $('f-owner').value, days = Number($('f-time').value);
    if (text && !(`${task.title || ''} ${task.id}`.toLowerCase().includes(text))) return false;
    if (state && task.state !== state) return false;
    if (owner && (task.owner || '') !== owner) return false;
    return !days || (task.updated_at || 0) >= Date.now() / 1000 - days * DAY;
  }
  function warnings(group) {
    const out = [];
    if (group.unfinished_jobs?.length) out.push([`${group.unfinished_jobs.length} 个作业未结束`, 'warn']);
    if (group.originals_elsewhere?.length) out.push([`${group.originals_elsewhere.length} 个作业原件在从节点上`, 'warn']);
    return out;
  }
  function row(id, title, sub, group, extra) {
    const label = el('label', null, 'ex-task'), box = el('input');
    box.type = 'checkbox'; box.dataset.pick = id; box.checked = picked.has(id);
    const name = el('span', null, 'ex-name');
    name.append(el('b', title), ...(sub ? [el('span', sub, 'mono muted')] : []));
    const facts = el('span', null, 'ex-facts');
    for (const part of extra) facts.append(part);
    facts.append(el('span', `作业 ${group.jobs} · 结果 ${group.results} · 附件 ${group.attachments}`), el('span', `约 ${size(group.bytes)}`, 'num'));
    for (const [text, tone] of warnings(group)) facts.append(el('span', text, `tag ${tone}`));
    label.append(box, name, facts);
    return label;
  }
  function taskRow(task) {
    return row(task.id, task.title || task.id, task.id, task,
      [el('span', STATE[task.state] || task.state || DASH, 'tag'), el('span', task.owner || '无人负责', task.owner ? 'ex-owner' : 'ex-owner muted')]);
  }
  function renderTasks() {
    const list = $('tasks'), shown = tasks().filter(matches), drawn = new Set();
    list.replaceChildren();
    for (const task of shown) {
      if (drawn.has(task.id)) continue;
      const chain = chainOf(task.id);
      if (!chain) { drawn.add(task.id); list.append(taskRow(task)); continue; }
      // A chain shows whole when any of it matches; 整条链 picks every task in it.
      chain.tasks.forEach(id => drawn.add(id));
      const box = el('fieldset', null, 'ex-chain'), legend = el('legend'), all = el('label', null, 'ex-chain-all'), check = el('input');
      check.type = 'checkbox'; check.dataset.chain = chain.root;
      const count = chain.tasks.filter(id => picked.has(id)).length;
      check.checked = count === chain.tasks.length; check.indeterminate = count > 0 && count < chain.tasks.length;
      all.append(check, el('span', '整条链'));
      legend.append(el('span', `任务链 · ${byId(chain.root)?.title || chain.root} · ${chain.tasks.length} 个任务`), all);
      box.append(legend, ...chain.tasks.map(byId).filter(Boolean).map(taskRow));
      list.append(box);
    }
    if (!shown.length) list.append(el('p', tasks().length ? '没有符合筛选的任务' : '还没有任务', 'note'));
    const loose = listing.unassigned;
    if (loose && (loose.jobs || loose.results || loose.attachments)) {
      const other = row(UNASSIGNED, '未归属任务的数据', null, loose, []);
      other.classList.add('ex-unassigned');
      list.append(other);
    }
    renderSummary();
  }
  function renderSummary() {
    const chosen = [...picked].filter(id => id !== UNASSIGNED).map(byId).filter(Boolean), loose = picked.has(UNASSIGNED) ? listing.unassigned : null;
    const bytes = chosen.reduce((sum, task) => sum + task.bytes, 0) + (loose ? loose.bytes : 0), free = listing.cache?.free_bytes;
    $('summary-text').textContent = chosen.length || loose
      ? `已选 ${chosen.length} 个任务${loose ? ' 和未归属任务的数据' : ''} · 约 ${size(bytes)} · 导出缓存剩余 ${size(free)}`
      : `还没选 · 导出缓存剩余 ${size(free)}`;
    const notes = [];
    for (const [name, group] of [...chosen.map(task => [task.title || task.id, task]), ...(loose ? [['未归属任务的数据', loose]] : [])]) {
      if (group.unfinished_jobs.length) notes.push(`${name}：${group.unfinished_jobs.length} 个作业还没结束（${group.unfinished_jobs.join('、')}）。导出的是此刻的目录，导入后它们不会再运行；可以等它们结束再导出。`);
      if (group.originals_elsewhere.length) notes.push(`${name}：${group.originals_elsewhere.length} 个作业的原件还在从节点上，导出时先收回，收不回的会标为未带原件。`);
    }
    if (free != null && bytes > free) notes.unshift(`导出缓存所在磁盘剩余 ${size(free)}，这次约需 ${size(bytes)}，空间不够。`);
    $('summary-warn').replaceChildren(...notes.map(text => el('li', text)));
    const running = exports.some(item => item.state === 'running');
    $('start').disabled = (!chosen.length && !loose) || running;
    $('start').title = running ? '已经有一个导出在进行' : '';
  }
  function fillFilters() {
    for (const [id, values, label] of [['f-state', [...new Set(tasks().map(t => t.state).filter(Boolean))], s => STATE[s] || s],
                                        ['f-owner', [...new Set(tasks().map(t => t.owner).filter(Boolean))].sort(), o => o]]) {
      const select = $(id), keep = select.value;
      select.replaceChildren(select.options[0], ...values.map(value => { const option = el('option', label(value)); option.value = value; return option; }));
      select.value = values.includes(keep) ? keep : '';
    }
  }

  // ── the export files ──
  function renderExports() {
    const list = $('exports');
    list.replaceChildren(...exports.map(item => {
      const li = el('li', null, `ex-file is-${item.state}`), head = el('div', null, 'ex-file-head');
      const what = `${item.tasks.length} 个任务${item.unassigned ? ' 和未归属任务的数据' : ''}`;
      head.append(el('b', `${time(item.created_at)} · ${what}`));
      const del = el('button', '删除', 'btn sm');
      del.type = 'button'; del.dataset.delete = item.id; del.disabled = item.state === 'running';
      if (item.state === 'running') {
        const bar = el('progress'), done = item.bytes_done || 0, total = item.bytes_total;
        if (total) { bar.max = total; bar.value = done; }
        head.append(el('span', `${PHASE[item.phase] || '准备中'}${total ? ` · ${size(done)} / ${size(total)}` : ''}`, 'tag'));
        li.append(head, bar);
      } else if (item.state === 'done') {
        const link = el('a', '下载', 'btn sm primary');
        link.href = `${API}/center-export/${encodeURIComponent(item.id)}/download`; link.setAttribute('download', '');
        head.append(el('span', size(item.size), 'num'), link);
        li.append(head, el('p', `保留到 ${time(item.expires_at)} · SHA-256 ${item.sha256}`, 'note mono ex-sha'));
      } else {
        head.append(el('span', '失败', 'tag bad'));
        li.append(head, el('p', item.error || '导出失败', 'note ex-error'));
      }
      head.append(del);
      const lists = [['未带原件的作业', (item.missing_originals || []).map(m => `${m.job_id}：${m.reason}`)],
                     ['导出时还没结束的作业', item.unfinished_jobs || []]].filter(([, items]) => items.length);
      for (const [title, items] of lists) {
        const more = el('details', null, 'ex-detail');
        more.append(el('summary', `${title} ${items.length} 个`), ...items.map(text => el('p', text, 'note')));
        li.append(more);
      }
      return li;
    }));
    if (!exports.length) list.append(el('li', '还没有导出文件', 'note'));
    $('files-note').textContent = listing?.cache ? `导出文件保留 ${Math.round(listing.cache.retain_s / DAY)} 天，过期自动删除` : '';
    clearTimeout(timer);
    if (exports.some(item => item.state === 'running')) timer = setTimeout(() => readExports().catch(() => {}), 1000);
  }
  async function readExports() {
    exports = (await call('/center-export')).exports;
    renderExports();
    if (listing) renderSummary();
  }

  // ── reading, unlocking, starting ──
  async function load() {
    $('message').hidden = false; $('message').textContent = '正在读取…';
    try {
      const access = await call('/access');
      if (!access.steer) {
        $('content').hidden = true;
        $('message').textContent = access.steer_configured ? '导出的是中心的全部数据，先用操作口令解锁。' : '本节点未配置操作口令，不能导出。';
        $('unlock').hidden = !access.steer_configured;
        return;
      }
      $('unlock').hidden = true;
      const [tasksRead] = await Promise.all([call('/center-export/tasks'), readExports()]);
      listing = tasksRead;
      const known = new Set([...tasks().map(task => task.id), UNASSIGNED]);
      for (const id of [...picked]) if (!known.has(id)) picked.delete(id);
      fillFilters();
      $('excluded').replaceChildren(...(listing.excluded || []).map(text => el('li', text)));
      $('message').hidden = true; $('content').hidden = false;
      renderTasks();
    } catch (error) {
      $('message').textContent = `读不到：${error.message}`;
    }
  }
  $('unlock').addEventListener('submit', async event => {
    event.preventDefault();
    const pw = $('unlock-pw').value; $('unlock-pw').value = '';
    try { await call('/steer/unlock', {method: 'POST', body: JSON.stringify({pw})}); $('unlock-msg').textContent = ''; await load(); }
    catch (error) { $('unlock-msg').textContent = `解锁失败：${error.message}`; }
  });
  $('tasks').addEventListener('change', event => {
    const box = event.target;
    if (box.dataset.pick) box.checked ? picked.add(box.dataset.pick) : picked.delete(box.dataset.pick);
    if (box.dataset.chain) for (const id of chainOf(box.dataset.chain).tasks) box.checked ? picked.add(id) : picked.delete(id);
    renderTasks();
  });
  for (const id of ['f-text', 'f-state', 'f-owner', 'f-time']) $(id).addEventListener('input', () => renderTasks());
  $('pick-shown').addEventListener('click', () => { for (const task of tasks().filter(matches)) picked.add(task.id); renderTasks(); });
  $('pick-none').addEventListener('click', () => { picked.clear(); renderTasks(); });
  $('start').addEventListener('click', async () => {
    $('start').disabled = true; $('start-msg').textContent = '正在开始…';
    try {
      await call('/center-export', {method: 'POST', body: JSON.stringify({tasks: [...picked].filter(id => id !== UNASSIGNED), unassigned: picked.has(UNASSIGNED)})});
      $('start-msg').textContent = '';
      await readExports();
    } catch (error) {
      $('start-msg').textContent = error.steer ? '操作口令已失效，刷新后重新解锁' : `没有开始：${error.message}`;
      renderSummary();
    }
  });
  $('exports').addEventListener('click', async event => {
    const button = event.target.closest('[data-delete]');
    if (!button) return;
    if (!button.dataset.armed) {   // the second click deletes
      button.dataset.armed = '1'; button.textContent = '确认删除';
      setTimeout(() => { if (button.isConnected) { delete button.dataset.armed; button.textContent = '删除'; } }, 4000);
      return;
    }
    button.disabled = true;
    try { await call(`/center-export/${encodeURIComponent(button.dataset.delete)}`, {method: 'DELETE'}); await readExports(); }
    catch (error) { button.disabled = false; $('files-note').textContent = `没有删除：${error.message}`; }
  });
  $('refresh').addEventListener('click', load);
  load();
})();
