"""B3 ghép tiện ích lần đầu, B4/B11 chặn SSRF, B5 header bảo mật + Host."""
import http.client
import io
import json
import threading
import urllib.error
from types import SimpleNamespace

import pytest

import knowledge_package
import media_assets
import product_ingest as pi
import server
from extension_pairing import ExtensionPairing, extension_id

GOOD = "chrome-extension://" + "a" * 32
OTHER = "chrome-extension://" + "b" * 32


# ------------------------------------------------------------------ B3 đơn vị
def test_pairing_first_heartbeat_pairs_then_other_id_blocked(tmp_path):
    store = ExtensionPairing(tmp_path / "machine" / "extension_pairing.json")
    assert store.check("tiktok", OTHER) is True          # chưa ghép: request thường qua, không ghép
    assert store.status()["tiktok"]["paired"] is False
    assert store.check("tiktok", GOOD, heartbeat=True) is True
    assert store.check("tiktok", GOOD) is True
    assert store.check("tiktok", OTHER) is False
    assert store.check("tiktok", OTHER, heartbeat=True) is False   # heartbeat không ghép đè
    status = store.status()["tiktok"]
    assert status["paired"] and status["id_short"].startswith("aaaaaa")
    assert status["blocked_recent"]["count"] == 2
    saved = json.loads((tmp_path / "machine" / "extension_pairing.json").read_text(encoding="utf-8"))
    assert saved["paired"]["tiktok"]["id"] == "a" * 32
    # nạp lại từ đĩa vẫn nhớ ghép
    again = ExtensionPairing(tmp_path / "machine" / "extension_pairing.json")
    assert again.check("tiktok", OTHER) is False


def test_pairing_platforms_are_separate_and_reset(tmp_path):
    store = ExtensionPairing(tmp_path / "p.json")
    store.check("tiktok", GOOD, heartbeat=True)
    assert store.check("shopee", OTHER, heartbeat=True) is True    # Shopee ghép riêng
    assert store.check("shopee", GOOD) is False
    assert store.check("tiktok", OTHER) is False
    assert store.reset("shopee")["removed"] is True
    assert store.status()["shopee"]["paired"] is False
    assert store.check("tiktok", OTHER) is False                   # TikTok không bị ảnh hưởng
    assert store.check("shopee", GOOD, heartbeat=True) is True
    assert store.check("shopee", OTHER) is False
    with pytest.raises(ValueError):
        store.reset("facebook")


def test_extension_id_parsing():
    assert extension_id(GOOD + "/") == "a" * 32
    assert extension_id("https://evil.example") == ""
    assert extension_id("chrome-extension://../x") == ""


def test_route_platform_classification():
    platform = server.Handler.extension_platform
    for route in ("/api/reply", "/api/reply/ack", "/api/reply/delivery", "/api/reply/claim",
                  "/api/catalog/sync", "/api/observe", "/api/tiktok/queue/next", "/api/reply/queue/enqueue"):
        assert platform(route) == "tiktok", route
    for route in ("/api/shopee/reply", "/api/shopee/delivery", "/api/shopee/mappings", "/api/shopee/sync",
                  "/api/shopee/queue/next", "/api/shopee/reply/status", "/api/shopee/reply/queue/next"):
        assert platform(route) == "shopee", route
    assert platform("/api/heartbeat", {"platform": "shopee"}) == "shopee"
    assert platform("/api/heartbeat", {}) == "tiktok"
    assert platform("/api/privacy/purge-ack", {"platform": "shopee"}) == "shopee"


# ------------------------------------------------------------------ B3/B5 qua HTTP
@pytest.fixture
def app(tmp_path):
    calls = []
    engine = SimpleNamespace(
        extension_pairing=ExtensionPairing(tmp_path / "pairing.json"),
        heartbeat=lambda payload: calls.append(("heartbeat", payload)) or {"ok": True},
        sync_catalog=lambda payload: calls.append(("sync", payload)) or {"ok": True},
        sync_shopee_catalog=lambda payload: calls.append(("shopee_sync", payload)) or {"ok": True},
        health=lambda: {"ok": True},
        video_timelines=lambda: {"ok": True},
        current_video_position=lambda platform: {"platform": platform},
        shopee_mapping_state=lambda: {"ok": True},
    )
    engine.reset_extension_pairing = lambda platform: server.ReplyEngine.reset_extension_pairing(engine, platform)

    class Handler(server.Handler):
        pass
    Handler.engine = engine
    http = server.LoopbackHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=http.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
    worker.start()

    def call(method, path, payload=None, origin=None, host=None):
        conn = http_client(http.server_port)
        headers = {"Content-Type": "application/json"}
        if origin:
            headers["Origin"] = origin
        if host:
            headers["Host"] = host
        body = None if payload is None else json.dumps(payload).encode()
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, dict(response.getheaders()), data
    try:
        yield SimpleNamespace(call=call, calls=calls, engine=engine, port=http.server_port)
    finally:
        http.shutdown()
        http.server_close()
        worker.join()


def http_client(port):
    return http.client.HTTPConnection("127.0.0.1", port, timeout=5)


def test_http_pairing_blocks_other_extension_and_reset(app):
    status, _, _ = app.call("POST", "/api/heartbeat", {"platform": "tiktok"}, GOOD)
    assert status == 200
    status, _, _ = app.call("POST", "/api/catalog/sync", {"live_context": {}}, GOOD)
    assert status == 200
    status, _, body = app.call("POST", "/api/catalog/sync", {"live_context": {}}, OTHER)
    assert status == 403 and json.loads(body)["error"] == "extension_not_paired"
    assert [c[0] for c in app.calls] == ["heartbeat", "sync"]
    # Shopee ghép riêng: tiện ích khác được ghép cho Shopee
    assert app.call("POST", "/api/heartbeat", {"platform": "shopee"}, OTHER)[0] == 200
    assert app.call("POST", "/api/shopee/sync", {"x": 1}, OTHER)[0] == 200
    assert app.call("POST", "/api/shopee/sync", {"x": 1}, GOOD)[0] == 403
    assert app.call("GET", "/api/shopee/mappings", origin=GOOD)[0] == 403
    assert app.call("GET", "/api/shopee/mappings", origin=OTHER)[0] == 200
    # reset chỉ từ trang app cục bộ
    assert app.call("POST", "/api/extension/pairing/reset", {"platform": "tiktok"}, GOOD)[0] == 403
    status, _, body = app.call("POST", "/api/extension/pairing/reset", {"platform": "tiktok"})
    assert status == 200 and json.loads(body)["removed"] is True
    assert app.call("POST", "/api/heartbeat", {"platform": "tiktok"}, OTHER)[0] == 200
    assert app.call("POST", "/api/catalog/sync", {"live_context": {}}, GOOD)[0] == 403


def test_http_read_routes_require_loopback_host(app):
    for path in ("/health", "/api/video/position?platform=tiktok", "/api/video/timelines"):
        assert app.call("GET", path)[0] == 200                       # plugin/OBS: không Origin
        assert app.call("GET", path, origin=GOOD)[0] == 200          # tiện ích
        assert app.call("GET", path, host="evil.example:%d" % app.port)[0] == 403   # DNS rebinding
        assert app.call("GET", path, origin="https://evil.example")[0] == 403


def test_security_headers_on_app_and_assets(app, monkeypatch):
    monkeypatch.setattr(server, "versioned_operator_page", lambda text, version: "<html></html>")
    status, headers, _ = app.call("GET", "/app")
    assert status == 200
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Content-Security-Policy"] == "frame-ancestors 'none'"
    assert headers["X-Content-Type-Options"] == "nosniff"
    status, headers, _ = app.call("GET", "/health")
    assert headers["X-Content-Type-Options"] == "nosniff"


# ------------------------------------------------------------------ B4/B11 SSRF
@pytest.mark.parametrize("address", ["127.0.0.1", "10.1.2.3", "192.168.1.1", "169.254.169.254",
                                     "100.64.0.1", "100.127.255.254", "64:ff9b::a00:1", "::ffff:127.0.0.1",
                                     "::1", "fd00::1", "0.0.0.0"])
def test_check_public_url_blocks_internal(monkeypatch, address):
    monkeypatch.setattr(pi, "_resolve", lambda host: [address])
    with pytest.raises(pi.IngestError):
        pi.check_public_http_url("http://images.example/a.jpg")


def test_check_public_url_allows_public(monkeypatch):
    monkeypatch.setattr(pi, "_resolve", lambda host: ["93.184.216.34"])
    assert pi.check_public_http_url("https://images.example/a.jpg")


def test_media_download_refuses_internal_host_without_connecting(monkeypatch):
    monkeypatch.setattr(pi, "_resolve", lambda host: ["192.168.1.1"])
    opened = []
    monkeypatch.setattr(pi.urllib.request, "build_opener", lambda *a: opened.append(a))
    with pytest.raises(ValueError):
        media_assets._download("http://router.example/snapshot.jpg")
    assert opened == []


def test_knowledge_source_refuses_internal_host(monkeypatch):
    monkeypatch.setattr(pi, "_resolve", lambda host: ["100.64.1.1"])
    with pytest.raises(ValueError):
        knowledge_package.read_source("https://pkg.example/knowledge.json")


def test_redirect_to_internal_is_refused(monkeypatch):
    monkeypatch.setattr(pi, "_resolve", lambda host: ["127.0.0.1"] if host == "internal.example" else ["93.184.216.34"])
    handler = pi._SafeRedirect()
    request = pi.urllib.request.Request("https://public.example/a.jpg")
    with pytest.raises(pi.IngestError):
        handler.redirect_request(request, io.BytesIO(), 302, "Found", {}, "http://internal.example/x")


def test_read_public_url_limits_size(monkeypatch):
    monkeypatch.setattr(pi, "_resolve", lambda host: ["93.184.216.34"])

    class Response(io.BytesIO):
        status = 200

    class Opener:
        def open(self, request, timeout=None):
            return Response(b"x" * 2048)
    monkeypatch.setattr(pi.urllib.request, "build_opener", lambda *a: Opener())
    assert pi.read_public_url("https://public.example/a", 4096) == b"x" * 2048
    with pytest.raises(pi.IngestError, match="vượt giới hạn"):
        pi.read_public_url("https://public.example/a", 1000)
