"""Cover URLs survive multi-cart syncs; transient failures can recover."""
import io
import json
import threading
import types
import urllib.error
import urllib.request

from PIL import Image
import pytest

import media_assets
import product_blocklist
import server


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "LIVE_CATALOG_PATH", tmp_path / "live.json")
    monkeypatch.setattr(server, "LIVE_RAW_CATALOG_PATH", tmp_path / "raw.json")
    monkeypatch.setattr(product_blocklist, "REMOVED_PATH", tmp_path / "removed.json")
    product_blocklist._cache.clear()
    return server.LiveCatalogStore()


def snap(uid, pid, url=None):
    result = {"host": {"user_id": uid}, "products": [{"product_id": pid, "title": pid}]}
    if url:
        result["raw_api_data"] = {"products": [{"product_id": pid, "cover": {"url_list": [url]}}]}
    return result


def test_covers_survive_two_carts_reload_and_comment_sync(store):
    a, b = "https://img.example/a", "https://img.example/b"
    store.sync(snap("1", "a", a))
    store.sync(snap("2", "b", b))
    store.sync(snap("1", "a"))  # compact comment sync has no image URLs
    store = server.LiveCatalogStore()
    engine = types.SimpleNamespace(catalog=store, knowledge=types.SimpleNamespace(
        all_records=lambda: [], mapping_assignments=lambda: {}))
    assert server.ReplyEngine._product_image_sources(engine) == {"a": [a], "b": [b]}
    store.select("uid:2")
    assert store.product_image_sources() == {"a": [a], "b": [b]}


def test_signed_url_refresh_persisted_without_price_change(store):
    store.sync(snap("1", "a", "https://img.example/old"))
    store.sync(snap("1", "a", "https://img.example/new"))
    assert server.LiveCatalogStore().product_image_sources() == {"a": ["https://img.example/new"]}


def test_upgrade_keeps_last_raw_covers_when_another_cart_syncs(store):
    store.sync(snap("1", "a"))
    server.LIVE_RAW_CATALOG_PATH.write_text(json.dumps(snap("1", "a", "https://img.example/a")))
    store = server.LiveCatalogStore()
    store.sync(snap("2", "b", "https://img.example/b"))
    assert server.LiveCatalogStore().product_image_sources() == {
        "a": ["https://img.example/a"], "b": ["https://img.example/b"]}


def test_removed_cart_does_not_supply_covers(store):
    store.sync(snap("1", "a", "https://img.example/a"))
    store.sync(snap("2", "b", "https://img.example/b"))
    store.delete("uid:1")
    assert store.product_image_sources() == {"b": ["https://img.example/b"]}


def test_cover_parser_handles_thumbnail_fallback_and_malformed_entries():
    raw = {"products": [None, {}, {"product_id": "a", "cover": []},
        {"product_id": "b", "cover": {"url_list": [None, "bad", "https://img.example/b"],
                                     "thumb_url_list": "https://img.example/thumb"}}]}
    assert media_assets.product_cover_sources(raw) == {
        "b": ["https://img.example/b", "https://img.example/thumb"]}


def test_nested_prelive_covers(store):
    data = snap("1", "a")
    data["raw_api_data"] = {"products": [{"base": {"productId": "a",
        "cover": {"url_list": ["https://img.example/a"]}}}]}
    store.sync(data)
    assert server.LiveCatalogStore().product_image_sources() == {"a": ["https://img.example/a"]}


@pytest.fixture
def cache(tmp_path, monkeypatch):
    buffer = io.BytesIO()
    Image.new("RGB", (10, 10)).save(buffer, "PNG")
    calls = []

    def download(url):
        calls.append(url)
        if url.endswith("bad"):
            raise OSError("transient failure")
        return buffer.getvalue()

    monkeypatch.setattr(media_assets, "_download", download)
    return media_assets.ProductImageCache(tmp_path), calls


def test_missing_source_does_not_block_later_cover(cache):
    cache, calls = cache
    assert cache.fetch("a", []) is None
    assert cache.fetch("a", ["https://img.example/good"]).is_file()
    assert len(calls) == 1


def test_new_signed_url_bypasses_failure_cooldown(cache):
    cache, calls = cache
    assert cache.fetch("a", ["https://img.example/bad"]) is None
    assert cache.fetch("a", ["https://img.example/bad"]) is None
    assert len(calls) == 1
    assert cache.fetch("a", ["https://img.example/good"]).is_file()


def test_thumbnails_for_both_carts_work_after_raw_file_is_gone(store, cache):
    cache, calls = cache
    store.sync(snap("1", "a", "https://img.example/a"))
    store.sync(snap("2", "b", "https://img.example/b"))
    server.LIVE_RAW_CATALOG_PATH.unlink()
    store = server.LiveCatalogStore()

    def no_upload(pid):
        raise ValueError("no_uploaded_image")

    engine = types.SimpleNamespace(catalog=store, product_images=cache,
        workspace=types.SimpleNamespace(product_image_path=no_upload),
        knowledge=types.SimpleNamespace(all_records=lambda: [], mapping_assignments=lambda: {}))
    engine._product_image_sources = server.ReplyEngine._product_image_sources.__get__(engine)
    for pid in ("a", "b"):
        assert server.ReplyEngine.product_thumb_path(engine, pid).is_file()
    assert calls == ["https://img.example/a", "https://img.example/b"]


def test_same_url_retries_after_short_cooldown(cache, monkeypatch):
    cache, calls = cache
    clock = [100.0]
    monkeypatch.setattr(media_assets.time, "monotonic", lambda: clock[0])
    assert cache.fetch("a", ["https://img.example/bad"]) is None
    clock[0] += 29
    assert cache.fetch("a", ["https://img.example/bad"]) is None
    assert len(calls) == 1
    clock[0] += 2
    assert cache.fetch("a", ["https://img.example/bad"]) is None
    assert len(calls) == 2


def test_prefetch_accepts_new_source_after_failure(cache, monkeypatch):
    cache, calls = cache
    assert cache.fetch("a", ["https://img.example/bad"]) is None
    done = threading.Event()
    original = cache.fetch

    def fetch(*args):
        try:
            return original(*args)
        finally:
            done.set()

    monkeypatch.setattr(cache, "fetch", fetch)
    assert cache.prefetch({"a": ["https://img.example/good"]}) == 1
    assert done.wait(5)
    assert cache.cached("a").is_file()


@pytest.mark.parametrize("kind", ["product", "profile"])
def test_browser_must_not_cache_missing_thumb(kind, tmp_path):
    image = tmp_path / "image.jpg"
    image.write_bytes(b"test-image")
    ready = [False]

    def thumb(pid):
        if not ready[0]:
            raise ValueError("image_not_found")
        return image

    class ImageHandler(server.Handler):
        engine = types.SimpleNamespace(product_thumb_path=thumb, profile_thumb_path=thumb)

    http = server.LoopbackHTTPServer(("127.0.0.1", 0), ImageHandler)
    worker = threading.Thread(target=http.serve_forever, args=(0.05,), daemon=True)
    worker.start()
    url = f"http://127.0.0.1:{http.server_port}/api/{kind}/thumb/a?v=remote"
    try:
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(url, timeout=3)
        assert error.value.code == 404
        assert error.value.headers["Cache-Control"] == "no-store"
        error.value.close()
        ready[0] = True
        with urllib.request.urlopen(url, timeout=3) as response:
            assert response.read() == b"test-image"
            assert response.headers["Cache-Control"] == "private, max-age=86400"
    finally:
        http.shutdown()
        http.server_close()
        worker.join(3)
