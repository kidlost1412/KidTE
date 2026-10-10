import json
from pathlib import Path

import pytest

from live_conversation import conversation_state, resolve_question

DIR = Path(__file__).parent / 'fixtures' / 'conversations'
IDS = json.loads((DIR / 'index.json').read_text(encoding='utf-8'))['ids']
INTENTS = {'price', 'dose', 'usage', 'stock', 'shipping', 'compare', 'follow-up', 'correction', 'greeting', 'forget-me'}


class FakeModel:
    """Returns the intent recorded in the fixture; never calls a real model."""
    def __init__(self, conv):
        self.intents = {t['comment']: t['expect']['intent'] for t in conv['turns']}

    def classify(self, comment):
        return self.intents[comment]


def load(cid):
    return json.loads((DIR / f'{cid}.json').read_text(encoding='utf-8'))


def test_fixture_count_is_40():
    files = sorted(p.stem for p in DIR.glob('*.json') if p.name != 'index.json')
    assert len(files) == 40 and files == sorted(IDS)
    assert {load(i)['platform'] for i in IDS} == {'tiktok', 'shopee'}
    assert {t['expect']['intent'] for i in IDS for t in load(i)['turns']} <= INTENTS


@pytest.mark.parametrize('cid', IDS)
def test_fixture_context_invariants(cid):
    conv = load(cid)
    model = FakeModel(conv)
    history = []  # newest first, as resolve_question expects
    for turn in conv['turns']:
        exp = turn['expect']
        resolved, inherited = resolve_question(turn['comment'], history)
        assert model.classify(turn['comment']) == exp['intent']
        assert exp['intent'] in INTENTS
        for needle in exp['resolved_question_contains']:
            assert needle in resolved, (needle, resolved)
        for banned in exp['must_not_contain']:
            assert banned not in resolved, (banned, resolved)
        assert exp['original_preserved'] is True
        record = {'comment': turn['comment'], 'resolved_question': resolved,
                  'response': turn['response'], 'delivery_status': turn['delivery_status'],
                  'conversation_state': conversation_state(turn['awaiting_fields'])}
        history.insert(0, record)
        # original text is stored verbatim; the resolved question is a separate field
        assert record['comment'] == turn['comment']
        if inherited is not None:
            assert resolved != turn['comment']
            assert turn['comment'] in resolved
        else:
            assert resolved == turn['comment']
