// Model choices come from Google's models.list for the key being used.
export const CUSTOM_MODEL = '__custom__';

export function geminiModelOptions(models = [], selected = '') {
  const out = [['', 'Tự động chọn model và chuyển khi quá tải']];
  const seen = new Set(['']);
  for (const row of models) {
    if (!row.id || seen.has(row.id)) continue;
    seen.add(row.id);
    const label = row.id === 'gemini-flash-latest' ? 'Flash mới nhất (Google tự đổi phiên bản)'
      : `${row.label || row.id}${row.label && row.label !== row.id ? ` · ${row.id}` : ''}`;
    out.push([row.id, label]);
  }
  if (selected && !seen.has(selected)) out.push([selected, `${selected} (đang lưu; chưa xác nhận trong danh sách)`]);
  out.push([CUSTOM_MODEL, 'Nhập tên model khác…']);
  return out;
}

export function chosenGeminiModel(value, custom) {
  return String(value === CUSTOM_MODEL ? custom || '' : value || '').trim();
}
