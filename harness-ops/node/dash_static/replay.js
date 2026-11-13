/* 我来当一次 Agent · 回放: a delivered task played back step by step from what the platform kept of it. Each step, side
   by side: what was sent (a sentence first, the request folded), what the platform answered and what a person saw of
   it on the web then. A time strip and ←/→ move through the steps. The page only reads. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const el = (tag, text, cls) => { const n = document.createElement(tag); if (text != null) n.textContent = String(text); if (cls) n.className = cls; return n; };
  const link = (text, href) => { const a = el('a', text); a.href = HGTarget.url(href); return a; };
  const tag = (text, tone) => el('span', text, ('tag ' + (tone || '')).trim());
  const tags = (...items) => { const p = el('p', null, 'replay-tags'); p.append(...items.filter(Boolean)); return p.childElementCount ? p : null; };
  const TIME = new Intl.DateTimeFormat('zh-CN', {month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false});
  const DAY = new Intl.DateTimeFormat('zh-CN', {month: 'numeric', day: 'numeric'});
  const when = t => t ? TIME.format(new Date(t * 1000)) : '';
  const SENT = {agent: 'Agent 发出的', human: '人在网页上发出的', admin: '管理员发出的', platform: '发生的'};
  const NAME = {human: '人', admin: '管理员', platform: '平台'};
  const who = w => NAME[w.kind] || w.name;
  const DIRECTION_TONE = {verified: 'ok', no_gain: 'off', rejected: 'off', deferred: 'off', proposed: 'off', running: 'acc', ready: 'acc', preparing: 'acc'};
  const DECISION = {adopt: ['采纳', 'ok'], defer: ['暂缓', 'warn'], reject: ['拒绝', 'bad']};
  const OUTCOME = {succeeded: ['成功', 'ok'], failed: ['失败', 'bad'], cancelled: ['已取消', 'off'], interrupted: ['已中断', 'bad'], unknown: ['结果未知', 'warn']};
  const MEDIA = /\.(png|jpe?g|webp|gif|mp4|webm)$/i;
  let replay = null, current = null, keyOnly = true, as = '';
  const outputs = new Map(), pictures = new Map();   // job id → its first pictures and videos; slot → its picture; read once

  async function get(path) {
    const response = await HGTarget.fetch('/dash/api' + path, {credentials: 'same-origin', cache: 'no-store'});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || 'HTTP ' + response.status);
    return data;
  }
  function message(text, bad) { $('replay-msg').textContent = text || ''; $('replay-msg').className = bad ? 'msg err' : 'msg'; }

  // The steps shown: the key ones unless all are asked for; as one agent, its own steps, people's and its jobs' ends.
  const mine = step => !as || step.who.name === as || step.who.kind === 'human' || step.who.kind === 'admin' || step.who.for === as;
  const visible = () => replay.steps.filter(step => (!keyOnly || step.key || step === current) && mine(step));

  // ── the three columns ──
  function raw(label, text) {
    const box = el('details', null, 'tour-raw');
    box.append(el('summary', label), el('pre', text));
    return box;
  }
  const json = value => JSON.stringify(value, null, 2);
  function sentParts(step) {
    const parts = [el('p', step.summary, 'replay-say')], sent = step.sent;
    if (sent) parts.push(raw('请求', `${sent.method} ${sent.path}\n` + (sent.headers ? Object.entries(sent.headers).map(([k, v]) => `${k}: ${json(v)}`).join('\n') : json(sent.body))));
    return parts;
  }
  function answerParts(step) {
    const answer = step.answer || {};
    const parts = [el('p', answer.text || '—', answer.text ? 'tour-plain' : 'muted')];
    if (answer.raw) parts.push(raw(step.sent ? '原始回执' : '原始记录', (answer.status ? answer.status + '\n' : '') + json(answer.raw)));
    return parts;
  }

  // Pictures and videos a note, a direction or a job showed, read when they come near the screen.
  function mediaSource(ref) {
    if (ref.kind === 'attachment' && ref.attachment_id) return [`/dash/api/research-attachments/${encodeURIComponent(ref.attachment_id)}`, ref.filename || ''];
    if (ref.kind === 'job' && ref.job_id && ref.path) return [`/dash/api/jobs/${encodeURIComponent(ref.job_id)}/out?${new URLSearchParams({path: ref.path})}`, ref.path];
    return null;
  }
  function media(refs, most = 4) {
    const shown = (refs || []).map(mediaSource).filter(source => source && MEDIA.test(source[1]));
    if (!shown.length && !(refs || []).length) return null;
    const box = el('div', null, 'replay-media');
    for (const [source, name] of shown.slice(0, most)) { const preview = window.HGMedia?.preview(source, name); if (preview) box.append(preview); }
    const rest = (refs || []).length - Math.min(shown.length, most);
    if (rest > 0) box.append(el('span', `另有 ${rest} 份材料`, 'muted'));
    return box;
  }
  function card(title, ...rest) { const box = el('div', null, 'tour-card'); box.append(el('strong', title), ...rest.filter(Boolean)); return box; }
  const text = value => value ? el('p', value, 'replay-text') : null;
  // A report's text without its markup: headings, emphasis and links as words, a picture as its caption.
  const plain = value => (value || '').replace(/!\[([^\]]*)\]\([^)]*\)/g, (_, alt) => `〔图${alt ? '：' + alt : ''}〕`)
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1').replace(/^#{1,6}\s*/gm, '').replace(/\*\*([^*]+)\*\*/g, '$1').replace(/`([^`]+)`/g, '$1');

  function noteCard(s) {
    const quote = s.reply_to ? el('blockquote', `回复${s.reply_to.who ? ' ' + s.reply_to.who : ''}：${s.reply_to.text || s.reply_to.id}`, 'replay-quote') : null;
    return card(`${who(s.who)} 的留言`, tags(s.decision && DECISION[s.decision] && tag(...DECISION[s.decision]), s.ask && tag('提问', 'warn'), s.to && tag('给 ' + s.to, 'plain')),
                quote, text(s.text), media(s.evidence), link('在任务页看', s.url));
  }
  // A blind comparison as a person was asked it: its slots, by letter.
  function slots(s) {
    const box = el('div', null, 'replay-slots');
    for (const slot of s.evaluation.slots || []) {
      const tile = el('figure', null, 'replay-slot'), name = el('figcaption', slot.slot), url = HGTarget.url('/dash/api' + slot.url);
      if (slot.kind === 'video') {
        const player = el('video'); Object.assign(player, {controls: true, preload: 'metadata', playsInline: true, muted: true});
        player.src = url + '#t=0.1'; player.onerror = () => player.replaceWith(el('span', '读不到', 'muted')); tile.append(player);
      } else {
        const image = el('img'); image.alt = '候选 ' + slot.slot; tile.append(image);
        if (!pictures.has(slot.url)) {
          pictures.set(slot.url, HGTarget.fetch('/dash/api' + slot.url, {credentials: 'same-origin'})
            .then(r => r.ok ? r.blob() : Promise.reject(new Error('HTTP ' + r.status))).then(blob => URL.createObjectURL(blob)));
        }
        pictures.get(slot.url).then(src => { image.src = src; }, () => image.replaceWith(el('span', '读不到', 'muted')));
      }
      tile.append(name); box.append(tile);
    }
    return box;
  }
  function requestCard(s) {
    const open = s.state === 'open';
    if (s.evaluation) {
      return card('盲评：' + s.title, tags(tag(open ? '等人评' : '已关闭', open ? 'warn' : 'off')),
                  text(s.need), !open && s.close_reason ? el('p', s.close_reason, 'muted') : null, slots(s), link(open ? '去评' : '打开请求', s.url));
    }
    return card('请人协助：' + s.title, tags(tag(open ? '等人处理' : '已关闭', open ? 'warn' : 'off')),
                text(s.need), link('打开请求', s.url));
  }
  // What a job left: its first pictures and videos, looked for a few directories deep. A level's directories are read
  // together, so the wait grows with the depth, not with the number of directories.
  async function firstMedia(job) {
    const found = [], list = path => get(`/jobs/${encodeURIComponent(job)}/artifacts?${new URLSearchParams({path, limit: '200'})}`);
    let level = [''], reads = 0;
    while (level.length && found.length < 4 && reads < 12) {
      level = level.slice(0, 12 - reads);
      reads += level.length;
      // The top must be readable; a directory below that cannot be read is passed over.
      const pages = await Promise.all(level.map(path => path ? list(path).catch(() => ({items: []})) : list(path)));
      const next = [];
      pages.forEach((page, i) => {
        for (const item of page.items) {
          if (level[i] === '' && (item.name.startsWith('.') || item.name === 'lease-receipts')) continue;
          if (item.kind === 'directory' && !/^gray-session-/.test(item.name)) next.push(item.path);
          else if (item.kind === 'file' && MEDIA.test(item.path) && found.length < 4) found.push({kind: 'job', job_id: job, path: item.path});
        }
      });
      level = next;
    }
    return found;
  }
  function jobCard(step) {
    const s = step.seen, state = s.state === 'queued' ? ['排队', 'warn'] : OUTCOME[s.outcome] || [s.outcome || '结束', 'off'];
    const box = card('作业：' + (s.label || s.kind || s.id), tags(tag(...state)),
                     el('p', [s.phone ? '在手机上跑' : '不占手机', s.direction?.title && '方向：' + s.direction.title].filter(Boolean).join(' · '), 'muted'));
    if (s.outputs) {
      const place = el('p', '读取输出…', 'muted');
      box.append(place);
      if (!outputs.has(s.id)) outputs.set(s.id, firstMedia(s.id).catch(() => null));
      outputs.get(s.id).then(found => {
        if (found === null) place.textContent = '输出读不到';
        else if (!found.length) place.textContent = '输出里没有图片或录屏';
        else place.replaceWith(media(found));
      });
    }
    box.append(link('在作业页看', s.url));
    return box;
  }
  function seen(step) {
    const s = step.seen;
    if (s.type === 'task') return card(s.title, el('p', `${s.state_word || s.state} · 负责人 ${s.owner || '无人'}`), s.state_reason ? el('p', s.state_reason, 'muted') : null, link('打开任务', s.url));
    if (s.type === 'report') return card(`交付报告 · 第 ${s.version} 版`, text(plain(s.summary)), s.artifacts ? el('p', `${s.artifacts} 份交付产物`, 'muted') : null, link('看交付报告', s.url));
    if (s.type === 'direction') {
      return card('方向：' + s.title, tags(tag(s.state_word || s.state, DIRECTION_TONE[s.state])),
                  text(s.state_reason || s.hypothesis), media(s.evidence), link('打开方向', s.url));
    }
    if (s.type === 'note') return noteCard(s);
    if (s.type === 'request') return requestCard(s);
    if (s.type === 'feedback') return card('人的答复：' + s.request.title, text(s.text), link('打开请求', s.url));
    if (s.type === 'job') return jobCard(step);
    return el('p', '—', 'muted');
  }

  // ── moving through the steps ──
  function go(step, focus) {
    current = step;
    const url = new URL(location.href); url.searchParams.set('id', replay.task.id); url.searchParams.set('step', step.n);
    history.replaceState(null, '', url);
    render();
    if (focus) $('replay-step').focus({preventScroll: true});
  }
  function move(by) {
    const steps = visible(), i = steps.indexOf(current), next = steps[i + by];
    if (next) go(next);
  }
  // After a filter changes, the step shown stays if it is still in, else the next one that is (or the last).
  function refilter() {
    const steps = replay.steps.filter(step => (!keyOnly || step.key) && mine(step));
    if (!steps.includes(current)) current = steps.find(step => step.n > current.n) || steps[steps.length - 1] || current;
    go(current);
  }

  function render() {
    const steps = visible(), i = steps.indexOf(current);
    $('replay-where').textContent = `第 ${i + 1} / ${steps.length} 步 · ${when(current.at)}`;
    $('replay-prev').disabled = i <= 0; $('replay-next').disabled = i >= steps.length - 1;
    let day = '';
    $('replay-strip').replaceChildren(...steps.map(step => {
      const li = el('li', null, 'k-' + step.who.kind), button = el('button'), date = DAY.format(new Date(step.at * 1000));
      if (day && date !== day) li.classList.add('day');
      day = date;
      button.type = 'button'; button.dataset.n = step.n; button.title = `${when(step.at)} · ${who(step.who)} · ${step.summary}`;
      button.setAttribute('aria-label', `第 ${step.n + 1} 步：${step.summary}`);
      if (step === current) button.setAttribute('aria-current', 'step');
      li.append(button); return li;
    }));
    const box = $('replay-step'), head = el('div', null, 'tour-step-head');
    head.append(el('span', i + 1, 'tour-num'), el('span', who(current.who), 'replay-who k-' + current.who.kind), el('time', when(current.at), 'muted'));
    if (!current.key) head.append(tag('细节', 'off'));
    const cols = el('div', null, 'tour-cols');
    for (const [name, parts] of [[SENT[current.who.kind] || '发出的', sentParts(current)], ['平台返回的', answerParts(current)], ['网页上看到的', [seen(current)]]]) {
      const col = el('section', null, 'tour-col'); col.append(el('h3', name), ...parts); cols.append(col);
    }
    box.replaceChildren(head, cols);
    const strip = $('replay-strip'), tick = strip.querySelector('[aria-current]')?.parentNode;
    if (tick && (tick.offsetLeft < strip.scrollLeft || tick.offsetLeft + tick.offsetWidth > strip.scrollLeft + strip.clientWidth)) strip.scrollLeft = tick.offsetLeft - strip.clientWidth / 2;
    $('replay-count').textContent = `全部 ${steps.length} 步`;
    $('replay-list').replaceChildren(...steps.map(step => {
      const li = el('li'), button = el('button', null, 'replay-row');
      button.type = 'button'; button.dataset.n = step.n;
      if (step === current) button.setAttribute('aria-current', 'step');
      button.append(el('time', when(step.at), 'muted'), el('span', who(step.who), 'replay-who k-' + step.who.kind), el('span', step.summary));
      li.append(button); return li;
    }));
  }

  async function open(id, step) {
    message('读取中…');
    try { replay = await get('/replay?' + new URLSearchParams({id})); }
    catch (error) { $('replay-body').hidden = true; message('没读到这个任务的记录：' + error.message, true); return; }
    message('');
    const task = replay.task, steps = replay.steps;
    if (![...$('replay-task').options].some(option => option.value === id)) $('replay-task').prepend(new Option(task.title || id, id));
    $('replay-task').value = id;
    document.title = `回放 · ${task.title || id} · HGamey`;
    const about = $('replay-about');
    about.replaceChildren(`负责人 ${task.owner || '无人'} · ${steps.length} 步 · ${when(steps[0]?.at)} 至 ${when(steps[steps.length - 1]?.at)} · `, link('打开任务', task.url));
    about.hidden = false;
    $('replay-as').replaceChildren(new Option('所有人', ''), ...replay.agents.map(name => new Option(name, name)));
    as = ''; keyOnly = true; $('replay-key').checked = true;
    if (!steps.length) { $('replay-body').hidden = true; message('这个任务没有记录'); return; }
    const asked = steps.find(s => String(s.n) === step);
    if (asked && !asked.key) { keyOnly = false; $('replay-key').checked = false; }
    $('replay-body').hidden = false;
    go(asked || steps[0]);
  }

  $('replay-prev').addEventListener('click', () => move(-1));
  $('replay-next').addEventListener('click', () => move(1));
  $('replay-key').addEventListener('change', event => { keyOnly = event.target.checked; refilter(); });
  $('replay-as').addEventListener('change', event => { as = event.target.value; refilter(); });
  $('replay-task').addEventListener('change', event => open(event.target.value));
  for (const id of ['replay-strip', 'replay-list']) {
    $(id).addEventListener('click', event => {
      const button = event.target.closest('button[data-n]');
      if (button) go(replay.steps[Number(button.dataset.n)], true);
    });
  }
  document.addEventListener('keydown', event => {
    if (!replay || !current || event.altKey || event.ctrlKey || event.metaKey || /^(INPUT|SELECT|TEXTAREA)$/.test(event.target.tagName)) return;
    if (event.key === 'ArrowLeft') { event.preventDefault(); move(-1); }
    if (event.key === 'ArrowRight') { event.preventDefault(); move(1); }
  });

  (async () => {
    const params = new URLSearchParams(location.search);
    let tasks = [];
    try { tasks = (await get('/tasks?all=1')).items.filter(task => task.state === 'delivered'); }
    catch (error) { message(error.message, true); }
    $('replay-task').replaceChildren(...tasks.map(task => new Option(`${task.title || task.id}${task.owner ? ' · ' + task.owner : ''}`, task.id)));
    const id = params.get('id') || tasks[0]?.id;
    if (id) open(id, params.get('step'));
    else if (!$('replay-msg').textContent) message('还没有交付过的任务');
  })();
})();
