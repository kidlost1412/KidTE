"""Quét lại giỏ do người dùng bấm phải lấy đủ sản phẩm TikTok trả về.

Lỗi 1.15.19: xoá 2 sản phẩm (còn 35) rồi bấm quét -> TikTok trả 37 nhưng app báo
"đã lưu 35" vì tombstone removed_products.json lọc mất 2 sản phẩm.
"""
import types

import product_blocklist
import server as server_module


def _engine(tmp_path, monkeypatch):
    monkeypatch.setattr(server_module, "LIVE_CATALOG_PATH", tmp_path / "live.json")
    monkeypatch.setattr(server_module, "LIVE_RAW_CATALOG_PATH", tmp_path / "raw.json")
    monkeypatch.setattr(product_blocklist, "PATH", tmp_path / "none.json")
    monkeypatch.setattr(product_blocklist, "REMOVED_PATH", tmp_path / "removed.json")
    product_blocklist._cache.clear()
    fake = types.SimpleNamespace(catalog=server_module.LiveCatalogStore(),
                                 workspace=types.SimpleNamespace(_touch=lambda *a: None))
    sync = lambda snap, **kw: server_module.ReplyEngine.sync_catalog(fake, snap, **kw)
    return fake, sync


def _tiktok(n=37):
    return {"phase": "prelive", "host": {"user_id": "u1"}, "products": [
        {"product_id": f"p{i}", "title": f"San pham {i}", "bag_index": i} for i in range(1, n + 1)]}


def test_user_rescan_restores_locally_removed_products(tmp_path, monkeypatch):
    fake, sync = _engine(tmp_path, monkeypatch)
    assert sync(_tiktok())["product_count"] == 37
    for pid in ("p3", "p9"):  # chủ shop xoá 2 sản phẩm trong Kid TE
        product_blocklist.set_removed({"product_id": pid, "title": pid})
    assert fake.catalog.snapshot["product_count"] == 35
    # Tự động đồng bộ: vẫn tôn trọng xoá cục bộ (giảm tải, không đổi hành vi cũ).
    auto = sync(_tiktok())
    assert auto["product_count"] == 35 and auto["received_count"] == 37
    # Người dùng bấm lấy lại: TikTok trả 37 -> danh mục 37.
    manual = sync(_tiktok(), user_initiated=True)
    assert manual["ok"] is True
    assert manual["product_count"] == 37 and manual["received_count"] == 37
    assert sorted(manual["restored_ids"]) == ["p3", "p9"]
    assert fake.catalog.snapshot["product_count"] == 37
    assert product_blocklist.removed_products() == {}


def test_user_rescan_skipped_by_active_live_reports_error(tmp_path, monkeypatch):
    _fake, sync = _engine(tmp_path, monkeypatch)
    live = _tiktok(37); live["room_id"] = "room1"
    sync(live)
    # Chọn ghi đè giỏ đang LIVE từ tab trước LIVE: không đè phiên LIVE, báo lỗi.
    result = sync(_tiktok(36), user_initiated=True, cart_target="uid:u1", confirmed=True)
    assert result["ok"] is False and result["skipped"] == "live_active" and result["error"]
    # "Giỏ mới" (1.15.26): không bị chặn, lưu sang giỏ mới, giỏ LIVE giữ nguyên.
    other = {"phase": "prelive", "products": [{"product_id": f"x{i}", "title": "x", "bag_index": i} for i in range(5)]}
    result = sync(other, user_initiated=True, cart_target="new")
    assert result["ok"] is True and result["cart_created"] is True
    assert _fake.catalog.snapshot["product_count"] == 37


def test_route_passes_user_initiated(tmp_path, monkeypatch):
    import routes_live
    seen = {}
    handler = types.SimpleNamespace(engine=types.SimpleNamespace(
        sync_catalog=lambda snap, user_initiated=False, **kw: seen.update(u=user_initiated, n=len(snap["products"]))))
    routes_live.post_route_endswith_catalog_sync(handler, "/api/catalog/sync",
                                                 {"live_context": _tiktok(), "user_initiated": True}, None)
    assert seen == {"u": True, "n": 37}


def _bulk(tmp_path, monkeypatch):
    fake, sync = _engine(tmp_path, monkeypatch)
    for name in ("_bulk_ids", "_trash_pool", "removed_listings", "trash_listings", "restore_listings", "purge_listings"):
        attr = getattr(server_module.ReplyEngine, name)
        setattr(fake, name, attr if name == "_bulk_ids" else attr.__get__(fake))
    sync(_tiktok(5))
    return fake, sync


def test_trash_restore_purge_batch(tmp_path, monkeypatch):
    import pytest
    fake, sync = _bulk(tmp_path, monkeypatch)
    out = fake.trash_listings({"product_ids": ["p1", "p2", "nope"]})
    assert sorted(out["removed"]) == ["p1", "p2"] and out["missing"] == ["nope"]
    assert fake.catalog.snapshot["product_count"] == 3
    trash = fake.removed_listings()
    assert {r["product_id"] for r in trash} == {"p1", "p2"}
    assert all(r["in_saved_catalog"] and r["bag_index"] for r in trash)
    assert fake.restore_listings({"product_ids": ["p1"]})["restored"] == ["p1"]
    assert fake.catalog.snapshot["product_count"] == 4
    # Xoá tất cả -> thùng rác; khôi phục tất cả.
    fake.trash_listings({"all": True})
    assert fake.catalog.snapshot["product_count"] == 0 and len(fake.removed_listings()) == 5
    fake.restore_listings({"all": True})
    assert fake.catalog.snapshot["product_count"] == 5
    # Xoá vĩnh viễn: khỏi danh mục đã lưu và khỏi thùng rác.
    fake.trash_listings({"product_ids": ["p4", "p5"]})
    assert sorted(fake.purge_listings({"all": True})["purged"]) == ["p4", "p5"]
    assert fake.removed_listings() == []
    assert [p["product_id"] for p in fake.catalog.raw_products()] == ["p1", "p2", "p3"]
    assert fake.catalog.snapshot["product_count"] == 3
    # Quét tay sau đó vẫn lấy lại nếu TikTok còn bán.
    assert sync(_tiktok(5), user_initiated=True)["product_count"] == 5
    with pytest.raises(ValueError):
        fake.trash_listings({"product_ids": []})
    with pytest.raises(ValueError):
        fake.purge_listings({"product_ids": ["p1"]})  # không nằm trong thùng rác


def test_purge_rolls_back_catalog_when_tombstone_write_fails(tmp_path, monkeypatch):
    import pytest
    fake, _sync = _bulk(tmp_path, monkeypatch)
    fake.trash_listings({"product_ids": ["p1"]})
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(product_blocklist, "set_removed_many", boom)
    with pytest.raises(OSError):
        fake.purge_listings({"product_ids": ["p1"]})
    assert len(fake.catalog.raw_products()) == 5
    assert "p1" in product_blocklist.removed_products()


def test_old_removed_file_shows_in_trash(tmp_path, monkeypatch):
    import json
    fake, _sync = _bulk(tmp_path, monkeypatch)
    (tmp_path / "removed.json").write_text(json.dumps({"products": {"p2": {"product_id": "p2", "title": "cu"}}}),
                                           encoding="utf-8")
    product_blocklist._cache.clear()
    rows = fake.removed_listings()
    assert rows[0]["product_id"] == "p2" and rows[0]["title"] == "cu" and rows[0]["in_saved_catalog"]
