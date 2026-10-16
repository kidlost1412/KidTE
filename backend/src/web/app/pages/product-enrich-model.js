// Mô hình thuần (không DOM) cho "Bổ sung bằng Google": dễ test bằng node --test.

/** Giá trị trường -> chữ hiển thị (danh sách: mỗi ý một dòng; thành phần: "tên: hàm lượng"). */
export function valueText(value) {
  if (value === null || value === undefined) return '';
  if (Array.isArray(value)) return value.map(String).join('\n');
  if (typeof value === 'object') return Object.entries(value).map(([k, v]) => (v ? `${k}: ${v}` : k)).join('\n');
  return String(value);
}

/** Đề xuất từ máy chủ -> các dòng để vẽ hộp duyệt (cũ → đề xuất + nguồn), mặc định chọn hết. */
export function proposalRows(proposal) {
  const fields = (proposal && proposal.fields) || {};
  return Object.entries(fields).map(([key, item]) => ({
    key,
    label: item.label || key,
    old: valueText(item.old) || '(đang trống)',
    value: valueText(item.value),
    sources: (item.sources || []).filter(s => s && /^https?:\/\//.test(s.url || '')).map(s => ({url: s.url, title: s.title || s.url})),
    general: !!item.general_sources,   // nguồn chung của cả câu trả lời, không gắn riêng mục này
    checked: true,
  })).filter(row => row.value && row.sources.length);
}

/** Các trường đã tích để gửi lên /enrich/apply. */
export function chosenFields(rows) {
  return rows.filter(r => r.checked).map(r => r.key);
}

/** Câu tóm tắt các trường bị bỏ (không tìm thấy / không có nguồn). */
export function droppedText(proposal) {
  const dropped = (proposal && proposal.dropped) || {};
  const keys = Object.keys(dropped);
  if (!keys.length) return '';
  return `Không điền ${keys.length} mục vì Google không có nguồn rõ ràng.`;
}

/** Chẩn đoán lần gọi gần nhất -> một dòng (khi kết quả rỗng). */
export function diagnosticText(proposal) {
  const d = proposal && proposal.diagnostic;
  if (!d) return '';
  return `Chẩn đoán: model ${d.model || '?'} · ${d.chunks || 0} nguồn Google · ${d.supports || 0} đoạn trích · ${d.queries || 0} lượt tìm · kết thúc: ${d.finish_reason || '?'}`;
}

/** Trạng thái chạy hàng loạt -> một dòng dễ hiểu. */
export function batchText(status) {
  if (!status || status.state === 'idle') return '';
  const {done = 0, total = 0, current = ''} = status;
  if (status.state === 'running') return `Đang tra ${done + 1}/${total}${current ? `: ${current}` : ''}…`;
  const okCount = (status.results || []).filter(r => r.ok && (r.fields || []).length).length;
  const failed = (status.results || []).filter(r => !r.ok).length;
  const head = status.state === 'cancelled' ? `Đã dừng sau ${done}/${total} hồ sơ.` : `Xong ${done}/${total} hồ sơ.`;
  if (!total) return 'Mọi hồ sơ đã đủ thông tin, không cần bổ sung.';
  return `${head} ${okCount} hồ sơ có đề xuất chờ anh/chị duyệt${failed ? `, ${failed} hồ sơ lỗi` : ''}.`;
}
