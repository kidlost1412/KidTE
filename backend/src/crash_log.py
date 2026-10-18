"""Ghi dấu vết khi lõi sập (O2): faulthandler cho lỗi native, excepthook cho lỗi Python.

- ``logs/crash.log``: faulthandler (sập trong DLL/driver, treo luồng) và traceback
  của ngoại lệ không ai bắt. Xoay vòng lúc khởi động (1 MB x 3).
- ``core.jsonl``: cùng traceback qua logger có cấu trúc (đã xoay vòng, đã che bí mật).
"""
from __future__ import annotations

import faulthandler
import sys
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path

from console_io import rotate_file

CRASH_LOG_MAX_BYTES = 1024 * 1024
CRASH_LOG_BACKUPS = 3
_state = {"stream": None, "installed": False}


def _write(text: str) -> None:
    stream = _state["stream"]
    if stream is None:
        return
    try:
        stream.write(text)
        stream.flush()
    except (OSError, ValueError):
        pass  # crash.log is a best-effort secondary copy; core.jsonl still has it


def _report(kind: str, exc_type, exc_value, exc_tb, logger=None, thread_name: str = "") -> None:
    rendered = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    where = f" thread={thread_name}" if thread_name else ""
    _write(f"\n=== {stamp} {kind}{where} ===\n{rendered}")
    if logger is not None:
        try:
            logger.critical(f"{kind}{where}: {exc_type.__name__}: {exc_value}",
                            exc_info=(exc_type, exc_value, exc_tb),
                            extra={"event": "unhandled_exception"})
        except Exception:  # noqa: BLE001 - never raise from an excepthook
            pass


def install(log_dir, logger=None) -> Path | None:
    """Bật faulthandler + sys.excepthook + threading.excepthook. Gọi một lần."""
    if _state["installed"]:
        return None
    path = Path(log_dir) / "crash.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file() and path.stat().st_size > CRASH_LOG_MAX_BYTES:
            rotate_file(path, backups=CRASH_LOG_BACKUPS)
        # faulthandler needs a real file descriptor that stays open for the process lifetime.
        _state["stream"] = open(path, "a", encoding="utf-8", errors="replace")
        faulthandler.enable(file=_state["stream"], all_threads=True)
    except (OSError, RuntimeError, ValueError) as exc:
        if logger is not None:
            logger.error(f"crash log unavailable: {type(exc).__name__}: {exc}",
                         extra={"event": "crash_log_unavailable"})
        path = None

    previous_hook = sys.excepthook
    previous_thread_hook = threading.excepthook

    def excepthook(exc_type, exc_value, exc_tb):
        if not issubclass(exc_type, KeyboardInterrupt):
            _report("unhandled_exception", exc_type, exc_value, exc_tb, logger)
        previous_hook(exc_type, exc_value, exc_tb)

    def thread_excepthook(args):
        if args.exc_type is not SystemExit:
            name = getattr(args.thread, "name", "") if args.thread is not None else ""
            _report("unhandled_thread_exception", args.exc_type, args.exc_value,
                    args.exc_traceback, logger, thread_name=name)
        previous_thread_hook(args)

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook
    _state["installed"] = True
    return path
