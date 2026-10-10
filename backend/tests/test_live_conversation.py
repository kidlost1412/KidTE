from live_conversation import resolve_question


def test_invalid_pending_fields_cannot_reopen_a_completed_answer():
    for fields in ('variant', ['made_up'], {'variant': True}, [None, 1]):
        turns = [{'comment': 'giá bao nhiêu', 'response': '80.000đ nha.',
                  'delivery_status': 'sent', 'conversation_state': {'awaiting_fields': fields}}]
        assert resolve_question('100ml', turns) == ('100ml', None)


def test_only_pending_question_is_inherited():
    pending = [{'comment': '10 chai kích rễ tặng gì', 'response': 'Anh hỏi loại nào?', 'delivery_status': 'sent', 'conversation_state': {'awaiting_fields': ['product']}}]
    assert resolve_question('TC01', pending)[0] == '10 chai kích rễ tặng gì — TC01'
    assert resolve_question('TC01 giá bao nhiêu', pending)[0] == 'TC01 giá bao nhiêu'
    assert resolve_question('cảm ơn', pending)[1] is None
    pending[0]['response'] = 'Mua 10 tặng 1 nha.'
    pending[0]['conversation_state'] = {'awaiting_fields': []}
    assert resolve_question('TC01', pending)[1] is None
    pending[0]['delivery_status'] = 'none'
    assert resolve_question('TC01', pending)[1] is not None


def test_no_search_past_the_latest_turn():
    turns = [
        {'comment': 'giá bao nhiêu', 'response': '120k nha.', 'delivery_status': 'sent'},
        {'comment': '10 chai tặng gì', 'response': 'Loại nào?', 'delivery_status': 'sent'},
    ]
    assert resolve_question('TC01', turns)[1] is None


def test_more_than_one_clarification_keeps_the_original_intent():
    turns = [{'comment': 'TC01', 'resolved_question': '10 chai tặng gì — TC01',
              'response': 'Anh lấy chai 100ml đúng không?', 'delivery_status': 'sent', 'conversation_state': {'awaiting_fields': ['variant']}}]
    assert resolve_question('100ml', turns)[0] == '10 chai tặng gì — TC01 — 100ml'


def test_unaccented_new_question_does_not_resume_old_promotion():
    turns = [{'comment': '10 chai tặng gì', 'response': 'Mình hỏi loại nào?', 'delivery_status': 'sent'}]
    assert resolve_question('TC01 gia bao nhieu', turns) == ('TC01 gia bao nhieu', None)
    assert resolve_question('TC01 tang gi', turns) == ('TC01 tang gi', None)


def test_product_range_question_starts_new_topic():
    turns = [{'comment': 'đạm cá có giá sao em', 'response': 'Anh hỏi loại nào?',
              'delivery_status': 'sent'}]
    assert resolve_question('em có đạm cá loại nào', turns) == ('em có đạm cá loại nào', None)


def test_rhetorical_question_does_not_reopen_answered_price():
    turns = [{'comment': 'TC01 giá bao nhiêu', 'response': '80.000đ, mình đặt luôn nhé?',
              'delivery_status': 'sent', 'conversation_state': {'awaiting_fields': []}}]
    assert resolve_question('100ml', turns) == ('100ml', None)


def test_explicit_awaiting_field_resumes_without_question_mark():
    turns = [{'comment': 'TC01 giá bao nhiêu', 'response': 'Mình chọn dung tích để em báo đúng giá nha',
        'delivery_status': 'sent', 'conversation_state': {'awaiting_fields': ['variant']}}]
    assert resolve_question('100ml', turns)[0] == 'TC01 giá bao nhiêu — 100ml'
