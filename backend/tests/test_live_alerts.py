"""O3/O4/O5: báo động khi LIVE và trạng thái phân tích video với job sai."""
import sys
import time
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from live_alerts import LiveAlerts, classify_ai_failure  # noqa: E402


def test_classify():
    assert classify_ai_failure(["deepseek_APIStatusError"], 402) == "ai_out_of_balance"
    assert classify_ai_failure(["deepseek_AuthenticationError"]) == "ai_key_rejected"
    assert classify_ai_failure(["deepseek_APITimeoutError"]) == "ai_timeout"
    assert classify_ai_failure(["deepseek_APIConnectionError"]) == "ai_network"
    assert classify_ai_failure(["deepseek_RateLimitError"], 429) == "ai_rate_limited"
    assert classify_ai_failure(["deepseek_key_missing"]) == "ai_key_missing"


def test_streak_needs_three_and_clears_on_success():
    alerts = LiveAlerts()
    assert alerts.ai_failed(["deepseek_APITimeoutError"]) is None
    assert alerts.ai_failed(["deepseek_APITimeoutError"]) is None
    alert = alerts.ai_failed(["deepseek_APITimeoutError"])
    assert alert["code"] == "ai_timeout" and "3 lần" in alert["message"] and alert["action"]
    alerts.ai_succeeded()
    assert alerts.ai_alert() is None


def test_out_of_balance_and_key_alert_immediately():
    alerts = LiveAlerts()
    assert alerts.ai_failed(["deepseek_APIStatusError"], 402)["code"] == "ai_out_of_balance"
    alerts.ai_succeeded()
    assert alerts.ai_failed(["deepseek_AuthenticationError"], 401)["code"] == "ai_key_rejected"


def test_comment_silence_only_when_live():
    now = 10_000.0
    alerts = LiveAlerts(clock=lambda: now)
    beat = {"live_running": True, "received_at": now - 2,
            "collector": {"ok": True, "bridge": True, "last_comment_at_ms": int((now - 400) * 1000)}}
    alert = alerts.comment_alert(beat, 300, now - 1000)
    assert alert["code"] == "comments_silent" and "6 phút" in alert["message"]
    assert alerts.comment_alert({**beat, "live_running": False}, 300, now - 1000) is None
    fresh = {**beat, "collector": {"ok": True, "bridge": True, "last_comment_at_ms": int((now - 30) * 1000)}}
    assert alerts.comment_alert(fresh, 300, now - 1000) is None
    # LIVE vừa bắt đầu 2 phút, chưa có bình luận nào: chưa báo.
    empty = {**beat, "collector": {"ok": True, "bridge": True, "last_comment_at_ms": 0}}
    assert alerts.comment_alert(empty, 300, now - 120) is None
    assert alerts.comment_alert(empty, 300, now - 400)["code"] == "comments_silent"
    # Cấu hình ngắn hơn (2 phút).
    assert alerts.comment_alert(beat, 120, now - 1000)["code"] == "comments_silent"


def test_collector_error_alert_and_old_extension_quiet():
    now = time.time()
    alerts = LiveAlerts()
    broken = {"live_running": True, "received_at": now,
              "collector": {"ok": True, "bridge": False, "last_comment_at_ms": int(now * 1000)}}
    assert alerts.comment_alert(broken, 300, now - 10)["code"] == "comments_collector_error"
    assert alerts.comment_alert({"live_running": True, "received_at": now}, 300, now - 9999) is None


def _engine():
    import server as server_module
    eng = object.__new__(server_module.ReplyEngine)
    eng.platform_heartbeats = {}
    return eng


def test_engine_live_alerts_and_heartbeat_collector():
    eng = _engine()
    for _ in range(3):
        eng._note_ai_failure(["deepseek_APIConnectionError"])
    codes = [a["code"] for a in eng.live_alerts()]
    assert codes == ["ai_network"]
    eng._live_alerts().ai_succeeded()
    assert eng.live_alerts() == []
    now = time.time()
    eng.platform_heartbeats["tiktok"] = {"live_running": True, "received_at": now, "live_since": now - 900,
                                         "collector": {"ok": True, "bridge": True,
                                                       "last_comment_at_ms": int((now - 600) * 1000)}}
    assert [a["code"] for a in eng.live_alerts()] == ["comments_silent"]


def test_readiness_marks_ai_step_error():
    from live_readiness import build_readiness
    dash = {"live_alerts": [{"code": "ai_out_of_balance", "message": "Hết tiền", "action": "Nạp"}]}
    out = build_readiness(dash)
    c4 = next(s for s in out["steps"] if s["id"] == "C4")
    assert c4["state"] == "error" and out["alerts"]


def test_ai_alert_names_platforms():
    alerts = LiveAlerts()
    alerts.ai_failed(["deepseek_APIStatusError"], 402, "shopee")
    alert = alerts.ai_alert()
    assert alert["code"] == "ai_out_of_balance" and alert["message"].startswith("Shopee: ")
    assert alert["platforms"] == ["shopee"]
    alerts.ai_failed(["deepseek_APIStatusError"], 402, "tiktok")
    assert alerts.ai_alert()["message"].startswith("Shopee + TikTok: ")
    alerts.ai_succeeded()
    assert alerts.ai_alert() is None


class _FakeResponder:
    def __init__(self, results):
        self.results = list(results)

    def reply(self, *args, **kwargs):
        item = self.results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _shopee_ai(eng, results):
    import threading
    from shopee_ai import ShopeeAiEngine as ShopeeAI
    eng.responder = _FakeResponder(results)
    ai = object.__new__(ShopeeAI)
    ai.owner, ai.lock, ai.seen = eng, threading.Lock(), {}
    return ai


def test_shopee_model_failures_raise_live_alert_and_clear_on_success():
    eng = _engine()
    skip = {"action": "skip", "missing_fields": ["deepseek_APITimeoutError"]}
    ai = _shopee_ai(eng, [skip, skip, {"action": "skip", "missing_fields": ["missing_price"]}, skip,
                          {"action": "reply", "reply": "Dạ có ạ"}])
    ai._call_model("k")
    ai._call_model("k")
    assert eng.live_alerts() == []                       # 2 lỗi chưa báo
    ai._call_model("k")                                  # bỏ qua có chủ đích: không đếm
    ai._call_model("k")
    alerts = eng.live_alerts()
    assert [a["code"] for a in alerts] == ["ai_timeout"] and alerts[0]["message"].startswith("Shopee: ")
    ai._call_model("k")
    assert eng.live_alerts() == []


def test_shopee_402_alerts_immediately_and_exception_counts():
    eng = _engine()
    ai = _shopee_ai(eng, [{"action": "skip", "missing_fields": ["deepseek_APIStatusError"],
                           "metrics": {"http_status": 402}}])
    ai._call_model("k")
    assert [a["code"] for a in eng.live_alerts()] == ["ai_out_of_balance"]
    eng2 = _engine()
    ai2 = _shopee_ai(eng2, [RuntimeError("x")] * 3)
    for _ in range(3):
        try:
            ai2._call_model("k")
        except RuntimeError:
            pass
    assert [a["code"] for a in eng2.live_alerts()] == ["ai_failing"]


def test_shopee_comment_silence_alert():
    eng = _engine()
    now = time.time()
    eng.platform_heartbeats["shopee"] = {"live_running": True, "ai_enabled": True, "received_at": now,
                                         "live_since": now - 900}
    alerts = eng.live_alerts()
    assert [a["code"] for a in alerts] == ["comments_silent"]
    assert "Shopee" in alerts[0]["message"] and alerts[0]["platform"] == "shopee"
    eng._note_comment_seen("shopee")
    assert eng.live_alerts() == []
    eng._comment_seen_at["shopee"] = now - 400
    assert [a["code"] for a in eng.live_alerts()] == ["comments_silent"]
    eng.platform_heartbeats["shopee"]["ai_enabled"] = False      # AI tắt: không đủ căn cứ
    assert eng.live_alerts() == []
    eng.platform_heartbeats["shopee"].update(ai_enabled=True, live_running=False)
    assert eng.live_alerts() == []
