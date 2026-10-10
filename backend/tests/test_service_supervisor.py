import sys
import subprocess
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from service_supervisor import ServiceSpec, ServiceSupervisor


class _Process:
    def __init__(self, pid): self.pid = pid; self.terminated = False
    def terminate(self): self.terminated = True
    def wait(self, timeout): return 0


def _spec(tmp_path):
    return ServiceSpec("dich-vu-mau", ("python.exe", "chay.py"), tmp_path)


def test_supervisor_does_not_spawn_duplicate_and_attaches_after_core_restart(tmp_path):
    calls = []
    supervisor = ServiceSupervisor(
        tmp_path, popen=lambda *args, **kwargs: calls.append((args, kwargs)) or _Process(11),
        pid_alive=lambda pid: pid == 11, image_for_pid=lambda pid: "python.exe",
    )
    spec = _spec(tmp_path)
    assert supervisor.start(spec) == {"state": "running", "pid": 11, "started": True}
    attached = supervisor.start(spec)
    assert {key: attached[key] for key in ("state", "pid", "attached", "started")} == {
        "state": "running", "pid": 11, "attached": True, "started": False,
    }
    assert attached["uptime_s"] >= 0
    restarted_core = ServiceSupervisor(tmp_path, pid_alive=lambda pid: pid == 11,
                                       image_for_pid=lambda pid: "python.exe")
    recovered = restarted_core.attach(spec)
    assert {key: recovered[key] for key in ("state", "pid", "attached")} == {
        "state": "running", "pid": 11, "attached": True,
    }
    assert recovered["uptime_s"] >= 0
    assert len(calls) == 1


def test_pid_reuse_with_wrong_executable_is_never_attached_or_killed(tmp_path):
    supervisor = ServiceSupervisor(tmp_path, popen=lambda *_a, **_k: _Process(22),
                                   pid_alive=lambda _pid: True,
                                   image_for_pid=lambda _pid: "notepad.exe")
    spec = _spec(tmp_path)
    supervisor._write_record(spec, 22)
    assert supervisor.attach(spec) == {"state": "stopped", "reason": "pid_identity_mismatch"}
    assert not (tmp_path / "services" / "dich-vu-mau.pid.json").exists()


def test_restart_limit_requires_attention(tmp_path):
    clock = [0]
    supervisor = ServiceSupervisor(tmp_path, popen=lambda *_a, **_k: _Process(33),
                                   clock=lambda: clock[0], pid_alive=lambda _pid: False,
                                   image_for_pid=lambda _pid: "", max_restarts=2)
    spec = _spec(tmp_path)
    assert supervisor.start(spec)["state"] == "running"
    clock[0] = 1
    assert supervisor.start(spec)["state"] == "running"
    clock[0] = 3
    assert supervisor.start(spec) == {"state": "needs_attention", "reason": "restart_limit"}


def test_restart_has_backoff_and_start_failure_is_reported(tmp_path):
    clock = [0]
    supervisor = ServiceSupervisor(tmp_path, popen=lambda *_a, **_k: _Process(55),
                                   clock=lambda: clock[0], pid_alive=lambda _pid: False,
                                   image_for_pid=lambda _pid: "")
    spec = _spec(tmp_path)
    assert supervisor.start(spec)["state"] == "running"
    assert supervisor.start(spec) == {"state": "backoff", "reason": "restart_backoff",
                                      "retry_after_s": 1.0}
    broken = ServiceSupervisor(tmp_path / "broken",
                               popen=lambda *_a, **_k: (_ for _ in ()).throw(OSError("no")),
                               clock=lambda: 0, pid_alive=lambda _pid: False)
    assert broken.start(spec)["reason"] == "start_failed"


def test_stop_is_soft_for_owned_process_and_never_kills_attached_pid(tmp_path):
    process = _Process(44)
    supervisor = ServiceSupervisor(tmp_path, popen=lambda *_a, **_k: process,
                                   pid_alive=lambda pid: pid == 44,
                                   image_for_pid=lambda _pid: "python.exe")
    spec = _spec(tmp_path)
    supervisor.start(spec)
    assert supervisor.stop(spec) == {"state": "stopped", "reason": "graceful"}
    assert process.terminated is True
    # Sau khi mở lại app: PID lấy từ file. PID bị cấp lại cho tiến trình KHÁC
    # (dòng lệnh không phải của service) thì tuyệt đối không động tới…
    stranger = _Attached(["python.exe", "khac.py"])
    recovered = ServiceSupervisor(tmp_path, pid_alive=lambda _pid: True,
                                  image_for_pid=lambda _pid: "python.exe",
                                  process_for_pid=lambda _pid: stranger)
    recovered._write_record(spec, 45)
    assert recovered.stop(spec) == {"state": "needs_attention", "reason": "pid_identity_mismatch"}
    assert stranger.terminated is False
    # …còn đúng tiến trình của service thì dừng được (bản cũ báo "không dừng được").
    own = _Attached(["python.exe", *spec.command[1:]])
    recovered.process_for_pid = lambda _pid: own
    assert recovered.stop(spec) == {"state": "stopped", "reason": "graceful_attached"}
    assert own.terminated is True


class _Attached:
    def __init__(self, cmdline):
        self._cmdline, self.terminated = cmdline, False

    def cmdline(self):
        return self._cmdline

    def terminate(self):
        self.terminated = True

    def wait(self, timeout):
        return 0


def test_stop_timeout_is_reported_and_pid_record_is_kept(tmp_path):
    class SlowProcess(_Process):
        def wait(self, timeout):
            raise subprocess.TimeoutExpired("plugin", timeout)
    process = SlowProcess(66)
    supervisor = ServiceSupervisor(tmp_path, popen=lambda *_a, **_k: process,
                                   pid_alive=lambda _pid: True,
                                   image_for_pid=lambda _pid: "python.exe")
    spec = _spec(tmp_path)
    supervisor.start(spec)
    assert supervisor.stop(spec, timeout_s=10) == {
        "state": "needs_attention", "reason": "stop_timeout",
    }
    assert (tmp_path / "services" / "dich-vu-mau.pid.json").is_file()


def test_service_died_between_core_restarts_is_reported_stale_not_reattached(tmp_path):
    """`[T9]` Kịch bản thật: service chết (crash/máy tắt đột ngột) TRONG LÚC
    core không chạy. Lần mở lại sau, PID trong file lock không còn sống nữa
    — phải báo `stale_pid` và xoá file, TUYỆT ĐỐI không được coi là đang
    chạy hay tự spawn PID mới đè lên (start() sẽ tự làm việc đó ở lần gọi
    sau, không phải nhiệm vụ của attach())."""
    dead_supervisor = ServiceSupervisor(tmp_path, popen=lambda *_a, **_k: _Process(77),
                                        pid_alive=lambda _pid: True,
                                        image_for_pid=lambda _pid: "python.exe")
    spec = _spec(tmp_path)
    dead_supervisor.start(spec)
    assert (tmp_path / "services" / "dich-vu-mau.pid.json").is_file()

    # Core mo lai SAU KHI service da chet that (pid khong con song nua).
    reopened_core = ServiceSupervisor(tmp_path, pid_alive=lambda _pid: False,
                                      image_for_pid=lambda _pid: "python.exe")
    result = reopened_core.attach(spec)

    assert result == {"state": "stopped", "reason": "stale_pid"}
    assert not (tmp_path / "services" / "dich-vu-mau.pid.json").exists()


def test_no_pid_file_at_all_is_reported_stopped_not_crashed(tmp_path):
    """Chưa từng chạy lần nào (không có file lock) phải khác hẳn với 'đã
    từng chạy rồi chết' — cùng trả `state: stopped` nhưng lý do khác, để
    log/UI không báo nhầm 'service vừa chết' cho một plugin chưa bật bao giờ."""
    supervisor = ServiceSupervisor(tmp_path, pid_alive=lambda _pid: False)
    result = supervisor.attach(_spec(tmp_path))
    assert result == {"state": "stopped", "reason": "no_pid_file"}


def test_core_restart_replaces_attached_service_running_older_code(tmp_path):
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / "chay.py").write_text("VERSION = 1\n", encoding="utf-8")
    spec = ServiceSpec("dich-vu-mau", ("python.exe", "chay.py"), plugin)
    roots = (plugin,)
    old_core = ServiceSupervisor(tmp_path, popen=lambda *_a, **_k: _Process(70),
                                 pid_alive=lambda _pid: True,
                                 image_for_pid=lambda _pid: "python.exe",
                                 fingerprint_roots=roots)
    assert old_core.start(spec)["pid"] == 70
    # Cùng mã: vẫn attach, không khởi chạy lại.
    same = ServiceSupervisor(tmp_path, pid_alive=lambda _pid: True,
                             image_for_pid=lambda _pid: "python.exe", fingerprint_roots=roots)
    assert same.start(spec)["started"] is False
    # Mã đổi sau khi service khởi chạy: dừng cả cây tiến trình và chạy bản mới.
    (plugin / "chay.py").write_text("VERSION = 2\n", encoding="utf-8")
    old_tree = _Attached(["python.exe", "chay.py"])
    spawned = []
    new_core = ServiceSupervisor(
        tmp_path, popen=lambda *_a, **_k: spawned.append(1) or _Process(71),
        pid_alive=lambda _pid: True, image_for_pid=lambda _pid: "python.exe",
        process_for_pid=lambda _pid: old_tree, fingerprint_roots=roots)
    assert new_core.status(spec)["code_stale"] is True
    result = new_core.start(spec)
    assert old_tree.terminated is True and spawned == [1]
    assert result["pid"] == 71 and result["started"] is True
    assert new_core.status(spec)["code_stale"] is False
