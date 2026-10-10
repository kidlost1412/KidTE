import sys
from pathlib import Path


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

from import_company_website import (  # noqa: E402
    TextExtractor, normalize_product, packages, split_sections,
)


def test_inline_section_heading_keeps_the_value_on_the_same_line():
    parsed = split_sections(
        "Thành phần:\nN: 10%\nCông dụng: Bung rễ\n"
        "Hướng dẫn sử dụng: Pha 50ml cho bình 20 lít"
    )
    assert parsed["ingredients"] == ["N: 10%"]
    assert parsed["uses"] == ["Bung rễ"]
    assert parsed["directions"] == ["Pha 50ml cho bình 20 lít"]


def test_footer_is_removed_before_it_can_become_a_direction():
    parser = TextExtractor()
    parser.feed(
        "<p>Hướng dẫn sử dụng:</p><p>Pha 20ml</p>"
        "<p>Nông Dược Toàn Cầu – Đi Đầu Chất Lượng</p>"
        "<p>https://nongduoctoancaugroup.com</p>"
    )
    parsed = split_sections(parser.text())
    assert parsed["directions"] == ["Pha 20ml"]


def test_package_normalization_does_not_turn_100ml_into_1ml():
    assert packages("chai 100ml và can 5 lít") == ["100ml", "5lit"]


def test_normalized_product_keeps_featured_image_and_product_category():
    normalized = normalize_product({
        "id": 1,
        "slug": "demo",
        "link": "https://example.test/demo",
        "title": {"rendered": "Demo 100ml"},
        "content": {"rendered": (
            "<p>Thành phần:</p><p>N 10%</p><p>Công dụng:</p><p>Xanh lá</p>"
            "<p>Hướng dẫn sử dụng:</p><p>Pha 10ml</p>"
        )},
        "_embedded": {
            "wp:featuredmedia": [{"source_url": "https://example.test/a.jpg",
                                    "alt_text": "Ảnh nhãn"}],
            "wp:term": [[], [{"taxonomy": "product_cat", "name": "Dinh dưỡng"}]],
        },
    })
    assert normalized["featured_image_url"] == "https://example.test/a.jpg"
    assert normalized["featured_image_alt"] == "Ảnh nhãn"
    assert normalized["categories"] == ["Dinh dưỡng"]
