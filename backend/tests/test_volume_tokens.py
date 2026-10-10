from knowledge import KnowledgeBase, _volume_tokens
import pytest


def test_integer_package_sizes_do_not_lose_trailing_zeroes():
    assert _volume_tokens('chai 100 ml') == {'100ml'}
    assert _volume_tokens('chai 10 ml') == {'10ml'}
    assert _volume_tokens('can 20 lit') == {'20lit'}
    assert _volume_tokens('goi 500 g') == {'500gr'}


def test_only_decimal_fraction_zeroes_are_trimmed():
    assert _volume_tokens('can 5,00 lit') == {'5lit'}
    assert _volume_tokens('chai 0.50 l') == {'0.5lit'}
    assert _volume_tokens('chai 100.00 ml') == {'100ml'}


@pytest.mark.parametrize('comment,intent', [
    ('gia TC01 bao nhieu', 'price'), ('10 chai tang gi', 'promotion'),
    ('pha chung humic duoc khong', 'mixing'), ('lieu dung sao', 'dose'),
    ('moi trong dung duoc chua', 'after_planting'),
])
def test_intents_accept_unaccented_customer_messages(comment, intent):
    assert KnowledgeBase.intents(comment)[intent]
