"""Tác vụ Windows chỉ chạy khi người dùng bấm nút tương ứng."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from swallowed import note as _note_swallowed


def chrome_executable():
    candidates = []
    for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(variable)
        if base:
            candidates.append(Path(base) / "Google/Chrome/Application/chrome.exe")
    return next((path for path in candidates if path.is_file()), None)


def open_extension_setup(extension_dir, *, opener=os.startfile, popen=subprocess.Popen):
    folder = Path(extension_dir) if extension_dir else None
    if folder is None or not (folder / "manifest.json").is_file():
        return {"ok": False, "error": "extension_folder_missing"}
    chrome = chrome_executable()
    if chrome is None:
        return {"ok": False, "error": "chrome_not_found", "folder": str(folder)}
    opened, errors = {"chrome_page": False, "folder": False}, []
    try:
        popen([str(chrome), "--new-tab", "chrome://extensions/"], shell=False)
        opened["chrome_page"] = True
    except OSError as exc:
        errors.append(f"chrome:{type(exc).__name__}")
    try:
        opener(str(folder))
        opened["folder"] = True
    except OSError as exc:
        errors.append(f"folder:{type(exc).__name__}")
    return {
        "ok": all(opened.values()), "folder": str(folder), "manual_install": True,
        "opened": opened, "error": ";".join(errors),
    }


def focus_current_process_window(title="Kid TE — Trợ lý vận hành LIVE"):
    try:
        import win32con
        import win32gui
        import win32process
    except ImportError:
        return False
    matches = []
    current_pid = os.getpid()
    def collect(hwnd, _):
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid == current_pid and win32gui.GetWindowText(hwnd) == title:
            matches.append(hwnd)
    win32gui.EnumWindows(collect, None)
    if len(matches) != 1:
        return False
    hwnd = matches[0]
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
        return True
    except Exception:
        # Windows có thể từ chối SetForegroundWindow nếu lời gọi đến từ
        # HTTP worker thread. Không để lỗi này biến thao tác mở EXE thành 500.
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
            win32gui.FlashWindow(hwnd, True)
        except Exception:
            _note_swallowed("window_flash_failed", once=True)
        return False


def dpapi_protect(payload: bytes) -> bytes:
    """Current-user DPAPI protect (CryptProtectData), reusing captcha_secret."""
    from captcha_secret import _protect
    return _protect(payload, decrypt=False)


def dpapi_unprotect(payload: bytes) -> bytes:
    from captcha_secret import _protect
    return _protect(payload, decrypt=True)
