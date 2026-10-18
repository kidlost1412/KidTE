from types import SimpleNamespace

from shopee_commerce import select_product


def owner_with_ambiguous_selector(product, ranked=None):
    selector = SimpleNamespace(explain_selection=lambda *args: {
        'reason': 'ambiguous_keeps_pinned', 'product': product, 'ranked': ranked or []})
    return SimpleNamespace(processors=SimpleNamespace(modules={'chon-san-pham': selector}))


def test_new_question_cannot_inherit_old_customer_product_or_pin():
    product = {'product_id': 'tc01', 'title': 'Kích Rễ TC01'}
    turns = [{'comment': 'TC01 giá bao nhiêu', 'product_title': product['title'],
              'delivery_status': 'sent', 'conversation_state': {'awaiting_fields': []}}]
    other = {'product_id': 'dc1', 'title': 'Đạm Cá 1L'}
    # Câu có chữ khớp riêng SP khác (điểm cao hơn SP ghim) -> không mượn SP cũ/SP ghim.
    ranked = [{'product_id': 'dc1', 'score': 0.9}, {'product_id': 'tc01', 'score': 0}]
    assert select_product(owner_with_ambiguous_selector(product, ranked), 'đạm cá có mấy loại',
                          [product, other], 'tc01', turns) == (None, 'ambiguous_product')


def test_single_listed_product_is_used_without_clarify():
    # Rà soát 17/10 K1: chỉ có 1 SKU thì không hỏi lại "sản phẩm nào".
    product = {'product_id': 'tc01', 'title': 'Kích Rễ TC01'}
    assert select_product(owner_with_ambiguous_selector(product), 'pha mấy lít',
                          [product], '', []) == (product, 'single_product')


def test_pinned_product_answers_question_that_names_nothing_else():
    product = {'product_id': 'tc01', 'title': 'Kích Rễ TC01'}
    other = {'product_id': 'dc1', 'title': 'Đạm Cá 1L'}
    ranked = [{'product_id': 'tc01', 'score': 0}, {'product_id': 'dc1', 'score': 0}]
    for text in ('pha mấy lít', 'còn hàng không', 'lấy 3', 'dùng cho sầu riêng được không'):
        assert select_product(owner_with_ambiguous_selector(product, ranked), text,
                              [product, other], 'tc01', []) == (product, 'currently_pinned'), text


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
