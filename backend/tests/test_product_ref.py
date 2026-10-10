import sqlite3

from product_ref import build_product_ref
from conversation_context import product_reference
from customer_memory import CustomerMemory

CATALOG = [
    {'product_id': '21', 'title': 'Đạm Cá Amino 1L'},
    {'product_id': '22', 'title': 'Đạm Cá Amino 5L'},
    {'product_id': '20', 'title': 'Kích Rễ TC01', 'aliases': ['tc01']},
]


def test_family_without_sku_keeps_candidates():
    ref = build_product_ref('shopee', CATALOG, 'em có đạm cá loại nào')
    assert ref['family_id'] == 'dam ca'
    assert ref['selected_sku'] is None
    assert {c['sku'] for c in ref['candidates']} >= {'21', '22'}


def test_pin_is_observed_not_selected():
    ref = build_product_ref('tiktok', CATALOG, 'giá sao shop',
                            pin={'sku': '20', 'observed_at': 1.0, 'confidence': 0.9})
    assert ref['observed_pin']['sku'] == '20'
    assert ref['selected_sku'] is None


def test_alias_matches_tiktok_and_shopee():
    for platform in ('tiktok', 'shopee'):
        ref = build_product_ref(platform, CATALOG, 'Cho mình hỏi KÍCH RỄ tc01 nhé')
        assert ref['selected_sku'] == '20'
        assert ref['aliases_matched']
    assert product_reference('shopee', CATALOG, 'tc01')['selected_sku'] == '20'


def test_ambiguous_candidates_not_promoted():
    ref = build_product_ref('shopee', CATALOG, 'đạm cá amino')
    assert len(ref['candidates']) >= 2
    assert ref['selected_sku'] is None


def test_customer_fact_persisted_with_source(tmp_path):
    memory = CustomerMemory(tmp_path / 'm.sqlite3', tmp_path / 'j', platform='shopee')
    rec = memory.record_comment('vườn cà phê 2 ha bị vàng lá', platform_user_id='9', display_name='A')
    rows = sqlite3.connect(tmp_path / 'm.sqlite3').execute(
        'SELECT fact_key, source_interaction_id FROM customer_facts').fetchall()
    assert rows and all(r[1] == rec['interaction_id'] for r in rows)
    memory.close()
