"""Thêm sản phẩm bằng AI: định tuyến nhà cung cấp, kiểm tra JSON, không-giá, giới hạn URL, hồ sơ nháp.

Không gọi mạng: mọi nhà cung cấp là đồ giả, URL/DNS được mock.
"""
import base64
import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest
from PIL import Image

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import ai_providers  # noqa: E402
import product_ingest as pi  # noqa: E402
from ai_providers import AIProviderError, DeepSeekTextProvider, GeminiProvider, choose_provider  # noqa: E402
from test_profile_approval import _engine, _titles  # noqa: E402


# ---------------------------------------------------------------- đồ giả
class FakeProvider:
    def __init__(self, name, replies, supports_files=True, configured=True):
        self.name, self.supports_files, self.configured = name, supports_files, configured
        self.model = name + "-model"
        self.replies = list(replies)
        self.calls = []

    def generate(self, system, user_text, files=()):
        self.calls.append({"system": system, "user": user_text, "files": list(files)})
        return self.replies.pop(0)


def good_json(**over):
    body = {
        "ten_san_pham": {"gia_tri": "Đạm Cá Amino 5L", "do_tin_cay": 0.95, "nguon": "n1"},
        "nha_san_xuat": {"gia_tri": "Cty ABC", "do_tin_cay": 0.9, "nguon": "n1"},
        "quy_cach_dong_goi": {"gia_tri": "can 5 lít", "do_tin_cay": 0.9, "nguon": "n1"},
        "thanh_phan_dang_ky": {"gia_tri": [{"ten": "Đạm tổng số", "ham_luong": "5%"}], "do_tin_cay": 0.8, "nguon": "n1"},
        "cong_dung": {"gia_tri": ["Kích rễ", "Xanh lá"], "do_tin_cay": 0.8, "nguon": "n1"},
        "doi_tuong_cay_trong": {"gia_tri": ["Lúa", "Rau màu"], "do_tin_cay": 0.7, "nguon": "n1"},
        "huong_dan_su_dung": {"gia_tri": ["Pha 20ml cho bình 16 lít"], "do_tin_cay": 0.9, "nguon": "n1"},
        "luu_y": {"gia_tri": None, "do_tin_cay": 0, "nguon": ""},
        "mo_ta_ngan": {"gia_tri": "Phân bón hữu cơ", "do_tin_cay": 0.4, "nguon": "n1"},
        "phan_khuc": {"gia_tri": None, "do_tin_cay": 0},
        "hieu_qua_chinh": {"gia_tri": None, "do_tin_cay": 0},
    }
    body.update(over)
    return json.dumps({"san_pham": body}, ensure_ascii=False)


def png_bytes(color=(10, 120, 30)):
    out = io.BytesIO()
    Image.new("RGB", (300, 200), color).save(out, "PNG")
    return out.getvalue()


def b64(data):
    return base64.b64encode(data).decode()


def text_payload(text="Đạm cá Amino 5L. Công dụng kích rễ. Pha 20ml cho bình 16 lít."):
    return {"text": text}


# ---------------------------------------------------------------- định tuyến
def test_routing_vision_requires_gemini_with_vietnamese_message():
    gem = FakeProvider("gemini", [], configured=False)
    deep = FakeProvider("deepseek", [], supports_files=False)
    with pytest.raises(AIProviderError) as err:
        choose_provider(True, gem, deep)
    assert err.value.code == "vision_needs_gemini" and "Gemini" in str(err.value) and "Lấy key miễn phí" in str(err.value)


def test_routing_text_prefers_gemini_then_deepseek_then_error():
    gem, deep = FakeProvider("gemini", []), FakeProvider("deepseek", [], supports_files=False)
    assert choose_provider(False, gem, deep) is gem
    assert choose_provider(True, gem, deep) is gem
    gem.configured = False
    assert choose_provider(False, gem, deep) is deep
    deep.configured = False
    with pytest.raises(AIProviderError) as err:
        choose_provider(False, gem, deep)
    assert err.value.code == "no_provider"


def test_extract_routes_sources_to_right_provider():
    gem = FakeProvider("gemini", [good_json()])
    deep = FakeProvider("deepseek", [good_json()], supports_files=False)
    ingest = pi.ProductIngest(None, gem, deep)
    out = ingest.extract({"images": [{"name": "truoc.png", "data": b64(png_bytes())}], "text": "x" * 40})
    assert out["provider"] == "gemini" and len(gem.calls[0]["files"]) == 1
    assert gem.calls[0]["files"][0]["mime"] == "image/jpeg"
    gem.configured = False
    with pytest.raises(pi.IngestError, match="Gemini"):
        ingest.extract({"images": [{"name": "a.png", "data": b64(png_bytes())}]})
    out = ingest.extract(text_payload())
    assert out["provider"] == "deepseek" and deep.calls[0]["files"] == []


def test_scanned_pdf_counts_as_vision(tmp_path):
    from pypdf import PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(200, 200)
    buf = io.BytesIO()
    writer.write(buf)
    sources = pi.prepare_sources({"files": [{"name": "scan.pdf", "data": b64(buf.getvalue())}]})
    assert sources[0]["kind"] == "pdf_scan" and pi.needs_vision(sources)
    assert pi.vision_files(sources)[0]["mime"] == "application/pdf"


def test_prompt_uses_shared_brief_and_task_block():
    from ai_context import APP_BRIEF
    system = pi.build_system()
    assert system.startswith(APP_BRIEF.rstrip()[:40]) and "[NHIỆM VỤ:" in system
    assert "KHÔNG ghi giá" in system


# ---------------------------------------------------------------- JSON
def test_validate_good_result_flags_uncertain():
    result = pi.validate_extraction(good_json(), ["n1"])
    f = result["fields"]
    assert f["ten_san_pham"] == "Đạm Cá Amino 5L"
    assert f["thanh_phan_dang_ky"] == {"Đạm tổng số": "5%"}
    assert f["cong_dung"] == ["Kích rễ", "Xanh lá"]
    assert f["luu_y"] is None and "luu_y" in result["uncertain"]
    assert "mo_ta_ngan" in result["uncertain"]                 # tin cậy 0.4 < 0.6
    assert "ten_san_pham" not in result["uncertain"]
    assert result["nguon"]["ten_san_pham"] == "n1"


def test_validate_accepts_fenced_json_and_bare_values():
    raw = "```json\n" + json.dumps({"ten_san_pham": "Kali 1kg", "cong_dung": "Nuôi trái\nTăng đường"}) + "\n```"
    result = pi.validate_extraction(raw)
    assert result["fields"]["cong_dung"] == ["Nuôi trái", "Tăng đường"]
    assert result["confidence"]["ten_san_pham"] == 0.5


@pytest.mark.parametrize("raw", ["không phải json", "[1,2]", "{}", json.dumps({"san_pham": {"gia": "5"}})])
def test_validate_rejects_bad_shapes(raw):
    with pytest.raises(pi.ExtractionInvalid):
        pi.validate_extraction(raw)


def test_one_retry_on_invalid_json_then_success_and_double_failure():
    prov = FakeProvider("gemini", ["rác", good_json()])
    out = pi.ProductIngest(None, prov, None).extract(text_payload())
    assert out["fields"]["ten_san_pham"] and len(prov.calls) == 2
    assert "KHÔNG HỢP LỆ" in prov.calls[1]["user"]
    prov = FakeProvider("gemini", ["rác", "vẫn rác", good_json()])
    with pytest.raises(pi.IngestError, match="hai lần"):
        pi.ProductIngest(None, prov, None).extract(text_payload())
    assert len(prov.calls) == 2                                  # đúng một lần thử lại


# ---------------------------------------------------------------- không giá
def test_prices_and_promos_never_survive_validation():
    raw = good_json(
        cong_dung={"gia_tri": ["Kích rễ", "Giá 150.000đ/chai", "Khuyến mãi mua 2 tặng 1"], "do_tin_cay": 0.9, "nguon": "n1"},
        luu_y={"gia_tri": "Bảo quản nơi mát. Giảm giá 20% hôm nay. Tránh ánh nắng.", "do_tin_cay": 0.9, "nguon": "n1"},
        mo_ta_ngan={"gia_tri": "Chỉ 99k freeship", "do_tin_cay": 0.9, "nguon": "n1"})
    data = json.loads(raw)
    data["san_pham"]["gia_goc_duyet"] = "150000"
    data["san_pham"]["live_promotion"] = "tặng quà"
    out = pi.validate_extraction(data, ["n1"])
    assert out["fields"]["cong_dung"] == ["Kích rễ"]
    assert out["fields"]["luu_y"] == "Bảo quản nơi mát. Tránh ánh nắng."
    assert out["filtered_price"] is True
    assert "gia_goc_duyet" not in out["fields"] and "live_promotion" not in out["fields"]
    assert not any(pi.has_price(str(v)) for v in out["fields"].values())


def test_legit_dose_text_not_mistaken_for_price():
    assert not pi.has_price("Pha 20ml cho bình 16 lít, giá trị dinh dưỡng cao")
    assert pi.has_price("Giá 120.000đ")


# ---------------------------------------------------------------- Gemini REST
class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_gemini_request_shape_and_key_not_in_url():
    seen = {}

    def opener(request, timeout):
        seen["url"], seen["headers"], seen["body"] = request.full_url, dict(request.header_items()), json.loads(request.data)
        return FakeResponse(json.dumps({"candidates": [{"content": {"parts": [{"text": "{\"ok\":1}"}]}}]}).encode())

    provider = GeminiProvider("AIza-test-key", "gemini-x", opener=opener)
    text = provider.generate("SYS", "USER", [{"mime": "image/jpeg", "data": b"\xff\xd8abc"}])
    assert text == '{"ok":1}'
    assert "generativelanguage.googleapis.com/v1beta/models/gemini-x:generateContent" in seen["url"]
    assert "AIza-test-key" not in seen["url"] and seen["headers"]["X-goog-api-key"] == "AIza-test-key"
    parts = seen["body"]["contents"][0]["parts"]
    assert parts[1]["inline_data"]["mime_type"] == "image/jpeg"
    assert base64.b64decode(parts[1]["inline_data"]["data"]) == b"\xff\xd8abc"
    assert seen["body"]["generationConfig"]["responseMimeType"] == "application/json"
    assert seen["body"]["systemInstruction"]["parts"][0]["text"] == "SYS"


def _http_error(status, message="x"):
    body = json.dumps({"error": {"message": message}}).encode()
    return urllib.error.HTTPError("u", status, "err", {}, io.BytesIO(body))


@pytest.mark.parametrize("status,message,code,needle", [
    (429, "quota", "gemini_quota", "Hết lượt miễn phí hôm nay"),
    (404, "not found", "gemini_model_rejected", "Model Gemini"),
    (400, "API key not valid", "gemini_key_rejected", "Khóa Gemini"),
    (403, "denied", "gemini_key_rejected", "Khóa Gemini"),
    (503, "overloaded", "gemini_overloaded", "quá tải"),
])
def test_gemini_http_errors_have_clear_vietnamese_messages(status, message, code, needle):
    def opener(request, timeout):
        raise _http_error(status, message)

    with pytest.raises(AIProviderError) as err:
        GeminiProvider("k" * 12, "m", opener=opener, sleep=lambda s: None).generate("s", "u")
    assert err.value.code == code and needle in str(err.value)


def test_gemini_without_key_and_deepseek_with_files():
    with pytest.raises(AIProviderError) as err:
        GeminiProvider("").generate("s", "u")
    assert err.value.code == "gemini_key_missing"
    with pytest.raises(AIProviderError):
        DeepSeekTextProvider(object()).generate("s", "u", [{"mime": "image/jpeg", "data": b"x"}])


def test_gemini_settings_and_open_target(tmp_path):
    from config_settings import save_gemini_key, save_gemini_model
    from server import ReplyEngine
    env = tmp_path / ".env"
    env.write_text("DEEPSEEK_API_KEY=keep\n", encoding="utf-8")
    result = save_gemini_key("AIza-abcdefgh", env)
    assert result["gemini_key_configured"] is True and "AIza" not in json.dumps(result)
    save_gemini_model("gemini-3.8-flash", env)
    content = env.read_text(encoding="utf-8")
    assert "DEEPSEEK_API_KEY=keep" in content and "GEMINI_API_KEY=AIza-abcdefgh" in content
    with pytest.raises(ValueError):
        save_gemini_key("short", env)
    assert ReplyEngine.OPEN_TARGETS["gemini_keys"] == "https://aistudio.google.com/apikey"


# ---------------------------------------------------------------- URL
class FakeHttp:
    def __init__(self, routes):
        self.routes, self.opened = routes, []

    def __call__(self, url, timeout=10):
        self.opened.append(url)
        ctype, data = self.routes[url]

        class R(io.BytesIO):
            headers = {"Content-Type": ctype}

        return R(data)


@pytest.fixture
def web(monkeypatch):
    monkeypatch.setattr(pi, "_resolve", lambda host: ["93.184.216.34"])
    routes = {}
    fake = FakeHttp(routes)
    monkeypatch.setattr(pi, "_open_url", fake)
    return fake


def test_url_only_http_https_and_public_hosts(monkeypatch, web):
    for bad in ("file:///etc/passwd", "ftp://x.com/a", "javascript:alert(1)", "", "//x.com"):
        with pytest.raises(pi.IngestError):
            pi.fetch_page(bad)
    monkeypatch.setattr(pi, "_resolve", lambda host: ["127.0.0.1"])
    with pytest.raises(pi.IngestError, match="nội bộ"):
        pi.fetch_page("http://localhost:8793/api/settings")
    monkeypatch.setattr(pi, "_resolve", lambda host: ["10.0.0.5"])
    with pytest.raises(pi.IngestError):
        pi.fetch_page("https://intranet.example/")
    assert web.opened == []                                      # chưa hề mở kết nối


def test_url_extracts_text_og_image_and_limits_size(web):
    page = ("<html><head><meta property='og:image' content='/img/p.png'><title>Đạm cá</title></head>"
            "<body><script>x()</script><p>Đạm cá Amino hữu cơ kích rễ cho lúa và rau màu.</p></body></html>")
    web.routes["https://shop.example/sp"] = ("text/html; charset=utf-8", page.encode())
    web.routes["https://shop.example/img/p.png"] = ("image/png", png_bytes())
    out = pi.fetch_page("https://shop.example/sp")
    assert "kích rễ" in out["text"] and "x()" not in out["text"]
    assert out["og_image"] and out["og_image"][:2] == b"\xff\xd8"      # JPEG đã nén
    big = b"<p>" + b"a" * (pi.URL_MAX_BYTES + 5000) + b"</p>"
    web.routes["https://shop.example/big"] = ("text/html", big)
    out = pi.fetch_page("https://shop.example/big")
    assert len(out["text"]) <= pi.URL_MAX_BYTES                   # cắt ở giới hạn, không đọc hết
    web.routes["https://shop.example/bin"] = ("application/zip", b"PK")
    with pytest.raises(pi.IngestError, match="không phải trang web"):
        pi.fetch_page("https://shop.example/bin")


def test_url_og_image_failure_does_not_break_text(web, monkeypatch):
    monkeypatch.setattr(pi, "_resolve", lambda host: ["127.0.0.1"] if host == "127.0.0.1" else ["93.184.216.34"])
    web.routes["https://shop.example/a"] = (
        "text/html", b"<meta property='og:image' content='http://127.0.0.1/x.png'><p>" + "chữ dài đủ đọc ".encode() * 5 + b"</p>")
    out = pi.fetch_page("https://shop.example/a")
    assert out["text"] and out["og_image"] is None
    assert "http://127.0.0.1/x.png" not in web.opened


def test_og_image_url_parsing_both_attribute_orders():
    assert pi.og_image_url("<meta content='/a.jpg' property='og:image'>", "https://s.vn/x/y") == "https://s.vn/a.jpg"
    assert pi.og_image_url("<meta property=\"og:image\" content=\"javascript:1\">", "https://s.vn") == ""


def test_redirect_to_private_host_blocked(monkeypatch):
    hosts = {"public.example": ["93.184.216.34"], "evil.internal": ["192.168.1.1"]}
    monkeypatch.setattr(pi, "_resolve", lambda host: hosts[host])
    handler = pi._SafeRedirect()
    import urllib.request
    req = urllib.request.Request("https://public.example/")
    with pytest.raises(pi.IngestError):
        handler.redirect_request(req, None, 302, "Found", {}, "http://evil.internal/admin")


# ---------------------------------------------------------------- đầu vào
def test_input_limits_and_validation():
    with pytest.raises(pi.IngestError, match="Chưa có gì"):
        pi.prepare_sources({})
    with pytest.raises(pi.IngestError, match="Tối đa"):
        pi.prepare_sources({"images": [{"name": f"{i}.png", "data": b64(png_bytes())} for i in range(pi.MAX_IMAGES + 1)]})
    with pytest.raises(pi.IngestError):
        pi.prepare_sources({"images": [{"name": "x.png", "data": b64(b"not an image")}]})
    with pytest.raises(pi.IngestError):
        pi.prepare_sources({"files": [{"name": "a.exe", "data": b64(b"MZ" * 30)}]})
    got = pi.prepare_sources({"images": [{"name": "a.png", "data": "data:image/png;base64," + b64(png_bytes())}],
                              "text": "  chữ dán đủ dài để đọc  "})
    assert [s["kind"] for s in got] == ["image", "text"]


def test_url_source_adds_og_image_candidate_not_sent_to_model():
    fake = lambda url: {"url": url, "text": "Nội dung trang sản phẩm đủ dài để đọc.", "og_image": pi.normalize_image(png_bytes())}
    sources = pi.prepare_sources({"url": "https://s.example/p"}, fetch=fake)
    assert [s["kind"] for s in sources] == ["url", "og_image"]
    assert not pi.needs_vision(sources) and pi.vision_files(sources) == []
    prov = FakeProvider("deepseek", [good_json()], supports_files=False)
    out = pi.ProductIngest(None, FakeProvider("gemini", [], configured=False), prov, fetch=fake).extract({"url": "https://s.example/p"})
    assert out["provider"] == "deepseek" and any(s.get("thumb") for s in out["sources"])


# ---------------------------------------------------------------- hồ sơ nháp
def _service(engine, reply=None):
    prov = FakeProvider("gemini", [reply or good_json()])
    return pi.ProductIngest(engine, prov, None), prov


def test_create_draft_is_unapproved_stores_image_and_ignores_price_fields(tmp_path):
    engine = _engine(tmp_path)
    ingest, _ = _service(engine)
    ex = ingest.extract({"images": [{"name": "truoc.png", "data": b64(png_bytes())}], "text": "Đạm cá Amino 5L " * 3})
    image_id = next(s["id"] for s in ex["sources"] if s.get("thumb"))
    fields = {k: v for k, v in ex["fields"].items() if v}
    fields.update({"gia_goc_duyet": "150000", "live_promotion": "tặng quà", "cong_dung": ["Kích rễ", "Giá 99.000đ"]})
    out = ingest.create_draft({"fields": fields, "session_id": ex["session_id"],
                               "image": {"source_id": image_id}, "source_url": "https://shop.example/p"})
    assert out["status"] == "draft" and out["approved_for_live"] is False and out["image_saved"] is True
    record = next(r for r in engine.knowledge.all_records() if r["san_pham_id"] == out["id"])
    assert record["ten_san_pham"] == "Đạm Cá Amino 5L" and record["nha_san_xuat"] == "Cty ABC"
    assert record["thanh_phan_dang_ky"] == {"Đạm tổng số": "5%"}
    assert record["cong_dung"] == ["Kích rễ"]
    assert record["nguon_chinh_thuc"] == "https://shop.example/p"
    assert not record.get("gia_goc_duyet") and not record.get("live_promotion")
    assert record.get("approved_for_live") in (False, None)
    assert ingest.staged_image_path(out["id"]).is_file()
    assert out["id"] not in engine.knowledge.mapping_assignments().values()      # chưa gắn, chưa duyệt


def test_create_draft_requires_name_and_unknown_image_session_is_ignored(tmp_path):
    engine = _engine(tmp_path)
    ingest, _ = _service(engine)
    with pytest.raises(pi.IngestError):
        ingest.create_draft({"fields": {"cong_dung": ["x"]}})
    out = ingest.create_draft({"fields": {"ten_san_pham": "Kali Bo 1L"}, "session_id": "nope", "image": {"source_id": "n1"}})
    assert out["image_saved"] is False


def test_suggest_skus_from_live_cart_and_owner_link(tmp_path):
    import listing_match
    engine = _engine(tmp_path)
    ingest, _ = _service(engine)
    title = _titles()[0]
    name = listing_match.short_name(title["title"])
    ex = ingest.extract({"images": [{"name": "a.png", "data": b64(png_bytes())}]})
    image_id = next(s["id"] for s in ex["sources"] if s.get("thumb"))
    out = ingest.create_draft({"fields": {"ten_san_pham": name, "cong_dung": ["Kích rễ"]},
                               "session_id": ex["session_id"], "image": {"source_id": image_id}})
    ids = [s["product_id"] for s in out["suggestions"]]
    assert title["product_id"] in ids
    assert ingest.suggest_skus(out["id"]) == out["suggestions"]
    done = ingest.link_draft(out["id"], title["product_id"])
    assert done["image_applied"] is True
    row = next(p for p in engine.mapping_state()["profiles"] if p["id"] == out["id"])
    assert row["draft_for"] == title["product_id"] and row["status"] == "draft"
    assert engine.workspace.product_image_path(title["product_id"]).is_file()
    with pytest.raises(ValueError):
        engine.workspace.set_draft_product(out["id"], "khong-co-sku")
    with pytest.raises(ValueError):
        engine.workspace.set_draft_product("WEB-1006", title["product_id"])      # không phải nháp tự tạo


def test_session_store_is_bounded_and_expires():
    clock = [1000.0]
    ingest = pi.ProductIngest(None, None, None, clock=lambda: clock[0])
    src = [{"id": "n1", "kind": "image", "data": b"x"}]
    first = ingest._remember(src)
    for _ in range(pi.MAX_SESSIONS + 3):
        clock[0] += 1
        ingest._remember(src)
    assert len(ingest._sessions) <= pi.MAX_SESSIONS and first not in ingest._sessions
    clock[0] += pi.SESSION_TTL + 1
    ingest._remember(src)
    assert len(ingest._sessions) == 1


def test_http_routes_extract_draft_link_and_image(tmp_path):
    import threading
    from http.server import ThreadingHTTPServer
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen
    import server
    engine = _engine(tmp_path)
    engine.ingest = pi.ProductIngest(engine, FakeProvider("gemini", [good_json()]), None)

    class Handler(server.Handler):
        pass
    Handler.engine = engine
    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=http.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
    worker.start()
    origin = f"http://127.0.0.1:{http.server_port}"

    def call(path, payload=None, from_origin=origin):
        req = Request(origin + path, data=None if payload is None else json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json", "Origin": from_origin})
        with urlopen(req, timeout=10) as response:
            return response.read(), response.headers.get("Content-Type")
    try:
        with pytest.raises(HTTPError) as error:
            call("/api/ingest/extract", {"text": "x"}, "https://other.example")
        assert error.value.code == 403
        assert json.loads(call("/api/ingest/status")[0])["gemini_configured"] is True
        with pytest.raises(HTTPError) as error:
            call("/api/ingest/extract", {})
        assert error.value.code == 400 and b"Ch" in error.value.read()
        ex = json.loads(call("/api/ingest/extract", {"images": [{"name": "a.png", "data": b64(png_bytes())}]})[0])
        image_id = next(s["id"] for s in ex["sources"] if s.get("thumb"))
        draft = json.loads(call("/api/ingest/draft", {"fields": {"ten_san_pham": "CHUYÊN ĐẬU PHỘNG", "cong_dung": ["Kích rễ"]},
                                                      "session_id": ex["session_id"], "image": {"source_id": image_id}})[0])
        assert draft["image_saved"] and draft["suggestions"]
        body, ctype = call("/api/ingest/image/" + draft["id"])
        assert ctype == "image/webp" and body[:4] == b"RIFF"
        link = json.loads(call("/api/ingest/link", {"id": draft["id"], "product_id": draft["suggestions"][0]["product_id"]})[0])
        assert link["image_applied"] is True
    finally:
        http.shutdown()
        http.server_close()
        worker.join()


# ---------------------------------------------------------------- Gemini: tự dò model
def _m(name, methods=("generateContent",)):
    return {"name": "models/" + name, "supportedGenerationMethods": list(methods)}


MODEL_LIST = [
    _m("gemini-2.5-flash"), _m("gemini-2.5-flash-lite"), _m("gemini-2.5-pro"),
    _m("gemini-3.0-flash-preview"), _m("gemini-2.0-flash"), _m("gemini-2.0-flash-001"),
    _m("gemini-2.5-flash-image"), _m("gemini-2.5-flash-preview-tts"),
    _m("gemini-embedding-flash", ("embedContent",)), _m("gemini-3.1-flash-live", ("bidiGenerateContent",)),
]


def test_pick_flash_prefers_newest_stable_non_lite():
    assert ai_providers.pick_flash_model(MODEL_LIST) == "gemini-2.5-flash"
    # bản ổn định mới hơn thắng, cả khi nhiều chữ số phiên bản
    assert ai_providers.pick_flash_model(MODEL_LIST + [_m("gemini-2.10-flash")]) == "gemini-2.10-flash"
    # không có bản ổn định: lấy preview mới nhất; không có gì: lite; không flash: rỗng
    previews = [_m("gemini-3.0-flash-preview"), _m("gemini-2.5-flash-lite"), _m("gemini-2.5-pro")]
    assert ai_providers.pick_flash_model(previews) == "gemini-3.0-flash-preview"
    assert ai_providers.pick_flash_model([_m("gemini-2.5-flash-lite"), _m("gemini-2.5-pro")]) == "gemini-2.5-flash-lite"
    assert ai_providers.pick_flash_model([_m("gemini-2.5-pro"), _m("gemini-x-flash", ("embedContent",))]) == ""


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _listing_opener(calls, pages=None, fail=False):
    pages = pages or [{"models": MODEL_LIST}]

    def opener(request, timeout):
        calls.append(request)
        if request.full_url.split("?")[0] == ai_providers.GEMINI_MODELS_ENDPOINT:
            if fail:
                raise urllib.error.URLError("offline")
            index = 0 if "pageToken" not in request.full_url else int(request.full_url.rsplit("=", 1)[1])
            return FakeResponse(json.dumps(pages[index]).encode())
        return FakeResponse(json.dumps({"candidates": [{"content": {"parts": [{"text": "{}"}]}}]}).encode())

    return opener


def test_detect_model_header_pagination_and_cache():
    calls, clock = [], Clock()
    pages = [{"models": MODEL_LIST[:3], "nextPageToken": "1"}, {"models": MODEL_LIST[3:]}]
    provider = GeminiProvider("AIza-test-key", "", opener=_listing_opener(calls, pages), clock=clock)
    assert provider.model == ai_providers.GEMINI_FALLBACK_MODEL and provider.model_source == "fallback"
    assert provider.detect_model() == "gemini-2.5-flash" and provider.model_source == "auto"
    assert len(calls) == 2
    first = calls[0]
    assert first.get_method() == "GET" and first.header_items() and dict(first.header_items())["X-goog-api-key"] == "AIza-test-key"
    assert "AIza-test-key" not in first.full_url
    provider.detect_model()                      # còn hạn: không gọi lại
    assert len(calls) == 2
    clock.now += ai_providers.GEMINI_DETECT_TTL + 1
    provider.detect_model()                      # quá ~1 ngày: dò lại
    assert len(calls) == 4


def test_generate_lazily_detects_and_uses_detected_model():
    calls = []
    provider = GeminiProvider("AIza-test-key", "", opener=_listing_opener(calls))
    provider.generate("s", "u")
    urls = [c.full_url for c in calls]
    assert urls[0].startswith(ai_providers.GEMINI_MODELS_ENDPOINT)
    assert urls[1].endswith("/gemini-2.5-flash:generateContent")
    provider.generate("s", "u")
    assert len(calls) == 3                       # lần hai không dò lại


def test_owner_model_is_never_overridden_or_probed():
    calls = []
    provider = GeminiProvider("AIza-test-key", "gemini-mine", opener=_listing_opener(calls))
    assert provider.detect_model(force=True) == "gemini-mine" and provider.model_source == "owner"
    provider.generate("s", "u")
    assert all("/models?" not in c.full_url for c in calls)
    provider.configure(model="")                 # xóa model tay -> quay về tự dò
    assert provider.detect_model() == "gemini-2.5-flash" and provider.model_source == "auto"


def test_detect_failure_falls_back_and_retries_later():
    calls, clock = [], Clock()
    provider = GeminiProvider("AIza-test-key", "", opener=_listing_opener(calls, fail=True), clock=clock)
    assert provider.detect_model() == ai_providers.GEMINI_FALLBACK_MODEL
    provider.detect_model()
    assert len(calls) == 1                       # không dò dồn dập khi lỗi
    clock.now += ai_providers.GEMINI_DETECT_RETRY + 1
    provider.detect_model()
    assert len(calls) == 2
    # không có khóa: không gọi mạng
    assert GeminiProvider("", "", opener=_listing_opener(calls)).detect_model() == ai_providers.GEMINI_FALLBACK_MODEL
    assert len(calls) == 2


def test_new_key_forces_fresh_detection_and_clear_setting(tmp_path):
    calls = []
    provider = GeminiProvider("AIza-old-key-1", "", opener=_listing_opener(calls))
    provider.detect_model()
    provider.configure(api_key="AIza-new-key-2")
    assert provider.model_source == "fallback"
    provider.detect_model(force=True)
    assert len(calls) == 2 and dict(calls[1].header_items())["X-goog-api-key"] == "AIza-new-key-2"
    from config_settings import clear_gemini_model, save_gemini_model
    env = tmp_path / ".env"
    env.write_text("KEEP=1" + chr(10), encoding="utf-8")
    save_gemini_model("gemini-mine", env)
    clear_gemini_model(env)
    assert env.read_text(encoding="utf-8") == "KEEP=1" + chr(10)
