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
