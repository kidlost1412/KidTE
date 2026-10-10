// Mô hình thuần (không DOM) cho panel "Sổ tay tình huống bán hàng". api = (url, {method, body}) => Promise.
// Giới hạn khớp backend/src/sales_playbook.py — backend vẫn là nơi kiểm tra cuối cùng.
export const ENDPOINT = '/api/workspace/sales-playbook';
export const LIMITS = {entries: 30, id: 40, tinhHuong: 100, huongXuLy: 500, phrases: 16, phrase: 40, items: 6, duKien: 160, cam: 120};
const ID_RE = /^[a-z0-9_]{1,40}$/;

export const SERVER_ERRORS = {
  playbook_entries_invalid: 'Dữ liệu sổ tay không đúng dạng.',
  playbook_entry_invalid: 'Có một mục sai dạng.',
  playbook_too_many_entries: `Tối đa ${LIMITS.entries} tình huống.`,
  playbook_id_invalid: 'Mã tình huống chỉ gồm chữ thường không dấu, số, gạch dưới (tối đa 40 ký tự).',
  playbook_id_duplicate: 'Có hai tình huống trùng mã.',
  playbook_tinh_huong_invalid: 'Tình huống cần ghi ngắn gọn (tối đa 100 ký tự).',
  playbook_dau_hieu_invalid: 'Mỗi tình huống cần ít nhất một dấu hiệu (tối đa 16 cụm, mỗi cụm 40 ký tự).',
  playbook_huong_xu_ly_invalid: 'Hướng xử lý cần có và tối đa 500 ký tự.',
  playbook_du_kien_invalid: 'Dữ kiện được dùng: tối đa 6 dòng, mỗi dòng 160 ký tự.',
  playbook_cam_invalid: 'Điều cấm: tối đa 6 dòng, mỗi dòng 120 ký tự.',
  playbook_too_large: 'Sổ tay quá dài — hãy rút gọn bớt.',
  invalid_body_size: 'Sổ tay quá dài — hãy rút gọn bớt.',
};

export const lines = text => [...new Set(String(text || '').split(/\r?\n|;/).map(x => x.trim()).filter(Boolean))];

export function slugify(text) {
  return String(text || '').normalize('NFD').replace(/[̀-ͯ]/g, '').replace(/đ/gi, 'd').toLowerCase()
    .replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '').slice(0, LIMITS.id);
}

// Mục từ API -> hàng soạn thảo (các danh sách thành văn bản mỗi dòng một ý).
export function toRow(entry = {}) {
  const join = list => (Array.isArray(list) ? list : []).join('\n');
  return {id: entry.id || '', tinhHuong: entry.tinh_huong || '', dauHieu: join(entry.dau_hieu),
    huongXuLy: entry.huong_xu_ly || '', duKien: join(entry.du_kien_duoc_dung), cam: join(entry.cam)};
}

export function toEntry(row) {
  return {id: String(row.id || '').trim(), tinh_huong: String(row.tinhHuong || '').trim(),
    dau_hieu: lines(row.dauHieu), huong_xu_ly: String(row.huongXuLy || '').trim(),
    du_kien_duoc_dung: lines(row.duKien), cam: lines(row.cam)};
}

export const blankRow = () => toRow({});

export function playbookModel(entries) {
  const rows = (Array.isArray(entries) ? entries : []).map(toRow);
  return {rows, count: rows.length, canAdd: rows.length < LIMITS.entries};
}

// Lỗi từng dòng (cùng luật backend) để báo ngay trước khi gửi.
export function validateRows(rows) {
  const errors = [];
  const seen = new Set();
  const add = (index, field, message) => errors.push({index, field, message});
  if (rows.length > LIMITS.entries) add(-1, 'entries', SERVER_ERRORS.playbook_too_many_entries);
  rows.forEach((row, index) => {
    const e = toEntry(row);
    if (!ID_RE.test(e.id)) add(index, 'id', SERVER_ERRORS.playbook_id_invalid);
    else if (seen.has(e.id)) add(index, 'id', SERVER_ERRORS.playbook_id_duplicate);
    seen.add(e.id);
    if (!e.tinh_huong || e.tinh_huong.length > LIMITS.tinhHuong) add(index, 'tinhHuong', SERVER_ERRORS.playbook_tinh_huong_invalid);
    if (!e.dau_hieu.length || e.dau_hieu.length > LIMITS.phrases || e.dau_hieu.some(x => x.length > LIMITS.phrase)) add(index, 'dauHieu', SERVER_ERRORS.playbook_dau_hieu_invalid);
    if (!e.huong_xu_ly || e.huong_xu_ly.length > LIMITS.huongXuLy) add(index, 'huongXuLy', SERVER_ERRORS.playbook_huong_xu_ly_invalid);
    if (e.du_kien_duoc_dung.length > LIMITS.items || e.du_kien_duoc_dung.some(x => x.length > LIMITS.duKien)) add(index, 'duKien', SERVER_ERRORS.playbook_du_kien_invalid);
    if (e.cam.length > LIMITS.items || e.cam.some(x => x.length > LIMITS.cam)) add(index, 'cam', SERVER_ERRORS.playbook_cam_invalid);
  });
  return errors;
}

export function errorText(error) {
  const code = String((error && (error.code || error.message)) || '');
  return SERVER_ERRORS[code] || 'Không lưu được sổ tay. Kiểm tra lại các ô rồi thử lại.';
}

export async function loadPlaybook(api) {
  const out = await api(ENDPOINT);
  return playbookModel(out && out.entries);
}

export async function savePlaybook(api, rows) {
  const errors = validateRows(rows);
  if (errors.length) { const first = errors[0]; const err = new Error(first.message); err.validation = errors; throw err; }
  const out = await api(ENDPOINT, {method: 'PUT', body: {entries: rows.map(toEntry)}});
  return playbookModel(out && out.entries);
}
