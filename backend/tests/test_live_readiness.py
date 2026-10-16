from live_readiness import build_readiness


def _by_id(out):
    return {s["id"]: s for s in out["steps"]}


def _ready_dash():
    return {
        "extension": {"connected": True, "live_running": True, "ai_enabled": True, "ai_ready": True},
        "ai": {"deepseek_available": True},
        "catalog": {"total": 5, "unmapped": 0, "approved_profiles": 3},
        "captcha": {"configured": True, "health": {"state": "ready"}},
        "video_review": {"pending_jobs": 0, "first_job_id": ""},
        "plugins": {"plugins": [{"id": "dong-bo-video", "runtime": {"state": "running"}}]},
        "backup": {"latest": "x", "overdue": False},
        "live_channels": {
            "tiktok": {"settings": {"enabled": True, "mode": "process", "process_name": "TikTok LIVE Studio.exe"},
                       "position": {"available": True}, "health": {}},
            "shopee": {"settings": {"enabled": True, "input_name": "Media"}, "extension": {"connected": True}, "catalog": {"products_count": 3},
                       "position": {"available": False}, "health": {"reason": "obs_not_streaming"}},
        },
    }


EXTRA = {"video_library": {"count": 2, "ready": 1}, "whisper_ready": True}


def test_empty_dash_everything_todo_or_blocked():
    out = build_readiness({}, {})
    steps = _by_id(out)
    assert [s["id"] for s in out["steps"]][:3] == ["A1", "A2", "A3"]
    assert steps["A1"]["state"] == "todo"
    assert steps["A2"]["state"] == "na"          # không có kênh Shopee
    assert steps["A5"]["state"] == "blocked"
    assert steps["A6"]["state"] == "blocked"
    assert steps["A7"]["state"] == "optional"     # whisper chưa biết
    assert steps["B4"]["state"] == "error"        # thiếu plugin
    assert out["summary"]["done"] == 0
    assert out["summary"]["blocking"] == sum(
        1 for s in out["steps"] if s["required"] and s["group"] != "live" and s["state"] in ("todo", "blocked", "error"))


def test_all_ready():
    out = build_readiness(_ready_dash(), EXTRA)
    steps = _by_id(out)
    for sid in ("A1", "A2", "A3", "A4", "A5", "A5S", "A6", "A7", "A8", "B1", "B2", "B3", "B3S", "B4", "B5", "C1", "C2", "C3", "C4"):
        assert steps[sid]["state"] == "done", sid
    assert out["summary"]["blocking"] == 0
    assert steps["B4"]["action"] == {"kind": "toggle", "target": "sync", "on": False, "label": "Tắt"}


def test_shopee_disabled_is_na_and_not_blocking():
    dash = _ready_dash()
    dash["live_channels"]["shopee"]["settings"] = {"enabled": False}
    dash["live_channels"]["shopee"]["extension"] = {"connected": False}
    out = build_readiness(dash, EXTRA)
    a2 = _by_id(out)["A2"]
    assert a2["state"] == "na" and a2["required"] is False
    assert out["summary"]["blocking"] == 0
    assert out["summary"]["total"] == len(out["steps"]) - 3   # A2, A5S, B3S


def test_states_and_blocking_count():
    dash = _ready_dash()
    dash["catalog"] = {"total": 5, "unmapped": 2, "approved_profiles": 3}
    dash["captcha"] = {"key_rejected": True}
    dash["video_review"] = {"pending_jobs": 1, "first_job_id": "a" * 32}
    dash["plugins"]["plugins"][0]["runtime"]["state"] = "stopped"
    dash["live_channels"]["tiktok"]["position"] = {"available": False}
    dash["live_channels"]["tiktok"]["health"] = {"reason": "audio_silent"}
    out = build_readiness(dash, {"video_library": {"count": 1, "ready": 0}, "whisper_ready": False})
    s = _by_id(out)
    assert s["A6"]["state"] == "todo" and s["A6"]["action"]["endpoint"] == "/api/mapping/auto-match"
    assert s["A7"]["state"] == "todo"
    assert s["A8"]["state"] == "error" and s["A8"]["required"] is False
    assert s["B2"]["state"] == "todo" and s["B2"]["action"]["arg"] == "a" * 32
    assert s["B4"]["state"] == "todo" and s["B4"]["action"]["on"] is True
    assert s["C2"]["state"] == "todo" and s["C2"]["detail"] == "audio_silent"
    assert s["C2"]["action"]["endpoint"] == "/api/live/reidentify"
    assert out["summary"]["blocking"] == 4   # A6, A7, B2, B4 (A8 không bắt buộc)


def test_disconnected_blocks_cart_and_ai():
    dash = _ready_dash()
    dash["extension"] = {"connected": False}
    dash["live_channels"]["shopee"]["extension"] = {"connected": False}
    dash["catalog"] = {"total": 0, "unmapped": 0, "approved_profiles": 0}
    s = _by_id(build_readiness(dash, EXTRA))
    assert s["A5"]["state"] == "blocked"
    assert s["C4"]["state"] == "blocked"
    assert s["C1"]["state"] == "todo"
    assert all(st["state"] in ("done", "todo", "blocked", "error", "optional", "na") for st in s.values())


def test_pin_running_from_heartbeat_and_shopee_ai():
    dash = _ready_dash()
    dash["extension"] = {**dash["extension"], "live_running": True, "pin_running": False,
                         "ai_enabled": False, "ai_ready": False}
    s = _by_id(build_readiness(dash, EXTRA))
    assert s["C3"]["state"] == "optional" and "tạm dừng" in s["C3"]["detail"]
    assert s["C4"]["state"] == "todo"
    dash["extension"]["pin_running"] = True
    dash["live_channels"]["shopee"]["extension"] = {"connected": True, "ai_enabled": True, "ai_ready": True}
    s = _by_id(build_readiness(dash, EXTRA))
    assert s["C4"]["state"] == "done" and "Shopee" in s["C4"]["detail"]


def _real_shopee_dash():
    """Dữ liệu thật 16/10: shop đang LIVE Shopee, TikTok chưa nối."""
    return {
        "extension": {},
        "ai": {"deepseek_available": True},
        "catalog": {"total": 0, "unmapped": 0, "approved_profiles": 3},
        "plugins": {"plugins": [{"id": "dong-bo-video", "runtime": {"state": "running"}}]},
        "live_channels": {
            "tiktok": {"settings": {"enabled": False}, "position": {"available": False}, "health": {}},
            "shopee": {
                "settings": {"enabled": True, "mode": "obs", "port": 4455, "input_name": "", "bindings": {},
                             "password_configured": True},
                "position": {"ok": True, "available": True, "video_id": "final-vjp-1", "state": "locked",
                             "source": "obs_media", "match_label": "Khop ro", "platform": "shopee"},
                "health": {"state": "waiting", "reason": "source_not_started"},
                "extension": {"live_running": True, "ai_enabled": True, "ai_ready": True, "observer": True,
                              "pin_running": None, "session_id": "42382146", "connected": True},
                "catalog": {"products_count": 24},
            },
        },
    }


def test_real_shopee_only_live():
    out = build_readiness(_real_shopee_dash(), EXTRA)
    s = _by_id(out)
    assert out["platforms"]["shopee"]["live"] and not out["platforms"]["tiktok"]["active"]
    assert s["B3S"]["state"] == "done"
    assert s["C1"]["state"] == "done" and "Shopee" in s["C1"]["detail"]
    assert s["C2"]["state"] == "done" and "Shopee" in s["C2"]["detail"]
    assert s["C3"]["state"] == "done" and "Shopee" in s["C3"]["detail"]
    assert s["C4"]["state"] == "done" and "Shopee" in s["C4"]["detail"]
    assert s["A5S"]["state"] == "done"
    for sid in ("A1", "A5", "A8", "B3"):
        assert s[sid]["state"] == "na" and s[sid]["required"] is False, sid
        assert "real" in s[sid]
    assert s["A1"]["real"]["state"] == "todo"
    assert out["summary"]["blocking"] == 0


def test_tiktok_only_hides_shopee():
    dash = _ready_dash()
    del dash["live_channels"]["shopee"]
    s = _by_id(build_readiness(dash, EXTRA))
    assert s["C1"]["detail"] == "Đang LIVE: TikTok"
    for sid in ("A2", "A5S", "B3S"):
        assert s[sid]["state"] == "na", sid


def test_both_platforms_require_both():
    dash = _ready_dash()
    dash["live_channels"]["shopee"]["settings"] = {"enabled": True, "mode": "obs"}
    dash["live_channels"]["shopee"]["extension"] = {"connected": True, "live_running": True}
    out = build_readiness(dash, EXTRA)
    s = _by_id(out)
    assert s["B3S"]["state"] == "todo" and s["B3"]["state"] == "done"
    assert s["C1"]["detail"] == "Đang LIVE: TikTok, Shopee"
    assert out["summary"]["blocking"] == 1