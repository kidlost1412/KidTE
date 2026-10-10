"""`[T13]` `legacy_profile_id` là cầu nối bắt buộc giữa sản phẩm hiện hành
(mã `WEB-xxx`) và hồ sơ kỹ thuật đã duyệt cũ (khoá `SP-xx` trong
`approved_live_profiles.json`). Ba sản phẩm dưới đây đang được bán thật
(`approved_for_live`) nhưng bị đứt cầu nối này — nghĩa là AI đang trả lời
liều dùng/thành phần cho khách MÀ KHÔNG CÓ hồ sơ kỹ thuật đã duyệt, dù hồ sơ
đó vẫn tồn tại trong `approved_live_profiles.json`. Khớp bằng tên sản phẩm
và quy cách đóng gói (package) trùng khớp tuyệt đối giữa hai file.

`SP-03` (rỗng, chỉ có `promotion`) và `SP-14` (dung tích 1L đã ngừng bán,
không còn SKU nào khớp trong catalog hiện tại) không có sản phẩm sống để
gán — để nguyên, không có gì để mất.
"""
import json
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from knowledge import KnowledgeBase  # noqa: E402

from _du_lieu_may import can_file  # noqa: E402

# (san_pham_id hiện hành, legacy_profile_id đã xác nhận khớp tên + quy cách)
EXPECTED_LINKS = (
    ("WEB-1236", "SP-12"),  # Bọ trĩ Top One SPI GGO 10SC, chai 200ml
    ("WEB-958", "SP-13"),   # Đạm cá hữu cơ Amino, can 5 lít
    ("WEB-1212", "SP-15"),  # Siêu bám dính, chai 100ml
)


def test_active_products_are_linked_to_their_approved_legacy_profile():
    """Tren may van hanh. Bo qua khi khong co du lieu rieng (mapping cua may
    khong nam trong ma nguon phat hanh)."""
    goc = can_file("machine/catalog_mapping.json", "shared/product_knowledge.json",
                   "approved_live_profiles.json")
    knowledge = KnowledgeBase(
        goc / "shared" / "product_knowledge.json",
        goc / "approved_live_profiles.json",
        goc / "machine" / "catalog_mapping.json",
    )
    for product_id, legacy_id in EXPECTED_LINKS:
        record = next(
            (r for r in knowledge.records if r.get("san_pham_id") == product_id), None,
        )
        assert record is not None, f"{product_id} khong con trong catalog"
        assert record.get("legacy_profile_id") == legacy_id, (
            f"{product_id} phai tro toi {legacy_id}"
        )
        context = knowledge.context(record)
        profile = context.get("ho_so_da_duyet")
        assert profile, f"{product_id} khong ke thua duoc ho so da duyet {legacy_id}"
        assert profile.get("technical_profile"), (
            f"{product_id} thieu technical_profile tu {legacy_id}"
        )


def test_empty_or_discontinued_legacy_profiles_are_left_unmapped():
    """`SP-03`/`SP-14` khong duoc gan bua vao san pham nao chi de het canh
    bao — that su khong con SKU song nao khop, de trong la dung."""
    goc = can_file("approved_live_profiles.json", "shared/product_knowledge.json")
    with open(goc / "approved_live_profiles.json", encoding="utf-8") as f:
        profiles = json.load(f)["products"]
    with open(goc / "shared" / "product_knowledge.json", encoding="utf-8") as f:
        shared = json.load(f)
    content = shared.get("content", shared)
    linked_legacy_ids = {
        p.get("legacy_profile_id")
        for group in content.get("nhom_san_pham", [])
        for p in group.get("san_pham", [])
        if p.get("legacy_profile_id")
    }
    assert "SP-03" not in linked_legacy_ids
    assert "SP-14" not in linked_legacy_ids
    assert set(profiles) >= {"SP-03", "SP-12", "SP-13", "SP-14", "SP-15"}
