"""Lease "người gọi TikTok" giữa nhiều trình duyệt/profile cùng chạy tiện ích.

Mỗi profile có chrome.storage riêng nên cổng một-tab của tiện ích không thấy
nhau; backend giữ lease chung. Chỉ trong bộ nhớ (mất khi tắt app), có khoá vì
server là ThreadingHTTPServer.

3.11.11: lease theo TÀI KHOẢN TikTok. Khoá = owner_key (uid:/sec:/handle:, giống
giỏ multi-cart); thiếu thì dùng khoá chung "" như cũ. Hai tài khoản khác nhau
không bao giờ chặn nhau. Cùng tài khoản: tab đang LIVE thật (live=True, có
room_id hợp lệ) luôn giành được lease từ holder chưa LIVE.
"""
import threading
import time

LEASE_TTL_SECONDS = 30.0
ACTIONS = ("claim", "renew", "release")


def _clean_id(value, limit=80):
    text = str(value or "").strip()
    return text[:limit] if text and all(ch.isalnum() or ch in "-_:." for ch in text) else ""


def _clean_owner_key(value):
    text = str(value or "").strip()
    if not text or len(text) > 260 or ":" not in text:
        return ""
    if any(ch.isspace() or ord(ch) < 32 for ch in text):
        return ""
    return text


class TikTokCallerLease:
    def __init__(self, ttl=LEASE_TTL_SECONDS, clock=time.monotonic):
        self.ttl = float(ttl)
        self.clock = clock
        self._lock = threading.Lock()
        # owner_key -> {"holder": (profile_id, tab_id), "expires": float, "live": bool}
        self._leases = {}

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
        key = _clean_owner_key(payload.get("owner_key"))
        live = payload.get("live") is True
        with self._lock:
            now = self.clock()
            for stale in [k for k, v in self._leases.items() if now >= v["expires"]]:
                del self._leases[stale]
            if action == "release":
                mine = [k for k, v in self._leases.items() if v["holder"] == me]
                for k in mine:
                    del self._leases[k]
                return {"ok": True, "granted": False, "released": bool(mine)}
            current = self._leases.get(key)
            # Cấp khi: trống/hết hạn, chính mình hoặc cùng profile (cổng một-tab của
            # tiện ích đã chọn tab này), hoặc mình đang LIVE thật còn holder thì chưa.
            if (current is None or current["holder"][0] == profile_id
                    or (live and not current["live"])):
                # Một tab chỉ giữ một khoá: bỏ khoá cũ (vd. từ khoá chung sang uid:).
                for k in [k for k, v in self._leases.items() if v["holder"] == me and k != key]:
                    del self._leases[k]
                self._leases[key] = {"holder": me, "expires": now + self.ttl, "live": live}
                return {"ok": True, "granted": True, "ttl_ms": int(self.ttl * 1000),
                        "owner_key": key, "preempted": bool(current and current["holder"][0] != profile_id)}
            return {"ok": True, "granted": False, "owner_key": key, "holder_live": current["live"],
                    "retry_after_ms": max(0, int((current["expires"] - now) * 1000))}


LEASE = TikTokCallerLease()
