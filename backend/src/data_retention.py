"""Dọn định kỳ dữ liệu thô/log cũ trong ``<ROOT>\\logs`` (D10).

Chỉ xoá các TỆP nhật ký thô quá hạn, theo lô nhỏ, ở nền:
  - raw (mặc định 90 ngày): ``logs/raw_stream/stream_YYYYMMDD.jsonl`` (ngày ở tên tệp VÀ
    thời điểm sửa cuối đều phải quá hạn), ``logs/customer_sessions/**/customers_*.jsonl``.
  - log (mặc định 30 ngày): ``logs/missing_answers/missing_*.jsonl`` và các tệp log đã xoay
    (``*.jsonl.N`` / ``*.log.N``) ở ngay thư mục ``logs``.
KHÔNG đụng DB, cấu hình, hồ sơ sản phẩm, kiến thức, đơn/khách đã duyệt, bản sao lưu.

An toàn: đường dẫn phải resolve nằm trong ``logs``; không theo symlink; lỗi đọc/stat thì BỎ QUA
tệp đó (fail safe); không chạy khi LIVE/bảo trì/đang sao lưu; số ngày cấu hình bằng
``KIDTE_RETENTION_RAW_DAYS`` / ``KIDTE_RETENTION_LOG_DAYS`` (0 = tắt nhóm đó; giá trị < 7 bị nâng lên 7;
giá trị không phải số dùng mặc định). ``KIDTE_DATA_RETENTION=0`` tắt hẳn.
"""
from __future__ import annotations

import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_RAW_DAYS = 90
DEFAULT_LOG_DAYS = 30
MIN_DAYS = 7
BATCH_SIZE = 20
BATCH_PAUSE_S = 0.2
STARTUP_DELAY_S = 600
CHECK_INTERVAL_S = 24 * 3600
DEFER_RETRY_S = 1800

_STREAM_NAME = re.compile(r"^stream_(\d{8})\.jsonl$")
_ROTATED = re.compile(r"^.+\.(?:jsonl|log)\.\d+$")


def _days_from_env(name, default):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default      # giá trị rác: dùng mặc định, không tắt/không xoá hung hăng
    if value <= 0:
        return 0
    return max(MIN_DAYS, value)


def enabled():
    return os.environ.get("KIDTE_DATA_RETENTION", "").strip().lower() not in {"0", "off", "false", "no"}


def policy_from_env():
    return {"raw_days": _days_from_env("KIDTE_RETENTION_RAW_DAYS", DEFAULT_RAW_DAYS),
            "log_days": _days_from_env("KIDTE_RETENTION_LOG_DAYS", DEFAULT_LOG_DAYS)}


def _inside(path, base):
    try:
        Path(path).resolve().relative_to(base)
    except (OSError, ValueError, RuntimeError):
        return False
    return True


def plan(root, *, raw_days, log_days, now=None):
    """Tệp đủ điều kiện xoá: [{"path", "kind", "age_days"}]. Không xoá gì."""
    now = float(now if now is not None else time.time())
    logs = Path(root) / "logs"
    try:
        base = logs.resolve(strict=True)
    except OSError:
        return []
    found = []

    def consider(path, kind, days, name_date=None):
        if not days:
            return
        try:
            if path.is_symlink() or not path.is_file() or not _inside(path, base):
                return
            mtime = path.stat().st_mtime
        except OSError:
            return          # không đọc được -> không xoá
        cutoff = now - days * 86400
        if mtime >= cutoff:
            return
        if name_date is not None:
            try:
                stamp = datetime.strptime(name_date, "%Y%m%d").replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                return
            if stamp >= cutoff:
                return
        found.append({"path": path, "kind": kind, "age_days": int((now - mtime) // 86400)})

    def listing(directory, pattern, recursive=False):
        try:
            if directory.is_symlink() or not directory.is_dir() or not _inside(directory, base):
                return []
            return sorted(directory.rglob(pattern) if recursive else directory.glob(pattern))
        except OSError:
            return []

    for path in listing(logs / "raw_stream", "stream_*.jsonl"):
        match = _STREAM_NAME.match(path.name)
        if match:
            consider(path, "raw_stream", raw_days, name_date=match.group(1))
    for path in listing(logs / "customer_sessions", "customers_*.jsonl", recursive=True):
        consider(path, "customer_journal", raw_days)
    for path in listing(logs / "missing_answers", "missing_*.jsonl"):
        consider(path, "missing_answers", log_days)
    try:
        top = sorted(p for p in logs.iterdir() if _ROTATED.match(p.name))
    except OSError:
        top = []
    for path in top:
        consider(path, "rotated_log", log_days)
    return found


def _log(logger, level, message, event):
    if logger is None:
        return
    try:
        getattr(logger, level)(message, extra={"event": event})
    except Exception:  # noqa: BLE001
        pass


def run_once(root, *, raw_days=DEFAULT_RAW_DAYS, log_days=DEFAULT_LOG_DAYS, now=None,
             batch_size=BATCH_SIZE, pause_s=BATCH_PAUSE_S, should_stop=None,
             acquire=None, release=None, logger=None):
    """Xoá theo lô. ``acquire()`` (nếu có) lấy khoá cho MỖI lô, trả False nếu bận (dừng, để lần sau);
    ``release()`` nhả khoá sau lô. Trả {"deleted","failed","bytes","stopped"}. Không ném lỗi."""
    result = {"deleted": 0, "failed": 0, "bytes": 0, "stopped": False}
    try:
        candidates = plan(root, raw_days=raw_days, log_days=log_days, now=now)
    except Exception as exc:  # noqa: BLE001 - dọn nền không được làm hỏng app
        _log(logger, "error", f"{type(exc).__name__}: {exc}", "data_retention_plan_failed")
        result["failed"] += 1
        return result
    size_of_batch = max(1, int(batch_size))
    for start in range(0, len(candidates), size_of_batch):
        if should_stop is not None and should_stop():
            result["stopped"] = True
            break
        if acquire is not None and not acquire():
            result["stopped"] = True
            break
        try:
            for item in candidates[start:start + size_of_batch]:
                path = item["path"]
                try:
                    size = path.stat().st_size
                    path.unlink()
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    result["failed"] += 1
                    _log(logger, "warning", f"{item['kind']}: {type(exc).__name__}", "data_retention_skip")
                    continue
                result["deleted"] += 1
                result["bytes"] += size
        finally:
            if release is not None and acquire is not None:
                release()
        if pause_s and start + size_of_batch < len(candidates):
            time.sleep(pause_s)
    if result["deleted"] or result["failed"]:
        _log(logger, "info", f"deleted={result['deleted']} failed={result['failed']} bytes={result['bytes']}",
             "data_retention")
    return result


class RetentionScheduler:
    def __init__(self, root, *, live_provider=None, maintenance_provider=None, backup_lock=None,
                 logger=None, policy=None, startup_delay_s=STARTUP_DELAY_S,
                 check_interval_s=CHECK_INTERVAL_S, defer_retry_s=DEFER_RETRY_S, pause_s=BATCH_PAUSE_S):
        self.root = Path(root)
        self.live_provider = live_provider
        self.maintenance_provider = maintenance_provider
        self.backup_lock = backup_lock
        self.logger = logger
        self.policy = policy
        self.startup_delay_s = startup_delay_s
        self.check_interval_s = check_interval_s
        self.defer_retry_s = defer_retry_s
        self.pause_s = pause_s
        self.cancel = threading.Event()
        self.thread = None
        self.last_result = None

    @staticmethod
    def _flag(provider):
        if provider is None:
            return False
        try:
            return bool(provider())
        except Exception:  # noqa: BLE001
            return True      # không biết -> coi như bận, hoãn

    def _busy(self):
        return self.cancel.is_set() or self._flag(self.live_provider) or self._flag(self.maintenance_provider)

    def _acquire(self):
        return self.backup_lock.acquire(blocking=False)

    def _release(self):
        self.backup_lock.release()

    def run_once(self, *, now=None):
        if self._busy():
            return {"deleted": 0, "failed": 0, "bytes": 0, "stopped": True, "deferred": True}
        policy = self.policy or policy_from_env()
        locked = self.backup_lock is not None
        result = run_once(self.root, raw_days=policy["raw_days"], log_days=policy["log_days"], now=now,
                          pause_s=self.pause_s, should_stop=self._busy,
                          acquire=self._acquire if locked else None,
                          release=self._release if locked else None, logger=self.logger)
        result["deferred"] = bool(result["stopped"])
        return result

    def _loop(self):
        if self.cancel.wait(self.startup_delay_s):
            return
        while not self.cancel.is_set():
            self.last_result = self.run_once()
            wait = self.defer_retry_s if self.last_result.get("deferred") else self.check_interval_s
            if self.cancel.wait(wait):
                return

    def start(self):
        if self.thread is not None and self.thread.is_alive():
            return self.thread
        self.thread = threading.Thread(target=self._loop, name="data-retention", daemon=True)
        self.thread.start()
        return self.thread

    def stop(self):
        self.cancel.set()
