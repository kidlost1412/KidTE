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


CONSOLE_LOG_MAX_BYTES = 5 * 1024 * 1024
CONSOLE_LOG_BACKUPS = 3


def rotate_file(path, *, backups=CONSOLE_LOG_BACKUPS):
    """console.log -> console.log.1 -> ... -> console.log.<backups> (bỏ bản cũ nhất)."""
    path = os.fspath(path)
    for index in range(backups - 1, 0, -1):
        older = f"{path}.{index}"
        if os.path.exists(older):
            os.replace(older, f"{path}.{index + 1}")
    if os.path.exists(path):
        os.replace(path, f"{path}.1")


class RotatingTextStream:
    """Luồng văn bản ghi vào tệp, tự xoay vòng khi vượt ``max_bytes`` (O6).

    stdout và stderr dùng CHUNG một đối tượng: Windows không cho đổi tên tệp
    đang mở bởi handle khác, nên chỉ được có một handle tới console.log.
    """
    encoding = "utf-8"

    def __init__(self, path, *, max_bytes=CONSOLE_LOG_MAX_BYTES, backups=CONSOLE_LOG_BACKUPS):
        import threading

        self.path = os.fspath(path)
        self.max_bytes = max_bytes
        self.backups = backups
        self._lock = threading.Lock()
        if os.path.exists(self.path) and os.path.getsize(self.path) >= max_bytes:
            try:
                rotate_file(self.path, backups=backups)
            except OSError:
                pass  # tệp đang bị chương trình khác giữ: ghi tiếp, không mất log
        self._stream = open(self.path, "a", encoding="utf-8", errors="replace", buffering=1)

    def write(self, text):
        if not isinstance(text, str):
            return 0
        with self._lock:
            written = self._stream.write(text)
            if self._stream.tell() >= self.max_bytes:
                self._rotate()
            return written

    def _rotate(self):
        self._stream.close()
        try:
            rotate_file(self.path, backups=self.backups)
        except OSError:
            pass  # không xoay được (tệp bị khoá): ghi tiếp vào tệp cũ, không mất log
        self._stream = open(self.path, "a", encoding="utf-8", errors="replace", buffering=1)

    def flush(self):
        with self._lock:
            self._stream.flush()

    def isatty(self):
        return False

    def reconfigure(self, **_kwargs):
        pass

    def fileno(self):
        return self._stream.fileno()


def ensure_std_streams(log_path=None):
    """Bản đóng gói console=False: sys.stdout/stderr là None. Gán luồng thay thế
    (file log xoay vòng nếu mở được, không thì bộ ghi rỗng) để print()/logging không lỗi."""
    shared = None
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is not None:
            continue
        if shared is None and log_path:
            try:
                os.makedirs(os.path.dirname(os.fspath(log_path)), exist_ok=True)
                shared = RotatingTextStream(log_path)
            except OSError:
                shared = None
        setattr(sys, name, shared or _NullWriter())
    if sys.stdin is None:
        sys.stdin = open(os.devnull, "r", encoding="utf-8")


def force_utf8_console():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def hide_child_consoles():
    """App chạy không có cửa sổ console: mọi tiến trình con kiểu console (tasklist,
    ffprobe, nvidia-smi, ...) sẽ bật một cửa sổ đen thoáng qua. Mặc định thêm
    CREATE_NO_WINDOW cho mọi subprocess không tự đặt cờ tạo cửa sổ."""
    import subprocess
    import sys
    if sys.platform != "win32" or getattr(subprocess.Popen, "_kidte_no_window", False):
        return
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    window_flags = no_window | getattr(subprocess, "CREATE_NEW_CONSOLE", 0x10) | 0x8  # DETACHED_PROCESS
    original_init = subprocess.Popen.__init__

    def __init__(self, *args, **kwargs):
        flags = kwargs.get("creationflags", 0) or 0
        if not flags & window_flags:
            kwargs["creationflags"] = flags | no_window
        original_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = __init__
    subprocess.Popen._kidte_no_window = True
