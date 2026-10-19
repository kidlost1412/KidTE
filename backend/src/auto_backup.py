"""Automatic operational (v1) backup: shortly after startup, then periodically.

v1 is the backup the dashboard watches (``latest_backup_time`` + 12 h ``overdue``),
so the scheduler uses the same threshold. It never blocks startup (daemon thread
with a start delay), never runs two backups at once (shared lock with manual
backups), and defers while a LIVE session or maintenance mode is active.
"""
from __future__ import annotations

import os
import threading
from datetime import datetime, timezone

import backup_policy

STARTUP_DELAY_S = 45
CHECK_INTERVAL_S = 3600      # re-check hourly; a backup is made only when the latest is >= max_age_hours old
DEFER_RETRY_S = 300          # retry sooner when deferred (LIVE / maintenance / busy)
MAX_AGE_HOURS = 12           # same threshold as dashboard "overdue"
KEEP = 20                    # same retention as ensure_startup_backup


def enabled():
    return os.environ.get("KIDTE_AUTO_BACKUP", "").strip().lower() not in {"0", "off", "false", "no"}


class AutoBackupScheduler:
    def __init__(self, *, live_provider=None, maintenance_provider=None, logger=None,
                 startup_delay_s=STARTUP_DELAY_S, check_interval_s=CHECK_INTERVAL_S,
                 defer_retry_s=DEFER_RETRY_S, max_age_hours=MAX_AGE_HOURS, keep=KEEP):
        self.live_provider = live_provider
        self.maintenance_provider = maintenance_provider
        self.logger = logger
        self.startup_delay_s = startup_delay_s
        self.check_interval_s = check_interval_s
        self.defer_retry_s = defer_retry_s
        self.max_age_hours = max_age_hours
        self.keep = keep
        self.cancel = threading.Event()
        self.thread = None
        self.last_result = None

    def _log(self, level, message, event):
        if self.logger is not None:
            try:
                getattr(self.logger, level)(message, extra={"event": event})
            except Exception:  # noqa: BLE001 - chính bộ ghi log lỗi: không còn nơi nào để ghi
                pass

    def _flag(self, provider):
        if provider is None:
            return False
        try:
            return bool(provider())
        except Exception:
            return True      # cannot tell -> be safe and defer

    def run_once(self, *, now=None):
        """One decision + (maybe) one backup. Never raises."""
        try:
            if self._flag(self.live_provider):
                return {"created": False, "reason": "live_active", "deferred": True}
            if self._flag(self.maintenance_provider):
                return {"created": False, "reason": "maintenance", "deferred": True}
            if not backup_policy.OPERATIONAL_BACKUP_LOCK.acquire(blocking=False):
                return {"created": False, "reason": "backup_busy", "deferred": True}
            try:
                result = backup_policy.ensure_startup_backup(
                    now=now or datetime.now(timezone.utc),
                    max_age_hours=self.max_age_hours, keep=self.keep, reason="auto")
            finally:
                backup_policy.OPERATIONAL_BACKUP_LOCK.release()
            if result.get("created"):
                self._log("info", str(result.get("backup_id", "")), "auto_backup")
            return result
        except Exception as exc:
            self._log("error", f"{type(exc).__name__}: {exc}", "auto_backup_failed")
            return {"created": False, "reason": "failed", "error": type(exc).__name__, "deferred": True}

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
        self.thread = threading.Thread(target=self._loop, name="auto-backup", daemon=True)
        self.thread.start()
        return self.thread

    def stop(self):
        self.cancel.set()
