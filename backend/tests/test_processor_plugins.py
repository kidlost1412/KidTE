import ast
import sys
from pathlib import Path
from types import SimpleNamespace

SRC = Path(__file__).resolve().parents[1] / "src"
ROOT = SRC.parents[1]
sys.path.insert(0, str(SRC))

from plugin_registry import discover_plugins
from processor_loader import load_processor


def test_dose_processor_contract_and_characteristic_cases():
    plugin = load_processor(ROOT / "plugins" / "lieu-dung" / "processor.py", "lieu-dung-test")
    assert plugin.target_tank_liters("đạm cá 1l pha bình 20 lít") == 20
    rows = plugin.verified_dose_calculations(
        "pha bình 20 lít", {"dose_extrapolation_rule": "nearest_endpoint",
                           "huong_dan_su_dung": ["Pha 1kg cho 1500 lít nước"]},
    )
    assert rows[0]["don_vi"] == "g"
    assert rows[0]["calculated_ml"] == 13.33
    ranged = plugin.verified_dose_calculations(
        "bình 20 lít pha sao",
        {"huong_dan_su_dung": ["Bình 16 - 25 lít: 20 - 30ml"]},
    )
    assert ranged[0]["within_source_range"] is True
    combo = plugin.verified_dose_calculations("bình 16 lít", {
        "is_combo": True, "combo_components": [
            {"ten_san_pham": "A", "huong_dan_su_dung": ["10ml cho 16 lít"]},
            {"ten_san_pham": "B", "huong_dan_su_dung": ["20g cho 16 lít"]},
        ],
    })
    assert [(row["combo_component"], row["calculated_ml"], row["don_vi"])
            for row in combo] == [("A", 10, "ml"), ("B", 20, "g")]


def test_processor_plugins_do_not_import_each_other():
    processor_ids = {path.parent.name.replace("-", "_")
                     for path in (ROOT / "plugins").glob("*/processor.py")}
    for path in (ROOT / "plugins").glob("*/processor.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        own = path.parent.name.replace("-", "_")
        forbidden = processor_ids - {own}
        assert not any(name.split(".")[0].replace("-", "_") in forbidden
                       for name in imported), f"{path} import plugin khác: {imported}"


def test_dose_manifest_is_discovered_as_processor():
    manifests, errors = discover_plugins(ROOT / "plugins")
    assert "lieu-dung" in manifests
    assert manifests["lieu-dung"].kind == "bo-xu-ly"
    assert "lieu-dung" not in errors


def test_price_processor_characteristic_cases():
    plugin = load_processor(
        ROOT / "plugins" / "chinh-sach-gia" / "processor.py", "price-test",
    )
    product = {"original_price": "289.000₫", "sale_price": "226.380₫"}
    assert plugin.price_context(product)["quotable_price"] == "289.000₫"
    assert "226.380" not in str(plugin.price_context(product))
    signal = plugin.customer_price_signal("em thấy còn 185k", product)
    assert signal["reported_price_vnd"] == 185_000
    assert signal["relation"] == "lower_than_shop_visible_price"
    assert plugin.customer_price_signal("đạm cá can 5 lít giá nhiêu", product) == {}
    after_sale = plugin.customer_price_signal("anh vừa mua rồi giá 289k", product)
    assert after_sale["da_mua_roi"] is True
    assert "không phải câu chốt đơn" in after_sale["response_goal"]
    assert plugin.violates_price_policy("Giá đúng là 289k", signal)
    assert plugin.asserts_voucher("tài khoản anh có mã rồi")
    assert plugin.denies_catalog("shop có thuốc kiến không", "Dạ không có")


def test_price_context_refuses_to_quote_when_only_sale_price_is_known():
    """`[?]` không được lấy sale_price làm số cuối khi original_price/price
    đều thiếu — sale_price là giá đang giảm trên live, tuyệt đối không được
    gắn nhãn coi như giá gốc/giá niêm yết rồi đọc cho khách."""
    plugin = load_processor(
        ROOT / "plugins" / "chinh-sach-gia" / "processor.py", "price-test",
    )
    product = {"sale_price": "226.380₫"}
    assert plugin.price_context(product) == {}


def test_data_collector_survives_restart_and_disk_failure(tmp_path, monkeypatch):
    plugin = load_processor(ROOT / "plugins" / "thu-du-lieu" / "processor.py", "data-test")
    row = {"text": "giá bao nhiêu", "msg_id": "m-1", "data_source": "real_live"}
    first = plugin.RawStreamRecorder(tmp_path, enabled=True)
    assert first.record([row]) == 1
    restarted = plugin.RawStreamRecorder(tmp_path, enabled=True)
    assert restarted.count == 1
    assert restarted.record([row]) == 0
    fresh = {"text": "không được mất", "msg_id": "m-2"}
    original = Path.open
    monkeypatch.setattr(Path, "open", lambda *_a, **_k: (_ for _ in ()).throw(OSError("full")))
    try:
        try:
            restarted.record([fresh])
        except OSError:
            pass
    finally:
        monkeypatch.setattr(Path, "open", original)
    assert restarted.record([fresh]) == 1


def test_data_delivery_adapter_preserves_memory_contract():
    plugin = load_processor(ROOT / "plugins" / "thu-du-lieu" / "processor.py", "delivery-test")

    class Memory:
        def confirm_reply_sent(self, *args): return ("confirm", args)
        def mark_reply_delivery(self, *args): return ("mark", args)

    memory = Memory()
    assert plugin.confirm_delivery(memory, "i", "r", {"ok": True})[0] == "confirm"
    assert plugin.mark_delivery(memory, "i", "r", "failed", "network_error")[0] == "mark"


def test_model_processor_exposes_replaceable_provider_contract():
    plugin = load_processor(ROOT / "plugins" / "model-deepseek" / "processor.py", "model-test")
    assert callable(plugin.DeepSeekResponder.reply)
    assert callable(plugin.DeepSeekResponder.check_balance)
    responder = plugin.DeepSeekResponder.__new__(plugin.DeepSeekResponder)
    responder.client = SimpleNamespace(models=SimpleNamespace(
        list=lambda: SimpleNamespace(data=[SimpleNamespace(id="model-b"),
                                           SimpleNamespace(id="model-a")])
    ))
    responder._models = None
    responder._models_checked_at = 0
    assert responder.list_models() == ["model-a", "model-b"]
    assert responder.set_model("model-b") == "model-b"


def test_product_selection_processor_locks_seo_volume_and_combo_rules():
    plugin = load_processor(ROOT / "plugins" / "chon-san-pham" / "processor.py", "selection-test")
    products = [
        {"product_id": "humic", "title": "HUMIC Can 5 Lít cải tạo đất đạm cá cô đặc kích rễ"},
        {"product_id": "fish1", "title": "Đạm Cá Amino chai 1 lít"},
        {"product_id": "fish5", "title": "Đạm Cá Amino can 5 lít"},
        {"product_id": "combo", "title": "Combo Đạm Cá + Humic"},
    ]
    assert plugin.select_live_product("đạm cá amino 1 lít giá", products, "humic")["product_id"] == "fish1"
    assert plugin.select_live_product("đạm cá amino 5 lít giá", products, "humic")["product_id"] == "fish5"
    assert plugin.select_live_product("đạm cá pha sao", products, "humic")["product_id"] != "combo"
    assert plugin.select_live_product("combo đạm cá humic", products, "humic")["product_id"] == "combo"
    wanted, frequency, total = {"furagin"}, {"furagin": 1}, 10
    head = plugin.evidence_score(wanted, "Furagin trị tuyến trùng", frequency, total)
    tail = plugin.evidence_score(wanted, "Humic " + "cải tạo đất " * 8 + "furagin", frequency, total)
    assert head > tail


def test_crop_names_do_not_collide_with_other_words_after_removing_accents():
    """24/09: "sầu riêng" bỏ dấu thành "sau" và kéo sang "SẠCH SÂU ĐẤT"."""
    plugin = load_processor(ROOT / "plugins" / "chon-san-pham" / "processor.py", "selection-crops")
    products = [
        {"product_id": "sau", "title": "SẠCH SÂU ĐẤT 1KG trị đục thân sùng đất tuyến trùng"},
        {"product_id": "fish", "title": "Đạm Cá Hữu Cơ Amino can 5 lít"},
        {"product_id": "npk", "title": "NPK ra hoa đậu quả dưỡng trái"},
        {"product_id": "tonic", "title": "Nano Tonic phục hồi xoăn lá"},
    ]
    for comment in ("đạm cá bón sầu riêng được không shop", "dam ca bon sau rieng dc ko"):
        assert plugin.select_live_product(comment, products, "npk")["product_id"] == "fish", comment
    assert plugin.select_live_product("thuốc sạch sâu đất giá sao", products, "npk")["product_id"] == "sau"
