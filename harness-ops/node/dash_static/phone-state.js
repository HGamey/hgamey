// A phone as the center sees it, the same on every page. Its computer's link down: off. Otherwise the newest reading
// decides: the phone in it and online is on (old when the reading is stale: shown as usual, with the reading's age);
// a reading without the phone, or with it not online, is gone; with no reading at all it is old, with nothing read.
window.HGPhoneState = (() => {
  'use strict';
  const NOTE = {off: '离线', gone: '未接入'};
  const os = text => text && text.replace(/\s*\(SDK \d+\)$/, '').replace(/^OpenHarmony-(\d+\.\d+).*$/, 'OpenHarmony $1');
  function age(at) {
    const s = Math.max(0, Date.now() / 1000 - at);
    if (s < 60) return '刚刚';
    if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
    if (s < 86400) return `${Math.floor(s / 3600)} 小时前`;
    return `${Math.floor(s / 86400)} 天前`;
  }
  function of(item) {
    const seen = item.observation || {}, read = seen.online != null;
    const state = !item.connected ? 'off' : seen.online === true ? (item.stale ? 'old' : 'on')
      : seen.online === false || item.sampled_at ? 'gone' : 'old';
    return {state, online: state === 'on' || state === 'old', read: read && item.sampled_at, seen: read,
            title: seen.brand && seen.model ? `${seen.brand} ${seen.model}` : item.model || item.device_id,
            os: os(seen.os), root: read ? !!seen.root : null, transport: seen.transport};
  }
  const note = phone => NOTE[phone.state] || (phone.state === 'old' && phone.read ? `读数 ${age(phone.read)}` : '');
  // A phone a person should look at, from its reading: battery under 10%, or a SoC at 45 °C or more. Shown only; jobs
  // are still sent to it.
  const LOW_PCT = 10, HOT_C = 45;
  function warnings(seen) {
    const level = seen?.battery?.level_pct, heat = seen?.thermal?.soc_max_c, out = [];
    if (typeof level === 'number' && level < LOW_PCT) out.push(`电量低 ${level}%`);
    if (typeof heat === 'number' && heat >= HOT_C) out.push(`过热 ${heat} °C`);
    return out;
  }
  // Leftover processes the node stopped on this phone in the last day (phone_cleanup): one line, each process on hover.
  const ran = s => { const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
    return d ? `${d} 天 ${h} 小时` : h ? `${h} 小时 ${m} 分` : `${Math.max(1, m)} 分`; };
  function cleaned(seen, now = Date.now() / 1000) {
    const processes = (seen?.cleanups || []).filter(c => c.at >= now - 86400).flatMap(c => c.processes || []);
    return processes.length ? {text: `清理了 ${processes.length} 个残留进程`,
      title: processes.map(p => `${p.name}（PID ${p.pid}，跑了 ${ran(p.elapsed_s)}）`).join('\n')} : null;
  }
  return {of, note, age, os, warnings, cleaned};
})();
