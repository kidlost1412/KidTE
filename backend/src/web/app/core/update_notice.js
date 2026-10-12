// Mô hình thông báo bản cập nhật: thuần dữ liệu, không phụ thuộc DOM (test được bằng node).

export const DISMISS_KEY = 'lr-update-dismissed';

export function newerPackages(state) {
  return (state && state.available || []).filter(p => p && p.newer);
}

// Banner đầu trang + chấm trên mục "Cài đặt & hệ thống".
// dismissed: phiên bản người dùng đã ẩn banner trong phiên này (badge vẫn hiện).
export function updateNotice(state, dismissed = '') {
  const newer = newerPackages(state);
  if (!state || !state.update_available || !newer.length) return {visible: false, badge: false, text: '', version: ''};
  const core = newer.find(p => p.id === 'app' || p.id === 'core') || newer[0];
  const bytes = state.update_total_bytes || core.download_bytes || 0;
  const mb = Math.max(1, Math.round(bytes / 1e6));
  const text = state.restart_required
    ? `Đã tải bản ${core.version} · Khởi động lại để hoàn tất`
    : `Có bản mới ${core.version} (~${mb} MB) — Cập nhật ngay`;
  return {visible: dismissed !== core.version, badge: true, text, version: core.version};
}

export function formatClock(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

// Dòng trạng thái trên trang Cập nhật khi không có bản mới.
export function upToDateLine(state) {
  if (!state || state.update_available) return '';
  const clock = formatClock(state.last_checked_at);
  const version = state.installed && (state.installed.app || state.installed.core);
  if (!clock) return 'Chưa kiểm tra bản mới trong phiên này.';
  return version
    ? `Bạn đang dùng bản mới nhất (${version}) · kiểm tra lúc ${clock}`
    : `Chưa có bản mới · kiểm tra lúc ${clock}`;
}

// Gói tiện ích Chrome vừa được launcher đồng bộ: Chrome không tự nạp lại.
export const EXTENSION_RELOAD_TEXT = 'Tiện ích Chrome đã cập nhật — vào chrome://extensions bấm Tải lại';
export function extensionNotice(state) {
  const n = state && state.extension_reload;
  return n && n.version ? {visible: true, text: EXTENSION_RELOAD_TEXT, version: n.version} : {visible: false, text: '', version: ''};
}
