import pytest

from live_conversation import resolve_question

PENDING = [{'comment': 'giá bao nhiêu', 'resolved_question': 'đạm cá giá bao nhiêu', 'delivery_status': 'failed'}]


@pytest.mark.parametrize('text', ['chào shop', 'Chào shop ạ', 'ok em nhé', 'cảm ơn shop', 'dạ vâng'])
def test_greeting_or_thanks_does_not_reopen_pending_question(text):
    assert resolve_question(text, PENDING) == (text, None)


def test_short_followup_still_inherits_pending_question():
    resolved, inherited = resolve_question('loại 1 lít', PENDING)
    assert inherited is not None
