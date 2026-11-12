/* 数据流向: one task, job by job, oldest first. What the platform received from the agent (the command, the code commit
   it ran, the files it was given), what it sent to the phone (packages installed, files pushed, commands run, as the
   job's device record saw them) and what came back to the agent (the outcome, the files written, the log). */
(() => {
  'use strict';
  const $ = id => document.getElementById(id), U = HGResearchTask.util, O = HGTaskOutputs, {el, tag} = U;
  const id = new URLSearchParams(location.search).get('id') || '', task = encodeURIComponent(id);
  const JOBS = 50, COMMANDS_SHOWN = 8;
  const DATE = new Intl.DateTimeFormat('zh-CN', {month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false});
  const when = at => at ? DATE.format(new Date(at * 1000)) : '';
  const ACCESS = {write: '写', read: '读', none: '不用手机', recovery: '恢复'};
  const name = path => String(path).split('/').pop();
  const short = sha => sha ? sha.slice(0, 8) : '';
  const totals = {jobs: 0, read: 0, outputs: 0, commands: 0, commits: new Set(), inputs: 0, installs: 0, pushes: 0, shells: 0, unrecorded: 0, files: 0, images: 0, logs: 0};

  // The job's device record: one line per adb call it made (out/.device-io.jsonl); null when it has none.
  async function deviceRecord(job) {
    const response = await HGTarget.fetch(`/dash/api/jobs/${encodeURIComponent(job.id)}/out?${new URLSearchParams({path: '.device-io.jsonl'})}`,
      {credentials: 'same-origin', cache: 'no-store'});
    if (response.status === 404) return null;
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return (await response.text()).split('\n').filter(Boolean).flatMap(line => { try { return [JSON.parse(line)]; } catch (_) { return []; } });
  }
  // A job whose own command is adb: its arguments are what it sent.
  function adbArgs(argv) {
    const rest = [...argv];
    let serial = null;
    while (rest[0]?.startsWith('-')) { const flag = rest.shift(); if (['-s', '-t', '-H', '-P', '-L'].includes(flag)) { const value = rest.shift(); if (flag === '-s') serial = value; } }
    const [command, ...operands] = rest, plain = operands.filter(arg => !arg.startsWith('-'));
    if (command === 'push' && plain.length >= 2) return [{serial, command, files: plain.slice(0, -1).map(path => ({path})), to: plain.at(-1)}];
    if (command === 'install' || command === 'install-multiple') return [{serial, command: 'install', files: plain.map(path => ({path}))}];
    if (['shell', 'exec-out', 'exec-in'].includes(command)) return [{serial, command, text: operands.join(' ')}];
    if (command === 'pull' && plain.length) return [{serial, command, source: plain[0], to: plain[1] || null}];
    return command ? [{serial, command, args: operands}] : [];
  }
  const phoneOf = job => job.device_binding ? `${job.device_binding.device_id} · ${job.device_binding.serial}`
    : (job.resources || []).filter(item => item.startsWith('device:')).map(item => item.slice(7)).join('、');

  function block(title, ...content) {
    const box = el('div', null, 'flow-block');
    box.append(el('h4', title), ...content);
    return box;
  }
  // Items as list rows (a ready row, such as a file's, is kept as it is); past `shown` they open on request.
  function list(items, shown = items.length) {
    const ul = el('ul', null, 'flow-list'), rows = items.map(item => { if (item instanceof HTMLLIElement) return item; const li = el('li'); li.append(...[item].flat()); return li; });
    rows.slice(shown).forEach(row => { row.hidden = true; });
    ul.append(...rows);
    if (rows.length > shown) {
      const more = el('button', `还有 ${rows.length - shown} 条`, 'link-btn'); more.type = 'button';
      more.onclick = () => { rows.forEach(row => { row.hidden = false; }); more.remove(); };
      const li = el('li'); li.append(more); ul.append(li);
    }
    return ul;
  }
  // An output by name and size, a download; its full path shows on hover.
  function outputLine(file) {
    const link = el('a', name(file.path), 'flow-file');
    link.href = HGTarget.url(`/dash/api/jobs/${encodeURIComponent(file.job)}/out?${new URLSearchParams({path: file.path})}`);
    link.download = name(file.path); link.title = file.path;
    const shown = window.HGMedia?.preview(`/dash/api/jobs/${encodeURIComponent(file.job)}/out?${new URLSearchParams({path: file.path})}`, file.path);
    return [link, ...(file.size != null ? [el('span', O.size(file.size), 'flow-meta')] : []), ...(shown ? [shown] : [])];
  }
  const fileLine = file => [el('span', name(file.path), 'flow-file'), ...(file.size != null ? [el('span', O.size(file.size), 'flow-meta')] : []),
    ...(file.sha256 ? [el('span', 'sha ' + short(file.sha256), 'flow-meta')] : [])];

  // 平台从 agent 收到: the command, the commit it ran, the files it was given.
  function received(job, commit, inputs) {
    const cell = el('div', null, 'flow-cell flow-in'), command = el('code', [job.bin, ...(job.argv || [])].join(' '), 'flow-command');
    cell.append(block('命令', command, el('p', [job.actor_id && `来自 ${job.actor_id}`, when(job.started)].filter(Boolean).join(' · '), 'flow-meta')));
    if (commit) { const commits = el('ul', null, 'output-commits'); commits.append(O.commitRow(commit)); cell.append(block('代码提交', commits)); }
    if (inputs?.files.length) cell.append(block(`输入文件 · ${inputs.files.length}`, list(inputs.files.map(file => [el('span', file.path, 'flow-file'), el('span', O.size(file.size), 'flow-meta')]), 6)));
    else if (inputs?.error) cell.append(block('输入文件', el('p', inputs.error, 'note')));
    totals.commands += 1; totals.inputs += inputs?.files.length || 0;
    if (commit) totals.commits.add(commit.commit);
    return cell;
  }
  // 发给手机: which phone, and what the device record saw go to it.
  function sent(job, record) {
    const cell = el('div', null, 'flow-cell flow-phone'), phone = phoneOf(job);
    if (!phone && !record?.length) { cell.append(el('p', '没有用手机', 'note')); return cell; }
    if (phone) cell.append(el('p', `${phone}${ACCESS[job.device_access] ? ' · ' + ACCESS[job.device_access] : ''}`, 'flow-phone-name'));
    if (!record) {
      cell.append(el('p', '没有逐条记录', 'note'));
      totals.unrecorded += 1;
      return cell;
    }
    const installs = record.filter(line => line.command === 'install'), pushes = record.filter(line => line.command === 'push');
    // The platform looks at the phone's apps before a job starts (marked by its status line); that is not the job's.
    const check = line => (line.text || '').includes('__PF_APP_STATUS__');
    const shells = record.filter(line => ['shell', 'exec-out', 'exec-in'].includes(line.command) && !check(line));
    const checks = record.filter(check).length;
    const others = record.filter(line => !['install', 'push', 'shell', 'exec-out', 'exec-in', 'pull'].includes(line.command));
    if (installs.length) cell.append(block(`装的包 · ${installs.length}`, list(installs.flatMap(line => line.files.map(fileLine)))));
    if (pushes.length) cell.append(block(`推的文件 · ${pushes.length}`, list(pushes.map(line => [...line.files.flatMap(fileLine), el('span', '→ ' + line.to, 'flow-meta')]), COMMANDS_SHOWN)));
    if (shells.length) cell.append(block(`执行的命令 · ${shells.length}`, list(shells.map(line => el('code', line.text || '（交互 shell）')), COMMANDS_SHOWN)));
    if (others.length) cell.append(block(`其他 adb 命令 · ${others.length}`, list(others.map(line => el('code', [line.command, ...(line.args || [])].join(' '))), COMMANDS_SHOWN)));
    if (!installs.length && !pushes.length && !shells.length && !others.length) cell.append(el('p', '作业本身没有发给手机的内容', 'note'));
    if (checks) cell.append(el('p', `开工前平台查看了手机上的应用（${checks} 条命令）`, 'note flow-check'));
    totals.installs += installs.length; totals.pushes += pushes.length; totals.shells += shells.length;
    return cell;
  }
  // 返回给 agent: how it ended, what it wrote (by kind, screenshots by name), what it pulled from the phone, its log.
  // The files it wrote are read after every row is up: a job's output tree can take many reads.
  function returned(job, record) {
    const cell = el('div', null, 'flow-cell flow-out'), head = el('p', null, 'flow-outcome');
    head.append(tag(HGJobState.label(job), HGJobState.tone(job)), ...(job.exit != null ? [el('span', `退出码 ${job.exit}`, 'flow-meta')] : []));
    cell.append(head, el('p', '读取中…', 'note flow-outputs-reading'));
    const pulled = (record || []).filter(line => line.command === 'pull');
    if (pulled.length) cell.append(block(`从手机取回 · ${pulled.length}`, list(pulled.map(line => el('code', `${line.source} → ${line.to || '.'}`)), COMMANDS_SHOWN)));
    const log = el('a', '作业日志', 'flow-log'); log.href = HGTarget.url(`/dash/jobs#job=${encodeURIComponent(job.id)}`);
    cell.append(log);
    totals.logs += 1;
    return cell;
  }
  function outputBlocks(outputs) {
    if (!outputs.files.length) return [block('输出', el('p', outputs.error ? `没读到：${outputs.error}` : '没有输出文件', 'note'))];
    const counts = {};
    for (const file of outputs.files) counts[file.kind] = (counts[file.kind] || 0) + 1;
    const tally = el('p', null, 'output-tally');
    tally.append(...Object.entries(counts).map(([kind, n]) => el('span', `${O.LABEL[kind]} ${n}`, `output-count k-${kind}`)));
    const images = outputs.files.filter(file => file.kind === 'image'), key = outputs.files.filter(file => ['weights', 'engine', 'report', 'video'].includes(file.kind));
    totals.files += outputs.files.length; totals.images += images.length;
    return [block(`输出 · ${outputs.files.length}${outputs.more ? '+' : ''}`, tally, ...(key.length ? [list(key.map(outputLine), 6)] : [])),
      ...(images.length ? [block(`截图 · ${images.length}`, list(images.map(outputLine), 4))] : [])];
  }

  function jobRow(job, parts) {
    const row = el('li', null, 'flow-job'), head = el('div', null, 'flow-job-head'), link = el('a', `作业 ${job.id}`);
    row.dataset.job = job.id;
    link.href = HGTarget.url(`/dash/jobs#job=${encodeURIComponent(job.id)}`);
    head.append(link, el('span', job.label || '', 'flow-label'), el('span', when(job.started), 'flow-meta'));
    const columns = el('div', null, 'flow-columns');
    columns.append(received(job, parts.commit, parts.inputs), sent(job, parts.record), returned(job, parts.record));
    row.append(head, columns);
    return row;
  }
  function showTotals() {
    $('total-in').textContent = [`${totals.commands} 条命令`, `${totals.commits.size} 个代码提交`, `${totals.inputs} 个输入文件`].join(' · ');
    $('total-phone').textContent = [`装包 ${totals.installs}`, `推文件 ${totals.pushes}`, `命令 ${totals.shells}`, ...(totals.unrecorded ? [`${totals.unrecorded} 个作业没有逐条记录`] : [])].join(' · ');
    $('total-out').textContent = [`${totals.files} 个输出文件`, `截图 ${totals.images}`, `日志 ${totals.logs}`].join(' · ');
    // Until every job and then every job's outputs are read the sums are partial.
    $('flow-progress').textContent = totals.read < totals.jobs ? `读取中 ${totals.read}/${totals.jobs}`
      : totals.outputs < totals.jobs ? `输出读取中 ${totals.outputs}/${totals.jobs}` : '';
    $('flow-progress').hidden = totals.outputs >= totals.jobs;
  }

  async function load() {
    if (!id) { $('message').textContent = '没有指定任务。'; return; }
    const [record, listed] = await Promise.all([U.api(`/research-state/research/${task}`), U.api(`/jobs?research=${task}&limit=${JOBS}`)]);
    const back = $('flow-back');
    back.href = HGTarget.url(`/dash/research?kind=research&id=${task}`);
    back.querySelector('span').textContent = record.data.title;
    document.title = `数据流向 · ${record.data.title} · HGamey`;
    $('flow-title').textContent = record.data.title;
    const jobs = listed.items.slice().sort((a, b) => (a.started || a.created || 0) - (b.started || b.created || 0));
    if (!jobs.length) { $('message').textContent = '这个任务还没有作业。'; return; }
    $('message').textContent = listed.next_cursor ? `只列出最近 ${JOBS} 个作业。` : '';
    $('message').hidden = !listed.next_cursor;
    $('flow').hidden = false;
    // The commits the jobs ran, in one read.
    const revs = [...new Set(jobs.map(job => job.experiment?.commit).filter(Boolean))];
    const found = revs.length ? await O.lookup(revs) : null;
    const known = new Map((found?.items || []).map(item => [item.rev, item]));
    const commitOf = job => job.experiment?.commit && {...(known.get(job.experiment.commit) || O.unknownCommit(job.experiment.commit, '', found?.repository)), source: '运行的代码'};
    const slots = jobs.map(job => { const slot = el('li', '读取中…', 'flow-job note'); slot.dataset.job = job.id; return slot; });
    $('flow-jobs').replaceChildren(...slots);
    totals.jobs = jobs.length;
    showTotals();
    const rows = await O.pool(jobs.map((job, i) => [job, slots[i]]), 6, async ([job, slot]) => {
      const given = job.input_preparation?.entries > 0 || job.input_preparation?.declared_bytes > 0;
      const [record, inputs] = await Promise.all([
        job.bin === 'adb' ? Promise.resolve(adbArgs(job.argv || [])) : deviceRecord(job).catch(() => null),
        given ? O.tree(job.id, 'input-files', entry => entry === 'experiment').catch(error => ({files: [], error: error.code === 'inputs_elsewhere' ? '输入在执行它的电脑上' : error.message})) : null]);
      const row = jobRow(job, {commit: commitOf(job), record, inputs});
      slot.replaceWith(row);
      return row;
    }, () => { totals.read += 1; showTotals(); });
    await O.pool(jobs.map((job, i) => [job, rows[i]]), 6, async ([job, row]) => {
      const outputs = await O.tree(job.id).catch(error => ({files: [], error: error.message}));
      if (row instanceof Element) row.querySelector('.flow-outputs-reading')?.replaceWith(...outputBlocks(outputs));
    }, () => { totals.outputs += 1; showTotals(); });
  }
  load().catch(error => {
    $('message').hidden = false;
    $('message').textContent = error.code === 'research_not_found' ? '没有这个任务。' : `没读到数据流向：${error.message}`;
    $('message').classList.add('task-error');
  });
})();
