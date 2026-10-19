"""Công tắc AI/tự ghim từ app (Trung tâm LIVE) -> heartbeat tiện ích, áp một lần theo seq."""
import json
import threading
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

import server


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.delenv("KIDTE_FACT_REVISION_CHECK", raising=False)
    eng = server.ReplyEngine.__new__(server.ReplyEngine)
    eng.privacy = SimpleNamespace(pending_purges=lambda platform: [])
    eng.platform_heartbeats = {}
    eng.extension_heartbeat = None
    eng._ai_disabled_by_restore = lambda: False
    return eng


def _beat(eng, platform, **extra):
    return eng.heartbeat({"platform": platform, "live_running": True, **extra})


def test_default_state_is_unset(engine):
    state = engine.live_control()
    assert state["tiktok"] == {"ai_enabled": None, "auto_pin": None, "applied": False, "control_error": "", "updated_at": None}
    assert state["shopee"] == {"ai_enabled": None, "applied": False, "control_error": "", "updated_at": None}
    assert state["queue_mode"] is False
    assert "control" not in _beat(engine, "tiktok")


def test_set_persists_and_seq_increments(engine, tmp_path):
    result = engine.set_live_control({"platform": "tiktok", "ai_enabled": True})
    assert result["ok"] and result["live_control"]["tiktok"]["ai_enabled"] is True
    engine.set_live_control({"platform": "tiktok", "auto_pin": False})
    saved = json.loads((tmp_path / "data" / "machine" / "live_control.json").read_text("utf-8"))
    assert saved["tiktok"]["seq"] == 2 and saved["tiktok"]["ai_enabled"] is True
    assert saved["tiktok"]["auto_pin"] is False
    engine._live_control_cache = None  # đọc lại từ tệp như sau khởi động lại
    assert engine.live_control()["tiktok"]["auto_pin"] is False
    with pytest.raises(ValueError):
        engine.set_live_control({"platform": "shopee", "auto_pin": True})
    with pytest.raises(ValueError):
        engine.set_live_control({"platform": "x", "ai_enabled": True})
    with pytest.raises(ValueError):
        engine.set_live_control({"platform": "tiktok", "ai_enabled": "yes"})


def test_heartbeat_delivers_control_per_platform_and_applied_on_matching_seq(engine):
    engine.set_live_control({"platform": "tiktok", "ai_enabled": True, "auto_pin": True})
    engine.set_live_control({"platform": "shopee", "ai_enabled": False})
    tiktok = _beat(engine, "tiktok")
    issued = tiktok["control"].pop("issued_at")
    assert isinstance(issued, float) and issued > 0
    assert tiktok["control"] == {"seq": 1, "ai_enabled": True, "auto_pin": True}
    shopee = _beat(engine, "shopee")["control"]
    shopee.pop("issued_at")
    assert shopee == {"seq": 1, "ai_enabled": False}
    _beat(engine, "tiktok", control_applied_seq=0)
    assert engine.live_control()["tiktok"]["applied"] is False
    _beat(engine, "tiktok", control_applied_seq=1)
    assert engine.live_control()["tiktok"]["applied"] is True
    assert engine.live_control()["shopee"]["applied"] is False
    engine.set_live_control({"platform": "tiktok", "ai_enabled": False})
    assert engine.live_control()["tiktok"]["applied"] is False
    _beat(engine, "tiktok", control_applied_seq=1)  # seq cũ không xác nhận lệnh mới
    assert engine.live_control()["tiktok"]["applied"] is False


def test_queue_mode_endpoint_writes_file(engine, tmp_path):
    assert engine.set_queue_mode({"enabled": True})["live_control"]["queue_mode"] is True
    assert json.loads((tmp_path / "data" / "machine" / "queue_mode.json").read_text("utf-8")) == {"enabled": True}
    assert engine.set_queue_mode({"enabled": False})["live_control"]["queue_mode"] is False
    with pytest.raises(ValueError):
        engine.set_queue_mode({"enabled": 1})


class _Stub:
    def __init__(self):
        self.calls = []

    def set_live_control(self, payload):
        self.calls.append(("control", payload))
        return {"ok": True, "live_control": {}}

    def set_queue_mode(self, payload):
        self.calls.append(("queue", payload))
        return {"ok": True, "live_control": {}}


@pytest.fixture
def http():
    stub = _Stub()
    server.Handler.engine = stub
    httpd = server.LoopbackHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, args=(0.1,), daemon=True).start()

    def post(path, body, origin=""):
        request = urllib.request.Request(
            f"http://127.0.0.1:{httpd.server_address[1]}{path}", data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Origin": origin})
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))
    yield post, stub
    httpd.shutdown()
    httpd.server_close()


@pytest.mark.parametrize("path", ["/api/live/control", "/api/live/queue-mode"])
def test_endpoints_local_ui_only(http, path):
    post, stub = http
    body = {"platform": "tiktok", "ai_enabled": True, "enabled": True}
    assert post(path, body)["ok"] is True
    for origin in ("chrome-extension://abc", "https://evil.example"):
        with pytest.raises(urllib.error.HTTPError) as caught:
            post(path, body, origin=origin)
        assert caught.value.code == 403
    assert len(stub.calls) == 1


def test_control_error_reported_with_applied(engine):
    engine.set_live_control({"platform": "shopee", "ai_enabled": True})
    _beat(engine, "shopee", control_applied_seq=1, control_error="DeepSeek chưa sẵn sàng")
    row = engine.live_control()["shopee"]
    assert row["applied"] is True and row["control_error"] == "DeepSeek chưa sẵn sàng"
    engine.set_live_control({"platform": "shopee", "ai_enabled": True})
    assert engine.live_control()["shopee"]["control_error"] == ""


def test_heartbeat_keeps_pin_running(engine):
    _beat(engine, "tiktok", pin_running=True)
    assert engine.platform_heartbeats["tiktok"]["pin_running"] is True
    _beat(engine, "tiktok")
    assert engine.platform_heartbeats["tiktok"]["pin_running"] is None
