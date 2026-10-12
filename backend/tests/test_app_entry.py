import sys
from pathlib import Path
from unittest import mock

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import app_entry
import backup_policy


class _Engine:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _Server:
    def __init__(self, *_):
        self.closed = False

    def serve_forever(self, *_):
        return None

    def server_close(self):
        self.closed = True

    def shutdown(self):
        return None


def test_entry_releases_lock_when_server_exits_immediately(tmp_path):
    engine = _Engine()
    old_argv = sys.argv
    try:
        sys.argv = ["app_entry.py", "--port", "0", "--no-ui"]
        with mock.patch.object(app_entry, "ROOT", tmp_path), \
             mock.patch.object(app_entry, "LOG_DIR", tmp_path / "logs"), \
             mock.patch.object(backup_policy, "ensure_startup_backup", side_effect=AssertionError("startup backup forbidden")), \
             mock.patch.object(app_entry, "ReplyEngine", return_value=engine), \
             mock.patch.object(app_entry, "discover_plugins", return_value=({}, {})), \
                 mock.patch.object(app_entry, "LoopbackHTTPServer", _Server), \
             mock.patch.object(app_entry.signal, "signal"):
            assert app_entry.main() == 0
    finally:
        sys.argv = old_argv
    assert engine.closed is True
    assert not (tmp_path / "runtime" / "live-re.lock").exists()


def test_entry_releases_lock_if_engine_initialization_fails(tmp_path):
    old_argv = sys.argv
    try:
        sys.argv = ["app_entry.py", "--port", "0", "--no-ui"]
        with mock.patch.object(app_entry, "ROOT", tmp_path), \
             mock.patch.object(app_entry, "LOG_DIR", tmp_path / "logs"), \
             mock.patch.object(backup_policy, "ensure_startup_backup", side_effect=AssertionError("startup backup forbidden")), \
             mock.patch.object(app_entry, "ReplyEngine", side_effect=RuntimeError("fail")), \
             mock.patch.object(app_entry, "discover_plugins", return_value=({}, {})):
            with pytest.raises(RuntimeError, match="fail"):
                app_entry.main()
    finally:
        sys.argv = old_argv
    assert not (tmp_path / "runtime" / "live-re.lock").exists()


def test_desktop_ui_starts_backend_before_window_and_stops_on_close():
    events = []

    class Server(_Server):
        def serve_forever(self, *_):
            events.append("backend")

        def shutdown(self):
            events.append("shutdown")

    class Webview:
        @staticmethod
        def create_window(title, url, **kwargs):
            events.append((title, url, kwargs["min_size"]))

        @staticmethod
        def start():
            events.append("window")

    assert app_entry.run_desktop_ui(Server(), "http://127.0.0.1:8793/app",
                                    webview_module=Webview) is True
    assert events[0] == "backend"
    assert events[1][1].endswith("/app")
    assert events[-2:] == ["window", "shutdown"]


def test_desktop_ui_failure_is_reported_for_console_fallback():
    class BrokenWebview:
        @staticmethod
        def create_window(*_args, **_kwargs):
            raise RuntimeError("webview unavailable")

    assert app_entry.run_desktop_ui(_Server(), "http://127.0.0.1:8793/app",
                                    webview_module=BrokenWebview) is False


def test_window_close_error_stops_backend_instead_of_leaving_hidden_server():
    events = []

    class Loaded:
        callback = None

        def __iadd__(self, callback):
            self.callback = callback
            return self

    loaded = Loaded()

    class Server(_Server):
        def shutdown(self):
            events.append("shutdown")

    class Webview:
        @staticmethod
        def create_window(*_args, **_kwargs):
            return type("Window", (), {"events": type("Events", (), {"loaded": loaded})()})()

        @staticmethod
        def start():
            loaded.callback()
            raise TypeError("cannot unpack non-iterable NoneType object")

    assert app_entry.run_desktop_ui(Server(), "http://127.0.0.1:8793/app",
                                    webview_module=Webview) is True
    assert events == ["shutdown"]


def test_notify_existing_instance_posts_local_focus():
    response = mock.MagicMock()
    response.__enter__.return_value.status = 200
    response.__enter__.return_value.read.return_value = b'{"ok": true}'
    with mock.patch.object(app_entry.urllib.request, "urlopen", return_value=response) as send:
        assert app_entry.notify_existing_instance("127.0.0.1", 8793) is True
    request = send.call_args.args[0]
    assert request.full_url == "http://127.0.0.1:8793/api/focus"


def test_notify_existing_instance_reports_missing_window():
    response = mock.MagicMock()
    response.__enter__.return_value.status = 200
    response.__enter__.return_value.read.return_value = b'{"ok": false}'
    with mock.patch.object(app_entry.urllib.request, "urlopen", return_value=response):
        assert app_entry.notify_existing_instance("127.0.0.1", 8793) is False


def test_existing_backend_ready_rejects_refused_connection():
    with mock.patch.object(app_entry.urllib.request, "urlopen", side_effect=ConnectionRefusedError):
        assert app_entry.existing_backend_ready("127.0.0.1", 8793) is False


def test_existing_lock_does_not_open_window_without_server(tmp_path):
    old_argv = sys.argv
    try:
        sys.argv = ["app_entry.py"]
        with mock.patch.object(app_entry, "ROOT", tmp_path), \
             mock.patch.object(app_entry.SingleInstanceLock, "acquire", side_effect=app_entry.InstanceAlreadyRunning("busy")), \
             mock.patch.object(app_entry, "notify_existing_instance", return_value=False), \
             mock.patch.object(app_entry, "existing_backend_ready", return_value=False), \
             mock.patch.object(app_entry, "run_client_ui") as client:
            assert app_entry.main() == 2
            client.assert_not_called()
    finally:
        sys.argv = old_argv


def test_client_ui_opens_existing_backend_without_starting_another_server(tmp_path):
    events = []

    class Webview:
        @staticmethod
        def create_window(title, url, **kwargs):
            events.append((title, url, kwargs["min_size"]))
            return mock.MagicMock(width=1200, height=800)

        @staticmethod
        def start():
            events.append("window")

    with (mock.patch.object(app_entry, "ROOT", tmp_path),
          mock.patch.object(app_entry.urllib.request, "urlopen")):
        assert app_entry.run_client_ui(
            "http://127.0.0.1:8793/app", webview_module=Webview,
        ) is True
    assert events[0][1].endswith("/app")
    assert events[1] == "window"


def test_app_shutdown_continues_after_captcha_and_engine_failure(tmp_path):
    engine = _Engine()
    engine.captcha = mock.Mock()
    engine.captcha.status.return_value = {"configured": False}
    engine.captcha.stop.side_effect = RuntimeError("captcha failure")
    engine.close = mock.Mock(side_effect=RuntimeError("close failure"))
    runtime = mock.Mock()
    runtime.stop_all.return_value = {"ok": True}
    with (mock.patch.object(sys, "argv", ["app_entry.py", "--port", "0", "--no-ui"]),
          mock.patch.object(app_entry, "ROOT", tmp_path),
          mock.patch.object(app_entry, "LOG_DIR", tmp_path / "logs"),
          mock.patch.object(backup_policy, "ensure_startup_backup", side_effect=AssertionError("startup backup forbidden")),
          mock.patch.object(app_entry, "ReplyEngine", return_value=engine),
          mock.patch.object(app_entry, "PluginRuntimeManager", return_value=runtime),
          mock.patch.object(app_entry, "discover_plugins", return_value=({}, {})),
          mock.patch.object(app_entry, "LoopbackHTTPServer", _Server),
          mock.patch.object(app_entry.signal, "signal")):
        assert app_entry.main() == 0
    engine.captcha.stop.assert_called_once_with(closing=True)
    runtime.stop_all.assert_called_once()
    engine.close.assert_called_once()
    assert not (tmp_path / "runtime" / "live-re.lock").exists()


def test_frozen_direct_start_hands_over_to_launcher(tmp_path):
    (tmp_path / "App").mkdir()
    launcher = tmp_path / "App" / "KidTELauncher.exe"
    launcher.write_bytes(b"x")
    core = tmp_path / "App" / "packages" / "runtime" / "1.0.0"
    core.mkdir(parents=True)
    spawned = []
    assert app_entry.redirect_to_launcher({}, frozen=True, exe_dir=core, spawn=spawned.append, argv=["KidTE.exe"]) is True
    assert spawned == [launcher.resolve()]


def test_launcher_redirect_skipped_with_nonce_source_or_missing_launcher(tmp_path):
    (tmp_path / "KidTELauncher.exe").write_bytes(b"x")
    spawned = []
    call = lambda env, frozen=True, d=tmp_path: app_entry.redirect_to_launcher(
        env, frozen=frozen, exe_dir=d, spawn=spawned.append, argv=["KidTE.exe"])
    assert call({"KIDTE_LAUNCH_NONCE": "abc"}) is False      # launcher always sets it: no loop
    assert call({}, frozen=False) is False                    # source mode
    assert call({"KIDTE_NO_LAUNCHER_REDIRECT": "1"}) is False
    empty = tmp_path / "empty"
    empty.mkdir()
    assert app_entry.find_launcher(empty, max_up=0) is None
    assert app_entry.redirect_to_launcher({}, frozen=True, exe_dir=empty, spawn=spawned.append, argv=["KidTE.exe"]) is True  # parent has it
    assert len(spawned) == 1


def test_launcher_spawn_failure_falls_back_to_normal_start(tmp_path):
    (tmp_path / "KidTELauncher.exe").write_bytes(b"x")
    def boom(_):
        raise OSError("no")
    assert app_entry.redirect_to_launcher({}, frozen=True, exe_dir=tmp_path, spawn=boom, argv=["KidTE.exe"]) is False


def test_launcher_redirect_skipped_when_cli_args_given(tmp_path):
    (tmp_path / "KidTELauncher.exe").write_bytes(b"x")
    spawned = []
    assert app_entry.redirect_to_launcher({}, frozen=True, exe_dir=tmp_path, spawn=spawned.append,
                                          argv=["KidTE.exe", "--no-ui"]) is False
    assert spawned == []
