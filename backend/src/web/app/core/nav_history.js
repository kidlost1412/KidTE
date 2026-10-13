// Ngăn xếp lịch sử điều hướng trong ứng dụng (thuần, không DOM).
// Mỗi phần tử là một hash đầy đủ (vd "#video/library/abc"); phần tử cuối là trang đang xem.
const DEFAULT_HOME = '#overview';

export function normalizeHash(hash) {
  const value = String(hash || '');
  return value === '' || value === '#' || value === '#/' ? DEFAULT_HOME : value;
}

export function createHistoryStack({limit = 50} = {}) {
  const entries = [];

  return {
    /** Ghi nhận một trang mới. Trùng với trang đang xem thì bỏ qua. */
    visit(hash) {
      const value = normalizeHash(hash);
      if (entries[entries.length - 1] === value) return;
      entries.push(value);
      if (entries.length > limit) entries.splice(0, entries.length - limit);
    },
    /** Thay trang đang xem (đổi sub-tab/bản ghi không tạo mục lịch sử mới). */
    replaceCurrent(hash) {
      const value = normalizeHash(hash);
      if (!entries.length) { entries.push(value); return; }
      entries[entries.length - 1] = value;
    },
    /** Trang sẽ quay lại, hoặc null nếu không có. */
    previous() {
      return entries.length >= 2 ? entries[entries.length - 2] : null;
    },
    canBack() {
      return entries.length >= 2;
    },
    /** Bỏ trang đang xem sau khi đã quay lại; trả về trang mới đang xem. */
    pop() {
      if (entries.length < 2) return null;
      entries.pop();
      return entries[entries.length - 1];
    },
    current() {
      return entries.length ? entries[entries.length - 1] : null;
    },
    size() {
      return entries.length;
    },
    snapshot() {
      return entries.slice();
    },
  };
}
