"""Nhật ký chẩn đoán captcha trình duyệt: lọc dữ liệu nhạy cảm, ghi JSONL, giới hạn dung lượng, route."""
import json

import pytest

import browser_diag_log as diag
import server
from test_security_pairing_ssrf import GOOD, OTHER, app  # noqa: F401  (fixture HTTP)


def lines(path):
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def test_append_writes_whitelisted_fields_only(tmp_path):
    entry = {"t": 1760000000000, "kind": "api", "tab": 7, "op": "pin", "user": False, "calls": 1,
             "ms": 230, "ok": False, "code": 403, "visible": True, "blocked": "",
             "msg": "verify msToken=abcDEF123 cookie: sessionid=zzz " + "A" * 300,
             "cookie": "SECRET-COOKIE", "headers": {"Authorization": "Bearer X"},
             "content": "xin chao khach", "customer": "user123",
             "url": "https://shop.tiktok.com/streamer/live?msToken=SECRET#frag"}
    result = diag.append(tmp_path, {"entries": [entry, {"kind": "api"}, "bad", {"t": 1}]})
    assert result == {"ok": True, "written": 1, "dropped": 3}
    row = lines(tmp_path / "tiktok-browser.jsonl")[0]
    text = json.dumps(row)
    for secret in ("SECRET", "abcDEF123", "sessionid=zzz", "Bearer", "xin chao", "user123"):
        assert secret not in text
    assert not {"cookie", "headers", "content", "customer"} & set(row)
    assert len(row["msg"]) <= 120
    assert row["url"] == "https://shop.tiktok.com/streamer/live"
    assert row["code"] == "403" and row["tab"] == 7 and row["ok"] is False


def test_captcha_summary_is_kept_and_cleaned(tmp_path):
    summary = {"window_s": 300, "calls": 9, "writes": 4, "tabs": 2,
               "by_method": {"ListLiveProduct": 5, "PinLiveProduct": "x", "Chat": 2}, "extra": "no"}
    diag.append(tmp_path, {"entries": [{"t": 5, "kind": "captcha", "phase": "show", "summary": summary}]})
    row = lines(tmp_path / "tiktok-browser.jsonl")[0]
    assert row["summary"] == {"window_s": 300, "calls": 9, "writes": 4, "tabs": 2,
                              "by_method": {"ListLiveProduct": 5, "Chat": 2}}


def test_rotation_limits_disk(tmp_path):
    entry = {"t": 1, "kind": "event", "name": "x", "reason": "r" * 60}
    for _ in range(40):
        diag.append(tmp_path, {"entries": [entry] * 50}, max_bytes=20_000, backups=2)
    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == ["tiktok-browser.jsonl", "tiktok-browser.jsonl.1", "tiktok-browser.jsonl.2"]
    assert all(p.stat().st_size <= 20_000 + 8_000 for p in tmp_path.iterdir())


def test_batch_size_and_invalid_payload(tmp_path):
    entries = [{"t": i, "kind": "event", "name": "n"} for i in range(500)]
    result = diag.append(tmp_path, {"entries": entries})
    assert result["written"] == diag.MAX_ENTRIES_PER_REQUEST and result["dropped"] == 300
    for bad in ({}, {"entries": "x"}, []):
        with pytest.raises(ValueError):
            diag.append(tmp_path, bad)


def test_http_route(app, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setattr(server, "LOG_DIR", tmp_path)
    body = {"entries": [{"t": 9, "kind": "event", "name": "ai_toggle", "on": True}]}
    status, _, data = app.call("POST", "/api/diag/tiktok-browser", body, GOOD)
    assert status == 200 and json.loads(data)["written"] == 1
    assert lines(tmp_path / "tiktok-browser.jsonl")[0]["name"] == "ai_toggle"
    # Không phải tiện ích -> 403; tiện ích khác chưa ghép -> 403; thân sai -> 400.
    assert app.call("POST", "/api/diag/tiktok-browser", body)[0] == 403
    app.call("POST", "/api/heartbeat", {"platform": "tiktok"}, GOOD)
    assert app.call("POST", "/api/diag/tiktok-browser", body, OTHER)[0] == 403
    assert app.call("POST", "/api/diag/tiktok-browser", {"entries": 3}, GOOD)[0] == 400
