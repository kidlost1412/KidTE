"""Đợt sửa AI 18/10 (rà soát 17/10): bớt chặn nhầm, bớt hỏi lại vô hồn, chặn chắc giá/quà bịa."""
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
ROOT = SRC.parents[1]
sys.path.insert(0, str(SRC))

from processor_loader import load_processor  # noqa: E402

PRICE = load_processor(ROOT / "plugins" / "chinh-sach-gia" / "processor.py", "price-fix1-test")
DEEPSEEK = load_processor(ROOT / "plugins" / "model-deepseek" / "processor.py", "deepseek-fix1-test")


# K2 -- nickname chỉ bị xoá khi là lời gọi tên
def test_nickname_digit_or_common_word_not_removed_mid_sentence():
    strip = DEEPSEEK._strip_nickname
    assert strip("Dạ can 5 lít pha 5 ml/bình nha", "5") == "Dạ can 5 lít pha 5 ml/bình nha"
    assert strip("Dạ Anh pha 2ml/lít nha", "Anh") == "Dạ Anh pha 2ml/lít nha"
    assert strip("Phân này bón gốc nha", "Phân") == "Phân này bón gốc nha"
    assert strip("Dạ miền Nam bón 20g", "Nam") == "Dạ miền Nam bón 20g"


def test_nickname_removed_when_addressing():
    strip = DEEPSEEK._strip_nickname
    assert strip("@Tư Lúa dạ pha 20ml nha", "Tư Lúa") == "dạ pha 20ml nha"
    assert strip("Nam ơi, can này pha 20ml nha", "Nam") == "can này pha 20ml nha"
    assert strip("Dạ Nam, pha 20ml nha", "Nam") == "Dạ pha 20ml nha"
    assert strip("Chào Lan nha.", "Lan") == "Chào nha."


# K3 -- số lượng không phải giá khách báo
def test_quantity_is_not_customer_price():
    for text in ("còn 50 bao không shop", "đang cần 20 chai", "hiện có 30 can",
                 "lấy 3 can", "cần 20 nha", "can 5 lít giá nhiêu"):
        assert PRICE.customer_price_signal(text, {}) == {}, text


def test_real_customer_price_still_detected():
    for text in ("mình thấy 99k", "giá 150 hả shop", "em thấy còn 185k", "đang 150 nghìn"):
        assert PRICE.customer_price_signal(text, {}), text


# K1 -- bớt câu mẫu "đang hỏi sản phẩm hoặc dung tích nào"
TEMPLATE = "Anh/chị đang hỏi sản phẩm hoặc dung tích nào ạ?"


def test_clarify_listing_shortens_titles_instead_of_template():
    from dose_selection import clarify_listing
    titles = ["Phân bón lá sinh học cao cấp siêu ra rễ cực mạnh chai 1 lít chính hãng",
              "Phân bón lá sinh học cao cấp siêu ra rễ cực mạnh can 5 lít chính hãng",
              "Thuốc trừ nấm bệnh sinh học thế hệ mới gói 100 gram nhập khẩu"]
    out = clarify_listing(titles, 60, TEMPLATE, 150)
    assert out != TEMPLATE and "Phân bón" in out


def test_shopee_real_selector_keeps_pin_for_generic_questions():
    from types import SimpleNamespace
    from shopee_commerce import select_product
    owner = SimpleNamespace()
    products = [{"product_id": "a1", "shop_id": 1, "title": "Kích Rễ TC01 chai 100ml"},
                {"product_id": "b2", "shop_id": 1, "title": "Đạm Cá Hữu Cơ can 5 lít"}]
    for text in ("pha mấy lít", "còn hàng không", "lấy 3", "bón lúc nào"):
        chosen, reason = select_product(owner, text, products, "a1", [])
        assert chosen and chosen["product_id"] == "a1", (text, reason)
    chosen, _ = select_product(owner, "pha mấy lít", products[:1], "", [])
    assert chosen["product_id"] == "a1"


def test_clarify_text_telling_customer_to_tap_price_is_ok_without_price():
    from types import SimpleNamespace
    from shopee_commerce import price_violation
    owner = SimpleNamespace()
    assert price_violation(owner, "Anh nhắn shop hoặc bấm giỏ xem giá giúp em nha", {}, {}) == ""
    assert price_violation(owner, "Bấm giỏ xem giá nha", {"quotable_price": "185.000đ",
                                                          "quotable_price_vnd": 185000}, {})


# P1 -- ý định giá/khuyến mãi so trên chữ có dấu trước
def test_intents_respect_diacritics():
    from knowledge import KnowledgeBase
    def it(text):
        found = KnowledgeBase.intents(text)
        return found["price"], found["promotion"]
    assert it("phân này tăng đậu trái không") == (False, False)
    assert it("tăng trưởng mạnh không") == (False, False)
    assert it("dùng cho gia đình được không") == (False, False)
    assert it("mua 2 chai") == (False, False)
    assert it("giá trị dinh dưỡng cao không") == (False, False)
    assert it("có tặng gì không shop") == (False, True)
    assert it("có quà không") == (False, True)
    assert it("gia bao nhiêu") == (True, False)
    assert it("gia bao nhieu shop") == (True, False)
    assert it("co qua tang ko") == (False, True)
    assert it("tang dau trai khong") == (False, False)
