// Mô hình thuần (không DOM) cho đề xuất học lại: mã từng thay đổi, đếm số
// đề xuất đang giữ lại và danh sách gửi lên khi bấm "Áp dụng".
// Mã phải khớp với relearn_change_ids() trong backend/src/learning.py.

/** Mã từng thay đổi của một đề xuất, theo thứ tự hiển thị. */
export function changeIds(output) {
  const p = output || {};
  const ids = [];
  (p.items || []).forEach((entry, idx) => { if (entry && entry.action !== 'keep') ids.push(`item:${idx}`); });
  for (const id of new Set(p.retire_ids || [])) ids.push(`retire:${id}`);
  for (const style of p.personas || []) ids.push(`style:${style.persona_id}`);
  return ids;
}

/** Số đề xuất còn giữ lại (không bị bỏ). */
export function keptCount(ids, dropped) {
  return ids.filter(id => !dropped.has(id)).length;
}

/** Nhãn nút áp dụng: "Áp dụng 7/10 đề xuất". */
export function applyLabel(ids, dropped) {
  return `Áp dụng ${keptCount(ids, dropped)}/${ids.length} đề xuất`;
}

/** Danh sách mã gửi lên backend (chỉ những đề xuất còn giữ lại). */
export function acceptedPayload(ids, dropped) {
  return ids.filter(id => !dropped.has(id));
}

/** Trạng thái hiển thị của một dòng: đang chờ quyết định, hay đã áp dụng/bỏ sau khi lưu. */
export function rowChecked(id, {editable, dropped, applied}) {
  return editable ? !dropped.has(id) : applied.has(id);
}
