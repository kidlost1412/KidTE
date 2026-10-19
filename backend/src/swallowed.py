"""Ghi lại lỗi đã bỏ qua có chủ đích (thay cho `except Exception: pass` im lặng).

Không bao giờ ném lỗi ra ngoài. `once=True` chỉ ghi lần đầu cho mỗi (event, key) để không
làm đầy log khi lỗi lặp lại trong vòng lặp nóng.
"""
import logging
import threading

_LOGGER = logging.getLogger("kid-te.core")
_SEEN = set()
_SEEN_LIMIT = 2000
_LOCK = threading.Lock()


def note(event, message="", *, level=logging.DEBUG, once=False, key="", exc_info=True):
    try:
        if once:
            mark = (event, str(key))
            with _LOCK:
                if mark in _SEEN:
                    return
                if len(_SEEN) >= _SEEN_LIMIT:
                    _SEEN.clear()
                _SEEN.add(mark)
        _LOGGER.log(level, message or event, exc_info=exc_info, extra={"event": event})
    except Exception:  # noqa: BLE001 - ghi log không được làm hỏng luồng gọi
        return
