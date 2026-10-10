"""T4: quy cách/cảnh báo/giá riêng của TỪNG SKU (canh_bao_quy_cach,
quy_cach_dong_goi, gia_goc_duyet) phải sống sót qua migration và tới đúng
context() theo shop_id — không phải theo mã kỹ thuật dùng chung, vì hai SKU
có thể dùng chung một mã (WEB-828) nhưng khác quy cách chai.
"""
import json
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from catalog_layers import build_catalog_layers  # noqa: E402
from knowledge import KnowledgeBase  # noqa: E402

PLUGINS = Path(__file__).resolve().parents[2] / "plugins"
sys.path.insert(0, str(PLUGINS / "chinh-sach-gia"))
from processor import price_context  # noqa: E402


def _write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _write_kb_fixture(tmp_path):
    """Một mã WEB-828 dùng chung cho hai SKU khác dung tích (250ml/500ml),
    y hệt case Nano Đồng thật. Mỗi SKU có canh_bao_quy_cach/quy_cach_dong_goi
    riêng, và một SKU có gia_goc_duyet riêng."""
    catalog = {
        "nhom_san_pham": [
            {"nhom": "website", "san_pham": [
                {"san_pham_id": "WEB-828", "ten_san_pham": "Nano Đồng",
                 "website_product_ids": ["828"],
                 "huong_dan_su_dung": ["Pha 20ml cho bình 20 lít"]},
            ]},
            {"nhom": "live", "san_pham": [
                {"san_pham_id": "LIVE-shop250", "ten_san_pham": "Nano Đồng 250ml",
                 "website_product_ids": ["828"], "shop_product_ids": ["shop250"],
                 "quy_cach_dong_goi": "250ml", "approved_for_live": True},
                {"san_pham_id": "LIVE-shop500", "ten_san_pham": "Nano Đồng 500ml",
                 "website_product_ids": ["828"], "shop_product_ids": ["shop500"],
                 "quy_cach_dong_goi": "500ml",
                 "canh_bao_quy_cach": "SKU đang bán là 500ml nhưng hồ sơ kỹ thuật"
                                      " lấy từ trang 250ml (cùng công thức).",
                 "gia_goc_duyet": "289.000₫",
                 "approved_for_live": True},
            ]},
        ],
    }
    live = {"products": [
        {"product_id": "shop250", "title": "Nano Đồng 250ml", "price": "155k"},
        {"product_id": "shop500", "title": "Nano Đồng 500ml", "price": "0"},
    ]}
    mapping = {"rules": [
        {"shop_product_id": "shop250", "website_product_ids": ["828"], "confidence": "high"},
        {"shop_product_id": "shop500", "website_product_ids": ["828"], "confidence": "high"},
    ]}
    shared, machine, _ = build_catalog_layers(catalog, live, mapping)

    shared_path = tmp_path / "shared.json"
    machine_path = tmp_path / "machine.json"
    profiles_path = tmp_path / "profiles.json"
    _write(shared_path, shared)
    _write(machine_path, machine)
    _write(profiles_path, {"products": {}})
    return shared_path, machine_path, profiles_path


def test_profile_overrides_are_saved_per_shop_id_not_per_product_code(tmp_path):
    _, machine_path, _ = _write_kb_fixture(tmp_path)
    machine = json.loads(machine_path.read_text(encoding="utf-8"))

    assert machine["assignments"]["shop250"]["product_code"] == "WEB-828"
    assert machine["assignments"]["shop500"]["product_code"] == "WEB-828"
    # Cùng mã WEB-828 nhưng chỉ shop500 có cảnh báo — đúng vì đó là cảnh báo
    # của RIÊNG chai 500ml, không phải của công thức Nano Đồng nói chung.
    assert machine["assignments"]["shop250"]["profile_overrides"] == {
        "quy_cach_dong_goi": "250ml",
    }
    assert machine["assignments"]["shop500"]["profile_overrides"] == {
        "quy_cach_dong_goi": "500ml",
        "canh_bao_quy_cach": (
            "SKU đang bán là 500ml nhưng hồ sơ kỹ thuật lấy từ trang 250ml"
            " (cùng công thức)."
        ),
        "gia_goc_duyet": "289.000₫",
    }


def test_context_merges_the_right_shop_specific_warning():
    """Chốt an toàn cốt lõi của T4: hỏi về shop500 phải thấy đúng
    canh_bao_quy_cach của nó; hỏi về shop250 (cùng mã kỹ thuật) không được
    thấy cảnh báo đó — nếu không AI sẽ nói sai quy cách cho khách mua 250ml."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        shared_path, machine_path, profiles_path = _write_kb_fixture(Path(tmp))
        kb = KnowledgeBase(shared_path, profiles_path, machine_path)

        record_500 = kb.match("Nano Đồng 500ml", product_id="shop500")
        approved_500 = kb.context(record_500, shop_id="shop500")
        assert approved_500["canh_bao_quy_cach"].startswith("SKU đang bán là 500ml")
        assert approved_500["quy_cach_dong_goi"] == "500ml"
        assert approved_500["gia_goc_duyet"] == "289.000₫"

        record_250 = kb.match("Nano Đồng 250ml", product_id="shop250")
        approved_250 = kb.context(record_250, shop_id="shop250")
        assert "canh_bao_quy_cach" not in approved_250
        assert approved_250["quy_cach_dong_goi"] == "250ml"
        assert "gia_goc_duyet" not in approved_250


def test_context_without_shop_id_does_not_leak_overrides_from_a_stale_cache():
    """Không truyền shop_id (vd chỗ gọi cũ chưa cập nhật) thì không được lấy
    nhầm cảnh báo của một SKU khác — im lặng an toàn hơn báo sai quy cách."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        shared_path, machine_path, profiles_path = _write_kb_fixture(Path(tmp))
        kb = KnowledgeBase(shared_path, profiles_path, machine_path)
        record_500 = kb.match("Nano Đồng 500ml", product_id="shop500")
        approved = kb.context(record_500)  # thiếu shop_id
        assert "canh_bao_quy_cach" not in approved


def test_gia_goc_duyet_is_top_priority_in_price_context():
    """`server.py`/plugin chinh-sach-gia đọc gia_goc_duyet làm bậc ưu tiên số
    1. Trước T4 trường này luôn rỗng vì machine không có chỗ chứa; giờ phải
    hoạt động đúng thứ tự đã ghi trong docstring của price_context()."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        shared_path, machine_path, profiles_path = _write_kb_fixture(Path(tmp))
        kb = KnowledgeBase(shared_path, profiles_path, machine_path)
        record_500 = kb.match("Nano Đồng 500ml", product_id="shop500")
        approved_500 = kb.context(record_500, shop_id="shop500")

        live_product_500 = {
            "original_price": "310.000₫", "price": "270.000₫", "sale_price": "260.000₫",
        }
        result = price_context(live_product_500, approved_500)
        assert result["quotable_price"] == "289.000₫"
        assert result["quotable_price_source"] == "gia_goc_da_duyet"

        # shop250 khong co gia_goc_duyet -> phai roi xuong bac original_price.
        record_250 = kb.match("Nano Đồng 250ml", product_id="shop250")
        approved_250 = kb.context(record_250, shop_id="shop250")
        result_250 = price_context(live_product_500, approved_250)
        assert result_250["quotable_price"] == "310.000₫"
        assert result_250["quotable_price_source"] == "original_price"
