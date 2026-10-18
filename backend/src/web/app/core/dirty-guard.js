// Hỏi "bỏ thay đổi chưa lưu?" trước khi đóng hộp/ngăn kéo. Thuần, không phụ thuộc DOM.
export const DISCARD_TITLE = 'Bạn có thay đổi chưa lưu, vẫn đóng?';
export const DISCARD_TEXT = 'Đóng bây giờ thì phần vừa nhập sẽ mất.';

/**
 * Tạo hàm dismiss: không dirty -> đóng ngay; dirty -> hỏi (ask trả Promise<boolean>), đồng ý mới đóng.
 * Đang hỏi thì bỏ qua các lần gọi thêm (Esc + bấm nền liên tiếp).
 */
export function makeGuardedDismiss({isDirty, ask, proceed}) {
  let asking = false;
  return function dismiss() {
    let dirty = false;
    try { dirty = typeof isDirty === 'function' && Boolean(isDirty()); } catch (_) { dirty = true; }
    if (!dirty) { proceed(); return Promise.resolve(true); }
    if (asking) return Promise.resolve(false);
    asking = true;
    return Promise.resolve(ask()).then(ok => { if (ok) proceed(); return Boolean(ok); }).finally(() => { asking = false; });
  };
}

/**
 * Theo dõi "form đã khác bản đã tải/đã lưu" bằng ảnh chụp chuỗi. read() trả giá trị bất kỳ (JSON được).
 * Chưa gọi reset() lần nào (dữ liệu chưa tải xong) thì coi là sạch.
 */
export function createDirtyTracker(read) {
  let base = null;
  const snap = () => { try { return JSON.stringify(read()); } catch (_) { return null; } };
  return {
    reset() { base = snap(); },
    isDirty() { return base !== null && snap() !== base; },
  };
}
