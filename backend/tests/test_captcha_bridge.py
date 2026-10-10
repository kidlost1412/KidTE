import sys
import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from captcha_bridge import CaptchaBridge
from captcha_secret import has_key, has_telegram, load_key, save_key, save_telegram


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI")
def test_key_is_user_protected_and_never_returned(tmp_path):
    save_key(tmp_path, "test-only-key")
    assert has_key(tmp_path)
    assert load_key(tmp_path) == "test-only-key"
    assert b"test-only-key" not in (tmp_path / "sadcaptcha-key.dpapi").read_bytes()
    with mock.patch.object(CaptchaBridge, "_processes", return_value=([], [])):
        state = CaptchaBridge(tmp_path).status()
    assert state["configured"] is True
    assert "test-only-key" not in str(state)


def test_other_solver_prevents_second_mouse_controller(tmp_path):
    bridge = CaptchaBridge(tmp_path)
    with mock.patch.object(bridge, "_processes", return_value=([], [mock.Mock(pid=101)])):
        result = bridge.start()
    assert result["ok"] is False
    assert result["error"] == "captcha_other_solver_running"


def test_log_distinguishes_drag_from_verified_success(tmp_path):
    path = tmp_path / "tiktok-captcha-solver-logs" / "ttls-captcha-solver.log"
    path.parent.mkdir()
    path.write_text(
        "2026-09-28 13:03:39 | INFO | solver | dragged puzzle slider by 159 pixels; awaiting TikTok verification\n"
        "2026-09-28 13:03:46 | WARNING | solver | puzzle still visible after drag\n"
        "2026-09-28 13:04:21 | INFO | solver | puzzle no longer visible after drag; TikTok result is not confirmed\n",
        encoding="utf-8",
    )
    events = CaptchaBridge(tmp_path)._events()
    assert len(events) == 3
    assert "not confirmed" in events[-1]["detail"]


def test_status_tolerates_log_removed_during_poll(tmp_path):
    bridge = CaptchaBridge(tmp_path)
    native = tmp_path / "tiktok-captcha-solver-logs" / "ttls-captcha-solver.log"
    native.parent.mkdir()
    native.write_text("old line", encoding="utf-8")
    original_open = Path.open

    def opened(path, *args, **kwargs):
        if path == native:
            raise FileNotFoundError(path)
        return original_open(path, *args, **kwargs)

    with (mock.patch.object(Path, "open", opened),
          mock.patch.object(bridge, "_processes", return_value=([], []))):
        state = bridge.status()
    assert state["events"] == []
    assert state["configured"] is False


def test_rejected_key_blocks_autostart(tmp_path):
    bridge = CaptchaBridge(tmp_path)
    (tmp_path / "key-rejected.marker").write_text("rejected", encoding="utf-8")
    with mock.patch.object(bridge, "_processes", return_value=([], [])):
        result = bridge.start()
    assert result["ok"] is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI")
def test_telegram_token_is_not_plaintext(tmp_path):
    save_telegram(tmp_path, "123456:fake-token", "-100123456789")
    assert has_telegram(tmp_path)
    assert b"fake-token" not in (tmp_path / "telegram-token.dpapi").read_bytes()
    assert "fake-token" not in (tmp_path / "telegram.json").read_text(encoding="utf-8")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI")
def test_key_remains_saved_when_admin_prompt_is_declined(tmp_path):
    bridge = CaptchaBridge(tmp_path)
    with (mock.patch.object(bridge, "status", return_value={"running": False}),
          mock.patch.object(bridge, "start", return_value={
              "ok": False, "error": "captcha_admin_declined"})):
        result = bridge.save_key("test-only-key")
    assert result["ok"] is True
    assert result["start_requested"] is False
    assert load_key(tmp_path) == "test-only-key"


def test_admin_badge_uses_worker_child_not_venv_launcher(tmp_path):
    bridge = CaptchaBridge(tmp_path)
    started = datetime.now(timezone.utc)
    (tmp_path / "solver-session.json").write_text(json.dumps({
        "pid": 123, "started_at": started.isoformat(), "admin": True,
    }), encoding="utf-8")
    worker = mock.Mock()
    worker.is_running.return_value = True
    worker.name.return_value = "pythonw.exe"
    worker.create_time.return_value = started.timestamp() - 1
    worker.ppid.return_value = 112
    with (mock.patch("captcha_bridge.psutil.Process", return_value=worker),
          mock.patch.object(bridge, "_processes", return_value=([SimpleNamespace(pid=112)], [])),
          mock.patch.object(bridge, "_monitor_pid", return_value=None)):
        state = bridge.status()
        worker.create_time.return_value = started.timestamp() - 100
        assert bridge._active_session() is None
    assert state["running"] is True
    assert state["pid"] == 123
    assert state["admin"] is True


def test_elevated_worker_is_owned_when_windows_hides_command_line(tmp_path):
    bridge = CaptchaBridge(tmp_path)
    started = datetime.now(timezone.utc)
    (tmp_path / "solver-session.json").write_text(json.dumps({
        "pid": 123, "started_at": started.isoformat(), "admin": True,
    }), encoding="utf-8")
    worker = mock.Mock(pid=123)
    worker.is_running.return_value = True
    worker.name.return_value = "KidTECaptcha.exe"
    worker.create_time.return_value = started.timestamp()
    with (mock.patch("captcha_bridge.psutil.process_iter", return_value=[]),
          mock.patch("captcha_bridge.psutil.Process", return_value=worker)):
        own, external = bridge._processes()
    assert [proc.pid for proc in own] == [123]
    assert external == []


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI")
def test_old_worker_rejection_does_not_block_a_replaced_key(tmp_path):
    bridge = CaptchaBridge(tmp_path)
    save_key(tmp_path, "new-test-key")
    marker = tmp_path / "key-rejected.marker"
    marker.write_text(json.dumps({"key_sha256": hashlib.sha256(
        b"old-test-key").hexdigest()}), encoding="utf-8")
    assert bridge._key_rejected() is False
    marker.write_text(json.dumps({"key_sha256": hashlib.sha256(
        b"new-test-key").hexdigest()}), encoding="utf-8")
    assert bridge._key_rejected() is True
    marker.write_text("rejected", encoding="utf-8")
    assert bridge._key_rejected() is True


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI")
def test_telegram_rejects_bots_own_id(tmp_path):
    with pytest.raises(ValueError, match="captcha_telegram_chat_is_bot"):
        save_telegram(tmp_path, "123456:fake-token", "123456")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI")
def test_saving_telegram_starts_observer_for_running_solver(tmp_path):
    bridge = CaptchaBridge(tmp_path)
    state = {"running": True, "telegram_configured": True,
             "monitor_pid": None, "pid": 123}
    with (mock.patch.object(bridge, "status", return_value=state),
          mock.patch.object(bridge, "_monitor_command", return_value=["powershell.exe"]),
          mock.patch("captcha_bridge.subprocess.Popen") as launch):
        result = bridge.save_telegram("123456:fake-token", "-100123456789")
    assert result["ok"] is True
    assert result["monitor_start_requested"] is True
    launch.assert_called_once()
    flags = launch.call_args.kwargs["creationflags"]
    assert not flags & __import__("subprocess").DETACHED_PROCESS


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI")
def test_chat_id_can_be_corrected_without_reentering_bot_token(tmp_path):
    bridge = CaptchaBridge(tmp_path)
    save_telegram(tmp_path, "123456:fake-token", "999999")
    with (mock.patch.object(bridge, "status", return_value={"running": False,
          "telegram_configured": True, "monitor_pid": None})):
        bridge.save_telegram("", "888888")
    from captcha_secret import load_telegram
    assert load_telegram(tmp_path) == ("123456:fake-token", "888888")


def test_another_kid_te_instance_is_not_mistaken_for_ours(tmp_path):
    bridge = CaptchaBridge(tmp_path / "this-instance")
    other = mock.Mock()
    other.info = {"pid": 555, "name": "KidTECaptcha.exe", "cmdline": [
        r"C:\Other\KidTECaptcha.exe", "--data-dir", str(tmp_path / "other-instance"),
    ]}
    with mock.patch("captcha_bridge.psutil.process_iter", return_value=[other]):
        own, external = bridge._processes()
    assert own == []
    assert external == [other]


def test_selected_solver_package_is_used_for_frozen_command(tmp_path):
    bridge = CaptchaBridge(tmp_path / "captcha-data")
    package_exe = tmp_path / "components" / "captcha-solver" / "1.0.0" / "Captcha" / "KidTECaptcha.exe"
    resolver = SimpleNamespace(component_path=mock.Mock(return_value=package_exe))
    with (mock.patch.dict(sys.modules, {"component_runtime": resolver}),
          mock.patch("captcha_bridge.IS_FROZEN", True),
          mock.patch("captcha_bridge.INSTALL_ROOT", tmp_path / "App")):
        assert bridge._command() == [str(package_exe), "--data-dir", str(bridge.data_dir)]
        assert bridge._solver_package_root() == package_exe.parent
    resolver.component_path.assert_called_with("captcha-solver", "Captcha/KidTECaptcha.exe")


def test_missing_solver_package_is_a_clear_unavailable_state(tmp_path):
    bridge = CaptchaBridge(tmp_path / "captcha-data")
    resolver = SimpleNamespace(component_path=mock.Mock(return_value=None))
    with (mock.patch.dict(sys.modules, {"component_runtime": resolver}),
          mock.patch("captcha_bridge.IS_FROZEN", True),
          mock.patch("captcha_bridge.INSTALL_ROOT", tmp_path / "App"),
          mock.patch.object(bridge, "_processes", return_value=([], []))):
        state = bridge.status()
        result = bridge.start()
    assert state["available"] is False
    assert state["health"]["state"] == "not_installed"
    assert result["ok"] is False
    assert result["error"] == "captcha_solver_missing"


def test_invalid_selected_solver_does_not_fall_back_to_legacy_path(tmp_path):
    bridge = CaptchaBridge(tmp_path / "captcha-data")
    resolver = SimpleNamespace(component_path=mock.Mock(
        side_effect=RuntimeError("component resource digest mismatch")))
    with (mock.patch.dict(sys.modules, {"component_runtime": resolver}),
          mock.patch("captcha_bridge.IS_FROZEN", True),
          mock.patch("captcha_bridge.INSTALL_ROOT", tmp_path / "App"),
          pytest.raises(RuntimeError, match="digest mismatch")):
        bridge._command()


def test_invalid_selected_solver_is_reported_without_breaking_status(tmp_path):
    bridge = CaptchaBridge(tmp_path / "captcha-data")
    resolver = SimpleNamespace(component_path=mock.Mock(
        side_effect=RuntimeError("component resource digest mismatch")))
    with (mock.patch.dict(sys.modules, {"component_runtime": resolver}),
          mock.patch("captcha_bridge.IS_FROZEN", True),
          mock.patch("captcha_bridge.INSTALL_ROOT", tmp_path / "App"),
          mock.patch.object(bridge, "_processes", return_value=([], []))):
        state = bridge.status()
    assert state["available"] is False
    assert state["package_error"] == "captcha_solver_package_invalid"
    assert state["health"]["state"] == "not_installed"


def test_monitor_uses_selected_package_and_resolved_ffmpeg(tmp_path):
    bridge = CaptchaBridge(tmp_path / "captcha-data")
    exe = tmp_path / "components" / "captcha-solver" / "1.0.0" / "Captcha" / "KidTECaptcha.exe"
    ffmpeg = tmp_path / "components" / "media-tools" / "1.0.0" / "tools" / "ffmpeg.exe"
    script = exe.parent / "_internal" / "captcha-monitor" / "monitor.ps1"
    script.parent.mkdir(parents=True)
    script.write_text("# monitor", encoding="utf-8")
    ffmpeg.parent.mkdir(parents=True)
    ffmpeg.write_text("ffmpeg", encoding="utf-8")
    with (mock.patch("captcha_bridge.IS_FROZEN", True),
          mock.patch.object(bridge, "_selected_solver_executable", return_value=exe),
          mock.patch.object(bridge, "_ffmpeg_path", return_value=ffmpeg)):
        command = bridge._monitor_command(123)
    assert command[command.index("-File") + 1] == str(script)
    assert command[command.index("-Ffmpeg") + 1] == str(ffmpeg)


def test_worker_environment_passes_absolute_ffmpeg_path(tmp_path):
    bridge = CaptchaBridge(tmp_path)
    ffmpeg = tmp_path / "tools" / "ffmpeg.exe"
    ffmpeg.parent.mkdir()
    ffmpeg.write_text("ffmpeg", encoding="utf-8")
    with mock.patch("captcha_bridge.find_tool", return_value=str(ffmpeg)):
        env = bridge._solver_environment()
    assert env["KIDTE_FFMPEG"] == str(ffmpeg.resolve())


def test_closing_app_writes_cooperative_stop_and_blocks_late_autostart(tmp_path):
    bridge = CaptchaBridge(tmp_path)
    with (mock.patch.object(bridge, "_processes", return_value=([], [])),
          mock.patch.object(bridge, "_monitor_pid", return_value=None)):
        assert bridge.stop(closing=True)["ok"]
    assert (tmp_path / "stop-requested.marker").is_file()
    assert bridge.start() == {"ok": False, "error": "captcha_app_closing"}
