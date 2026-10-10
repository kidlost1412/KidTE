import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import update_launcher as launcher
from update_catalog import CORE_DATA_SCHEMA


def _component(app_root, version="2.0.0"):
    target = app_root / "packages" / "core" / version
    target.mkdir(parents=True)
    executable = target / "KidTE.exe"
    executable.write_bytes(b"signed-core")
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    package = {
        "id": "core", "entrypoint": "KidTE.exe",
        "migration": "none",
        "compatibility": {"data_schema": CORE_DATA_SCHEMA},
        "files": [{"path": "KidTE.exe", "sha256": digest}],
    }
    return {"package": package, "path": f"packages/core/{version}"}


class FakeStore:
    def __init__(self, app_root, *, pending=False, active=None):
        self.app_root = app_root
        self.active = {"generation": "prior-generation", "components": {"core": active}} if active else None
        self.pending = ({"marker": True} if pending else None)
        self.rollback_called = False
        self.confirmed = []
        self.recovered = False

    def recover(self):
        self.recovered = True

    def load_pending(self):
        return self.pending

    def activate_pending(self):
        self.pending = None
        self.active = {"generation": "new-generation", "components": {"core": _component(self.app_root)}}
        return self.active

    def load_active(self):
        return self.active

    def confirm_active(self, generation):
        self.confirmed.append(generation)

    def rollback(self):
        self.rollback_called = True
        self.active = {"generation": "prior-generation", "components": {"core": None}}
        return self.active


class FakeProcess:
    pid = 9876

    def __init__(self, code=None):
        self.code = code
        self.terminated = False
        self.waited = False

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        self.waited = True
        return 0 if self.code is None else self.code

    def terminate(self):
        self.terminated = True
        self.code = 1

    def kill(self):
        self.terminated = True
        self.code = 1


class FakeJob:
    def __init__(self):
        self.process = None
        self.closed = False

    def attach(self, process):
        self.process = process

    def close(self):
        self.closed = True


def test_child_environment_defaults_data_root_and_carries_signed_gpu_selection(tmp_path):
    app_root = tmp_path / "App"
    data_root = tmp_path / "DuLieu"
    runtime = app_root / "packages" / "gpu-cuda12" / "3.0.0"
    runtime.mkdir(parents=True)
    (runtime / "gpu-runtime.json").write_text("{}", encoding="utf-8")
    active = {"components": {"gpu-cuda12": {
        "path": "packages/gpu-cuda12/3.0.0",
        "package": {"files": [{"path": "gpu-runtime.json", "sha256": "a" * 64}]},
    }}}
    gpu_root, gpu_hash = launcher._select_gpu(app_root, active)
    env = launcher._environment(app_root, data_root, 8793, {}, "g1", "nonce", gpu_root, gpu_hash)

    assert env["KID_TE_ROOT"] == str(data_root)
    assert env["KIDTE_APP_ROOT"] == str(app_root.resolve())
    assert env["KIDTE_GPU_RUNTIME_ROOT"] == str(runtime.resolve())
    assert env["KIDTE_GPU_RUNTIME_SHA256"] == "a" * 64
    assert env["CHAT_HOST"] == "127.0.0.1"


def test_explicit_data_root_overrides_are_preserved(tmp_path):
    app_root = tmp_path / "App"
    source = {"LIVE_RE_ROOT": "relative-live"}
    data_root = launcher._data_root(app_root, source)
    env = launcher._environment(app_root, data_root, 8793,
                                source, "g1", "n", None, None)
    assert env["LIVE_RE_ROOT"] == "relative-live"
    assert env["KID_TE_ROOT"] == str(data_root)


def test_explicit_gpu_runtime_trust_anchor_is_preserved(tmp_path):
    source = {"KIDTE_GPU_RUNTIME_ROOT": "D:/operator/runtime",
              "KIDTE_GPU_RUNTIME_SHA256": "b" * 64}
    env = launcher._environment(tmp_path / "App", tmp_path / "DuLieu", 8793,
                                source, "g1", "n", None, None)
    assert env["KIDTE_GPU_RUNTIME_ROOT"] == source["KIDTE_GPU_RUNTIME_ROOT"]
    assert env["KIDTE_GPU_RUNTIME_SHA256"] == source["KIDTE_GPU_RUNTIME_SHA256"]


def test_launcher_confirms_only_after_exact_pid_nonce_generation_readiness(tmp_path):
    app_root = tmp_path / "App"
    app_root.mkdir()
    fake_store = FakeStore(app_root, pending=True)
    children = []
    jobs = []

    def spawn(command, *, env, **kwargs):
        process = FakeProcess()
        children.append((process, env))
        return process

    def ready_probe(port):
        if not children:
            return None
        process, env = children[-1]
        return {"ok": True, "pid": process.pid,
                "launcher_nonce": env["KIDTE_LAUNCH_NONCE"],
                "launcher_generation": env["KIDTE_LAUNCH_GENERATION"],
                "data_root": env["KID_TE_ROOT"]}

    def job_factory():
        job = FakeJob()
        jobs.append(job)
        return job

    app = launcher.Launcher(store_factory=lambda _: fake_store, popen=spawn,
                            ready_probe=ready_probe, occupied_probe=lambda _: False,
                            job_factory=job_factory, environ={})
    result = app.run(app_root / "KidTELauncher.exe")

    assert result == 0
    assert fake_store.recovered
    assert fake_store.confirmed == ["new-generation"]
    assert children[0][1]["KIDTE_LAUNCH_GENERATION"] == "new-generation"
    assert jobs[0].closed


def test_bad_readiness_rolls_back_and_launches_previous_only_once(tmp_path):
    app_root = tmp_path / "App"
    app_root.mkdir()
    baseline = app_root / "KidTE.exe"
    baseline.write_bytes(b"baseline")
    fake_store = FakeStore(app_root, pending=True)
    children = []

    def spawn(command, **kwargs):
        process = FakeProcess()
        children.append(process)
        return process

    app = launcher.Launcher(
        store_factory=lambda _: fake_store, popen=spawn,
        ready_probe=lambda _: None, occupied_probe=lambda _: False,
        job_factory=FakeJob, startup_timeout=0, sleep=lambda _: None, environ={},
    )
    with pytest.raises(launcher.LauncherError, match="could not start safely"):
        app.run(app_root / "KidTELauncher.exe")

    assert fake_store.rollback_called
    assert len(children) == 2
    assert all(process.terminated for process in children)
    assert fake_store.confirmed == []


def test_pending_validation_failure_keeps_the_previous_core_running(tmp_path):
    app_root = tmp_path / "App"
    app_root.mkdir()
    (app_root / "KidTE.exe").write_bytes(b"baseline")

    class CorruptPending(FakeStore):
        def activate_pending(self):
            raise RuntimeError("pending file digest mismatch")

        def cancel_pending(self):
            self.pending = None
            self.cancelled = True

    store = CorruptPending(app_root, pending=True)
    processes = []

    def spawn(command, *, env, **kwargs):
        process = FakeProcess()
        processes.append((process, env))
        assert Path(command[0]) == app_root / "KidTE.exe"
        return process

    def ready(_port):
        if not processes:
            return None
        process, env = processes[-1]
        return {"ok": True, "pid": process.pid, "launcher_nonce": env["KIDTE_LAUNCH_NONCE"],
                "launcher_generation": env["KIDTE_LAUNCH_GENERATION"], "data_root": env["KID_TE_ROOT"]}

    app = launcher.Launcher(store_factory=lambda _: store, popen=spawn, ready_probe=ready,
                            occupied_probe=lambda _: False, job_factory=FakeJob, environ={})
    assert app.run(app_root / "KidTELauncher.exe") == 0
    assert store.cancelled
    assert store.confirmed == []
    assert len(processes) == 1
    assert "pending update rejected" in (app_root / "launcher-startup.log").read_text(encoding="utf-8")


def test_running_instance_preflight_does_not_touch_store_or_activate(tmp_path, monkeypatch):
    app_root = tmp_path / "App"
    app_root.mkdir()
    data_root = tmp_path / "DuLieu"
    lock = data_root / "runtime" / "live-re.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text(json.dumps({"pid": 123}), encoding="utf-8")
    monkeypatch.setattr(launcher, "_is_pid_alive", lambda pid: True)
    created = []
    app = launcher.Launcher(store_factory=lambda _: created.append(True),
                            ready_probe=lambda _: None, occupied_probe=lambda _: False,
                            environ={})

    with pytest.raises(launcher.LauncherError, match="already running"):
        app.run(app_root / "KidTELauncher.exe")
    assert created == []


def test_launcher_packaging_is_separate_from_legacy_core():
    spec = (Path(__file__).resolve().parents[2] / "packaging" / "Updater.spec").read_text(encoding="utf-8")
    entry = (Path(__file__).resolve().parents[2] / "packaging" / "updater_entry.py").read_text(encoding="utf-8")
    assert 'name="KidTELauncher"' in spec
    assert "update_launcher" in entry
    assert "name=\"KidTE\"" not in spec


def test_main_records_startup_failure_in_app_directory(tmp_path, monkeypatch):
    class BrokenLauncher:
        def __init__(self):
            pass

        def run(self, **kwargs):
            raise launcher.LauncherError("busy port")

    monkeypatch.setattr(launcher, "Launcher", BrokenLauncher)
    monkeypatch.setattr(launcher, "_app_root", lambda: tmp_path)
    monkeypatch.setattr(launcher.os, "name", "posix")
    assert launcher.main([]) == 2
    assert "busy port" in (tmp_path / "launcher-startup.log").read_text(encoding="utf-8")


def test_corrupt_active_core_rolls_back_to_baseline_without_changing_data_root(tmp_path):
    app_root = tmp_path / "App"
    app_root.mkdir()
    baseline = app_root / "KidTE.exe"
    baseline.write_bytes(b"baseline")
    broken_component = _component(app_root)
    (app_root / broken_component["path"] / "KidTE.exe").write_bytes(b"tampered")
    gpu_root = app_root / "packages" / "gpu-cuda12" / "3.0.0"
    gpu_root.mkdir(parents=True)
    (gpu_root / "gpu-runtime.json").write_text("{}", encoding="utf-8")
    store = FakeStore(app_root, active=broken_component)
    store.active["components"]["gpu-cuda12"] = {
        "path": "packages/gpu-cuda12/3.0.0",
        "package": {"files": [{"path": "gpu-runtime.json", "sha256": "a" * 64}]},
    }
    processes = []
    jobs = []
    data_root = tmp_path / "operator-data"

    def spawn(command, *, env, **kwargs):
        process = FakeProcess()
        processes.append((process, Path(command[0]), env))
        return process

    def ready(port):
        if not processes:
            return None
        process, _, env = processes[-1]
        return {"ok": True, "pid": process.pid,
                "launcher_nonce": env["KIDTE_LAUNCH_NONCE"],
                "launcher_generation": env["KIDTE_LAUNCH_GENERATION"],
                "data_root": env["KID_TE_ROOT"]}

    app = launcher.Launcher(
        store_factory=lambda _: store, popen=spawn, ready_probe=ready,
        occupied_probe=lambda _: False, job_factory=lambda: jobs.append(FakeJob()) or jobs[-1],
        environ={"KID_TE_ROOT": str(data_root)},
    )
    assert app.run(app_root / "KidTELauncher.exe") == 0

    _, selected, env = processes[0]
    assert selected == baseline
    assert env["KID_TE_ROOT"] == str(data_root.resolve())
    assert "KIDTE_GPU_RUNTIME_ROOT" not in env
    assert "KIDTE_GPU_RUNTIME_SHA256" not in env
    assert not data_root.exists()
    assert store.rollback_called
    assert "starting legacy baseline" in (app_root / "launcher-startup.log").read_text(encoding="utf-8")


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object integration")
def test_windows_job_attaches_short_lived_dummy_process(tmp_path):
    job = launcher._WindowsJob()
    process = subprocess.Popen(
        [sys.executable, "-c", "pass"], cwd=str(tmp_path),
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    try:
        job.attach(process)
        assert process.wait(timeout=10) == 0
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        job.close()
