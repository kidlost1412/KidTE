"""F7/F8/F15: chia tài nguyên khi LIVE, dừng sạch khi thoát, hoãn học khi bận."""
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from unittest import mock

import psutil
import pytest

import resource_share
import transcription_client
import video_analysis


def test_threads_limited_only_when_live(monkeypatch):
    monkeypatch.setattr(resource_share.os, "cpu_count", lambda: 16)
    assert resource_share.live_cpu_threads(True) == 8
    assert resource_share.live_cpu_threads(False) is None
    monkeypatch.setattr(resource_share.os, "cpu_count", lambda: 2)
    assert resource_share.live_cpu_threads(True) == 2


def test_lower_priority_uses_below_normal(monkeypatch):
    proc = mock.Mock()
    monkeypatch.setattr(resource_share.psutil, "Process", lambda pid: proc)
    monkeypatch.setattr(resource_share.psutil, "BELOW_NORMAL_PRIORITY_CLASS", 0x4000,
                        raising=False)
    assert resource_share.lower_priority(123) is True
    proc.nice.assert_called_once_with(0x4000)


def test_ffmpeg_gets_threads_and_low_priority(monkeypatch):
    seen = {}

    class FakePopen:
        pid, returncode = 77, 0

        def __init__(self, command, **kwargs):
            seen["command"] = command

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    monkeypatch.setattr(video_analysis, "_ffmpeg_binary", lambda name: name)
    monkeypatch.setattr(resource_share, "_assign_to_job", lambda p: True)
    with mock.patch.object(resource_share, "lower_priority") as low:
        video_analysis._extract_audio("in.mp4", "out.wav", threads=4)
        assert seen["command"][seen["command"].index("-threads") + 1] == "4"
        low.assert_called_once_with(77)
        video_analysis._extract_audio("in.mp4", "out.wav", threads=None)
        assert "-threads" not in seen["command"]
    assert not resource_share._children


@pytest.mark.parametrize("cpu_threads,expected", [(6, "6"), (None, None)])
def test_worker_env_carries_cpu_threads(monkeypatch, tmp_path, cpu_threads, expected):
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"x")
    model = tmp_path / "m"
    model.mkdir()
    worker = tmp_path / "KidTETranscription.exe"
    worker.write_bytes(b"")
    import component_runtime
    monkeypatch.setattr(component_runtime, "component_path", lambda *a: str(worker))
    monkeypatch.setenv("KID_TE_CPU_THREADS", "99")
    seen = {}

    def run_worker(w, request, env, *a, **k):
        seen["env"] = env
        return 1, b""

    monkeypatch.setattr(transcription_client, "_run_worker", run_worker)
    with pytest.raises(RuntimeError):
        transcription_client.transcribe(str(wav), str(model), cpu_threads=cpu_threads)
    assert seen["env"].get("KID_TE_CPU_THREADS") == expected


def test_worker_passes_cpu_threads_only_on_cpu(monkeypatch):
    import transcription_worker
    monkeypatch.setenv("KID_TE_CPU_THREADS", "3")
    assert transcription_worker._env_cpu_threads() == 3
    monkeypatch.setenv("KID_TE_CPU_THREADS", "abc")
    assert transcription_worker._env_cpu_threads() == 0


@pytest.mark.parametrize("live,label,threads", [
    (True, "Đang chép lời (chia tài nguyên vì đang LIVE)", 4),
    (False, "Đang chép lời", 0),
])
def test_job_status_marks_resource_share(monkeypatch, tmp_path, live, label, threads):
    monkeypatch.setattr(resource_share.os, "cpu_count", lambda: 8)
    manager = video_analysis.VideoAnalysisManager(root=tmp_path, live_provider=lambda: live)
    job = "a" * 32
    folder = tmp_path / job
    folder.mkdir()
    states = []
    real_write = manager._write

    def fake_read(job_id):
        return {"filename": "v.mp4", "state": "analyzing"}

    def fake_write(job_id, **changes):
        states.append(changes)
        return {"filename": "v.mp4"}

    monkeypatch.setattr(manager, "_write", fake_write)
    monkeypatch.setattr(video_analysis, "_probe_video", lambda v: {})
    monkeypatch.setattr(video_analysis, "_sha256", lambda v: "")
    calls = {}
    monkeypatch.setattr(video_analysis, "_extract_audio",
                        lambda v, w, threads=None: calls.setdefault("ff", threads))
    monkeypatch.setattr(video_analysis, "_build_fingerprint", lambda w, d: (10.0, 5))

    def fake_transcribe(wav, cpu_threads=None):
        calls["wh"] = cpu_threads
        raise RuntimeError("stop_here")

    monkeypatch.setattr(video_analysis, "_transcribe", fake_transcribe)
    manager._analyze_one(job)
    row = next(s for s in states if s.get("phase") == "transcribing")
    assert row["phase_label"] == label
    assert row["cpu_threads"] == threads
    assert calls["ff"] == (threads or None) and calls["wh"] == (threads or None)
    del real_write


def test_stop_all_kills_tracked_children(monkeypatch):
    proc = mock.Mock(pid=55)
    monkeypatch.setattr(resource_share, "_assign_to_job", lambda p: True)
    monkeypatch.setattr(resource_share, "lower_priority", lambda pid: True)
    resource_share.adopt(proc)
    with mock.patch("process_cleanup.stop_process_tree") as stop:
        assert resource_share.stop_all() == 1
        stop.assert_called_once_with(proc, timeout_s=3)
    assert not resource_share._children


def test_job_object_kill_on_close_flag(monkeypatch):
    if sys.platform != "win32":
        pytest.skip("Windows only")
    monkeypatch.setitem(resource_share._job, "handle", None)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert resource_share._assign_to_job(child) is True
    finally:
        child.kill()
        child.wait()


def test_app_exit_calls_stop_all():
    import inspect
    import app_entry
    assert "resource_share.stop_all()" in inspect.getsource(app_entry)


def test_learning_waits_while_live_has_recent_comments(tmp_path):
    import learning
    worker = learning.Learner.__new__(learning.Learner)
    now = datetime.now(timezone.utc).isoformat()
    worker._activity = lambda: now
    assert worker._live_busy() is True
    old = datetime.fromtimestamp(time.time() - 600, timezone.utc).isoformat()
    worker._activity = lambda: old
    assert worker._live_busy() is False
    worker._activity = lambda: None
    assert worker._live_busy() is False


def test_learning_loop_defers_job_until_idle(monkeypatch):
    import learning
    import queue
    worker = learning.Learner.__new__(learning.Learner)
    worker._stop = threading.Event()
    worker._queue = queue.Queue()
    worker.BUSY_POLL_S = 0.01
    busy = {"v": True}
    worker._live_busy = lambda: busy["v"]
    ran = []

    def run(run_id):
        ran.append(run_id)
        worker._stop.set()

    worker._run_distill = run
    worker._run_relearn = run
    worker.maybe_auto_relearn = lambda: None
    worker._queue.put(("distill", "r1"))
    thread = threading.Thread(target=worker._loop, daemon=True)
    thread.start()
    time.sleep(0.2)
    assert ran == []
    busy["v"] = False
    thread.join(timeout=5)
    assert ran == ["r1"]
