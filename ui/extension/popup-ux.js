/* Shared popup interactions. Platform adapters own LIVE and AI actions. */
(() => {
  'use strict';
  // C06: ba trạng thái nguồn video + pin đã xác nhận, kèm tuổi đo (đọc từ /api/video/position).
  const secs = value => Number.isFinite(Number(value)) && Number(value) >= 0 ? Math.round(Number(value)) : null;
  function videoSourceBadge(pos) {
    const p = pos || {};
    const state = String(p.state || '');
    const stale = p.available === false && p.reason === 'stale' || state === 'stale' || state === 'offline' || state === 'waiting' || (!state && p.available !== true);
    const verified = secs(p.verification_age_s), age = secs(p.age_s);
    if (stale && state !== 'locked' && state !== 'uncertain') {
      return {kind: 'stale', className: 'src-stale', text: 'Mất tín hiệu nguồn' + (age !== null ? ` · đo cách đây ${age} giây` : ' · chưa có số đo')};
    }
    if (state === 'uncertain' || state === 'weak' || state === 'verifying') {
      return {kind: 'uncertain', className: 'src-uncertain', text: 'Video đã khoá · vị trí chưa chắc chắn' + (verified !== null ? ` · kiểm chứng cách đây ${verified} giây` : '')};
    }
    if (state === 'locked') {
      return {kind: 'locked', className: 'src-locked', text: 'Video đã khoá · vị trí đã kiểm chứng' + (verified !== null ? ` · ${verified} giây trước` : '')};
    }
    return {kind: 'stale', className: 'src-stale', text: 'Chờ nguồn phát'};
  }
  function pinBadge(observedAtMs, title, now = Date.now()) {
    const ago = Number(observedAtMs) > 0 ? Math.round((now - Number(observedAtMs)) / 1000) : null;
    if (ago !== null && ago >= 0 && ago < 15) {
      return {kind: 'confirmed', className: 'pin-confirmed', text: 'Ghim đã xác nhận' + (title ? ` · ${title}` : '') + ` · quan sát ${ago} giây trước`};
    }
    return {kind: 'unconfirmed', className: 'pin-unconfirmed', text: 'Ghim chưa xác nhận' + (ago !== null && ago >= 0 ? ` · lần cuối ${ago} giây trước` : '')};
  }
  if (typeof document === 'undefined') { module.exports = {videoSourceBadge, pinBadge}; return; }
  const tabs = [...document.querySelectorAll('[role="tab"]')];
  const panels = [...document.querySelectorAll('[role="tabpanel"]')];
  function select(tab, focus = false) {
    for (const item of tabs) {
      const active = item === tab;
      item.setAttribute('aria-selected', String(active));
      item.tabIndex = active ? 0 : -1;
    }
    for (const panel of panels) panel.hidden = panel.id !== tab.getAttribute('aria-controls');
    if (focus) tab.focus();
  }
  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => select(tab));
    tab.addEventListener('keydown', event => {
      let next;
      if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
      if (event.key === 'ArrowLeft') next = (index + tabs.length - 1) % tabs.length;
      if (event.key === 'Home') next = 0;
      if (event.key === 'End') next = tabs.length - 1;
      if (next !== undefined) { event.preventDefault(); select(tabs[next], true); }
    });
  });
  let toastTimer;
  globalThis.KidTEPopupUX = {
    notify(text, error = false) {
      const toast = document.getElementById('action-feedback');
      if (!toast) return;
      clearTimeout(toastTimer);
      toast.textContent = text;
      toast.dataset.tone = error ? 'error' : 'success';
      toast.hidden = false;
      toastTimer = setTimeout(() => { toast.hidden = true; }, error ? 10000 : 4000);
    },
    videoSourceBadge, pinBadge,
    state(value) { document.body.dataset.connection = value; },
  };
  window.addEventListener('unhandledrejection', event => {
    globalThis.KidTEPopupUX.notify(event.reason?.message || 'Thao tác chưa hoàn tất. Hãy thử lại.', true);
    event.preventDefault();
  });
})();
