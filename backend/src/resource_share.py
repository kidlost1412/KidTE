"""Chia tài nguyên cho tác vụ nặng (chép lời, ffmpeg) chạy song song với LIVE.

- Hạ ưu tiên tiến trình con xuống BELOW_NORMAL để trả lời khách không chậm.
- Khi có sàn đang LIVE, giới hạn luồng CPU = max(2, số nhân logic // 2).
- Gắn tiến trình con vào Windows Job Object KILL_ON_JOB_CLOSE và theo dõi
  để app_entry dừng sạch khi thoát giữa lúc chép lời.
"""
from __future__ import annotations

import os
import threading

import psutil

THREADS_ENV = "KID_TE_CPU_THREADS"
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

_lock = threading.Lock()
_children = set()
_job = {"handle": None}


def live_cpu_threads(live):
    """Số luồng CPU khi LIVE; None = giữ mặc định cũ."""
    if not live:
        return None
    return max(2, int(os.cpu_count() or 2) // 2)


def lower_priority(pid):
    try:
        proc = psutil.Process(pid)
        level = getattr(psutil, "BELOW_NORMAL_PRIORITY_CLASS", None)
        proc.nice(level if level is not None else 10)
        return True
    except (psutil.Error, OSError, ValueError, TypeError):
        return False


def _job_handle():
    if os.name != "nt":
        return None
    with _lock:
        if _job["handle"] is not None:
            return _job["handle"]
        import ctypes
        from ctypes import wintypes

        class _Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class _Io(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class _Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", _Basic), ("IoInfo", _Io),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            return None
        info = _Extended()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
                wintypes.HANDLE(handle), _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(info), ctypes.sizeof(info)):
            kernel32.CloseHandle(wintypes.HANDLE(handle))
            return None
        _job["handle"] = handle
        return handle


def _assign_to_job(process):
    handle = _job_handle()
    proc_handle = getattr(process, "_handle", None)
    if not handle or proc_handle is None:
        return False
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    return bool(kernel32.AssignProcessToJobObject(
        wintypes.HANDLE(handle), wintypes.HANDLE(int(proc_handle))))


def adopt(process):
    """Hạ ưu tiên, gắn Job Object và theo dõi tiến trình con vừa khởi."""
    lower_priority(process.pid)
    try:
        _assign_to_job(process)
    except (OSError, AttributeError, ValueError, TypeError):
        pass
    with _lock:
        _children.add(process)
    return process


def release(process):
    with _lock:
        _children.discard(process)


def stop_all(timeout_s=3):
    """Gọi khi app thoát: dừng mọi tiến trình con chép lời/ffmpeg còn chạy."""
    from process_cleanup import stop_process_tree
    with _lock:
        targets = list(_children)
        _children.clear()
    stopped = 0
    for process in targets:
        try:
            stop_process_tree(process, timeout_s=timeout_s)
            stopped += 1
        except Exception:  # noqa: BLE001 - best effort on shutdown.
            pass
    return stopped


def run(command, *, timeout, **kwargs):
    """subprocess.run thay thế: hạ ưu tiên + theo dõi để dừng khi thoát."""
    import subprocess
    kwargs.setdefault("creationflags", getattr(subprocess, "CREATE_NO_WINDOW", 0))
    process = subprocess.Popen(command, **kwargs)
    adopt(process)
    try:
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise
    finally:
        release(process)
    if process.returncode != 0:
        raise subprocess.CalledProcessError(process.returncode, command)
    return process.returncode
