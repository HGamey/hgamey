/* Bounded host upload UI. Browser state is a locator, never evidence of received bytes. */
(() => {
  'use strict';
  const API = '/dash/api/uploads', STORE = 'hgamey.game-upload.identities';
  const ID = /^[a-z0-9][a-z0-9_-]{0,63}$/, CHUNK = 1024 * 1024;
  const TERMINAL = new Set(['verified', 'failed', 'cancelled', 'superseded']);
  const PHASE = {unclaimed:'尚未取得缓存写入保留',receiving:'接收中',received:'字节已收齐，待校验',sealed:'已封存，待校验任务',verifying:'后台校验中',verified:'上传校验已完成',failed:'上传或校验失败',cancelled:'上传保留已取消',superseded:'已由其他尝试接替',busy:'缓存忙，当前偏移未知',unknown:'上传状态未知'};
  const el = (tag, text, cls) => { const n = document.createElement(tag); if (text != null) n.textContent = text; if (cls) n.className = cls; return n; };
  const bytes = n => Number.isSafeInteger(n) && n >= 0 ? n < 1048576 ? `${(n / 1024).toFixed(1)} KiB` : `${(n / 1048576).toFixed(1)} MiB` : '未知';
  const keyOf = b => JSON.stringify([b.node_id,b.game_id,b.version,b.asset_name]);
  const url = id => `${API}/${encodeURIComponent(id)}`;
  const safeLink = path => { if (!path) return ''; try { const u = new URL(path, location.origin); return u.origin === location.origin && !u.username && !u.password ? u.pathname + u.search + u.hash : ''; } catch (_) { return ''; } };
  window.createGameUploads = ({context, refresh, auth}) => {
    const saved = new Map(), views = new Map(); let active = null;
    try { for (const b of JSON.parse(sessionStorage.getItem(STORE) || '[]')) if (b && ['node_id','game_id','version','asset_name','upload_id'].every(k => typeof b[k] === 'string') && ID.test(b.upload_id)) saved.set(keyOf(b), b); } catch (_) { /* No local record establishes remote state. */ }
    function persist(v) {
      if (v.id) saved.set(v.key, {...v.binding, upload_id:v.id});
      try { sessionStorage.setItem(STORE, JSON.stringify([...saved.values()])); } catch (_) { v.message = '浏览器无法保存上传 ID，请复制可见 ID 后再离开页面。'; }
    }
    function supported() { const c = context(); return !!c.node?.capabilities?.includes('games.upload-v1'); }
    function canWrite(v) { const c = context(); return supported() && c.canWrite && c.detailFresh && !c.disposed && c.node.node_id === v.binding.node_id && c.selected === `${v.binding.game_id}/${v.binding.version}`; }
    function validFile(v) { return !!v.file && v.file.size === v.asset.size; }
    function checked(v, value, id = v.id) {
      if (!value || value.schema_version !== 1 || value.upload_id !== id || Object.entries(v.binding).some(([k,x]) => value[k] !== x) || value.size !== v.asset.size || value.sha256 !== v.asset.sha256) throw new Error('上传回包的节点、版本、资源或校验声明不匹配，已停止。');
      if (value.offset !== null && (!Number.isSafeInteger(value.offset) || value.offset < 0 || value.offset > value.size)) throw new Error('服务器偏移无效，已停止。');
      return value;
    }
    function remember(v, value) { v.record = checked(v,value); v.fresh = true; v.readAt = new Date().toLocaleString(); update(); return v.record; }
    async function http(op, path, method = 'GET', body, offset) {
      if (op.stopped) throw new Error('传输已暂停。');
      const controller = new AbortController(); op.controller = controller;
      const timer = setTimeout(() => controller.abort(), 40000);
      try {
        const options = {method,credentials:'same-origin',cache:'no-store',redirect:'error',signal:controller.signal};
        if (method !== 'GET') {
          options.headers = {'X-HG':'1','Content-Type':method === 'PUT' ? 'application/octet-stream' : 'application/json'};
          if (method === 'PUT') options.headers['Upload-Offset'] = String(offset);
          else options.headers['X-Request-ID'] = op.view.id;
          options.body = method === 'PUT' ? body : JSON.stringify(body);
        }
        const response = await fetch(path, options);
        if (response.status === 401 || response.status === 403) { const error = new Error('上传权限已失效，请重新核对登录与掌舵权限。'); error.status = response.status; throw error; }
        if (!(response.headers.get('content-type') || '').includes('application/json')) throw new Error(`节点未返回可核对的 JSON（HTTP ${response.status}）。`);
        const value = await response.json();
        if (!response.ok) { const error = new Error(typeof value.error === 'string' ? value.error : value.error?.message || `HTTP ${response.status}`); error.status = response.status; error.code = value.code || value.error?.code; throw error; }
        return value;
      } finally { clearTimeout(timer); if (op.controller === controller) op.controller = null; }
    }
    function halt(reason = '已暂停传输；服务器可能已收到当前块。继续前将查询原 ID，不取消校验任务。') {
      if (!active) return;
      active.stopped = true; active.controller?.abort(); active.view.fresh = false; active.view.message = reason; active.view.error = false; update();
    }
    async function run(v, kind, fn) {
      if (active || context().disposed || !supported()) return;
      const op = {view:v,kind,stopped:false,controller:null}; active = op; v.error = false; v.message = kind === 'query' ? '正在查询原上传…' : '操作进行中；不会自动重试写请求。'; update();
      try { await fn(op); }
      catch (error) {
        v.fresh = false;
        if (!op.stopped) {
          v.error = true; v.message = `${error.message} 保留原上传 ID；先查询状态，不自动重发或新建。${error.status === 409 ? '缓存写入或校验可能正在占用；若已有校验任务，请打开原任务核对。' : ''}`;
          if (error.status === 401 || error.status === 403) await auth();
        }
      } finally { if (active === op) active = null; update(); }
    }
    function assertRunning(op) { if (op.stopped || !canWrite(op.view)) throw new Error('当前页面或权限已改变，停止发送后续块。'); }
    async function query(v) {
      const id = v.refs.id.value.trim();
      if (!ID.test(id)) { v.error = true; v.message = '请输入有效上传 ID：1–64 位小写字母、数字、横线或下划线。'; update(); return; }
      await run(v, 'query', async op => {
        const value = checked(v, await http(op, url(id)), id);
        if (v.id && v.id !== id) v.previous.add(v.id);
        v.id = id; persist(v); remember(v,value); v.message = '已读取原上传。重新选择同一文件后可按服务器偏移接续；不会自动发送。';
      });
    }
    async function finalize(op) {
      assertRunning(op); const v = op.view;
      if (v.record.offset === null || v.record.offset !== v.asset.size || !['received','sealed'].includes(v.record.phase)) throw new Error('尚未确认字节收齐或可封存状态，不能提交校验。');
      remember(v,await http(op,url(v.id)+'/finalize','POST',{}));
      v.message = v.record.job ? `校验任务 ${v.record.job.id} 已关联；接受任务或退出码不代表当前缓存已校验。` : '已收到封存回执；请查询原上传核对校验任务。';
      if (!op.stopped) await refresh(v.binding);
    }
    async function start(v) {
      if (!canWrite(v) || !validFile(v) || active || (v.fresh && TERMINAL.has(v.record?.phase))) return;
      await run(v, 'transfer', async op => {
        if (!v.id) { v.id = crypto.randomUUID(); v.refs.id.value = v.id; persist(v); update(); }
        let value;
        try { value = checked(v,await http(op,url(v.id))); }
        catch (error) {
          if (error.status !== 404 || error.code !== 'upload_not_found') throw error;
          assertRunning(op); value = checked(v,await http(op,API,'POST',{upload_id:v.id,game_id:v.binding.game_id,version:v.binding.version,asset_name:v.binding.asset_name}));
        }
        remember(v,value); assertRunning(op);
        if (v.record.phase === 'unclaimed') remember(v,await http(op,API,'POST',{upload_id:v.id,game_id:v.binding.game_id,version:v.binding.version,asset_name:v.binding.asset_name}));
        while (v.record.phase === 'receiving' && v.record.offset !== null && v.record.offset < v.asset.size) {
          assertRunning(op);
          const before = v.record.offset, maximum = v.record.chunk_max_bytes;
          if (!Number.isSafeInteger(maximum) || maximum < 1) throw new Error('节点未提供有效块大小。');
          const end = Math.min(v.asset.size, before + Math.min(CHUNK,maximum,8388608));
          const blob = v.file.slice(before,end);
          remember(v,await http(op,url(v.id),'PUT',blob,before));
          if (v.record.offset !== end) throw new Error('写回偏移未确认整块；停止并查询原 ID。');
        }
        assertRunning(op);
        if (['received','sealed'].includes(v.record.phase) && v.record.offset === v.asset.size) await finalize(op);
        else if (v.record.phase === 'verified') { v.message = '服务器已复用当前校验缓存；请核对资源观测。'; await refresh(v.binding); }
        else v.message = v.record.offset === null ? '当前持久偏移未知，已停止；稍后查询，不从 0 开始覆盖。' : '当前上传不可继续写入。请查看会话状态和原校验任务。';
      });
    }
    async function finish(v) {
      if (!canWrite(v)) return;
      await run(v,'finalize',async op => { remember(v,await http(op,url(v.id))); await finalize(op); });
    }
    async function cancel(v) {
      if (!canWrite(v) || !v.id) return;
      await run(v,'cancel',async op => {
        remember(v,await http(op,url(v.id))); assertRunning(op);
        remember(v,await http(op,url(v.id)+'/cancel','POST',{}));
        v.message = '已收到取消上传保留的回执，partial 保留。校验任务是否取消请查看原任务；本操作不等于取消 job。';
      });
    }
    async function newAttempt(v) {
      if (!canWrite(v) || !v.id) return;
      await run(v,'new',async op => {
        remember(v,await http(op,url(v.id))); assertRunning(op);
        if (!TERMINAL.has(v.record.phase)) throw new Error('旧上传尚未确认终态，不能新建尝试；先暂停并查询或显式取消上传保留。');
        v.previous.add(v.id); v.id = crypto.randomUUID(); v.record = null; v.fresh = false; v.file = null; v.refs.file.value = ''; v.refs.id.value = v.id; persist(v);
        v.message = '已生成新尝试 ID，尚未发送文件。旧上传与校验报告保留；请重新选择正确文件。';
      });
    }
    function updateView(v) {
      const r = v.refs, record = v.record, mine = active?.view === v, busy = !!active, terminal = TERMINAL.has(record?.phase), boundID = r.id.value.trim() === (v.id || '');
      r.block.setAttribute('aria-busy',String(mine));
      r.hint.textContent = !supported() ? '此节点未声明 games.upload-v1，不能上传。' : !canWrite(v) ? '请成功读取当前版本并解锁操作；上传仅写此节点宿主缓存。' : '每次最多一个上传，默认 1 MiB 块。重选同大小文件不证明内容相同，最终由节点校验 SHA-256。';
      r.file.disabled = busy || !canWrite(v); r.id.disabled = busy;
      r.fileInfo.textContent = v.file ? `${v.file.name} · ${bytes(v.file.size)}${validFile(v) ? ' · 大小匹配清单' : ` · 与清单 ${bytes(v.asset.size)} 不符，不能上传`}` : '未选择文件；刷新后需重新选择原文件，浏览器不保存文件或路径。';
      r.begin.disabled = busy || !canWrite(v) || !validFile(v) || !boundID || (v.fresh && (terminal || ['sealed','verifying'].includes(record?.phase))) || !globalThis.crypto?.randomUUID;
      r.begin.textContent = mine && active.kind === 'transfer' ? '正在上传…' : v.id ? '按原 ID 上传并校验' : '开始上传并校验';
      r.pause.hidden = !(mine && active.kind === 'transfer'); r.pause.disabled = !!active?.stopped;
      r.query.disabled = busy || !supported();
      r.finish.hidden = !v.id; r.finish.disabled = busy || !canWrite(v) || !boundID || !v.fresh || !['received','sealed'].includes(record?.phase) || record.offset !== v.asset.size;
      r.cancel.hidden = !v.id; r.cancel.disabled = busy || !canWrite(v) || !boundID || (v.fresh && terminal);
      r.newAttempt.hidden = !record || !terminal; r.newAttempt.disabled = busy || !canWrite(v) || !boundID || !v.fresh;
      r.status.textContent = record ? `${v.fresh ? '' : '上次记录 · 当前待核对：'}${PHASE[record.phase] || '未知'} · 已确认 ${bytes(record.offset)} / ${bytes(record.size)} · 读取于 ${v.readAt}` : v.id ? `上传 ID：${v.id} · 尚未取得状态` : '尚未建立上传；选择文件不会开始传输。';
      r.progress.hidden = !record || record.offset === null; if (!r.progress.hidden) { r.progress.max = Math.max(1,record.size); r.progress.value = record.offset; }
      r.cache.textContent = record ? `当前缓存（会话读取时）：${record.observation?.state === 'verified' ? '已校验' : record.observation?.state || '未知'}。上传阶段和历史 job 不代替当前缓存事实。${record.observation?.error ? ` ${record.observation.error}` : ''}` : '';
      r.serverError.textContent = record?.error ? typeof record.error === 'string' ? record.error : record.error.message || JSON.stringify(record.error) : '';
      r.message.textContent = v.message || ''; r.message.classList.toggle('task-error',!!v.error);
      r.links.replaceChildren();
      if (record?.job?.id) {
        const task = el('a',`校验任务 ${record.job.id}`); task.href = safeLink(record.job.job_url) || `/dash/jobs#job=${encodeURIComponent(record.job.id)}`; r.links.append(task);
        const reportURL = safeLink(record.job.report_url); if (reportURL) { const report = el('a','校验报告原件（生成后可读）'); report.href = reportURL; r.links.append(report); }
      }
      for (const id of v.previous) { const old = el('a',`旧上传 ${id}`); old.href = url(id); r.links.append(old); }
    }
    function update() { for (const v of views.values()) updateView(v); }
    function mount(record, asset) {
      const binding = {node_id:record.node_id,game_id:record.game_id,version:record.version,asset_name:asset.name}, key = keyOf(binding);
      if (views.has(key)) { updateView(views.get(key)); return views.get(key).refs.block; }
      const block = el('details',null,'asset-upload'), summary = el('summary','从本机上传 / 续传'), hint = el('p',null,'note');
      const file = el('input'); file.type = 'file'; file.setAttribute('aria-label',`为 ${asset.name} 选择文件`);
      const fileInfo = el('p',null,'asset-stage'), idLabel = el('label','上传 ID（可粘贴找回）','upload-id-label'), id = el('input'); id.type = 'text'; id.maxLength = 64; id.autocomplete = 'off'; id.spellcheck = false; id.setAttribute('aria-label',`${asset.name} 上传 ID`); idLabel.append(id);
      const actions = el('div',null,'submission-actions'), buttons = {};
      for (const [name,label] of Object.entries({begin:'开始上传并校验',pause:'暂停传输',query:'查询 / 找回',finish:'提交 / 找回校验任务',cancel:'取消上传保留',newAttempt:'新建上传尝试'})) { buttons[name] = el('button',label,'ghost'+(name==='begin'?' primary':'')); buttons[name].type='button'; actions.append(buttons[name]); }
      const status=el('p',null,'asset-stage'),progress=el('progress',null,'asset-progress'),cache=el('p',null,'note'),serverError=el('p',null,'asset-error'),message=el('p',null,'note'),links=el('div',null,'preparation-links'); message.setAttribute('role','status'); progress.setAttribute('aria-label',`${asset.name} 服务器确认上传字节`);
      block.append(summary,hint,file,fileInfo,idLabel,actions,status,progress,cache,serverError,message,links,el('p','暂停仅停止发送；取消上传保留会留下已收 partial，不等于取消校验任务。已有校验任务请从原任务入口处理。','note'));
      const v = {key,binding,asset,id:saved.get(key)?.upload_id || null,record:null,fresh:false,file:null,previous:new Set(),message:'',error:false,refs:{block,hint,file,fileInfo,id,status,progress,cache,serverError,message,links,...buttons}}; id.value = v.id || ''; views.set(key,v);
      file.addEventListener('change',()=>{v.file=file.files?.[0] || null; v.error=false; v.message=''; update();});
      id.addEventListener('input',()=>{v.message='粘贴其他上传 ID 后，请先点击“查询 / 找回”核对资源绑定。'; update();});
      buttons.begin.addEventListener('click',()=>start(v)); buttons.pause.addEventListener('click',()=>halt()); buttons.query.addEventListener('click',()=>query(v)); buttons.finish.addEventListener('click',()=>finish(v)); buttons.cancel.addEventListener('click',()=>cancel(v)); buttons.newAttempt.addEventListener('click',()=>newAttempt(v));
      updateView(v); return block;
    }
    return {mount,update,pause:halt};
  };
})();
