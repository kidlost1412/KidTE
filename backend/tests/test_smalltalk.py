import pytest

import smalltalk
from smalltalk import catalog_terms, is_small_talk

POSITIVE = [
    "hello", "Hello", "hello shop", "hi", "hi shop ơi", "chào", "chào shop", "Chào shop ạ", "xin chào",
    "xin chào cả nhà", "chào anh", "chào chị nha", "chao shop", "xin chao", "chao ca nha", "alo", "alo alo",
    "hellooo", "hiii", "ok", "OK", "ok em", "ok em nhé", "oke", "okie", "dạ", "dạ vâng", "da vang",
    "vâng ạ", "ừ", "uh", "um", "rồi", "roi nha", "được", "duoc", "cảm ơn", "cảm ơn shop", "cám ơn shop nha",
    "cam on shop", "cam on", "thanks", "thank you", "tks shop", "haha", "hahaha", "Hahahaha", "hehe", "hihi",
    "kkkk", "kkk", "huhu", "wow", "[laugh]", "[smile]", "[love]", "[cool] [laugh]", "😂", "😂😂😂", "👍", "❤️",
    "🥰🥰", "👏👏 tuyệt", "tuyệt vời", "tuyet voi", "hay quá", "live vui quá", "shop xinh quá", "dễ thương quá",
    "đẹp quá", "ngon", "tốt", "chuc mung shop", "chúc shop may mắn", "tạm biệt", "bye shop",
    "ngủ ngon", "chào buổi sáng", "chao buoi sang", "chào buổi tối shop", "good morning", "anh ơi", "chị ơi",
    "em ơi", "shop ơi", "admin ơi", "hello cả nhà", "oke shop", "ok nha", "haha vui quá", "hay quá shop ơi",
]

NEGATIVE = [
    # giá / khuyến mãi / tồn kho / ship
    "giá bao nhiêu ạ?", "gia bao nhieu", "bao nhiêu tiền", "hello giá bao nhiêu", "chào shop giá sao", "ok giá nhiêu",
    "có khuyến mãi không", "có tặng gì không", "còn hàng không", "hết hàng chưa", "ship bao lâu", "phí ship",
    "giao hàng mấy ngày", "có bán chịu không em", "cod được không", "mã giảm giá đâu",
    # mua / chốt / số lượng
    "ok lấy 2 chai", "ok lay 2 chai", "lấy 1 can", "chốt", "chốt đơn", "ok chốt", "đặt", "đặt 3 chai", "mua",
    "em mua nha", "cho em 2", "2 chai", "5", "ok 3", "dạ lấy", "lay giup em", "ok em lấy",
    # liều / sử dụng / kỹ thuật / cây trồng
    "pha như nào", "1 chai kich rễ pha như nào", "5 lít pha bao nhiêu", "kích rể xịt lên lá đc ko e",
    "sao cà bị xoắn lá", "lá vàng", "cây bị bệnh", "sâu ăn lá", "bón cho sầu riêng", "phun được không",
    "cho em hỏi", "hỏi chút", "em muốn hỏi", "tư vấn giúp em", "có loại nào trị nấm", "phân này bón sao",
    # sản phẩm / tên gọi
    "đạm cá", "kích rễ", "tc01", "humic", "npk", "ok đạm cá", "hello kích rễ", "combo", "chai này", "loại này",
    # câu hỏi / mơ hồ
    "?", "??", "...", "!!", "", "   ", "có", "không", "ko", "hello?", "ok không?", "shop ở đâu", "ở đâu vậy",
    "mấy giờ bán", "ai vậy", "tại sao", "vậy hả", "shop bán", "shop ban", "chúc shop bán đắt",
    # quá dài / lạ
    "hello shop mình bán những gì vậy ạ cho em hỏi với", "chào shop em là khách mới xem live lần đầu",
    "chúc mừng shop ra mắt sản phẩm mới nhiều quà tặng hấp dẫn",
]


@pytest.mark.parametrize("text", POSITIVE)
def test_small_talk_positive(text):
    assert is_small_talk(text), text


@pytest.mark.parametrize("text", NEGATIVE)
def test_small_talk_negative(text):
    assert not is_small_talk(text), text


def test_non_string_and_none_are_not_small_talk():
    assert not is_small_talk(None)
    assert not is_small_talk(123)
    assert not is_small_talk(["hello"])


def test_known_catalog_terms_block_small_talk():
    terms = catalog_terms(["Phân bón Chào Mào Siêu Tốt 1kg", "Kích Rễ TC01"])
    assert "chao" in terms and "kich" in terms
    assert not is_small_talk("chào", terms)
    assert not is_small_talk("tốt", terms)
    assert is_small_talk("ok", terms)  # từ ngắn dưới 3 ký tự không bị chặn
    assert is_small_talk("hello", terms)


def test_classifier_never_raises_on_odd_input():
    class Boom:
        def __str__(self):
            raise RuntimeError("boom")
    assert smalltalk.is_small_talk(Boom()) is False
    assert smalltalk.is_small_talk("\x00\x01") is False


def test_no_diacritics_and_diacritics_agree():
    pairs = [("cảm ơn shop", "cam on shop"), ("chào cả nhà", "chao ca nha"), ("tuyệt vời", "tuyet voi"),
             ("giá bao nhiêu", "gia bao nhieu"), ("ok lấy 2 chai", "ok lay 2 chai")]
    for accented, plain in pairs:
        assert is_small_talk(accented) == is_small_talk(plain), (accented, plain)
