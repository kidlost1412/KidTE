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
    return {"phase": "prelive", "products": [
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
    live = _tiktok(5); live["room_id"] = "room1"
    sync(live)
    result = sync(_tiktok(), user_initiated=True)
    assert result["ok"] is False and result["skipped"] == "live_active" and result["error"]


def test_route_passes_user_initiated(tmp_path, monkeypatch):
    import routes_live
    seen = {}
    handler = types.SimpleNamespace(engine=types.SimpleNamespace(
        sync_catalog=lambda snap, user_initiated=False: seen.update(u=user_initiated, n=len(snap["products"]))))
    routes_live.post_route_endswith_catalog_sync(handler, "/api/catalog/sync",
                                                 {"live_context": _tiktok(), "user_initiated": True}, None)
    assert seen == {"u": True, "n": 37}
