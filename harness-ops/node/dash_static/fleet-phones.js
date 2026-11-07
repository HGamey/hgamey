// 全部手机: every phone on every computer that joined, as the center sees it, with what can be done to it remotely and
// links to control it or send it a job.
(() => {
  'use strict';
  const API = '/dash/api', $ = id => document.getElementById(id);
  const ACTIONS = [['install', '装包'], ['push', '推文件'], ['shell', '命令'], ['screenshot', '截图']];
  const STATE = {on: '在线', old: '在线', off: '离线', gone: '未接入'};
  let busy = false;

  function el(tag, text, className) {
    const node = document.createElement(tag);
    if (text != null) node.textContent = text;
    if (className) node.className = className;
    return node;
  }
  function link(text, href) { const node = el('a', text, 'workspace-link'); node.href = href; return node; }
  async function json(path) {
    const response = await HGTarget.fetch(API + path, {credentials: 'same-origin', cache: 'no-store'});
    if (response.ok) return response.json();
    const body = await response.json().catch(() => null);
    throw new Error(body?.error || `HTTP ${response.status}`);
  }

  function row(item, node) {
    const phone = HGPhoneState.of(item), note = HGPhoneState.note(phone);
    const li = el('li', null, `phone-row is-${phone.state}`);
    li.dataset.ref = item.ref;
    const name = el('div', null, 'phone-name');
    name.append(el('strong', phone.title), el('span', item.serial, 'phone-serial'));
    const state = el('span', null, 'phone-state');
    state.append(el('i', null, 'phone-dot'), el('span', STATE[phone.state]));
    if (phone.state === 'old') state.append(el('span', note || '暂无读数', 'phone-note'));
    const actions = ACTIONS.filter(([action]) => (item.remote_actions || []).includes(action)).map(([, label]) => label);
    // Only a phone that is there can be controlled or sent a job.
    const go = el('span', null, 'phone-links');
    if (phone.online && item.remote_actions?.length) go.append(link('远程控制', `/dash/control?device=${encodeURIComponent(item.ref)}`));
    if (phone.online) go.append(link('派作业', `/dash/jobs?phone=${encodeURIComponent(item.ref)}`));
    li.append(name, el('span', node, 'phone-node'), state, el('span', phone.os || '—', 'phone-os'),
      el('span', phone.root == null ? '—' : phone.root ? 'root' : '无 root', 'phone-root'),
      el('span', actions.length ? actions.join(' · ') : '不可远程', 'phone-actions'), go);
    return li;
  }

  async function refresh() {
    if (busy || document.hidden) return;
    busy = true;
    try {
      const [nodes, phones, registry] = await Promise.all([json('/nodes'), json('/phones'),
        json('/node/devices').catch(() => ({devices: []}))]);
      // Retired registrations on the center are not phones.
      const retired = new Set(registry.devices.filter(device => !device.enabled).map(device => device.device_id));
      const local = nodes.local;
      const names = new Map([[local.node_id, local.host?.hostname || '中心'], ...nodes.items.map(entry => [entry.node_id, entry.name])]);
      const items = phones.items.filter(item => !(item.node_id === local.node_id && retired.has(item.device_id)));
      const online = items.filter(item => HGPhoneState.of(item).online).length;
      $('all-phones-count').textContent = `${items.length} 台 · ${online} 台在线`;
      $('all-phones-list').replaceChildren(...items.map(item => row(item, names.get(item.node_id) || item.node_name)));
      $('all-phones-message').textContent = items.length ? '' : '还没有手机。';
    } catch (error) {
      $('all-phones-message').textContent = `读取失败：${error.message}`;
    } finally { busy = false; }
  }
  $('refresh-fleet').addEventListener('click', refresh);
  document.addEventListener('visibilitychange', refresh);
  setInterval(refresh, 30000);
  refresh();
})();
