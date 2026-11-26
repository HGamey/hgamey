/* 数据导入: two ways to bring data into this center, differing in whether it must be stopped.
   A) 结果 / 数据集 — register a result manifest or import a small report (adapter) straight into the running
      center; no stop needed, exactly what `pf-node results register/import` does (POST /results, /result-imports).
   B) 整条任务（导出包） — upload a 数据导出 zip and preview the import plan (dry-run, nothing is written). Applying a
      whole task backs up and replaces the center's databases, so it needs the center stopped: the page only shows
      the plan and the exact command to run by hand.
   Everything here needs the steer password, exactly as 数据导出 does. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id), API = '/dash/api', DASH = '—';
  const ID = /^[A-Za-z0-9._-]{1,64}$/, IMPORT_ID = /^[A-Za-z0-9._-]{1,48}$/;
  const MAX_RECORD = 256 << 10, MAX_SOURCE = 1 << 20, MAX_ZIP = 512 << 20;
  const STATE = {active: '推进中', idle: '暂空闲', paused: '已暂停', closed: '已关闭', delivered: '已交付'};
  const NAMES = {'queue/jobs': '作业队列记录', 'queue/job_events': '作业事件', 'queue/job_targets': '作业路由记录',
    'results/results': '结果', 'results/imports': '结果导入记录', 'research/objects': '研究对象',
    'research/objects (新版本)': '研究对象（追加新版本）', 'research/revisions': '研究对象版本',
    'research/events': '研究事件', 'research/operations': '研究操作', 'research/dependencies': '方向依赖',
    'research/revisions (结果序号)': '研究版本（改写冻结的结果序号）'};
  let target = null;

  const el = (tag, value, className) => { const n = document.createElement(tag); if (value != null) n.textContent = String(value); if (className) n.className = className; return n; };
  const validID = (value, re = ID) => typeof value === 'string' && re.test(value) && value !== '.' && value !== '..';
  const utf8 = text => new TextEncoder().encode(text).length;
  function size(n) {
    if (n == null) return DASH;
    const units = ['B', 'KB', 'MB', 'GB', 'TB'], at = Math.min(units.length - 1, Math.floor(Math.log(Math.max(n, 1)) / Math.log(1024)));
    return `${at ? (n / 1024 ** at).toFixed(n / 1024 ** at < 100 ? 1 : 0) : n} ${units[at]}`;
  }
  const time = at => at ? new Date(at * 1000).toLocaleString('zh-CN', {hour12: false, year: 'numeric', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit'}) : DASH;
  function message(id, value, error = false) { const n = $(id); n.textContent = value; n.classList.toggle('task-error', error); }

  async function call(path, init = {}) {
    const blob = init.body instanceof Blob;
    const headers = blob ? {'X-HG': '1', 'Content-Type': 'application/zip'}
      : init.body != null ? {'Content-Type': 'application/json', 'X-HG': '1'}
      : init.method ? {'X-HG': '1'} : {};
    const response = await HGTarget.fetch(API + path, {credentials: 'same-origin', cache: 'no-store', ...init, headers});
    const body = await response.json().catch(() => null);
    if (response.ok) return body;
    const error = new Error(body?.error || `HTTP ${response.status}`);
    error.status = response.status; error.steer = !!body?.need_steer; error.details = Array.isArray(body?.details) ? body.details : [];
    throw error;
  }

  function resultLink(container, id) {
    const a = el('a', `查看结果 ${id}`);
    a.href = HGTarget.url(`/dash/results#result=${encodeURIComponent(id)}`);
    container.append(a);
  }

  // ── A) 结果 / 数据集 ──
  function defaultManifest(id) {
    return {schema: 'phonefarm/result@1', result_id: id, title: '', job_id: null,
      labels: {game_id: null, game_version: null, device: null, research_id: null, direction_id: null},
      origin: {producer: null, producer_version: null, evidence_origin: 'unknown', observed_at: null},
      outcome: 'unknown', source_outcome: null, sources: [], metrics: [], claims: [], limitations: [], supersedes: null};
  }
  function showMode(mode) {
    $('register-form').hidden = mode !== 'register';
    $('import-form').hidden = mode !== 'import';
    $('open-register').setAttribute('aria-pressed', String(mode === 'register'));
    $('open-import').setAttribute('aria-pressed', String(mode === 'import'));
    if (mode === 'register' && !$('manifest-input').value.trim()) $('manifest-input').value = JSON.stringify(defaultManifest(crypto.randomUUID()), null, 2);
    if (mode === 'import' && !$('import-id').value.trim()) $('import-id').value = crypto.randomUUID();
  }
  function parseManifest() {
    const value = JSON.parse($('manifest-input').value);
    if (!value || Array.isArray(value) || value.schema !== 'phonefarm/result@1' || !validID(value.result_id)
        || typeof value.title !== 'string' || !value.title.trim() || value.title.length > 240)
      throw new Error('请填写 phonefarm/result@1、有效 result_id 和非空标题（最多 240 字符）。');
    if (utf8(JSON.stringify(value)) > MAX_RECORD) throw new Error('结果登记超过 256 KiB；大原件请保留为命名来源。');
    return value;
  }
  async function importBody() {
    const id = $('import-id').value.trim(), file = $('import-file').files[0];
    if (!validID(id, IMPORT_ID)) throw new Error('导入 ID 须为 1–48 位字母、数字、点、下划线或短横线。');
    if (!file) throw new Error('请选择原 JSON 文件。');
    if (file.size > MAX_SOURCE) throw new Error('文件超过 1 MiB，请选择小 JSON 报告。');
    const labels = $('import-labels').value.trim() ? JSON.parse($('import-labels').value) : {};
    if (!labels || typeof labels !== 'object' || Array.isArray(labels)) throw new Error('标签必须是 JSON 对象。');
    const content = await file.text();
    if (utf8(content) > MAX_SOURCE) throw new Error('解码后的 UTF-8 文本超过 1 MiB。');
    return {schema: 'phonefarm/result-import@1', import_id: id, adapter: $('import-adapter').value, filename: file.name, content, labels};
  }
  async function doRegister() {
    let value;
    try { value = parseManifest(); } catch (error) { message('register-message', `没有登记：${error.message}`, true); return; }
    $('register-submit').disabled = true; message('register-message', '正在保存…'); $('register-links').replaceChildren();
    try {
      const receipt = await call('/results', {method: 'POST', body: JSON.stringify(value)});
      message('register-message', '已登记（不可变）。登记不表示实验或设备恢复已通过。');
      resultLink($('register-links'), receipt.result_id);
      const datasets = el('a', '打开数据集'); datasets.href = HGTarget.url('/dash/datasets'); $('register-links').append(datasets);
      $('manifest-input').value = JSON.stringify(defaultManifest(crypto.randomUUID()), null, 2);
    } catch (error) {
      message('register-message', error.steer ? '操作口令已失效，刷新后重新解锁' : `没有登记：${error.message}`, true);
    } finally { $('register-submit').disabled = false; }
  }
  async function doImport() {
    let body;
    try { body = await importBody(); } catch (error) { message('import-message', `没有导入：${error.message}`, true); return; }
    $('import-submit').disabled = true; message('import-message', '正在导入…'); $('import-links').replaceChildren();
    try {
      const receipt = await call('/result-imports', {method: 'POST', body: JSON.stringify(body)});
      message('import-message', `已导入，登记了 ${receipt.result_ids.length} 条结果；原报告字节已保留。`);
      for (const id of receipt.result_ids) resultLink($('import-links'), id);
      const datasets = el('a', '打开数据集'); datasets.href = HGTarget.url('/dash/datasets'); $('import-links').append(datasets);
      $('import-id').value = crypto.randomUUID(); $('import-file').value = '';
    } catch (error) {
      message('import-message', error.steer ? '操作口令已失效，刷新后重新解锁' : `没有导入：${error.message}`, true);
    } finally { $('import-submit').disabled = false; }
  }

  // ── B) 整条任务（导出包） ──
  function counts(bucket) {
    const row = el('div', null, 'im-counts');
    const entries = Object.entries(bucket || {});
    if (!entries.length) { row.append(el('span', '没有', 'im-count')); return row; }
    for (const [key, n] of entries) {
      const tag = el('span', null, 'im-count');
      tag.append(document.createTextNode((NAMES[key] || key) + ' '), el('b', n, 'num'));
      row.append(tag);
    }
    return row;
  }
  function listSection(title, items, render) {
    if (!items || !items.length) return null;
    const section = el('section');
    section.append(el('h2', `${title}（${items.length}）`));
    const ul = el('ul');
    for (const item of items) ul.append(el('li', render ? render(item) : item));
    section.append(ul);
    return section;
  }
  function renderPlan(report) {
    const plan = $('plan'); plan.replaceChildren();
    const applyCmd = `python3 harness-ops/node/center_import.py <你下载的导出 zip 路径> --data-root ${report.target.data_root} --apply`;

    const head = el('section');
    head.append(el('h2', '这份导出包'));
    const facts = el('dl', null, 'im-facts');
    const add = (label, value, mono) => { facts.append(el('dt', label)); const dd = el('dd', value, mono ? 'mono' : null); facts.append(dd); };
    add('来源中心', `${report.source?.hostname || DASH}（${report.source?.node_id || DASH}）`);
    add('导出于', time(report.source?.exported_at));
    add('目标中心（本机）', report.target.data_root, true);
    add('作业目录', report.target.jobs_dir, true);
    head.append(facts);
    if (report.node_id_rewritten) {
      const r = report.node_id_rewritten;
      head.append(el('p', `node_id 对照：来源 ${r.from} → 这里 ${r.to}（新写入的行里值正好等于来源 node_id 的字符串会被替换：研究库 ${r.replaced.research} 处、结果库 ${r.replaced.results} 处、作业库 ${r.replaced.queue} 处）。`, 'note'));
    }
    plan.append(head);

    const tasks = listSection('任务', report.tasks, t => `${t.id}  ${t.title || ''} · 作业 ${t.jobs} · 结果 ${t.results} · 附件 ${t.attachments}${t.state ? ' · ' + (STATE[t.state] || t.state) : ''}${t.follows ? ' · 接续 ' + t.follows : ''}`);
    if (tasks) plan.append(tasks);
    if (report.unassigned) {
      const u = report.unassigned, s = el('section');
      s.append(el('h2', '未归属任务的数据'), el('p', `作业 ${u.jobs} · 结果 ${u.results} · 附件 ${u.attachments}`, 'note'));
      plan.append(s);
    }

    const diff = el('section');
    diff.append(el('h2', '计划（只校验，未写入）'));
    diff.append(el('p', '新增：', 'note'), counts(report.new));
    diff.append(el('p', '已存在、跳过：', 'note'), counts(report.same));
    plan.append(diff);

    for (const [title, items, render] of [
      ['冲突（有冲突 apply 也会被整体拒绝）', report.conflicts],
      ['悬空的引用（照常导入，指向的东西不在这里）', report.dangling],
      ['导出时还没结束的作业', report.notes],
      ['未带原件的作业', report.missing_originals, m => `${m.job_id}：${m.reason}`],
      ['不会重新绑定的结果源（导入后不可用）', report.unverified_sources, u => `${u.result_id} / ${u.source}：${u.reason}`]]) {
      const section = listSection(title, items, render);
      if (section) plan.append(section);
    }
    if (report.rebound_sources || report.excluded?.length) {
      const extra = el('section');
      extra.append(el('h2', '其他'));
      if (report.rebound_sources) extra.append(el('p', `重新绑定的结果源：${report.rebound_sources} 个`, 'note'));
      if (report.excluded?.length) extra.append(el('p', `这次导入不包含：${report.excluded.join('、')}`, 'note'));
      plan.append(extra);
    }
    if (report.pause_agents) plan.append((() => { const s = el('section'); s.append(el('p', report.pause_agents, 'note')); return s; })());

    const stop = el('section', null, 'im-stop');
    stop.append(el('h2', '真正导入（apply）：先停中心，再手动执行'));
    if (report.running_services?.length) stop.append(el('p', `注意：中心服务正在这个数据根上运行（PID ${report.running_services.join(', ')}）。apply 前必须先停掉它，否则脚本会拒绝写入。`, 'note task-error'));
    stop.append(el('p', 'apply 会备份并替换这台中心的三个数据库，因此只能在中心停机后由人手动运行。网页不代为执行：', 'note'));
    stop.append(el('pre', applyCmd, 'im-cmd'));
    const copy = el('button', '复制命令', 'btn sm im-copy'); copy.type = 'button';
    copy.addEventListener('click', async () => { try { await navigator.clipboard.writeText(applyCmd); copy.textContent = '已复制'; setTimeout(() => { copy.textContent = '复制命令'; }, 2000); } catch (_) { copy.textContent = '复制失败'; } });
    stop.append(copy);
    plan.append(stop);
  }
  async function doPreview() {
    $('preview-details').replaceChildren(); $('plan').replaceChildren();
    const file = $('zip-file').files[0];
    if (!file) { message('preview-message', '请选择一个数据导出 zip。', true); return; }
    if (file.size > MAX_ZIP) { message('preview-message', `文件 ${size(file.size)} 超过 512 MiB，请用命令行预演与导入。`, true); return; }
    $('preview-submit').disabled = true; message('preview-message', '正在预演（dry-run，不写入任何数据）…');
    try {
      const report = await call('/center-import', {method: 'POST', body: file});
      message('preview-message', '预演完成，未写入任何数据。');
      renderPlan(report);
    } catch (error) {
      message('preview-message', error.steer ? '操作口令已失效，刷新后重新解锁' : `预演失败：${error.message}`, true);
      $('preview-details').replaceChildren(...(error.details || []).map(text => el('li', text)));
    } finally { $('preview-submit').disabled = false; }
  }
  function fillTarget() {
    const info = $('target-info'); info.replaceChildren();
    if (!target) {
      info.append(el('dt', '状态'), el('dd', '此节点不是中心，整条任务导入不可用。'));
      $('zip-file').disabled = $('preview-submit').disabled = true;
      return;
    }
    const add = (label, value, mono) => { info.append(el('dt', label)); info.append(el('dd', value, mono ? 'mono' : null)); };
    add('本中心数据根', target.data_root, true);
    add('作业目录', target.jobs_dir, true);
    add('中心身份', target.node_id, true);
  }

  // ── tabs, unlock, wiring ──
  function selectTab(which) {
    for (const [id, panel] of [['tab-results', 'panel-results'], ['tab-package', 'panel-package']]) {
      const active = id === which;
      $(id).classList.toggle('is-active', active); $(id).setAttribute('aria-selected', String(active));
      $(panel).hidden = !active;
    }
  }
  async function load() {
    $('message').hidden = false; $('message').textContent = '正在读取…'; $('content').hidden = true;
    try {
      const access = await call('/access');
      if (!access.steer) {
        $('message').textContent = access.steer_configured ? '数据导入会动用中心数据，先用操作口令解锁。' : '本节点未配置操作口令，不能导入。';
        $('unlock').hidden = !access.steer_configured;
        return;
      }
      $('unlock').hidden = true;
      try { target = await call('/center-import'); } catch (_) { target = null; }
      fillTarget();
      showMode('register');
      $('message').hidden = true; $('content').hidden = false;
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
  $('tab-results').addEventListener('click', () => selectTab('tab-results'));
  $('tab-package').addEventListener('click', () => selectTab('tab-package'));
  $('open-register').addEventListener('click', () => showMode('register'));
  $('open-import').addEventListener('click', () => showMode('import'));
  $('register-id').addEventListener('click', () => { $('manifest-input').value = JSON.stringify(defaultManifest(crypto.randomUUID()), null, 2); });
  $('import-generate').addEventListener('click', () => { $('import-id').value = crypto.randomUUID(); });
  $('register-form').addEventListener('submit', event => { event.preventDefault(); doRegister(); });
  $('import-form').addEventListener('submit', event => { event.preventDefault(); doImport(); });
  $('preview-form').addEventListener('submit', event => { event.preventDefault(); doPreview(); });
  $('refresh').addEventListener('click', load);
  load();
})();
