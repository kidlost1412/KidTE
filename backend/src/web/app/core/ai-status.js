// Trạng thái DeepSeek từ dash.ai. "deepseek_balance_checking" (backend đang
// kiểm số dư ở nền, chưa có kết quả) là trung tính, không phải lỗi.
export function aiStatus(ai) {
  const s = ai || {};
  if (s.deepseek_available) return {kind: 'ok', checking: false};
  if (s.reason === 'deepseek_balance_checking') return {kind: 'warn', checking: true, text: 'Đang kiểm tra số dư DeepSeek…'};
  return {kind: 'danger', checking: false};
}
