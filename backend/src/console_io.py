"""Ép cửa sổ console sang UTF-8.

Console Windows mặc định là cp1252. Mọi dòng log tiếng Việt — kể cả thông báo
lỗi do thư viện sinh ra — sẽ giết tiến trình bằng UnicodeEncodeError. Đã xảy ra
thật khi công cụ đồng bộ in tên thiết bị âm thanh không mở được.
"""
import os
import sys


class _NullWriter:
    encoding = "utf-8"

    def write(self, text):
        return len(text) if isinstance(text, str) else 0

    def flush(self):
        pass

    def isatty(self):
        return False

    def reconfigure(self, **_kwargs):
        pass


def ensure_std_streams(log_path=None):
    """Bản đóng gói console=False: sys.stdout/stderr là None. Gán luồng thay thế
    (file log nếu mở được, không thì bộ ghi rỗng) để print()/logging không lỗi."""
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is not None:
            continue
        stream = None
        if log_path:
            try:
                os.makedirs(os.path.dirname(os.fspath(log_path)), exist_ok=True)
                stream = open(log_path, "a", encoding="utf-8", errors="replace", buffering=1)
            except OSError:
                stream = None
        setattr(sys, name, stream or _NullWriter())
    if sys.stdin is None:
        sys.stdin = open(os.devnull, "r", encoding="utf-8")


def force_utf8_console():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
