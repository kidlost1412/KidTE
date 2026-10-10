import {request} from './api.js';

/** Store tối giản có subscribe theo khoá. */
export function createStore(initial = {}) {
  let state = {...initial};
  const listeners = new Set();
  return {
    get: () => state,
    set(patch) {
      const prev = state;
      state = {...state, ...patch};
      listeners.forEach(fn => { try { fn(state, prev); } catch (e) { console.error(e); } });
    },
    subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn); },
  };
}

/**
 * Một vòng poll /api/dashboard DUY NHẤT cho cả ứng dụng (bất biến F03).
 * Trang nào cần dữ liệu thì subscribe store; không tự poll riêng.
 */
export function startDashboardPoll(store, intervalMs = 5000) {
  let controller = null;
  let timer = 0;
  let stopped = false;
  let refreshPending = false;
  async function tick() {
    if (stopped) return;
    if (controller) {refreshPending = true; return;}
    controller = new AbortController();
    const current = controller;
    try {
      const data = await request('/api/dashboard', {signal: current.signal});
      if (stopped || current !== controller) return;
      store.set({dashboard: data, dashboardError: null, dashboardAt: new Date()});
    } catch (error) {
      if (stopped || current !== controller) return;
      if (error.code !== 'aborted') store.set({dashboardError: error});
    }
    controller = null;
    if (!stopped) {
      const delay = refreshPending ? 0 : document.hidden ? intervalMs * 3 : intervalMs;
      refreshPending = false;
      timer = setTimeout(tick, delay);
    }
  }
  tick();
  return {
    refresh() { clearTimeout(timer); tick(); },
    stop() { stopped = true; clearTimeout(timer); if (controller) controller.abort(); },
  };
}
