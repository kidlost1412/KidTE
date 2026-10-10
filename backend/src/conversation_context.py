"""Shared approved shop context; platform price/catalog stay in each engine."""
from __future__ import annotations

import math
from knowledge import clean


EXAMPLE_POLICY = ('Các ví dụ được chủ shop duyệt là căn cứ học ý và giọng, không chép nguyên câu. '
    'Giá, liều, quy cách và điều kiện hiện hành của sàn luôn ưu tiên hơn ví dụ. '
    'Không lấy giá hoặc ID SKU từ ví dụ để thay catalog hiện tại.')


def platform_video_context(owner, platform, expected_video_id=''):
    getter = getattr(owner, 'current_video_position', None)
    timelines = getattr(owner, 'video_timelines', None)
    if not callable(getter) or not callable(timelines):
        return {}
    position = getter(platform)
    video_id = position.get('video_id', '')
    if position.get('available') is not True or not video_id or (expected_video_id and video_id != expected_video_id):
        return {}
    seconds = position.get('position_s')
    if isinstance(seconds, bool) or not isinstance(seconds, (float, int)) or not math.isfinite(seconds):
        return {}
    timeline = timelines().get('timelines', {}).get(video_id, {})
    contexts = [row for row in timeline.get('script_contexts', []) if isinstance(row, dict)
        and isinstance(row.get('at_ms'), (float, int)) and not isinstance(row.get('at_ms'), bool)
        and math.isfinite(row['at_ms']) and row['at_ms'] <= seconds * 1000]
    current = max(contexts, key=lambda row: row['at_ms']) if contexts else {}
    facts = current.get('facts', [])
    return {'platform': platform, 'video_id': video_id, 'position_s': seconds,
            'lock_id': position.get('lock_id', ''), 'revision': position.get('revision', 0),
            'current_topic': clean(current.get('topic'), 240),
            'reviewed_script_facts': [clean(row, 260) for row in facts[:6]
                if isinstance(row, str) and clean(row, 260)] if isinstance(facts, list) else []}


def shared_shop_context(owner, comment, product_id, persona_id='', *, mark_used=True):
    result = {'thong_tin_shop': getattr(owner, 'shop_info', {}),
              'approved_example_policy': EXAMPLE_POLICY}
    workspace = getattr(owner, 'workspace', None)
    if workspace is not None:
        result['operator_approved_reply_examples'] = workspace.reviewed_examples(comment,
            workspace._question_type(comment), product_id, persona_id=persona_id, mark_used=mark_used)
    return result


def product_reference(platform, catalog, comment, pin=None, prior_state=None, customer_facts=None):
    """Additive product_ref for both platforms; callers keep their legacy keys."""
    from product_ref import build_product_ref
    return build_product_ref(platform, catalog, comment, pin, prior_state, customer_facts)
