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


# ---- 3.11.11: hai trình duyệt, hai tài khoản (kịch bản chủ shop 20/10) ----
def _ids_n(prefix, n):
    return [f"{prefix}{i:02d}" for i in range(1, n + 1)]


def test_corrupt_history_file_is_kept_aside(env):
    (env / "live_carts_history.json").write_text("{hong", encoding="utf-8")
    store = server_module.LiveCatalogStore()
    assert store.cart_history("x") == []
    assert (env / "live_carts_history.json.corrupt").read_text(encoding="utf-8") == "{hong"


def test_live_sync_without_host_stays_in_its_room_cart(env):
    store = server_module.LiveCatalogStore()
    store.sync(_snap("111", ["a1"], room="7300"))
    store.sync(_snap("", ["p1"]))  # giỏ khác (không rõ chủ)
    # live_context của bình luận không có host nhưng cùng room: đúng giỏ của room đó.
    out = store.sync(_snap("", ["a1", "a9"], room="7300"))
    assert out["cart_key"] == "uid:111"
    assert _ids(store.cart_snapshot("uid:111")) == ["a1", "a9"]


# ---- 1.15.26 / tiện ích 3.11.14: quét tay "lấy ở đâu lưu ở đó", không so khớp ----
def _scan(engine, snap, target="new", name="", confirmed=False):
    return engine.sync(snap, user_initiated=True, cart_target=target, cart_name=name, confirmed=confirmed)


def test_payload_without_host_auto_sync_never_overwrites(env):
    store = server_module.LiveCatalogStore()
    store.sync(_snap("222", ["b1", "b2"]))
    out = store.sync(_snap("", ["b1", "b2", "b3"]))  # tự động, không host, không room
    assert out["skipped"] == "no_owner"
    assert _ids(store.cart_snapshot("uid:222")) == ["b1", "b2"]
    assert [c["key"] for c in store.carts_state()["carts"]] == ["uid:222"]  # không giỏ rác


def test_auto_sync_without_host_or_room_on_empty_store_creates_nothing(env):
    store = server_module.LiveCatalogStore()
    assert store.sync(_snap("", ["x"]))["skipped"] == "no_owner"
    assert store.carts_state()["carts"] == []


def test_auto_sync_without_host_uses_room_cart(env):
    store = server_module.LiveCatalogStore()
    out = store.sync(_snap("", ["r1"], room="7300"))
    assert out["cart_key"] == "room:7300"
    assert store.sync(_snap("", ["r1", "r2"], room="7300"))["cart_key"] == "room:7300"
    assert len(store.carts_state()["carts"]) == 1


def test_auto_live_sync_goes_to_account_cart_not_named_manual_cart(env):
    engine = _engine()
    manual = _scan(engine, _snap("111", ["a1"], unique_id="shopA"), name="Giỏ chiều")
    assert manual["cart_key"].startswith("scan:")
    out = engine.sync(_snap("111", ["a1", "a2"], room="7300", unique_id="shopA"))
    assert out["cart_key"] == "uid:111"
    assert _ids(engine.catalog.cart_snapshot(manual["cart_key"])) == ["a1"]
    # Đồng bộ lại nhiều lần: vẫn đúng một giỏ của tài khoản, không giỏ mới.
    engine.sync(_snap("111", ["a1", "a3"], room="7300"))
    assert len(engine.catalog.carts_state()["carts"]) == 2


def test_selected_cart_wins_for_live_else_live_account(env):
    store = server_module.LiveCatalogStore()
    store.sync(_snap("111", ["a1"]))
    store.sync(_snap("222", ["b1"]))
    assert store.carts_state()["selected_explicit"] is False
    store.sync(_snap("222", ["b1", "b2"], room="room9"))  # B lên LIVE
    assert store.active_key() == "uid:222"  # chưa chọn: giỏ của tài khoản LIVE
    store.select("uid:111")  # chủ shop chọn giỏ cho buổi LIVE
    assert store.active_key() == "uid:111" and _ids(store.snapshot) == ["a1"]
    state = store.carts_state()
    assert state["active_name"] and state["selected_explicit"] is True
    assert server_module.LiveCatalogStore().active_key() == "uid:111"  # bền qua khởi động lại
    store.select("")  # bỏ chọn -> theo tài khoản LIVE
    assert store.active_key() == "uid:222"
    with pytest.raises(ValueError):
        store.delete("uid:222")  # không xoá giỏ đang LIVE


def test_manual_scan_two_tabs_make_two_named_carts(env):
    engine = _engine()
    first = _scan(engine, _snap("", _ids_n("p", 19)), name="Tab A")
    second = _scan(engine, _snap("", _ids_n("p", 19) + _ids_n("q", 20)), name="Tab B")
    assert first["ok"] and second["ok"] and first["cart_key"] != second["cart_key"]
    assert first["cart_name"] == "Tab A" and second["cart_name"] == "Tab B"
    assert first["cart_created"] and second["cart_created"]
    assert len(engine.catalog.cart_snapshot(first["cart_key"])["products"]) == 19
    assert len(engine.catalog.cart_snapshot(second["cart_key"])["products"]) == 39
    # Cùng sản phẩm, "Giỏ mới": vẫn tạo giỏ mới, trùng tên thêm " (2)".
    third = _scan(engine, _snap("", _ids_n("p", 19)), name="Tab A")
    assert third["cart_name"] == "Tab A (2)" and third["cart_key"] not in (first["cart_key"], second["cart_key"])
    # Quét xong chỉ LƯU: giỏ đang dùng vẫn là giỏ đầu tiên (giỏ duy nhất lúc đó).
    assert engine.catalog.active_key() == first["cart_key"]
    again = server_module.LiveCatalogStore()
    assert {c["name"] for c in again.carts_state()["carts"]} == {"Tab A", "Tab B", "Tab A (2)"}


def test_manual_scan_default_name(env):
    engine = _engine()
    out = _scan(engine, _snap("", ["x"]))
    assert out["cart_name"].startswith("Giỏ ") and out["cart_key"].startswith("scan:")


def test_old_extension_payload_creates_new_cart(env):
    engine = _engine()
    engine.sync(_snap("111", ["a1"]))
    out = engine.sync(_snap("111", ["a9"]), user_initiated=True)  # tiện ích cũ: không cart_target
    assert out["cart_created"] is True and out["cart_key"].startswith("scan:")
    assert _ids(engine.catalog.cart_snapshot("uid:111")) == ["a1"]


def test_route_old_payload_maps_to_new_cart(env):
    import routes_live
    seen = {}
    fake = types.SimpleNamespace(engine=types.SimpleNamespace(
        sync_catalog=lambda ctx, **kw: seen.update(kw) or {"ok": True}))
    routes_live.post_route_endswith_catalog_sync.__globals__.setdefault("clean", server_module.clean)
    routes_live.post_route_endswith_catalog_sync(fake, "/api/catalog/sync",
                                                 {"live_context": {}, "user_initiated": True}, {})
    assert seen["cart_target"] == "new" and seen["confirmed"] is False


def test_manual_scan_during_live_does_not_switch_ai_cart(env):
    engine = _engine()
    engine.sync(_snap("111", ["a1"], room="7300"))
    out = _scan(engine, _snap("", ["b1"]), name="Tài khoản 2")
    assert out["ok"] and engine.catalog.active_key() == "uid:111"


def test_manual_overwrite_needs_confirm_keeps_history_and_restores(env):
    engine = _engine()
    base = _scan(engine, _snap("", ["a1", "a2"]), name="Giỏ A")
    key = base["cart_key"]
    ask = _scan(engine, _snap("", ["a3"]), target=key)
    assert ask["ok"] is False and ask["needs_confirm"] and ask["old_count"] == 2 and ask["new_count"] == 1
    assert _ids(engine.catalog.cart_snapshot(key)) == ["a1", "a2"]
    out = _scan(engine, _snap("", ["a3"]), target=key, confirmed=True)
    assert out["ok"] and out["cart_key"] == key and out["cart_name"] == "Giỏ A" and "cart_created" not in out
    assert _ids(engine.catalog.cart_snapshot(key)) == ["a3"]
    hist = engine.catalog.cart_history(key)
    assert len(hist) == 1 and hist[0]["reason"] == "manual_scan" and hist[0]["product_count"] == 2
    engine.catalog.restore_previous(key)
    assert _ids(engine.catalog.cart_snapshot(key)) == ["a1", "a2"]
    again = server_module.LiveCatalogStore()  # lịch sử bền; bấm lại = hoàn tác
    again.restore_previous(key)
    assert _ids(again.cart_snapshot(key)) == ["a3"]
    with pytest.raises(ValueError, match="cart_not_found"):
        _scan(engine, _snap("", ["z"]), target="uid:999", confirmed=True)


def test_manual_overwrite_of_live_cart_is_blocked(env):
    engine = _engine()
    engine.sync(_snap("111", ["a1"], room="7300", unique_id="shop_mot"))
    out = _scan(engine, _snap("", ["x"]), target="uid:111", confirmed=True)
    assert out["ok"] is False and out["skipped"] == "live_active" and "@shop_mot" in out["error"]
    assert _ids(engine.catalog.cart_snapshot("uid:111")) == ["a1"]


def test_restore_previous_errors(env):
    engine = _engine()
    engine.sync(_snap("111", ["a1"], room="7300"))
    engine.sync(_snap("222", ["b1"]))
    with pytest.raises(ValueError, match="cart_history_empty"):
        engine.catalog.restore_previous("uid:222")
    engine.catalog._history["uid:111"] = [{"saved_at": "x", "snapshot": {"products": [{"product_id": "o"}]}}]
    with pytest.raises(ValueError, match="cart_is_live"):
        engine.catalog.restore_previous("uid:111")


def test_history_keeps_last_ten(env):
    engine = _engine()
    key = _scan(engine, _snap("", ["v0"]), name="G")["cart_key"]
    for n in range(1, 13):
        _scan(engine, _snap("", [f"v{n}"]), target=key, confirmed=True)
    assert len(engine.catalog.cart_history(key)) == 10
    data = json.loads((env / "live_carts_history.json").read_text(encoding="utf-8"))
    assert [e["snapshot"]["products"][0]["product_id"] for e in data["carts"][key]] == \
        [f"v{n}" for n in range(11, 1, -1)]


def test_system_route_restore(env):
    import engine_system
    engine = _engine()
    key = _scan(engine, _snap("", ["a1"]), name="G")["cart_key"]
    _scan(engine, _snap("", ["a2"]), target=key, confirmed=True)
    fake = types.SimpleNamespace(catalog=engine.catalog, workspace=types.SimpleNamespace(_touch=lambda *a: None))
    owner = next(c for c in vars(engine_system).values()
                 if isinstance(c, type) and "system_post" in vars(c))
    state = owner.system_post(fake, "carts/restore", {"key": key})
    assert state["ok"] is True and _ids(engine.catalog.cart_snapshot(key)) == ["a1"]


def test_ai_reads_selected_cart(env):
    """AI/tự ghim đọc catalog.snapshot: phải là giỏ chủ shop chọn, kể cả khi đang LIVE."""
    engine = _engine()
    named = _scan(engine, _snap("", ["n1", "n2"]), name="Giỏ tối nay")["cart_key"]
    synced = engine.catalog.sync({**_snap("111", ["a1"], room="7300"), "pin_id": "a1"})
    assert engine.catalog.active_key() == "uid:111"  # quét xong không tự chọn: theo tài khoản LIVE
    assert engine.catalog.ai_catalog(synced) is synced  # không có giỏ ưu tiên: giữ giỏ vừa đồng bộ
    engine.catalog.select(named)
    assert _ids(engine.catalog.snapshot) == ["n1", "n2"]
    view = engine.catalog.ai_catalog(synced)  # đường AI của handle()
    assert _ids(view) == ["n1", "n2"] and view["room_id"] == "7300" and view["pin_id"] == ""
    assert view["cart_key"] == named and view["cart_source"] == "selected"
    engine.catalog.select("")
    assert _ids(engine.catalog.snapshot) == ["a1"]


def test_manual_scan_name_defaults_to_account(env):
    engine = _engine()
    out = _scan(engine, _snap("111", ["a1"], unique_id="shopA"))
    assert out["cart_name"] == "@shopA" and out["cart_key"].startswith("scan:")
    assert _scan(engine, _snap("111", ["a2"], unique_id="shopA"))["cart_name"] == "@shopA (2)"


# ---- 1.15.26: đổi giỏ trên trang Sản phẩm phải hiện ngay sản phẩm của giỏ đó ----
def _mapping_engine():
    """Engine giả đủ để chạy ReplyEngine.mapping_state (danh sách trang Sản phẩm/Studio)."""
    fake = _engine()
    fake.knowledge = types.SimpleNamespace(
        all_records=lambda: [], hidden_ids=lambda: set(), mapping_assignments=lambda: {},
        approved_product_codes=lambda: set(), mappable_records=lambda: [],
        mapping_history_count=lambda: 0)
    fake._product_image_sources = lambda: {}
    fake._prefetch_mapping_images = lambda sources: None
    fake._pinned_product_ids = lambda: set()
    fake._listing_image_url = lambda *a, **k: ""
    fake._suggest_profiles = lambda *a, **k: []
    fake.mapping_state = server_module.ReplyEngine.mapping_state.__get__(fake)
    return fake


def _system_post(engine, path, payload):
    import engine_system
    owner = next(c for c in vars(engine_system).values()
                 if isinstance(c, type) and "system_post" in vars(c))
    return owner.system_post(engine, path, payload)


def test_select_then_products_and_trash_follow_selected_cart_even_when_other_is_live(env):
    engine = _mapping_engine()
    engine.sync(_snap("111", ["a1", "a2"]))
    engine.sync(_snap("", ["d1"], room="7300"))  # giỏ khác (kiểu "Mặc định") đang LIVE
    live = engine.catalog.live_key()
    assert live and engine.catalog.active_key() == live
    product_blocklist.set_removed_many([{"product_id": "a2", "title": "SP a2"}], True)
    state = _system_post(engine, "carts/select", {"key": "uid:111"})
    assert state["active"] == "uid:111" and state["selected"] == "uid:111"
    mapping = engine.mapping_state()
    assert [p["product_id"] for p in mapping["products"]] == ["a1"]
    assert mapping["cart_key"] == "uid:111"
    assert [r["product_id"] for r in engine.removed_listings()] == ["a2"]
    assert _ids(engine.catalog.snapshot) == ["a1"]
    # Bản sao live_products_current.json cũng là giỏ vừa chọn.
    assert _ids(json.loads((env / "live.json").read_text(encoding="utf-8"))) == ["a1", "a2"]


def test_live_mismatch_warning_state_and_use_live_route(env):
    import routes_live
    engine = _engine()
    engine.sync(_snap("111", ["a1"]))
    engine.sync(_snap("222", ["b1"], room="room9", unique_id="shopb"))
    assert engine.catalog.carts_state()["live_mismatch"] is False  # chưa chọn: theo LIVE
    engine.catalog.select("uid:111")
    state = engine.catalog.carts_state()
    assert state["live_mismatch"] is True and state["live_name"] == "@shopb"
    handler = types.SimpleNamespace(engine=engine)
    out = routes_live.post_catalog_carts(handler, "/api/catalog/carts", {}, {})  # chỉ đọc: không đổi giỏ
    assert out["live_mismatch"] is True and engine.catalog.active_key() == "uid:111"
    out = routes_live.post_catalog_carts(handler, "/api/catalog/carts", {"use_live": True}, {})
    assert out["live_mismatch"] is False and out["active"] == "uid:222"
    assert engine.catalog.carts_state()["selected_explicit"] is False


# ---- 1.15.26: quét xong chỉ lưu; đang LIVE theo giỏ Studio chọn cho video đang khoá ----
def test_manual_scan_never_changes_selected_cart(env):
    engine = _engine()
    engine.sync(_snap("111", ["a1"]))
    engine.sync(_snap("222", ["b1"]))
    assert engine.catalog.active_key() == "uid:111"  # chưa chọn tay: giỏ đầu
    first = _scan(engine, _snap("", ["n1"]), name="Mới 1")
    assert first["ok"] and engine.catalog.active_key() == "uid:111"
    engine.catalog.select("uid:222")
    second = _scan(engine, _snap("", ["n2"]), name="Mới 2")
    assert second["ok"] and second["cart_created"]
    state = engine.catalog.carts_state()
    assert state["active"] == "uid:222" and state["selected"] == "uid:222" and state["selected_explicit"]
    assert len(state["carts"]) == 4


def _live_with_carts(engine):
    """A = giỏ chủ shop chọn, B = giỏ Studio chọn cho video, uid:333 đang LIVE."""
    a = _scan(engine, _snap("", ["a1", "a2"]), name="Giỏ A")["cart_key"]
    b = _scan(engine, _snap("", ["b1", "b2", "b3"]), name="Giỏ B")["cart_key"]
    synced = engine.catalog.sync({**_snap("333", ["l1", "b2"], room="7300", unique_id="shopl"), "pin_id": "b2"})
    engine.catalog.select(a)
    return a, b, synced


def test_live_uses_locked_video_cart_over_selected(env):
    engine = _mapping_engine()
    a, b, synced = _live_with_carts(engine)
    engine.catalog.set_session_cart_provider(lambda: b)
    assert engine.catalog.active_key() == b
    assert _ids(engine.catalog.snapshot) == ["b1", "b2", "b3"]  # tự ghim / panel / bảng điều khiển
    view = engine.catalog.ai_catalog(synced)  # AI tư vấn
    assert _ids(view) == ["b1", "b2", "b3"] and view["pin_id"] == "b2" and view["room_id"] == "7300"
    assert view["cart_source"] == "video"
    assert [p["product_id"] for p in engine.mapping_state()["products"]] == ["b1", "b2", "b3"]
    state = engine.catalog.carts_state()
    assert state["active"] == b and state["active_source"] == "video" and state["active_name"] == "Giỏ B"
    assert state["live_mismatch"] is True and state["selected"] == a  # lệch theo giỏ thực sự dùng
    # Ngoài LIVE thì giỏ của video không áp dụng.
    engine.catalog._carts["uid:333"]["snapshot"]["synced_at"] = "2000-01-01T00:00:00+07:00"
    assert engine.catalog.active_key() == a


def test_live_video_cart_deleted_falls_back_to_selected_then_live(env):
    engine = _engine()
    a, b, synced = _live_with_carts(engine)
    engine.catalog.set_session_cart_provider(lambda: b)
    engine.catalog.delete(b)
    assert engine.catalog.active_key() == a and engine.catalog.carts_state()["active_source"] == "selected"
    assert _ids(engine.catalog.ai_catalog(synced)) == ["a1", "a2"]
    engine.catalog.select("")
    assert engine.catalog.active_key() == "uid:333"
    assert engine.catalog.carts_state()["live_mismatch"] is False
    assert engine.catalog.ai_catalog(synced) is synced


def test_live_without_locked_video_uses_selected_or_live_account(env):
    engine = _engine()
    a, b, synced = _live_with_carts(engine)
    engine.catalog.set_session_cart_provider(lambda: "")
    assert engine.catalog.active_key() == a

    def broken():
        raise RuntimeError("x")
    engine.catalog.set_session_cart_provider(broken)
    assert engine.catalog.active_key() == a  # provider lỗi: không làm hỏng việc chọn giỏ
    engine.catalog.select("")
    assert engine.catalog.active_key() == "uid:333"
    assert engine.catalog.carts_state()["active_source"] == "live"


def test_engine_reads_catalog_cart_of_locked_video(env, monkeypatch):
    import video_library
    videos = env / "videos"
    (videos / "vid1").mkdir(parents=True)
    (videos / "vid1" / "timeline.json").write_text(
        json.dumps({"video_id": "vid1", "catalog_cart": "scan:x"}), encoding="utf-8")
    monkeypatch.setattr(video_library, "VIDEO_DIR", videos)
    position = server_module.VideoPositionStore()
    fake = types.SimpleNamespace(video_positions={"tiktok": position})
    read = server_module.ReplyEngine._locked_video_cart.__get__(fake)
    assert read() == ""  # chưa khoá video
    position.update({"video_id": "vid1", "position_s": 3, "state": "locked"})
    assert read() == "scan:x"
    position.update({"video_id": "vid1", "position_s": 3, "state": "uncertain"})
    assert read() == "scan:x"
    position.update({"video_id": "../vid1", "position_s": 3, "state": "locked"})
    assert read() == ""
    position.update({"video_id": "nope", "position_s": 3, "state": "locked"})
    assert read() == ""
    position.update({"video_id": "vid1", "position_s": 3, "state": "locked"})
    with position._lock:
        position._state["monotonic"] -= 60  # nhịp cũ quá 25 giây
    assert read() == ""


def test_current_facts_use_same_cart_as_ai(env):
    engine = _engine()
    a, b, synced = _live_with_carts(engine)
    engine.catalog.set_session_cart_provider(lambda: b)
    engine.video_positions = {"tiktok": server_module.VideoPositionStore()}
    engine._video_lock = server_module.ReplyEngine._video_lock.__get__(engine)
    now = server_module.ReplyEngine._current_facts(engine, "tiktok", "7300", {"product_ids": ["b1"]})
    assert now["pin_id"] == "b2"


def test_carts_route_reports_active_source(env):
    import routes_live
    engine = _engine()
    a, b, _ = _live_with_carts(engine)
    engine.catalog.set_session_cart_provider(lambda: b)
    out = routes_live.post_catalog_carts(types.SimpleNamespace(engine=engine), "/api/catalog/carts", {}, {})
    assert out["active"] == b and out["active_source"] == "video" and out["live_mismatch"] is True
