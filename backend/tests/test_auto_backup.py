import threading
from datetime import datetime, timedelta, timezone

import auto_backup
import backup_policy


def _make(tmp_path, monkeypatch, **kw):
    monkeypatch.setattr(backup_policy, "BACKUP_DIR", tmp_path / "backups")
    db = tmp_path / "customer_memory.sqlite3"
    db.write_bytes(b"")
    monkeypatch.setattr(backup_policy, "MEMORY_DB", db)
    calls = []

    def fake_create(*, now=None, keep=1, parts=None, reason="manual"):
        calls.append({"keep": keep, "reason": reason})
        return {"created": True, "backup_id": "b%d" % len(calls)}

    monkeypatch.setattr(backup_policy, "create_operational_backup", fake_create)
    return auto_backup.AutoBackupScheduler(**kw), calls


def test_creates_when_no_backup_with_auto_reason_and_retention(tmp_path, monkeypatch):
    sched, calls = _make(tmp_path, monkeypatch)
    result = sched.run_once()
    assert result["created"] is True
    assert calls == [{"keep": 20, "reason": "auto"}]


def test_skips_when_recent_backup(tmp_path, monkeypatch):
    sched, calls = _make(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(backup_policy, "latest_backup_time", lambda root: now - timedelta(hours=1))
    assert sched.run_once(now=now)["reason"] == "recent_backup"
    monkeypatch.setattr(backup_policy, "latest_backup_time", lambda root: now - timedelta(hours=13))
    assert sched.run_once(now=now)["created"] is True
    assert len(calls) == 1


def test_defers_during_live_and_maintenance(tmp_path, monkeypatch):
    state = {"live": True, "maint": False}
    sched, calls = _make(tmp_path, monkeypatch, live_provider=lambda: state["live"],
                         maintenance_provider=lambda: state["maint"])
    assert sched.run_once()["reason"] == "live_active"
    state.update(live=False, maint=True)
    assert sched.run_once()["reason"] == "maintenance"
    assert calls == []
    state["maint"] = False
    assert sched.run_once()["created"] is True


def test_provider_error_defers_safely(tmp_path, monkeypatch):
    def boom():
        raise RuntimeError("x")
    sched, calls = _make(tmp_path, monkeypatch, live_provider=boom)
    assert sched.run_once()["reason"] == "live_active"
    assert calls == []


def test_never_concurrent_with_other_backup(tmp_path, monkeypatch):
    sched, calls = _make(tmp_path, monkeypatch)
    held = threading.Event()
    release = threading.Event()

    def holder():
        with backup_policy.OPERATIONAL_BACKUP_LOCK:
            held.set()
            release.wait(5)

    t = threading.Thread(target=holder)
    t.start()
    held.wait(5)
    try:
        assert sched.run_once()["reason"] == "backup_busy"
    finally:
        release.set()
        t.join()
    assert calls == []


def test_failure_never_raises(tmp_path, monkeypatch):
    sched, _ = _make(tmp_path, monkeypatch)

    def bad(**kw):
        raise OSError("disk")

    monkeypatch.setattr(backup_policy, "create_operational_backup", bad)
    out = sched.run_once()
    assert out["created"] is False and out["reason"] == "failed"


def test_background_loop_waits_startup_delay_then_periodic_and_stops(tmp_path, monkeypatch):
    sched, calls = _make(tmp_path, monkeypatch, startup_delay_s=0.05, check_interval_s=0.05,
                         defer_retry_s=0.05)
    monkeypatch.setattr(backup_policy, "latest_backup_time", lambda root: None)
    sched.start()
    deadline = threading.Event()
    for _ in range(100):
        if len(calls) >= 2:
            break
        deadline.wait(0.05)
    sched.stop()
    sched.thread.join(2)
    assert len(calls) >= 2 and not sched.thread.is_alive()


def test_cancel_before_delay_never_backs_up(tmp_path, monkeypatch):
    sched, calls = _make(tmp_path, monkeypatch, startup_delay_s=60)
    sched.start()
    sched.stop()
    sched.thread.join(2)
    assert calls == [] and not sched.thread.is_alive()


def test_env_kill_switch(monkeypatch):
    monkeypatch.setenv("KIDTE_AUTO_BACKUP", "off")
    assert auto_backup.enabled() is False
    monkeypatch.delenv("KIDTE_AUTO_BACKUP")
    assert auto_backup.enabled() is True
