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
  return {of, note, age, os};
})();
