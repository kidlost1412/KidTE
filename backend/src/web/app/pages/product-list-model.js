// Mô hình thuần (không DOM) cho danh sách sản phẩm/hồ sơ: tên ngắn, chọn tất cả, tóm tắt hàng loạt.

// Cụm tiếp thị thường đứng sau tên sản phẩm trên listing; gặp là cắt (khớp server `_short_product_name`).
const MARKETING = ['giup', 'chuyen dung', 'chuyen tri', 'cong nghe', 'co tac dung', 'danh cho', 'dung cho',
  'bao hanh', 'chinh hang', 'hang chinh', 'nong duoc toan cau', 'nong duoc', 'toan cau'];
const MAX_WORDS = 7;

function fold(value) {
  return String(value ?? '').toLowerCase().replace(/đ/g, 'd').normalize('NFD').replace(/[̀-ͯ]/g, '').replace(/\s+/g, ' ').trim();
}

/** Tên ngắn đọc được nhanh: bỏ phần sau " - " / "|", cắt trước cụm tiếp thị, tối đa 7 từ và `max` ký tự. */
export function shortProductName(title, max = 60) {
  const text = String(title ?? '').replace(/\s+/g, ' ').trim();
  if (!text) return '';
  const head = text.split(/\s[-–]\s|\|/)[0].trim() || text;
  const words = head.split(' ');
  const keys = words.map(w => fold(w).replace(/[^a-z0-9]/g, ''));
  let cut = words.length;
  for (const marker of MARKETING) {
    const parts = marker.split(' ');
    for (let i = 1; i <= keys.length - parts.length; i += 1) {
      if (parts.every((p, j) => keys[i + j] === p)) { cut = Math.min(cut, i); break; }
    }
  }
  let kept = words.slice(0, Math.max(1, Math.min(cut, MAX_WORDS)));
  kept = kept.map((w, i) => (i >= 2 ? w.replace(/,$/, '') : w));
  let short = kept.join(' ').replace(/[ ,;:\-]+$/, '');
  if (short.length > max) {
    const slice = short.slice(0, max);
    const at = slice.lastIndexOf(' ');
    short = (at > max * 0.5 ? slice.slice(0, at) : slice).replace(/[ ,;:\-]+$/, '') + '…';
  }
  return short || text.slice(0, max);
}

/** Trạng thái ô "Chọn tất cả" theo danh sách đang hiện (đã lọc/tìm). */
export function selectionState(visibleIds, picked) {
  const ids = (visibleIds || []).filter(Boolean);
  const count = ids.filter(id => picked && picked.has(id)).length;
  return {total: ids.length, count, all: ids.length > 0 && count === ids.length, some: count > 0 && count < ids.length};
}

/** Bật/tắt chọn cả danh sách đang hiện; giữ nguyên lựa chọn ở ngoài bộ lọc. Trả về chính Set đã sửa. */
export function setAllVisible(picked, visibleIds, on) {
  (visibleIds || []).filter(Boolean).forEach(id => { if (on) picked.add(id); else picked.delete(id); });
  return picked;
}

/** Chuỗi kết quả thao tác hàng loạt, luôn có số lượng. */
export function bulkResultText(verb, doneCount, failedCount) {
  const ok = `${verb} ${doneCount} hồ sơ`;
  return failedCount ? `${ok}; ${failedCount} hồ sơ chưa được xử lý.` : `${ok}.`;
}

export function bulkRestoreText(count) {
  return {
    title: count === 1 ? 'Khôi phục 1 hồ sơ?' : `Khôi phục ${count} hồ sơ đã chọn?`,
    text: 'Hồ sơ quay lại danh sách. Sản phẩm từng gắn hồ sơ này sẽ được nối lại.',
  };
}

export function bulkDraftText(count) {
  return {
    title: `Tạo ${count} hồ sơ nháp?`,
    text: 'Mỗi sản phẩm được chọn sẽ có một hồ sơ nháp để điền. Nháp chưa duyệt thì AI chưa dùng.',
  };
}

export function bulkApproveText(count) {
  return {
    title: `Duyệt ${count} hồ sơ?`,
    text: 'Sau khi duyệt, AI dùng các hồ sơ này để tư vấn khách ngay.',
  };
}
