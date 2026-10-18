"""Đọc nhật ký cho giao diện: dịch sang tiếng Việt + gợi ý cách xử lý.

Không đọc nội dung bình luận của khách; mọi dòng đều qua ``redact`` để không lộ khoá.
"""
from __future__ import annotations

import json
from collections import deque
from pathlib import Path

from structured_log import redact

# sự kiện → (tên dễ hiểu, gợi ý xử lý). Không có gợi ý = chỉ để biết.
EVENT_TEXT = {
    "previous_run_ended_unexpectedly": ("Lần trước ứng dụng bị tắt đột ngột",
                                        "Thường do tắt máy/mất điện khi app đang mở. Dữ liệu đã lưu không mất. "
                                        "Lặp lại nhiều lần mà không rõ lý do thì gửi nhật ký cho kỹ thuật."),
    "startup_backup": ("Kiểm tra sao lưu khi mở ứng dụng", ""),
    "startup_backup_failed": ("Không tự sao lưu được khi mở ứng dụng",
                              "Vào Sao lưu → Sao lưu ngay. Vẫn lỗi thì xem Chẩn đoán (ổ đĩa đầy / bị khoá)."),
    "plugins_loaded": ("Đã nạp các bộ xử lý", ""),
    "plugin_invalid": ("Một bộ xử lý không nạp được", "Cài lại Kid TE bằng bộ cài mới nhất (dữ liệu giữ nguyên)."),
    "backend_ready": ("Ứng dụng đã mở xong", ""),
    "backend_stop_requested": ("Có yêu cầu tắt ứng dụng", ""),
    "backend_stopping": ("Ứng dụng đang tắt", ""),
    "backend_stopped": ("Ứng dụng đã tắt", ""),
    "desktop_ui_failed": ("Không mở được cửa sổ ứng dụng",
                          "Ứng dụng vẫn chạy — mở http://127.0.0.1:8793/app trong trình duyệt. "
                          "Xem Chẩn đoán → WebView2."),
    "service_start_ok": ("Đã bật dịch vụ đồng bộ video", ""),
    "service_start_failed": ("Không bật được dịch vụ đồng bộ video", "Xem Chẩn đoán; thử Khởi động lại ở Trợ lý tự động."),
    "service_stop_result": ("Đã tắt dịch vụ", ""),
    "service_restart_limit_reached": ("Dịch vụ lỗi liên tục nên bị tạm khoá 5 phút",
                                      "Kiểm tra thiết bị âm thanh ở Cài đặt → Âm thanh, rồi bật lại."),
    "service_crash_detected": ("Dịch vụ đồng bộ video bị tắt bất thường", "Bật lại ở Trợ lý tự động."),
    "service_reattached_after_restart": ("Đã nối lại dịch vụ đang chạy", ""),
    "service_stale_after_core_restart": ("Dọn dịch vụ cũ của lần chạy trước", ""),
    "safe_mode_changed": ("Đổi chế độ an toàn", ""),
    "api_error": ("Một thao tác bị lỗi bên trong ứng dụng",
                  "Thao tác vừa làm không thành công. Lặp lại nhiều lần thì bấm \"Sao chép cho kỹ thuật\"."),
    "ai_reply_failed": ("AI không trả lời được một bình luận", ""),
    "learning_failed": ("Một lượt AI tự học bị lỗi", "Thử lại ở Trí nhớ AI → Học lại; kiểm tra số dư DeepSeek."),
    "video_analysis_failed": ("Phân tích video bị lỗi", "Mở Thư viện video, bấm Thử lại. Xem Chẩn đoán → ffmpeg / model chép lời."),
    "whisper_gpu_fallback": ("Chép lời bằng GPU lỗi, đã chuyển sang CPU",
                             "Cập nhật driver NVIDIA rồi mở lại app. Video vẫn được chép lời, chỉ chậm hơn."),
    "customer_history_failed": ("Không đọc được lịch sử khách", "AI vẫn trả lời, chỉ thiếu lịch sử khách lần này."),
    "document_distill_failed": ("AI đọc tài liệu bị lỗi", "Kiểm tra khoá / số dư DeepSeek rồi bấm lại “AI đọc & rút kiến thức”."),
    "whisper_download_failed": ("Tải model chép lời bị lỗi", "Kiểm tra mạng rồi bấm Tải tiếp — phần đã tải được giữ."),
}

# Lý do AI im lặng → gợi ý cụ thể.
AI_HINTS = (
    ("AuthenticationError", "Khoá DeepSeek sai hoặc đã bị xoá → Cài đặt → nhập lại khoá."),
    ("insufficient", "DeepSeek hết tiền → nạp thêm ở platform.deepseek.com."),
    ("PaymentRequired", "DeepSeek hết tiền → nạp thêm ở platform.deepseek.com."),
    ("RateLimitError", "Gửi quá nhiều câu cùng lúc; tự hết sau vài giây."),
    ("Timeout", "DeepSeek phản hồi chậm hoặc mạng yếu. Lặp lại nhiều thì kiểm tra Internet."),
    ("APIConnectionError", "Máy mất mạng hoặc không tới được DeepSeek."),
    ("key_missing", "Chưa nhập khoá DeepSeek → Cài đặt."),
    ("model_json_invalid", "DeepSeek trả sai định dạng — hiếm gặp, tự hết."),
)

PROBLEM_SEVERITIES = {"warning", "error", "critical"}


def _humanize(event, message):
    title, hint = EVENT_TEXT.get(event, (event or "Sự kiện", ""))
    if event == "ai_reply_failed":
        hint = next((text for key, text in AI_HINTS if key.casefold() in message.casefold()),
                    "Câu đó không được gửi; xem trong Hộp thư → Bị bỏ sót.")
    return title, hint


def recent_logs(log_dir, *, component="", severity="", limit=200):
    """``severity``: "" (tất cả), "problems" (cảnh báo + lỗi), hoặc đúng một mức."""
    limit = max(1, min(int(limit), 500))
    component, severity = str(component).strip(), str(severity).strip().casefold()
    rows = deque(maxlen=limit)
    for path in sorted(Path(log_dir).glob("*.jsonl")):
        if component and path.stem != component:
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines[-2000:]:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue
            level = str(item.get("severity", "")).casefold()
            if severity == "problems":
                if level not in PROBLEM_SEVERITIES:
                    continue
            elif severity and level != severity:
                continue
            message = redact(item.get("message", ""))[:1000]
            title, hint = _humanize(item.get("event"), message)
            rows.append({
                "timestamp": item.get("timestamp"), "component": item.get("component") or path.stem,
                "severity": item.get("severity"), "event": item.get("event"),
                "title": title, "hint": hint, "message": message,
                "exception": redact(item.get("exception", ""))[-4000:],
            })
    return {"ok": True, "rows": sorted(rows, key=lambda item: str(item.get("timestamp") or ""), reverse=True)}
