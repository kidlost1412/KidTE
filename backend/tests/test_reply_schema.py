import reply_schema
import fact_revision

ROWS = {'1': {'title': 'a', 'price': 1}}


def test_missing_fields_defaulted():
    s = reply_schema.normalize({'reply': 'Dạ ok'})
    assert s['ok'] and s['messages'] == ['Dạ ok'] and s['evidence'] == [] and s['fields'] == {}
    assert s['awaiting_fields'] == [] and set(s['depends_on']) == set(reply_schema.DEPENDS_KEYS)
    assert reply_schema.normalize({'messages': ['x']})['messages'] == ['x']


def test_invalid_evidence_rejected():
    # Trích dẫn hỏng bị loại (có cờ) nhưng câu trả lời vẫn giữ.
    for bad in ('x', [1], [{'source': '', 'ref': 'a'}], [{'source': 's'}]):
        result = reply_schema.normalize({'messages': ['x'], 'evidence': bad})
        assert result['ok'] and result['evidence'] == [] and result['evidence_dropped']
        assert result['messages'] == ['x']
    assert reply_schema.normalize({'messages': ['x'], 'evidence': [{'source': 'price', 'ref': 3}]})['ok']


def test_stale_depends_triggers_single_recompose():
    calls = []
    rows = {'1': {'title': 'a', 'price': 1}}

    def compose():
        calls.append(1)
        return reply_schema.normalize({'messages': ['x'], 'depends_on': {'video_rev': 1}})
    state = {'rows': {'1': {'title': 'a', 'price': 2}}}

    def stored_now(schema):
        stored = fact_revision.build(ROWS if len(calls) == 1 else state['rows'], product_ids=['1'])
        return stored, fact_revision.current(stored, state['rows'])
    schema, status = reply_schema.settle(compose, stored_now)
    assert len(calls) == 2 and status == 'ok'


def test_second_stale_marks_needs_review():
    calls = []

    def compose():
        calls.append(1)
        return reply_schema.normalize({'messages': ['x']})
    changed = {'1': {'title': 'a', 'price': 9}}

    def stored_now(schema):
        stored = fact_revision.build(ROWS, product_ids=['1'])
        return stored, fact_revision.current(stored, changed)
    schema, status = reply_schema.settle(compose, stored_now)
    assert len(calls) == 2 and status == 'needs_review'
