/* Read host job records only. Device state and experiment validity are separate. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const API = '/dash/api';
  const LOG_CHUNK = 64 * 1024;
  const LOG_WINDOW = 256 * 1024;
  let nodeRevision = 0, listRevision = 0, detailRevision = 0, artifactRevision = 0, metadataRevision = 0, logRevision = 0;
  let jobsCursor = null, filesCursor = null, selected = '', directory = '';
  let logOffset = 0, logText = '', logDecoder = new TextDecoder();
  let activeFilters = {query: '', state: ''};

  const text = value => value == null ? '未记录' : String(value);
  function el(tag, value, className) {
    const node = document.createElement(tag);
    if (value != null) node.textContent = String(value);
    if (className) node.className = className;
    return node;
  }
  function message(id, value, error = false) {
    $(id).textContent = value;
    $(id).classList.toggle('task-error', error);
  }
  function busy(id, value, label) {
    const button = $(id);
    button.disabled = value;
    button.setAttribute('aria-busy', String(value));
    if (!button.dataset.idleLabel) button.dataset.idleLabel = button.textContent;
    button.textContent = value && label ? label : button.dataset.idleLabel;
  }
  function regionBusy(id, value) { $(id).setAttribute('aria-busy', String(value)); }
  function readTime() { return `读取于 ${new Date().toLocaleTimeString()}`; }
  function time(value) {
    if (value == null) return '未记录';
    const date = new Date(typeof value === 'number' ? value * 1000 : value);
    return Number.isNaN(date.getTime()) ? '未记录' : date.toLocaleString();
  }
  function bytes(value) {
    if (!Number.isFinite(value)) return '大小未记录';
    if (value < 1024) return `${value} B`;
    if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
    return `${(value / 1024 / 1024).toFixed(1)} MiB`;
  }
  function facts(id, rows) {
    $(id).replaceChildren(...rows.flatMap(([key, value]) => [el('dt', key), el('dd', text(value))]));
  }
  function status(job) {
    if (job.lost) return ['退出状态丢失', 'unknown'];
    if (job.state === 'running') return ['运行中', 'running'];
    if (job.exit === 0) return ['已结束 · exit 0', 'finished'];
    if (Number.isInteger(job.exit)) return [`执行失败 · exit ${job.exit}`, 'failed'];
    return ['退出码未知', 'unknown'];
  }
  function jobURL(suffix = '', params = {}) {
    const query = new URLSearchParams(params);
    return `${API}/jobs/${encodeURIComponent(selected)}${suffix}${query.size ? '?' + query : ''}`;
  }
  function errorText(error) {
    return error.name === 'AbortError' ? '请求超时，请重试。' : (error.message || '网络错误，请重试。');
  }
  async function request(url, consume) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(url, {credentials: 'same-origin', cache: 'no-store', signal: controller.signal});
      if (response.status === 401 || response.status === 403 || (response.redirected && new URL(response.url).pathname === '/dash/login')) {
        throw new Error('查看登录已失效，请通过首页重新登录。');
      }
      if (!response.ok) throw new Error(`读取失败（HTTP ${response.status}），可刷新重试。`);
      return await consume(response);
    } finally { clearTimeout(timeout); }
  }
  async function json(url) {
    return request(url, async response => {
      if (!(response.headers.get('content-type') || '').includes('application/json')) throw new Error('服务返回了非 JSON 内容，请检查登录或节点连接。');
      return response.json();
    });
  }

  async function loadNode() {
    const revision = ++nodeRevision;
    regionBusy('node-card', true);
    $('node-summary').hidden = true;
    $('node-details').hidden = true;
    $('node-updated').textContent = '';
    message('node-message', '正在读取节点信息…');
    try {
      const node = await json(`${API}/node`);
      if (revision !== nodeRevision) return;
      facts('node-info', [
        ['节点 ID', node.node_id], ['访问入口', location.origin],
        ['宿主', `${text(node.host?.hostname)} · ${text(node.host?.platform)}`],
        ['服务版本', `${text(node.service?.revision)}${node.service?.dirty === true ? '（有本地修改）' : ''}`],
        ['任务目录', node.storage?.jobs_root], ['数据目录', node.storage?.data_root],
        ['服务启动', time(node.started_at)]
      ]);
      $('node-summary').textContent = `${text(node.node_id)} · ${text(node.host?.hostname)}`;
      $('node-summary').hidden = false;
      $('node-details').hidden = false;
      message('node-message', '');
      $('node-updated').textContent = readTime();
    } catch (error) {
      if (revision === nodeRevision) message('node-message', errorText(error), true);
    } finally { if (revision === nodeRevision) regionBusy('node-card', false); }
  }
  function highlight() {
    for (const button of $('task-list').querySelectorAll('button')) {
      if (button.dataset.job === selected) button.setAttribute('aria-current', 'true');
      else button.removeAttribute('aria-current');
    }
  }
  function addJob(job) {
    const item = el('li');
    const button = el('button', null, 'task-item');
    button.type = 'button';
    button.dataset.job = job.id;
    button.append(el('div', job.label || job.bin || '未命名任务', 'task-item-title'));
    const row = el('div', null, 'task-item-line');
    const [label, kind] = status(job);
    row.append(el('span', job.id, 'task-item-id'), el('span', label, `task-state ${kind}`));
    const command = [job.bin, ...(Array.isArray(job.argv) ? job.argv : [])].map(text).join(' ');
    button.append(row, el('span', time(job.started), 'task-item-time'), el('span', command.length > 220 ? command.slice(0, 220) + '…' : command, 'task-item-command'));
    button.addEventListener('click', () => {
      if (job.id !== selected) selectJob(job.id);
      if (window.matchMedia('(max-width: 780px)').matches) $('detail-heading').focus();
    });
    item.append(button);
    $('task-list').append(item);
  }
  async function loadJobs(append = false) {
    const revision = ++listRevision;
    if (!append) {
      jobsCursor = null;
      $('task-list').replaceChildren();
      $('more-jobs').hidden = true;
      $('task-count').hidden = true;
      $('list-warnings').hidden = true;
    }
    regionBusy('task-browser', true);
    busy('search-jobs', true, '搜索中…');
    busy('more-jobs', true, '正在加载…');
    message('list-message', append ? '正在读取更多任务…' : '正在读取任务…');
    const query = new URLSearchParams({limit: '50', ...activeFilters});
    if (append && jobsCursor) query.set('cursor', jobsCursor);
    try {
      const result = await json(`${API}/jobs?${query}`);
      if (revision !== listRevision) return;
      if (!Array.isArray(result.items)) throw new Error('任务列表格式不正确。');
      result.items.forEach(addJob);
      jobsCursor = result.next_cursor || null;
      $('more-jobs').hidden = !jobsCursor;
      const count = $('task-list').children.length;
      $('task-count').textContent = `${count} 条已加载`;
      $('task-count').hidden = false;
      message('list-message', count ? `已显示 ${count} 个任务${jobsCursor ? '，还有更多' : ''}。` : (activeFilters.query || activeFilters.state ? '没有匹配的任务，可调整搜索条件。' : '这个节点还没有任务记录。'));
      const warnings = Array.isArray(result.warnings) ? result.warnings : [];
      $('list-warnings').textContent = warnings.map(w => typeof w === 'string' ? w : JSON.stringify(w)).join('；');
      $('list-warnings').hidden = !warnings.length;
      highlight();
    } catch (error) {
      if (revision === listRevision) message('list-message', errorText(error), true);
    } finally {
      if (revision === listRevision) {
        busy('search-jobs', false);
        busy('more-jobs', false);
        regionBusy('task-browser', false);
      }
    }
  }
  async function selectJob(id, keepDirectory = false) {
    if (typeof id !== 'string' || !/^[0-9a-f]{12}$/.test(id)) {
      message('detail-message', '任务 ID 格式不正确。', true);
      return;
    }
    selected = id;
    const revision = ++detailRevision;
    ++artifactRevision;
    ++metadataRevision;
    ++logRevision;
    if (!keepDirectory) directory = '';
    $('detail-content').hidden = true;
    $('detail-placeholder').hidden = false;
    $('detail-updated').textContent = '';
    $('refresh-detail').hidden = false;
    busy('refresh-detail', true, '读取中…');
    regionBusy('task-detail', true);
    message('detail-message', `正在读取 ${id}…`);
    highlight();
    history.replaceState(null, '', `${location.pathname}${location.search}#job=${encodeURIComponent(id)}`);
    try {
      const job = await json(jobURL());
      if (revision !== detailRevision) return;
      const [label, kind] = status(job);
      $('job-title').textContent = job.label || job.bin || '未命名任务';
      $('job-state').textContent = label;
      $('job-state').className = `task-state ${kind}`;
      $('detail-updated').textContent = readTime();
      facts('job-info', [['任务 ID', job.id], ['命令', job.bin], ['开始', time(job.started)], ['结束', time(job.ended)], ['记录说明', job.note || '无']]);
      $('job-command').textContent = JSON.stringify({bin: job.bin, argv: job.argv || []}, null, 2);
      $('detail-content').hidden = false;
      $('detail-placeholder').hidden = true;
      message('detail-message', '');
      resetLog();
      await Promise.all([loadArtifacts(), loadLog()]);
    } catch (error) { if (revision === detailRevision) message('detail-message', errorText(error), true); }
    finally {
      if (revision === detailRevision) {
        busy('refresh-detail', false);
        regionBusy('task-detail', false);
      }
    }
  }
  function breadcrumbs() {
    const root = el('button', '输出根目录');
    root.type = 'button';
    root.addEventListener('click', () => openDirectory(''));
    $('breadcrumbs').replaceChildren(root);
    let path = '';
    for (const part of directory.split('/').filter(Boolean)) {
      path = path ? `${path}/${part}` : part;
      const destination = path;
      const button = el('button', part);
      button.type = 'button';
      button.addEventListener('click', () => openDirectory(destination));
      $('breadcrumbs').append(el('span', '/'), button);
    }
    $('breadcrumbs').lastElementChild.setAttribute('aria-current', 'location');
  }
  function openDirectory(path) { directory = path; loadArtifacts(); }
  function addArtifact(file) {
    const item = el('li', null, 'artifact-item');
    const main = el('div', null, 'artifact-main');
    if (file.kind === 'directory') {
      const button = el('button', `${file.name}/`, 'file-name');
      button.type = 'button';
      button.addEventListener('click', () => openDirectory(file.path));
      main.append(button, el('span', '目录', 'artifact-sub'));
    } else {
      main.append(el('span', file.name));
      main.append(el('span', file.kind === 'file' ? `${bytes(file.size)} · ${time(file.modified)}` : `不可读取：${file.reason || '类型不支持'}`, 'artifact-sub'));
    }
    item.append(main);
    if (file.kind === 'file') {
      const actions = el('div', null, 'artifact-actions');
      const info = el('button', '校验信息', 'ghost');
      info.type = 'button';
      info.addEventListener('click', () => loadMetadata(file.path, info));
      const download = el('a', '下载');
      download.href = jobURL('/out', {path: file.path});
      download.download = file.name;
      actions.append(info, download);
      item.append(actions);
    }
    $('artifact-list').append(item);
  }
  async function loadArtifacts(append = false) {
    const revision = ++artifactRevision;
    ++metadataRevision;
    if (!append) { filesCursor = null; $('artifact-list').replaceChildren(); $('more-artifacts').hidden = true; }
    $('artifact-meta').hidden = true;
    regionBusy('artifacts', true);
    busy('refresh-artifacts', true, '读取中…');
    busy('more-artifacts', true, '正在加载…');
    breadcrumbs();
    message('artifact-message', '正在读取目录…');
    const params = {path: directory, limit: '100'};
    if (append && filesCursor) params.cursor = filesCursor;
    try {
      const result = await json(jobURL('/artifacts', params));
      if (revision !== artifactRevision) return;
      if (!Array.isArray(result.items)) throw new Error('文件列表格式不正确。');
      result.items.forEach(addArtifact);
      filesCursor = result.next_cursor || null;
      $('more-artifacts').hidden = !filesCursor;
      const count = $('artifact-list').children.length;
      message('artifact-message', count ? `已显示 ${count} 项。` : '这个目录还没有输出文件。');
    } catch (error) { if (revision === artifactRevision) message('artifact-message', errorText(error), true); }
    finally {
      if (revision === artifactRevision) {
        busy('refresh-artifacts', false);
        busy('more-artifacts', false);
        regionBusy('artifacts', false);
      }
    }
  }
  async function loadMetadata(path, button) {
    const revision = artifactRevision;
    const metadata = ++metadataRevision;
    $('artifact-meta').hidden = false;
    regionBusy('artifact-meta', true);
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    button.textContent = '计算中…';
    $('artifact-meta').textContent = `正在计算校验信息：${path}`;
    try {
      const meta = await json(jobURL('/out', {path, meta: '1'}));
      if (revision !== artifactRevision || metadata !== metadataRevision) return;
      $('artifact-meta').textContent = `${path}\n大小：${bytes(meta.size)}\nSHA-256：${text(meta.sha256)}\n运行中的文件可能继续变化。`;
    } catch (error) { if (revision === artifactRevision && metadata === metadataRevision) $('artifact-meta').textContent = errorText(error); }
    finally {
      button.disabled = false;
      button.setAttribute('aria-busy', 'false');
      button.textContent = '校验信息';
      if (revision === artifactRevision && metadata === metadataRevision) regionBusy('artifact-meta', false);
    }
  }
  function resetLog() {
    ++logRevision;
    logOffset = 0;
    logText = '';
    logDecoder = new TextDecoder();
    $('job-log').textContent = '';
    busy('log-next', false);
    busy('log-reset', false);
  }
  async function loadLog() {
    const revision = ++logRevision;
    const start = logOffset;
    regionBusy('task-log', true);
    busy('log-next', true, '读取中…');
    busy('log-reset', true);
    message('log-message', `正在从第 ${start} 字节读取…`);
    try {
      const data = await request(jobURL('/log', {offset: String(start), limit: String(LOG_CHUNK)}), async response => {
        if (!response.body) throw new Error('浏览器不支持分段读取日志。');
        const reader = response.body.getReader();
        const chunks = [];
        let length = 0;
        try {
          while (length < LOG_CHUNK) {
            const {done, value} = await reader.read();
            if (done) break;
            const chunk = value.subarray(0, LOG_CHUNK - length);
            chunks.push(chunk);
            length += chunk.byteLength;
          }
        } finally { await reader.cancel(); }
        const result = new Uint8Array(length);
        let at = 0;
        for (const chunk of chunks) { result.set(chunk, at); at += chunk.byteLength; }
        return result;
      });
      if (revision !== logRevision) return;
      logOffset += data.byteLength;
      logText += logDecoder.decode(data, {stream: true});
      if (logText.length > LOG_WINDOW) logText = logText.slice(-LOG_WINDOW);
      $('job-log').textContent = logText;
      message('log-message', data.byteLength ? `已读取至第 ${logOffset} 字节；每次最多 64 KiB，保留最近 262,144 个字符。` : `第 ${logOffset} 字节之后暂无新日志。运行中的任务可稍后再读取。`);
    } catch (error) { if (revision === logRevision) message('log-message', errorText(error), true); }
    finally {
      if (revision === logRevision) {
        busy('log-next', false);
        busy('log-reset', false);
        regionBusy('task-log', false);
      }
    }
  }

  $('filters').addEventListener('submit', event => {
    event.preventDefault();
    activeFilters = {query: $('query').value.trim(), state: $('state').value};
    loadJobs();
  });
  $('state').addEventListener('change', () => $('filters').requestSubmit());
  $('more-jobs').addEventListener('click', () => loadJobs(true));
  async function refreshAll() {
    busy('refresh-all', true, '刷新中…');
    try { await Promise.all([loadNode(), loadJobs(), selected ? selectJob(selected, true) : null]); }
    finally { busy('refresh-all', false); }
  }
  $('refresh-all').addEventListener('click', refreshAll);
  $('refresh-detail').addEventListener('click', () => { if (selected) selectJob(selected, true); });
  $('refresh-artifacts').addEventListener('click', () => loadArtifacts());
  $('more-artifacts').addEventListener('click', () => loadArtifacts(true));
  $('log-next').addEventListener('click', loadLog);
  $('log-reset').addEventListener('click', () => { resetLog(); loadLog(); });
  const initial = new URLSearchParams(location.hash.slice(1)).get('job');
  if (initial && /^[0-9a-f]{12}$/.test(initial)) selected = initial;
  else if (initial) message('detail-message', '任务 ID 格式不正确。', true);
  refreshAll();
})();
