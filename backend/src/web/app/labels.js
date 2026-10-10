// Nhãn tiếng Việt cho mọi mã trạng thái backend. Một chỗ duy nhất.

export const CONVERSATION_STATUS = {
  ai_sent: {label: 'AI đã trả lời', tone: 'ai', short: 'AI đã trả lời'},
  ai_unconfirmed: {label: 'AI trả lời · chưa xác nhận gửi', tone: 'ai', short: 'AI chưa xác nhận'},
  ai_failed: {label: 'AI gửi lỗi', tone: 'danger', short: 'Gửi lỗi'},
  manual_replied: {label: 'Chủ shop đã trả lời', tone: 'ok', short: 'Shop đã trả lời'},
  unanswered: {label: 'Bị bỏ sót', tone: 'danger', short: 'Bị bỏ sót'},
  waiting: {label: 'Đang chờ trả lời', tone: 'warn', short: 'Đang chờ'},
  skipped_safely: {label: 'Bỏ qua an toàn', tone: 'info', short: 'Bỏ qua an toàn'},
  ignored: {label: 'Không cần trả lời', tone: '', short: 'Không cần'},
  answered: {label: 'Đã xử lý', tone: 'ok', short: 'Đã xử lý'},
  skipped: {label: 'Đã bỏ qua', tone: '', short: 'Đã bỏ qua'},
};

export const OPERATOR_STATUS_CHOICES = [
  ['', '— Giữ trạng thái hệ thống —'],
  ['manual_replied', 'Chủ shop đã trả lời'],
  ['answered', 'Đã xử lý'],
  ['skipped', 'Đã bỏ qua'],
  ['unanswered', 'Bị bỏ sót (cần xử lý)'],
];

export const RESPONSE_QUALITY = [
  ['unreviewed', 'Chưa đánh giá'],
  ['good', 'Trả lời đúng'],
  ['needs_fix', 'Cần sửa'],
  ['wrong', 'Trả lời sai'],
];

export const DELIVERY = {
  sent: {label: 'Đã gửi', tone: 'ok'},
  failed: {label: 'Gửi lỗi', tone: 'danger'},
  cancelled: {label: 'Đã huỷ', tone: 'warn'},
  unconfirmed: {label: 'Chưa xác nhận', tone: 'warn'},
  none: {label: 'Chưa gửi', tone: ''},
};

export const JOB_STATE = {
  uploading: {label: 'Đang tải lên', tone: 'info'},
  queued: {label: 'Chờ phân tích', tone: 'info'},
  analyzing: {label: 'Đang phân tích', tone: 'info'},
  review: {label: 'Chờ duyệt', tone: 'warn'},
  published: {label: 'Đã phát hành', tone: 'ok'},
  error: {label: 'Lỗi phân tích', tone: 'danger'},
  interrupted: {label: 'Bị gián đoạn', tone: 'danger'},
};

export const JOB_PHASE = {
  checking_video: 'Kiểm tra file video',
  extracting_audio: 'Tách âm thanh',
  building_fingerprint: 'Tạo dấu vân tay âm thanh',
  transcribing: 'Chuyển lời nói thành chữ',
  matching_products: 'Tìm sản phẩm được nhắc tới',
  owner_review: 'Chờ chủ shop duyệt',
  complete: 'Hoàn tất',
  queued: 'Đang xếp hàng',
  failed: 'Thất bại',
  interrupted: 'Bị gián đoạn khi đang chạy',
};

export const JOB_PHASE_ORDER = ['checking_video', 'extracting_audio', 'building_fingerprint', 'transcribing', 'matching_products'];

export const REVIEW_STATUS = {
  pending: {label: 'Chưa duyệt', tone: 'warn'},
  approved: {label: 'Đã duyệt', tone: 'ok'},
  edited: {label: 'Đã sửa', tone: 'info'},
  rejected: {label: 'Đã loại', tone: 'danger'},
};

export const CONFIDENCE = {
  high: {label: 'Chắc chắn cao', tone: 'ok'},
  medium: {label: 'Cần xem lại', tone: 'warn'},
  low: {label: 'Không chắc', tone: 'danger'},
};

export const RESOURCE_NAME = {
  profiles: 'Hồ sơ sản phẩm', products: 'Sản phẩm', videos: 'Video', 'video-jobs': 'Duyệt video',
  conversations: 'Bình luận', 'raw-events': 'Bình luận', sessions: 'Phiên LIVE', interactions: 'Tương tác',
  customers: 'Khách hàng', facts: 'Thông tin khách', 'product-images': 'Ảnh sản phẩm', mapping: 'Ánh xạ',
};

export function lookup(table, key, fallback) {
  return table[key] || {label: fallback || key || 'Chưa xác định', tone: ''};
}
