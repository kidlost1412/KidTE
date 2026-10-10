from types import SimpleNamespace
from conversation_context import platform_video_context, shared_shop_context


def test_shopee_context_comes_from_its_backend_position_not_other_platform():
    calls = []
    owner = SimpleNamespace(current_video_position=lambda platform: calls.append(platform) or
        {'available': True, 'video_id': 'shopee-v', 'position_s': 12, 'lock_id': 'lock'},
        video_timelines=lambda: {'timelines': {'shopee-v': {'script_contexts': [
            {'at_ms': 0, 'topic': 'Intro', 'facts': ['First']},
            {'at_ms': 10000, 'topic': 'Đạm cá', 'facts': ['Only approved']},
            {'at_ms': 20000, 'topic': 'Future', 'facts': ['Must not appear']},
            {'at_ms': float('nan'), 'topic': 'Invalid'}]}}})
    context = platform_video_context(owner, 'shopee', 'shopee-v')
    assert calls == ['shopee']
    assert context['current_topic'] == 'Đạm cá'
    assert context['reviewed_script_facts'] == ['Only approved']
    assert platform_video_context(owner, 'shopee', 'old-video') == {}


def test_uncertain_backend_video_is_not_script_evidence():
    owner = SimpleNamespace(current_video_position=lambda platform: {'available': False,
        'video_id': 'v', 'state': 'uncertain'}, video_timelines=lambda: (_ for _ in ()).throw(AssertionError()))
    assert platform_video_context(owner, 'shopee') == {}


def test_shared_context_keeps_platform_product_key_and_does_not_supply_prices():
    calls = []
    owner = SimpleNamespace(shop_info={'address': 'Approved address'}, workspace=SimpleNamespace(
        _question_type=lambda text: 'dose', reviewed_examples=lambda *args, **kwargs:
            calls.append((args, kwargs)) or [{'approved_answer': 'approved'}]))
    result = shared_shop_context(owner, 'dùng sao', 'shopee:20', 'operator')
    assert calls[0][0] == ('dùng sao', 'dose', 'shopee:20')
    assert calls[0][1] == {'persona_id': 'operator', 'mark_used': True}
    assert result['thong_tin_shop'] == owner.shop_info
    assert 'price' not in result and 'current_product' not in result
