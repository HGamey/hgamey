// 远程控制：只显式发出设备动作；未决动作保留原 ID，通过只读查询接续。
(() => {
  "use strict";
  const API = "/dash/api/remote", STORAGE = "hg.remote.control.v2";
  const $ = (id) => document.getElementById(id);
  const PHASE = { ready: "可操作", starting: "正在建立会话", closing: "正在交回，等待当前动作收尾", closed: "会话已关闭", recovery_required: "需要恢复核查", unknown: "会话状态未知" };
  const STATUS = { accepted: "已接受，尚未完成", started: "已开始，尚未完成", completed: "已完成", failed: "失败", unknown: "结果未知", not_executed: "未执行" };
  const ACTION = { acquire: "接管", release: "交回", renew: "续期", screenshot: "截图", logcat: "读取日志", tap: "点击", swipe: "滑动", text: "输入文本", key: "按键", app_start: "启动应用", app_stop: "强停应用" };
  const FRAME_ERROR = { input_contract_unavailable: "此会话未提供截图绑定输入", frame_unavailable: "来源截图已不可输入，请显式采集新图", frame_stale: "来源截图已过期，请显式采集新图", frame_orientation_unknown: "截图方向未确认，不能输入", coordinate_out_of_bounds: "坐标超出原图范围", frame_geometry_changed: "设备尺寸或方向已变化，请显式采集新图" };
  const TERMINAL = new Set(["completed", "failed", "not_executed"]);
  const state = { op: undefined, devices: [], capabilities: null, devicesFresh: false, devicesError: "尚未读取状态",
    serial: null, binding: null, session: null, sessionFresh: false, sessionError: "", live: null, frame: null, url: null,
    requests: [], selectedRequest: null, posting: false, querying: false, authBusy: false, refreshing: false,
    revision: 0, imageRevision: 0, refreshCtl: null, timer: 0,
    acquireScope: "global", acquireBinding: null, bindingBusy: false, bindingError: "", bindingRevision: 0 };

  function message(value) { return typeof value === "string" ? value : value?.message || value?.error || "请求失败"; }
  function msg(text, err) { $("msg").textContent = text || ""; $("msg").classList.toggle("err", !!err); }
  const dev = (serial, action) => "/devices/" + encodeURIComponent(serial) + "/" + action;
  const query = (values) => "?" + new URLSearchParams(values).toString();
  function save() {
    try { sessionStorage.setItem(STORAGE, JSON.stringify({ serial: state.serial, binding: state.binding,
      requests: state.requests.map(({ id, serial, session_id, action, status, at, target, input, binding_recorded, device_binding }) => ({ id, serial, session_id, action, status, at, target, ...(input ? {input} : {}), ...(binding_recorded ? {binding_recorded:true} : {}), ...(device_binding!==undefined ? {device_binding} : {}) })), selectedRequest: state.selectedRequest })); }
    catch (_) { /* 页面仍保留原 ID，回执区可手工记录。 */ }
  }
  try {
    const saved = JSON.parse(sessionStorage.getItem(STORAGE) || "null");
    if (saved && typeof saved.serial === "string") state.serial = saved.serial;
    if (saved?.binding && saved.binding.serial === state.serial) state.binding = saved.binding;
    if (Array.isArray(saved?.requests)) state.requests = saved.requests.filter(r => r && typeof r.id === "string" && typeof r.serial === "string" && ACTION[r.action]);
    state.selectedRequest = saved?.selectedRequest || null;
  } catch (_) { /* 不据损坏的本机记录推断持有会话。 */ }

  async function call(method, path, body, options = {}) {
    const ctl = new AbortController(), timeout = setTimeout(() => ctl.abort(), options.timeout || 20000);
    const abort = () => ctl.abort();
    if (options.signal?.aborted) ctl.abort();
    options.signal?.addEventListener("abort", abort, { once: true });
    const headers = { "X-HG": "1" };
    if (method !== "GET") headers["Content-Type"] = "application/json";
    try {
      const r = await HGTarget.fetch(API + path, { method, headers, body: method === "GET" ? undefined : JSON.stringify(body || {}), signal: ctl.signal, credentials: "same-origin", cache: "no-store" }, Object.hasOwn(options,"target") ? {target:options.target} : {});
      if ((r.headers.get("Content-Type") || "").includes("application/json")) {
        const value = await r.json();
        if (!r.ok) {
          const e = new Error(message(value.error || `HTTP ${r.status}`)); e.httpStatus = r.status; e.data = value;
          if (r.status === 401) { if (value.need_operator) setOp(null); else location.href = "/dash/login"; }
          throw e;
        }
        return value;
      }
      if (!r.ok) { const e = new Error(`HTTP ${r.status}`); e.httpStatus = r.status; throw e; }
      let meta;
      try { meta = JSON.parse(r.headers.get("X-Meta") || "null"); } catch (_) { throw new Error("截图响应缺少有效回执，须查询原请求"); }
      return { blob: await r.blob(), meta };
    } catch (e) {
      if (e.name === "AbortError") throw new Error("请求中断或超时；尚未确认结果");
      throw e;
    } finally { clearTimeout(timeout); options.signal?.removeEventListener("abort", abort); }
  }
  function setOp(name) {
    if (state.op !== name) { state.sessionFresh = false; invalidateFrame(); }
    state.op = name;
    $("op-login").hidden = !!name; $("op-name").hidden = !name; $("op-logout").hidden = !name;
    $("op-name").textContent = name ? "操作员：" + name : "";
  }
  function selectedDevice() { return state.devices.find(d => d.serial === state.serial); }
  function validDeviceBinding(value) {
    return value?.schema === "phonefarm/device-binding@1" && Object.keys(value).sort().join(",") === "device_id,domain_id,registry_revision,schema,serial" &&
      typeof value.domain_id === "string" && /^[0-9a-f]{32}$/.test(value.domain_id) && Number.isSafeInteger(value.registry_revision) && value.registry_revision > 0 &&
      typeof value.device_id === "string" && /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(value.device_id) &&
      typeof value.serial === "string" && !!value.serial && new TextEncoder().encode(value.serial).length <= 256 && !/[\s\x00-\x1f\x7f]/.test(value.serial);
  }
  function acquireBindingProblem() {
    if (state.acquireScope !== "device") return "";
    if (state.capabilities?.device_binding_v1 !== true) return "节点未声明按登记设备接管能力，不会改为全域请求。";
    if (state.bindingBusy) return "正在读取设备登记…";
    return validDeviceBinding(state.acquireBinding) && state.acquireBinding.serial === state.serial ? "" : state.bindingError || "先显式读取当前设备的登记绑定。";
  }
  function clearAcquireBinding() { state.acquireBinding = null; state.bindingError = ""; state.bindingBusy = false; state.bindingRevision++; }
  function scopeText(session) {
    if (session?.lease_scope === "device") return `登记设备 ${session.device_binding?.device_id || "未提供"} · revision ${session.device_binding?.registry_revision ?? "未知"}`;
    return session?.device_binding ? "实际 lease 范围未确认" : "兼容全域";
  }
  async function resolveAcquireBinding() {
    if ($("resolve-take-binding").disabled) return;
    clearAcquireBinding(); const revision = state.bindingRevision, serial = state.serial, target = HGTarget.binding(); state.bindingBusy = true; render();
    try {
      const value = await call("GET", "/devices/resolve" + query({serial}), null, {target});
      if (revision !== state.bindingRevision) return;
      if (HGTarget.problem(target) || value.node_id !== target.node_id || !validDeviceBinding(value.binding) || value.binding.serial !== serial) throw new Error("登记绑定与固定节点或当前设备不一致");
      state.acquireBinding = value.binding;
    } catch (error) { if (revision === state.bindingRevision) state.bindingError = error.message; }
    finally { if (revision === state.bindingRevision) { state.bindingBusy = false; render(); } }
  }
  function actionSupported(action) {
    const d = selectedDevice();
    return state.capabilities?.available === true && Array.isArray(state.capabilities.actions) && state.capabilities.actions.includes(action)
      && d?.transport !== "hdc" && Array.isArray(d?.capabilities) && d.capabilities.includes(action);
  }
  function unresolved(action) { return state.requests.some(r => r.serial === state.serial && r.session_id === state.binding?.session_id && !TERMINAL.has(r.status) && !(r.action === "acquire" && r.receipt?.phase === "closed") && (!action || r.action === action)); }
  function sessionReason() {
    if (!HGTarget.canWrite()) return HGTarget.problem() || "节点未提供目标约束协议，设备观察与操作禁用。";
    if (state.binding && HGTarget.problem(state.binding.target || null)) return HGTarget.problem(state.binding.target || null);
    if (!state.op) return "先使用操作员令牌登录。";
    if (document.hidden) return "页面已暂停，返回后须重新确认状态。";
    if (!state.devicesFresh) return "设备状态未确认：" + state.devicesError;
    if (state.capabilities?.available !== true) return state.capabilities?.reason || "节点未提供可用的远程会话能力。";
    const d = selectedDevice();
    if (!d) return "先选择设备。";
    if (d.transport === "hdc") return "本批尚不支持 HDC 设备操作。";
    if (!d.enabled) return "此设备未对远程操作开放。";
    if (!state.binding?.session_id) return state.binding?.acquisition_id ? "接管结果未确认，请查询原会话。" : "尚未建立本页可核验的会话。";
    if (!state.sessionFresh) return state.sessionError || "会话状态未确认，请查询。";
    if (state.session?.session_id !== state.binding.session_id || state.session?.serial !== state.serial) return "会话身份不匹配。";
    if (state.session.phase !== "ready") return PHASE[state.session.phase] || PHASE.unknown;
    if (state.session.live_owner !== true) return "未确认活跃 owner，禁止新操作。";
    if (state.session.recovery?.required !== false) return "恢复状态未确认或仍需核查，禁止新操作。";
    return "";
  }
  const coordinateAction = action => action === "tap" || action === "swipe";
  function canAction(action) { return !sessionReason() && !state.posting && !state.authBusy && actionSupported(action) && (action !== "logcat" || state.session?.capabilities?.includes("logcat")) && (action === "release" ? !unresolved("release") : !unresolved()) && (!coordinateAction(action) || !frameReason()); }
  function holderText(d) {
    const h = d.holder;
    if (!state.devicesFresh) return "上次观测；当前状态未知";
    if (!h) return d.enabled ? "当前目录未报告占用" : "未对远程开放";
    if (h.kind === "remote") return `${h.operator || "操作员"} · ${PHASE[h.phase] || PHASE.unknown}${h.serial && h.serial !== d.serial ? " · 全域占用来自 " + h.serial : ""}`;
    if (h.kind === "experiment") return "实验占用：" + (h.label || "未知");
    return "设备 lease 占用或待核查";
  }
  function invalidateFrame() { ++state.imageRevision; if (state.frame) state.frame.inputValid = false; }
  function frameTime(frame) { return typeof frame?.captured_at === "number" ? frame.captured_at * 1000 : Date.parse(frame?.captured_at); }
  function frameReason() {
    const reason = sessionReason(), f = state.frame;
    if (reason) return reason;
    if (state.session.input_contract !== "frame-bound-v1") return "此旧会话未提供截图绑定输入；可继续查看截图、使用其他已支持操作或交回。";
    if (!f) return "先显式采集本会话截图。";
    if (!f.bound || f.serial !== state.serial || f.session_id !== state.binding.session_id) return "截图缺少本设备、本会话的完整 frame 身份。";
    if (!f.decoded || !f.displayed) return "截图尚未完成解码、尺寸核对和显示。";
    if (!Number.isInteger(f.rotation) || f.rotation < 0 || f.rotation > 3) return "截图方向未确认，仅可查看。";
    if (state.session.input_frame_id !== f.frame_id) return "owner 未授权此图输入；请显式采集新图。";
    const age = Date.now() - frameTime(f), maxAge = state.session.frame_max_age_seconds;
    if (!Number.isFinite(age) || age < 0) return "截图采集时间未确认或晚于当前时间，不能输入。";
    if (!Number.isFinite(maxAge) || maxAge <= 0) return "会话未提供截图有效期，不能输入。";
    if (age > maxAge * 1000) return `截图已超过 ${maxAge} 秒，请显式采集新图。`;
    if (!f.inputValid) return "本页输入资格已撤销；请显式采集新图。";
    if (state.posting || state.querying || state.authBusy || unresolved()) return "动作或回执尚待确认；先查询原请求。";
    return "";
  }
  function acquisitionStatus(s) { return s.not_started === true ? "not_executed" : s.session_id ? "completed" : "unknown"; }
  function sessionText(s) { return s.phase === "closed" && s.original_outcome === "unknown" ? "已核验并关闭，原启动结果未知" : PHASE[s.phase] || PHASE.unknown; }
  function requestStatus(r) {
    if (r.action === "acquire" && r.receipt?.phase === "closed" && r.receipt.original_outcome === "unknown") return sessionText(r.receipt);
    if (r.status === "completed" && r.action === "release" && !(state.session?.session_id === r.session_id && state.session.phase === "closed")) return "请求已处理，待确认会话关闭";
    if (r.status === "completed" && r.action === "acquire") return "已取得会话记录";
    return STATUS[r.status] || STATUS.unknown;
  }
  function renderFrame() {
    const f = state.frame, reason = frameReason(), valid = !!f && !reason;
    $("hg-screen").classList.toggle("stale", !!f && !valid);
    $("hg-screen").classList.toggle("input-ready", !!valid);
    if (!f) { $("frame-info").textContent = "共享画面仅供查看。" + reason; return; }
    const at = frameTime(f), age = Date.now() - at;
    const time = Number.isFinite(at) ? `采集于 ${new Date(at).toLocaleTimeString()} · ${age >= 0 ? `约 ${Math.floor(age / 1000)} 秒前` : "采集时间晚于当前时间"}` : "采集时间未提供";
    const scaled = f.width !== f.imageWidth || f.height !== f.imageHeight ? ` · 传输图 ${f.imageWidth} × ${f.imageHeight}` : "";
    const rotation = Number.isInteger(f.rotation) ? `${f.rotation}（SurfaceOrientation）` : "未知";
    $("frame-info").textContent = `设备 ${f.serial} · frame ${f.frame_id || "未提供"} · 原图 ${f.width} × ${f.height}${scaled} · 方向 ${rotation} · ${time}。${valid ? "可点击或拖动；同尺寸与方向不保证游戏内容未变化。" : "保留图片，仅供查看：" + reason}`;
  }
  function renderReceipt() {
    const rows = state.requests.filter(r => r.serial === state.serial), select = $("request-select");
    if (!rows.some(r => r.id === state.selectedRequest)) state.selectedRequest = rows.at(-1)?.id || null;
    select.replaceChildren(...rows.map(r => { const o = document.createElement("option"); o.value = r.id; o.textContent = `${ACTION[r.action]} · ${requestStatus(r)} · ${r.id}`; return o; }));
    select.value = state.selectedRequest || ""; select.disabled = !rows.length || state.querying;
    const r = rows.find(r => r.id === state.selectedRequest);
    $("check-request").disabled = !state.op || !r || !!(r && HGTarget.problem(r.target || null)) || state.querying || state.authBusy || state.posting;
    $("check-request").textContent = state.querying ? "查询中…" : "查询原请求";
    const source = r?.input ? `来源 frame ${r.input.frame_id}；原图坐标 ${r.action === "tap" ? `${r.input.x}, ${r.input.y}` : `${r.input.x1}, ${r.input.y1} → ${r.input.x2}, ${r.input.y2}`}。` : "";
    const code = r?.receipt?.error_code || r?.receipt?.result?.code;
    const result = r?.receipt?.result?.input_issued === false ? "动作已开始核验，但未发出坐标输入。" : r?.receipt?.result?.input_issued === true ? "已发出输入命令；不证明应用已响应。" : "";
    $("request-info").textContent = (r && HGTarget.problem(r.target || null)) || (r ? `${ACTION[r.action]}：${requestStatus(r)}。请求 ID ${r.id}${r.session_id ? "；会话 " + r.session_id : "；按接管请求 ID 查询会话"}。${source}${result}${code ? (FRAME_ERROR[code] || code) + "。" : ""}${r.action === "release" ? "交回须以会话 closed 为准；接受请求不代表已释放。" : "未决或网络错误只查询原 ID，不自动重发动作。"}${r.error ? " " + r.error : ""}` : "尚无动作。接受请求只表示已记录，须查询实际完成状态。");
    $("out").textContent = r?.receipt ? JSON.stringify(r.receipt, null, 2) : r?.error || "";
    const logs = r?.action === "logcat" ? r.receipt?.result : null;
    $("logcat-result").hidden = typeof logs?.stdout !== "string";
    $("logcat-output").textContent = typeof logs?.stdout === "string" ? logs.stdout : "";
    $("logcat-result-info").textContent = !logs ? "" : logs.stdout_truncated === true ? "输出已截断；这里只显示本次快照保留下来的内容。" : logs.stdout_truncated === false ? "本次返回未截断；范围限于请求的行数和 PID，不代表完整日志历史。" : "此回执未声明输出是否截断。";
  }
  function render() {
    $("devices").replaceChildren(...state.devices.map(d => {
      const li = document.createElement("li"), b = document.createElement("button"), serial = document.createElement("span"), sub = document.createElement("span");
      b.type = "button"; b.setAttribute("aria-pressed", String(d.serial === state.serial)); serial.className = "dev-serial"; serial.textContent = d.serial;
      sub.className = "dev-sub" + (d.holder ? " dev-held" : ""); sub.textContent = `${d.model || d.transport || "设备"} · ${holderText(d)}`;
      b.append(serial, sub); b.addEventListener("click", () => select(d.serial)); li.append(b); return li;
    }));
    if (!state.devices.length) { const p = document.createElement("li"); p.className = "note"; p.textContent = state.devicesFresh ? "当前目录没有设备" : "设备目录尚未确认"; $("devices").append(p); }
    const d = selectedDevice(), reason = sessionReason(), h = d?.holder;
    const reconnect = !state.binding && h?.kind === "remote" && h.serial === state.serial && h.session_id;
    const canTake = HGTarget.canWrite() && state.op && state.devicesFresh && d?.enabled && actionSupported("acquire") && !state.posting && !state.authBusy
      && !state.binding && (!h || reconnect) && (reconnect || !acquireBindingProblem());
    const fixed = state.binding, fixedScope = fixed ? (fixed.device_binding ? "device" : "global") : state.acquireScope;
    $("take-scope").value = fixedScope; $("take-scope").disabled = !!fixed || !!reconnect || !state.op || state.posting || state.authBusy || state.bindingBusy;
    $("take-scope").querySelector('[value="device"]').disabled = state.capabilities?.device_binding_v1 !== true;
    $("resolve-take-binding").hidden = fixedScope !== "device" || !!fixed || !!reconnect;
    $("resolve-take-binding").disabled = !!fixed || !!reconnect || !state.op || !state.serial || !HGTarget.canWrite() || state.capabilities?.device_binding_v1 !== true || state.posting || state.authBusy || state.bindingBusy;
    $("resolve-take-binding").setAttribute("aria-busy", String(state.bindingBusy));
    const chosen = fixed?.device_binding || (!fixed && state.acquireScope === "device" ? state.acquireBinding : null);
    $("take-binding-json").hidden = !chosen; $("take-binding-json").textContent = chosen ? JSON.stringify(chosen, null, 2) : "";
    $("take-binding-info").textContent = fixed ? "原接管或会话身份已固定，查询不会重新选择设备绑定。实际范围以下方会话回执为准。" : reconnect ? "将连接已有会话并读取其原范围，不创建新接管。" : acquireBindingProblem() || (chosen ? "登记绑定已读取，新接管将固定此范围；可与其他已绑定设备并行，全域操作仍会互斥。" : "默认保持兼容全域。显式按登记设备接管前只读核对宿主登记，不采样手机。");
    $("cur-serial").textContent = state.serial || "";
    $("take").textContent = reconnect ? "连接已有会话" : "接管设备"; $("take").disabled = !canTake;
    $("release").disabled = !canAction("release"); $("renew").disabled = !canAction("renew");
    $("check-session").disabled = !!(state.binding && HGTarget.problem(state.binding.target || null)) || !state.op || !state.serial || state.refreshing || state.authBusy || state.posting || !(state.binding || reconnect);
    $("check-session").textContent = state.refreshing ? "查询中…" : "查询会话";
    $("ops").disabled = !!reason || state.posting || state.authBusy || unresolved();
    document.querySelectorAll("[data-action], [data-key]").forEach(b => { const action = b.dataset.action || "key"; b.disabled = !canAction(action); b.title = action === "logcat" && !state.session?.capabilities?.includes("logcat") ? "当前 owner 未声明支持读取日志；新程序不会自动升级已有会话。" : actionSupported(action) ? reason || (unresolved() ? "先查询未决动作" : "") : "当前设备未声明支持此动作"; });
    $("lease-info").textContent = state.session ? `${sessionText(state.session)} · ${state.session.session_id || "身份未知"}${state.sessionFresh ? "" : "（上次观测，当前未确认）"}${state.session.deadline ? " · 最晚 " + state.session.deadline : ""}` : state.binding?.acquisition_id ? "接管请求待确认 · " + state.binding.acquisition_id : d ? holderText(d) : "先选择设备";
    $("session-scope").textContent = state.session ? `会话报告的 lease 范围：${scopeText(state.session)}${state.sessionFresh ? "" : "（历史记录）"}。` : "尚无当前会话范围回执。";
    $("operation-info").textContent = reason || (unresolved() ? "存在未决动作；先查询原请求，或显式请求交回等待收尾。" : "会话已确认；每次操作只提交一次。交回和恢复完成是不同事实。");
    $("capability-info").textContent = state.capabilities?.available === true ? "当前支持：" + (state.capabilities.actions || []).map(a => ACTION[a] || a).join("、") : state.capabilities?.reason || "远程能力尚未确认";
    $("op-logout").disabled = state.authBusy || state.posting;
    $("op-login").querySelector("button").disabled = state.authBusy;
    $("ctl-panel")?.setAttribute("aria-busy", String(state.posting));
    renderReceipt(); renderFrame();
  }
  function dropLive() { if (state.live?.detach) state.live.detach(); state.live = null; }
  function startView() {
    dropLive();
    if (!state.serial || state.frame) return;
    const deviceBinding = state.binding?.device_binding || state.session?.device_binding || null;
    state.liveScope = JSON.stringify(deviceBinding);
    if (window.HGLive?.attach) {
      try { state.live = window.HGLive.attach($("hg-screen-wrap"), state.serial, () => null, {device_binding:deviceBinding, target:state.binding?.target || HGTarget.binding()}); }
      catch (_) { state.live = null; }
    }
    $("screen-empty").hidden = !!state.live;
  }
  function select(serial) {
    if (serial === state.serial) return;
    if (state.binding || state.posting) { msg("当前会话或接管结果尚未关闭；请先查询并完成交回再切换设备。", true); return; }
    clearAcquireBinding(); state.revision++; state.serial = serial; state.session = null; state.sessionFresh = false; state.frame = null;
    if (state.url) URL.revokeObjectURL(state.url); state.url = null;
    $("hg-screen").hidden = true; $("hg-screen").removeAttribute("src");
    startView(); save(); render();
  }
  function validateSession(s, binding) {
    const unavailable = s?.session_id == null && ["unknown", "starting", "closed"].includes(s?.phase);
    if (!s || (typeof s.session_id !== "string" && !unavailable) || s.serial !== binding.serial
      || (binding.session_id && s.session_id !== binding.session_id && !(unavailable && s.phase === "unknown"))
      || (binding.acquisition_id && s.acquisition_id !== binding.acquisition_id)) throw new Error("会话回包身份不匹配，未获得操作资格");
    if (binding.binding_recorded && (binding.device_binding ? s.lease_scope !== "device" || !validDeviceBinding(s.device_binding) || Object.keys(binding.device_binding).some(key => s.device_binding[key] !== binding.device_binding[key]) : s.device_binding || s.lease_scope === "device")) throw new Error("会话设备范围与原接管声明不一致，请保留原请求查询");
  }
  function applySession(s, binding) {
    validateSession(s, binding);
    state.binding = { ...binding, session_id: s.session_id || binding.session_id }; state.session = s; state.sessionFresh = !document.hidden; state.sessionError = "";
    const acquisition = state.requests.find(r => r.action === "acquire" && r.id === binding.acquisition_id);
    if (acquisition) { acquisition.session_id = s.session_id; acquisition.status = acquisitionStatus(s); acquisition.receipt = s; acquisition.error = s.error || null; }
    if (s.phase !== "ready" || s.live_owner !== true || s.recovery?.required !== false) invalidateFrame();
    if (s.phase === "closed") { state.binding = null; invalidateFrame(); msg(s.not_started === true ? "本次接管明确未启动，节点已关闭此尝试。" + (s.error || "") : sessionText(s) + "；关闭记录不代表游戏或设备状态已恢复。"); }
    if (!state.frame && JSON.stringify(state.binding?.device_binding || s.device_binding || null) !== state.liveScope) startView();
    save();
  }
  async function readSession(binding, signal) {
    const params = binding.session_id ? { session_id: binding.session_id } : { acquisition_id: binding.acquisition_id };
    return call("GET", dev(binding.serial, "session") + query(params), null, { signal, target: binding.target || null });
  }
  async function refresh() {
    if (state.refreshing || state.authBusy || state.posting || document.hidden) return;
    const ctl = new AbortController(), revision = state.revision; state.refreshing = true; state.refreshCtl = ctl; render();
    try {
      const who = await call("GET", "/whoami", null, { signal: ctl.signal });
      if (revision !== state.revision || ctl.signal.aborted) return;
      setOp(who.operator);
      if (!state.op) { state.devicesFresh = false; state.devicesError = "尚未登录操作员"; return; }
      const r = await call("GET", "/devices", null, { signal: ctl.signal });
      if (revision !== state.revision || ctl.signal.aborted) return;
      if (!Array.isArray(r.devices)) throw new Error("设备目录格式不正确");
      state.devices = r.devices; state.capabilities = r.capabilities; state.devicesFresh = !r.lock_storage_error; state.devicesError = r.lock_storage_error || "";
      if (r.lock_storage_error) invalidateFrame();
      const binding = state.binding && { ...state.binding };
      if (binding) {
        try {
          const s = await readSession(binding, ctl.signal);
          if (revision === state.revision && !ctl.signal.aborted && state.binding?.session_id === binding.session_id && state.binding?.acquisition_id === binding.acquisition_id) applySession(s, binding);
        } catch (e) { if (revision === state.revision && !ctl.signal.aborted) { state.sessionFresh = false; state.sessionError = e.message; invalidateFrame(); } }
      }
    } catch (e) {
      if (revision === state.revision && !ctl.signal.aborted) { state.devicesFresh = false; state.sessionFresh = false; state.devicesError = e.message; invalidateFrame(); msg("当前状态未确认：" + e.message, true); }
    } finally { if (state.refreshCtl === ctl) { state.refreshCtl = null; state.refreshing = false; render(); } }
  }
  function addRequest(action, session_id, input) {
    const r = { id: crypto.randomUUID(), serial: state.serial, session_id: session_id || null, action, status: "unknown", at: Date.now(), target: HGTarget.binding(), ...(input ? {input: {...input}} : {}) };
    state.requests = state.requests.filter((item, i, all) => !TERMINAL.has(item.status) || i >= all.length - 19);
    state.requests.push(r); state.selectedRequest = r.id; save(); return r;
  }
  function receipt(r, value) {
    if (!value || value.request_id !== r.id || value.session_id !== r.session_id || !STATUS[value.status]) throw new Error("动作回执身份或状态不匹配，请查询原 ID");
    r.status = value.status; r.receipt = value; r.error = value.error || value.result?.error ? message(value.error || value.result.error) : null; save();
  }
  function unknownRequest(r, e) {
    r.status = e.httpStatus >= 400 && e.httpStatus < 500 && e.httpStatus !== 409 ? "failed" : "unknown";
    r.error = e.message; state.sessionFresh = false; state.sessionError = "动作结果需查询原 ID 并重新确认会话"; invalidateFrame(); save();
    msg(`请求 ${r.id}：${e.message}。不会自动重发。`, true);
  }
  async function showImage(r, image, knownMeta) {
    const generation = ++state.imageRevision, revision = state.revision;
    const current = () => generation === state.imageRevision && revision === state.revision && !document.hidden && !HGTarget.problem(r.target || null)
      && state.serial === r.serial && state.binding?.session_id === r.session_id && state.selectedRequest === r.id;
    let response = image;
    if (!response) response = await call("GET", dev(r.serial, "requests/" + encodeURIComponent(r.id)) + query({ session_id: r.session_id, image: "1" }), null, {target:r.target || null});
    const meta = response.meta || knownMeta;
    if (!response.blob || !meta || meta.request_id !== r.id || meta.session_id !== r.session_id || meta.status !== "completed") throw new Error("截图身份未确认，保留旧图并查询原请求");
    const result = meta.result || knownMeta || {};
    const size = result.original_size || (result.source_width && result.source_height ? [result.source_width, result.source_height] : meta.size);
    if (!Array.isArray(size) || size.length !== 2 || !size.every(n => Number.isInteger(n) && n > 0)) throw new Error("截图未提供可用的像素尺寸");
    if (![result.width, result.height].every(n => Number.isInteger(n) && n > 0)) throw new Error("截图未提供传输图片尺寸");
    for (const [key, expected] of [["frame_id", r.id], ["session_id", r.session_id], ["serial", r.serial]]) {
      if (result[key] != null && result[key] !== expected) throw new Error("截图内容的设备、会话或 frame 身份不匹配");
    }
    if (!current()) return;
    const url = URL.createObjectURL(response.blob), decoded = new Image();
    let shown = false;
    try {
      decoded.src = url;
      await decoded.decode();
      if (decoded.naturalWidth !== result.width || decoded.naturalHeight !== result.height) throw new Error("图片解码尺寸与截图回执不一致");
      if (!current()) return;
      const frame = { request_id: r.id, frame_id: result.frame_id || null, serial: r.serial, session_id: r.session_id,
        bound: result.frame_id === r.id && result.session_id === r.session_id && result.serial === r.serial,
        width: size[0], height: size[1], imageWidth: decoded.naturalWidth, imageHeight: decoded.naturalHeight,
        rotation: Number.isInteger(result.rotation) && result.rotation >= 0 && result.rotation <= 3 ? result.rotation : null,
        captured_at: result.captured_at ?? meta.captured_at ?? null, decoded: true, displayed: false, inputValid: true };
      dropLive();
      const old = state.url, img = $("hg-screen");
      img.onload = () => { if (old) URL.revokeObjectURL(old); if (state.frame === frame) { frame.displayed = true; renderFrame(); } };
      img.onerror = () => { if (old) URL.revokeObjectURL(old); if (state.frame === frame) { invalidateFrame(); msg("截图显示失败，保留动作回执；请查询原请求。", true); renderFrame(); } };
      state.url = url; state.frame = frame; shown = true;
      img.src = url; img.hidden = false; $("screen-empty").hidden = true;
    } finally { if (!shown) URL.revokeObjectURL(url); }
  }
  async function displayImage(r, image, knownMeta) {
    const generation = state.imageRevision + 1;
    try { await showImage(r, image, knownMeta); }
    catch (e) { r.error = "动作回执已取得，但图片未显示：" + e.message; if (generation === state.imageRevision) invalidateFrame(); }
  }
  function supersedeRead() {
    state.revision++; state.refreshCtl?.abort();
  }
  async function act(action, body = {}) {
    if (!canAction(action)) { msg((coordinateAction(action) ? frameReason() : sessionReason()) || "当前动作不可用，先查询未决请求或确认能力。", true); return; }
    if (coordinateAction(action) && body.frame_id !== state.frame.frame_id) { msg("截图已变化，本次输入未提交；请依据当前图重新操作。", true); return; }
    supersedeRead();
    const r = addRequest(action, state.binding.session_id, coordinateAction(action) ? body : null), revision = state.revision;
    state.posting = true;
    if (!["renew", "logcat"].includes(action)) { invalidateFrame(); state.sessionFresh = false; state.sessionError = "动作提交后须重新读取 owner 的截图资格。"; }
    render();
    try {
      const path = dev(r.serial, action === "release" ? "session" : action);
      const response = await call(action === "release" ? "DELETE" : "POST", path, { ...body, request_id: r.id, session_id: r.session_id }, {target:r.target || null});
      if (response.blob) { receipt(r, response.meta); if (action === "screenshot") await displayImage(r, response); }
      else {
        receipt(r, response);
        if (action === "screenshot" && r.status === "completed" && response.result) await displayImage(r, null, { ...response.result, request_id: r.id, session_id: r.session_id, status: r.status });
      }
      if (revision === state.revision) msg(`${ACTION[action]}：${requestStatus(r)}。${r.error || ""}${action === "release" ? "等待查询会话 closed 后确认交回。" : ""}`, !!r.error || r.status === "failed" || r.status === "unknown");
      return r;
    } catch (e) { unknownRequest(r, e); }
    finally { state.posting = false; render(); refresh(); }
  }
  async function acquire() {
    if ($("take").disabled) return;
    const holder = selectedDevice()?.holder;
    if (holder?.kind === "remote" && holder.serial === state.serial && holder.session_id) {
      await connectExisting(holder); return;
    }
    supersedeRead();
    const r = addRequest("acquire", null); r.binding_recorded = true;
    if (state.acquireScope === "device") r.device_binding = {...state.acquireBinding};
    const binding = { serial: state.serial, acquisition_id: r.id, target:r.target, binding_recorded:true, ...(r.device_binding ? {device_binding:r.device_binding} : {}) };
    state.binding = binding; state.posting = true; state.sessionFresh = false; save(); render();
    try { const s = await call("POST", dev(r.serial, "session"), { request_id: r.id, ...(r.device_binding ? {device_binding:r.device_binding} : {}) }, {target:r.target || null}); applySession(s, binding); msg(sessionText(s)); }
    catch (e) {
      unknownRequest(r, e);
      if (r.status === "failed") { state.binding = null; save(); }
    } finally { state.posting = false; render(); refresh(); }
  }
  async function connectExisting(holder) {
    if (state.posting || !state.op) return;
    const binding = { serial: state.serial, session_id: holder.session_id, target:HGTarget.binding() };
    supersedeRead(); state.posting = true; render();
    try { const s = await readSession(binding); applySession(s, binding); msg("已读取会话：" + sessionText(s)); }
    catch (e) { state.sessionFresh = false; msg("无法连接此会话：" + e.message, true); }
    finally { state.posting = false; render(); refresh(); }
  }
  async function queryRequest() {
    const r = state.requests.find(item => item.id === state.selectedRequest);
    if (!r || state.querying || state.posting || !state.op) return;
    const revision = state.revision;
    state.querying = true; render();
    try {
      if (r.action === "acquire") {
        const binding = { serial: r.serial, acquisition_id: r.id, target:r.target || null, ...(r.binding_recorded ? {binding_recorded:true} : {}), ...(Object.hasOwn(r,"device_binding") ? {device_binding:r.device_binding} : {}) }, s = await readSession(binding);
        validateSession(s, binding);
        if (revision === state.revision && state.serial === r.serial && (!state.binding || state.binding.acquisition_id === r.id || state.binding.session_id === s.session_id)) applySession(s, binding);
        r.status = acquisitionStatus(s); r.session_id = s.session_id; r.receipt = s; r.error = s.error || null;
      } else {
        const response = await call("GET", dev(r.serial, "requests/" + encodeURIComponent(r.id)) + query({ session_id: r.session_id }), null, {target:r.target || null});
        receipt(r, response);
        if (r.action === "screenshot" && r.status === "completed" && response.result) await displayImage(r, null, { ...response.result, request_id: r.id, session_id: r.session_id, status: r.status });
      }
      save(); if (revision === state.revision) msg("已查询原请求：" + requestStatus(r) + (r.error ? "；" + r.error : ""), !!r.error);
    } catch (e) { r.error = "查询未能确认结果：" + e.message; if (revision === state.revision) msg(r.error + "。保留原 ID，不重发。", true); }
    finally { state.querying = false; render(); refresh(); }
  }
  function bindPointer(el) {
    let down = null;
    function pos(ev, frame, rect) {
      const x = ev.clientX - rect.left, y = ev.clientY - rect.top;
      if (!rect.width || !rect.height || x < 0 || y < 0 || x >= rect.width || y >= rect.height) return null;
      return [Math.floor(x / rect.width * frame.width), Math.floor(y / rect.height * frame.height)];
    }
    const sameRect = (a, b) => ["left", "top", "width", "height"].every(key => a[key] === b[key]);
    function cancel(reason) { down = null; msg(reason, true); renderFrame(); }
    el.addEventListener("pointerdown", ev => {
      if (!ev.isPrimary || ev.button !== 0 || (!canAction("tap") && !canAction("swipe"))) return;
      const frame = state.frame, rect = el.getBoundingClientRect(), p = pos(ev, frame, rect);
      if (!p) return;
      down = { p, clientX: ev.clientX, clientY: ev.clientY, at: Date.now(), frame, rect, pointer: ev.pointerId, session: state.binding.session_id };
      el.setPointerCapture(ev.pointerId); ev.preventDefault();
    });
    el.addEventListener("pointermove", ev => {
      if (down?.pointer === ev.pointerId && !pos(ev, down.frame, down.rect)) cancel("拖动已超出图片，本次输入已取消。请在图内重新操作。");
    });
    el.addEventListener("pointercancel", () => { down = null; });
    el.addEventListener("lostpointercapture", () => { down = null; });
    el.addEventListener("pointerup", ev => {
      const d = down; down = null;
      if (!d || d.pointer !== ev.pointerId) return;
      const reason = frameReason();
      if (d.frame !== state.frame || d.session !== state.binding?.session_id || !sameRect(d.rect, el.getBoundingClientRect()) || reason) { cancel(reason || "截图或显示位置已变化，本次输入已取消。"); return; }
      const p = pos(ev, d.frame, d.rect);
      if (!p) { cancel("结束位置超出图片，本次输入已取消。"); return; }
      const frame_id = d.frame.frame_id;
      if (Math.hypot(ev.clientX - d.clientX, ev.clientY - d.clientY) > 8) act("swipe", { frame_id, x1: d.p[0], y1: d.p[1], x2: p[0], y2: p[1], ms: Math.min(5000, Math.max(80, Date.now() - d.at)) });
      else act("tap", { frame_id, x: d.p[0], y: d.p[1] });
    });
  }
  document.addEventListener("hgamey-target-change", () => { if (!HGTarget.canWrite()) invalidateFrame(); render(); });
  $("op-login").addEventListener("submit", async ev => {
    ev.preventDefault(); if (state.authBusy) return; supersedeRead(); state.authBusy = true; render();
    const token = $("op-token").value.trim(); $("op-token").value = "";
    try { const r = await call("POST", "/login", { token }); setOp(r.operator); await HGTarget.refresh(); msg("已登录，正在查询会话。"); }
    catch (e) { msg(e.message, true); }
    finally { state.authBusy = false; render(); refresh(); }
  });
  $("op-logout").addEventListener("click", async () => {
    if (state.authBusy || state.posting) return; supersedeRead(); state.authBusy = true; render();
    try { await call("POST", "/logout", {}); setOp(null); state.devicesFresh = false; msg("已退出操作员。退出登录不代表设备已交回；原会话和请求 ID 已保留。"); }
    catch (e) { state.sessionFresh = false; invalidateFrame(); msg("退出结果未确认：" + e.message, true); }
    finally { state.authBusy = false; render(); }
  });
  $("take").addEventListener("click", acquire);
  $("take-scope").addEventListener("change", () => { if ($("take-scope").disabled) return; clearAcquireBinding(); state.acquireScope = $("take-scope").value; render(); });
  $("resolve-take-binding").addEventListener("click", resolveAcquireBinding);
  $("renew").addEventListener("click", () => act("renew"));
  $("release").addEventListener("click", () => act("release"));
  $("check-session").addEventListener("click", () => {
    if (!state.binding) { const h = selectedDevice()?.holder; if (h?.kind === "remote" && h.session_id) connectExisting(h); }
    else refresh();
  });
  $("request-select").addEventListener("change", () => { state.selectedRequest = $("request-select").value; save(); renderReceipt(); });
  $("check-request").addEventListener("click", queryRequest);
  document.querySelectorAll("[data-key]").forEach(b => b.addEventListener("click", () => act("key", { key: b.dataset.key })));
  $("shot-now").addEventListener("click", () => act("screenshot"));
  $("text-form").addEventListener("submit", async ev => { ev.preventDefault(); const value = $("text").value; if (!value) return; const r = await act("text", { text: value }); if (r?.status === "completed" && $("text").value === value) $("text").value = ""; });
  $("app-form").addEventListener("submit", ev => { ev.preventDefault(); const pkg = $("pkg").value.trim(); if (pkg) act("app_start", { package: pkg }); });
  $("app-stop").addEventListener("click", () => { const pkg = $("pkg").value.trim(); if (pkg) act("app_stop", { package: pkg }); });
  $("logcat-form").addEventListener("submit", ev => {
    ev.preventDefault(); const lines = Number($("logcat-lines").value), rawPID = $("logcat-pid").value.trim(), pid = Number(rawPID);
    if (!Number.isInteger(lines) || lines < 1 || lines > 2000 || rawPID && (!Number.isInteger(pid) || pid < 1 || pid > 2147483647)) { msg("日志行数须为 1–2000 的整数，PID 留空或填写 1–2147483647 的整数。", true); return; }
    act("logcat", {lines, ...(rawPID ? {pid} : {})});
  });
  $("shell-form").addEventListener("submit", ev => ev.preventDefault());
  function pause() {
    state.refreshCtl?.abort(); state.sessionFresh = false; state.devicesFresh = false; state.devicesError = "页面已暂停，需重新读取"; invalidateFrame(); render();
  }
  function startTimer() { if (!state.timer) state.timer = setInterval(() => { if (!document.hidden) { refresh(); renderFrame(); } }, 5000); }
  document.addEventListener("visibilitychange", () => { if (document.hidden) pause(); else refresh(); });
  window.addEventListener("pagehide", () => { pause(); clearInterval(state.timer); state.timer = 0; });
  window.addEventListener("pageshow", () => { startTimer(); refresh(); });
  bindPointer($("hg-screen")); startView(); render(); refresh(); startTimer();
})();
