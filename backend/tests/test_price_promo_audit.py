"""Hậu kiểm giá/quà — rà soát 18/10 (R1–R8, R11, R12): bảng câu lọt và câu chặn nhầm."""
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
ROOT = SRC.parents[1]
sys.path.insert(0, str(SRC))

from processor_loader import load_processor  # noqa: E402

DEEPSEEK = load_processor(ROOT / "plugins" / "model-deepseek" / "processor.py", "deepseek-audit-test")


def _engine(shop_info=None):
    import server as server_module
    eng = object.__new__(server_module.ReplyEngine)
    eng.shop_info = shop_info or {}
    return eng


def _values(text):
    import server as server_module
    return server_module.ReplyEngine._price_values(text)


# --- R1/R3: số tiền phải nhận ra -------------------------------------------------
@pytest.mark.parametrize("text, amount", [
    ("Combo 3 chai 1 triệu 6 nha", 1_600_000),
    ("mua 2 chai 1 triệu 110 nha", 1_110_000),
    ("2 chai 1 triệu 110", 1_110_000),
    ("Giá 1 triệu rưỡi anh", 1_500_000),
    ("Còn 1tr2 thôi", 1_200_000),
    ("Chỉ 2 triệu 5 nha", 2_500_000),
    ("Giá 1 triệu nha", 1_000_000),
    ("1.6 triệu nha", 1_600_000),
    ("Giá 450.000 hợp lý nha", 450_000),
    ("450.000 cần anh chốt", 450_000),
    ("450.000 bình thường thôi", 450_000),
    ("450.000 gọi em nha", 450_000),
    ("450.000 bảo hành", 450_000),
    ("Giá 450.000 sao anh", 450_000),
    ("2 can tổng 370 luôn anh", 370_000),
    ("hết 1110 nha", 1110),
    ("tổng 1110k", 1_110_000),
])
def test_money_detected(text, amount):
    assert amount in _values(text), (text, _values(text))


@pytest.mark.parametrize("text, extra", [
    ("tổng 1110k", 1110),
    ("tổng 12tr", 12_000),
])
def test_no_spurious_number(text, extra):
    assert extra not in _values(text)


# --- R2/R3: không phải tiền -------------------------------------------------------
@pytest.mark.parametrize("text", [
    "Hết 10 ngày là thấy hiệu quả",
    "tổng 12 tháng mới thu",
    "tổng 30 phút là ngấm",
    "hết 50 cái",
    "hết 20 ngày phun lại",
    "Trồng 20.000 cây",
    "Can này pha được 1.000 lít nước nha",
    "1k lít nước",
    "Đủ 2.000 gốc",
    "20.000 bao phân",
    "pha 1.000 lit nuoc",
])
def test_not_money(text):
    vals = _values(text)
    assert not vals, (text, vals)


# --- R4/R5: hứa quà lọt ---------------------------------------------------------
LEAKS = [
    "Chưa có khuyến mãi nhưng em tặng kèm 1 chai nha",
    "Shop không có freeship nhưng em tặng thêm 1 chai",
    "Không có giảm giá mà em tặng 1 chai nha",
    "Giảm tới 20% nha anh",
    "Giảm hẳn 20% luôn",
    "Ưu đãi 20% cho anh",
    "Hôm nay giảm 20 phần trăm",
    "Em gửi kèm 1 chai dùng thử",
    "Em biếu anh 1 chai",
    "Em khuyến mãi thêm 1 chai",
    "Shop gửi tặng anh 1 gói",
    "Giảm 20% thuốc nha anh",
]


@pytest.mark.parametrize("text", LEAKS)
def test_invented_promo_caught(text):
    eng = _engine()
    assert eng._promo_sentences(text, eng._promo_source({}, {})), text


# --- không chặn nhầm ------------------------------------------------------------
FALSE_POSITIVES = [
    "Giảm 20% liều khi cây còn nhỏ",
    "Giảm 20% lượng nước tưới",
    "Hiện chưa có khuyến mãi cho món này ạ",
    "Shop không có freeship ạ",
    "Anh muốn có quà tặng không ạ?",
    "Em kiểm tra quà tặng rồi báo anh nha",
    "Cây tăng 20% năng suất",
    "Em gửi kèm hướng dẫn sử dụng",
]


@pytest.mark.parametrize("text", FALSE_POSITIVES)
def test_no_false_promo(text):
    eng = _engine()
    assert not eng._promo_sentences(text, eng._promo_source({}, {})), text


def test_real_gift_in_data_still_basis():
    eng = _engine()
    source = eng._promo_source({"promotion": "Hết hàng lẻ, tặng 1 chai khi mua 2"}, {})
    assert not eng._promo_sentences("Mua 2 em tặng 1 chai nha", source)
    source = eng._promo_source({"promotion": "Không có quà tặng"}, {})
    assert eng._promo_sentences("Em tặng kèm 1 chai nha", source)


def test_percent_in_promotion_allowed():
    eng = _engine()
    source = eng._promo_source({"promotion": "Giảm 20 phần trăm cho đơn đầu"}, {})
    assert not eng._promo_sentences("Giảm 20% nha anh", source)


# --- R7: số tiền khuyến mãi chỉ của đúng SKU ---------------------------------------
def test_allowed_prices_only_current_sku_promotion():
    eng = _engine()
    a = {"product_id": "a", "original_price": "185.000₫", "promotion": "Mua 2 giảm 20k"}
    b = {"product_id": "b", "original_price": "300.000₫", "promotion": "Mua 3 giảm 50k"}
    live = {"products": [a, b]}
    reply = {"action": "reply", "reply": "Mua 2 giảm 50k nha"}
    assert eng._invented_price(reply, live, {}, {}, a)
    assert eng._invented_price(reply, live, {}, {}, b) == ""
    assert eng._invented_price({"action": "reply", "reply": "Giá 300.000đ nha"}, live, {}, {}, a) == ""


# --- R11 -------------------------------------------------------------------------
@pytest.mark.parametrize("text", ["co chuong trinh gi khong", "co qua gi khong shop",
                                  "giam may phan tram vay", "giảm bao nhiêu vậy shop"])
def test_promo_intent_plain(text):
    from knowledge import KnowledgeBase
    assert KnowledgeBase.intents(text)["promotion"], text


# --- R12 -------------------------------------------------------------------------
def test_nickname_repeated_after_tag():
    strip = DEEPSEEK._strip_nickname
    assert strip("@Hoa Dạ anh Hoa ơi, pha 20ml nha", "Hoa") == "Dạ anh pha 20ml nha"
    assert strip("@Hoa Hoa ơi pha 20ml nha", "Hoa") == "pha 20ml nha"
    assert strip("Dạ anh Hoa ơi, pha 20ml nha", "Hoa") == "Dạ anh Hoa ơi, pha 20ml nha"
    assert strip("@5 Dạ pha 5 ml/bình", "5") == "Dạ pha 5 ml/bình"


# --- R8 --------------------------------------------------------------------------
def test_shopee_recommendation_does_not_borrow_pin():
    from types import SimpleNamespace
    from shopee_commerce import select_product
    products = [{"product_id": "a1", "shop_id": 1, "title": "Nano Đồng chai 100ml"},
                {"product_id": "b2", "shop_id": 1, "title": "Đạm Cá Hữu Cơ can 5 lít"}]
    for text in ("cây mai bị vàng lá dùng gì", "nên dùng loại nào", "có thuốc gì trị rệp không"):
        chosen, reason = select_product(SimpleNamespace(), text, products, "a1", [])
        assert chosen is None, (text, reason)
    chosen, _ = select_product(SimpleNamespace(), "pha mấy lít", products, "a1", [])
    assert chosen["product_id"] == "a1"


# --- M.6: số tiền viết bằng chữ + "giá ... gốc" ------------------------------------
@pytest.mark.parametrize("text, expected", [
    ("Giá ba trăm nghìn nha", {300_000}),
    ("Dạ một trăm tám lăm nghìn ạ", {185_000}),
    ("Combo hai triệu rưỡi nha", {2_500_000}),
    ("Chai này năm chục thôi", {50_000}),
    ("Giá trăm rưỡi nha anh", {150_000}),
    ("Bốn chục nghìn một gói", {40_000}),
    ("năm mươi nghìn đồng", {50_000}),
    ("hai trăm tư nghìn", {240_000}),
    ("mười lăm nghìn", {15_000}),
    ("ba trăm linh năm nghìn", {305_000}),
    ("hai mươi mốt nghìn", {21_000}),
    ("một triệu sáu trăm nghìn", {1_600_000}),
    ("Combo hai triệu hai nha", {2_200_000}),
    ("Giá từ ba trăm nghìn", {300_000}),
    ("Một trăm k nha", {100_000}),
    ("Giá 450.000 gốc nha", {450_000}),
    ("Giá 450k gốc nha", {450_000}),
    ("Lấy hai chai nha", set()),
    ("Dùng ba ngày là thấy", set()),
    ("Năm chục cây thì pha hai can", set()),
    ("Năm sau em về hàng", set()),
    ("Tư vấn ba ngày liền", set()),
    ("Ba trăm cây bón đều", set()),
    ("Bón 450.000 gốc cây được", set()),
    ("Giá rẻ, 450.000 gốc đã bón", set()),
    ("Hơn ba nghìn người đã dùng", set()),
    ("Hai triệu lượt xem rồi", set()),
])
def test_price_values_spelled_out(text, expected):
    assert _values(text) == expected
