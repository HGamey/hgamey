/* A small preview under any file a page links to that is a picture or a video: pictures as a thumbnail that opens
   large, videos as a small player. Nothing is read until it comes near the screen; one that cannot be read says so.
   Pages with the comparison viewer open pictures and videos in it; others open them in a new tab. */
window.HGMedia = (() => {
  'use strict';
  const PICTURES = {png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', webp: 'image/webp', gif: 'image/gif', svg: 'image/svg+xml'};
  const VIDEOS = {mp4: 'video/mp4', webm: 'video/webm'};
  const extension = path => String(path || '').split('.').pop().toLowerCase();
  const kind = path => PICTURES[extension(path)] ? 'picture' : VIDEOS[extension(path)] ? 'video' : null;
  const el = (tag, value, className) => { const item = document.createElement(tag); if (value != null) item.textContent = String(value); if (className) item.className = className; return item; };
  const runs = new WeakMap(), seen = 'IntersectionObserver' in window ? new IntersectionObserver(entries => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      seen.unobserve(entry.target); const run = runs.get(entry.target); runs.delete(entry.target); run?.();
    }
  }, {rootMargin: '600px 0px'}) : null;
  const near = (node, run) => { if (!seen) return run(); runs.set(node, run); seen.observe(node); };
  function open(url, name, video) {
    if (window.HGResearchCompare && window.HGResearchTask) {
      HGResearchCompare.viewer(document.body, [{slot: name, url, kind: video ? 'video' : 'image'}], name, HGResearchTask.util, {label: '放大查看'});
    } else window.open(url, '_blank', 'noopener');
  }
  // `source` is the file's API path (it is fetched with the page's target binding); `path` names it.
  function preview(source, path) {
    const name = String(path).split('/').pop(), type = kind(path);
    if (type === 'video') {
      const box = el('span', null, 'media-preview video'), player = el('video'), zoom = el('button', '放大', 'media-zoom');
      Object.assign(player, {controls: true, preload: 'metadata', playsInline: true, muted: true});
      player.setAttribute('aria-label', name);
      player.onerror = () => box.replaceWith(el('span', '无法预览', 'note media-unread'));
      near(box, () => { player.src = HGTarget.url(source) + '#t=0.1'; });
      zoom.type = 'button'; zoom.setAttribute('aria-label', '放大播放 ' + name);
      zoom.onclick = () => { player.pause(); open(HGTarget.url(source), name, true); };
      box.append(player, zoom);
      return box;
    }
    if (type !== 'picture') return null;
    // Until the picture is in, a plain box says it is on its way: an image without its picture yet would draw as broken.
    const button = el('button', null, 'media-preview'), wait = el('span', '读取中…', 'media-wait');
    button.type = 'button'; button.setAttribute('aria-label', '放大查看 ' + name);
    button.append(wait);
    const unread = () => button.replaceWith(el('span', '无法预览', 'note media-unread'));
    near(button, async () => {
      try {
        const response = await HGTarget.fetch(source, {credentials: 'same-origin', cache: 'no-store'});
        if (!response.ok) throw new Error('HTTP ' + response.status);
        const blob = await response.blob(), url = URL.createObjectURL(blob.slice(0, blob.size, PICTURES[extension(path)]));
        const image = el('img');
        image.alt = name; image.onerror = unread; image.onload = () => wait.remove();
        image.src = url; button.prepend(image);
        button.onclick = () => open(url, name, false);
      } catch (_) { unread(); }
    });
    return button;
  }
  return {kind, preview};
})();
