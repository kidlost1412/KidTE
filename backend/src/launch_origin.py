"""Phân biệt "người dùng tự mở Kid TE" với "một tiến trình nền vô tình chạy lại exe".

Chỉ lần mở do người dùng (Explorer/Start menu/shortcut) mới được phép đưa cửa sổ
đang chạy lên trước. Tiến trình nền (script, cập nhật, công cụ thứ ba...) mở lại
KidTE.exe/KidTELauncher.exe thì phải im lặng: không focus, không mở cửa sổ mới.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# Tiến trình cha hợp lệ khi người dùng bấm shortcut / Start menu / tìm kiếm / trình cài đặt.
USER_PARENTS = frozenset({
    "explorer.exe", "searchhost.exe", "searchapp.exe", "searchui.exe",
    "startmenuexperiencehost.exe", "shellexperiencehost.exe", "applicationframehost.exe",
})
FORCE_ENV = "KIDTE_FORCE_FOCUS"
LOG_NAME = "launch-origin.log"


def _snapshot():
    """{pid: (ppid, exe_name_lower)} qua Toolhelp32 (không cần psutil)."""
    import ctypes
    from ctypes import wintypes

    class ENTRY(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    handle = kernel32.CreateToolhelp32Snapshot(0x2, 0)
    rows = {}
    if handle in (None, wintypes.HANDLE(-1).value):
        return rows
    try:
        entry = ENTRY()
        entry.dwSize = ctypes.sizeof(ENTRY)
        ok = kernel32.Process32FirstW(handle, ctypes.byref(entry))
        while ok:
            rows[int(entry.th32ProcessID)] = (int(entry.th32ParentProcessID), entry.szExeFile.casefold())
            ok = kernel32.Process32NextW(handle, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(handle)
    return rows


def real_parent(*, pid=None, table=None, own_name=None, max_hops=4):
    """(pid, tên exe) của tiến trình cha thật, bỏ qua bootloader PyInstaller cùng tên exe."""
    table = _snapshot() if table is None else table
    pid = os.getpid() if pid is None else pid
    own = (own_name or Path(sys.executable).name).casefold()
    seen = {pid}
    current = pid
    for _ in range(max_hops):
        ppid = table.get(current, (0, ""))[0]
        if not ppid or ppid in seen or ppid not in table:
            return (ppid or None), None  # cha đã thoát: không xác định được
        name = table[ppid][1]
        seen.add(ppid)
        if name == own:
            current = ppid  # bootloader onefile / launcher mẹ cùng exe
            continue
        return ppid, name
    return None, None


def started_by_user(environ=None, *, table=None, own_name=None, pid=None):
    """(bool, mô tả) — True khi cha thật là Explorer/Start menu hoặc có KIDTE_FORCE_FOCUS."""
    environ = os.environ if environ is None else environ
    if environ.get(FORCE_ENV):
        return True, "forced"
    if os.name != "nt" and table is None:
        return True, "non-windows"
    try:
        ppid, name = real_parent(pid=pid, table=table, own_name=own_name)
    except Exception as exc:  # noqa: BLE001 - không xác định được thì KHÔNG focus
        return False, f"unknown ({type(exc).__name__})"
    return (name in USER_PARENTS), f"parent={name or 'gone'}:{ppid}"


def record(directory, action, detail):
    """Ghi dấu vết để lần ra ai đã mở lại exe (không bao giờ ném lỗi)."""
    try:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / LOG_NAME
        if path.exists() and path.stat().st_size > 256 * 1024:
            path.replace(path.with_suffix(".log.1"))
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        with path.open("a", encoding="utf-8") as stream:
            stream.write(f"{stamp} pid={os.getpid()} exe={Path(sys.executable).name} "
                         f"args={sys.argv[1:]} {action} {detail}\n")
    except OSError:
        pass
