import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from _du_lieu_may import can_file

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from catalog_layers import (
    CatalogLayerError, ambiguous_reasons, build_catalog_layers,
    canonical_product_code, find_ambiguous_approved_assignments,
    website_ids_from_product_code,
)
from knowledge import KnowledgeBase


NOW = datetime(2026, 9, 12, tzinfo=timezone.utc)


def _fixture():
    catalog = {
        "nhom_san_pham": [
            {"nhom": "legacy", "san_pham": [
                {"san_pham_id": "SP-01", "ten_san_pham": "Cũ",
                 "shop_product_ids": ["p1"], "dose_rules": [{"ml": 10}],
                 "approved_for_live": False},
            ]},
            {"nhom": "website", "san_pham": [
                {"san_pham_id": "WEB-20", "ten_san_pham": "Hồ sơ web",
                 "website_product_ids": ["20"], "thanh_phan_dang_ky": ["A"],
                 "cong_dung": ["Kích rễ"]},
            ]},
            {"nhom": "live", "san_pham": [
                {"san_pham_id": "LIVE-p1", "legacy_profile_id": "SP-01",
                 "ten_san_pham": "Tên SEO 1L", "website_product_ids": ["20"],
                 "shop_product_ids": ["p1"], "quy_cach_dong_goi": "1L",
                 "gia_goc_duyet": "100k", "approved_for_live": True},
                {"san_pham_id": "LIVE-p2", "ten_san_pham": "Tên SEO combo",
                 "website_product_ids": ["30", "10"], "shop_product_ids": ["p2"],
                 "approved_for_live": False, "is_combo": True,
                 "cong_dung": ["Combo kích rễ"]},
            ]},
        ],
    }
    live = {"products": [
        {"product_id": "p1", "title": "Tên SEO 1L", "price": "90k", "bag_index": 1},
        {"product_id": "p2", "title": "Tên SEO combo", "price": "200k", "bag_index": 2},
    ]}
    mapping = {"rules": [
        {"shop_product_id": "p1", "website_product_ids": ["20"], "confidence": "high"},
        {"shop_product_id": "p2", "website_product_ids": ["30", "10"], "confidence": "high", "combo": True},
    ]}
    return catalog, live, mapping


def test_canonical_codes_are_deterministic():
    assert canonical_product_code(["20"]) == "WEB-20"
    assert canonical_product_code(["30", "10"]) == "COMBO-WEB-10-WEB-30"
    with pytest.raises(CatalogLayerError, match="lặp"):
        canonical_product_code(["10", "10"])


def test_website_ids_from_product_code_reverses_canonical_product_code():
    assert website_ids_from_product_code("WEB-150") == ["150"]
    assert website_ids_from_product_code("COMBO-WEB-2-WEB-10") == ["2", "10"]
    assert website_ids_from_product_code(
        canonical_product_code(["10", "2"])
    ) == ["2", "10"]
    with pytest.raises(CatalogLayerError):
        website_ids_from_product_code("khong-hop-le")
    with pytest.raises(CatalogLayerError):
        website_ids_from_product_code("")


def test_website_ids_from_product_code_round_trips_every_code_shape():
    """Quy tắc tra ngược product_code -> website id phải khứ hồi trọn vẹn.

    Bản cũ chỉ kiểm trên `data/machine/catalog_mapping.json` của máy vận hành
    — file dữ liệu riêng không có trong mã nguồn — nên test fail ngay trên
    một bản checkout sạch. Ở đây khoá lại chính quy tắc, bằng mọi dạng mã
    thực sự xuất hiện: đơn lẻ, combo 2 phần, combo nhiều phần, và mã cho
    trước theo thứ tự đảo."""
    for ids in (["150"], ["2", "10"], ["10", "2"], ["1", "2", "3"],
                ["828"], ["1236", "958", "1212"], ["7"]):
        code = canonical_product_code(ids)
        assert canonical_product_code(website_ids_from_product_code(code)) == code
        assert website_ids_from_product_code(code) == sorted(ids, key=int)


def test_website_ids_from_product_code_matches_every_real_assignment():
    """Trên máy vận hành: khoá để mapping thật không lệch âm thầm.

    Bỏ qua khi chạy trên bản checkout không có dữ liệu riêng."""
    root = can_file("machine/catalog_mapping.json")
    machine = json.loads(
        (root / "machine" / "catalog_mapping.json").read_text(encoding="utf-8")
    )
    codes = {item["product_code"] for item in machine["assignments"].values()}
    assert codes, "machine/catalog_mapping.json không có assignment nào để kiểm"
    for code in codes:
        ids = website_ids_from_product_code(code)
        assert canonical_product_code(ids) == code


def test_ambiguous_reasons_flags_review_note_combo_and_low_confidence():
    assert ambiguous_reasons({"mapping_audit": {"confidence": "high"}}) == []
    assert ambiguous_reasons({
        "mapping_audit": {"confidence": "high", "combo": True, "combo_da_duyet": True},
    }) == []
    reasons = ambiguous_reasons({
        "mapping_audit": {"confidence": "medium", "review_note": "lệch quy cách",
                          "combo": True},
    })
    assert any("review_note" in r for r in reasons)
    assert any("combo_da_duyet" in r for r in reasons)
    assert any("confidence" in r for r in reasons)


def test_find_ambiguous_approved_assignments_ignores_properly_blocked_ones():
    machine = {"assignments": {
        # Mơ hồ nhưng KHÔNG duyệt -> bị chặn đúng cách, không phải lỗi.
        "blocked_ok": {"approved_for_live": False,
                       "mapping_audit": {"confidence": "medium"}},
        # Rõ ràng và đang duyệt -> sạch.
        "clean_ok": {"approved_for_live": True,
                     "mapping_audit": {"confidence": "high"}},
        # Mơ hồ MÀ VẪN duyệt -> đúng thứ T5 phải bắt.
        "dangerous": {"approved_for_live": True,
                      "mapping_audit": {"confidence": "high", "review_note": "chưa chắc"}},
    }}
    found = find_ambiguous_approved_assignments(machine)
    assert set(found) == {"dangerous"}


def test_build_catalog_layers_refuses_to_publish_an_ambiguous_approved_assignment():
    """`[T5]` Nếu record LIVE ghi approved_for_live=True nhưng rule mapping
    của nó vẫn còn review_note (vd ai đó sửa tay approved_product_knowledge.json
    mà quên hạ duyệt), build_catalog_layers() phải TỪ CHỐI publish, không
    được âm thầm mang một SKU mơ hồ lên machine."""
    catalog, live, mapping = _fixture()
    mapping["rules"][0]["review_note"] = "phát hiện lệch quy cách sau khi đã duyệt"

    with pytest.raises(CatalogLayerError, match="TỪ CHỐI PUBLISH"):
        build_catalog_layers(catalog, live, mapping, published_at=NOW)

    # Ca sạch (fixture gốc, không sửa) vẫn phải publish được bình thường —
    # chốt T5 không được chặn nhầm assignment hợp lệ.
    shared, machine, report = build_catalog_layers(*_fixture(), published_at=NOW)
    assert find_ambiguous_approved_assignments(machine) == {}


def test_migration_separates_machine_fields_and_preserves_approval():
    shared, machine, report = build_catalog_layers(*_fixture(), published_at=NOW)
    shared_text = json.dumps(shared, ensure_ascii=False)

    assert "shop_product_id" not in shared_text
    assert "Tên SEO" not in shared_text
    assert "100k" not in shared_text
    assert report["legacy_shop_product_id_references"] == 3
    assert report["machine_assignment_count"] == 2
    assert report["approved_assignment_count"] == 1
    assert machine["assignments"]["p1"]["product_code"] == "WEB-20"
    assert machine["assignments"]["p1"]["listing"]["title"] == "Tên SEO 1L"
    products = shared["content"]["nhom_san_pham"][0]["san_pham"]
    web = next(item for item in products if item["san_pham_id"] == "WEB-20")
    assert web["ten_san_pham"] == "Hồ sơ web"
    assert web["dose_rules"] == [{"ml": 10}]


def test_migration_refuses_missing_rule_or_live_record():
    catalog, live, mapping = _fixture()
    broken = copy.deepcopy(mapping)
    broken["rules"] = broken["rules"][:1]
    with pytest.raises(CatalogLayerError, match="không phủ"):
        build_catalog_layers(catalog, live, broken, published_at=NOW)

    broken_catalog = copy.deepcopy(catalog)
    broken_catalog["nhom_san_pham"][2]["san_pham"] = []
    with pytest.raises(CatalogLayerError, match="Không có LIVE record"):
        build_catalog_layers(broken_catalog, live, mapping, published_at=NOW)


def test_migration_refuses_conflicting_technical_data_for_one_code():
    catalog, live, mapping = _fixture()
    duplicate_live = copy.deepcopy(catalog["nhom_san_pham"][2]["san_pham"][0])
    duplicate_live.update({"san_pham_id": "LIVE-p3", "shop_product_ids": ["p3"],
                           "thanh_phan_dang_ky": ["B"]})
    catalog["nhom_san_pham"][2]["san_pham"].append(duplicate_live)
    live["products"].append({"product_id": "p3", "title": "Tên khác", "price": "99k"})
    mapping["rules"].append(
        {"shop_product_id": "p3", "website_product_ids": ["20"], "confidence": "high"}
    )
    with pytest.raises(CatalogLayerError, match="mâu thuẫn"):
        build_catalog_layers(catalog, live, mapping, published_at=NOW)


def test_machine_reassignment_never_changes_shared_knowledge(tmp_path):
    shared, machine, _ = build_catalog_layers(*_fixture(), published_at=NOW)
    shared_path = tmp_path / "shared.json"
    machine_path = tmp_path / "machine.json"
    profiles_path = tmp_path / "profiles.json"
    shared_path.write_text(json.dumps(shared, ensure_ascii=False), encoding="utf-8")
    machine_path.write_text(json.dumps(machine, ensure_ascii=False), encoding="utf-8")
    profiles_path.write_text('{"products": {}}', encoding="utf-8")
    before = shared_path.read_bytes()

    knowledge = KnowledgeBase(shared_path, profiles_path, machine_path)
    assert knowledge.apply_mapping({"p1": "COMBO-WEB-10-WEB-30"}) == 1
    knowledge.reload()

    assert shared_path.read_bytes() == before
    assert knowledge.match("", product_id="p1")["san_pham_id"] == "COMBO-WEB-10-WEB-30"
    saved = json.loads(machine_path.read_text(encoding="utf-8"))
    assert saved["assignments"]["p1"]["approved_for_live"] is True


def test_reassigning_shop_id_to_a_different_product_clears_stale_overrides(tmp_path):
    """`p1` từng được gán cho WEB-20 với cảnh báo quy cách riêng của SKU đó
    (vd chai 250ml). Gán lại sang một mã khác (COMBO-WEB-10-WEB-30) thì
    cảnh báo/quy cách cũ KHÔNG được đi theo — đó là dữ liệu của sản phẩm cũ,
    giữ lại là bịa quy cách/liều cho sản phẩm mới."""
    shared, machine, _ = build_catalog_layers(*_fixture(), published_at=NOW)
    shared_path = tmp_path / "shared.json"
    machine_path = tmp_path / "machine.json"
    profiles_path = tmp_path / "profiles.json"
    machine["assignments"]["p1"]["profile_overrides"] = {
        "canh_bao_quy_cach": "Chai 250ml, khác quy cách web ghi 1L",
    }
    shared_path.write_text(json.dumps(shared, ensure_ascii=False), encoding="utf-8")
    machine_path.write_text(json.dumps(machine, ensure_ascii=False), encoding="utf-8")
    profiles_path.write_text('{"products": {}}', encoding="utf-8")

    knowledge = KnowledgeBase(shared_path, profiles_path, machine_path)
    assert knowledge.apply_mapping({"p1": "COMBO-WEB-10-WEB-30"}) == 1

    saved = json.loads(machine_path.read_text(encoding="utf-8"))
    assert saved["assignments"]["p1"]["product_code"] == "COMBO-WEB-10-WEB-30"
    assert "profile_overrides" not in saved["assignments"]["p1"]
    assert saved["history"][-1]["reason"] == "manual_mapping_ui"
    assert knowledge.mapping_history_count() == 1
    assert knowledge.undo_last_mapping() is True
    knowledge.reload()
    assert knowledge.match("", product_id="p1")["san_pham_id"] == "WEB-20"
    assert knowledge.mapping_history_count() == 0


def test_shared_layer_keeps_the_link_to_its_approved_legacy_profile():
    """`[F13]` Lớp shared phải GIỮ `legacy_profile_id`, không chỉ gộp nội dung.

    `_merge_missing()` cố ý bỏ qua `legacy_profile_id`, nên trước đây bằng
    chứng ràng buộc bị vứt đi ngay tại bước gộp: bản shared có công thức của
    hồ sơ legacy nhưng không còn biết mình đến từ hồ sơ nào. Hậu quả thật đo
    được trên máy vận hành: WEB-1236 / WEB-958 / WEB-1212 đều mất liên kết,
    `knowledge.context()` không nạp được `ho_so_da_duyet`, và AI trả lời liều
    dùng mà không có hồ sơ kỹ thuật đã duyệt.
    """
    catalog, live, mapping = _fixture()
    shared_layer, _machine, _report = build_catalog_layers(
        catalog, live, mapping, published_at=NOW)
    shared = {
        item["san_pham_id"]: item
        for group in shared_layer["content"]["nhom_san_pham"]
        for item in group["san_pham"]
    }
    assert shared["WEB-20"]["legacy_profile_id"] == "SP-01", (
        "mã WEB-20 gộp từ hồ sơ SP-01 thì phải ghi lại đúng SP-01"
    )
    # Và nội dung kỹ thuật vẫn được mang sang như cũ.
    assert shared["WEB-20"]["dose_rules"] == [{"ml": 10}]


def test_shared_layer_refuses_two_legacy_profiles_on_one_code():
    """Hai hồ sơ kỹ thuật tranh nhau một mã là mơ hồ nguy hiểm — phải chặn,
    không được im lặng lấy cái cuối cùng."""
    catalog, live, mapping = _fixture()
    catalog["nhom_san_pham"][0]["san_pham"].append(
        {"san_pham_id": "SP-02", "ten_san_pham": "Cũ 2",
         "shop_product_ids": ["p3"], "approved_for_live": False},
    )
    catalog["nhom_san_pham"][2]["san_pham"].append(
        {"san_pham_id": "LIVE-p3", "legacy_profile_id": "SP-02",
         "ten_san_pham": "Tên SEO 2L", "website_product_ids": ["20"],
         "shop_product_ids": ["p3"], "approved_for_live": True},
    )
    live["products"].append(
        {"product_id": "p3", "title": "Tên SEO 2L", "price": "150k", "bag_index": 3})
    mapping["rules"].append(
        {"shop_product_id": "p3", "website_product_ids": ["20"], "confidence": "high"})

    with pytest.raises(CatalogLayerError, match="tranh nhau"):
        build_catalog_layers(catalog, live, mapping, published_at=NOW)
