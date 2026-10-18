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
