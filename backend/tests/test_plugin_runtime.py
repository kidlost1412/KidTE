import sys
from pathlib import Path
from types import SimpleNamespace

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from plugin_runtime import PluginRuntimeManager  # noqa: E402


class FakeSupervisor:
    def __init__(self): self.calls = []
    def status(self, spec): return {"state": "stopped", "reason": "no_pid_file"}
    def start(self, spec, manual=False): self.calls.append(("start", spec.service_id)); return {"state": "running", "pid": 7}
    def stop(self, spec, timeout_s): self.calls.append(("stop", spec.service_id, timeout_s)); return {"state": "stopped", "reason": "graceful"}


def manifest(root, plugin_id, resources=()):
    entry = root / plugin_id / "chay.py"; entry.parent.mkdir(); entry.write_text("", encoding="utf-8")
    return SimpleNamespace(plugin_id=plugin_id, name=plugin_id, kind="dich-vu", version="1",
                           resources=tuple(resources), command=("chay.py",), command_path=entry,
                           directory=entry.parent, description="")


def test_safe_mode_blocks_start_but_allows_stop(tmp_path):
    supervisor = FakeSupervisor()
    runtime = PluginRuntimeManager(
        {"dong-bo-video": manifest(tmp_path, "dong-bo-video")}, supervisor,
        safe_mode=True,
    )
    assert runtime.start("dong-bo-video") == {"ok": False, "error": "safe_mode_blocks_plugins"}
    assert runtime.stop("dong-bo-video")["ok"] is True
    assert supervisor.calls == [("stop", "dong-bo-video", 10)]


def test_enabling_safe_mode_stops_services_then_blocks_start(tmp_path):
    supervisor = FakeSupervisor()
    runtime = PluginRuntimeManager(
        {"dong-bo-video": manifest(tmp_path, "dong-bo-video")}, supervisor,
    )
    result = runtime.set_safe_mode(True)
    assert result["ok"] is True and result["safe_mode"] is True
    assert supervisor.calls == [("stop", "dong-bo-video", 10)]
    assert runtime.start("dong-bo-video")["error"] == "safe_mode_blocks_plugins"


# ---------------------------------------------------------------------------
# `[T9]` Log chứng minh các kịch bản lỗi thật của dịch vụ.
# ---------------------------------------------------------------------------

class _RecordingLogger:
    def __init__(self):
        self.records = []

    def info(self, _message, extra=None):
        self.records.append(dict(extra or {}))


class ScriptedSupervisor:
    """Supervisor giả lập trạng thái tuỳ ý qua từng lần gọi — mô phỏng
    dịch vụ chết/sống/gặp giới hạn restart mà không cần tiến trình thật."""

    def __init__(self):
        self.status_value = {"state": "stopped", "reason": "no_pid_file"}
        self.start_value = {"state": "running", "pid": 1}
        self.stop_value = {"state": "stopped", "reason": "graceful"}
        self.max_restarts = 3

    def status(self, spec):
        return self.status_value

    def start(self, spec, manual=False):
        return self.start_value

    def stop(self, spec, timeout_s):
        return self.stop_value


def test_snapshot_logs_reattach_after_restart_only_once_per_transition(tmp_path):
    """`[T9]` Bằng chứng cho 'service chết -> lõi mở lại -> attach đúng PID':
    khi supervisor báo `attached: True`, phải log ĐÚNG MỘT LẦN tại thời điểm
    chuyển trạng thái — không log lại ở những lần UI poll tiếp theo khi
    trạng thái không đổi (tránh spam log)."""
    logger = _RecordingLogger()
    supervisor = ScriptedSupervisor()
    demo = manifest(tmp_path, "demo")
    runtime = PluginRuntimeManager({"demo": demo}, supervisor, logger=logger)

    runtime.snapshot()
    assert logger.records == [], "chua tung chay thi khong co gi de log"

    supervisor.status_value = {"state": "running", "pid": 99, "attached": True, "uptime_s": 120.0}
    runtime.snapshot()
    assert [r["event"] for r in logger.records] == ["service_reattached_after_restart"]
    assert logger.records[0]["pid"] == 99

    runtime.snapshot()
    runtime.snapshot()
    assert len(logger.records) == 1, "trang thai khong doi thi khong duoc log them"


def test_snapshot_logs_crash_detected_when_previously_running_goes_stale(tmp_path):
    """`[T9]` Bằng chứng cho 'service chết' được PHÁT HIỆN (không chỉ tự
    lành âm thầm): sau khi từng thấy `running`, lần sau thấy `stale_pid`
    phải log rõ sự kiện, kèm lý do."""
    logger = _RecordingLogger()
    supervisor = ScriptedSupervisor()
    demo = manifest(tmp_path, "demo")
    now = [0.0]
    runtime = PluginRuntimeManager({"demo": demo}, supervisor, logger=logger, clock=lambda: now[0])

    supervisor.status_value = {"state": "running", "pid": 5, "attached": True}
    runtime.snapshot()
    now[0] = 3600.0
    supervisor.status_value = {"state": "stopped", "reason": "stale_pid"}
    runtime.snapshot()

    events = [r["event"] for r in logger.records]
    assert events == ["service_reattached_after_restart", "service_crash_detected"]
    assert logger.records[1]["reason"] == "stale_pid"


def test_stale_pid_right_after_core_restart_is_not_reported_as_crash(tmp_path):
    """O8: PID cũ của lần chạy lõi trước chết ngay sau khi nối lại -> nhãn riêng."""
    logger = _RecordingLogger()
    supervisor = ScriptedSupervisor()
    demo = manifest(tmp_path, "demo")
    now = [0.0]
    runtime = PluginRuntimeManager({"demo": demo}, supervisor, logger=logger, clock=lambda: now[0])
    supervisor.status_value = {"state": "running", "pid": 5, "attached": True}
    runtime.snapshot()
    now[0] = 5.0
    supervisor.status_value = {"state": "stopped", "reason": "stale_pid"}
    runtime.snapshot()
    events = [r["event"] for r in logger.records]
    assert events == ["service_reattached_after_restart", "service_stale_after_core_restart"]


def test_start_logs_restart_limit_reached(tmp_path):
    """`[T9]` Bằng chứng cho 'chạm giới hạn restart'."""
    logger = _RecordingLogger()
    supervisor = ScriptedSupervisor()
    supervisor.start_value = {"state": "needs_attention", "reason": "restart_limit"}
    demo = manifest(tmp_path, "demo")
    runtime = PluginRuntimeManager({"demo": demo}, supervisor, logger=logger)

    runtime.start("demo")

    assert logger.records[-1] == {
        "event": "service_restart_limit_reached", "component": "demo", "max_restarts": 3,
    }


def test_start_and_stop_log_results(tmp_path):
    logger = _RecordingLogger()
    supervisor = ScriptedSupervisor()
    demo = manifest(tmp_path, "demo")
    runtime = PluginRuntimeManager({"demo": demo}, supervisor, logger=logger)

    runtime.start("demo")
    runtime.stop("demo")

    assert [r["event"] for r in logger.records] == ["service_start_ok", "service_stop_result"]


def test_safe_mode_change_is_logged(tmp_path):
    logger = _RecordingLogger()
    supervisor = FakeSupervisor()
    demo = manifest(tmp_path, "dong-bo-video")
    runtime = PluginRuntimeManager({"dong-bo-video": demo}, supervisor, logger=logger)

    runtime.set_safe_mode(True)

    assert any(r["event"] == "safe_mode_changed" and r["enabled"] is True
               for r in logger.records)


def test_no_logger_means_no_crash_and_no_logging(tmp_path):
    """Mặc định `logger=None` — hành vi phải y hệt trước T9, không được
    crash vì thiếu logger."""
    supervisor = FakeSupervisor()
    demo = manifest(tmp_path, "dong-bo-video")
    runtime = PluginRuntimeManager({"dong-bo-video": demo}, supervisor)
    assert runtime.start("dong-bo-video")["ok"] is True
    runtime.snapshot()
    runtime.set_safe_mode(True)
