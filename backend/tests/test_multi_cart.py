"""Giỏ hàng riêng theo tài khoản LIVE (live_carts.json)."""
import json
import types

import pytest

import product_blocklist
import server as server_module


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(server_module, "LIVE_CATALOG_PATH", tmp_path / "live.json")
    monkeypatch.setattr(server_module, "LIVE_RAW_CATALOG_PATH", tmp_path / "raw.json")
    monkeypatch.setattr(product_blocklist, "PATH", tmp_path / "none.json")
    monkeypatch.setattr(product_blocklist, "REMOVED_PATH", tmp_path / "removed.json")
    product_blocklist._cache.clear()
    return tmp_path


def _snap(uid, ids, room="", **host):
    return {"room_id": room, "phase": "live" if room else "prelive",
            "host": {"user_id": uid, **host} if uid else None,
            "products": [{"product_id": i, "title": f"SP {i}", "bag_index": n + 1} for n, i in enumerate(ids)]}


def _engine():
    fake = types.SimpleNamespace(catalog=server_module.LiveCatalogStore(),
                                 workspace=types.SimpleNamespace(_touch=lambda *a: None))
    for name in ("_bulk_ids", "_trash_pool", "removed_listings", "trash_listings", "restore_listings",
                 "purge_listings"):
        attr = getattr(server_module.ReplyEngine, name)
        setattr(fake, name, attr if name == "_bulk_ids" else attr.__get__(fake))
    fake.sync = lambda snap, **kw: server_module.ReplyEngine.sync_catalog(fake, snap, **kw)
    return fake


def _ids(snapshot):
    return [p["product_id"] for p in snapshot["products"]]


def test_scan_account_a_never_touches_account_b(env):
    store = server_module.LiveCatalogStore()
    store.sync(_snap("111", ["a1", "a2"], unique_id="shopA"))
    store.sync(_snap("222", ["b1"], unique_id="shopB"))
    store.sync(_snap("111", ["a3"]))  # quét lại A: chỉ giỏ A đổi
    assert _ids(store.cart_snapshot("uid:111")) == ["a3"]
    assert _ids(store.cart_snapshot("uid:222")) == ["b1"]
    state = store.carts_state()
    names = {c["key"]: c["name"] for c in state["carts"]}
    assert names == {"uid:111": "@shopA", "uid:222": "@shopB"}
    # Ghi xuống đĩa, mở lại vẫn đủ 2 giỏ.
    again = server_module.LiveCatalogStore()
    assert _ids(again.cart_snapshot("uid:222")) == ["b1"]


def test_same_account_matched_by_sec_uid_when_user_id_missing(env):
    store = server_module.LiveCatalogStore()
    store.sync({**_snap("111", ["a1"]), "host": {"user_id": "111", "sec_uid": "SEC1"}})
    out = store.sync({**_snap("", ["a9"]), "host": {"sec_uid": "SEC1"}})
    assert out["cart_key"] == "uid:111"
    assert len(store.carts_state()["carts"]) == 1


def test_legacy_payload_without_host_goes_to_active_cart(env):
    store = server_module.LiveCatalogStore()
    store.sync(_snap("111", ["a1"]))
    store.sync(_snap("222", ["b1"]))
    store.select("uid:222")
    out = store.sync(_snap("", ["b2"]))
    assert out["cart_key"] == "uid:222"
    assert _ids(store.cart_snapshot("uid:111")) == ["a1"]
    assert _ids(store.snapshot) == ["b2"]


def test_live_account_wins_over_ui_selection(env):
    store = server_module.LiveCatalogStore()
    store.sync(_snap("111", ["a1"]))
    store.sync(_snap("222", ["b1"]))
    store.select("uid:111")
    assert _ids(store.snapshot) == ["a1"]
    store.sync(_snap("222", ["b1", "b2"], room="room9"))  # B lên LIVE
    assert store.active_key() == "uid:222" and _ids(store.snapshot) == ["b1", "b2"]
    with pytest.raises(ValueError):
        store.delete("uid:222")  # không xoá giỏ đang LIVE
    # LIVE cũ hơn 10 phút: quay về giỏ đang chọn.
    store._carts["uid:222"]["snapshot"]["synced_at"] = "2000-01-01T00:00:00+07:00"
    assert store.active_key() == "uid:111"


def test_migrate_legacy_catalog_with_owner(env):
    legacy = {"schema_version": 1, "synced_at": "2026-10-01T10:00:00+07:00", "room_id": "",
              "owner_key": "uid:777", "pin_id": "", "products": [{"product_id": "p1"}, {"product_id": "p2"}]}
    (env / "live.json").write_text(json.dumps(legacy), encoding="utf-8")
    store = server_module.LiveCatalogStore()
    assert store.active_key() == "uid:777"
    assert _ids(store.snapshot) == ["p1", "p2"]
    saved = json.loads((env / "live_carts.json").read_text(encoding="utf-8"))
    assert saved["carts"]["uid:777"]["snapshot"]["products"] == legacy["products"]
    assert saved["selected"] == "uid:777"
    # Lần quét sau của đúng tài khoản đó cập nhật giỏ di chuyển, không tạo giỏ mới.
    store.sync(_snap("777", ["p3"], unique_id="shop7"))
    assert [c["key"] for c in store.carts_state()["carts"]] == ["uid:777"]


def test_migrate_legacy_catalog_without_owner_becomes_default(env):
    (env / "live.json").write_text(json.dumps({"products": [{"product_id": "p1"}]}), encoding="utf-8")
    store = server_module.LiveCatalogStore()
    state = store.carts_state()
    assert [(c["key"], c["name"]) for c in state["carts"]] == [("default", "Mặc định")]
    assert _ids(store.snapshot) == ["p1"]
    # Tài khoản mới quét -> giỏ riêng, giỏ Mặc định giữ nguyên.
    store.sync(_snap("999", ["n1"]))
    assert _ids(store.cart_snapshot("default")) == ["p1"]


def test_corrupt_carts_file_is_kept_aside(env):
    (env / "live_carts.json").write_text("{hong", encoding="utf-8")
    (env / "live.json").write_text(json.dumps({"products": [{"product_id": "p1"}]}), encoding="utf-8")
    store = server_module.LiveCatalogStore()
    assert (env / "live_carts.json.corrupt").read_text(encoding="utf-8") == "{hong"
    assert _ids(store.snapshot) == ["p1"]


def test_rename_and_delete_keep_backup(env):
    store = server_module.LiveCatalogStore()
    store.sync(_snap("111", ["a1"], unique_id="shopA"))
    store.sync(_snap("222", ["b1"]))
    state = store.rename("uid:111", "Shop chính")
    assert {c["key"]: c["name"] for c in state["carts"]}["uid:111"] == "Shop chính"
    assert store.rename("uid:111", "")["carts"][0]["name"] == "@shopA"  # tên rỗng = tên tự động
    store.select("uid:222")
    state = store.delete("uid:222")
    assert [c["key"] for c in state["carts"]] == ["uid:111"] and state["selected"] == "uid:111"
    deleted = json.loads((env / "live_carts_deleted.json").read_text(encoding="utf-8"))
    assert deleted[0]["key"] == "uid:222" and deleted[0]["cart"]["snapshot"]["products"][0]["product_id"] == "b1"
    with pytest.raises(ValueError):
        store.select("uid:222")


def test_trash_and_purge_are_per_cart(env):
    fake = _engine()
    fake.sync(_snap("111", ["a1", "shared"]))
    fake.sync(_snap("222", ["b1", "shared"]))
    fake.catalog.select("uid:111")
    fake.trash_listings({"product_ids": ["a1", "shared"]})
    fake.catalog.select("uid:222")
    # Thùng rác giỏ B chỉ thấy sản phẩm có trong giỏ B.
    assert [r["product_id"] for r in fake.removed_listings()] == ["shared"]
    fake.purge_listings({"product_ids": ["shared"]})
    assert _ids(fake.catalog.cart_snapshot("uid:222")) == ["b1"]
    # Giỏ A vẫn còn "shared" (đang trong thùng rác của A), tombstone giữ nguyên.
    assert "shared" in [p["product_id"] for p in fake.catalog.raw_products("uid:111")]
    fake.catalog.select("uid:111")
    assert sorted(r["product_id"] for r in fake.removed_listings()) == ["a1", "shared"]
    fake.purge_listings({"all": True})
    assert fake.catalog.raw_products("uid:111") == []
    assert product_blocklist.removed_products() == {}


def test_user_scan_selects_scanned_cart(env):
    fake = _engine()
    fake.sync(_snap("111", ["a1"]))
    out = fake.sync(_snap("222", ["b1"]), user_initiated=True)
    assert out["cart_key"] == "uid:222" and out["ok"] is True
    assert fake.catalog.active_key() == "uid:222"
    # Đồng bộ tự động không đổi giỏ đang chọn.
    fake.sync(_snap("111", ["a2"]))
    assert fake.catalog.active_key() == "uid:222"


def test_video_job_catalog_uses_its_own_cart(env, tmp_path):
    import video_analysis
    store = server_module.LiveCatalogStore()
    store.sync(_snap("111", ["a1"]))
    store.sync(_snap("222", ["b1"]))
    store.select("uid:111")

    def provider(cart=""):
        snap = store.cart_snapshot(cart)
        return {**snap, "products": snap["products"], "cart_key": cart or store.active_key()}

    manager = video_analysis.VideoAnalysisManager(root=tmp_path / "videos", catalog_provider=provider)
    assert [p["product_id"] for p in manager._cart_catalog("uid:222")["products"]] == ["b1"]
    # Giỏ đã xoá / không có -> giỏ đang hoạt động, không ném lỗi.
    assert [p["product_id"] for p in manager._cart_catalog("uid:999")["products"]] == ["a1"]
    # Provider kiểu cũ (không nhận tham số) vẫn chạy.
    legacy = video_analysis.VideoAnalysisManager(root=tmp_path / "v2", catalog_provider=lambda: {"products": []})
    assert legacy._cart_catalog("uid:222") == {"products": []}
