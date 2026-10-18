// Mô hình thuần cho dải trạng thái đầu trang (kiểm thử được không cần DOM).

/** Mất kết nối khi lần poll gần nhất lỗi — kể cả khi còn dashboard cũ (dữ liệu cũ không được coi là đang kết nối). */
export function connectionLost(error) {
  return Boolean(error);
}

export const OFFLINE_LABEL = 'Mất kết nối với Kid TE';
export const OFFLINE_TITLE = 'Không liên lạc được với Kid TE; số liệu đang hiện có thể đã cũ. Sẽ tự cập nhật khi kết nối lại. Bấm để tải lại.';

/** Nhãn rút gọn cho màn hẹp: tên + dấu hiệu bằng chữ (không chỉ dựa vào màu). tone: ok|live|warn|danger. */
export function shortPill(name, tone) {
  const mark = tone === 'ok' || tone === 'live' ? '✓' : tone === 'danger' ? '✗' : '…';
  return `${name} ${mark}`;
}
