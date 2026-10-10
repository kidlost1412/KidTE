// Pure view-model for the CAPTCHA health card (tested with node --test).
const dash = '—';
const hhmm = (at) => (at && at.length >= 16 ? at.slice(11, 16) : '');

export function ago(at, now = new Date()) {
  if (!at) return dash;
  const then = new Date(at.replace(' ', 'T'));
  if (Number.isNaN(then.getTime())) return dash;
  const s = Math.max(0, Math.round((now - then) / 1000));
  const when = s < 60 ? `${s} giây trước` : s < 3600 ? `${Math.floor(s / 60)} phút trước`
    : s < 86400 ? `${Math.floor(s / 3600)} giờ trước` : at.slice(0, 16);
  return `${hhmm(at)} · ${when}`;
}

export function healthModel(state, now = new Date()) {
  const h = (state && state.health) || null;
  if (!h) {
    return {tone: state && state.running ? 'ok' : 'warn', label: state && state.running ? 'Đang chạy' : 'Chưa chạy',
      sub: '', stats: [], state: 'legacy'};
  }
  const sub = state.running
    ? `PID ${state.pid} · ${state.admin ? 'quyền Administrator' : 'đang xác minh quyền Admin'}`
    : (h.detail && h.state !== 'start_failed' ? h.detail : '');
  const error = h.last_api_error ? `${h.last_api_error.slice(0, 80)} (${hhmm(h.last_api_error_at)})` : 'Không có';
  return {
    tone: h.tone, label: h.label, state: h.state, sub,
    result: h.last_result_text || '',
    stats: [
      {name: 'Quét lần cuối', value: ago(h.last_heartbeat_at, now) + (h.last_scan_resolution ? ` · ${h.last_scan_resolution}` : '')},
      {name: 'Lần giải gần nhất', value: h.last_detect_at ? `${ago(h.last_detect_at, now)}${h.last_detect_type ? ' · ' + h.last_detect_type : ''}` : dash},
      {name: 'Số lần thử hôm nay', value: String(h.attempts_today ?? 0)},
      {name: 'Lỗi gần nhất', value: error},
    ],
  };
}

export const SELFTEST_NAMES = {
  solver_import: 'Nạp gói bộ giải', resources: 'Tài nguyên nhận dạng', ffmpeg: 'FFmpeg (quay video)',
  key_decrypt: 'Giải mã key SadCaptcha', screen_capture: 'Chụp màn hình', sadcaptcha_tls: 'Kết nối SadCaptcha',
};

export function selftestRows(result) {
  if (!result || !Array.isArray(result.checks) || !result.checks.length) {
    return [{name: 'Kiểm tra thiết lập', ok: false, detail: '',
      hint: result && result.error === 'captcha_selftest_timeout' ? 'Hết thời gian chờ; thử lại.' : 'Không chạy được phép kiểm tra.'}];
  }
  return result.checks.map(c => ({name: SELFTEST_NAMES[c.name] || c.name, ok: !!c.ok, detail: c.detail || '', hint: c.hint || ''}));
}
