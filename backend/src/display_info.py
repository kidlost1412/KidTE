"""Display/DPI info: text parser plus a real Windows ctypes probe (no PowerShell)."""
from __future__ import annotations

import re
from swallowed import note as _note_swallowed

_LINE = re.compile(r"^\s*(?P<name>\S+)\s+(?P<w>\d+)x(?P<h>\d+)\s+dpi=(?P<dpi>\d+)(?P<primary>\s+primary)?\s*$",
                   re.IGNORECASE)


def parse_displays(text: str) -> list[dict]:
    """One display per line: NAME WxH dpi=N [primary]. Bad lines are skipped."""
    out = []
    for raw in str(text or "").lstrip("\ufeff").splitlines():
        m = _LINE.match(raw)
        if not m:
            continue
        dpi = int(m["dpi"])
        out.append({"name": m["name"], "width": int(m["w"]), "height": int(m["h"]),
                    "dpi": dpi, "scale": round(dpi / 96, 2), "primary": bool(m["primary"])})
    return out


def probe_displays(*, _api=None) -> list[dict]:
    """Enumerate monitors via EnumDisplayMonitors/GetMonitorInfoW/GetDpiForMonitor.

    Same shape as parse_displays plus x, y (position) and work_x/work_y/work_width/work_height.
    Read-only; the per-thread DPI awareness is switched temporarily and restored, the process-wide
    setting is never changed. Non-Windows or any failure -> [].
    """
    try:
        api = _api if _api is not None else _windows_api()
        if api is None:
            return []
        return _collect(api)
    except Exception:
        return []


def _windows_api():
    import sys
    if not sys.platform.startswith("win"):
        return None
    import ctypes
    from ctypes import wintypes

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT), ("rcWork", RECT),
                    ("dwFlags", wintypes.DWORD), ("szDevice", ctypes.c_wchar * 32)]

    user32 = ctypes.windll.user32
    try:
        shcore = ctypes.windll.shcore
    except OSError:
        shcore = None
    proc = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HANDLE, wintypes.HDC,
                              ctypes.POINTER(RECT), wintypes.LPARAM)

    def enum():
        handles = []

        def cb(handle, _dc, _rect, _lp):
            handles.append(handle)
            return 1
        user32.EnumDisplayMonitors(None, None, proc(cb), 0)
        return handles

    def info(handle):
        mi = MONITORINFOEXW()
        mi.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if not user32.GetMonitorInfoW(handle, ctypes.byref(mi)):
            return None
        r, w = mi.rcMonitor, mi.rcWork
        return {"name": mi.szDevice, "rect": (r.left, r.top, r.right, r.bottom),
                "work": (w.left, w.top, w.right, w.bottom), "primary": bool(mi.dwFlags & 1)}

    def dpi(handle):
        if shcore is None:
            return 96
        x, y = ctypes.c_uint(0), ctypes.c_uint(0)
        if shcore.GetDpiForMonitor(handle, 0, ctypes.byref(x), ctypes.byref(y)) != 0:
            return 96
        return int(x.value) or 96

    def set_thread_awareness():
        try:
            fn = user32.SetThreadDpiAwarenessContext
            fn.restype = ctypes.c_void_p
            fn.argtypes = [ctypes.c_void_p]
            old = fn(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
            return lambda: fn(ctypes.c_void_p(old)) if old else None
        except Exception:
            return lambda: None

    return {"enum": enum, "info": info, "dpi": dpi, "awareness": set_thread_awareness}


def _collect(api) -> list[dict]:
    restore = api["awareness"]()
    out = []
    try:
        for handle in api["enum"]():
            data = api["info"](handle)
            if not data:
                continue
            l, t, r, b = data["rect"]
            wl, wt, wr, wb = data["work"]
            dpi = int(api["dpi"](handle)) or 96
            out.append({"name": _short(data["name"]),
                        "width": r - l, "height": b - t, "dpi": dpi, "scale": round(dpi / 96, 2),
                        "primary": bool(data["primary"]), "x": l, "y": t,
                        "work_x": wl, "work_y": wt, "work_width": wr - wl, "work_height": wb - wt})
    finally:
        try:
            restore()
        except Exception:
            _note_swallowed("display_dpi_restore_failed", once=True)
    return out


def _short(name) -> str:
    name = str(name)
    prefix = chr(92) * 2 + "." + chr(92)
    return name[len(prefix):] if name.startswith(prefix) and len(name) > len(prefix) else name
