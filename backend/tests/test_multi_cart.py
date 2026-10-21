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


def test_payload_without_host_updates_active_cart_only_when_same_products(env):
    store = server_module.LiveCatalogStore()
    store.sync(_snap("111", ["a1"]))
    store.sync(_snap("222", ["b1", "b2", "b3", "b4", "b5"]))
    store.select("uid:222")
    # Không rõ chủ nhưng trùng >= 80% sản phẩm: cùng giỏ -> cập nhật giỏ đang hoạt động.
    out = store.sync(_snap("", ["b1", "b2", "b3", "b4", "b5", "b6"]))
    assert out["cart_key"] == "uid:222" and out["cart_created"] is False
    assert _ids(store.snapshot) == ["b1", "b2", "b3", "b4", "b5", "b6"]
    # Không rõ chủ và khác hẳn: KHÔNG đè giỏ đang hoạt động, tạo giỏ mới.
    out = store.sync(_snap("", ["z1"]))
    assert out["cart_key"].startswith("scan:") and out["cart_created"] is True
    assert _ids(store.cart_snapshot("uid:222")) == ["b1", "b2", "b3", "b4", "b5", "b6"]
    assert _ids(store.cart_snapshot("uid:111")) == ["a1"]


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


# ---- 3.11.11: hai trình duyệt, hai tài khoản (kịch bản chủ shop 20/10) ----
def test_prelive_scan_other_account_not_blocked_by_live_of_account_a(env):
    """A LIVE tài khoản 1; B (trình duyệt khác, tài khoản 2) quét giỏ trước LIVE."""
    engine = _engine()
    live = {**_snap("111", ["a1", "a2"], room="7300", unique_id="shop_mot"), "profile_id": "prof-A"}
    assert engine.sync(live)["cart_key"] == "uid:111"
    # Có host: giỏ riêng của tài khoản 2, không bị guard live_active.
    out = engine.sync({**_snap("222", ["b1"], unique_id="shop_hai"), "profile_id": "prof-B"},
                      user_initiated=True)
    assert out["ok"] is True and "skipped" not in out and out["cart_key"] == "uid:222"
    # Không có host (TikTok chưa trả chủ phòng trước LIVE): vẫn không đè/không bị chặn
    # bởi giỏ LIVE của A — vào giỏ riêng của profile B.
    out = engine.sync({**_snap("", ["b2"]), "profile_id": "prof-B"}, user_initiated=True)
    assert out["ok"] is True and "skipped" not in out
    assert out["cart_key"] == "profile:prof-B"
    assert _ids(engine.catalog.cart_snapshot("uid:111")) == ["a1", "a2"]
    assert engine.catalog.live_key() == "uid:111"


def test_prelive_scan_same_account_still_blocked_and_names_account(env):
    engine = _engine()
    engine.sync({**_snap("111", ["a1"], room="7300", unique_id="shop_mot"), "profile_id": "prof-A"})
    # Cùng tài khoản (có host) ở trình duyệt khác.
    out = engine.sync({**_snap("111", ["x"]), "profile_id": "prof-B"}, user_initiated=True)
    assert out["ok"] is False and out["skipped"] == "live_active"
    assert "@shop_mot" in out["error"] and "đang LIVE" in out["error"]
    # Cùng trình duyệt (cùng profile), không có host, cùng sản phẩm: cùng tài khoản -> chặn như cũ.
    out = engine.sync({**_snap("", ["a1"]), "profile_id": "prof-A"}, user_initiated=True)
    assert out["ok"] is False and out["skipped"] == "live_active"
    assert "@shop_mot" in out["error"]
    # Không có host, sản phẩm khác hẳn: không đè giỏ LIVE, lưu sang giỏ mới.
    out = engine.sync({**_snap("", ["x"]), "profile_id": "prof-A"}, user_initiated=True)
    assert out["ok"] is True and out["cart_key"].startswith("scan:") and out["cart_created"] is True
    assert _ids(engine.catalog.cart_snapshot("uid:111")) == ["a1"]
    assert engine.catalog.active_key() == "uid:111"  # AI vẫn dùng giỏ LIVE


# ---- 1.15.26: mất dữ liệu giỏ (chủ shop 21/10) ----
def _ids_n(prefix, n):
    return [f"{prefix}{i:02d}" for i in range(1, n + 1)]


def test_second_tab_scan_without_host_never_overwrites_first_tab_cart(env):
    """Tab 1 quét 20, xoá 1 còn 19; tab 2 (tài khoản khác, trước LIVE, không host) quét 39."""
    engine = _engine()
    tab1 = _ids_n("p", 20)
    out = engine.sync(_snap("", tab1), user_initiated=True)
    assert out["cart_key"] == "default" and out["product_count"] == 20
    assert out["cart_name"] == "Mặc định"
    engine.trash_listings({"product_ids": ["p20"]})
    assert len(engine.catalog.cart_snapshot("default")["products"]) == 19
    # Tab 2: 39 sản phẩm, gồm cả 19 sản phẩm của tab 1 (trường hợp xấu nhất) + 20 khác.
    tab2 = tab1[:19] + _ids_n("q", 20)
    out = engine.sync(_snap("", tab2), user_initiated=True)
    assert out["ok"] is True and "skipped" not in out
    assert out["cart_key"].startswith("scan:") and out["cart_created"] is True
    assert out["cart_name"].startswith("Giỏ quét lúc ")
    assert out["product_count"] == 39
    # Giỏ 19 còn nguyên.
    assert _ids(engine.catalog.cart_snapshot("default")) == tab1[:19]
    assert len(engine.catalog.cart_snapshot(out["cart_key"])["products"]) == 39
    # Trang Sản phẩm chuyển sang giỏ vừa quét; giỏ cũ vẫn còn trong danh sách.
    assert engine.catalog.active_key() == out["cart_key"]
    counts = {c["key"]: c["product_count"] for c in engine.catalog.carts_state()["carts"]}
    assert counts == {"default": 19, out["cart_key"]: 39}
    # Không ghi đè -> không có lịch sử.
    assert engine.catalog.cart_history("default") == []
    # Mở lại từ đĩa: đủ 2 giỏ.
    again = server_module.LiveCatalogStore()
    assert len(again.cart_snapshot("default")["products"]) == 19
    assert len(again.cart_snapshot(out["cart_key"])["products"]) == 39


def test_second_tab_disjoint_products_also_gets_own_cart(env):
    engine = _engine()
    engine.sync(_snap("", _ids_n("p", 19)), user_initiated=True)
    out = engine.sync(_snap("", _ids_n("q", 39)), user_initiated=True)
    assert out["cart_key"].startswith("scan:")
    assert _ids(engine.catalog.cart_snapshot("default")) == _ids_n("p", 19)
    # Tab 1 quét lại: về đúng giỏ của nó (dù giỏ đang chọn là giỏ của tab 2).
    out = engine.sync(_snap("", _ids_n("p", 20)), user_initiated=True)
    assert out["cart_key"] == "default"
    assert len(engine.catalog.cart_snapshot("default")["products"]) == 20
    assert len(engine.catalog.carts_state()["carts"]) == 2


def test_rescan_same_cart_updates_it_and_keeps_history(env):
    engine = _engine()
    first = engine.sync(_snap("", _ids_n("p", 20)), user_initiated=True)
    # Quét lại cùng tab, TikTok thêm 2 SP: trùng 20/22 >= 80% -> cập nhật đúng giỏ đó.
    out = engine.sync(_snap("", _ids_n("p", 22)), user_initiated=True)
    assert out["cart_key"] == first["cart_key"] == "default" and "cart_created" not in out
    assert len(engine.catalog.carts_state()["carts"]) == 1
    assert _ids(engine.catalog.cart_snapshot("default")) == _ids_n("p", 22)
    hist = engine.catalog.cart_history("default")
    assert len(hist) == 1 and hist[0]["reason"] == "manual_scan" and hist[0]["product_count"] == 20
    # Quét lại y hệt: không đổi -> không thêm lịch sử.
    engine.sync(_snap("", _ids_n("p", 22)), user_initiated=True)
    assert len(engine.catalog.cart_history("default")) == 1
    # Đồng bộ tự động (không phải quét tay) không ghi lịch sử.
    engine.sync(_snap("", _ids_n("p", 21)))
    assert len(engine.catalog.cart_history("default")) == 1
    row = engine.catalog.carts_state()["carts"][0]
    assert row["history_count"] == 1 and row["history_at"]


def test_restore_previous_version_and_undo(env):
    store = server_module.LiveCatalogStore()
    store.sync(_snap("111", ["a1", "a2"]))
    store.sync(_snap("111", ["a3"]), user_initiated=True)  # có host: ghi đè giỏ của chính tài khoản
    assert _ids(store.cart_snapshot("uid:111")) == ["a3"]
    state = store.restore_previous("uid:111")
    assert _ids(store.cart_snapshot("uid:111")) == ["a1", "a2"]
    assert state["carts"][0]["history_count"] == 1  # bản ["a3"] được giữ để hoàn tác
    # Lịch sử bền qua khởi động lại; bấm lại = hoàn tác.
    again = server_module.LiveCatalogStore()
    again.restore_previous("uid:111")
    assert _ids(again.cart_snapshot("uid:111")) == ["a3"]
    with pytest.raises(ValueError, match="cart_not_found"):
        again.restore_previous("uid:999")


def test_restore_previous_errors(env):
    store = server_module.LiveCatalogStore()
    store.sync(_snap("111", ["a1"], room="7300"))
    store.sync(_snap("222", ["b1"]))
    with pytest.raises(ValueError, match="cart_history_empty"):
        store.restore_previous("uid:222")
    store.sync(_snap("111", ["a2"], room="7300"), user_initiated=True)
    with pytest.raises(ValueError, match="cart_is_live"):
        store.restore_previous("uid:111")


def test_history_keeps_last_ten(env):
    store = server_module.LiveCatalogStore()
    for n in range(13):
        store.sync(_snap("111", [f"v{n}"]), user_initiated=True)
    assert len(store.cart_history("uid:111")) == 10
    data = json.loads((env / "live_carts_history.json").read_text(encoding="utf-8"))
    assert [e["snapshot"]["products"][0]["product_id"] for e in data["carts"]["uid:111"]] == \
        [f"v{n}" for n in range(11, 1, -1)]


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


def test_system_route_restore(env):
    import engine_system
    store = server_module.LiveCatalogStore()
    store.sync(_snap("111", ["a1"]))
    store.sync(_snap("111", ["a2"]), user_initiated=True)
    fake = types.SimpleNamespace(catalog=store, workspace=types.SimpleNamespace(_touch=lambda *a: None))
    owner = next(c for c in vars(engine_system).values()
                 if isinstance(c, type) and "system_post" in vars(c))
    state = owner.system_post(fake, "carts/restore", {"key": "uid:111"})
    assert state["ok"] is True and _ids(store.cart_snapshot("uid:111")) == ["a1"]
