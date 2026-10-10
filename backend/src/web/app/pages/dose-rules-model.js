// Mô hình thuần (không DOM) cho panel "Quy tắc liều": D01/D04. api = (url, {method, body}) => Promise.
export const STATUS_CHIP = {
  candidate: {label: 'Chờ duyệt', tone: 'warn'},
  approved: {label: 'Đã duyệt', tone: 'ok'},
  rejected: {label: 'Đã loại', tone: 'danger'},
};
export const ONLY_APPROVED_WARNING = 'Chỉ quy tắc ĐÃ DUYỆT mới được AI dùng để tính liều. Quy tắc chờ duyệt hoặc đã loại không bao giờ được dùng.';

export function ruleText(rule) {
  const per = rule.per_volume != null ? ` cho ${Number(rule.per_volume)} ${rule.per_unit || 'lít'}` : '';
  return `${Number(rule.amount)}${rule.unit || ''}${per}`;
}

export function doseRuleRow(rule) {
  const status = STATUS_CHIP[rule.status] ? rule.status : 'candidate';
  const meta = [rule.method, rule.crop, rule.stage].filter(Boolean).join(' · ');
  return {
    id: rule.rule_id, text: ruleText(rule), meta, status,
    chip: STATUS_CHIP[status],
    source: [rule.source_doc, rule.source_page ? `tr. ${rule.source_page}` : ''].filter(Boolean).join(' · ') || 'không rõ nguồn',
    needsReview: status === 'candidate' || rule.needs_review === true,
    canReview: status === 'candidate',
  };
}

export function doseRulesModel(rules) {
  const rows = (Array.isArray(rules) ? rules : []).map(doseRuleRow);
  const count = s => rows.filter(r => r.status === s).length;
  return {rows, approved: count('approved'), candidate: count('candidate'), rejected: count('rejected'),
    warning: ONLY_APPROVED_WARNING, noApproved: count('approved') === 0};
}

export async function loadDoseRules(api, sku) {
  const out = await api(`/api/workspace/dose-rules?sku=${encodeURIComponent(sku)}`);
  return doseRulesModel(out && out.rules);
}

export async function reviewDoseRule(api, ruleId, decision) {
  if (decision !== 'approve' && decision !== 'reject') throw new Error('decision_invalid');
  return api(`/api/workspace/dose-rules/${encodeURIComponent(ruleId)}/review`, {method: 'POST', body: {decision}});
}

export async function importLabel(api, {sku, text, sourceDoc}) {
  if (!sku || !String(text || '').trim()) throw new Error('Cần nhập nội dung nhãn.');
  const out = await api('/api/workspace/dose-rules/import-label',
    {method: 'POST', body: {sku, text, source_doc: sourceDoc || ''}});
  const n = Number((out && (out.candidates ?? out.imported)) || 0);
  return {imported: n, needsReview: !!(out && out.needs_review),
    message: n ? `Đã nhập ${n} tỷ lệ ở trạng thái CHỜ DUYỆT. Hệ thống không tự chọn tỷ lệ nào — hãy duyệt từng dòng.`
      : 'Không đọc được tỷ lệ pha nào từ nhãn này.'};
}
