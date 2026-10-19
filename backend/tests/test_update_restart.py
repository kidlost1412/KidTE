import json
import sys
import time
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import server
import update_launcher as launcher
import update_restart as restart
from test_update_launcher import FakeJob, FakeProcess, FakeStore, _component


def test_marker_is_consumed_once_and_only_for_the_matching_nonce(tmp_path):
    restart.write_marker(tmp_path, "n1")
    assert restart.consume_marker(tmp_path, "n1") is True
    assert not restart.marker_path(tmp_path).exists()
    restart.write_marker(tmp_path, "n1")
    assert restart.consume_marker(tmp_path, "other") is False
    assert not restart.marker_path(tmp_path).exists()
    assert restart.consume_marker(tmp_path, "n1") is False


def test_stale_or_corrupt_marker_is_rejected(tmp_path):
    restart.marker_path(tmp_path).write_text(json.dumps(
        {"nonce": "n", "requested_at": time.time() - 3600}), encoding="utf-8")
    assert restart.consume_marker(tmp_path, "n") is False
    restart.marker_path(tmp_path).write_text("{not json", encoding="utf-8")
    assert restart.consume_marker(tmp_path, "n") is False


def test_evaluate_restart_rules():
    ev = restart.evaluate_restart
    assert ev(has_pending=True, live=False, payload={}, supported=False)["status"] == 409
    nothing = ev(has_pending=False, live=False, payload={}, supported=True)
    assert nothing["go"] is False and nothing["body"]["error"] == "update_nothing_to_apply"
    assert ev(has_pending=False, live=False, payload={"force": True}, supported=True)["go"] is True
    needs = ev(has_pending=True, live=True, payload={}, supported=True)
    assert needs["go"] is False and needs["body"]["needs_confirm"] is True
    assert ev(has_pending=True, live=True, payload={"confirm_live": True}, supported=True)["go"] is True
    assert ev(has_pending=True, live=False, payload={}, supported=True)["go"] is True


def _make_launcher(app_root, store, spawn, *, ready=None):
    children = spawn.children

    def ready_probe(port):
        if ready is not None:
            return ready(children)
        if not children or children[-1][0].exited:
            return None
        process, env = children[-1]
        return {"ok": True, "pid": process.pid, "launcher_nonce": env["KIDTE_LAUNCH_NONCE"],
                "launcher_generation": env["KIDTE_LAUNCH_GENERATION"],
                "data_root": env["KID_TE_ROOT"]}
    return launcher.Launcher(store_factory=lambda _: store, popen=spawn, ready_probe=ready_probe,
                             occupied_probe=lambda _: False, job_factory=FakeJob,
                             sleep=lambda _: None, environ={})


class Spawner:
    """Fake popen: each child exits with the next scripted code from wait()."""

    def __init__(self, app_root, codes, *, write_marker_for=()):
        self.children = []
        self.codes = list(codes)
        self.app_root = app_root
        self.marker_for = set(write_marker_for)

    def __call__(self, command, *, env, **kwargs):
        index = len(self.children)
        code = self.codes[index] if index < len(self.codes) else 0
        process = FakeProcess()
        process.exited = False

        def wait(timeout=None, c=code, p=process):
            p.exited = True
            return c
        process.wait = wait
        if index in self.marker_for:
            restart.write_marker(self.app_root, env["KIDTE_LAUNCH_NONCE"])
        self.children.append((process, env))
        return process


def test_launcher_restarts_when_core_requests_it_then_activates_pending(tmp_path):
    app_root = tmp_path / "App"
    app_root.mkdir()
    (app_root / "KidTE.exe").write_bytes(b"x")
    store = FakeStore(app_root, active=_component(app_root, "1.0.0"))
    spawn = Spawner(app_root, [restart.RESTART_EXIT_CODE, 0], write_marker_for={0})
    original_wait = store.load_pending
    # After the first run a pending generation appears (the updater staged it).
    runs = {"n": 0}
    real_recover = store.recover

    def recover():
        real_recover()
        runs["n"] += 1
        if runs["n"] == 2:
            store.pending = {"marker": True}
    store.recover = recover
    result = _make_launcher(app_root, store, spawn).run(app_root / "KidTELauncher.exe")
    assert result == 0
    assert len(spawn.children) == 2
    assert store.confirmed == ["new-generation"]
    assert spawn.children[1][1]["KIDTE_LAUNCH_GENERATION"] == "new-generation"
    assert not restart.marker_path(app_root).exists()


def test_restart_exit_without_matching_marker_is_not_a_restart(tmp_path):
    app_root = tmp_path / "App"
    app_root.mkdir()
    store = FakeStore(app_root, pending=True)
    spawn = Spawner(app_root, [restart.RESTART_EXIT_CODE])
    result = _make_launcher(app_root, store, spawn).run(app_root / "KidTELauncher.exe")
    assert result == restart.RESTART_EXIT_CODE
    assert len(spawn.children) == 1


def test_restart_loop_is_capped(tmp_path):
    app_root = tmp_path / "App"
    app_root.mkdir()
    store = FakeStore(app_root, pending=True)
    spawn = Spawner(app_root, [restart.RESTART_EXIT_CODE] * 20, write_marker_for=range(20))
    result = _make_launcher(app_root, store, spawn).run(app_root / "KidTELauncher.exe")
    assert result == restart.RESTART_EXIT_CODE
    assert len(spawn.children) == launcher.MAX_RESTARTS_PER_WINDOW + 1
    assert "too often" in (app_root / "launcher-startup.log").read_text(encoding="utf-8")


def test_foreign_core_does_not_roll_back_or_lose_the_pending_update(tmp_path):
    app_root = tmp_path / "App"
    app_root.mkdir()
    store = FakeStore(app_root, pending=True)
    store.restore_pending = lambda pending: setattr(store, "pending", pending)
    created = []

    def popen(command, *, env, **kwargs):
        # Our core loses the instance lock and exits with ALREADY_RUNNING.
        process = FakeProcess(restart.ALREADY_RUNNING_EXIT_CODE)
        created.append((process, env))
        return process

    def ready(_probe):
        # ...while the other core answers readiness with its own nonce.
        return {"ok": True, "pid": 4242, "launcher_nonce": "someone-else",
                "launcher_generation": "old", "data_root": created[-1][1]["KID_TE_ROOT"]}

    app = launcher.Launcher(store_factory=lambda _: store, popen=popen,
                            ready_probe=lambda port: ready(port) if created else None,
                            occupied_probe=lambda _: False, job_factory=FakeJob,
                            sleep=lambda _: None, environ={})
    with pytest.raises(launcher.AlreadyRunning):
        app.run(app_root / "KidTELauncher.exe")
    assert store.confirmed == []
    assert store.pending == {"marker": True}  # update kept for the next start
    log = (app_root / "launcher-startup.log").read_text(encoding="utf-8")
    assert "another Kid TE instance is running" in log


def test_readiness_mismatch_reasons_are_logged(tmp_path):
    app_root = tmp_path / "App"
    app_root.mkdir()
    store = FakeStore(app_root, pending=True)
    (app_root / "KidTE.exe").write_bytes(b"x")
    children = []

    def popen(command, *, env, **kwargs):
        process = FakeProcess()
        children.append((process, env))
        return process

    def ready(_children):
        if not _children:
            return None
        return {"ok": True, "pid": 1, "launcher_nonce": "zzz", "launcher_generation": "g",
                "data_root": str(tmp_path / "elsewhere")}

    app = launcher.Launcher(store_factory=lambda _: store, popen=popen,
                            ready_probe=lambda port: ready(children),
                            occupied_probe=lambda _: False, job_factory=FakeJob,
                            startup_timeout=0.05, sleep=lambda _: time.sleep(0.01), environ={})
    with pytest.raises(launcher.LauncherError):
        app.run(app_root / "KidTELauncher.exe")
    log = (app_root / "launcher-startup.log").read_text(encoding="utf-8")
    assert "readiness mismatch" in log and "nonce differs" in log and "data_root expected" in log


def test_second_launcher_focuses_running_one_and_exits(tmp_path, monkeypatch):
    app_root = tmp_path / "App"
    app_root.mkdir()
    focused = []
    monkeypatch.setattr(launcher, "_call_focus", lambda port: focused.append(port))
    with launcher._launcher_lock(app_root):
        app = launcher.Launcher(store_factory=lambda _: pytest.fail("must not start"),
                                popen=lambda *a, **k: pytest.fail("must not spawn"),
                                environ={})
        with pytest.raises(launcher.AlreadyRunning):
            app.run(app_root / "KidTELauncher.exe")
    assert focused


def test_restart_endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "ROOT", tmp_path / "DuLieu")
    monkeypatch.setenv("KIDTE_LAUNCHER_CAPS", "split-components restart-exit")
    monkeypatch.setenv("KIDTE_LAUNCH_NONCE", "nonce")
    calls = []
    state = {"restart_required": True}
    live = {"v": True}
    manager = SimpleNamespace(state=lambda: dict(state), action=lambda a, b: {"ok": True})
    engine = SimpleNamespace(updates=manager, request_restart=lambda: calls.append(1),
                             _is_live_running=lambda: live["v"])

    class Handler(server.Handler):
        pass

    Handler.engine = engine
    http = server.LoopbackHTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=http.serve_forever, kwargs={"poll_interval": .05}, daemon=True).start()
    origin = f"http://127.0.0.1:{http.server_port}"

    def post(body, request_origin=origin):
        request = Request(origin + "/api/updates/restart", data=json.dumps(body).encode(),
                          headers={"Origin": request_origin, "Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            return error.code, json.loads(error.read())

    try:
        status, body = post({})
        assert status == 409 and body["needs_confirm"] is True and not calls
        status, body = post({"confirm_live": True})
        assert status == 200 and body["restarting"] is True
        deadline = time.time() + 3
        while not calls and time.time() < deadline:
            time.sleep(0.05)
        assert calls == [1]
        live["v"] = False
        state["restart_required"] = False
        assert post({})[1]["error"] == "update_nothing_to_apply"
        assert post({}, "chrome-extension://foreign")[0] == 403
    finally:
        http.shutdown()
        http.server_close()
