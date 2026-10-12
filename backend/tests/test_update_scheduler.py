"""Background update check: uses the channel, never installs, skips busy/pending, fails silently."""
import threading
import time

import pytest
from update_manager import AUTO_CHECK_DELAY_SECONDS, AUTO_CHECK_INTERVAL_SECONDS, UpdateManager, UpdateScheduler


class FakeStore:
    """Only the calls a check may make; any install-side call raises AttributeError."""

    def __init__(self, pending=None, active=None):
        self.pending = pending
        self.active = active or {"components": {"core": {"package": {"version": "1.14.0", "dependencies": {}}}}}

    def load_pending(self):
        return self.pending

    def load_active(self):
        return self.active

    def read_highwater(self):
        return 0

    def accept_sequence(self, sequence):
        self.accepted = sequence


def make_manager(tmp_path, store, transport=None):
    manager = UpdateManager(str(tmp_path), transport=transport or object(), trusted_keys={"k": b"x"}, store=store)
    return manager


def finish(manager):
    thread = manager._thread
    if thread is not None:
        thread.join(5)


def recording_check(manager, calls):
    def _check(store, channel):
        calls.append(("check", channel))
    manager._check = _check


def test_auto_check_uses_current_channel_default_beta(tmp_path):
    calls = []
    manager = make_manager(tmp_path, FakeStore())
    recording_check(manager, calls)
    assert manager.auto_check() == {"started": True, "channel": "beta"}
    finish(manager)
    assert calls == [("check", "beta")]


def test_auto_check_follows_selected_stable_channel(tmp_path):
    calls = []
    manager = make_manager(tmp_path, FakeStore())
    recording_check(manager, calls)
    manager.action("check", {"channel": "stable"})
    finish(manager)
    calls.clear()
    manager.auto_check()
    finish(manager)
    assert calls == [("check", "stable")]


def test_auto_check_never_installs_or_prepares(tmp_path):
    calls = []
    manager = make_manager(tmp_path, FakeStore())
    recording_check(manager, calls)
    manager.auto_check()
    finish(manager)
    # Only the check ran; the fake store would raise AttributeError on prepare/stage/install calls.
    assert calls == [("check", "beta")]
    assert manager.state()["operation"] == "ready"


def test_auto_check_skips_when_busy(tmp_path):
    calls = []
    manager = make_manager(tmp_path, FakeStore())
    recording_check(manager, calls)
    manager._state["busy"] = True
    assert manager.auto_check() == {"skipped": "busy"}
    assert calls == []


def test_auto_check_skips_when_restart_pending(tmp_path):
    calls = []
    manager = make_manager(tmp_path, FakeStore(pending={"components": {}}))
    recording_check(manager, calls)
    assert manager.auto_check() == {"skipped": "restart_pending"}
    assert calls == []


def test_auto_check_disabled_without_app_root_or_trust(tmp_path):
    calls = []
    manager = UpdateManager(None, transport=object(), trusted_keys={"k": b"x"}, store=FakeStore())
    recording_check(manager, calls)
    assert manager.auto_check() == {"skipped": "disabled"}
    manager = make_manager(tmp_path, FakeStore())
    manager.trusted_keys = {}
    assert manager.auto_check() == {"skipped": "disabled"}
    assert calls == []


def test_auto_check_failure_is_silent(tmp_path, caplog):
    class BrokenTransport:
        def fetch_catalog(self, channel):
            raise RuntimeError("network down")

    manager = make_manager(tmp_path, FakeStore(), transport=BrokenTransport())
    with caplog.at_level("WARNING", logger="kidte.update"):
        assert manager.auto_check() == {"started": True, "channel": "beta"}
        finish(manager)
    # Worker failure lands in state, never raises into the caller or the scheduler.
    assert manager.state()["operation"] == "failed"
    assert manager.state()["busy"] is False


def test_state_flags_newer_packages_and_last_checked(tmp_path):
    manager = make_manager(tmp_path, FakeStore())
    manager._catalog = {"components": [
        {"id": "core", "version": "1.14.1", "archive_bytes": 48_000_000, "unpacked_bytes": 1, "dependencies": {}},
        {"id": "media-tools", "version": "2.0.0", "archive_bytes": 5_000_000, "unpacked_bytes": 1, "dependencies": {}},
    ]}
    state = manager.state()
    assert state["update_available"] is True
    assert state["update_total_bytes"] == 48_000_000  # media-tools is not installed, so it is not an update
    core = next(row for row in state["available"] if row["id"] == "core")
    assert core["newer"] is True and core["installed_version"] == "1.14.0" and core["version"] == "1.14.1"
    assert state["last_checked_at"] is None

    manager._catalog = {"components": [
        {"id": "core", "version": "1.14.0", "archive_bytes": 48_000_000, "unpacked_bytes": 1, "dependencies": {}}]}
    state = manager.state()
    assert state["update_available"] is False and state["update_total_bytes"] == 0


def test_scheduler_defaults_are_sixty_seconds_then_six_hours():
    assert AUTO_CHECK_DELAY_SECONDS == 60
    assert AUTO_CHECK_INTERVAL_SECONDS == 6 * 60 * 60
    scheduler = UpdateScheduler(object())
    assert scheduler.initial_delay == 60 and scheduler.interval == 6 * 60 * 60


def test_scheduler_runs_repeatedly_and_survives_errors_until_stopped():
    class FakeManager:
        def __init__(self):
            self.calls = 0
            self.seen = threading.Event()

        def auto_check(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("boom")  # a failing check must not kill the loop
            if self.calls >= 3:
                self.seen.set()
            return {"started": True}

    manager = FakeManager()
    scheduler = UpdateScheduler(manager, initial_delay=0, interval=0.01).start()
    try:
        assert manager.seen.wait(5)
    finally:
        scheduler.stop()
    stopped_at = manager.calls
    time.sleep(0.05)
    assert manager.calls == stopped_at


def test_scheduler_waits_initial_delay_before_first_check():
    class FakeManager:
        def __init__(self):
            self.calls = []

        def auto_check(self):
            self.calls.append(time.monotonic())

    manager = FakeManager()
    scheduler = UpdateScheduler(manager, initial_delay=0.2, interval=3600).start()
    try:
        started = time.monotonic()
        deadline = started + 5
        while not manager.calls and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        scheduler.stop()
    assert manager.calls and manager.calls[0] - started >= 0.15
    assert len(manager.calls) == 1  # the 3600 s interval does not fire again within the test


def _row(cid, version):
    return {"id": cid, "version": version, "archive_bytes": 1_000_000, "unpacked_bytes": 1, "dependencies": {}}


def test_downgrade_or_equal_catalog_is_never_an_update(tmp_path):
    manager = make_manager(tmp_path, FakeStore(active={"components": {"core": {"package": {"version": "1.14.10", "dependencies": {}}}}}))
    for version in ("1.14.9", "1.14.10", "1.2.0", "1.14.10-beta.1"):
        manager._catalog = {"components": [_row("core", version)]}
        state = manager.state()
        assert state["update_available"] is False, version
    manager._catalog = {"components": [_row("core", "1.14.11")]}  # numeric, not lexicographic: 11 > 10
    assert manager.state()["update_available"] is True


def _split_active(app="1.15.0", runtime="1.0.0", ext="1.0.0"):
    return {"components": {k: {"package": {"version": v, "dependencies": {}}}
                           for k, v in (("runtime", runtime), ("app", app), ("extensions", ext))}}


def test_split_install_compares_each_package_to_itself(tmp_path):
    manager = make_manager(tmp_path, FakeStore(active=_split_active()))
    manager._catalog = {"components": [_row("runtime", "1.0.0"), _row("app", "1.15.1"), _row("extensions", "1.0.0")]}
    state = manager.state()
    assert [r["id"] for r in state["available"] if r["newer"]] == ["app"]
    manager._catalog = {"components": [_row("runtime", "1.0.0"), _row("app", "1.14.9"), _row("extensions", "1.0.0")]}
    assert manager.state()["update_available"] is False


def test_monolithic_core_sees_split_catalog_as_update_only_when_app_is_newer(tmp_path):
    manager = make_manager(tmp_path, FakeStore())  # core 1.14.0
    split = [_row("runtime", "1.0.0"), _row("app", "1.15.0"), _row("extensions", "1.0.0")]
    manager._catalog = {"components": split}
    state = manager.state()
    assert state["update_available"] is True and state["update_total_bytes"] == 3_000_000
    assert next(r for r in state["available"] if r["id"] == "app")["installed_version"] == "1.14.0"
    manager._catalog = {"components": [_row("runtime", "1.0.0"), _row("app", "1.13.0"), _row("extensions", "1.0.0")]}
    assert manager.state()["update_available"] is False


def test_extension_reload_notice_is_reported_and_can_be_acknowledged(tmp_path):
    import json
    manager = make_manager(tmp_path, FakeStore())
    assert manager.state()["extension_reload"] is None
    (tmp_path / "extensions-notice.json").write_text(json.dumps({"version": "1.0.1", "files": 2}), encoding="utf-8")
    assert manager.state()["extension_reload"]["version"] == "1.0.1"
    assert manager.action("ack_extension_notice", {})["ok"] is True
    assert manager.state()["extension_reload"] is None
