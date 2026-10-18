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


# K6/K7/K8/K9 -- hậu kiểm tiền/quà
def _engine(shop_info=None):
    import server as server_module
    eng = object.__new__(server_module.ReplyEngine)
    eng.shop_info = shop_info or {}
    return eng


def _reply(text):
    return {"action": "reply", "reply": text}


LIVE = {"products": [{"product_id": "p1", "title": "Đạm cá 1L", "original_price": "185.000₫"}]}


def test_measurements_are_not_money():
    eng = _engine()
    for text in ("Can này pha được 1.000 lít nước nha", "Bón 1.200 kg/ha", "Đủ cho 2.500 m2",
                 "Trồng 20.000 cây", "Gói 500g", "Pha 2ml/16 lít", "1k lít nước"):
        assert eng._invented_price(_reply(text), LIVE, {}, {}) == "", text


def test_money_in_real_promotion_or_shop_info_is_allowed():
    eng = _engine({"freeship": "Freeship đơn từ 500k"})
    live = {"products": [{**LIVE["products"][0], "promotion": "Mua 2 giảm 20k"}]}
    assert eng._invented_price(_reply("Mua 2 giảm 20k nha anh"), live, {}, {}) == ""
    assert eng._invented_price(_reply("Đơn từ 500k được freeship nha"), live, {}, {}) == ""
    assert eng._invented_price(_reply("Mua 2 giảm 50k nha"), live, {}, {})


def test_new_price_shapes_are_caught():
    eng = _engine()
    for text in ("Dạ 1,5k một gói nha", "Giá 2.5k thôi", "2 can tổng 370 luôn anh"):
        assert eng._invented_price(_reply(text), LIVE, {}, {}), text
    assert eng._invented_price(_reply("Gốc 185k nha anh"), LIVE, {}, {}) == ""


def test_invented_price_after_retry_is_dropped_not_sent():
    eng = _engine()
    out = eng._drop_invented_price(_reply("Pha 20ml/bình nha. 2 can hết 370k luôn anh."), LIVE, {}, {})
    assert out["reply"] == "Pha 20ml/bình nha." and "370" not in out["reply"]
    out = eng._drop_invented_price(_reply("2 can hết 370k luôn anh."), LIVE, {}, {})
    assert out["action"] == "reply" and out["reply"] == eng._SAFE_PRICE_LINE
    assert not eng._price_values(eng._SAFE_PRICE_LINE)


def test_agronomy_percent_is_not_discount():
    eng = _engine()
    for text in ("Thuốc bột 80% pha 20g/bình nha", "Mưa nhiều thì giảm 30% liều nha anh",
                 "Hàm lượng đạm 5% nha"):
        assert eng._invented_promo(_reply(text), {}, {}) == "", text
    assert eng._invented_promo(_reply("Hôm nay shop giảm 30% cho anh nha"), {}, {})


def test_gift_promise_with_trailing_question_is_caught():
    eng = _engine()
    assert eng._invented_promo(_reply("Shop tặng kèm 1 chai nha anh, anh lấy mấy chai ạ?"), {}, {})
    assert eng._invented_promo(_reply("Anh hỏi có tặng kèm không ạ?"), {}, {}) == ""


def test_gift_basis_only_from_promotion_data_and_not_negated():
    eng = _engine({"ghi_chu": "Hiện không có quà tặng"})
    approved = {"thanh_phan_dang_ky": "Đạm 5%", "cong_dung": "tặng sức đề kháng"}
    assert eng._invented_promo(_reply("Shop tặng kèm 1 chai nha"), {}, approved)
    assert eng._invented_promo(_reply("Shop giảm 5% cho anh nha"), {}, approved)
    assert eng._invented_promo(_reply("Shop tặng kèm 1 chai nha"), {"promotion": "Mua 2 tặng 1 chai"}, {}) == ""


# P8 -- sổ tay tình huống không khớp từ khoá thô
def test_playbook_does_not_misfire_on_farming_words():
    import sales_playbook
    assert "doi_tra_hu_hong" not in sales_playbook.match("cây bị hư rễ thì dùng gì")
    assert "ai_hay_nguoi" not in sales_playbook.match("tưới tự động pha được không")
    assert "che_dat_xin_giam" not in sales_playbook.match("cây mắc bệnh nấm")
    assert "cong_kich_lua_dao" not in sales_playbook.match("ko tặng quà hả shop")
    assert "doi_tra_hu_hong" in sales_playbook.match("hàng bị hư rồi shop ơi")
    assert "cong_kich_lua_dao" in sales_playbook.match("shop lừa đảo")
    assert "che_dat_xin_giam" in sales_playbook.match("mắc quá shop")


# H3 -- tên cây theo ranh giới từ
def test_crop_extraction_uses_word_boundaries():
    from customer_memory import extract_customer_facts
    for text in ("shop cam kết hàng thật không", "đạt tiêu chuẩn không", "ngày mai giao được không",
                 "điều kiện đổi trả sao", "camera mờ quá"):
        assert "cay_trong" not in extract_customer_facts(text), text
    assert extract_customer_facts("vườn cam bị vàng lá")["cay_trong"] == "cam"
    assert extract_customer_facts("tôi trồng tiêu 2 ha")["cay_trong"] == "hồ tiêu"
    assert extract_customer_facts("cây mai nhà tôi rụng lá")["cay_trong"] == "mai"
    assert extract_customer_facts("trồng điều ở Bình Phước")["cay_trong"] == "điều"
