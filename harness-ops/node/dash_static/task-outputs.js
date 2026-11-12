/* 成果: what a task's delivery produced, file by file and by kind (weights, engine libraries, images, videos, reports,
   the rest), and the code commits behind it: those its delivered experiment jobs ran, the GitHub commits it links and
   the commits its report names that the node's experiments mirror knows. */
window.HGTaskOutputs = (() => {
  'use strict';
  const U = () => HGResearchTask.util;
  const API = '/dash/api';
  const KINDS = [['weights', '权重'], ['engine', '引擎库'], ['commit', '代码提交'], ['image', '图片'], ['video', '视频'],
    ['report', '报告'], ['other', '其他文件'], ['result', '结果记录'], ['link', '外部链接']];
  const LABEL = Object.fromEntries(KINDS);
  const EXT = {weights: /\.(npz|pt|pth|ckpt|safetensors|onnx|tflite|dlc|h5|pb|mnn)$/i, engine: /\.(so|apk|aab|hap|dylib|dll|a)$/i,
    image: /\.(png|jpe?g|webp|gif|bmp)$/i, video: /\.(mp4|webm|mov|mkv)$/i, report: /\.(md|html?|pdf)$/i};
  const kindOf = path => Object.keys(EXT).find(kind => EXT[kind].test(path)) || (/(^|\/)(report|summary)[^/]*\.json$/i.test(path) ? 'report' : 'other');
  const STATUS = {A: '新增', M: '修改', D: '删除', R: '重命名', C: '复制', T: '类型变化'};
  // The platform's records in a job's output (lease receipts, its device record and runtime report), not the agent's work.
  const platform = name => name === 'lease-receipts' || name.startsWith('.');
  const FILES_PER_JOB = 300, DIRECTORIES_PER_JOB = 30;
  const GITHUB = /^https:\/\/github\.com\/([^/]+\/[^/]+)\/(?:commit|tree|blob)\/([0-9a-f]{7,40})(?:[/?#]|$)/;
  const HEX = /\b[0-9a-f]{7,40}\b/g;

  const size = n => !Number.isFinite(n) ? '' : n >= 1 << 20 ? `${(n / (1 << 20)).toFixed(1)} MB` : n >= 1024 ? `${Math.round(n / 1024)} KB` : `${n} 字节`;
  const filePath = file => `${API}/jobs/${encodeURIComponent(file.job)}/out?${new URLSearchParams({path: file.path})}`;
  const fileUrl = file => HGTarget.url(filePath(file));
  // A picture or a video among the outputs previews under its row.
  const media = file => window.HGMedia?.preview(filePath(file), file.path) || null;
  const repoPage = url => /^https:\/\/github\.com\/.+\.git$/.test(url || '') ? url.slice(0, -4) : null;

  // A job's output tree, breadth first, without the platform's records; bounded so a job of thousands of frames
  // stays a short read. The directories of one level are read together, so a tree costs its depth in round trips.
  async function tree(job, listing = 'artifacts', skip = () => false) {
    const files = [];
    let level = [''], reads = 0, more = false;
    while (level.length) {
      if (reads >= DIRECTORIES_PER_JOB || files.length >= FILES_PER_JOB) { more = true; break; }
      if (reads + level.length > DIRECTORIES_PER_JOB) { level = level.slice(0, DIRECTORIES_PER_JOB - reads); more = true; }
      reads += level.length;
      const listed = await pool(level, 4, async path => {
        const found = [], directories = [];
        let cursor = null;
        do {
          const page = await U().api(`/jobs/${encodeURIComponent(job)}/${listing}?${new URLSearchParams({path, limit: '200', ...(cursor ? {cursor} : {})})}`);
          for (const item of page.items) {
            if (path === '' && (listing === 'artifacts' ? platform(item.name) : skip(item.name))) continue;
            if (item.kind === 'directory') directories.push(item.path);
            else if (item.kind === 'file') found.push({job, path: item.path, size: item.size, kind: kindOf(item.path)});
          }
          cursor = page.next_cursor;
        } while (cursor && found.length < FILES_PER_JOB);
        return {found, directories, cut: !!cursor};
      });
      level = [];
      for (const value of listed) {
        if (value.error) throw value.error;
        files.push(...value.found); level.push(...value.directories);
        if (value.cut) more = true;
      }
    }
    return {files: files.slice(0, FILES_PER_JOB), more: more || files.length > FILES_PER_JOB};
  }
  // At most `limit` items worked at once; a failed item is {error}. `settled` runs after each item, however it went.
  async function pool(items, limit, work, settled = () => {}) {
    const out = new Array(items.length);
    let next = 0;
    await Promise.all(Array.from({length: Math.min(limit, items.length)}, async () => {
      while (next < items.length) { const i = next++; out[i] = await work(items[i]).catch(error => ({error})); settled(); }
    }));
    return out;
  }

  // What the node's experiments mirror knows about these commit ids (at most 50); a failed read is returned, not thrown.
  const lookup = revs => U().api(`/experiments/commits?${new URLSearchParams(revs.slice(0, 50).map(rev => ['rev', rev]))}`).catch(error => ({error}));
  // A commit the mirror does not know still shows, by id and with its page when that can be named.
  const unknownCommit = (rev, source, repository) => {
    const page = repoPage(repository);
    return {rev, commit: rev, source, url: page && rev.length === 40 ? `${page}/commit/${rev}` : null, files: null};
  };

  // Everything the delivery of `record` produced. `jobs` is the task's job list (one read the task page already makes).
  async function read(record, jobs) {
    const deliverable = record.data.deliverable;
    if (!deliverable) return null;
    const local = HGTarget.binding()?.node_id, refs = deliverable.artifacts || [];
    const files = [], results = [], links = [], wanted = new Map(), unread = [];
    const want = (rev, source) => { if (!wanted.has(rev)) wanted.set(rev, source); };
    const whole = [...new Set(refs.filter(ref => ref.kind === 'job' && !ref.path && (!ref.node_id || ref.node_id === local)).map(ref => ref.job_id))];
    for (const ref of refs) if (ref.kind === 'job' && ref.node_id && ref.node_id !== local) unread.push(`作业 ${ref.job_id}（在另一台电脑）`);
    for (const ref of refs) {
      if (ref.kind === 'job' && ref.path) files.push({job: ref.job_id, path: ref.path, kind: kindOf(ref.path)});
      else if (ref.kind === 'result' || ref.kind === 'result_source') results.push(ref);
      else if (ref.kind === 'external') {
        const github = GITHUB.exec(ref.locator || '');
        if (github && github[1] === 'HGamey/experiments') want(github[2], '交付引用');
        else links.push(ref.locator);
      }
    }
    // The commits the delivered experiment jobs ran.
    const delivered = new Set(refs.filter(ref => ref.kind === 'job').map(ref => ref.job_id));
    for (const job of jobs || []) if (delivered.has(job.id) && job.experiment?.commit) want(job.experiment.commit, `作业 ${job.id} 运行的代码`);
    // Commits the report names (ids of jobs are not commits).
    const jobIds = new Set([...(jobs || []).map(job => job.id), ...delivered]);
    for (const [hex] of String(deliverable.summary || '').matchAll(HEX)) if (!jobIds.has(hex) && wanted.size < 50) want(hex, '报告里提到');
    const [trees, found] = await Promise.all([
      pool(whole, 4, job => tree(job)), wanted.size ? lookup([...wanted.keys()]) : null]);
    trees.forEach((value, i) => { if (value.error) unread.push(whole[i]); else { files.push(...value.files); if (value.more) unread.push(whole[i] + '（只列出一部分）'); } });
    // Every commit a job ran is shown, known to the mirror or not; a hex string from the report only when it is a commit.
    // What the delivery itself names (a link, the report) comes before the code its jobs ran.
    const known = new Map((found?.items || []).map(item => [item.rev, item]));
    const rank = source => source === '交付引用' ? 0 : source === '报告里提到' ? 1 : 2;
    const commits = [...wanted].sort((a, b) => rank(a[1]) - rank(b[1])).flatMap(([rev, source]) => {
      const item = known.get(rev);
      if (item) return [{...item, source}];
      return source === '报告里提到' ? [] : [unknownCommit(rev, source, found?.repository)];
    });
    const unique = [...new Map(commits.map(commit => [commit.commit, commit])).values()];
    return {files, commits: unique, results, links, unread, lookup: found?.error ? found.error.message : ''};
  }

  function counts(outputs) {
    const n = {commit: outputs.commits.length, result: outputs.results.length, link: outputs.links.length};
    for (const file of outputs.files) n[file.kind] = (n[file.kind] || 0) + 1;
    return KINDS.filter(([kind]) => n[kind]).map(([kind, label]) => [kind, label, n[kind]]);
  }
  function fileRow(file, withJob = true) {
    const {el} = U(), row = el('li', null, 'output-file'), link = el('a', file.path.split('/').pop());
    link.href = fileUrl(file); link.download = file.path.split('/').pop(); link.title = file.path;
    row.append(link);
    if (file.path.includes('/')) row.append(el('span', file.path.slice(0, file.path.lastIndexOf('/') + 1), 'output-dir'));
    if (file.size != null) row.append(el('span', size(file.size), 'output-size'));
    if (withJob) {
      const job = el('a', `作业 ${file.job}`, 'output-job tech'); job.href = HGTarget.url(`/dash/jobs#job=${encodeURIComponent(file.job)}`);
      row.append(job);
    }
    return row;
  }
  const short = sha => sha.slice(0, 7);
  const day = at => new Date(at * 1000).toLocaleDateString('zh-CN', {month: '2-digit', day: '2-digit'});
  // A commit: its id, subject and where it comes from; opened, the files it changed. Its page shows the diff.
  function commitRow(commit, open = false) {
    const {el, tag} = U(), row = el('li', null, 'output-commit'), box = el('details'), head = el('summary');
    head.append(el('code', short(commit.commit), 'tech'), el('span', commit.subject || '本节点的实验仓库镜像里没有这个提交', 'output-subject'));
    if (commit.author) head.append(el('span', `${commit.author} · ${day(commit.at)}`, 'output-meta'));
    head.append(tag(commit.source, 'plain'));
    if (commit.files) head.append(el('span', `改了 ${commit.files_total} 个文件`, 'output-open'));
    box.append(head);
    if (commit.files) {
      const list = el('ul', null, 'output-changes');
      list.append(...commit.files.map(file => {
        const item = el('li'); item.append(el('span', STATUS[file.status] || file.status, `output-status s-${file.status}`), el('span', file.from ? `${file.from} → ${file.path}` : file.path));
        return item;
      }));
      if (commit.files_total > commit.files.length) list.append(el('li', `还有 ${commit.files_total - commit.files.length} 个文件`, 'note'));
      box.append(list);
    }
    box.open = open;
    row.append(box);
    if (commit.url) { const link = el('a', 'GitHub 上看改动', 'output-github'); link.href = commit.url; link.target = '_blank'; link.rel = 'noopener'; row.append(link); }
    return row;
  }

  // The top of a task page: how much of each kind, the weights and engine libraries by name, the commits.
  function brief(outputs, reportUrl) {
    const {el} = U(), box = el('div', null, 'summary-block summary-outputs'), head = el('div', null, 'section-head'), link = el('a', '全部成果', 'summary-link');
    link.href = reportUrl + '#outputs';
    head.append(el('h2', '成果'), link);
    const tally = el('p', null, 'output-tally');
    tally.append(...counts(outputs).map(([kind, label, n]) => el('span', `${label} ${n}`, `output-count k-${kind}`)));
    box.append(head, tally);
    const shown = outputs.files.filter(file => window.HGMedia?.kind(file.path));
    if (shown.length) {
      const strip = el('div', null, 'output-media');
      strip.append(...shown.slice(0, 6).map(media));
      if (shown.length > 6) { const all = el('a', `还有 ${shown.length - 6} 个`, 'note'); all.href = reportUrl + '#outputs'; strip.append(all); }
      box.append(strip);
    }
    const key = outputs.files.filter(file => file.kind === 'weights' || file.kind === 'engine');
    if (key.length) {
      // A name that comes from more than one job says which job each is from.
      const names = key.reduce((n, file) => n.set(file.path, (n.get(file.path) || 0) + 1), new Map());
      const list = el('ul', null, 'output-files');
      list.append(...key.slice(0, 8).map(file => { const row = fileRow(file, names.get(file.path) > 1); row.prepend(el('span', LABEL[file.kind], `output-kind k-${file.kind}`)); return row; }));
      if (key.length > 8) list.append(el('li', `还有 ${key.length - 8} 个`, 'note'));
      box.append(list);
    }
    if (outputs.commits.length) {
      const list = el('ul', null, 'output-commits');
      list.append(...outputs.commits.slice(0, 4).map(commit => commitRow(commit)));
      if (outputs.commits.length > 4) list.append(el('li', `还有 ${outputs.commits.length - 4} 个提交`, 'note'));
      box.append(list);
    }
    return box;
  }

  // The report page: every kind with every item. `preview(file)` may add something under a file's row (a picture's
  // thumbnail).
  function full(outputs, preview = media) {
    const {el} = U(), section = el('section', null, 'report-outputs');
    section.id = 'outputs';
    section.append(el('h2', '成果', 'report-files'));
    const tally = el('p', null, 'output-tally');
    tally.append(...counts(outputs).map(([kind, label, n]) => el('span', `${label} ${n}`, `output-count k-${kind}`)));
    section.append(tally);
    for (const [kind, label] of KINDS) {
      let list = null, more = null;
      if (kind === 'commit' && outputs.commits.length) { list = el('ul', null, 'output-commits'); list.append(...outputs.commits.map(commit => commitRow(commit))); }
      else if (kind === 'result' && outputs.results.length) list = U().evidenceList(outputs.results);
      else if (kind === 'link' && outputs.links.length) {
        list = el('ul', null, 'output-files');
        list.append(...outputs.links.map(url => { const item = el('li'), a = el('a', url); a.href = url; a.target = '_blank'; a.rel = 'noopener'; item.append(a); return item; }));
      } else {
        const files = outputs.files.filter(file => file.kind === kind);
        if (files.length) {
          list = el('ul', null, 'output-files');
          const rows = files.map(file => { const row = fileRow(file), shown = preview(file); if (shown) row.append(shown); return row; });
          rows.slice(20).forEach(row => { row.hidden = true; });
          list.append(...rows);
          if (rows.length > 20) {
            more = el('button', `显示全部 ${rows.length} 个`, 'link-btn output-more'); more.type = 'button';
            more.onclick = () => { rows.forEach(row => { row.hidden = false; }); more.remove(); };
          }
        }
      }
      if (list) section.append(el('h3', `${label} · ${list.querySelectorAll(':scope > li:not(.note)').length}`), list, ...(more ? [more] : []));
    }
    if (outputs.unread.length) section.append(el('p', `没读全的作业：${outputs.unread.join('、')}`, 'note'));
    if (outputs.lookup) section.append(el('p', `没查到提交改了什么：${outputs.lookup}`, 'note'));
    return section;
  }

  return {read, brief, full, kindOf, tree, pool, lookup, unknownCommit, commitRow, fileRow, size, LABEL};
})();
