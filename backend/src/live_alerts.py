"""Báo động khi LIVE (O3/O4): AI hỏng liên tiếp, không nhận được bình luận TikTok/Shopee.

Chỉ giữ trạng thái trong bộ nhớ: mất khi tắt app là đúng (LIVE mới thì đếm lại).
"""
import threading
import time

# Số lỗi AI liên tiếp trước khi báo (hết tiền/khoá sai thì báo ngay).
AI_FAIL_STREAK = 3
# Mặc định: đang LIVE mà 5 phút không có bình luận mới thì báo.
DEFAULT_COMMENT_SILENCE_S = 300

PLATFORM_LABELS = {"tiktok": "TikTok", "shopee": "Shopee"}


def _platform_prefix(platforms):
    names = [PLATFORM_LABELS.get(p, str(p)) for p in platforms if p]
    return (" + ".join(names) + ": ") if names else ""

_FATAL = {
    "ai_out_of_balance": ("Tài khoản DeepSeek đã hết tiền — AI đang KHÔNG trả lời khách.",
                          "Nạp thêm tiền DeepSeek, rồi bấm Kiểm tra lại ở Cài đặt → AI."),
    "ai_key_rejected": ("Khoá DeepSeek bị từ chối (sai hoặc đã thu hồi) — AI đang KHÔNG trả lời khách.",
                        "Vào Cài đặt → AI, dán lại khoá DeepSeek đúng rồi bấm Lưu."),
    "ai_key_missing": ("Chưa có khoá DeepSeek — AI không trả lời được.",
                       "Vào Cài đặt → AI, dán khoá DeepSeek rồi bấm Lưu."),
}
_STREAK = {
    "ai_timeout": ("AI trả lời quá chậm/hết giờ {n} lần liền — khách đang không được trả lời.",
                   "Kiểm tra mạng Internet; nếu mạng ổn có thể DeepSeek đang quá tải, đợi vài phút."),
    "ai_network": ("Không kết nối được tới DeepSeek {n} lần liền — khách đang không được trả lời.",
                   "Kiểm tra mạng Internet/Wi-Fi, tường lửa hoặc proxy của máy."),
    "ai_rate_limited": ("DeepSeek báo quá hạn lượt {n} lần liền — AI tạm không trả lời.",
                        "Đợi 1–2 phút; nếu lặp lại, giảm số bình luận AI trả lời hoặc liên hệ hỗ trợ."),
    "ai_failing": ("AI lỗi {n} lần liền — khách đang không được trả lời.",
                   "Mở Nhật ký để xem lỗi; thử Kiểm tra lại AI ở Cài đặt → AI."),
}


def classify_ai_failure(reasons, http_status=None):
    """Mã lỗi từ danh sách lý do bỏ qua của model (+ mã HTTP nếu có)."""
    text = " ".join(str(r) for r in reasons or ()).lower()
    if http_status == 402 or "insufficient_balance" in text:
        return "ai_out_of_balance"
    if http_status in (401, 403) or "authenticationerror" in text or "permissiondenied" in text \
            or "key_rejected" in text:
        return "ai_key_rejected"
    if "key_missing" in text:
        return "ai_key_missing"
    if http_status == 429 or "ratelimit" in text:
        return "ai_rate_limited"
    if "timeout" in text or "turndeadline" in text:
        return "ai_timeout"
    if "connection" in text or "offline" in text or "dns" in text or "proxy" in text:
        return "ai_network"
    return "ai_failing"


class LiveAlerts:
    def __init__(self, clock=time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self._streak = 0
        self._last_code = ""
        self._since = 0.0
        self._platforms = []

    # --- O3 -------------------------------------------------------------------
    def ai_failed(self, reasons, http_status=None, platform=None):
        code = classify_ai_failure(reasons, http_status)
        with self._lock:
            if not self._streak:
                self._since = self._clock()
                self._platforms = []
            if platform and platform not in self._platforms:
                self._platforms.append(platform)
            self._streak += 1
            self._last_code = code
            return self._ai_alert_locked()

    def ai_succeeded(self):
        with self._lock:
            self._streak, self._last_code, self._since = 0, "", 0.0
            self._platforms = []

    def _ai_alert_locked(self):
        code, n = self._last_code, self._streak
        if not n:
            return None
        if code in _FATAL:
            message, action = _FATAL[code]
        elif n >= AI_FAIL_STREAK:
            message, action = _STREAK.get(code, _STREAK["ai_failing"])
            message = message.format(n=n)
        else:
            return None
        return {"code": code, "severity": "error", "message": _platform_prefix(self._platforms) + message,
                "action": action, "count": n, "since": self._since, "platforms": list(self._platforms)}

    def ai_alert(self):
        with self._lock:
            return self._ai_alert_locked()

    # --- O4 -------------------------------------------------------------------
    def comment_alert(self, heartbeat, silence_s=DEFAULT_COMMENT_SILENCE_S, live_started_at=0.0,
                      platform="tiktok"):
        """Đang LIVE mà bộ thu báo lỗi, hoặc ≥ silence_s không có bình luận mới → cảnh báo."""
        label = PLATFORM_LABELS.get(platform, "TikTok")
        heartbeat = heartbeat or {}
        now = self._clock()
        if heartbeat.get("live_running") is not True:
            return None
        if now - float(heartbeat.get("received_at") or 0) > 30:
            return None          # tiện ích mất kết nối: đã có trạng thái riêng
        collector = heartbeat.get("collector")
        if not isinstance(collector, dict):
            return None          # tiện ích cũ chưa gửi trạng thái thu
        if collector.get("bridge") is False or collector.get("ok") is False:
            return {"code": "comments_collector_error", "severity": "error", "platform": platform,
                    "message": f"Tiện ích {label} báo lỗi bộ thu bình luận — AI có thể không thấy khách hỏi.",
                    "action": f"Tải lại trang {label} LIVE (F5); nếu vẫn lỗi, kiểm tra/ cập nhật tiện ích Kid TE."}
        last_ms = float(collector.get("last_comment_at_ms") or 0)
        last = last_ms / 1000 if last_ms else 0.0
        reference = max(last, float(live_started_at or 0))
        if not reference:
            return None          # chưa biết LIVE bắt đầu lúc nào: chưa đủ căn cứ
        idle = now - reference
        if idle < max(60, int(silence_s or DEFAULT_COMMENT_SILENCE_S)):
            return None
        minutes = int(idle // 60)
        return {"code": "comments_silent", "severity": "warning", "minutes": minutes, "platform": platform,
                "message": f"Không nhận được bình luận {label} {minutes} phút — kiểm tra tiện ích/tải lại trang.",
                "action": f"Mở tab {label} LIVE, tải lại trang (F5) và xem tiện ích Kid TE còn bật không."}
