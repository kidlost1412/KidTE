"""Ghép tiện ích Chrome lần đầu (trust-on-first-use) theo từng sàn.

Tiện ích Kid TE cài dạng "unpacked" nên ID khác nhau giữa các máy: không thể
ghim ID cố định trong mã. Thay vào đó ID của tiện ích đầu tiên gửi heartbeat
hợp lệ cho một sàn được ghi nhận; từ đó request chrome-extension:// có ID khác
cho sàn đó bị từ chối. Chủ shop bấm "Ghép lại tiện ích" để xoá và ghép lại.
"""
import json
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path

PLATFORMS = ("tiktok", "shopee")
EXTENSION_ID_RE = re.compile(r"^[a-z0-9]{1,64}$")
BLOCK_RECENT_S = 15 * 60

log = logging.getLogger("kid-te.core")


def extension_id(origin):
    """ID tiện ích từ Origin chrome-extension://<id>; rỗng nếu không hợp lệ."""
    origin = str(origin or "")
    prefix = "chrome-extension://"
    if not origin.startswith(prefix):
        return ""
    value = origin[len(prefix):].rstrip("/").lower()
    return value if EXTENSION_ID_RE.match(value) else ""


def short_id(value):
    value = str(value or "")
    return value if len(value) <= 12 else f"{value[:6]}…{value[-4:]}"


class ExtensionPairing:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._blocked = {}          # platform -> {"id", "at", "count"}
        self._logged = set()        # (platform, id) đã ghi log một lần
        self._state = self._load()

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        paired = data.get("paired") if isinstance(data, dict) else None
        if not isinstance(paired, dict):
            return {}
        result = {}
        for platform, entry in paired.items():
            if (platform in PLATFORMS and isinstance(entry, dict)
                    and EXTENSION_ID_RE.match(str(entry.get("id") or ""))):
                result[platform] = {"id": entry["id"], "paired_at": float(entry.get("paired_at") or 0)}
        return result

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps({"schema_version": 1, "paired": self._state},
                         ensure_ascii=False, indent=2).encode("utf-8")
        fd, tmp = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def check(self, platform, origin, heartbeat=False):
        """True nếu request từ Origin này được phép cho sàn ``platform``.

        Chưa ghép: heartbeat hợp lệ ghép luôn; request khác vẫn cho qua (tiện
        ích có thể gọi trước heartbeat đầu tiên) nhưng không ghép.
        """
        if platform not in PLATFORMS:
            return True
        ext = extension_id(origin)
        with self._lock:
            paired = (self._state.get(platform) or {}).get("id")
            if ext and paired == ext:
                return True
            if not paired and ext:
                if heartbeat:
                    self._state[platform] = {"id": ext, "paired_at": time.time()}
                    try:
                        self._save()
                    except OSError as exc:
                        log.warning("extension_pairing_save_failed %s", type(exc).__name__)
                    log.info("extension_paired platform=%s id=%s", platform, short_id(ext))
                return True
            entry = self._blocked.get(platform)
            if entry and entry.get("id") == ext:
                entry["at"] = time.time()
                entry["count"] += 1
            else:
                self._blocked[platform] = {"id": ext, "at": time.time(), "count": 1}
            if (platform, ext) not in self._logged:
                self._logged.add((platform, ext))
                log.warning("extension_not_paired platform=%s id=%s paired=%s",
                            platform, short_id(ext) or "?", short_id(paired))
            return False

    def reset(self, platform):
        if platform not in PLATFORMS:
            raise ValueError("invalid_platform")
        with self._lock:
            removed = self._state.pop(platform, None)
            self._blocked.pop(platform, None)
            self._logged = {item for item in self._logged if item[0] != platform}
            self._save()
        log.info("extension_pairing_reset platform=%s", platform)
        return {"ok": True, "platform": platform, "removed": bool(removed)}

    def status(self):
        now = time.time()
        with self._lock:
            result = {}
            for platform in PLATFORMS:
                paired = self._state.get(platform) or {}
                blocked = self._blocked.get(platform)
                recent = None
                if blocked and now - blocked["at"] <= BLOCK_RECENT_S:
                    recent = {"id_short": short_id(blocked["id"]) or "?",
                              "age_s": round(now - blocked["at"]), "count": blocked["count"]}
                result[platform] = {"paired": bool(paired.get("id")),
                                    "id_short": short_id(paired.get("id")),
                                    "paired_at": paired.get("paired_at") or None,
                                    "blocked_recent": recent}
            return result
