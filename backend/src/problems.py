"""Một chỗ duy nhất để ghi SỰ CỐ thật vào nhật ký (kèm dấu vết lỗi).

Trước đây lỗi của máy chủ chỉ ``print`` ra console — bản cài đặt không có
console nên mọi lỗi đều mất. Nhật ký chỉ còn lại dòng bật/tắt ứng dụng.
"""
from __future__ import annotations

import threading

_state = {"logger": None}
_lock = threading.Lock()


def attach(logger):
    with _lock:
        _state["logger"] = logger


def report_problem(event, message, exc=None, *, severity="error"):
    text = str(message or event)
    if exc is not None:
        text = f"{text} — {type(exc).__name__}: {exc}"
    try:
        print(f"[kid-te] {event}: {text}")
    except (UnicodeEncodeError, OSError):
        pass
    logger = _state["logger"]
    if logger is None:
        return
    try:
        log = logger.warning if severity == "warning" else logger.info if severity == "info" else logger.error
        log(text[:2000], extra={"event": event},
            exc_info=(type(exc), exc, exc.__traceback__) if exc is not None else None)
    except Exception:
        pass  # ghi nhật ký không bao giờ được làm hỏng việc chính
