// Mô hình thuần (không DOM) cho tab "Gắn hồ sơ cho sản phẩm": gợi ý tự động,
// nhận hàng loạt cặp chắc chắn, hồ sơ nháp và điều kiện duyệt.

export const TIERS = {
  chac_chan: {label: 'Chắc chắn', tone: 'ok', rank: 0},
  can_xem: {label: 'Cần xem', tone: 'warn', rank: 1},
  khong_co: {label: 'Không có gợi ý', tone: '', rank: 2},
};

export const SAVE_NOTE = 'Bấm “Lưu ánh xạ” = cho AI được dùng hồ sơ này để tư vấn khách ngay.';

export function tierInfo(tier) { return TIERS[tier] || TIERS.khong_co; }

/** Kết quả auto-match -> map product_id -> kết quả (bỏ hàng không có hồ sơ gợi ý). */
export function suggestionIndex(results) {
  const index = new Map();
  for (const item of results || []) {
    if (item && item.product_id && item.profile_id && item.tier !== 'khong_co') index.set(item.product_id, item);
  }
  return index;
}

/** Cặp "chắc chắn" mà người dùng chưa sửa/bỏ qua. Khác quy cách không bao giờ lọt vào đây. */
export function sureAssignments(results, {skipped = new Set(), pending = new Map(), mapped = new Set()} = {}) {
  const out = {};
  for (const item of results || []) {
    if (!item || item.tier !== 'chac_chan' || !item.profile_id || item.size_match === false || item.is_combo) continue;
    if (skipped.has(item.product_id) || pending.has(item.product_id) || mapped.has(item.product_id)) continue;
    out[item.product_id] = item.profile_id;
  }
  return out;
}

export function countTiers(results) {
  const counts = {chac_chan: 0, can_xem: 0, khong_co: 0};
  for (const item of results || []) if (item && counts[item.tier] !== undefined) counts[item.tier] += 1;
  return counts;
}

/** Mục nội dung hồ sơ cần điền, theo đúng thứ tự hiển thị. */
export const COMPLETENESS_FIELDS = [
  ['thanh_phan', 'Thành phần'], ['cong_dung', 'Công dụng'], ['huong_dan', 'Hướng dẫn'], ['luu_y', 'Lưu ý'],
];

export function completenessChips(completeness) {
  const c = completeness || {};
  return COMPLETENESS_FIELDS.map(([key, label]) => ({key, label, done: c[key] === true}));
}

/** Đủ điều kiện duyệt: có tên + (công dụng hoặc hướng dẫn) — khớp backend `is_approvable`. */
export function canApprove(profile) {
  if (!profile) return false;
  if (typeof profile.approvable === 'boolean') return profile.approvable;
  const c = profile.completeness || {};
  return Boolean(profile.name) && (c.cong_dung === true || c.huong_dan === true);
}

export function missingForApproval(profile) {
  if (canApprove(profile)) return '';
  return 'Cần điền Công dụng hoặc Hướng dẫn sử dụng trước khi duyệt.';
}

/** Hồ sơ nháp (tự tạo, chưa duyệt) xếp theo mức hoàn thiện. */
export function draftRows(profiles) {
  return (profiles || []).filter(p => p && p.created_local && p.status === 'draft')
    .map(p => ({...p, chips: completenessChips(p.completeness), ready: canApprove(p),
      filled: completenessChips(p.completeness).filter(c => c.done).length}))
    .sort((a, b) => Number(b.ready) - Number(a.ready) || b.filled - a.filled || String(a.name).localeCompare(String(b.name), 'vi'));
}

/** Nội dung gửi /api/profiles/approve: mỗi hồ sơ nháp kèm listing nguồn (nếu có). */
export function approvalItems(rows, selected) {
  return rows.filter(r => selected.has(r.id) && r.ready)
    .map(r => ({profile_id: r.id, product_id: r.draft_for || ''}));
}

/** Chuỗi báo kết quả duyệt hàng loạt. */
export function approvalSummary(response) {
  const results = (response && response.results) || [];
  const ok = results.filter(r => r.ok).length;
  const blocked = results.length - ok;
  return blocked ? `Đã duyệt ${ok}, ${blocked} hồ sơ chưa đủ nội dung nên bị chặn.` : `Đã duyệt ${ok} hồ sơ. AI dùng ngay.`;
}
