"""Hộp thoại tiếng Việt cho launcher và lõi chạy đóng băng (không cửa sổ console).

Chỉ dùng thư viện chuẩn: launcher import được mà không kéo theo ``config``.
Mọi thông báo lỗi phải nói rõ người dùng nên làm gì và tệp nhật ký ở đâu.
"""
from __future__ import annotations

import os
import subprocess
import sys
import webbrowser
from pathlib import Path

WEBVIEW2_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
_MB_OK = 0x0
_MB_YESNO = 0x4
_MB_ICONERROR = 0x10
_MB_ICONWARNING = 0x30
_MB_ICONINFO = 0x40
_MB_TOPMOST = 0x40000
_MB_SETFOREGROUND = 0x10000
_IDYES = 6


def _message_box(text: str, title: str, flags: int) -> int | None:
    if os.name != "nt":
        return None
    try:
        import ctypes

        return int(ctypes.windll.user32.MessageBoxW(None, text, title,
                                                     flags | _MB_TOPMOST | _MB_SETFOREGROUND))
    except (AttributeError, OSError):
        return None


def open_folder(path) -> bool:
    """Mở thư mục (hoặc thư mục chứa tệp) trong Explorer; trả False nếu không mở được."""
    target = Path(path)
    try:
        if target.is_file():
            if os.name == "nt":
                subprocess.Popen(["explorer", "/select,", str(target)])
                return True
            target = target.parent
        if os.name == "nt":
            os.startfile(str(target))  # noqa: S606 - local folder chosen by the app
            return True
        return webbrowser.open(target.as_uri())
    except (OSError, ValueError):
        return False


def open_browser(url: str) -> bool:
    try:
        return bool(webbrowser.open(url, new=2))
    except Exception:  # noqa: BLE001 - webbrowser may raise many backend-specific errors
        return False


def show_error(title: str, body: str, *, log_path=None, detail: str = "") -> None:
    """Hộp thoại lỗi. Có ``log_path`` thì hỏi có mở thư mục nhật ký không."""
    text = body.strip()
    if detail:
        text += f"\n\nChi tiết kỹ thuật (gửi cho hỗ trợ nếu cần):\n{detail.strip()}"
    if log_path:
        text += f"\n\nNhật ký: {log_path}\n\nBấm Yes để mở thư mục nhật ký."
        answer = _message_box(text, title, _MB_YESNO | _MB_ICONERROR)
        if answer == _IDYES:
            open_folder(log_path)
        elif answer is not None:
            return
    else:
        answer = _message_box(text, title, _MB_OK | _MB_ICONERROR)
    if answer is None:
        _print(title, text)


def show_warning(title: str, body: str) -> None:
    if _message_box(body, title, _MB_OK | _MB_ICONWARNING) is None:
        _print(title, body)


def _print(title: str, text: str) -> None:
    try:
        print(f"[Kid TE] {title}: {text}", file=sys.stderr, flush=True)
    except (AttributeError, OSError, ValueError):
        pass


def describe_port_owners(owners) -> str:
    names = [f"{row.get('process') or 'không xác định'} (PID {row.get('pid')})"
             for row in owners or [] if isinstance(row, dict)]
    return ", ".join(names)


def port_busy_message(port: int, owners) -> str:
    who = describe_port_owners(owners)
    lines = [f"Cổng {port} mà Kid TE cần đang bị chương trình khác chiếm"
             + (f": {who}." if who else ".")]
    if any("kidte" in str((row or {}).get("process", "")).casefold() for row in owners or []):
        lines.append("Có vẻ một bản Kid TE cũ vẫn đang chạy ngầm. Mở Task Manager, "
                     "kết thúc KidTE.exe / KidTELauncher.exe rồi mở lại Kid TE.")
    else:
        lines.append("Cách xử lý: đóng chương trình đó rồi mở lại Kid TE. Nếu không biết "
                     "chương trình nào, hãy khởi động lại máy tính rồi mở Kid TE trước.")
    lines.append("Kid TE không tự tắt chương trình khác để tránh mất dữ liệu của bạn.")
    return "\n".join(lines)


WEBVIEW2_MESSAGE = (
    "Không mở được cửa sổ Kid TE vì thành phần Microsoft Edge WebView2 bị thiếu hoặc hỏng.\n\n"
    "Kid TE vẫn đang chạy: giao diện sẽ được mở trong trình duyệt mặc định "
    "({url}).\n\n"
    "Cách sửa lâu dài: cài lại \"Microsoft Edge WebView2 Runtime\" từ trang của Microsoft:\n"
    f"{WEBVIEW2_URL}\n"
    "rồi tắt hẳn Kid TE và mở lại."
)
