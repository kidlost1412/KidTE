"""O2 giám sát lõi, C4 khoá hỏng, C5 ghi lý do lùi bản, C3 thông báo tiếng Việt."""
import json
import os
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import update_launcher as launcher
from test_update_launcher import FakeJob, FakeStore, _component


class ExitingProcess:
    """Sẵn sàng bình thường (poll None) rồi thoát với ``code`` khi launcher chờ."""
    pid = 9876

    def __init__(self, code):
        self.code = code
        self.terminated = False

    def poll(self):
        return None

    def wait(self, timeout=None):
        return self.code

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.terminated = True


def _ready_for(children):
    def probe(_port):
        if not children:
            return None
        process, env = children[-1]
        return {"ok": True, "pid": process.pid, "launcher_nonce": env["KIDTE_LAUNCH_NONCE"],
                "launcher_generation": env["KIDTE_LAUNCH_GENERATION"], "data_root": env["KID_TE_ROOT"]}
    return probe


def _launcher(app_root, codes, children, *, clock=None, store=None):
    sequence = list(codes)

    def spawn(command, *, env, **kwargs):
        process = ExitingProcess(sequence.pop(0))
        children.append((process, env))
        return process

    # Preflight: the ready probe answers only for a child this launcher started.
    started = {"n": 0}

    def ready_probe(port):
        if started["n"] == len(children):
            return None  # preflight before the next spawn: nothing is serving
        answer = _ready_for(children)(port)
        started["n"] = len(children)
        return answer

    return launcher.Launcher(
        store_factory=lambda _: store or FakeStore(app_root), popen=spawn, ready_probe=ready_probe,
        occupied_probe=lambda _: False, job_factory=FakeJob, sleep=lambda _: None, environ={},
        clock=clock or (lambda: 0.0),
    )


def test_core_crash_after_ready_is_restarted_then_clean_exit_stops(tmp_path):
    app_root = tmp_path / "App"
    app_root.mkdir()
    (app_root / "KidTE.exe").write_bytes(b"baseline")
    children = []
    result = _launcher(app_root, [1, 3221225477, 0], children).run(app_root / "KidTELauncher.exe")
    assert result == 0
    assert len(children) == 3
    log = (app_root / "launcher-startup.log").read_text(encoding="utf-8")
    assert "exited unexpectedly (exit code 1); restarting (1/3)" in log


def test_crash_loop_gives_up_after_three_restarts_in_ten_minutes(tmp_path):
    app_root = tmp_path / "App"
    app_root.mkdir()
    (app_root / "KidTE.exe").write_bytes(b"baseline")
    children = []
    with pytest.raises(launcher.CoreCrashLoop):
        _launcher(app_root, [1] * 10, children).run(app_root / "KidTELauncher.exe")
    assert len(children) == 4
    assert "not restarting again" in (app_root / "launcher-startup.log").read_text(encoding="utf-8")


def test_crashes_spread_over_time_keep_being_restarted(tmp_path):
    app_root = tmp_path / "App"
    app_root.mkdir()
    (app_root / "KidTE.exe").write_bytes(b"baseline")
    ticks = iter(range(0, 100000, 400))  # one crash every ~7 minutes
    children = []
    result = _launcher(app_root, [1, 1, 1, 1, 1, 0], children,
                       clock=lambda: float(next(ticks))).run(app_root / "KidTELauncher.exe")
    assert result == 0 and len(children) == 6


@pytest.mark.parametrize("code,unexpected", [(0, False), (3221225786, False), (1, True),
                                             (3221225477, True), (-1, True)])
def test_exit_code_classification(code, unexpected):
    assert launcher._is_unexpected_exit(code) is unexpected


def test_restart_and_already_running_codes_are_not_crashes():
    from update_restart import ALREADY_RUNNING_EXIT_CODE, RESTART_EXIT_CODE
    assert not launcher._is_unexpected_exit(RESTART_EXIT_CODE)
    assert not launcher._is_unexpected_exit(ALREADY_RUNNING_EXIT_CODE)


@pytest.mark.parametrize("content", [b"", b"{broken", b"[]", b'{"pid": "x"}'])
def test_old_corrupt_live_lock_is_free_but_fresh_one_is_unknown(tmp_path, content):
    app_root, data_root = tmp_path / "App", tmp_path / "DuLieu"
    app_root.mkdir()
    lock = data_root / "runtime" / "live-re.lock"
    lock.parent.mkdir(parents=True)
    lock.write_bytes(content)
    assert launcher._live_lock_state(data_root, app_root) == ("unknown", None)
    old = lock.stat().st_mtime - 600
    os.utime(lock, (old, old))
    assert launcher._live_lock_state(data_root, app_root) == ("free", None)


class VersionedStore(FakeStore):
    def activate_pending(self):
        active = super().activate_pending()
        active["components"]["core"]["package"]["version"] = "2.0.0"
        return active

    def rollback(self):
        self.rollback_called = True
        self.active = {"generation": "prior-generation",
                       "components": {"core": {"package": {"version": "1.0.0"}, "path": "x"}}}
        return self.active


def test_failed_update_records_versions_and_reason_for_the_update_page(tmp_path):
    app_root = tmp_path / "App"
    app_root.mkdir()
    store = VersionedStore(app_root, pending=True)
    app = launcher.Launcher(store_factory=lambda _: store, popen=lambda *a, **k: ExitingProcess(0),
                            ready_probe=lambda _: None, occupied_probe=lambda _: False,
                            job_factory=FakeJob, startup_timeout=0, sleep=lambda _: None, environ={})
    with pytest.raises(launcher.LauncherError):
        app.run(app_root / "KidTELauncher.exe")
    record = json.loads((app_root / "update-failure.json").read_text(encoding="utf-8"))
    assert record["failed_version"] == "2.0.0"
    assert record["restored_version"] == "1.0.0"
    assert record["reason_code"] == "startup_timeout"


def test_user_retry_of_timed_out_version_gets_longer_timeout_and_success_clears_record(tmp_path, monkeypatch):
    app_root = tmp_path / "App"
    app_root.mkdir()
    (app_root / "update-failure.json").write_text(json.dumps(
        {"failed_version": "2.0.0", "reason_code": "startup_timeout"}), encoding="utf-8")
    store = VersionedStore(app_root, pending=True)
    seen = []
    original = launcher._wait_ready

    def wait_ready(*args, timeout, **kwargs):
        seen.append(timeout)
        return original(*args, timeout=timeout, **kwargs)

    monkeypatch.setattr(launcher, "_wait_ready", wait_ready)
    children = []
    app = launcher.Launcher(store_factory=lambda _: store,
                            popen=lambda command, *, env, **k: children.append((ExitingProcess(0), env))
                            or children[-1][0],
                            ready_probe=_ready_for(children), occupied_probe=lambda _: False,
                            job_factory=FakeJob, startup_timeout=50, sleep=lambda _: None, environ={})
    monkeypatch.setattr(launcher, "_preflight", lambda *a, **k: None)
    assert app.run(app_root / "KidTELauncher.exe") == 0
    assert seen == [100]
    assert not (app_root / "update-failure.json").exists()


def test_port_busy_message_names_the_program_and_what_to_do(monkeypatch):
    monkeypatch.setattr(launcher, "_port_owners", lambda port: [{"pid": 4321, "process": "node.exe"}])
    text = launcher._user_message(launcher.PortBusy(8793, "Port 8793 is already in use"))
    assert "8793" in text and "node.exe (PID 4321)" in text and "đóng chương trình" in text
    core_exit = launcher._user_message(launcher.LauncherError(
        "Kid TE could not start safely: core exited before readiness (exit code 3)"))
    assert "node.exe" in core_exit


def test_messages_are_vietnamese_with_an_action():
    timeout = launcher._user_message(launcher.LauncherError(
        "Kid TE could not start safely: core did not become ready within 120 seconds"))
    assert "diệt virus" in timeout
    broken = launcher._user_message(launcher.LauncherError(
        "Installed core executable failed its signed digest check."))
    assert "cài lại" in broken and "DuLieu" in broken
    assert "10 giây" in launcher._user_message(launcher.LockBusy("x"))
