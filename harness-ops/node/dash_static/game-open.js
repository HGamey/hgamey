/* 打开游戏: one result of a task (an engine library, weights, …) opened in an installed game on a chosen phone, and the
   original next to it. The game's registration says where its result files go and how it starts (its `opening`); each
   opening runs as ordinary adb jobs under the task, one after another, so 数据流向 shows every step.

   What a placed file replaces is kept beside it as <file>.pf-orig, and a file that was not there gets a <file>.pf-absent
   mark. The first placement makes the mark and later ones keep it, so it always holds the original. "Open the original"
   puts each .pf-orig back or removes the file a .pf-absent mark stands for, then drops the mark; with no marks it does
   nothing, so it can run any number of times. */
window.HGGameOpen = (() => {
  'use strict';
  const STAGE = '/data/local/tmp/pf-open-', SHOT = '/data/local/tmp/pf-open-screen.png';
  const q = value => `'${String(value).replace(/'/g, `'\\''`)}'`;
  const base = path => String(path).split('/').pop();
  const glob = match => new RegExp(`^${match.replace(/[.+?^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*')}$`);
  const entries = (opening, path) => opening.files.filter(entry => glob(entry.match).test(base(path)));
  const isResult = (opening, path) => entries(opening, path).some(entry => entry.result);
  const targets = (opening, path) => entries(opening, path).flatMap(entry =>
    entry.to.map(place => `${opening.root}/${place.endsWith('/') ? place + base(path) : place}`));

  // A chosen result and the files of its job that go with it: per file name the game takes, the one nearest the result.
  function companions(opening, files, chosen) {
    const depth = file => { const a = chosen.path.split('/'), b = file.path.split('/'); let n = 0; while (n < a.length && a[n] === b[n]) n++; return n; };
    const picked = new Map([[base(chosen.path), chosen]]);
    for (const file of files) {
      if (file.job !== chosen.job || !entries(opening, file.path).length || picked.get(base(file.path)) === chosen) continue;
      const have = picked.get(base(file.path));
      if (!have || depth(file) > depth(have)) picked.set(base(file.path), file);
    }
    return [...picked.values()];
  }

  // Shell for the phone. Files are first pushed as STAGE<name>; placing backs up once, then copies.
  function placeFiles(opening, files, stage = STAGE) {
    return ['place() { mkdir -p "${2%/*}" || exit 3; if [ ! -e "$2.pf-orig" ] && [ ! -e "$2.pf-absent" ]; then if [ -e "$2" ]; then cp -p "$2" "$2.pf-orig" || exit 3; else : > "$2.pf-absent" || exit 3; fi; fi; cp "$1" "$2" && chmod 664 "$2" || exit 3; }',
      ...files.flatMap(file => targets(opening, file.path).map(target => `place ${q(stage + base(file.path))} ${q(target)}`)),
      `rm -f ${files.map(file => q(stage + base(file.path))).join(' ')}`].join('; ');
  }
  function restoreFiles(opening) {
    const lines = ['back() { if [ -e "$1.pf-orig" ]; then mv -f "$1.pf-orig" "$1" || exit 3; elif [ -e "$1.pf-absent" ]; then rm -f "$1" "$1.pf-absent" || exit 3; fi; }'];
    for (const entry of opening.files) for (const place of entry.to) {
      if (!place.endsWith('/')) lines.push(`back ${q(`${opening.root}/${place}`)}`);
      else if (!entry.match.includes('*')) lines.push(`back ${q(`${opening.root}/${place}${entry.match}`)}`);
      else lines.push(`for m in ${q(opening.root + '/' + place)}${entry.match}.pf-orig ${q(opening.root + '/' + place)}${entry.match}.pf-absent; do if [ -e "$m" ]; then back "\${m%.pf-*}"; fi; done`);
    }
    return lines.join('; ');
  }
  const stop = opening => `am force-stop ${q(opening.package)}`;
  const launch = opening => [`am start ${opening.launch.map(q).join(' ')}`, `sleep ${opening.wait_seconds}`, `screencap -p ${q(SHOT)}`].join('; ');

  // The jobs of one opening, in order: with a result, push it, place it and start; for the original, restore and start.
  function steps(opening, files, nodeId) {
    const take = files.map((file, i) => ({name: `f${i}`, target: `pf-open-${base(file.path)}`,
      source: {job_id: file.job, path: file.path, size: file.size, sha256: file.sha256}}));
    const shot = {label: '截图', argv: ['pull', SHOT, 'screen.png']};
    if (!files.length) return [{label: '还原并启动', argv: ['shell', [stop(opening), restoreFiles(opening), launch(opening)].join('; ')]}, shot];
    return [
      {label: `推送 ${files.map(file => base(file.path)).join('、')}`, argv: ['push', ...take.map(item => `@in/${item.target}`), '/data/local/tmp/'],
       inputs: {schema: 'phonefarm/job-inputs@1', node_id: nodeId, files: take, bindings: [], cwd: 'default'}},
      {label: '放入并启动', argv: ['shell', [stop(opening), placeFiles(opening, files), launch(opening)].join('; ')]}, shot];
  }

  const page = () => {
    const $ = id => document.getElementById(id), U = HGResearchTask.util, O = HGTaskOutputs, {el} = U;
    const id = new URLSearchParams(location.search).get('id') || '', task = encodeURIComponent(id);
    const API = '/dash/api', DATE = new Intl.DateTimeFormat('zh-CN', {month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false});
    let results = [], phones = [], nodeId = '', busy = false;
    const chosen = name => document.querySelector(`input[name="${name}"]:checked`)?.value;

    function render() {
      const ready = !busy && chosen('result') !== undefined && chosen('phone') !== undefined;
      $('open-result').disabled = $('open-original').disabled = !ready;
    }
    function choice(name, value, checked, ...content) {
      const row = el('li', null, 'open-choice'), label = el('label'), input = el('input');
      Object.assign(input, {type: 'radio', name, value, checked});
      input.addEventListener('change', render);
      label.append(input, el('span', null, 'open-choice-text'));
      label.lastChild.append(...content);
      row.append(label);
      return row;
    }
    async function post(spec) {
      const response = await HGTarget.fetch(`${API}/jobs`, {method: 'POST', credentials: 'same-origin',
        headers: {'Content-Type': 'application/json', 'X-HG': '1', 'X-Request-ID': crypto.randomUUID()}, body: JSON.stringify(spec)});
      const value = await response.json().catch(() => ({}));
      if (!response.ok) throw Object.assign(new Error(value.error || `HTTP ${response.status}`), {needSteer: value.need_steer === true});
      return value.job?.id || value.id;
    }
    async function finished(jid, row) {
      for (;;) {
        const job = await U.api(`/jobs/${encodeURIComponent(jid)}`);
        row.querySelector('.open-step-state').replaceChildren(el('span', HGJobState.label(job), `task-state ${HGJobState.tone(job)}`));
        if (job.state === 'finished') return job;
        await new Promise(resolve => setTimeout(resolve, 2000));
      }
    }
    async function open(withResult) {
      // The original is opened in the game of the chosen result.
      const result = results[Number(chosen('result'))], phone = phones[Number(chosen('phone'))];
      const {opening} = result.game, title = withResult ? `用 ${base(result.file.path)} 打开` : '打开原版';
      busy = true; render(); $('open-message').textContent = ''; $('open-message').classList.remove('task-error');
      $('open-steps').replaceChildren();
      try {
        const files = withResult ? await Promise.all(result.files.map(async file =>
          ({...file, ...await U.api(`/jobs/${encodeURIComponent(file.job)}/out?${new URLSearchParams({path: file.path, meta: '1'})}`)}))) : [];
        let last = null;
        for (const step of steps(opening, files, nodeId)) {
          const row = el('li', null, 'open-step'), link = el('a', '作业');
          row.append(el('span', `${withResult ? '打开游戏' : '打开原版'} · ${step.label}`, 'open-step-label'), link, el('span', '提交中…', 'open-step-state'));
          $('open-steps').append(row);
          const jid = await post({bin: 'adb', argv: ['-s', '@serial', ...step.argv], device_access: 'write', device_ref: phone.ref,
            research_id: id, label: `${withResult ? '打开游戏' : '打开原版'}：${step.label}`, ...(step.inputs ? {inputs: step.inputs} : {})});
          link.textContent = `作业 ${jid}`; link.href = HGTarget.url(`/dash/jobs#job=${encodeURIComponent(jid)}`);
          last = await finished(jid, row);
          if (last.outcome !== 'succeeded') throw new Error(`「${step.label}」没有成功（退出码 ${last.exit ?? '无'}），后面的步骤没有做。`);
          last.id = jid;
        }
        const figure = el('figure', null, 'open-shot'), image = el('img');
        image.src = HGTarget.url(`${API}/jobs/${encodeURIComponent(last.id)}/out?${new URLSearchParams({path: 'screen.png'})}`);
        image.alt = `${title} · ${phone.model || phone.device_id}`;
        figure.dataset.mode = withResult ? 'result' : 'original';
        figure.append(image, el('figcaption', `${title} · ${phone.model || phone.device_id} · ${DATE.format(new Date())}`));
        $('open-shots').querySelector(`figure[data-mode="${figure.dataset.mode}"]`)?.remove();
        $('open-shots').append(figure);
      } catch (error) {
        $('open-message').textContent = error.needSteer ? '需要先解锁操作（掌舵密码）。' : error.message;
        $('open-message').classList.add('task-error');
      } finally { busy = false; render(); }
    }

    async function load() {
      if (!id) { $('message').textContent = '没有指定任务。'; return; }
      const [record, listed, games, catalog, node] = await Promise.all([U.api(`/research-state/research/${task}`), U.api(`/jobs?research=${task}&limit=50`),
        U.api('/games'), U.api('/phones'), U.api('/node')]);
      nodeId = node.node_id;
      const back = $('open-back');
      back.href = HGTarget.url(`/dash/research?kind=research&id=${task}`);
      back.querySelector('span').textContent = record.data.title;
      document.title = `打开游戏 · ${record.data.title} · HGamey`;
      $('open-title').textContent = record.data.title;
      // The latest registration of each game that says how a result is opened in it.
      const latest = new Map();
      for (const item of games.items) if (item.manifest?.opening && !(latest.get(item.game_id)?.created_at > item.created_at)) latest.set(item.game_id, item);
      if (!latest.size) { $('message').textContent = '还没有登记了打开方式的游戏。'; return; }
      if (!record.data.deliverable) { $('message').textContent = '这个任务还没有交付成果。'; return; }
      const outputs = await O.read(record, listed.items);
      for (const game of latest.values()) {
        const opening = game.manifest.opening;
        for (const file of outputs.files) if (isResult(opening, file.path)) results.push({file, game: {...game, opening}, files: companions(opening, outputs.files, file)});
      }
      if (!results.length) { $('message').textContent = `这个任务的成果里没有 ${[...latest.values()].map(game => game.manifest.title).join('、')} 能打开的文件。`; return; }
      $('message').hidden = true; $('open').hidden = false;
      $('open-game').textContent = [...new Set(results.map(item => item.game.manifest.title))].join('、');
      $('open-results').replaceChildren(...results.map((item, i) => {
        const others = item.files.filter(file => file !== item.file).map(file => base(file.path));
        return choice('result', String(i), i === 0, el('span', O.LABEL[item.file.kind] || '文件', `output-kind k-${item.file.kind}`),
          el('strong', base(item.file.path)), el('span', `${O.size(item.file.size)} · 作业 ${item.file.job}`, 'open-meta'),
          ...(others.length ? [el('span', `一起放入：${others.join('、')}`, 'open-meta open-with')] : []));
      }));
      // A game opens on a phone that is there now.
      phones = catalog.items.filter(phone => phone.online);
      $('open-phones').replaceChildren(...phones.map((phone, i) => choice('phone', String(i), i === 0,
        el('strong', phone.model || phone.device_id), el('span', `${phone.node_name === 'local' ? '中心' : phone.node_name} · ${phone.serial}`, 'open-meta'))));
      if (!phones.length) $('open-phones').append(el('li', '现在没有在线的手机。', 'note'));
      render();
    }
    $('open-result').addEventListener('click', () => open(true));
    $('open-original').addEventListener('click', () => open(false));
    load().catch(error => {
      $('message').hidden = false;
      $('message').textContent = error.code === 'research_not_found' ? '没有这个任务。' : `没读到：${error.message}`;
      $('message').classList.add('task-error');
    });
  };
  if (document.getElementById('open-steps')) page();
  return {companions, placeFiles, restoreFiles, steps, targets, isResult, STAGE, SHOT};
})();
