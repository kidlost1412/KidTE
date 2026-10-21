"""Link ảnh chết không bị tải lại dồn dập; request /thumb không treo, không tải trùng.

Mạng giả lập, đồng hồ giả lập, đồng bộ bằng Event (không sleep dài).
"""
import io
import threading
import time
import types

from PIL import Image
import pytest

import media_assets
import server

BAD = "https://images.example.test/dead?signature=expired"
NEW = "https://images.example.test/dead?signature=renewed"
GOOD = "https://images.example.test/good"


@pytest.fixture
def image_bytes():
    stream = io.BytesIO()
    Image.new("RGB", (64, 64), (200, 10, 10)).save(stream, "PNG")
    return stream.getvalue()


@pytest.fixture
def cache(tmp_path):
    return media_assets.ProductImageCache(tmp_path)


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(media_assets.time, "monotonic", lambda: now[0])
    return now


def _failing(calls):
    def download(url):
        calls.append(url)
        raise OSError("HTTP 403")
    return download


# ---- (a) lùi luỹ tiến -------------------------------------------------------

def test_dead_link_backoff_grows_30s_2m_10m_then_caps_at_30m(cache, clock, monkeypatch):
    calls = []
    monkeypatch.setattr(media_assets, "_download", _failing(calls))
    assert cache.RETRY_BACKOFF == (30, 120, 600, 1800)
    assert cache.fetch("p", [BAD]) is None
    assert len(calls) == 1
    for delay, expected_calls in ((30, 2), (120, 3), (600, 4), (1800, 5), (1800, 6)):
        clock[0] += delay - 0.01
        assert cache.fetch("p", [BAD]) is None
        assert len(calls) == expected_calls - 1, f"retried before {delay}s"
        clock[0] += 0.01
        assert cache.fetch("p", [BAD]) is None
        assert len(calls) == expected_calls, f"not retried after {delay}s"


def test_changed_url_set_retries_immediately_and_restarts_backoff(cache, clock, monkeypatch):
    calls = []
    monkeypatch.setattr(media_assets, "_download", _failing(calls))
    for step in (0, 30, 120, 600):
        clock[0] += step
        cache.fetch("p", [BAD])
    assert len(calls) == 4  # đang ở bậc 30 phút
    assert cache.fetch("p", [NEW]) is None
    assert calls[-1] == NEW, "link ký mới phải được thử ngay"
    clock[0] += 30
    assert cache.fetch("p", [NEW]) is None
    assert len(calls) == 6, "bộ URL mới bắt đầu lại từ bậc 30 giây"


def test_success_clears_backoff(cache, clock, image_bytes, monkeypatch):
    calls = []
    state = {"ok": False}

    def download(url):
        calls.append(url)
        if not state["ok"]:
            raise OSError("HTTP 404")
        return image_bytes

    monkeypatch.setattr(media_assets, "_download", download)
    cache.fetch("p", [BAD])
    clock[0] += 30
    cache.fetch("p", [BAD])
    clock[0] += 120
    state["ok"] = True
    assert cache.fetch("p", [BAD]).is_file()
    assert "p" not in cache._failed


def test_prefetch_on_every_page_load_does_not_hammer_dead_links(cache, clock, monkeypatch):
    calls = []
    monkeypatch.setattr(media_assets, "_download", _failing(calls))
    started = []

    class InlineThread:
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            started.append(1)
            self.target()

    monkeypatch.setattr(media_assets.threading, "Thread", InlineThread)
    sources = {f"p{i}": [BAD + f"&i={i}", GOOD + f"?bad={i}"] for i in range(10)}
    assert cache.prefetch(sources) == 10
    first = len(calls)
    assert first == 20
    # UI mở và tải lại trang liên tục trong 25 phút sau lần lỗi thứ hai.
    clock[0] += 30
    cache.prefetch(sources)
    assert len(calls) == 40
    for _ in range(150):
        clock[0] += 10
        cache.prefetch(sources)
    # 1500 s sau lần 2: chỉ thêm đúng lần 3 (sau 120 s) và lần 4 (sau 600 s nữa).
    assert len(calls) == 80, len(calls)


def test_prefetch_limits_new_products_per_call(cache, image_bytes, monkeypatch):
    monkeypatch.setattr(media_assets, "_download", lambda url: image_bytes)
    workers = []

    class Worker:
        def __init__(self, target, **kwargs):
            self.target = target
            workers.append(self)

        def start(self):
            pass

    monkeypatch.setattr(media_assets.threading, "Thread", Worker)
    sources = {f"p{i}": [GOOD + f"?i={i}"] for i in range(cache.PREFETCH_BATCH + 8)}
    assert cache.prefetch(sources) == cache.PREFETCH_BATCH
    workers[0].target()
    assert cache.prefetch(sources) == 8, "phần vượt hạn mức được tải ở lần sau"
    workers[1].target()
    assert all(cache.cached(pid) for pid in sources)


def test_parallel_downloads_are_bounded(cache, image_bytes, monkeypatch):
    lock = threading.Lock()
    active = [0]
    peak = [0]

    def download(url):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.03)
        with lock:
            active[0] -= 1
        return image_bytes

    monkeypatch.setattr(media_assets, "_download", download)
    threads = [threading.Thread(target=cache.fetch, args=(f"p{i}", [GOOD + f"?i={i}"]))
               for i in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert peak[0] <= cache.MAX_PARALLEL_DOWNLOADS
    assert all(cache.cached(f"p{i}") for i in range(12))


# ---- (b) request /thumb -----------------------------------------------------

def test_request_wait_budget_is_at_most_8_seconds():
    assert 0 < media_assets.ProductImageCache.REQUEST_WAIT_SECONDS <= 8


def test_concurrent_requests_for_same_product_download_once(cache, image_bytes, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def download(url):
        calls.append(url)
        entered.set()
        assert release.wait(5)
        return image_bytes

    monkeypatch.setattr(media_assets, "_download", download)
    results = []
    threads = [threading.Thread(target=lambda: results.append(cache.fetch_waiting("p", [GOOD], 5)))
               for _ in range(5)]
    for thread in threads:
        thread.start()
    assert entered.wait(5)
    time.sleep(0.05)
    release.set()
    for thread in threads:
        thread.join(5)
    assert calls == [GOOD], "cùng pid đang tải thì phải chờ, không tải thêm"
    assert len(results) == 5 and all(r == cache.cached("p") for r in results)


def test_slow_download_does_not_hold_request_beyond_budget(cache, image_bytes, monkeypatch):
    release = threading.Event()
    done = threading.Event()

    def download(url):
        assert release.wait(5)
        done.set()
        return image_bytes

    monkeypatch.setattr(media_assets, "_download", download)
    started = time.monotonic()
    assert cache.fetch_waiting("p", [GOOD], timeout=0.2) is None
    assert time.monotonic() - started < 2
    release.set()
    assert done.wait(5)
    for _ in range(100):
        if cache.cached("p"):
            break
        time.sleep(0.02)
    assert cache.cached("p"), "ảnh tải xong ở nền vẫn được lưu cho lần sau"


def test_request_respects_backoff_without_network(cache, clock, monkeypatch):
    calls = []
    monkeypatch.setattr(media_assets, "_download", _failing(calls))
    assert cache.fetch("p", [BAD]) is None
    monkeypatch.setattr(media_assets, "_download", lambda url: pytest.fail("must not download"))
    assert cache.fetch_waiting("p", [BAD], timeout=5) is None


def test_product_thumb_path_is_bounded_and_deduplicated(cache, image_bytes, monkeypatch):
    release = threading.Event()
    calls = []

    def download(url):
        calls.append(url)
        assert release.wait(5)
        return image_bytes

    def no_upload(pid):
        raise ValueError("no_uploaded_image")

    monkeypatch.setattr(media_assets, "_download", download)
    monkeypatch.setattr(cache, "REQUEST_WAIT_SECONDS", 0.2)
    engine = types.SimpleNamespace(
        product_images=cache, workspace=types.SimpleNamespace(product_image_path=no_upload),
        _product_image_sources=lambda: {"p": [GOOD]})
    errors = []

    def request():
        try:
            server.ReplyEngine.product_thumb_path(engine, "p")
        except ValueError as exc:
            errors.append(str(exc))

    started = time.monotonic()
    threads = [threading.Thread(target=request) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert time.monotonic() - started < 3
    assert errors == ["product_image_not_found"] * 4
    assert calls == [GOOD]
    release.set()
    for _ in range(100):
        if cache.cached("p"):
            break
        time.sleep(0.02)
    assert server.ReplyEngine.product_thumb_path(engine, "p") == cache.cached("p")
    assert calls == [GOOD]
