import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import captcha_health
from captcha_bridge import CaptchaBridge

FIX = Path(__file__).parent / "fixtures" / "captcha"
NOW = datetime(2026, 10, 5, 10, 1, 10)
LOG = "tiktok-captcha-solver-logs"


def health(tmp_path, log=None, now=NOW, *, key=True, running=True, available=True, rejected=False):
    bridge = CaptchaBridge(tmp_path)
    if log:
        target = tmp_path / LOG / "ttls-captcha-solver.log"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((FIX / log).read_bytes())
    with mock.patch("captcha_bridge.has_key", return_value=key), \
            mock.patch.object(bridge, "_key_rejected", return_value=rejected):
        return bridge.health(running=running, available=available, now=now)


@pytest.mark.parametrize("log,now,kw,state", [
    (None, NOW, {"available": False}, "not_installed"),
    (None, NOW, {"key": False}, "no_key"),
    ("ready.log", NOW, {"rejected": True}, "key_rejected"),
    ("ready.log", NOW, {"running": False}, "not_running"),
    ("ready.log", datetime(2026, 10, 5, 10, 5, 0), {}, "unresponsive"),
    ("manual.log", NOW, {}, "manual_required"),
    ("solving.log", datetime(2026, 10, 5, 10, 0, 50), {}, "solving"),
    ("service_error.log", datetime(2026, 10, 5, 10, 0, 50), {}, "service_error"),
    ("dragged.log", NOW, {}, "ready"),
    ("ready.log", NOW, {}, "ready"),
])
def test_states(tmp_path, log, now, kw, state):
    result = health(tmp_path, log, now, **kw)
    assert result["state"] == state
    assert result["label"].startswith(captcha_health.LABELS[state])


def test_start_failed_priority_and_label(tmp_path):
    (tmp_path / "last-start.json").write_text(json.dumps(
        {"at": "x", "error": "captcha_admin_declined", "message": "UAC bị từ chối"}), encoding="utf-8")
    result = health(tmp_path, "ready.log", running=False)
    assert result["state"] == "start_failed"
    assert result["label"] == "Không khởi động được: UAC bị từ chối"
    assert health(tmp_path, "ready.log", running=True)["state"] == "ready"


def test_key_rejected_beats_start_failed(tmp_path):
    (tmp_path / "last-start.json").write_text(json.dumps({"message": "x"}), encoding="utf-8")
    assert health(tmp_path, running=False, rejected=True)["state"] == "key_rejected"


def test_service_error_details(tmp_path):
    result = health(tmp_path, "service_error.log", datetime(2026, 10, 5, 10, 0, 50))
    assert result["label"].startswith("Lỗi dịch vụ giải: SadCaptcha shapes HTTP 400")
    assert result["last_api_error_at"] == "2026-10-05 10:00:11"
    assert result["attempts_today"] == 1


def test_drag_text_never_claims_acceptance(tmp_path):
    result = health(tmp_path, "dragged.log")
    assert result["last_result_text"] == "đã kéo, CAPTCHA biến mất lúc 10:00 (TikTok chưa xác nhận)"
    assert result["last_api_ok_at"] == "2026-10-05 10:00:11"
    assert result["last_scan_resolution"] == "2560x1440"


def test_garbled_log_is_tolerated(tmp_path):
    result = health(tmp_path, "garbled.log")
    assert result["last_heartbeat_at"] == "2026-10-05 10:01:00"
    assert result["last_scan_resolution"] == "1920x1080"


def test_missing_empty_and_rotated_log(tmp_path):
    assert health(tmp_path)["last_heartbeat_at"] is None
    log = tmp_path / LOG / "ttls-captcha-solver.log"
    log.parent.mkdir()
    log.write_bytes(b"")
    assert health(tmp_path)["state"] == "ready"
    with mock.patch.object(Path, "open", side_effect=FileNotFoundError):
        assert CaptchaBridge._tail_text(log, 100) == ""


def test_large_log_only_tail_is_read(tmp_path):
    log = tmp_path / LOG / "ttls-captcha-solver.log"
    log.parent.mkdir()
    line = (FIX / "ready.log").read_text(encoding="utf-8").splitlines()[0] + "\n"
    log.write_text(line * 5000 + (FIX / "manual.log").read_text(encoding="utf-8"), encoding="utf-8")
    assert health(tmp_path)["manual_required_at"] == "2026-10-05 10:00:55"


def test_telegram_ok_timestamp(tmp_path):
    (tmp_path / "telegram-monitor.log").write_text(
        "2026-10-05T11:16:23.26+07:00 Recording failed\n"
        "2026-10-05T11:16:24.58+07:00 Telegram delivery OK\n", encoding="utf-8")
    assert health(tmp_path)["last_telegram_ok_at"] == "2026-10-05 11:16:24"


def test_start_failure_recorded_and_cleared(tmp_path):
    bridge = CaptchaBridge(tmp_path)
    with mock.patch.object(bridge, "_start", return_value={"ok": False, "error": "captcha_admin_declined"}):
        assert bridge.start()["ok"] is False
    saved = json.loads((tmp_path / "last-start.json").read_text(encoding="utf-8"))
    assert saved["error"] == "captcha_admin_declined" and "UAC" in saved["message"] and saved["at"]
    with mock.patch.object(bridge, "_start", return_value={"ok": True, "starting": True}):
        bridge.start()
    assert not (tmp_path / "last-start.json").exists()
    with mock.patch.object(bridge, "_start", side_effect=OSError("denied")):
        assert bridge.start()["error"] == "captcha_start_failed"
    assert (tmp_path / "last-start.json").exists()


def test_status_has_additive_health(tmp_path):
    with mock.patch.object(CaptchaBridge, "_processes", return_value=([], [])):
        state = CaptchaBridge(tmp_path).status()
    assert state["health"]["state"] in captcha_health.LABELS and "running" in state


def test_selftest_parses_output(tmp_path):
    report = {"ok": False, "checks": [{"name": "ffmpeg", "ok": False, "detail": "", "hint": "h"}]}
    done = subprocess.CompletedProcess([], 1, stdout="noise\n" + json.dumps(report) + "\n", stderr="")
    with mock.patch("captcha_bridge.subprocess.run", return_value=done) as run:
        result = CaptchaBridge(tmp_path).selftest()
    assert result == {"ok": False, "checks": report["checks"]}
    assert "--selftest" in run.call_args.args[0]


def test_selftest_failure_modes(tmp_path):
    bridge = CaptchaBridge(tmp_path)
    with mock.patch("captcha_bridge.subprocess.run", side_effect=subprocess.TimeoutExpired("x", 1)):
        assert bridge.selftest()["error"] == "captcha_selftest_timeout"
    with mock.patch("captcha_bridge.subprocess.run", side_effect=FileNotFoundError):
        assert bridge.selftest()["error"] == "captcha_selftest_unavailable"
    bad = subprocess.CompletedProcess([], 1, stdout="Traceback...", stderr="")
    with mock.patch("captcha_bridge.subprocess.run", return_value=bad):
        assert bridge.selftest()["error"] == "captcha_selftest_failed"
