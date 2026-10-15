// Mô hình thuần cho Trợ lý chuẩn bị LIVE (không DOM) — test bằng node --test.
// Dữ liệu vào: dash.readiness (backend/src/live_readiness.py) + vài trường dashboard.

export const MODE_KEY = 'kt-wizard-mode';
export const GROUPS = [
  {id: 'once', title: 'Chuẩn bị một lần', sub: 'Làm xong một lần là dùng mãi'},
  {id: 'session', title: 'Trước mỗi phiên LIVE', sub: 'Kiểm lại trước khi bấm phát'},
  {id: 'live', title: 'Trong khi LIVE', sub: 'Theo dõi khi đang phát'},
];
export const STATE_VIEW = {
  done: {label: 'Xong', tone: 'ok', icon: 'check'},
  todo: {label: 'Cần làm', tone: 'warn', icon: 'alert'},
  blocked: {label: 'Chờ bước trước', tone: 'neutral', icon: 'clock'},
  error: {label: 'Có lỗi', tone: 'danger', icon: 'x'},
  optional: {label: 'Tuỳ chọn', tone: 'info', icon: 'sparkles'},
  na: {label: 'Không cần', tone: 'neutral', icon: 'check'},
};
// Nhãn ngắn cho hàng chip chế độ "Đã quen".
export const SHORT = {A1: 'Tiện ích', A2: 'Tiện ích Shopee', A3: 'AI', A4: 'Hồ sơ', A5: 'Giỏ', A6: 'Gắn hồ sơ',
  A7: 'Whisper', A8: 'Captcha', B1: 'Video', B2: 'Lịch ghim', B3: 'Nguồn', B4: 'Đồng bộ', B5: 'Sao lưu',
  C1: 'LIVE', C2: 'Nhận diện', C3: 'Tự ghim', C4: 'AI trả lời', C5: 'Theo dõi'};
const OPEN = new Set(['todo', 'blocked', 'error']);

export function readinessOf(dash) {
  const r = dash && dash.readiness;
  if (!r || r.ok === false || !Array.isArray(r.steps)) return null;
  return r;
}

export function loadMode(storage) {
  try { const v = storage && storage.getItem(MODE_KEY); return v === 'new' || v === 'pro' ? v : ''; } catch (_) { return ''; }
}
export function saveMode(storage, mode) {
  try { storage && storage.setItem(MODE_KEY, mode); } catch (_) { /* trình duyệt chặn lưu: bỏ qua */ }
}
export function pickMode(stored, summary) {
  if (stored === 'new' || stored === 'pro') return stored;
  return (summary && Number(summary.done) > 2) ? 'pro' : 'new';
}

// Thứ tự hiển thị: theo nhóm A -> B -> C, giữ thứ tự backend trong nhóm.
export function orderSteps(steps) {
  const rank = id => { const i = GROUPS.findIndex(g => g.id === id); return i < 0 ? 99 : i; };
  return (steps || []).map((s, i) => [s, i]).sort((a, b) => rank(a[0].group) - rank(b[0].group) || a[1] - b[1]).map(x => x[0]);
}

// Bước hiện tại: bước bắt buộc đầu tiên chưa xong (ưu tiên lỗi), không có thì bước cần làm đầu tiên.
export function currentStep(steps) {
  const list = orderSteps(steps).filter(s => s.state !== 'na');
  return list.find(s => s.required && s.state === 'error')
    || list.find(s => s.required && OPEN.has(s.state))
    || list.find(s => s.state === 'todo' || s.state === 'error') || null;
}

export function groupSteps(steps, mode) {
  const ordered = orderSteps(steps);
  return GROUPS.map(g => {
    const items = ordered.filter(s => s.group === g.id && (mode === 'new' || (OPEN.has(s.state) && s.state !== 'blocked') || s.state === 'error'));
    const all = ordered.filter(s => s.group === g.id && s.state !== 'na');
    return {...g, items, done: all.filter(s => s.state === 'done').length, total: all.length};
  });
}

export function bannerModel(summary) {
  const s = summary || {};
  const blocking = Number(s.blocking) || 0;
  if (blocking > 0) return {tone: 'warn', title: `Còn ${blocking} việc bắt buộc trước khi LIVE`, text: `Đã xong ${s.done || 0}/${s.total || 0} bước.`};
  return {tone: 'ok', title: 'Đã sẵn sàng để LIVE', text: `Đã xong ${s.done || 0}/${s.total || 0} bước. Các bước còn lại là tuỳ chọn.`};
}

export function chipRow(steps) {
  return orderSteps(steps).filter(s => s.group !== 'live' && s.state !== 'na')
    .map(s => ({id: s.id, label: SHORT[s.id] || s.title, tone: (STATE_VIEW[s.state] || STATE_VIEW.todo).tone, state: s.state}));
}

// Mô tả nút hành động của một bước. Không gọi mạng; trang tự thực hiện.
export function actionView(step) {
  const a = step && step.action;
  if (!a || step.state === 'done' && a.kind !== 'toggle') return null;
  if (a.kind === 'navigate') return {kind: 'navigate', label: step.state === 'error' ? 'Sửa' : 'Mở', page: a.page, sub: a.sub || '', arg: a.arg || ''};
  if (a.kind === 'post') return {kind: 'post', label: a.label || 'Làm ngay', endpoint: a.endpoint, body: a.body || {},
    then: step.id === 'A6' ? {page: 'catalog', sub: 'mapping'} : null};
  if (a.kind === 'toggle') return {kind: 'toggle', label: a.label || (a.on ? 'Bật' : 'Tắt'), target: a.target, on: !!a.on};
  return null;
}

const SYNC_ID = 'dong-bo-video';

// Lệnh gửi đi cho từng công tắc; confirm != null thì trang phải hỏi trước.
export function toggleRequest(id, on, ctx = {}) {
  const live = !!ctx.live;
  if (id === 'sync') return {endpoint: '/api/plugins/action', body: {id: SYNC_ID, action: on ? 'start' : 'stop'},
    confirm: !on && live ? {title: 'Tắt đồng bộ video?', text: 'Đang LIVE: tắt sẽ xoá vị trí video của cả hai sàn, tự ghim sẽ dừng.', tone: 'danger', confirm: 'Tắt đồng bộ'} : null};
  if (id === 'src-tiktok' || id === 'src-shopee') {
    const platform = id === 'src-tiktok' ? 'tiktok' : 'shopee';
    return {endpoint: '/api/live/sources', body: {platform, settings: {enabled: on}},
      confirm: !on && live ? {title: `Tắt nhận diện ${platform === 'tiktok' ? 'TikTok' : 'Shopee'}?`, text: 'Đang LIVE: Kid TE sẽ không biết video tới đâu, tự ghim trên sàn này dừng lại.', tone: 'danger', confirm: 'Tắt nhận diện'} : null};
  }
  if (id === 'captcha') return {endpoint: on ? '/api/captcha/start' : '/api/captcha/stop', body: {},
    confirm: on ? null : {title: 'Dừng giải CAPTCHA?', text: 'Nếu TikTok hiện ô xác minh, LIVE có thể bị gián đoạn cho tới khi bạn tự giải.', tone: 'danger', confirm: 'Dừng'}};
  if (id === 'ai-tiktok' || id === 'ai-shopee') {
    const platform = id === 'ai-tiktok' ? 'tiktok' : 'shopee';
    return {endpoint: '/api/live/control', body: {platform, ai_enabled: on},
      confirm: !on && live ? {title: 'Tắt AI trả lời?', text: 'Khách hỏi sẽ không được AI trả lời cho tới khi bật lại.', tone: 'danger', confirm: 'Tắt AI'} : null};
  }
  if (id === 'queue-mode') return {endpoint: '/api/live/queue-mode', body: {enabled: !!on},
    confirm: {title: on ? 'Bật chế độ hàng chờ?' : 'Tắt chế độ hàng chờ?',
      text: (on ? 'Tiện ích sẽ lấy câu trả lời qua hàng chờ của Kid TE và kiểm lại dữ kiện ngay lúc gửi.'
        : 'Tiện ích quay về cách trả lời trực tiếp như trước.') + ' Nên đổi khi chưa LIVE' + (live ? ' — bạn đang LIVE.' : '.'),
      tone: live ? 'danger' : 'warn', confirm: on ? 'Bật' : 'Tắt'}};
  if (id === 'pin-tiktok') return {endpoint: '/api/live/control', body: {platform: 'tiktok', auto_pin: on}, confirm: null};
  return null;
}

// Khối Bật/tắt nhanh. dash.live_control vắng (backend cũ) => AI/Tự ghim chỉ hiển thị + "Cách bật".
export function quickToggles(dash) {
  const d = dash || {};
  const ext = d.extension || {};
  const ch = d.live_channels || {};
  const tk = ch.tiktok || {}, sp = ch.shopee || {};
  const plugins = (d.plugins && d.plugins.plugins) || [];
  const sync = plugins.find(p => p && p.id === SYNC_ID);
  const cap = d.captcha || {};
  const lc = d.live_control && typeof d.live_control === 'object' ? d.live_control : null;
  const out = [];
  if (sync) out.push({id: 'sync', label: 'Đồng bộ video', on: (sync.runtime || {}).state === 'running', note: 'Biết video đang tới đâu để ghim đúng'});
  if (tk.settings) out.push({id: 'src-tiktok', label: 'Nhận diện TikTok', on: tk.settings.enabled !== false, note: 'Nghe TikTok Studio'});
  if (sp.settings) out.push({id: 'src-shopee', label: 'Nhận diện Shopee', on: sp.settings.enabled !== false, note: 'Đọc OBS'});
  if (cap.available !== false && (cap.configured || cap.running)) out.push({id: 'captcha', label: 'Giải CAPTCHA', on: !!cap.running,
    note: cap.running ? 'Đang chạy' : 'Khi bật, Windows sẽ hỏi quyền quản trị (UAC) — bấm Có'});
  const aiTk = !!(ext.ai_enabled && ext.ai_ready);
  const spExt = sp.extension || {};
  const aiSp = !!(spExt.connected !== false && spExt.ai_enabled && spExt.ai_ready === true);
  const ctl = (pf, key, actual) => {
    if (!lc) return {mode: 'info', on: actual};
    const c = lc[pf] || {};
    const want = c[key];
    const pending = want !== null && want !== undefined && c.applied === false;
    // Tiện ích đã nhận lệnh nhưng không áp được (vd. DeepSeek chưa sẵn sàng, lệnh quá hạn): hiện trạng thái thật + lý do.
    const failed = !pending && c.applied === true && !!c.control_error;
    return {mode: 'control', on: failed || want === null || want === undefined ? actual : !!want, actual, pending,
      error: failed ? c.control_error : '',
      note: pending ? 'Đang chờ tiện ích nhận lệnh…' : failed ? 'Không áp được: ' + c.control_error : null};
  };
  const ai = (id, label, pf, actual) => {
    const c = ctl(pf, 'ai_enabled', actual);
    return {id, label, ...c, note: c.note || (actual ? 'AI đang trả lời' : 'AI đang tắt'),
      howTo: c.mode === 'info' ? 'Bật/tắt trong cửa sổ tiện ích trên Chrome (biểu tượng Kid TE, mục “AI trả lời”).' : null};
  };
  out.push(ai('ai-tiktok', 'AI trả lời TikTok', 'tiktok', aiTk));
  if (sp.settings ? sp.settings.enabled !== false : !!sp.extension) out.push(ai('ai-shopee', 'AI trả lời Shopee', 'shopee', aiSp));
  if (lc) {
    // Trạng thái thật từ heartbeat TikTok (đồng hồ ghim); tiện ích cũ thì suy từ vị trí video.
    const pinActual = typeof ext.pin_running === 'boolean' ? ext.pin_running
      : !!(tk.position && tk.position.available && ext.live_running);
    const c = ctl('tiktok', 'auto_pin', pinActual);
    out.push({id: 'pin-tiktok', label: 'Tự ghim TikTok', ...c, note: c.note || (c.on ? 'Ghim theo lịch' : 'Đang tạm dừng')});
    out.push({id: 'queue-mode', label: 'Chế độ hàng chờ', on: !!lc.queue_mode,
      note: lc.queue_mode ? 'Trả lời qua hàng chờ, kiểm dữ kiện lúc gửi' : 'Trả lời trực tiếp'});
  }
  return out;
}
