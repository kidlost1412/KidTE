"""Bộ ghép listing <-> hồ sơ: quy cách, combo, tiếng Việt không dấu, tiêu đề thật."""
import json
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import listing_match as lm

HERE = Path(__file__).resolve().parent
SEED = HERE.parent / "prompts" / "knowledge_seed.json"


def _profiles():
    package = json.loads(SEED.read_text(encoding="utf-8"))
    return [p for g in package["content"]["nhom_san_pham"] for p in g["san_pham"]]


def _synthetic_titles():
    return json.loads((HERE / "fixtures" / "live_titles.json").read_text(encoding="utf-8"))["titles"]


def _rec(pid, name, **extra):
    return {"san_pham_id": pid, "ten_san_pham": name, "cong_dung": ["x"], **extra}


@pytest.mark.parametrize("text,expected", [
    ("Đạm cá can 5L", {("v", 5000.0)}),
    ("Can 5 Lít", {("v", 5000.0)}),
    ("chai 5 lit", {("v", 5000.0)}),
    ("100ml", {("v", 100.0)}),
    ("1,5 lít", {("v", 1500.0)}),
    ("túi 1Kg", {("m", 1000.0)}),
    ("gói 50gr", {("m", 50.0)}),
    ("50g", {("m", 50.0)}),
    ("500G", {("m", 500.0)}),
    ("chai 250-500ml", {("v", 250.0), ("v", 500.0)}),
    ("Bọ trĩ 350SC TC01", set()),
])
def test_parse_sizes_units(text, expected):
    assert lm.parse_sizes(text) == expected


def test_one_litre_equals_1000ml_but_not_5l_and_kinds_do_not_mix():
    assert lm.parse_sizes("1L") == lm.parse_sizes("1000ml")
    assert lm.size_state(lm.parse_sizes("1L"), lm.parse_sizes("5L")) is False
    assert lm.size_state(lm.parse_sizes("1kg"), lm.parse_sizes("1L")) is False
    assert lm.size_state(set(), lm.parse_sizes("5L")) is None


@pytest.mark.parametrize("title,expected", [
    ("Combo Đạm Cá + Humic (Can 5 Lít)", True),
    ("COMBO Kích Rễ ROOTS + MIG 38", True),
    ("Nano Đồng + 5 gói RYDOXIN", True),
    ("Phân Bón Chuyên Ớt (50gr)", False),
    ("Đạm cá hữu cơ amino 5 lít", False),
])
def test_combo_detection(title, expected):
    assert lm.is_combo(title) is expected


def test_size_gate_one_litre_listing_never_sure_for_five_litre_profile():
    index = lm.ProfileIndex([_rec("A-5L", "ĐẠM CÁ HỮU CƠ AMINO (5 lít)"),
                             _rec("B", "Furagin trị nấm bệnh")])
    result = lm.match_listing("Đạm Cá Hữu Cơ Amino chai 1L giúp cây xanh lá", index)
    assert result["tier"] != lm.TIER_SURE
    assert result["size_match"] is False
    assert any("KHÁC quy cách" in reason for reason in result["reasons"])


def test_same_name_prefers_profile_with_matching_size():
    index = lm.ProfileIndex([_rec("DAM-1L", "ĐẠM CÁ HỮU CƠ AMINO (1 lít)"),
                             _rec("DAM-5L", "ĐẠM CÁ HỮU CƠ AMINO (5 lít)"),
                             _rec("X", "Trichoderma ủ phân")])
    result = lm.match_listing("Đạm Cá Hữu Cơ Amino (Can 5 Lít) cô đặc", index)
    assert result["profile_id"] == "DAM-5L"
    assert result["size_match"] is True


def test_combo_listing_is_never_sure_even_with_perfect_name():
    index = lm.ProfileIndex([_rec("F", "FURAGIN (100ml) – Khắc tinh nấm bệnh"),
                             _rec("Z", "Trichoderma ủ phân")])
    result = lm.match_listing("Combo Furagin 100ml + Trichoderma tặng kèm", index)
    assert result["is_combo"] is True
    assert result["tier"] != lm.TIER_SURE


def test_combo_profile_is_found_from_component_names():
    combo = _rec("COMBO-1", "Combo WEB-1 + WEB-2", is_combo=True, combo_components=[
        {"ten_san_pham": "NANO ĐỒNG – Tẩy rửa vườn (250ml)"},
        {"ten_san_pham": "RYDOXIN – Tiêu diệt nấm bệnh"}])
    index = lm.ProfileIndex([combo, _rec("W1", "NANO ĐỒNG – Tẩy rửa vườn (250ml)"),
                             _rec("W2", "RYDOXIN – Tiêu diệt nấm bệnh"), _rec("W3", "Humic hữu cơ")])
    result = lm.match_listing("Combo Nano Đồng + 5 gói RYDOXIN giúp đẩy lùi nấm", index)
    assert result["profile_id"] == "COMBO-1"
    assert result["tier"] == lm.TIER_REVIEW


def test_titles_without_diacritics_match_like_accented_ones():
    index = lm.ProfileIndex([_rec("MG", "MAGIE KẼM (50GR) – BIẾN VÀNG THÀNH XANH"),
                             _rec("CO", "CHUYÊN ỚT (50GR) – Bóng quả"), _rec("HU", "HUMIC")])
    accented = lm.match_listing("Phân bón Magie Kẽm - Bổ sung vi lượng (50gr pha 2 bình)", index)
    plain = lm.match_listing("phan bon magie kem bo sung vi luong 50gr pha 2 binh", index)
    assert accented["profile_id"] == plain["profile_id"] == "MG"
    # Không dấu và không dấu câu: vẫn đúng hồ sơ, chỉ được phép kém tự tin hơn (không sai).
    assert accented["tier"] == lm.TIER_SURE
    assert plain["tier"] in (lm.TIER_SURE, lm.TIER_REVIEW)
    assert plain["size_match"] is True


def test_generic_words_alone_never_produce_a_suggestion():
    index = lm.ProfileIndex([_rec("A", "NANO ĐỒNG – Tẩy rửa vườn"), _rec("B", "Trichoderma ủ phân")])
    result = lm.match_listing("Phân bón hữu cơ cao cấp giúp cây xanh lá", index)
    assert result["tier"] == lm.TIER_NONE


def test_short_name_cuts_marketing_text():
    name = lm.short_name("SIÊU BÁM DÍNH TOÀN CẦU Công Nghệ NANO Mới Giúp Bám Dính Lâu, Loang Trải")
    assert 4 <= len(name) <= 80
    assert lm.short_name("Phân Bón Chuyên Ớt (50gr) - Bóng quả, chín đều") == "Phân Bón Chuyên Ớt"


def test_synthetic_titles_against_seed_profiles_are_safe():
    """Synthetic listing examples exercise safety checks against the shipped profile set."""
    records = _profiles()
    results = lm.auto_match(_synthetic_titles(), records)
    assert len(results) == 41
    tiers = [r["tier"] for r in results]
    assert set(tiers) <= {lm.TIER_SURE, lm.TIER_REVIEW, lm.TIER_NONE}
    for r in results:
        if r["tier"] == lm.TIER_SURE:
            assert r["size_match"] is not False
            assert r["is_combo"] is False
            assert not r["profile_id"].startswith("COMBO-")
    for r in results:
        if r["is_combo"]:
            assert r["tier"] != lm.TIER_SURE


def test_synthetic_sure_pairs_agree_with_known_profiles():
    expected = {
        "SYN-LISTING-001": "WEB-1006",
        "SYN-LISTING-002": "WEB-826",
        "SYN-LISTING-003": "WEB-1126",
        "SYN-LISTING-004": "WEB-1149",
        "SYN-LISTING-005": "WEB-830",
    }
    by_id = {r["product_id"]: r for r in lm.auto_match(_synthetic_titles(), _profiles())}
    for product_id, profile_id in expected.items():
        assert by_id[product_id]["profile_id"] == profile_id, product_id
        assert by_id[product_id]["tier"] == lm.TIER_SURE, product_id
