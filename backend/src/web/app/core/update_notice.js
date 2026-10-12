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

// ---- Luồng cập nhật một nút (trang Cập nhật + banner) ----
// Tính năng tuỳ chọn: nằm trong mục thu gọn; tên nội bộ runtime/extensions chỉ ở "Chi tiết kỹ thuật".
export const OPTIONAL_IDS = ['gpu-cuda12', 'video-sync', 'video-analysis-cpu', 'captcha-solver', 'media-tools'];
export const CORE_IDS = ['core', 'app', 'runtime', 'extensions'];

export function formatDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  return `${String(d.getDate()).padStart(2, '0')}/${String(d.getMonth() + 1).padStart(2, '0')}/${d.getFullYear()}`;
}

// {id: version} cho mọi gói cần lên bản mới (bắt buộc + tính năng đã cài có bản mới).
export function updateComponents(state) {
  const out = {};
  for (const p of newerPackages(state)) out[p.id] = p.version;
  return out;
}

export function headerLines(state) {
  const current = state && state.current_version;
  const latest = state && state.latest;
  const using = current ? `Đang dùng: Kid TE ${current}` : 'Đang dùng: Kid TE (chưa rõ bản)';
  let newest = '';
  if (latest && latest.version) {
    const date = formatDate(latest.date);
    newest = `Mới nhất trên GitHub: ${latest.version}${date ? ` (${date})` : ''}`;
  }
  const upToDate = Boolean(state && state.enabled !== false && latest && !state.update_available && !state.restart_required);
  return {using, newest, upToDate, upToDateText: 'Bạn đang dùng bản mới nhất ✓'};
}

export function progressView(state) {
  const p = state && state.progress;
  if (!p) return null;
  const total = p.overall_total || p.total || 0;
  const received = p.overall_total ? p.overall_received : p.received;
  const percent = total > 0 ? Math.max(0, Math.min(100, Math.floor(received * 100 / total))) : 0;
  const mb = n => (n / 1e6).toFixed(n >= 1e8 ? 0 : 1);
  return {percent, component: p.component, index: p.index || 1, count: p.count || 1,
    text: `${percent}% · ${mb(received)} / ${mb(total)} MB`};
}

export function restartNeeded(state) {
  return Boolean(state && state.restart_required);
}

// Văn bản xác nhận khi bấm khởi động lại lúc đang LIVE (chủ app chọn cho phép).
export const LIVE_RESTART_TEXT = 'Đang LIVE — app sẽ tắt khoảng 20–30 giây rồi tự mở lại; bình luận đến trong lúc đó vẫn được giữ trong hàng chờ.';
