// Trạng thái DeepSeek từ dash.ai. "deepseek_balance_checking" (backend đang
// kiểm số dư ở nền, chưa có kết quả) là trung tính, không phải lỗi.
// alerts (dash.live_alerts): AI hỏng giữa LIVE (O3) thắng cả cache "sẵn sàng" của số dư.
export function aiStatus(ai, alerts) {
  const s = ai || {};
  const alert = aiAlert(alerts);
  if (alert) return {kind: 'danger', checking: false, text: alert.message, alert};
  if (s.deepseek_available) return {kind: 'ok', checking: false};
  if (s.reason === 'deepseek_balance_checking') return {kind: 'warn', checking: true, text: 'Đang kiểm tra số dư DeepSeek…'};
  return {kind: 'danger', checking: false};
}

export function aiAlert(alerts) {
  return (Array.isArray(alerts) ? alerts : []).find(a => a && String(a.code || '').startsWith('ai_')) || null;
}

// Báo động khi LIVE để hiện băng đỏ: [{tone, title, text}] — lỗi (đỏ) trước, cảnh báo sau.
export function liveAlertBanners(alerts) {
  return (Array.isArray(alerts) ? alerts : []).filter(a => a && a.message).map(a => ({
    code: String(a.code || ''),
    tone: a.severity === 'error' ? 'danger' : 'warn',
    title: a.message,
    text: a.action || '',
  })).sort((x, y) => (x.tone === 'danger' ? 0 : 1) - (y.tone === 'danger' ? 0 : 1));
}
