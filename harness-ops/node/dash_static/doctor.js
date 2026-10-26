/* Read-only host diagnostics. Device qualification and repair remain explicit. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const statuses = {ok:'通过', warning:'需关注', error:'需修复', unknown:'未知', not_applicable:'不适用'};
  const categories = {connection:'连接与节点身份', runtime:'程序、版本与签名', tools:'宿主工具', storage:'磁盘与数据位置', permissions:'目录权限', devices:'设备授权与实测资格'};
  let busy = false, report = null;
  const text = (tag, value, className) => { const el = document.createElement(tag); el.textContent = value; if (className) el.className = className; return el; };
  const timestamp = value => typeof value === 'number' && Number.isFinite(value) ? new Date(value * 1000).toLocaleString() : '未记录';
  const json = value => JSON.stringify(value, null, 2);
  function validate(value, nodeId) {
    if (value?.schema !== 'hgamey/node-doctor@1' || value.scope !== 'host-only' || !Array.isArray(value.checks) || !value.summary || !Array.isArray(value.limitations)) throw new Error('诊断返回了不支持的记录结构；请核对服务版本后重试。');
    if (value.node_id !== nodeId) throw new Error('诊断记录的节点身份与当前绑定不一致，已拒绝显示。请核对目标身份后重试。');
    if (Object.keys(statuses).some(key => !Number.isInteger(value.summary[key]) || value.summary[key] < 0) || value.checks.some(check => !check || !Object.hasOwn(statuses, check.status) || !Object.hasOwn(categories, check.category) || !Array.isArray(check.remediation))) throw new Error('诊断状态记录不完整；请核对服务版本后重试。');
    if (Object.keys(statuses).some(key => value.summary[key] !== value.checks.filter(check => check.status === key).length)) throw new Error('诊断汇总与逐项状态不一致，已拒绝显示。请维护者核对服务版本后重试。');
    return value;
  }
  function renderCheck(check) {
    const item = document.createElement('li'); item.className = 'doctor-check';
    const heading = document.createElement('div'); heading.className = 'doctor-check-heading';
    heading.append(text('h3', check.title || check.id), text('span', statuses[check.status], `doctor-state ${check.status}`));
    item.append(heading, text('p', check.message || '未提供说明。'), text('p', `观察时间：${timestamp(check.observed_at)}`, 'read-time'));
    if (check.remediation.length) {
      const remediation = document.createElement('div'); remediation.className = 'doctor-remediation';
      remediation.append(text('h4', '建议下一步（不会自动执行）'));
      const list = document.createElement('ul');
      for (const action of check.remediation) {
        const row = text('li', action.text || '未提供说明。');
        if (action.command) { const command = text('pre', action.command); command.tabIndex = 0; row.append(command); }
        list.append(row);
      }
      remediation.append(list); item.append(remediation);
    }
    const evidence = document.createElement('details');
    const body = text('pre', json(check.evidence ?? {})); body.tabIndex = 0;
    evidence.append(text('summary', '查看观察依据'), body); item.append(evidence);
    return item;
  }
  function render(value) {
    $('doctor-time').textContent = `诊断时间：${timestamp(value.checked_at)}`;
    $('doctor-node').textContent = `节点 ${value.node_id} · 仅宿主检查`;
    $('doctor-counts').replaceChildren(...Object.entries(statuses).map(([key, label]) => {
      const item = document.createElement('li'); item.append(text('strong', String(value.summary[key])), text('span', label)); return item;
    }));
    const groups = [];
    for (const [category, label] of Object.entries(categories)) {
      const checks = value.checks.filter(check => check.category === category);
      if (!checks.length) continue;
      const section = document.createElement('section'); section.className = 'doctor-group';
      const heading = text('h2', label); heading.id = `doctor-group-${category}`; section.setAttribute('aria-labelledby', heading.id);
      const list = document.createElement('ul'); list.className = 'doctor-checks'; list.append(...checks.map(renderCheck));
      section.append(heading, list); groups.push(section);
    }
    $('doctor-groups').replaceChildren(...groups);
    $('doctor-limitations').replaceChildren(...value.limitations.map(item => text('li', item)));
    $('doctor-json').textContent = json(value);
    $('doctor-report').hidden = false;
  }
  async function refresh() {
    if (busy) return;
    busy = true;
    $('doctor-refresh').disabled = true; $('doctor-refresh').setAttribute('aria-busy', 'true');
    document.querySelector('main').setAttribute('aria-busy', 'true');
    $('doctor-message').textContent = report ? '正在重新诊断；下方保留上次记录。' : '正在读取环境诊断…';
    $('doctor-error').hidden = true; $('doctor-login').hidden = true;
    const controller = new AbortController(); let timer;
    try {
      const identity = await window.HGTarget.ready();
      if (!identity.capabilities?.includes('node.doctor-v1')) throw new Error('此节点尚未提供 node.doctor-v1。请维护者核对并部署支持环境诊断的服务版本，再刷新目标身份。');
      timer = setTimeout(() => controller.abort(), 15000);
      const response = await window.HGTarget.fetch('/dash/api/doctor', {credentials:'same-origin', cache:'no-store', signal:controller.signal});
      if (!response.ok) {
        if (response.status === 401) $('doctor-login').hidden = false;
        let message = `诊断读取失败（HTTP ${response.status}）。`;
        try { const error = await response.json(); const detail = typeof error.error === 'string' ? error.error : error.error?.message; if (detail) message += ` ${detail}`; } catch (_) { /* HTTP status remains the useful failure. */ }
        throw new Error(message);
      }
      report = validate(await response.json(), identity.node_id);
      render(report); document.querySelector('main').classList.remove('doctor-report-stale');
      $('doctor-message').textContent = '诊断读取完成。页面不会自动重复检查；环境调整后可重新诊断。';
    } catch (error) {
      $('doctor-error').textContent = error.name === 'AbortError' ? '诊断读取超时。请核对节点连接，然后点击「重新诊断」。' : error.message;
      $('doctor-error').hidden = false;
      document.querySelector('main').classList.toggle('doctor-report-stale', !!report);
      $('doctor-message').textContent = report ? '本次诊断失败。下方是上次成功记录，不能代表当前状态。' : '尚未取得可用诊断记录。';
    } finally {
      clearTimeout(timer); busy = false;
      $('doctor-refresh').disabled = false; $('doctor-refresh').removeAttribute('aria-busy');
      document.querySelector('main').removeAttribute('aria-busy');
    }
  }
  $('doctor-refresh').addEventListener('click', refresh);
  refresh();
})();
