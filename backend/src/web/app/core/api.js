export class ApiError extends Error {
  constructor(code, detail = '', status = 0, payload = null) {
    super(detail || code);
    this.name = 'ApiError';
    this.code = code;
    this.detail = detail;
    this.status = status;
    this.payload = payload;
  }
}

// Phạm vi trang: mọi request không tự truyền signal đều gắn vào trang đang mở.
// Rời trang -> abort + tăng generation; phản hồi muộn bị bỏ (không toast/render lỗi "aborted").
let pageScope = null;
export function beginPageScope() {
  endPageScope();
  const scope = {controller: new AbortController(), generation: (pageScope?.generation || 0) + 1};
  pageScope = scope;
  return scope;
}
export function endPageScope(scope = pageScope) {
  if (!scope || scope.ended) return;
  scope.ended = true;
  scope.controller.abort();
  if (pageScope === scope) pageScope = null;
}
export const pageGeneration = () => pageScope?.generation || 0;

export async function request(url, options = {}) {
  const config = {...options};
  // Chỉ request đọc gắn vào trang; lệnh ghi (POST/PATCH/PUT/DELETE) chạy tới cùng dù rời trang.
  const method = String(config.method || 'GET').toUpperCase();
  const scope = config.signal || !['GET', 'HEAD'].includes(method) ? null : pageScope;
  if (scope) config.signal = scope.controller.signal;
  const dropped = () => scope && scope.ended;
  const never = () => new Promise(() => {}); // trang đã đóng: không ai chờ kết quả này nữa
  const headers = {Accept: 'application/json', ...(config.headers || {})};
  const body = config.body;
  if (body !== undefined && body !== null && typeof body !== 'string' && !(body instanceof Blob) && !(body instanceof FormData) && !(body instanceof ArrayBuffer)) {
    config.body = JSON.stringify(body);
    headers['Content-Type'] = 'application/json';
  }
  config.headers = headers;
  const controller = new AbortController();
  const callerSignal = config.signal;
  const timeoutMs = config.timeoutMs ?? 15000;
  delete config.timeoutMs;
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) throw new ApiError('timeout_invalid');
  let timedOut = false;
  const abort = () => controller.abort();
  if (callerSignal?.aborted) abort();
  else callerSignal?.addEventListener('abort', abort, {once: true});
  config.signal = controller.signal;
  const timer = setTimeout(() => {timedOut = true; controller.abort();}, timeoutMs);
  try {
  let response;
  try {
    response = await fetch(url, config);
  } catch (error) {
    if (dropped() && !timedOut) return never();
    if (controller.signal.aborted) throw new ApiError(timedOut ? 'timeout' : 'aborted');
    throw new ApiError('network_error');
  }
  let payload = null;
  try { payload = await response.json(); } catch (_) {
    if (dropped() && !timedOut) return never();
    if (controller.signal.aborted) throw new ApiError(timedOut ? 'timeout' : 'aborted');
    throw new ApiError(response.ok ? 'payload_invalid' : `http_${response.status}`, '', response.status);
  }
  if (dropped()) return never();
  if (!response.ok || (payload && payload.ok === false)) {
    throw new ApiError((payload && payload.error) || `http_${response.status}`, (payload && payload.message) || '', response.status, payload);
  }
  return payload;
  } finally {
    clearTimeout(timer);
    callerSignal?.removeEventListener('abort', abort);
  }
}

export const get = (url, opts) => request(url, opts);
export const patch = (url, body, opts = {}) => request(url, {...opts, method: 'PATCH', body: body ?? {}});

// Mã lỗi backend -> câu người vận hành hiểu được và biết phải làm gì.
const MESSAGES = {
  transcription_worker_package_unavailable: 'Chưa cài bộ chép lời. Vào Cài đặt → Cập nhật → Bộ chép lời trên máy để tải.',
  missing_ffmpeg: 'Chưa có công cụ video. Vào Cài đặt → Cập nhật → Công cụ video để tải.',
  missing_ffprobe: 'Chưa có công cụ video. Vào Cài đặt → Cập nhật → Công cụ video để tải.',
  component_in_use: 'Gói đang được một tính năng khác sử dụng. Gỡ tính năng phụ thuộc trước.',
  component_not_installed: 'Tính năng chưa được cài trên máy.',
  update_source_mode_disabled: 'Cập nhật chưa bật ở bản mã nguồn. Hãy dùng bản cài đã đóng gói.',
  update_trust_not_configured: 'Bản ứng dụng chưa có khóa xác minh của nhà phát hành.',
  update_check_required: 'Hãy kiểm tra bản phát hành trước khi chọn gói.',
  update_job_busy: 'Một thao tác cập nhật đang chạy. Chờ thao tác hoàn tất.',
  update_restart_required: 'Có cập nhật đang chờ. Đóng và mở lại Kid TE hoặc hủy cập nhật chờ trước.',
  update_launcher_upgrade_required: 'Bản cài hiện tại cần được nâng cấp một lần bằng bộ cài Kid TE mới nhất trước khi nhận cập nhật nhỏ (ứng dụng / tiện ích / thư viện).',
  split_generation_incomplete: 'Bộ cập nhật phải có đủ thư viện (runtime) và ứng dụng (app).',
  runtime_api_mismatch: 'Ứng dụng và thư viện không cùng phiên bản giao tiếp; không thể cập nhật riêng lẻ.',
  rollback_not_available: 'Chưa có bộ chương trình trước để quay lại.',
  catalog_http_404: 'Chưa có danh mục cập nhật cho kênh này trên GitHub.',
  catalog_network_error: 'Không kết nối được GitHub. Kiểm tra mạng rồi thử lại.',
  catalog_backoff: 'GitHub vừa từ chối kết nối. Chờ một phút rồi kiểm tra lại.',
  package_network_error: 'Tải gói bị gián đoạn. Thử lại để tiếp tục tải.',
  invalid_signature: 'Chữ ký bản phát hành không hợp lệ. Gói chưa được cài.',
  unknown_signing_key: 'Bản phát hành được ký bằng khóa chưa được ứng dụng tin cậy.',
  catalog_rollback: 'Danh sách bản cập nhật trên GitHub cũ hơn bản máy này đã ghi nhận. Bấm "Thử lại"; nếu vẫn báo, hãy cài lại bằng bộ cài Kid TE mới nhất (dữ liệu được giữ nguyên).',
  live_confirm_required: 'Đang LIVE — cần xác nhận trước khi khởi động lại.',
  update_nothing_to_apply: 'Chưa có bản nào đã tải để áp dụng.',
  launcher_missing: 'Không tìm thấy KidTELauncher.exe. Hãy đóng và mở lại Kid TE bằng biểu tượng Kid TE.',
  restart_helper_failed: 'Không tự khởi động lại được. Hãy đóng và mở lại Kid TE bằng biểu tượng Kid TE.',
  catalog_expired_or_not_yet_valid: 'Danh mục đã hết hạn hoặc giờ máy chưa đúng. Kiểm tra ngày giờ và bản phát hành.',
  package_digest_mismatch: 'Nội dung gói không khớp bản phát hành. Gói chưa được cài.',
  key_rejected: 'Khoá DeepSeek bị từ chối (sai hoặc hết hạn). Kiểm tra lại khoá rồi dán lại.',
  key_http_401: 'Khoá DeepSeek bị từ chối (sai hoặc hết hạn). Kiểm tra lại khoá rồi dán lại.',
  key_http_402: 'Tài khoản DeepSeek đã hết số dư.',
  key_http_5xx: 'DeepSeek đang lỗi, thử lại sau.',
  key_tls_untrusted: 'Máy chưa tin chứng chỉ bảo mật của api.deepseek.com — thường do phần mềm diệt virus/proxy quét HTTPS hoặc Windows thiếu chứng chỉ gốc.',
  key_tls_error: 'Lỗi bắt tay bảo mật (TLS) với api.deepseek.com. Kiểm tra diệt virus/proxy và giờ hệ thống.',
  key_dns: 'Không phân giải được tên api.deepseek.com (DNS). Kiểm tra mạng.',
  key_proxy: 'Proxy/VPN của máy chặn kết nối tới api.deepseek.com.',
  key_timeout: 'api.deepseek.com phản hồi quá chậm. Kiểm tra mạng rồi thử lại.',
  key_offline: 'Không kết nối được api.deepseek.com. Kiểm tra mạng.',
  maintenance_required: 'Còn phiên LIVE hoạt động. Dừng phiên và tác vụ ghi trước khi vào bảo trì để khôi phục.',
  timeout: 'Kid TE phản hồi chậm. Kiểm tra trạng thái thao tác trước khi thử lại.',
  network_error: 'Mất kết nối với Kid TE. Kiểm tra ứng dụng còn đang chạy rồi thử lại.',
  revision_conflict: 'Dữ liệu vừa được thay đổi ở nơi khác. Hãy tải lại để xem bản mới nhất trước khi lưu.',
  local_only: 'Chỉ thao tác được từ ứng dụng trên máy này.',
  internal_error: 'Kid TE gặp lỗi nội bộ. Thử lại; nếu lặp lại hãy tạo báo cáo hỗ trợ ở mục Hệ thống.',
  saved_catalog_required: 'Chưa có danh mục sản phẩm đã quét. Mở phiên LIVE để tiện ích quét giỏ hàng trước.',
  video_extension_not_supported: 'Định dạng video chưa hỗ trợ. Dùng MP4, MOV, MKV, WebM, AVI hoặc M4V.',
  invalid_video_size: 'Kích thước video không hợp lệ.',
  upload_offset_mismatch: 'Điểm tiếp nối bị lệch. Chọn lại đúng video đó để tiếp tục tải.',
  upload_part_truncated_on_disk: 'Phần đã tải trên máy không còn nguyên vẹn. Hãy tải lại video này từ đầu.',
  upload_size_mismatch: 'Phần đã tải không khớp kích thước. Hãy tải lại từ đầu.',
  job_not_ready_for_review: 'Video này chưa phân tích xong hoặc đã phát hành.',
  job_already_published: 'Video này đã được phát hành trước đó.',
  video_review_has_pending_events: 'Còn mốc chưa duyệt. Duyệt hoặc loại hết các mốc trước khi phát hành.',
  event_time_out_of_range: 'Có mốc nằm ngoài thời lượng video.',
  duplicate_event_time: 'Có hai mốc trùng đúng một thời điểm.',
  events_required: 'Cần ít nhất một mốc ghim.',
  pinned_product_required: 'Cần ít nhất một đoạn có ghim sản phẩm.',
  event_product_not_in_saved_catalog: 'Có sản phẩm không còn trong danh mục đã quét.',
  alternate_interval_too_short: 'Chu kỳ luân phiên phải từ 10 giây trở lên.',
  alternate_product_must_be_different: 'Sản phẩm luân phiên phải khác sản phẩm chính.',
  video_in_use_choose_replacement: 'Video đang được dùng cho LIVE. Dừng đồng bộ trước khi thao tác.',
  video_source_not_available: 'Không tìm thấy file video gốc trên máy.',
  audio_device_not_allowed: 'Thiết bị âm thanh này không được phép dùng.',
  audio_device_restart_failed: 'Không khởi động lại được bộ đồng bộ với thiết bị mới; đã giữ thiết bị cũ.',
  model_provider_not_configurable: 'Nhà cung cấp AI hiện tại không cho phép đổi cấu hình.',
  image_format_not_supported: 'Chỉ nhận ảnh PNG, JPEG hoặc WebP.',
  image_size_invalid: 'Ảnh phải nhỏ hơn 10 MB.',
  image_content_invalid: 'File ảnh bị hỏng hoặc không đúng định dạng.',
  correction_fields_required: 'Thiếu nội dung hoặc lý do sửa.',
  workspace_record_not_found: 'Bản ghi không còn tồn tại (có thể đã bị xóa).',
  dose_liters_invalid: 'Số lít phải từ 0 đến 10.000.',
  publish_draft_missing: 'Không tìm thấy bản duyệt đã lưu.',
  aborted: 'Đã huỷ.',
  product_required: 'Chọn một sản phẩm để thử.',
  comment_required: 'Gõ câu khách hỏi để thử.',
  download_in_progress: 'Đang tải một model khác — chờ xong hoặc bấm Dừng trước.',
  model_not_installed: 'Model này chưa tải về máy.',
  model_unknown: 'Không có model này.',
  backup_parts_required: 'Chọn ít nhất một mục để sao lưu.',
  backup_busy: 'Có thao tác sao lưu hoặc xóa đang chạy. Chờ hoàn tất rồi thử lại.',
  maintenance_job_busy: 'Có tác vụ đang chạy. Chờ hoàn tất rồi thử lại.',
  backup_nothing_to_save: 'Các mục đã chọn chưa có dữ liệu nào trên máy.',
  backup_parts_not_in_backup: 'Bản sao lưu này không chứa mục đã chọn.',
  backup_not_found: 'Không tìm thấy bản sao lưu này (có thể đã bị dọn).',
  prompt_too_long: 'Lời nhắc quá dài (tối đa 60.000 ký tự).',
  processor_not_found: 'Không có bộ xử lý này.',
  captcha_admin_declined: 'Windows chưa cấp quyền Administrator cho solver. Hãy chấp nhận hộp thoại UAC rồi khởi chạy lại.',
  captcha_other_solver_running: 'Đã có solver độc lập khác đang chạy. Hãy đóng solver đó trước khi khởi chạy bản trong Kid TE.',
  captcha_solver_missing: 'Gói CAPTCHA chưa được cài. Vào Cài đặt → Cập nhật và chọn gói CAPTCHA để cài.',
  captcha_solver_package_invalid: 'Gói CAPTCHA đã chọn bị thiếu hoặc sai nội dung. Vào Cài đặt → Cập nhật để cài lại gói CAPTCHA.',
  captcha_key_missing: 'Chưa lưu SadCaptcha API key.',
  captcha_key_rejected: 'SadCaptcha đã từ chối key. Hãy kiểm tra số lượt và lưu key còn hiệu lực.',
  captcha_stop_requires_admin: 'Cần quyền Administrator để dừng solver. Hãy đóng tiến trình solver từ cửa sổ Admin.',
  captcha_telegram_chat_is_bot: 'Chat ID đang là ID của chính bot. Hãy nhập Chat ID của cuộc trò chuyện với anh hoặc nhóm nhận cảnh báo.',
  captcha_telegram_chat_invalid: 'Chat ID phải là số. Mở cuộc trò chuyện với bot và lấy Chat ID của anh hoặc nhóm.',
  captcha_telegram_chat_unreachable: 'Bot chưa gửi được tới Chat ID này. Hãy mở bot, gửi /start và kiểm tra lại Chat ID.',
  captcha_telegram_network_error: 'Không kết nối được Telegram. Kiểm tra mạng rồi gửi tin thử lại.',
  captcha_telegram_missing: 'Chưa lưu cấu hình Telegram.',
  captcha_telegram_unreadable: 'Không đọc được bot token đã lưu trên tài khoản Windows này. Hãy nhập lại token và Chat ID.',
  captcha_telegram_rejected: 'Telegram không xác nhận tin nhắn thử.',
  captcha_monitor_missing: 'Thiếu script giám sát hoặc FFmpeg trong bộ cài.',
  captcha_monitor_start_failed: 'Không khởi chạy được tiến trình giám sát Telegram.',
};

export function friendlyError(error) {
  if (!error) return 'Có lỗi không xác định.';
  const code = String(error.code || error.message || error);
  if (MESSAGES[code]) return MESSAGES[code];
  const prefix = code.split(':')[0];
  if (prefix === 'invalid_timeline') return 'Kịch bản chưa hợp lệ: ' + code.slice(prefix.length + 1);
  if (prefix === 'video_fields_not_editable' || prefix === 'correction_fields_not_allowed') return 'Trường này không được phép sửa.';
  if (MESSAGES[prefix]) return MESSAGES[prefix];
  if (error.detail) return error.detail;
  // Máy chủ đã trả sẵn một câu tiếng Việt (vd kiểm tra cài đặt) thì hiện nguyên câu đó.
  if (/\s/.test(code) && /[^\x00-\x7f]/.test(code)) return code;
  return `Không thực hiện được (${code}).`;
}
