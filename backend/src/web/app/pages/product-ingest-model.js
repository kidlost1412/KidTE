// Mô hình thuần (không DOM) cho ngăn kéo "Thêm sản phẩm bằng AI":
// kết quả AI -> các dòng sửa được, đánh dấu chỗ cần xem, và dữ liệu gửi lưu nháp.

export const CONFIDENCE_LOW = 0.6;

export const FIELD_DEFS = [
  ['ten_san_pham', 'Tên sản phẩm', 'text'],
  ['nha_san_xuat', 'Nhà sản xuất', 'text'],
  ['quy_cach_dong_goi', 'Quy cách đóng gói', 'text'],
  ['thanh_phan_dang_ky', 'Thành phần đăng ký (mỗi dòng: tên: hàm lượng)', 'pairs'],
  ['cong_dung', 'Công dụng (mỗi dòng một ý)', 'list'],
  ['doi_tuong_cay_trong', 'Cây trồng phù hợp (mỗi dòng một loại)', 'list'],
  ['huong_dan_su_dung', 'Cách dùng & liều lượng (mỗi dòng một hướng dẫn)', 'list'],
  ['luu_y', 'Lưu ý an toàn', 'textarea'],
  ['mo_ta_ngan', 'Mô tả ngắn', 'textarea'],
  ['phan_khuc', 'Nhóm công dụng', 'text'],
  ['hieu_qua_chinh', 'Hiệu quả chính', 'textarea'],
];

export const PROVIDER_NOTE = {
  gemini: 'Đọc bằng Gemini',
  deepseek: 'Đọc bằng DeepSeek (chỉ chữ)',
};

export function pairsToLines(pairs) {
  return Object.entries(pairs || {}).map(([name, amount]) => (amount ? `${name}: ${amount}` : name)).join('\n');
}

export function linesToPairs(text) {
  const out = {};
  for (const raw of String(text || '').split('\n')) {
    const line = raw.trim();
    if (!line) continue;
    const i = line.indexOf(':');
    const name = (i < 0 ? line : line.slice(0, i)).trim();
    if (name) out[name] = i < 0 ? '' : line.slice(i + 1).trim();
  }
  return out;
}

function toText(type, value) {
  if (value === null || value === undefined) return '';
  if (type === 'list') return (Array.isArray(value) ? value : [value]).join('\n');
  if (type === 'pairs') return pairsToLines(value);
  return String(value);
}

/** Nhãn nguồn dễ đọc từ mã nguồn (n1, n2...). */
export function sourceLabel(sources, id) {
  const hit = (sources || []).find(s => s.id === id);
  return hit ? hit.label : '';
}

/** Kết quả extract -> {rows, sources, thumbs, provider}. Mỗi dòng: text sửa được + cờ cần xem. */
export function buildReviewModel(result) {
  const r = result || {};
  const fields = r.fields || {};
  const conf = r.confidence || {};
  const uncertain = new Set(r.uncertain || []);
  const rows = FIELD_DEFS.map(([key, label, type]) => {
    const value = toText(type, fields[key]);
    const c = typeof conf[key] === 'number' ? conf[key] : 0;
    return {
      key, label, type, value, initial: value,
      confidence: c,
      empty: !value.trim(),
      uncertain: uncertain.has(key) || !value.trim() || c < CONFIDENCE_LOW,
      source: sourceLabel(r.sources, (r.nguon || {})[key]),
    };
  });
  const sources = r.sources || [];
  return {
    rows, sources,
    images: sources.filter(s => s.thumb),
    provider: r.provider || '', providerNote: PROVIDER_NOTE[r.provider] || '',
    filteredPrice: !!r.filtered_price,
    sessionId: r.session_id || '',
  };
}

/** Người dùng đã chạm vào dòng thì không còn "cần xem" (đã được người xác nhận). */
export function markEdited(row, value) {
  row.value = value;
  if (value !== row.initial) row.uncertain = false;
  row.empty = !value.trim();
}

export function uncertainCount(model) {
  return model.rows.filter(row => row.uncertain && !row.empty).length + model.rows.filter(row => row.empty).length;
}

export function canSaveDraft(model) {
  const name = model.rows.find(row => row.key === 'ten_san_pham');
  return !!name && name.value.trim().length >= 2;
}

/** Các dòng -> trường gửi lên server (không bao giờ có giá/khuyến mãi). */
export function fieldsPayload(model) {
  const out = {};
  for (const row of model.rows) {
    const text = row.value.trim();
    if (!text) continue;
    if (row.type === 'list') out[row.key] = text.split('\n').map(l => l.trim()).filter(Boolean);
    else if (row.type === 'pairs') out[row.key] = linesToPairs(text);
    else out[row.key] = text;
  }
  return out;
}

export function draftBody(model, {imageSourceId = '', sourceUrl = ''} = {}) {
  return {
    fields: fieldsPayload(model), session_id: model.sessionId,
    ...(imageSourceId ? {image: {source_id: imageSourceId}} : {}),
    ...(sourceUrl ? {source_url: sourceUrl} : {}),
  };
}

/** Ảnh mặc định: ảnh tải lên đầu tiên, nếu không có thì ảnh og:image. */
export function defaultImageSource(model) {
  const imgs = model.images || [];
  return (imgs.find(s => s.kind === 'image') || imgs[0] || {}).id || '';
}

export function suggestionLabel(s) {
  const pct = Math.round((s.score || 0) * 100);
  const size = s.size_match === false ? ' · LỆCH quy cách' : s.size_match === true ? ' · cùng quy cách' : '';
  return `Giỏ #${s.bag_index || '—'} · ${pct}%${size}`;
}

/** Thông báo gọn khi chưa có khoá Gemini mà đầu vào cần đọc ảnh. */
export function needsGeminiWarning(hasImages, status) {
  if (hasImages && !(status && status.gemini_configured)) {
    return 'Đọc ảnh cần khoá Gemini miễn phí. Vào Cài đặt → “Key Gemini (đọc ảnh/PDF)” → “Lấy key miễn phí”.';
  }
  if (!status || (!status.gemini_configured && !status.deepseek_configured)) {
    return 'Chưa có khoá AI nào (Gemini hoặc DeepSeek) ở Cài đặt.';
  }
  return '';
}

/** Kết quả extract -> danh sách mô hình duyệt, mỗi sản phẩm một mô hình (dùng chung nguồn/phiên/nhà cung cấp). */
export function buildProductsModels(result) {
  const r = result || {};
  const list = Array.isArray(r.products) && r.products.length ? r.products : [r];
  return list.map((product, index) => ({
    index,
    model: buildReviewModel({...product, provider: r.provider, session_id: r.session_id, sources: r.sources,
      filtered_price: product.filtered_price || (list.length === 1 && r.filtered_price)}),
    picked: true,
  }));
}

export function productName(entry) {
  const row = entry.model.rows.find(x => x.key === 'ten_san_pham');
  return (row && row.value.trim()) || 'Sản phẩm chưa có tên';
}

/** Sản phẩm được tích và đủ tên để lưu nháp. */
export function pickedForSave(entries) {
  return (entries || []).filter(e => e.picked && canSaveDraft(e.model));
}

export function saveLabel(entries) {
  const n = pickedForSave(entries).length;
  return n <= 1 ? 'Lưu nháp' : `Lưu nháp ${n} sản phẩm`;
}

/** Nhà cung cấp mặc định: Gemini khi đã có khoá, không thì DeepSeek, không có gì thì Gemini. */
export function defaultProvider(status) {
  const s = status || {};
  if (s.gemini_configured) return 'gemini';
  if (s.deepseek_configured) return 'deepseek';
  return 'gemini';
}

export const PROVIDER_OPTIONS = [
  ['gemini', 'Gemini (đọc ảnh và tài liệu)'],
  ['deepseek', 'DeepSeek (chỉ tài liệu/chữ)'],
];

export function providerHint(provider, status) {
  const s = status || {};
  if (provider === 'deepseek') {
    return s.deepseek_configured ? 'DeepSeek chỉ đọc chữ: ảnh nhãn và PDF scan bị tắt. Tệp PDF/Word có chữ vẫn đọc được.'
      : 'Chưa có khoá DeepSeek ở Cài đặt → Hệ thống.';
  }
  return s.gemini_configured ? '' : 'Chưa có khoá Gemini. Vào Cài đặt → “Key Gemini (đọc ảnh/PDF)”.';
}

export const imagesAllowed = provider => provider !== 'deepseek';

/** Cảnh báo trước khi gửi: tệp quá nặng so với giới hạn máy chủ. */
export const MAX_FILE_MB = 100;
export function oversizeMessage(files) {
  const big = (files || []).find(f => f.size > MAX_FILE_MB * 1048576);
  return big ? `Tệp “${big.name}” nặng hơn ${MAX_FILE_MB} MB nên không đọc được. Hãy nén PDF hoặc tách bớt trang.` : '';
}
