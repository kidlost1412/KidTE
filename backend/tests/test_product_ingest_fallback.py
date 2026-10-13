"""Gemini: thử lần lượt các model (404/429/503), nhớ model lỗi, báo model đã đọc; quét thêm trang sản phẩm.

Không gọi mạng: opener giả theo từng model, DNS và HTTP của trang web đều được mock.
"""
import json
import sys
import urllib.error
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ai_providers  # noqa: E402
import product_ingest as pi  # noqa: E402
from ai_providers import AIProviderError, GeminiProvider, auto_chain, pick_default_model  # noqa: E402
from test_product_ingest import FakeHttp, FakeProvider, FakeResponse, _http_error, _m, good_json, MODEL_LIST  # noqa: E402

ALIAS = "gemini-flash-latest"
ALIAS_LIST = [_m("gemini-3.8-flash"), _m("gemini-3.7-flash"), _m(ALIAS), _m("gemini-2.5-flash"),
              _m("gemini-2.5-flash-lite"), _m("gemini-3.0-flash-preview")]


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Script:
    """Trả lời theo từng model. plan: {model: [mã HTTP lỗi, ...]} dùng lần lượt, hết thì thành công."""

    def __init__(self, plan=None, listing=None, listing_fail=False, text='{"ok": 1}'):
        self.plan = {k: list(v) for k, v in (plan or {}).items()}
        self.text = text                                  # chữ model trả về (trong candidates)
        self.listing = ALIAS_LIST if listing is None else listing
        self.listing_fail = listing_fail
        self.model_calls = []        # (model, timeout) cho mỗi lần gọi generateContent
        self.listing_calls = 0

    def __call__(self, request, timeout):
        url = request.full_url
        if url.split("?")[0] == ai_providers.GEMINI_MODELS_ENDPOINT:
            self.listing_calls += 1
            if self.listing_fail:
                raise urllib.error.URLError("offline")
            return FakeResponse(json.dumps({"models": self.listing}).encode())
        model = url.rsplit("/", 1)[1].split(":")[0]
        self.model_calls.append((model, timeout))
        queue = self.plan.get(model) or []
        step = queue.pop(0) if queue else 200
        if step == 200:
            return FakeResponse(json.dumps({"candidates": [{"content": {"parts": [{"text": self.text}]}}]}).encode())
        message = "API key not valid" if step == 400 else "boom"
        raise _http_error(step, message)

    def models(self):
        return [model for model, _ in self.model_calls]


def _provider(script, clock=None, sleeps=None, model=""):
    return GeminiProvider("AIza-test-key", model, opener=script, clock=clock or Clock(),
                          sleep=(sleeps.append if sleeps is not None else (lambda s: None)))


# ---------------------------------------------------------------- thứ tự model
def test_auto_chain_puts_alias_first_and_stable_before_preview_and_lite():
    assert auto_chain(ALIAS_LIST) == [ALIAS, "gemini-3.8-flash", "gemini-3.7-flash", "gemini-2.5-flash",
                                      "gemini-3.0-flash-preview", "gemini-2.5-flash-lite"]
    chain = auto_chain(MODEL_LIST)                 # không có alias: bản mới nhất trước, alias ngay sau
    assert chain[0] == "gemini-2.5-flash" and chain[1] == ALIAS
    assert auto_chain([]) == [] and auto_chain([_m("gemini-2.5-pro")]) == []


def test_default_model_is_alias_when_listed():
    assert pick_default_model(ALIAS_LIST) == ALIAS
    assert pick_default_model(MODEL_LIST) == "gemini-2.5-flash"
    assert pick_default_model([]) == ""


def test_default_timeout_is_180_seconds_and_used_for_generate():
    assert GeminiProvider("k").timeout == 180 == ai_providers.GEMINI_TIMEOUT
    script = Script({"gemini-3.8-flash": []})
    provider = GeminiProvider("AIza-test-key", "gemini-3.8-flash", opener=script)
    provider.generate("s", "u")
    assert script.model_calls == [("gemini-3.8-flash", 180)]


# ---------------------------------------------------------------- fallback + nhớ model lỗi
def test_503_then_404_falls_through_and_remembers_failures():
    clock, sleeps = Clock(), []
    script = Script({ALIAS: [503], "gemini-3.8-flash": [404]})
    provider = _provider(script, clock, sleeps)
    assert provider.generate("s", "u") == json.dumps({"ok": 1})
    assert script.models() == [ALIAS, "gemini-3.8-flash", "gemini-3.7-flash"]
    assert sleeps == [ai_providers.GEMINI_RETRY_BACKOFF]          # chỉ chờ sau 503
    assert provider.last_model == "gemini-3.7-flash"

    script.model_calls.clear()
    provider.generate("s", "u")                                  # alias (503) và 3.8 (404) đang bị bỏ qua
    assert script.models() == ["gemini-3.7-flash"]

    clock.now += ai_providers.GEMINI_SKIP_OVERLOADED + 1         # hết 10 phút: alias được thử lại
    script.model_calls.clear()
    provider.generate("s", "u")
    assert script.models() == [ALIAS]
    assert provider.last_model == ALIAS


def test_404_model_is_skipped_for_one_day_not_longer():
    clock = Clock()
    script = Script({ALIAS: [404]})
    provider = _provider(script, clock)
    provider.generate("s", "u")
    clock.now += ai_providers.GEMINI_SKIP_NOT_FOUND - 60
    script.model_calls.clear()
    provider.generate("s", "u")
    assert ALIAS not in script.models()
    clock.now += 120
    script.model_calls.clear()
    provider.generate("s", "u")
    assert script.models()[0] == ALIAS


def test_429_moves_on_without_remembering_the_model():
    clock = Clock()
    script = Script({ALIAS: [429]})
    provider = _provider(script, clock)
    provider.generate("s", "u")
    assert provider.last_model == "gemini-3.8-flash"
    script.model_calls.clear()
    provider.generate("s", "u")
    assert script.models()[0] == ALIAS                           # 429 không bị nhớ: lần sau thử lại trước


def test_at_most_four_attempts_and_overloaded_message():
    sleeps = []
    listing = [_m(f"gemini-3.{i}-flash") for i in range(9, 0, -1)]
    script = Script({name: [503] * 3 for name in ai_providers.auto_chain(listing)}, listing=listing)
    provider = _provider(script, sleeps=sleeps)
    with pytest.raises(AIProviderError) as err:
        provider.generate("s", "u")
    assert err.value.code == "gemini_overloaded" and "quá tải" in str(err.value) and "thử lại sau ít phút" in str(err.value)
    assert len(script.model_calls) == ai_providers.GEMINI_MAX_ATTEMPTS == 4
    assert len(sleeps) == 3                                       # không chờ thêm sau lần thử cuối


def test_all_models_quota_gives_quota_message():
    script = Script({name: [429] for name in ["gemini-3.8-flash", "gemini-3.7-flash", ALIAS, "gemini-2.5-flash"]})
    provider = _provider(script)
    with pytest.raises(AIProviderError) as err:
        provider.generate("s", "u")
    assert err.value.code == "gemini_quota" and "Hết lượt miễn phí hôm nay" in str(err.value)


def test_invalid_key_stops_at_first_attempt():
    script = Script({ALIAS: [400]})
    provider = _provider(script)
    with pytest.raises(AIProviderError) as err:
        provider.generate("s", "u")
    assert err.value.code == "gemini_key_rejected" and "Khóa Gemini" in str(err.value)
    assert script.models() == [ALIAS]


def test_owner_model_fails_then_auto_chain_is_loaded_as_backup():
    script = Script({"gemini-mine": [404]})
    provider = _provider(script, model="gemini-mine")
    assert provider.generate("s", "u") == json.dumps({"ok": 1})
    assert script.models() == ["gemini-mine", ALIAS]
    assert script.listing_calls == 1                              # chỉ dò khi model tay lỗi
    assert provider.last_model == ALIAS


def test_owner_model_success_never_probes_listing():
    script = Script()
    provider = _provider(script, model="gemini-mine")
    provider.generate("s", "u")
    assert script.listing_calls == 0 and script.models() == ["gemini-mine"]


def test_network_error_is_not_retried_across_models():
    def opener(request, timeout):
        if request.full_url.split("?")[0] == ai_providers.GEMINI_MODELS_ENDPOINT:
            raise urllib.error.URLError("offline")
        raise urllib.error.URLError("offline")
    provider = GeminiProvider("AIza-test-key", "", opener=opener, sleep=lambda s: None)
    with pytest.raises(AIProviderError) as err:
        provider.generate("s", "u")
    assert err.value.code == "gemini_network"


def test_model_that_answered_is_reported_in_extraction_result():
    script = Script({ALIAS: [503]}, text=good_json())
    gemini = _provider(script)
    out = pi.ProductIngest(None, gemini, None).extract({"text": "Đạm cá Amino 5L. Công dụng kích rễ cho lúa."})
    assert out["model"] == "gemini-3.8-flash" and out["provider"] == "gemini"


# ---------------------------------------------------------------- quét thêm trang sản phẩm
def test_root_url_detection():
    assert pi.is_root_url("https://shop.example") and pi.is_root_url("https://shop.example/")
    assert not pi.is_root_url("https://shop.example/san-pham/x")
    assert not pi.is_root_url("https://shop.example/?page=2")


def test_find_links_same_site_product_paths_card_blocks_and_order():
    page = """
    <a href="/san-pham/dam-ca.html">Đạm cá</a>
    <a href="https://www.shop.example/danh-muc/phan-bon">Phân bón</a>
    <a href="https://other.example/san-pham/x">ngoài</a>
    <a href="/images/logo.png">logo</a>
    <a href="/gioi-thieu">Giới thiệu</a>
    <a href="/">Trang chủ</a>
    <a href="#top">đầu trang</a>
    <a href="mailto:a@b.c">mail</a>
    <a href="/san-pham/dam-ca.html#tab">trùng</a>
    <div class="product-card"><a href="/chi-tiet/kali-1kg">Kali 1kg</a></div>
    <div class="sp-item"><a href="/khac">   </a></div>
    """
    assert pi.find_product_links(page, "https://shop.example/") == [
        "https://shop.example/san-pham/dam-ca.html",
        "https://www.shop.example/danh-muc/phan-bon",
        "https://shop.example/chi-tiet/kali-1kg",
    ]


def test_find_links_limit_is_twenty():
    page = "".join(f'<a href="/san-pham/p{i}.html">P{i}</a>' for i in range(30))
    links = pi.find_product_links(page, "https://shop.example/")
    assert len(links) == pi.CRAWL_MAX_PAGES == 20 and links[0] == "https://shop.example/san-pham/p0.html"


def test_crawl_on_for_root_reads_at_most_twenty_pages():
    home = {"url": "https://shop.example/", "text": "Trang chủ có đủ chữ để đọc.", "og_image": None,
            "links": [f"https://shop.example/san-pham/p{i}" for i in range(25)]}
    seen = []

    def crawl(link):
        seen.append(link)
        return {"url": link, "text": "Sản phẩm " + link, "og_image": None}

    stats = {}
    sources = pi.prepare_sources({"url": "https://shop.example/"}, fetch=lambda url: home, crawl_fetch=crawl, stats=stats)
    assert len(seen) == 20
    assert sum(1 for s in sources if s["kind"] == "url") == 21
    assert stats == {"enabled": True, "found": 25, "read": 20, "failed": 0}


def test_crawl_off_for_deep_link_or_when_unchecked():
    home = {"url": "https://shop.example/", "text": "chữ đủ dài để đọc", "og_image": None,
            "links": ["https://shop.example/san-pham/p1"]}

    def no_crawl(link):
        raise AssertionError("không được quét")

    stats = {}
    pi.prepare_sources({"url": "https://shop.example/san-pham/x"}, fetch=lambda url: home, crawl_fetch=no_crawl, stats=stats)
    assert stats["enabled"] is False
    stats = {}
    pi.prepare_sources({"url": "https://shop.example/", "crawl": False}, fetch=lambda url: home,
                       crawl_fetch=no_crawl, stats=stats)
    assert stats["enabled"] is False and stats["read"] == 0


def test_crawl_skips_failed_pages_and_caps_total_characters():
    links = [f"https://shop.example/san-pham/p{i}" for i in range(6)]
    home = {"url": "https://shop.example/", "text": "chữ đủ dài để đọc", "og_image": None, "links": links}
    big = "a" * 50_000
    fetched = []

    def crawl(link):
        fetched.append(link)
        if link.endswith("p0"):
            raise pi.IngestError("không tải được")
        return {"url": link, "text": big, "og_image": None}

    stats = {}
    sources = pi.prepare_sources({"url": "https://shop.example/"}, fetch=lambda url: home, crawl_fetch=crawl, stats=stats)
    crawled = [s for s in sources if s["kind"] == "url"][1:]
    assert sum(len(s["text"]) for s in crawled) == pi.CRAWL_MAX_CHARS == 120_000
    assert stats["failed"] == 1 and stats["read"] == 3
    assert links[4] not in fetched and links[5] not in fetched      # đã đầy 120k: không quét thêm


def test_crawl_page_headers_reach_the_model_prompt():
    home = {"url": "https://shop.example/", "text": "Trang chủ đủ chữ.", "og_image": None,
            "links": ["https://shop.example/san-pham/p0"]}
    sources = pi.prepare_sources({"url": "https://shop.example/"}, fetch=lambda url: home,
                                 crawl_fetch=lambda link: {"url": link, "text": "Kali 1kg", "og_image": None})
    text = pi.build_user_text(sources)
    assert "(trang web: https://shop.example/san-pham/p0) ===" in text


def test_fetch_page_returns_links_and_skips_image_when_asked(monkeypatch):
    monkeypatch.setattr(pi, "_resolve", lambda host: ["93.184.216.34"])
    html = ("<meta property='og:image' content='/img/p.png'><p>Trang chủ đủ chữ để đọc hôm nay.</p>"
            "<a href='/san-pham/kali'>Kali</a>")
    routes = {"https://shop.example/": ("text/html", html.encode("utf-8")),
              "https://shop.example/img/p.png": ("image/png", b"PNG")}
    fake = FakeHttp(routes)
    monkeypatch.setattr(pi, "_open_url", fake)
    out = pi.fetch_page("https://shop.example/", with_image=False)
    assert out["links"] == ["https://shop.example/san-pham/kali"] and out["og_image"] is None
    assert "https://shop.example/img/p.png" not in fake.opened


def test_extract_with_crawl_reports_stats_and_sources():
    home = {"url": "https://shop.example/", "text": "Trang chủ có đủ chữ.", "og_image": None,
            "links": ["https://shop.example/san-pham/a", "https://shop.example/san-pham/b"]}
    gem = FakeProvider("gemini", [good_json()])
    ingest = pi.ProductIngest(None, gem, None, fetch=lambda url: home,
                              crawl_fetch=lambda link: {"url": link, "text": "Đạm cá Amino 5L", "og_image": None})
    out = ingest.extract({"url": "https://shop.example/"})
    assert out["crawl"] == {"enabled": True, "found": 2, "read": 2, "failed": 0}
    assert [s["label"] for s in out["sources"]] == ["https://shop.example/", "https://shop.example/san-pham/a",
                                                    "https://shop.example/san-pham/b"]
