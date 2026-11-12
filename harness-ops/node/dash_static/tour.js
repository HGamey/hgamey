/* 我来当一次 Agent: the five steps an agent takes, run for real under a temporary identity. For each step, side by
   side: what the agent sent, what the platform answered and what a person sees of it on the web. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const el = (tag, text, cls) => { const n = document.createElement(tag); if (text != null) n.textContent = String(text); if (cls) n.className = cls; return n; };
  const link = (text, href) => { const a = el('a', text); a.href = HGTarget.url(href); return a; };
  const TIME = new Intl.DateTimeFormat('zh-CN', {month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false});
  const when = t => t ? TIME.format(new Date(t * 1000)) : '';
  const STEPS = [
    ['checkin', '签到', '告诉平台我在，问有什么要做'],
    ['claim', '认领任务', '认领「体验：给一台空闲手机截一张图」，负责人写成我'],
    ['shot', '让空闲手机截图', '挑一台空闲的手机，排一个截图作业'],
    ['note', '写进展留言', '在任务里留言，附上截图'],
    ['deliver', '交付', '交一份带截图的结论'],
  ];
  const STATES = {active: '进行中', delivered: '已交付', closed: '已关闭', paused: '已暂停'};
  const ENDED = {delivered: '已交付', ended: '手动结束', left: '放了一天，自动结束'};
  let tour = null, busy = '', pending = null, timer = null, picture = {key: '', url: ''};

  async function call(path, body) {
    const init = body === undefined ? {credentials: 'same-origin', cache: 'no-store'}
      : {method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json', 'X-HG': '1'}, body: JSON.stringify(body)};
    const response = await HGTarget.fetch('/dash/api' + path, init);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) { const error = new Error(data.error || 'HTTP ' + response.status); error.data = data; throw error; }
    return data;
  }
  function message(text, bad) { $('tour-msg').textContent = text || ''; $('tour-msg').className = bad ? 'msg err' : 'msg'; }

  async function act(path, label) {
    if (busy) return;
    busy = label; message(''); render();
    try { show(await call(path, {})); }
    catch (error) {
      if (error.data?.need_steer) { pending = () => act(path, label); $('tour-unlock').hidden = false; $('tour-pw').focus(); message('先解锁掌舵', true); }
      else { if (error.data?.tour) show(error.data.tour); message(error.message, true); }
    } finally { busy = ''; render(); }
  }
  function show(value) {
    tour = value;
    const url = new URL(location.href); url.searchParams.set('id', tour.id); history.replaceState(null, '', url);
    clearTimeout(timer);
    if (!tour.ended && tour.seen.shot === 'waiting') timer = setTimeout(reload, 2000);
    render();
  }
  async function reload() {
    try { show(await call('/tour/' + encodeURIComponent(tour.id))); } catch (error) { message(error.message, true); }
  }

  // The screenshot, read once through the dashboard's own job-output route.
  function shotImage() {
    const s = tour.seen, image = el('img', null, 'tour-shot'); image.alt = (s.phone?.name || '手机') + ' 的截图';
    const key = s.job.id + ':' + s.picture;
    if (picture.key === key && picture.url) image.src = picture.url;
    else if (picture.key !== key) {
      picture = {key, url: ''};
      HGTarget.fetch(`/dash/api/jobs/${encodeURIComponent(s.job.id)}/out?${new URLSearchParams({path: s.picture})}`, {credentials: 'same-origin'})
        .then(r => r.ok ? r.blob() : Promise.reject(new Error('HTTP ' + r.status)))
        .then(blob => { if (picture.key === key) { picture.url = URL.createObjectURL(blob); render(); } }, () => {});
    }
    return image;
  }

  function sent(key, result) {
    const box = [el('p', STEPS.find(s => s[0] === key)[2])];
    if (result) { const raw = el('details', null, 'tour-raw'); raw.append(el('summary', '命令'), el('pre', result.command)); box.push(raw); }
    return box;
  }
  function answered(key, result, failed) {
    if (!result) return [el('p', '—', 'muted')];
    const lines = (result.stdout || '').split('\n').map(line => line.trim()).filter(Boolean);
    const plain = failed ? el('p', '没成：' + ((result.stderr || result.stdout || '').trim().split('\n').slice(-2).join(' ') || `退出码 ${result.code}`), 'err')
      : key === 'shot' ? el('p', '截图作业排上了')
      : el('p', receipt(result.stdout) || lines.slice(0, 4).join('\n') || '收到了', 'tour-plain');
    const raw = el('details', null, 'tour-raw');
    raw.append(el('summary', '原始输出'), el('pre', [result.stdout, result.stderr].filter(Boolean).join('\n') || '（没有输出）'));
    return [plain, raw];
  }
  // A write's receipt in words: what it recorded or changed.
  const FIELDS = {owner: '负责人', state: '状态', state_reason: '状态说明', deliverable: '交付内容', title: '标题', objective: '目标'};
  function receipt(stdout) {
    let value; try { value = JSON.parse(stdout); } catch (_) { return ''; }
    if (value?.object?.kind === 'note') return '记下了一条留言';
    if (value?.object?.kind !== 'research') return '';
    const changed = (value.changed_fields || []).map(name => FIELDS[name] || name);
    return changed.length ? '任务改了：' + changed.join('、') : '任务没有变';
  }
  function card(title, ...rest) { const box = el('div', null, 'tour-card'); box.append(el('strong', title), ...rest.filter(Boolean)); return box; }
  function seen(key) {
    const s = tour.seen, task = s.task;
    if (key === 'checkin') return card(s.agent.name, el('p', `签到于 ${when(s.agent.checkin_at)}`), link('在 Agent 列表里', '/dash/research#agents-directory'));
    if (key === 'claim') return card(task.title, el('p', `${STATES[task.state] || task.state} · 负责人 ${task.owner || '无人'}`), link('打开任务', task.url));
    if (key === 'shot') {
      const state = {waiting: el('p', '手机正在截图…', 'tour-waiting'), failed: el('p', '截图没成', 'err'), ready: shotImage()}[s.shot];
      return card(s.phone?.name || '手机', state, link('在作业页看', `/dash/jobs#job=${encodeURIComponent(s.job.id)}`));
    }
    if (key === 'note') {
      const note = s.notes[0];
      return note ? card('任务里的留言', el('p', note.text), s.picture ? shotImage() : null, link('在任务页看', `${task.url}#note-${encodeURIComponent(note.id)}`)) : el('p', '—', 'muted');
    }
    return card(task.title, el('p', STATES[task.state] || task.state), link('看交付报告', task.report_url));
  }

  function render() {
    const ended = !!tour?.ended;
    $('tour-start').hidden = !!tour && !ended; $('tour-start').textContent = tour ? '再来一次' : '开始';
    $('tour-start').disabled = !!busy; $('tour-end').hidden = !tour || ended; $('tour-end').disabled = !!busy;
    $('tour-who').hidden = !tour;
    if (tour) $('tour-who').textContent = `临时身份 ${tour.agent}` + (ended ? ` · ${ENDED[tour.end_reason] || '已结束'}` : '');
    // Before a tour starts, the five steps are listed by name.
    if (!tour) {
      $('tour-steps').replaceChildren(...STEPS.map(([key, title], index) => {
        const li = el('li', null, 'tour-step'), head = el('div', null, 'tour-step-head'); li.dataset.step = key; li.dataset.state = 'later';
        head.append(el('span', index + 1, 'tour-num'), el('h2', title)); li.append(head); return li;
      }));
      return;
    }
    const redo = tour.seen.shot === 'failed' && !tour.steps.note;
    $('tour-steps').replaceChildren(...STEPS.map(([key, title], index) => {
      const result = tour.steps[key], failed = !result && tour.failed?.step === key ? tour.failed : null;
      const next = !ended && (redo ? key === 'shot' : tour.next === key);
      const li = el('li', null, 'tour-step'); li.dataset.step = key;
      li.dataset.state = failed || key === 'shot' && redo ? 'failed' : result ? 'done' : next ? 'next' : 'later';
      const head = el('div', null, 'tour-step-head');
      head.append(el('span', index + 1, 'tour-num'), el('h2', title));
      if (result && !(key === 'shot' && redo)) head.append(el('span', '完成', 'tag ok'));
      if (next) {
        const button = el('button', busy === key ? '正在做…' : result || failed ? '重做这一步' : '做这一步', 'btn primary sm');
        button.type = 'button'; button.disabled = !!busy || (key === 'note' || key === 'deliver') && tour.seen.shot !== 'ready';
        button.addEventListener('click', () => act(`/tour/${encodeURIComponent(tour.id)}/${key}`, key));
        head.append(button);
      }
      li.append(head);
      if (result || failed || next) {
        const cols = el('div', null, 'tour-cols');
        for (const [name, parts] of [['发出的', sent(key, result || failed)], ['平台返回的', answered(key, result || failed, !!failed)],
                                     ['网页上看到的', [result ? seen(key) : el('p', '—', 'muted')]]]) {
          const col = el('section', null, 'tour-col'); col.append(el('h3', name), ...parts); cols.append(col);
        }
        li.append(cols);
      }
      return li;
    }));
  }

  $('tour-start').addEventListener('click', () => act('/tour', 'start'));
  $('tour-end').addEventListener('click', () => act(`/tour/${encodeURIComponent(tour.id)}/end`, 'end'));
  $('tour-unlock').addEventListener('submit', async event => {
    event.preventDefault();
    try { await call('/steer/unlock', {pw: $('tour-pw').value}); }
    catch (error) { message(error.message, true); return; }
    $('tour-pw').value = ''; $('tour-unlock').hidden = true; message('');
    const then = pending; pending = null; then?.();
  });

  (async () => {
    try {
      const id = new URLSearchParams(location.search).get('id');
      const open = id ? null : (await call('/tour')).open;
      if (id || open.length) show(await call('/tour/' + encodeURIComponent(id || open[0].id)));
      else render();
    } catch (error) { render(); message(error.message, true); }
  })();
})();
