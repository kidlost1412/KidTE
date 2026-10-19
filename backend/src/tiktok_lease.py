"""Lease "người gọi TikTok" giữa nhiều trình duyệt/profile cùng chạy tiện ích.

Mỗi profile có chrome.storage riêng nên cổng một-tab của tiện ích không thấy
nhau; backend giữ lease chung. Chỉ trong bộ nhớ (mất khi tắt app), có khoá vì
server là ThreadingHTTPServer. Holder hiện tại giữ tới khi hết hạn hoặc tự release.
"""
import threading
import time

LEASE_TTL_SECONDS = 30.0
ACTIONS = ("claim", "renew", "release")


def _clean_id(value, limit=80):
    text = str(value or "").strip()
    return text[:limit] if text and all(ch.isalnum() or ch in "-_:." for ch in text) else ""


class TikTokCallerLease:
    def __init__(self, ttl=LEASE_TTL_SECONDS, clock=time.monotonic):
        self.ttl = float(ttl)
        self.clock = clock
        self._lock = threading.Lock()
        self._holder = None  # (profile_id, tab_id)
        self._expires = 0.0

    def handle(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("invalid_payload")
        action = payload.get("action")
        if action not in ACTIONS:
            raise ValueError("invalid_lease_action")
        profile_id = _clean_id(payload.get("profile_id"))
        tab_id = _clean_id(payload.get("tab_id"), 32)
        if not profile_id or not tab_id:
            raise ValueError("invalid_lease_holder")
        me = (profile_id, tab_id)
        with self._lock:
            now = self.clock()
            if self._holder is not None and now >= self._expires:
                self._holder = None
            if action == "release":
                released = self._holder == me
                if released:
                    self._holder = None
                return {"ok": True, "granted": False, "released": released}
            # claim và renew cùng luật: trống/hết hạn/chính mình thì cấp (gia hạn).
            # Cùng profile khác tab: cổng một-tab của tiện ích đã chọn tab này
            # (tab cũ đóng/rời LIVE) nên cho chuyển ngay, không chờ hết hạn.
            if self._holder is None or self._holder[0] == profile_id:
                self._holder = me
                self._expires = now + self.ttl
                return {"ok": True, "granted": True, "ttl_ms": int(self.ttl * 1000)}
            return {"ok": True, "granted": False,
                    "retry_after_ms": max(0, int((self._expires - now) * 1000))}


LEASE = TikTokCallerLease()
