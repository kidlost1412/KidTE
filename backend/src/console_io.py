"""Ép cửa sổ console sang UTF-8.

Console Windows mặc định là cp1252. Mọi dòng log tiếng Việt — kể cả thông báo
lỗi do thư viện sinh ra — sẽ giết tiến trình bằng UnicodeEncodeError. Đã xảy ra
thật khi công cụ đồng bộ in tên thiết bị âm thanh không mở được.
"""
import sys


def force_utf8_console():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
