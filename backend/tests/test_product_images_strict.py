"""Adversarial image regressions: isolated disk, mocked network, controlled workers.

These tests assert observable recovery and persisted source selection, rather than
implementation strings or private bookkeeping. Events order races without sleeps.
"""
import io
import json
import threading
import types

from PIL import Image
import pytest

import media_assets
import product_blocklist
import server


OLD = "https://images.example.test/shared?signature=expired"
NEW = "https://images.example.test/shared?signature=renewed"
OTHER = "https://images.example.test/other"


def snapshot(owner, products, sources=None):
    return {
        "host": {"user_id": owner},
        "products": [{"product_id": pid, "title": pid} for pid in products],
        "raw_api_data": {"products": [
            {"product_id": pid, "cover": {"url_list": urls}}
            for pid, urls in (sources or {}).items()
        ]} if sources is not None else {},
    }


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "LIVE_CATALOG_PATH", tmp_path / "catalog.json")
    monkeypatch.setattr(server, "LIVE_RAW_CATALOG_PATH", tmp_path / "raw.json")
    monkeypatch.setattr(product_blocklist, "REMOVED_PATH", tmp_path / "removed.json")
    monkeypatch.setattr(product_blocklist, "_cache", {})
    return server.LiveCatalogStore()


def engine_sources(catalog):
    engine = types.SimpleNamespace(catalog=catalog, knowledge=types.SimpleNamespace(
        all_records=lambda: [], mapping_assignments=lambda: {}))
    return server.ReplyEngine._product_image_sources(engine)


@pytest.fixture
def image_bytes():
    stream = io.BytesIO()
    Image.new("RGBA", (900, 450), (12, 24, 36, 128)).save(stream, "PNG")
    return stream.getvalue()


@pytest.fixture
def image_cache(tmp_path):
    return media_assets.ProductImageCache(tmp_path)


@pytest.fixture
def queued_workers(monkeypatch):
    """Execute prefetch workers explicitly, capturing errors without thread noise."""
    workers = []

    class Worker:
        def __init__(self, target, **kwargs):
            self.target = target
            workers.append(self)

        def start(self):
            pass

        def run(self):
            try:
                self.target()
            except Exception as error:
                self.error = error

    monkeypatch.setattr(media_assets.threading, "Thread", Worker)
    return workers


@pytest.mark.parametrize("payload", [None, [], "bad", 42, {}, {"products": None},
    {"products": {}}, {"products": [None, [], "bad", 42, {}]}])
def test_cover_parser_ignores_non_product_payloads(payload):
    assert media_assets.product_cover_sources(payload) == {}


@pytest.mark.parametrize("wrapper", [None, "base", "product"])
@pytest.mark.parametrize("id_field", ["product_id", "productId"])
def test_cover_parser_handles_all_supported_product_wrappers(wrapper, id_field):
    product = {id_field: "p", "cover": {"url_list": [OLD, OLD, None, "javascript:bad", NEW],
                                         "thumb_url_list": [NEW, OTHER]}}
    if wrapper:
        product = {wrapper: product}
    assert media_assets.product_cover_sources({"products": [product]}) == {
        "p": [OLD, NEW, OTHER]}


def test_partial_cover_sync_preserves_other_products_and_survives_restart(catalog):
    catalog.sync(snapshot("one", ["shared", "other"], {"shared": [OLD], "other": [OTHER]}))
    catalog.sync(snapshot("two", ["second"], {"second": [OTHER + "/second"]}))
    catalog.sync(snapshot("one", ["shared", "other"], {"shared": [NEW]}))
    catalog.sync(snapshot("two", ["second"]))
    server.LIVE_RAW_CATALOG_PATH.unlink()
    assert engine_sources(server.LiveCatalogStore()) == {
        "shared": [NEW], "other": [OTHER], "second": [OTHER + "/second"]}


def test_invalid_or_empty_snapshot_does_not_erase_existing_image_sources(catalog):
    catalog.sync(snapshot("one", ["shared"], {"shared": [OLD]}))
    for payload in (None, {}, snapshot("one", [], {})):
        catalog.sync(payload)
    assert server.LiveCatalogStore().product_image_sources() == {"shared": [OLD]}


def test_product_removed_by_complete_sync_stays_removed_after_restart(catalog):
    catalog.sync(snapshot("one", ["shared", "other"], {"shared": [OLD], "other": [OTHER]}))
    catalog.sync(snapshot("one", ["other"]))
    assert engine_sources(catalog) == {"other": [OTHER]}
    assert engine_sources(server.LiveCatalogStore()) == {"other": [OTHER]}


def test_deleted_last_raw_cart_cannot_reenter_engine_sources(catalog):
    catalog.sync(snapshot("one", ["other"], {"other": [OTHER]}))
    catalog.sync(snapshot("two", ["shared"], {"shared": [OLD]}))
    catalog.delete("uid:two")
    assert engine_sources(catalog) == {"other": [OTHER]}
    assert engine_sources(server.LiveCatalogStore()) == {"other": [OTHER]}


def test_latest_shared_product_source_wins_even_with_same_second_clock(catalog, monkeypatch):
    monkeypatch.setattr(server, "now_iso", lambda: "2026-10-21T10:00:00+07:00")
    catalog.sync(snapshot("one", ["shared"], {"shared": [OLD]}))
    catalog.sync(snapshot("two", ["shared"], {"shared": [NEW]}))
    assert catalog.product_image_sources()["shared"][0] == NEW
    assert server.LiveCatalogStore().product_image_sources()["shared"][0] == NEW


def test_unrelated_cover_refresh_does_not_make_old_shared_cover_newest(catalog, monkeypatch):
    monkeypatch.setattr(server, "now_iso", lambda: "2026-10-21T10:00:00+07:00")
    catalog.sync(snapshot("one", ["shared", "other"], {"shared": [OLD], "other": [OTHER]}))
    catalog.sync(snapshot("two", ["shared"], {"shared": [NEW]}))
    catalog.sync(snapshot("one", ["shared", "other"], {"other": [OTHER + "?signature=renewed"]}))
    assert catalog.product_image_sources()["shared"][0] == NEW
    assert server.LiveCatalogStore().product_image_sources()["shared"][0] == NEW


def test_legacy_upgrade_survives_another_cart_sync_and_raw_deletion(catalog):
    catalog.sync(snapshot("one", ["shared"]))
    server.LIVE_RAW_CATALOG_PATH.write_text(json.dumps({
        "cart_key": "uid:one", "raw_api_data": {"products": [
            {"product_id": "shared", "cover": {"url_list": [OLD]}}]}}), encoding="utf-8")
    migrated = server.LiveCatalogStore()
    migrated.sync(snapshot("two", ["other"], {"other": [OTHER]}))
    server.LIVE_RAW_CATALOG_PATH.unlink()
    assert engine_sources(server.LiveCatalogStore()) == {"shared": [OLD], "other": [OTHER]}


def test_stale_legacy_raw_does_not_replace_persisted_new_signed_url(catalog):
    catalog.sync(snapshot("one", ["shared"], {"shared": [NEW]}))
    server.LIVE_RAW_CATALOG_PATH.write_text(json.dumps({"raw_api_data": {"products": [
        {"product_id": "shared", "cover": {"url_list": [OLD]}}]}}), encoding="utf-8")
    assert engine_sources(server.LiveCatalogStore()) == {"shared": [NEW]}


@pytest.mark.parametrize("raw_text", ["{", "null", "[]", '"bad"'])
def test_legacy_raw_malformed_does_not_break_persisted_images(catalog, raw_text):
    catalog.sync(snapshot("one", ["shared"], {"shared": [NEW]}))
    server.LIVE_RAW_CATALOG_PATH.write_text(raw_text, encoding="utf-8")
    assert engine_sources(server.LiveCatalogStore()) == {"shared": [NEW]}


def test_migration_persists_before_noop_other_cart_overwrites_shared_raw(catalog):
    catalog.sync(snapshot("one", ["shared"]))
    catalog.sync(snapshot("two", ["other"]))
    server.LIVE_RAW_CATALOG_PATH.write_text(json.dumps({"cart_key": "uid:one",
        "raw_api_data": {"products": [{"product_id": "shared", "cover": {"url_list": [OLD]}}]}}),
        encoding="utf-8")
    migrated = server.LiveCatalogStore()
    assert migrated.product_image_sources() == {"shared": [OLD]}
    unchanged = snapshot("two", ["other"])
    unchanged["raw_api_data"] = {"products": [{"product_id": "other", "cover": None}]}
    migrated.sync(unchanged)
    assert server.LiveCatalogStore().product_image_sources() == {"shared": [OLD]}


def test_legacy_single_cart_file_migrates_its_raw_image_sources(catalog):
    legacy = snapshot("one", ["shared"])
    legacy.update(owner_key="uid:one", synced_at="2026-10-20T10:00:00+07:00")
    server.LIVE_CATALOG_PATH.write_text(json.dumps(legacy), encoding="utf-8")
    server.LIVE_RAW_CATALOG_PATH.write_text(json.dumps({"raw_api_data": {"products": [
        {"product_id": "shared", "cover": {"url_list": [OLD]}}]}}), encoding="utf-8")
    migrated = server.LiveCatalogStore()
    server.LIVE_RAW_CATALOG_PATH.unlink()
    assert engine_sources(server.LiveCatalogStore()) == {"shared": [OLD]}
    assert migrated.carts_state()["selected"] == "uid:one"


def test_fallback_skips_bad_network_and_invalid_image_then_writes_real_jpeg(
        image_cache, image_bytes, monkeypatch):
    calls = []

    def download(url):
        calls.append(url)
        if url == OLD:
            raise OSError("expired signature")
        return b"<html>403</html>" if url == NEW else image_bytes

    monkeypatch.setattr(media_assets, "_download", download)
    path = image_cache.fetch("shared", [None, "file:///bad", OLD, OLD, NEW, OTHER])
    assert calls == [OLD, NEW, OTHER]
    with Image.open(path) as image:
        assert image.format == "JPEG"
        assert image.size == (320, 160)
    assert not list(path.parent.glob("*.tmp"))
    monkeypatch.setattr(media_assets, "_download", lambda url: pytest.fail("cache must survive restart"))
    restarted = media_assets.ProductImageCache(image_cache.root.parent)
    assert restarted.fetch("shared", [NEW]) == path


def test_negative_cache_retries_exactly_at_boundary_and_recovers(
        image_cache, image_bytes, monkeypatch):
    clock = [100.0]
    attempts = []
    monkeypatch.setattr(media_assets.time, "monotonic", lambda: clock[0])

    def download(url):
        attempts.append(url)
        if len(attempts) == 1:
            raise OSError("temporary outage")
        return image_bytes

    monkeypatch.setattr(media_assets, "_download", download)
    assert image_cache.fetch("shared", [OLD]) is None
    clock[0] += image_cache.RETRY_SECONDS - 0.001
    assert image_cache.fetch("shared", [OLD]) is None
    assert len(attempts) == 1
    clock[0] += 0.001
    assert image_cache.fetch("shared", [OLD]).is_file()
    assert len(attempts) == 2


def test_adding_new_fallback_bypasses_failed_source_cooldown(
        image_cache, image_bytes, monkeypatch):
    attempts = []

    def download(url):
        attempts.append(url)
        if url == OLD:
            raise OSError("expired")
        return image_bytes

    monkeypatch.setattr(media_assets, "_download", download)
    assert image_cache.fetch("shared", [OLD]) is None
    assert image_cache.fetch("shared", [OLD, NEW]).is_file()
    assert attempts == [OLD, OLD, NEW]


def test_failed_product_does_not_suppress_unrelated_product(
        image_cache, image_bytes, monkeypatch):
    monkeypatch.setattr(media_assets, "_download", lambda url: (
        b"bad image" if url == OLD else image_bytes))
    assert image_cache.fetch("shared", [OLD]) is None
    assert image_cache.fetch("other", [OTHER]).is_file()


def test_prefetch_deduplicates_pending_and_releases_after_network_failure(
        image_cache, image_bytes, queued_workers, monkeypatch):
    calls = []

    def download(url):
        calls.append(url)
        return b"invalid image" if url == OLD else image_bytes

    monkeypatch.setattr(media_assets, "_download", download)
    assert image_cache.prefetch({"shared": [OLD], "other": [OTHER]}) == 2
    assert image_cache.prefetch({"shared": [OLD], "other": [OTHER]}) == 0
    assert len(queued_workers) == 1
    queued_workers[0].run()
    assert calls == [OLD, OTHER]
    assert image_cache.cached("other").is_file()
    assert image_cache.prefetch({"shared": [NEW]}) == 1
    queued_workers[1].run()
    assert image_cache.cached("shared").is_file()


def test_prefetch_local_write_failure_does_not_strand_rest_of_batch(
        image_cache, image_bytes, queued_workers, monkeypatch):
    calls = []
    original_write = media_assets._atomic_bytes

    def download(url):
        calls.append(url)
        return image_bytes

    def write(path, data):
        if path == image_cache._path("shared"):
            raise PermissionError("simulated Windows antivirus lock")
        return original_write(path, data)

    monkeypatch.setattr(media_assets, "_download", download)
    monkeypatch.setattr(media_assets, "_atomic_bytes", write)
    assert image_cache.prefetch({"shared": [NEW], "other": [OTHER]}) == 2
    queued_workers[0].run()
    assert calls == [NEW, OTHER], "one failed write must not abort unrelated image downloads"
    assert image_cache.cached("other").is_file()
    monkeypatch.setattr(media_assets, "_atomic_bytes", original_write)
    assert image_cache.prefetch({"shared": [NEW]}) == 1
    queued_workers[1].run()
    assert image_cache.cached("shared").is_file()


def test_refreshed_prefetch_source_is_not_lost_while_old_source_is_pending(
        image_cache, image_bytes, queued_workers, monkeypatch):
    calls = []

    def download(url):
        calls.append(url)
        if url == OLD:
            raise OSError("expired signature")
        return image_bytes

    monkeypatch.setattr(media_assets, "_download", download)
    assert image_cache.prefetch({"shared": [OLD]}) == 1
    image_cache.prefetch({"shared": [NEW]})
    # Run every queued worker, including any followup the implementation schedules.
    index = 0
    while index < len(queued_workers):
        assert index < 4, "a bounded source refresh must not requeue forever"
        queued_workers[index].run()
        index += 1
    assert image_cache.cached("shared") is not None, "fresh signed URL was lost behind old in-flight URL"
    assert NEW in calls


def test_old_inflight_failure_cannot_poison_successful_direct_refresh(
        image_cache, image_bytes, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    results, errors = [], []

    def download(url):
        if url == OLD:
            entered.set()
            assert release.wait(5), "test did not release the stale request"
            raise OSError("old signed URL expired")
        return image_bytes

    def stale_request():
        try:
            results.append(image_cache.fetch("shared", [OLD]))
        except Exception as error:
            errors.append(error)

    monkeypatch.setattr(media_assets, "_download", download)
    worker = threading.Thread(target=stale_request)
    worker.start()
    try:
        assert entered.wait(5)
        good = image_cache.fetch("shared", [NEW])
        assert good.is_file()
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert errors == []
    assert results == [good], "failed stale request must see the concurrently downloaded image"
    assert image_cache.fetch("shared", [NEW]) == good
    with Image.open(good) as image:
        image.verify()


def test_overlapping_direct_fetches_leave_valid_atomic_image_and_no_temps(
        image_cache, image_bytes, monkeypatch):
    barrier = threading.Barrier(2)
    results, errors = [], []

    def download(url):
        barrier.wait(timeout=5)
        return image_bytes

    def request():
        try:
            results.append(image_cache.fetch("shared", [NEW]))
        except Exception as error:
            errors.append(error)

    monkeypatch.setattr(media_assets, "_download", download)
    workers = [threading.Thread(target=request) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(5)
    assert all(not worker.is_alive() for worker in workers)
    assert errors == []
    assert len(results) == 2 and results[0] == results[1]
    with Image.open(results[0]) as image:
        image.verify()
    assert not list(image_cache.root.glob("*.tmp"))


def test_prefetch_thread_start_failure_releases_all_queued_products(
        image_cache, image_bytes, monkeypatch):
    real_thread = threading.Thread

    class CannotStart:
        def __init__(self, **kwargs):
            pass

        def start(self):
            raise RuntimeError("cannot start new thread")

    monkeypatch.setattr(media_assets.threading, "Thread", CannotStart)
    with pytest.raises(RuntimeError, match="cannot start"):
        image_cache.prefetch({"shared": [NEW], "other": [OTHER]})
    monkeypatch.setattr(media_assets.threading, "Thread", real_thread)
    monkeypatch.setattr(media_assets, "_download", lambda url: image_bytes)
    done = threading.Event()
    original_fetch = image_cache.fetch

    def fetch(pid, urls):
        try:
            return original_fetch(pid, urls)
        finally:
            if pid == "other":
                done.set()

    monkeypatch.setattr(image_cache, "fetch", fetch)
    assert image_cache.prefetch({"shared": [NEW], "other": [OTHER]}) == 2
    assert done.wait(5)
    assert image_cache.cached("shared").is_file()
    assert image_cache.cached("other").is_file()


def test_invalid_prefetch_product_id_cannot_strand_earlier_valid_product(
        image_cache, image_bytes, queued_workers, monkeypatch):
    monkeypatch.setattr(media_assets, "_download", lambda url: image_bytes)
    try:
        image_cache.prefetch({"shared": [NEW], "???": [OTHER]})
    except ValueError:
        # Rejecting the batch is allowed, but its valid entry must remain retryable.
        pass
    image_cache.prefetch({"shared": [NEW]})
    for worker in queued_workers:
        worker.run()
    assert image_cache.cached("shared") is not None, "invalid later ID stranded the valid pending image"
    with Image.open(image_cache.cached("shared")) as image:
        image.verify()


def test_prefetch_keeps_refresh_arriving_during_old_network_request(
        image_cache, image_bytes, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    done = threading.Event()
    calls = []

    def download(url):
        calls.append(url)
        if url == OLD:
            entered.set()
            assert release.wait(5), "stale network request was never released"
            raise OSError("expired signature")
        return image_bytes

    original_fetch = image_cache.fetch

    def fetch(pid, urls):
        result = original_fetch(pid, urls)
        if list(urls) == [NEW]:
            done.set()
        return result

    monkeypatch.setattr(media_assets, "_download", download)
    monkeypatch.setattr(image_cache, "fetch", fetch)
    assert image_cache.prefetch({"shared": [OLD]}) == 1
    try:
        assert entered.wait(5)
        image_cache.prefetch({"shared": [NEW]})
    finally:
        release.set()
    assert done.wait(5), "fresh source must run without needing another UI request"
    assert calls == [OLD, NEW]
    assert image_cache.cached("shared").is_file()
