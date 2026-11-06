/* 结构: this center, the computers that joined it, their phones and the agents, by network zone. A computer or an
   agent is in a zone when it carries a `zone:<name>` tag; a phone is where its computer is. Cards and zone labels drag
   (a focused one moves with the arrow keys; on a touch screen, after a long press); the positions stay in this
   browser, one set for wide screens and one for narrow. Only what is running glows: a computer with active jobs, a
   phone an active job is bound to, an agent that started one. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id), API = '/dash/api', SVG = 'http://www.w3.org/2000/svg';
  const ZONE_TAG = 'zone:', NO_ZONE = '未标区域', NARROW = 720, AUTO_MS = 15000, STORE = 'hgamey-structure:';
  const PLATFORMS = {Darwin: 'macOS', Linux: 'Linux', Windows: 'Windows'};
  // Wide screens: agents | 主节点 | 从节点 | phones, one band per zone. Narrow: zones stacked, one card per row.
  const WIDE = {margin: 16, pad: 20, head: 48, gap: 12, column: 76, group: 22, lane: 28,
                agent: [228, 54], center: [248, 100], node: [248, 100], phone: [268, 54]};
  const SMALL = {margin: 8, pad: 12, head: 44, gap: 8, gutter: 18, group: 16, lane: 16, agent: 50, node: 96, phone: 50};
  const ICONS = {
    center: '<rect x="3.5" y="4" width="17" height="6.5" rx="1.6"/><rect x="3.5" y="13.5" width="17" height="6.5" rx="1.6"/><path d="M7 7.25h.01M7 16.75h.01M10.5 7.25h6M10.5 16.75h6"/>',
    node: '<rect x="3" y="4.5" width="18" height="11.5" rx="1.6"/><path d="M8.5 20h7M12 16v4"/>',
    phone: '<rect x="6.5" y="2.5" width="11" height="19" rx="2.4"/><path d="M10.75 18.5h2.5"/>',
  };

  let data = null, mode = null, frame = null, links = [], moved = {}, timer = 0, busy = false, drawnLinks = 0, dragged = null;
  const cards = new Map(), labels = new Map(), rects = new Map(), paths = new Map();

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = String(text);
    return node;
  }
  function icon(name) {
    const node = document.createElementNS(SVG, 'svg');
    node.setAttribute('viewBox', '0 0 24 24');
    node.setAttribute('class', 'st-icon');
    node.setAttribute('aria-hidden', 'true');
    node.innerHTML = ICONS[name];
    return node;
  }
  const svg = (tag, attrs) => {
    const node = document.createElementNS(SVG, tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    return node;
  };
  const when = at => new Date(at * 1000).toLocaleString('zh-CN', {month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false});
  function ago(at) {
    if (!at) return '未签到';
    const s = Math.max(0, Date.now() / 1000 - at);
    if (s < 60) return '刚刚签到';
    if (s < 3600) return `${Math.floor(s / 60)} 分钟前签到`;
    if (s < 86400) return `${Math.floor(s / 3600)} 小时前签到`;
    return `${Math.floor(s / 86400)} 天前签到`;
  }
  const os = text => text && text.replace(/\s*\(SDK \d+\)$/, '').replace(/^OpenHarmony-(\d+\.\d+).*$/, 'OpenHarmony $1');
  const zoneOf = tags => (tags.find(tag => tag.startsWith(ZONE_TAG)) || ZONE_TAG + NO_ZONE).slice(ZONE_TAG.length);

  async function json(path) {
    const response = await HGTarget.fetch(API + path, {credentials: 'same-origin', cache: 'no-store'});
    if (response.ok) return response.json();
    const body = await response.json().catch(() => null), error = new Error(body?.error || `HTTP ${response.status}`);
    error.status = response.status;
    throw error;
  }

  // ── What is where ──
  function model(nodes, phones, agents, jobs) {
    const active = jobs.items.filter(HGJobState.active), local = nodes.local;
    const on = (nodeId, also = () => true) => active.filter(job => job.execution_node_id === nodeId && also(job)).length;
    const phone = item => {
      const seen = item.observation || {}, read = seen.online != null;
      return {id: 'p:' + item.ref, kind: 'phone', title: seen.brand && seen.model ? `${seen.brand} ${seen.model}` : item.model || item.device_id,
              facts: read ? [os(seen.os), seen.root ? 'root' : '无 root', seen.transport].filter(Boolean) : ['无读数'],
              online: item.connected && seen.online === true, serial: item.serial,
              jobs: on(item.node_id, job => job.device_binding?.device_id === item.device_id)};
    };
    const phonesOf = nodeId => phones.items.filter(item => item.node_id === nodeId).map(phone);
    const center = {id: 'n:' + local.node_id, kind: 'node', role: 'center', title: local.host.hostname, host: null,
                    platform: PLATFORMS[local.host.platform] || local.host.platform, revision: local.service.revision,
                    online: true, seen: null, zone: zoneOf(local.tags || []), jobs: on(local.node_id), phones: phonesOf(local.node_id)};
    const joined = nodes.items.map(entry => {
      const identity = entry.snapshot?.identity;
      return {id: 'n:' + entry.node_id, kind: 'node', role: 'node', title: entry.name, host: identity?.host.hostname,
              platform: identity ? PLATFORMS[identity.host.platform] || identity.host.platform : null,
              revision: identity?.service.revision, online: entry.connection.status === 'ready',
              seen: entry.connection.last_seen_at, zone: zoneOf(entry.tags), jobs: on(entry.node_id), phones: phonesOf(entry.node_id)};
    });
    const people = agents.agents.map(agent => ({id: 'a:' + agent.name, kind: 'agent', title: agent.name,
      tags: agent.tags.filter(tag => !tag.startsWith(ZONE_TAG)), checkin: agent.checkin?.at, enabled: agent.enabled,
      zone: zoneOf(agent.tags), jobs: active.filter(job => job.actor_id === agent.name).length}));
    const all = [center, ...joined];
    const rank = name => name === center.zone ? 0 : name === NO_ZONE ? 2 : 1;
    const names = [...new Set([center.zone, ...joined.map(node => node.zone), ...people.map(agent => agent.zone)])]
      .sort((a, b) => rank(a) - rank(b) || a.localeCompare(b, 'zh-CN'));
    let tone = 0;
    const zones = names.map(name => ({name, tone: name === NO_ZONE ? null : tone++ % 4,
      agents: people.filter(agent => agent.zone === name), nodes: all.filter(node => node.zone === name)}));
    return {center, nodes: all, agents: people, zones, jobs: active.length};
  }
  const members = zone => [...zone.agents.map(agent => agent.id), ...zone.nodes.flatMap(node => [node.id, ...node.phones.map(phone => phone.id)])];

  // ── Cards ──
  function nodeCard(node, card) {
    const center = node.role === 'center', role = center ? '主节点' : '从节点';
    card.className = `st-card st-node${center ? ' st-center' : ''}${node.online ? '' : ' is-off'}${node.jobs ? ' is-running' : ''}`;
    const head = el('div', 'st-head'), line = el('div', 'st-line', [node.platform, node.host].filter(Boolean).join(' · ') || '未上报');
    head.append(icon(center ? 'center' : 'node'), el('strong', 'st-title', node.title), el('span', 'st-role', role));
    line.title = line.textContent;
    const status = node.online ? (node.jobs ? `运行 ${node.jobs} 个作业` : '在线') : `离线${node.seen ? ' · 最后在线 ' + when(node.seen) : ''}`;
    const state = el('div', 'st-state'), revision = el('span', 'st-revision', node.revision ? node.revision.slice(0, 7) : '');
    if (node.revision) revision.title = node.revision;
    state.append(el('i', 'st-dot'), el('span', 'st-status', status), revision);
    card.replaceChildren(head, line, state);
    card.setAttribute('aria-label', [node.title, role, line.textContent, status, `${node.phones.length} 台手机`].join('，'));
  }
  function phoneCard(phone, card) {
    card.className = `st-card st-phone${phone.online ? '' : ' is-off'}${phone.jobs ? ' is-running' : ''}`;
    const text = el('div', 'st-text'), busy = phone.jobs ? `运行 ${phone.jobs} 个作业` : null;
    text.append(el('strong', 'st-title', phone.title), el('span', 'st-line', phone.facts.join(' · ')));
    card.title = phone.serial;
    card.replaceChildren(icon('phone'), text, busy ? el('span', 'st-busy', '运行中') : el('i', 'st-dot'));
    card.setAttribute('aria-label', [phone.title, ...phone.facts, busy || (phone.online ? '在线' : '离线')].join('，'));
  }
  function agentCard(agent, card) {
    card.className = `st-card st-agent${agent.enabled ? '' : ' is-off'}${agent.jobs ? ' is-running' : ''}`;
    const text = el('div', 'st-text');
    const facts = [...agent.tags, agent.enabled ? (agent.jobs ? `运行 ${agent.jobs} 个作业` : ago(agent.checkin)) : '已停用'];
    text.append(el('strong', 'st-title', agent.title), el('span', 'st-line', facts.join(' · ')));
    card.replaceChildren(el('span', 'st-avatar', agent.title.slice(0, 1).toUpperCase()), text);
    card.setAttribute('aria-label', ['Agent ' + agent.title, ...facts].join('，'));
  }
  function zoneLabel(zone, label) {
    const phones = zone.nodes.reduce((sum, node) => sum + node.phones.length, 0);
    label.className = 'st-zone-label' + (zone.tone == null ? ' is-none' : ` st-tone-${zone.tone}`);
    const counts = [zone.nodes.length && `${zone.nodes.length} 个节点`, phones && `${phones} 台手机`, zone.agents.length && `${zone.agents.length} 个 Agent`].filter(Boolean);
    label.replaceChildren(el('i', 'st-swatch'), el('strong', null, zone.name), el('span', null, counts.join(' · ')));
    label.setAttribute('aria-label', `${zone.name}，${counts.join('，')}`);
  }

  function render() {
    const world = $('world'), seen = new Set(), zones = new Set();
    const put = (item, paint) => {
      let card = cards.get(item.id);
      if (!card) {
        card = el('div');
        card.dataset.id = item.id;
        card.tabIndex = 0;
        world.append(card);
        cards.set(item.id, card);
      }
      paint(item, card);
      card.classList.toggle('is-dragging', card === dragged);
      seen.add(item.id);
    };
    for (const zone of data.zones) {
      let label = labels.get(zone.name), rect = rects.get(zone.name);
      if (!label) {
        label = el('div');
        label.dataset.zone = zone.name;
        label.tabIndex = 0;
        world.insertBefore(label, $('links'));
        labels.set(zone.name, label);
        rect = svg('rect', {rx: 18, 'data-zone': zone.name});
        $('zones').append(rect);
        rects.set(zone.name, rect);
      }
      zoneLabel(zone, label);
      label.classList.toggle('is-dragging', label === dragged);
      rect.setAttribute('class', 'st-zone' + (zone.tone == null ? ' is-none' : ` st-tone-${zone.tone}`));
      zones.add(zone.name);
      for (const agent of zone.agents) put(agent, agentCard);
      for (const node of zone.nodes) {
        put(node, nodeCard);
        for (const phone of node.phones) put(phone, phoneCard);
      }
    }
    for (const [id, card] of cards) if (!seen.has(id)) { card.remove(); cards.delete(id); }
    for (const [name, label] of labels) if (!zones.has(name)) { label.remove(); rects.get(name).remove(); labels.delete(name); rects.delete(name); }

    const center = data.center.id;
    links = [...data.agents.map(agent => ({from: agent.id, to: center, running: agent.jobs > 0, off: !agent.enabled})),
             ...data.nodes.filter(node => node.role === 'node').map(node => ({from: center, to: node.id, running: node.jobs > 0, off: !node.online, trunk: true})),
             ...data.nodes.flatMap(node => node.phones.map(phone => ({from: node.id, to: phone.id, running: phone.jobs > 0, off: !phone.online})))];
    // A link keeps its elements across reads, so a packet already travelling is not restarted; running ones sit on top.
    const kept = new Set();
    for (const link of links) {
      const name = link.from + '>' + link.to;
      let drawn = paths.get(name);
      if (!drawn) {
        drawn = {path: svg('path', {id: 'st-link-' + ++drawnLinks}), packet: null};
        paths.set(name, drawn);
      }
      drawn.path.setAttribute('class', 'st-link' + (link.trunk ? ' is-trunk' : '') + (link.off ? ' is-off' : '') + (link.running ? ' is-running' : ''));
      if (link.running && !drawn.packet) {
        const motion = svg('animateMotion', {dur: '2.4s', repeatCount: 'indefinite'});
        motion.append(svg('mpath', {href: '#' + drawn.path.id}));
        drawn.packet = svg('circle', {r: 3.2, class: 'st-packet'});
        drawn.packet.append(motion);
      } else if (!link.running && drawn.packet) {
        drawn.packet.remove();
        drawn.packet = null;
      }
      const layer = $(link.running ? 'links-busy' : 'links-quiet');
      if (drawn.path.parentNode !== layer) layer.append(drawn.path);
      if (drawn.packet && drawn.packet.parentNode !== layer) layer.append(drawn.packet);
      link.path = drawn.path;
      kept.add(name);
    }
    for (const [name, drawn] of paths) if (!kept.has(name)) { drawn.path.remove(); drawn.packet?.remove(); paths.delete(name); }

    const phones = data.nodes.reduce((sum, node) => sum + node.phones.length, 0), named = data.zones.filter(zone => zone.tone != null).length;
    const facts = [`${data.nodes.length} 个节点`, `${phones} 台手机`, `${data.agents.length} 个 Agent`,
      named && `${named} 个网络区域`, data.jobs && `${data.jobs} 个作业在运行`].filter(Boolean);
    $('facts').replaceChildren(...facts.flatMap((fact, i) => [i ? ' · ' : '', el('span', null, fact)]));
    $('stage').hidden = false;
    place();
  }

  // ── Layout: where each card starts (x, y, width, height); what the viewer moved replaces x and y ──
  function wide(model) {
    const P = WIDE, x = {agent: P.pad}, boxes = new Map();
    x.center = x.agent + P.agent[0] + P.column;
    x.node = x.center + P.center[0] + P.column;
    x.phone = x.node + P.node[0] + P.column;
    let y = 0;
    for (const zone of model.zones) {
      const top = y + P.head;
      let agents = top, nodes = top;
      for (const agent of zone.agents) { boxes.set(agent.id, [x.agent, agents, ...P.agent]); agents += P.agent[1] + P.gap; }
      for (const node of zone.nodes) {
        const [width, height] = P[node.role], stack = node.phones.length * (P.phone[1] + P.gap) - P.gap, block = Math.max(height, stack);
        boxes.set(node.id, [x[node.role], nodes + (block - height) / 2, width, height]);
        node.phones.forEach((phone, i) => boxes.set(phone.id, [x.phone, nodes + (block - stack) / 2 + i * (P.phone[1] + P.gap), ...P.phone]));
        nodes += block + P.group;
      }
      y = Math.max(agents - P.gap, nodes - P.group, top) + P.pad + P.lane;
    }
    return {boxes, width: x.phone + P.phone[0] + P.pad};
  }
  function narrow(model, width) {
    const P = SMALL, boxes = new Map(), full = width - 2 * P.pad, indent = P.pad + P.gutter, branch = full - P.gutter;
    let y = 0;
    for (const zone of model.zones) {
      let at = y + P.head;
      for (const agent of zone.agents) { boxes.set(agent.id, [P.pad, at, full - P.gutter, P.agent]); at += P.agent + P.gap; }
      if (zone.agents.length) at += P.group - P.gap;
      for (const node of zone.nodes) {
        boxes.set(node.id, [indent, at, branch, P.node]);
        at += P.node + P.gap;
        for (const phone of node.phones) { boxes.set(phone.id, [indent + P.gutter, at, branch - P.gutter, P.phone]); at += P.phone + P.gap; }
        at += P.group - P.gap;
      }
      y = at - P.group + P.pad + P.lane;
    }
    return {boxes, width};
  }
  function place() {
    const outer = $('stage').clientWidth, next = outer < NARROW ? 'narrow' : 'wide', P = next === 'wide' ? WIDE : SMALL;
    if (next !== mode) { mode = next; moved = load(); $('relayout').disabled = !Object.keys(moved).length; }
    const width = outer - 2 * P.margin, auto = mode === 'wide' ? wide(data) : narrow(data, width), scale = Math.min(1, width / auto.width);
    frame = {boxes: auto.boxes, scale, width: width / scale, offset: (width / scale - auto.width) / 2, P};
    $('world').style.left = $('world').style.top = P.margin + 'px';
    for (const [id, card] of cards) {
      const [, , w, h] = frame.boxes.get(id);
      card.style.width = w + 'px';
      card.style.height = h + 'px';
    }
    draw();
  }
  // A card placed on a wider screen stays inside a narrower one.
  function box(id) {
    const [x, y, w, h] = frame.boxes.get(id), at = moved[id];
    return at ? [Math.min(Math.max(at[0] + frame.offset, frame.P.pad), frame.width - frame.P.pad - w), at[1], w, h] : [x + frame.offset, y, w, h];
  }
  function bounds(ids) {
    const all = ids.map(box);
    return [Math.min(...all.map(b => b[0])), Math.min(...all.map(b => b[1])), Math.max(...all.map(b => b[0] + b[2])), Math.max(...all.map(b => b[1] + b[3]))];
  }
  // Facing edges, joined by a curve that leaves and arrives square to them; wide screens read left to right, narrow
  // ones top to bottom. On a narrow screen computers hang off a rail in the left gutter and phones off their computer,
  // like a tree, while the cards stay where the rail can reach them.
  function rail(x, y0, y1, x0, x1) {
    const s = Math.sign(y1 - y0) || 1, r = Math.min(8, Math.abs(y1 - y0) / 2), t = Math.sign(x1 - x) || 1;
    return `M${x0} ${y0} H${x + (Math.sign(x0 - x) || 1) * r} Q${x} ${y0} ${x} ${y0 + s * r} V${y1 - s * r} Q${x} ${y1} ${x + t * r} ${y1} H${x1}`;
  }
  function curve(a, b, link) {
    const ax = a[0] + a[2] / 2, ay = a[1] + a[3] / 2, bx = b[0] + b[2] / 2, by = b[1] + b[3] / 2, gutter = frame.P.gutter;
    if (mode === 'narrow' && link.trunk) {
      const x = Math.min(a[0], b[0]) - gutter / 2;
      if (x > 0) return rail(x, ay, by, a[0], b[0]);
    }
    if (mode === 'narrow' && link.to.startsWith('p:') && by > a[1] + a[3] + 8 && b[0] > a[0] + gutter / 2 + 8) {
      const x = a[0] + gutter / 2, r = Math.min(8, (by - a[1] - a[3]) / 2);
      return `M${x} ${a[1] + a[3]} V${by - r} Q${x} ${by} ${x + r} ${by} H${b[0]}`;
    }
    if (mode === 'narrow' && link.from.startsWith('a:')) {   // agents share a rail in the right gutter down to the center
      const x = a[0] + a[2] + gutter / 2, down = by > ay, end = down ? b[1] : b[1] + b[3];
      if (x > b[0] + 4 && x < b[0] + b[2] - 4 && Math.abs(end - ay) > a[3] / 2) {
        const r = Math.min(8, Math.abs(end - ay) / 2);
        return `M${a[0] + a[2]} ${ay} H${x - r} Q${x} ${ay} ${x} ${ay + (down ? r : -r)} V${end}`;
      }
    }
    const apartX = Math.abs(bx - ax) > (a[2] + b[2]) / 2 + 8, apartY = Math.abs(by - ay) > (a[3] + b[3]) / 2 + 8;
    if (mode === 'wide' ? apartX || !apartY : apartX && !apartY) {
      const s = Math.sign(bx - ax) || 1, x1 = ax + s * a[2] / 2, x2 = bx - s * b[2] / 2, k = Math.max(28, Math.abs(x2 - x1) / 2);
      return `M${x1} ${ay} C${x1 + s * k} ${ay} ${x2 - s * k} ${by} ${x2} ${by}`;
    }
    const s = Math.sign(by - ay) || 1, y1 = ay + s * a[3] / 2, y2 = by - s * b[3] / 2, k = Math.max(20, Math.abs(y2 - y1) / 2);
    return `M${ax} ${y1} C${ax} ${y1 + s * k} ${bx} ${y2 - s * k} ${bx} ${y2}`;
  }
  function draw() {
    const P = frame.P;
    let bottom = 0;
    for (const [id, card] of cards) {
      const b = box(id);
      card.style.transform = `translate(${b[0]}px, ${b[1]}px)`;
      bottom = Math.max(bottom, b[1] + b[3]);
    }
    for (const zone of data.zones) {
      const [x1, y1, x2, y2] = bounds(members(zone)), rect = rects.get(zone.name);
      rect.setAttribute('x', x1 - P.pad);
      rect.setAttribute('y', y1 - P.head);
      rect.setAttribute('width', x2 - x1 + 2 * P.pad);
      rect.setAttribute('height', y2 - y1 + P.head + P.pad);
      labels.get(zone.name).style.transform = `translate(${x1 - P.pad + 14}px, ${y1 - P.head + 12}px)`;
      bottom = Math.max(bottom, y2 + P.pad);
    }
    for (const link of links) link.path.setAttribute('d', curve(box(link.from), box(link.to), link));
    const world = $('world');
    world.style.width = frame.width + 'px';
    world.style.height = bottom + 'px';
    world.style.transform = frame.scale === 1 ? '' : `scale(${frame.scale})`;
    for (const layer of [$('zones'), $('links')]) { layer.setAttribute('width', frame.width); layer.setAttribute('height', bottom); }
    $('stage').style.height = bottom * frame.scale + 2 * frame.P.margin + 'px';
  }

  // ── Moving cards and zones ──
  const key = () => STORE + mode;
  function load() {
    try { return JSON.parse(localStorage.getItem(key()) || '{}'); } catch (_) { return {}; }
  }
  function save() {
    try { localStorage.setItem(key(), JSON.stringify(moved)); } catch (_) { /* private mode: the layout lasts this visit */ }
    $('relayout').disabled = !Object.keys(moved).length;
  }
  function handleIds(handle) {
    if (handle.dataset.id) return [handle.dataset.id];
    return members(data.zones.find(zone => zone.name === handle.dataset.zone));
  }
  // Move `ids` from where they `start`ed by (dx, dy), keeping the group whole and inside the stage.
  function shift(ids, start, dx, dy) {
    const P = frame.P, [x1, y1, x2] = start.bounds;
    dx = Math.min(Math.max(dx, P.pad - x1), frame.width - P.pad - x2);
    dy = Math.max(dy, P.head - y1);
    for (const id of ids) {
      const [x, y] = start.boxes.get(id);
      moved[id] = [Math.round(x + dx - frame.offset), Math.round(y + dy)];
    }
  }
  const snapshot = ids => ({boxes: new Map(ids.map(id => [id, box(id)])), bounds: bounds(ids)});
  let pending = 0;
  const redraw = () => { if (!pending) pending = requestAnimationFrame(() => { pending = 0; draw(); }); };

  let held = null;   // a touch drag after a long press: page scrolling stops while it lasts
  $('world').addEventListener('pointerdown', event => {
    const handle = event.target.closest('.st-card, .st-zone-label');
    if (!handle || event.button !== 0) return;
    const ids = handleIds(handle), start = snapshot(ids), sx = event.clientX, sy = event.clientY, touch = event.pointerType === 'touch';
    let dragging = false, press = 0;
    const begin = () => { dragging = true; dragged = handle; handle.classList.add('is-dragging'); if (touch) held = handle; };
    const move = e => {
      const dx = e.clientX - sx, dy = e.clientY - sy;
      if (!dragging) {
        if (touch || Math.hypot(dx, dy) < 4) { if (touch && Math.hypot(dx, dy) > 8) finish(); return; }
        begin();
      }
      shift(ids, start, dx / frame.scale, dy / frame.scale);
      redraw();
    };
    const finish = () => {
      clearTimeout(press);
      handle.removeEventListener('pointermove', move);
      handle.removeEventListener('pointerup', finish);
      handle.removeEventListener('pointercancel', finish);
      if (handle.hasPointerCapture(event.pointerId)) handle.releasePointerCapture(event.pointerId);
      held = dragged = null;
      if (dragging) { handle.classList.remove('is-dragging'); save(); }
    };
    handle.setPointerCapture(event.pointerId);
    handle.focus({preventScroll: true, focusVisible: false});   // arrow keys work next; no focus ring after a drag
    handle.addEventListener('pointermove', move);
    handle.addEventListener('pointerup', finish);
    handle.addEventListener('pointercancel', finish);
    if (touch) press = setTimeout(begin, 320);
    else event.preventDefault();
  });
  $('world').addEventListener('touchmove', event => { if (held) event.preventDefault(); }, {passive: false});
  $('world').addEventListener('keydown', event => {
    const handle = event.target.closest('.st-card, .st-zone-label'), step = event.shiftKey ? 40 : 8;
    const delta = {ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step]}[event.key];
    if (!handle || !delta) return;
    event.preventDefault();
    const ids = handleIds(handle);
    shift(ids, snapshot(ids), ...delta);
    draw();
    save();
  });
  $('relayout').addEventListener('click', () => { moved = {}; save(); draw(); });

  // ── Reading ──
  function message(text, login) {
    const box = $('message');
    box.replaceChildren(text);
    if (login) {
      const link = el('a', null, '重新登录');
      link.href = '/dash/login';
      box.append(' ', link);
    }
    box.hidden = !text;
  }
  async function refresh() {
    if (busy) return;
    busy = true;
    clearTimeout(timer);
    $('refresh').disabled = true;
    try {
      const [nodes, phones, agents, jobs] = await Promise.all([json('/nodes'), json('/phones'), json('/agents'), json('/jobs?state=running&limit=200')]);
      data = model(nodes, phones, agents, jobs);
      render();
      message('');
    } catch (error) {
      message(`读取失败：${error.message}${data ? '。下面是上次读到的结构。' : ''}`, error.status === 401);
    } finally {
      busy = false;
      $('refresh').disabled = false;
      if (!document.hidden) timer = setTimeout(refresh, AUTO_MS);
    }
  }
  $('refresh').addEventListener('click', refresh);
  document.addEventListener('visibilitychange', () => { if (document.hidden) clearTimeout(timer); else refresh(); });
  addEventListener('resize', () => { if (data) place(); });
  refresh();
})();
