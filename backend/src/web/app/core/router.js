import {createHistoryStack, normalizeHash} from './nav_history.js';

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
  // Lịch sử trong ứng dụng (gồm cả sub-tab). Đến đúng trang ngay trước = quay lại (pop), còn lại = trang mới.
  const history_ = createHistoryStack();

  function record(mode) {
    const hash = normalizeHash(location.hash);
    if (mode === 'replace') history_.replaceCurrent(hash);
    else if (hash === history_.previous()) history_.pop();
    else history_.visit(hash);
  }

  function emit(mode = 'visit') {
    current = location.hash;
    record(mode);
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
    start: () => emit(),
    current: () => parseHash(location.hash),
    setGuard(fn) { guard = typeof fn === 'function' ? fn : null; },
    go(page, sub, id, {replace = false, force = false} = {}) {
      const target = buildHash(page, sub, id);
      if (target === location.hash && !force) return;
      if (!force && guard && guard(target) === false) return;
      if (replace) history.replaceState(null, '', target); else history.pushState(null, '', target);
      emit(replace ? 'replace' : 'visit');
    },
    /** Đổi URL không dựng lại trang (vd chọn bản ghi trong danh sách). */
    replaceSilently(page, sub, id) {
      const target = buildHash(page, sub, id);
      history.replaceState(null, '', target);
      current = target;
      history_.replaceCurrent(target);
    },
    canBack: () => history_.canBack(),
    /** Quay lại trang trước trong ứng dụng. Trả về false nếu không có trang để quay lại hoặc bị chặn. */
    back() {
      const prev = history_.previous();
      if (!prev) return false;
      if (guard && guard(prev) === false) return false;
      history.pushState(null, '', prev);
      emit();
      return true;
    },
    /** Lịch sử hiện tại (để kiểm thử/gỡ lỗi). */
    history: () => history_.snapshot(),
  };
}
