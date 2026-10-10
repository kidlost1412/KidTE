"""Ánh xạ Shopee phải bền vững và cho phép sản phẩm thay thế dùng chung ID."""
from shopee_mapping import ShopeeMappingStore


def test_many_timeline_products_can_share_shopee_item(tmp_path):
    store = ShopeeMappingStore(tmp_path / "machine" / "shopee_live_mappings.json")
    timelines = {"live1": {"events": [
        {"at_ms": 0, "product": "tc01"},
        {"at_ms": 1000, "product": "topone", "alternate_with": "sachsau"},
    ]}}
    payload = {"video_id": "live1", "mappings": {
        "tc01": {"shop_id": "10", "product_id": "111"},
        "topone": {"shop_id": "10", "product_id": "111"},
        "sachsau": {"shop_id": "10", "product_id": "111"},
    }, "alternates": {"topone": {"shop_id": "10", "product_id": "222"}}}
    assert store.save(payload, timelines)["ok"] is True
    reloaded = ShopeeMappingStore(store.path).all()["videos"]["live1"]
    assert reloaded["mappings"]["topone"] == reloaded["mappings"]["tc01"]
    assert reloaded["alternates"]["topone"]["product_id"] == "222"


def test_rejects_missing_or_invalid_mapping_without_overwriting(tmp_path):
    store = ShopeeMappingStore(tmp_path / "mapping.json")
    timelines = {"live1": {"events": [{"at_ms": 0, "product": "a"}]}}
    good = {"video_id": "live1", "mappings": {"a": {"shop_id": "1", "product_id": "2"}}}
    assert store.save(good, timelines)["ok"]
    assert not store.save({"video_id": "live1", "mappings": {}}, timelines)["ok"]
    assert not store.save({"video_id": "live1", "mappings": {
        "a": {"shop_id": "1", "product_id": "../../bad"}}}, timelines)["ok"]
    assert store.all()["videos"]["live1"]["mappings"]["a"]["product_id"] == "2"


def auto_fixture():
    return {"v1": {"products": {"tc01": {"title": "TC01"}, "topone": {"title": "Bọ Trĩ Top One"}, "soil": {"title": "Sạch Sâu"}, "combo": {"title": "Combo Đạm Cá Humic"}, "fish5": {"title": "Đạm Cá 5L"}}, "events": [{"at_ms": 0, "product": "tc01"}, {"at_ms": 1000, "product": "topone"}, {"at_ms": 2000, "product": "soil"}, {"at_ms": 3000, "product": "combo", "alternate_with": "fish5", "alternate_every_ms": 1000}]}, "v2": {"products": {"fish1": {"title": "Đạm Cá 1L"}}, "events": [{"at_ms": 0, "product": "fish1"}]}}


def auto_bag():
    return [{"shop_id": "10", "product_id": "1", "title": "Kích Rễ TC01"}, {"shop_id": "10", "product_id": "2", "title": "Đạm Cá Amino 5 Lít"}, {"shop_id": "10", "product_id": "3", "title": "Đạm Cá Amino 1L"}]


def test_auto_sync_all_scripts_and_owner_substitutions_persist(tmp_path):
    store = ShopeeMappingStore(tmp_path / "mapping.json")
    result = store.sync({"session_id": "123", "products": auto_bag()}, auto_fixture())
    assert result["ok"] and not result["issues"]
    record = result["videos"]["v1"]
    assert record["mappings"]["topone"] == record["mappings"]["soil"] == record["mappings"]["tc01"]
    assert record["mappings"]["combo"]["product_id"] == "2"
    assert record["alternates"]["combo"]["product_id"] == "3"
    assert result["videos"]["v2"]["mappings"]["fish1"]["product_id"] == "3"
    assert ShopeeMappingStore(store.path).all()["videos"] == result["videos"]


def test_auto_sync_ambiguous_missing_and_replaced_catalog(tmp_path):
    store = ShopeeMappingStore(tmp_path / "mapping.json")
    duplicate = auto_bag() + [{"shop_id": "20", "product_id": "4", "title": "Kích Rễ TC01"}]
    result = store.sync({"session_id": "123", "products": duplicate}, auto_fixture())
    assert "tc01" in result["issues"]["v1"]
    assert "tc01" not in result["videos"]["v1"]["mappings"]
    store.sync({"session_id": "123", "products": auto_bag()}, auto_fixture())
    changed = [{**row, "product_id": str(int(row["product_id"]) + 10)} for row in auto_bag()]
    result = store.sync({"session_id": "456", "products": changed}, auto_fixture())
    assert result["videos"]["v1"]["mappings"]["tc01"]["product_id"] == "11"
    result = store.sync({"session_id": "456", "products": changed[:2]}, auto_fixture())
    assert "combo:luan_phien_1l" in result["issues"]["v1"]


def test_auto_sync_rejects_invalid_catalog_without_mutation(tmp_path):
    store = ShopeeMappingStore(tmp_path / "mapping.json")
    assert not store.sync({"session_id": "bad", "products": []}, auto_fixture())["ok"]
    assert not store.path.exists()


def test_auto_sync_repairs_stale_manual_alternates(tmp_path):
    store = ShopeeMappingStore(tmp_path / "mapping.json")
    store.sync({"session_id": "123", "products": auto_bag()}, auto_fixture())
    record = store.all()["videos"]["v1"]
    record["alternates"] = {"combo": record["mappings"]["combo"], "topone": record["mappings"]["combo"]}
    store.save({"video_id": "v1", **record}, auto_fixture())
    result = store.sync({"session_id": "123", "products": auto_bag()}, auto_fixture())
    assert result["videos"]["v1"]["alternates"] == {"combo": {"shop_id": "10", "product_id": "3"}}


def test_auto_sync_real_titles_skip_gift_and_allow_humic_ingredient(tmp_path):
    store = ShopeeMappingStore(tmp_path / "mapping.json")
    bag = auto_bag()
    bag[1]["title"] = "ĐẠM CÁ HỮU CƠ AMINO BỔ SUNG HUMIC FULVIC PHÂN BÓN 5 LÍT"
    bag[2]["title"] = "ĐẠM CÁ HỮU CƠ AMINO 1L MUA 1 TẶNG 1 BỔ SUNG HUMIC"
    bag += [{"shop_id": "10", "product_id": "4", "title": "(HÀNG TẶNG KÈM KHÔNG BÁN) KÍCH RỄ TC01"},
            {"shop_id": "10", "product_id": "5", "title": "NANO ĐỒNG - Tẩy rửa vườn (500ML)"}]
    timelines = auto_fixture()
    timelines["v2"]["products"]["fish1"]["title"] = bag[2]["title"]
    timelines["v2"]["products"]["nano"] = {"title": "SẠCH RONG RÊU NANO ĐỒNG NẤM HỒNG (Chai 500ml)"}
    timelines["v2"]["events"].append({"at_ms": 1000, "product": "nano"})
    result = store.sync({"session_id": "123", "products": bag}, timelines)
    assert not result["issues"]
    assert result["videos"]["v1"]["mappings"]["tc01"]["product_id"] == "1"
    assert result["videos"]["v2"]["mappings"]["fish1"]["product_id"] == "3"
    assert result["videos"]["v2"]["mappings"]["nano"]["product_id"] == "5"
