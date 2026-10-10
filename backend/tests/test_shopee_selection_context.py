from types import SimpleNamespace

from shopee_commerce import select_product


def owner_with_ambiguous_selector(product):
    selector = SimpleNamespace(explain_selection=lambda *args: {
        'reason': 'ambiguous_keeps_pinned', 'product': product, 'ranked': []})
    return SimpleNamespace(processors=SimpleNamespace(modules={'chon-san-pham': selector}))


def test_new_question_cannot_inherit_old_customer_product_or_pin():
    product = {'product_id': 'tc01', 'title': 'Kích Rễ TC01'}
    turns = [{'comment': 'TC01 giá bao nhiêu', 'product_title': product['title'],
              'delivery_status': 'sent', 'conversation_state': {'awaiting_fields': []}}]
    assert select_product(owner_with_ambiguous_selector(product), 'đạm cá có mấy loại',
                          [product], 'tc01', turns) == (None, 'ambiguous_product')


def test_explicit_pointing_can_use_observed_pin():
    product = {'product_id': 'tc01', 'title': 'Kích Rễ TC01'}
    assert select_product(owner_with_ambiguous_selector(product), 'chai này dùng sao',
                          [product], 'tc01', [])[0] == product


def test_pending_variant_can_inherit_customer_product():
    product = {'product_id': 'tc01', 'title': 'Kích Rễ TC01'}
    turns = [{'comment': 'TC01 giá bao nhiêu', 'product_title': product['title'],
              'delivery_status': 'sent', 'conversation_state': {'awaiting_fields': ['variant']}}]
    assert select_product(owner_with_ambiguous_selector(product), '100ml',
                          [product], '', turns) == (product, 'customer_conversation')
