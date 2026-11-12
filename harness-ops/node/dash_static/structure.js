/* 结构: this center, the computers that joined it, their phones and the agents, by network zone. A computer or an
   agent is in a zone when it carries a `zone:<name>` tag; a phone is where its computer is. What is shown as online is
   what the center sees: a computer's link to it, and for a phone that link plus how old its last reading is (a reading
   a computer reported long ago says nothing about now). Registrations the center has disabled are not phones. Cards and zone labels drag
   (a focused one moves with the arrow keys; on a touch screen, after a long press); the positions stay in this
   browser, one set for wide screens and one for narrow. Only what is running glows: a computer with active jobs, a
   phone an active job is bound to, an agent that started one. A computer's or an agent's zone is changed from its card
   (with the steer password): only its zone: tag changes, at the revision it was read at. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id), API = '/dash/api', SVG = 'http://www.w3.org/2000/svg';
  const ZONE_TAG = 'zone:', NO_ZONE = '未标区域', NARROW = 720, AUTO_MS = 15000, STORE = 'hgamey-structure:';
  const PLATFORMS = {Darwin: 'macOS', Linux: 'Linux', Windows: 'Windows'};
  // Wide screens: agents | 主节点 | 从节点 | phones, one band per zone. Narrow: zones stacked, one card per row.
  const WIDE = {margin: 16, pad: 20, head: 48, gap: 12, column: 76, group: 22, lane: 28,
                agent: [228, 54], center: [248, 100], node: [248, 100], phone: [292, 54]};
  const SMALL = {least: 300, margin: 8, pad: 12, head: 44, gap: 8, gutter: 18, group: 16, lane: 16, agent: 50, node: 96, phone: 50};
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
  const ago = at => at ? HGPhoneState.age(at) + '签到' : '未签到';
  const zoneOf = tags => (tags.find(tag => tag.startsWith(ZONE_TAG)) || ZONE_TAG + NO_ZONE).slice(ZONE_TAG.length);

  async function json(path) {
    const response = await HGTarget.fetch(API + path, {credentials: 'same-origin', cache: 'no-store'});
    if (response.ok) return response.json();
    const body = await response.json().catch(() => null), error = new Error(body?.error || `HTTP ${response.status}`);
    error.status = response.status;
    throw error;
  }

  // ── What is where ──
  function model(nodes, phones, agents, jobs, registry) {
    const active = jobs.items.filter(HGJobState.active), local = nodes.local;
    const disabled = new Set(registry.devices.filter(device => !device.enabled).map(device => device.device_id));
    const on = (nodeId, also = () => true) => active.filter(job => job.execution_node_id === nodeId && also(job)).length;
    const phone = item => {
      const seen = HGPhoneState.of(item);
      return {...seen, id: 'p:' + item.ref, kind: 'phone', serial: item.serial,
              facts: seen.seen ? [seen.os, seen.root ? 'root' : '无 root', seen.transport].filter(Boolean) : ['暂无读数'],
              jobs: on(item.node_id, job => job.device_binding?.device_id === item.device_id)};
    };
    const phonesOf = nodeId => phones.items.filter(item => item.node_id === nodeId && !(nodeId === local.node_id && disabled.has(item.device_id))).map(phone);
    const center = {id: 'n:' + local.node_id, kind: 'node', role: 'center', title: local.host.hostname, host: null,
                    platform: PLATFORMS[local.host.platform] || local.host.platform, revision: local.service.revision,
                    online: true, seen: null, zone: zoneOf(local.tags || []), jobs: on(local.node_id), phones: phonesOf(local.node_id),
                    edit: {path: '/nodes/local', revision: local.revision, tags: local.tags || []}};
    const joined = nodes.items.map(entry => {
      const identity = entry.snapshot?.identity;
      return {id: 'n:' + entry.node_id, kind: 'node', role: 'node', title: entry.name, host: identity?.host.hostname,
              platform: identity ? PLATFORMS[identity.host.platform] || identity.host.platform : null,
              revision: identity?.service.revision, online: entry.connection.status === 'ready',
              seen: entry.connection.last_seen_at, zone: zoneOf(entry.tags), jobs: on(entry.node_id), phones: phonesOf(entry.node_id),
              edit: {path: '/nodes/' + encodeURIComponent(entry.name), revision: entry.revision, tags: entry.tags}};
    });
    const people = agents.agents.map(agent => ({id: 'a:' + agent.name, kind: 'agent', title: agent.name,
      tags: agent.tags.filter(tag => !tag.startsWith(ZONE_TAG)), checkin: agent.checkin?.at, enabled: agent.enabled,
      zone: zoneOf(agent.tags), jobs: active.filter(job => job.actor_id === agent.name).length,
      edit: {path: '/agents/' + encodeURIComponent(agent.name), revision: agent.revision, tags: agent.tags}}));
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
  // A computer's or an agent's way to its zone; pressing it never starts a drag.
  function zoneButton(item) {
    const button = el('button', 'st-zone-btn');
    button.type = 'button'; button.dataset.edit = item.id; button.title = `修改区域（现在：${item.zone}）`;
    button.setAttribute('aria-label', `修改 ${item.title} 的区域，现在是${item.zone}`);
    button.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20h4L19 9l-4-4L4 16v4zM13.5 6.5l4 4"/></svg>';
    return button;
  }
  function nodeCard(node, card) {
    const center = node.role === 'center', role = center ? '主节点' : '从节点';
    card.className = `st-card st-node${center ? ' st-center' : ''}${node.online ? '' : ' is-off'}${node.jobs ? ' is-running' : ''}`;
    const head = el('div', 'st-head'), line = el('div', 'st-line', [node.platform, node.host].filter(Boolean).join(' · ') || '未上报');
    head.append(icon(center ? 'center' : 'node'), el('strong', 'st-title', node.title), el('span', 'st-role', role), zoneButton(node));
    line.title = line.textContent;
    const status = node.online ? (node.jobs ? `运行 ${node.jobs} 个作业` : '在线') : `离线${node.seen ? ' · 最后在线 ' + when(node.seen) : ''}`;
    const state = el('div', 'st-state'), revision = el('span', 'st-revision tech', node.revision ? node.revision.slice(0, 7) : '');
    if (node.revision) revision.title = node.revision;
    state.append(el('i', 'st-dot'), el('span', 'st-status', status), revision);
    card.replaceChildren(head, line, state);
    card.setAttribute('aria-label', [node.title, role, line.textContent, status, `${node.phones.length} 台手机`].join('，'));
  }
  function phoneCard(phone, card) {
    card.className = `st-card st-phone${phone.online ? '' : ' is-off'}${phone.jobs ? ' is-running' : ''}`;
    const text = el('div', 'st-text'), head = el('div', 'st-phone-head'), busy = phone.jobs ? `运行 ${phone.jobs} 个作业` : null;
    // Beside the name: why it is not shown as online, or how old a reading is that is not current.
    const note = HGPhoneState.note(phone);
    head.append(el('strong', 'st-title', phone.title));
    if (note) { const tip = el('span', 'st-note', note); tip.title = note; head.append(tip); }
    text.append(head, el('span', 'st-line', phone.facts.join(' · ')));
    card.title = phone.serial;
    card.replaceChildren(icon('phone'), text, busy ? el('span', 'st-busy', '运行中') : el('i', 'st-dot'));
    card.setAttribute('aria-label', [phone.title, ...phone.facts, note, busy || (phone.online ? '在线' : '')].filter(Boolean).join('，'));
  }
  function agentCard(agent, card) {
    card.className = `st-card st-agent${agent.enabled ? '' : ' is-off'}${agent.jobs ? ' is-running' : ''}`;
    const text = el('div', 'st-text');
    const facts = [...agent.tags, agent.enabled ? (agent.jobs ? `运行 ${agent.jobs} 个作业` : ago(agent.checkin)) : '已停用'];
    text.append(el('strong', 'st-title', agent.title), el('span', 'st-line', facts.join(' · ')));
    card.replaceChildren(el('span', 'st-avatar', agent.title.slice(0, 1).toUpperCase()), text, zoneButton(agent));
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

    // How many are offline, right after the totals: a mostly offline map reads as one computer down, not the platform.
    const phones = data.nodes.flatMap(node => node.phones), named = data.zones.filter(zone => zone.tone != null).length;
    const nodesOff = data.nodes.filter(node => !node.online).length, phonesOff = phones.filter(phone => !phone.online).length;
    const facts = [`${data.nodes.length} 个节点${nodesOff ? `（${nodesOff} 个离线）` : ''}`, `${phones.length} 台手机${phonesOff ? `（${phonesOff} 台离线）` : ''}`, `${data.agents.length} 个 Agent`,
      named && `${named} 个网络区域`, data.jobs && `${data.jobs} 个作业在运行`].filter(Boolean);
    $('facts').replaceChildren(...facts.flatMap((fact, i) => [i ? ' · ' : '', el('span', null, fact)]));
    $('stage').hidden = false;
    place();
  }

  // ── Layout: where each card starts (x, y, width, height); what the viewer moved replaces x and y ──
  // Each zone takes the columns its cards use (agents 0, 主节点 1, 从节点 2, phones 3) and drops to the first place
  // below what already fills those columns: a zone of agents alone sits beside the computers of another.
  function wide(model) {
    const P = WIDE, x = [P.pad], boxes = new Map(), filled = [0, 0, 0, 0];
    x[1] = x[0] + P.agent[0] + P.column;
    x[2] = x[1] + P.center[0] + P.column;
    x[3] = x[2] + P.node[0] + P.column;
    for (const zone of model.zones) {
      const used = [...zone.agents.length ? [0] : [], ...zone.nodes.map(node => node.role === 'center' ? 1 : 2),
                    ...zone.nodes.some(node => node.phones.length) ? [3] : []];
      const first = Math.min(...used), last = Math.max(...used);
      const top = Math.max(...filled.slice(first, last + 1)) + P.head;
      let agents = top, nodes = top;
      for (const agent of zone.agents) { boxes.set(agent.id, [x[0], agents, ...P.agent]); agents += P.agent[1] + P.gap; }
      for (const node of zone.nodes) {
        const [width, height] = P[node.role], stack = node.phones.length * (P.phone[1] + P.gap) - P.gap, block = Math.max(height, stack);
        boxes.set(node.id, [x[node.role === 'center' ? 1 : 2], nodes + (block - height) / 2, width, height]);
        node.phones.forEach((phone, i) => boxes.set(phone.id, [x[3], nodes + (block - stack) / 2 + i * (P.phone[1] + P.gap), ...P.phone]));
        nodes += block + P.group;
      }
      const bottom = Math.max(agents - P.gap, nodes - P.group, top) + P.pad + P.lane;
      for (let column = first; column <= last; ++column) filled[column] = bottom;
    }
    return {boxes, width: x[3] + P.phone[0] + P.pad};
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
  // Below SMALL.least a narrow layout is drawn at that width and scaled down. A stage with no width (a window resized to
  // nothing) is laid out on the next resize.
  function place() {
    const outer = $('stage').clientWidth, next = outer < NARROW ? 'narrow' : 'wide', P = next === 'wide' ? WIDE : SMALL;
    if (outer <= 2 * P.margin) return;
    if (next !== mode) { mode = next; moved = load(); $('relayout').disabled = !Object.keys(moved).length; }
    const width = outer - 2 * P.margin, auto = mode === 'wide' ? wide(data) : narrow(data, Math.max(width, P.least)), scale = Math.min(1, width / auto.width);
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
    if (!handle || event.button !== 0 || event.target.closest('.st-zone-btn')) return;
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
  let again = false;
  async function refresh() {
    if (busy) { again = true; return; }
    again = false;
    busy = true;
    clearTimeout(timer);
    $('refresh').disabled = true;
    try {
      const [nodes, phones, listed, jobs, registry] = await Promise.all([json('/nodes'), json('/phones'), json('/agents'),
        json('/jobs?state=running&limit=200'), json('/node/devices')]);
      // Archived agents (nothing for three days, archived by hand, disabled) are drawn only when asked for.
      const folded = listed.agents.filter(agent => agent.archived).length;
      $('archived-agents').hidden = !folded;
      $('archived-agents').textContent = `${showArchived ? '收起' : '显示'}已归档的 ${folded} 个 Agent`;
      data = model(nodes, phones, {...listed, agents: listed.agents.filter(agent => showArchived || !agent.archived)}, jobs, registry);
      render();
      message('');
    } catch (error) {
      message(`读取失败：${error.message}${data ? '。下面是上次读到的结构。' : ''}`, error.status === 401);
    } finally {
      busy = false;
      $('refresh').disabled = false;
      if (again) refresh();
      else if (!document.hidden) timer = setTimeout(refresh, AUTO_MS);
    }
  }

  // ── Changing a zone ──
  // Only the zone: tag changes (kept where it was among the tags); other tags stay as they are. The write carries the
  // revision the card was read at, so a change made elsewhere since is not overwritten.
  let editing = null, access = null, writing = false;
  const item = id => [...data.nodes, ...data.agents].find(entry => entry.id === id);
  async function post(path, body) {
    const response = await HGTarget.fetch(path, {method: 'POST', credentials: 'same-origin', cache: 'no-store',
      headers: {'Content-Type': 'application/json', 'X-HG': '1'}, body: JSON.stringify(body)});
    const value = await response.json().catch(() => null);
    if (response.ok) return value;
    const error = new Error(value?.error || `HTTP ${response.status}`);
    error.status = response.status; error.steer = !!value?.need_steer;
    throw error;
  }
  function withZone(tags, zone) {
    const tag = zone === NO_ZONE ? null : ZONE_TAG + zone, at = tags.findIndex(t => t.startsWith(ZONE_TAG));
    const rest = tags.filter(t => !t.startsWith(ZONE_TAG));
    if (!tag) return rest;
    if (at < 0) return [...tags, tag];
    return [...tags.slice(0, at).filter(t => !t.startsWith(ZONE_TAG)), tag, ...tags.slice(at + 1).filter(t => !t.startsWith(ZONE_TAG))];
  }
  function chosen() {
    const value = $('zone-form').elements.zone?.value;
    return value === '\u0000new' ? $('zone-new').value.trim() : value;
  }
  function zoneProblem() {
    const zone = chosen();
    if (!zone) return '先选一个区域，或者写下新区域的名字。';
    if (zone !== NO_ZONE && (/[\u0000-\u001f]/.test(zone) || (ZONE_TAG + zone).length > 64)) return '区域名太长或含控制字符。';
    if (zone === editing.zone) return '这就是现在的区域。';
    return '';
  }
  function renderZone(text = '', error = false) {
    const locked = !access?.steer;
    $('zone-unlock').hidden = !locked || !access?.steer_configured;
    $('zone-save').disabled = writing || locked || !!zoneProblem();
    $('zone-save').textContent = writing ? '保存中…' : '保存';
    const note = text || (!access ? '正在读取操作权限…' : !access.steer_configured ? '本节点未配置掌舵密码，区域只能查看。'
      : locked ? '修改区域需要先解锁掌舵权限。' : zoneProblem());
    $('zone-message').textContent = note;
    $('zone-message').classList.toggle('task-error', error);
  }
  async function readAccess() {
    try { access = await (await HGTarget.fetch(API + '/access', {credentials: 'same-origin', cache: 'no-store'})).json(); }
    catch (_) { access = null; }
    if (editing) renderZone();
  }
  function openZone(id) {
    const current = item(id);
    if (!current || writing) return;
    editing = {id, kind: current.kind, title: current.title, zone: current.zone, edit: current.edit};
    $('zone-heading').textContent = `修改区域 · ${current.title}`;
    const names = data.zones.map(zone => zone.name).filter(name => name !== NO_ZONE);
    const choice = (value, label, checked) => {
      const row = el('label', 'st-zone-choice'), input = el('input');
      Object.assign(input, {type: 'radio', name: 'zone', value, checked});
      row.append(input, el('span', null, label));
      return row;
    };
    const fresh = choice('\u0000new', '新区域：', false), name = el('input');
    Object.assign(name, {id: 'zone-new', type: 'text', maxLength: 59, placeholder: '区域名'});
    name.setAttribute('aria-label', '新区域名');
    name.addEventListener('focus', () => { fresh.querySelector('input').checked = true; renderZone(); });
    fresh.append(name);
    $('zone-choices').replaceChildren(el('legend', null, `现在：${current.zone}`),
      ...names.map(zone => choice(zone, zone, zone === current.zone)), choice(NO_ZONE, NO_ZONE + '（去掉区域标签）', current.zone === NO_ZONE), fresh);
    renderZone();
    $('zone-dialog').showModal();
    readAccess();
  }
  function closeZone() { if (writing) return; editing = null; $('zone-steer').value = ''; $('zone-dialog').close(); }
  async function saveZone(event) {
    event.preventDefault();
    if ($('zone-save').disabled) return;
    const attempt = editing, zone = chosen();
    writing = true; renderZone('正在保存…');
    try {
      await post(API + attempt.edit.path, {revision: attempt.edit.revision, tags: withZone(attempt.edit.tags, zone)});
      // The card leaves its old band: the place remembered for it (and its phones) belonged there.
      const before = data.nodes.find(node => node.id === attempt.id);
      for (const id of [attempt.id, ...(before?.phones || []).map(phone => phone.id)]) delete moved[id];
      save();
      writing = false; editing = null; $('zone-dialog').close();
      await refresh();
    } catch (error) {
      writing = false;
      if (error.status === 409) {
        // Changed elsewhere: read it again and keep the dialog, so nothing is overwritten unseen.
        await refresh();
        const now = item(attempt.id);
        if (editing === attempt && now) { attempt.edit = now.edit; attempt.zone = now.zone; }
        renderZone(`刚被别处改过（现在的区域：${now?.zone || '未知'}），已重新读取。确认后再保存。`, true);
      } else {
        if (error.steer) await readAccess();
        renderZone(`没有保存：${error.message}`, true);
      }
    }
  }
  async function unlockZone() {
    const pw = $('zone-steer').value;
    $('zone-steer').value = '';
    if (!pw) return;
    try { await post(API + '/steer/unlock', {pw}); await readAccess(); }
    catch (error) { renderZone(`解锁失败：${error.message}`, true); }
  }
  $('world').addEventListener('click', event => { const button = event.target.closest('.st-zone-btn'); if (button) openZone(button.dataset.edit); });
  $('zone-form').addEventListener('change', () => renderZone());
  $('zone-form').addEventListener('input', () => renderZone());
  $('zone-form').addEventListener('submit', saveZone);
  $('zone-cancel').addEventListener('click', closeZone);
  $('zone-dialog').addEventListener('cancel', event => { event.preventDefault(); closeZone(); });
  $('zone-unlock-submit').addEventListener('click', unlockZone);
  $('zone-steer').addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); unlockZone(); } });
  let showArchived = false;
  $('archived-agents').addEventListener('click', () => { showArchived = !showArchived; refresh(); });
  $('refresh').addEventListener('click', refresh);
  document.addEventListener('visibilitychange', () => { if (document.hidden) clearTimeout(timer); else refresh(); });
  addEventListener('resize', () => { if (data) place(); });
  refresh();
})();
