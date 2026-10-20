"""E2E bật/tắt nhanh TikTok: route /api/live/control -> heartbeat -> comments.js áp -> app thấy đã áp.
Không gọi cổng 8793: backend chạy trong bộ nhớ, tiện ích chạy bằng Node với chrome giả."""
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import routes_live
import server

EXT = Path(__file__).resolve().parents[2] / "extension"
NODE = shutil.which("node")


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "ROOT", tmp_path)
    eng = server.ReplyEngine.__new__(server.ReplyEngine)
    eng.privacy = SimpleNamespace(pending_purges=lambda platform: [])
    eng.platform_heartbeats = {}
    eng.extension_heartbeat = None
    eng._ai_disabled_by_restore = lambda: False
    return eng


def _run_extension(response, pin="running"):
    out = subprocess.run([NODE, str(EXT / "test_control_e2e_driver.cjs"), json.dumps(response), pin],
                         cwd=str(EXT), capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.skipif(NODE is None, reason="cần node")
def test_toggle_ai_then_pin_from_app_applies_in_extension(engine):
    route = SimpleNamespace(engine=engine)
    routes_live.post_live_control(route, "/api/live/control", {"platform": "tiktok", "auto_pin": False}, {})
    first = engine.heartbeat({"platform": "tiktok"})
    assert first["control"]["auto_pin"] is False and first["control"]["ai_enabled"] is None
    ext = _run_extension(first, "running")
    assert ext["pinCalls"] == ["LIVE_RE_PAUSE"]
    assert ext["last"]["pin_running"] is False
    engine.heartbeat({"platform": "tiktok", **ext["last"]})
    lc = engine.live_control()["tiktok"]
    assert lc["applied"] is True and lc["control_error"] == ""

    # Bật AI sau đó KHÔNG được mang theo auto_pin=False cũ (ép tạm dừng ghim lần nữa).
    routes_live.post_live_control(route, "/api/live/control", {"platform": "tiktok", "ai_enabled": True}, {})
    second = engine.heartbeat({"platform": "tiktok"})
    assert second["control"]["ai_enabled"] is True and second["control"]["auto_pin"] is None
    ext = _run_extension(second, "running")
    assert ext["pinCalls"] == []
    assert ext["storage"]["liveReAiEnabled"] is True
    assert ext["last"]["ai_enabled"] is True and ext["last"]["control_applied_seq"] == second["control"]["seq"]
    engine.heartbeat({"platform": "tiktok", **ext["last"]})
    assert engine.live_control()["tiktok"]["applied"] is True
    assert engine.platform_heartbeats["tiktok"]["ai_enabled"] is True


def test_unacked_commands_merge_fields(engine):
    engine.set_live_control({"platform": "tiktok", "ai_enabled": True})
    engine.set_live_control({"platform": "tiktok", "auto_pin": True})
    control = engine.heartbeat({"platform": "tiktok"})["control"]
    assert control["ai_enabled"] is True and control["auto_pin"] is True
    engine.heartbeat({"platform": "tiktok", "control_applied_seq": control["seq"]})
    engine.set_live_control({"platform": "tiktok", "auto_pin": False})
    control = engine.heartbeat({"platform": "tiktok"})["control"]
    assert control["ai_enabled"] is None and control["auto_pin"] is False
    assert "fields" not in engine.live_control()["tiktok"]
