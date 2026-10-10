// Hash router: #trang/muc/ma  (vd #video/studio/final-vjp-20260920)
const LEGACY = {live: 'inbox', products: 'catalog', videos: 'video', trash: 'system'};
const LEGACY_SUB = {'video-jobs': 'library', videos: 'library'};

export function parseHash(hash) {
  const raw = String(hash || '').replace(/^#\/?/, '');
  const parts = raw.split('/').map(part => { try { return decodeURIComponent(part); } catch (_) { return part; } });
  let page = parts[0] || 'overview';
  let sub = parts[1] || '';
  const id = parts.slice(2).join('/');
  if (LEGACY[page]) {
    if (page === 'trash') sub = 'trash';
    page = LEGACY[page];
  }
  if (page === 'video' && LEGACY_SUB[sub]) sub = LEGACY_SUB[sub];
  return {page, sub, id};
}

export function buildHash(page, sub, id) {
  return '#' + [page, sub, id].filter(v => v !== undefined && v !== null && v !== '').map(v => encodeURIComponent(String(v))).join('/');
}

export function createRouter(onRoute) {
  let guard = null;
  let current = location.hash;
  let generation = 0;

  function emit() {
    current = location.hash;
    generation += 1;
    onRoute({...parseHash(location.hash), generation});
  }

  function onHashChange() {
    if (location.hash === current) return;
    const target = location.hash;
    if (guard && guard(target) === false) {
      history.replaceState(null, '', current || '#overview');
      return;
    }
    emit();
  }

  window.addEventListener('hashchange', onHashChange);
  return {
    start: emit,
    current: () => parseHash(location.hash),
    setGuard(fn) { guard = typeof fn === 'function' ? fn : null; },
    go(page, sub, id, {replace = false, force = false} = {}) {
      const target = buildHash(page, sub, id);
      if (target === location.hash && !force) return;
      if (!force && guard && guard(target) === false) return;
      if (replace) history.replaceState(null, '', target); else history.pushState(null, '', target);
      emit();
    },
    /** Đổi URL không dựng lại trang (vd chọn bản ghi trong danh sách). */
    replaceSilently(page, sub, id) {
      const target = buildHash(page, sub, id);
      history.replaceState(null, '', target);
      current = target;
    },
  };
}
