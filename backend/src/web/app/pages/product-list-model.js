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

/** Hộp xác nhận đưa sản phẩm vào thùng rác (không xoá trên sàn). */
export function trashProductsText(count, all = false) {
  return {
    title: all ? `Xoá tất cả ${count} sản phẩm khỏi KidTE?` : (count === 1 ? 'Xoá 1 sản phẩm khỏi KidTE?' : `Xoá ${count} sản phẩm đã chọn khỏi KidTE?`),
    text: `${count} sản phẩm sẽ vào Thùng rác: AI không dùng nữa, tự đồng bộ giỏ không đưa lại. Có thể Khôi phục. Bấm quét giỏ bằng tay trên tiện ích sẽ lấy lại đủ như TikTok. Không xoá trên sàn.`,
  };
}

/** Hộp xác nhận xoá vĩnh viễn khỏi thùng rác. */
export function purgeProductsText(count) {
  return {
    title: count === 1 ? 'Xoá vĩnh viễn 1 sản phẩm?' : `Xoá vĩnh viễn ${count} sản phẩm?`,
    text: `${count} sản phẩm bị gỡ khỏi danh mục đã lưu và khỏi Thùng rác, không khôi phục được. Quét giỏ bằng tay sau này vẫn lấy lại nếu TikTok còn bán. Hồ sơ kỹ thuật vẫn giữ.`,
  };
}

/** Hộp xác nhận khôi phục sản phẩm từ thùng rác. */
export function restoreProductsText(count) {
  return {
    title: count === 1 ? 'Khôi phục 1 sản phẩm?' : `Khôi phục ${count} sản phẩm?`,
    text: 'Sản phẩm quay lại danh sách và AI dùng lại nếu còn trong giỏ đã đồng bộ.',
  };
}

// ---------- Giỏ theo tài khoản LIVE ----------
/** Nhãn một giỏ trong ô chọn: tên + số sản phẩm + (đang LIVE). */
export function cartLabel(cart) {
  if (!cart) return '';
  const tags = [`${Number(cart.product_count) || 0} SP`];
  if (cart.live) tags.push('đang LIVE');
  return `${cart.name || cart.key} (${tags.join(' · ')})`;
}

/** [[key, nhãn]] cho ô chọn giỏ; giỏ đang LIVE lên đầu. */
export function cartOptions(state) {
  const carts = (state && state.carts) || [];
  return [...carts].sort((a, b) => Number(Boolean(b.live)) - Number(Boolean(a.live)))
    .map(c => [c.key, cartLabel(c)]);
}

/** Giỏ hiện trong ô chọn = giỏ danh sách đang hiển thị (active); thiếu thì selected, rồi giỏ đầu. */
export function pickerCartKey(state) {
  const carts = (state && state.carts) || [];
  const has = key => key && carts.some(c => c.key === key);
  if (has(state && state.active)) return state.active;
  if (has(state && state.selected)) return state.selected;
  return carts.length ? carts[0].key : '';
}

/** Giỏ AI + tự ghim đang dùng: giỏ chủ shop chọn; chưa chọn thì giỏ của tài khoản đang LIVE. */
export function cartLockNote(state) {
  if (!state || !(state.carts || []).length) return '';
  const key = state.active || state.live;
  const active = (state.carts || []).find(c => c.key === key);
  const name = state.active_name || (active ? active.name : key);
  if (!name) return '';
  const why = state.selected_explicit ? ' (giỏ anh/chị đã chọn)'
    : state.live ? ' (giỏ của tài khoản đang LIVE)' : '';
  return `Buổi LIVE đang dùng giỏ «${name}»${why}.`;
}

/**
 * Cảnh báo khi đang LIVE mà AI dùng giỏ không thuộc tài khoản đang LIVE (chủ shop đã chọn giỏ khác).
 * Trả null nếu không lệch. Không tự đổi giỏ: chỉ gợi ý nút "Dùng giỏ của tài khoản LIVE".
 */
export function liveMismatchWarning(state) {
  if (!state || !state.live || !state.active || state.active === state.live) return null;
  const carts = state.carts || [];
  const live = carts.find(c => c.key === state.live) || {};
  const active = carts.find(c => c.key === state.active) || {};
  const liveName = live.unique_id ? '@' + live.unique_id : (state.live_name || live.name || state.live);
  const activeName = state.active_name || active.name || state.active;
  return {
    title: `Đang LIVE ${liveName} nhưng AI đang dùng giỏ «${activeName}»`,
    text: 'AI tư vấn và tự ghim theo giỏ đang chọn. Nếu đây không phải giỏ của buổi LIVE này, bấm nút bên cạnh.',
    action: 'Dùng giỏ của tài khoản LIVE',
  };
}

/** Hộp xác nhận "Khôi phục bản trước" của một giỏ (bản trước khi quét tay ghi đè). */
export function restoreCartText(cart) {
  const name = (cart && (cart.name || cart.key)) || '';
  const count = Number(cart && cart.history_count) || 0;
  const at = String((cart && cart.history_at) || '').replace('T', ' ').slice(0, 16);
  return {
    title: `Khôi phục bản trước của giỏ ${name}?`,
    text: `Giỏ quay về danh sách sản phẩm đã lưu${at ? ` lúc ${at}` : ''} (còn ${count} bản trước). ` +
      'Bản hiện tại được giữ lại: bấm "Khôi phục bản trước" lần nữa để quay lại.',
  };
}

/** Hộp xác nhận xoá giỏ. */
export function deleteCartText(cart) {
  const name = (cart && (cart.name || cart.key)) || '';
  const count = Number(cart && cart.product_count) || 0;
  return {
    title: `Xoá giỏ ${name}?`,
    text: `Giỏ có ${count} sản phẩm. Xoá giỏ không xoá hồ sơ kỹ thuật hay gắn hồ sơ (dùng chung theo mã sản phẩm). Quét lại ở tài khoản này sẽ tạo lại giỏ.`,
  };
}
