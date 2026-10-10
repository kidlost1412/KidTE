"""X1: A06 schema wired on both platforms (depends_on, evidence, one recompose) + D01 TikTok dose merge."""
import json
import unittest
import unittest.mock
from types import SimpleNamespace

import pytest

import dose_rules
import reply_schema
import server as server_module
from customer_memory import CustomerMemory
from knowledge import KnowledgeBase
from server import LiveCatalogStore, MissingReporter, ReplyEngine
from shopee_ai import ShopeeAiEngine
import test_server as ts
import test_shopee_ai as tsa

ROOM = 'room-1'
EVIDENCE = [{'source': 'catalog', 'ref': 'p1'}, {'source': 'bad'}]  # second one is malformed, dropped


def schema_reply(**extra):
    return {'action': 'reply', 'reply': 'Dạ, câu trả lời đủ dữ kiện ạ.', 'intent': 'usage',
            'evidence': EVIDENCE, 'depends_on': {'video_rev': 'v1'}, **extra}


class TikTokSchemaTests(unittest.TestCase):
    setUp = ts.StandaloneServerTests.setUp
    tearDown = ts.StandaloneServerTests.tearDown
    _sync = ts.StandaloneServerTests._sync

    def prepare(self):
        patcher = unittest.mock.patch.dict('os.environ', {'KIDTE_FACT_REVISION_CHECK': '1'})
        patcher.start()
        self.addCleanup(patcher.stop)
        real_sync = self.catalog.sync
        self.catalog.sync = lambda snap: (self.catalog.snapshot if not (snap or {}).get('products')
                                          and self.catalog.snapshot else real_sync(snap))
        self.engine.heartbeat({'platform': 'tiktok', 'session_id': ROOM,
                               'live_running': True, 'ai_enabled': True, 'ai_ready': True})
        self.responder.reply = lambda *a, **k: (self.responder.calls.append(a) or schema_reply())

    def ask(self, text, cid):
        return self.engine.queued_reply('tiktok', {**ts.comment(text, cid), 'protocol_version': 2})

    def claim(self, answer):
        return self.engine.delivery_operation('tiktok', 'claim', {
            'turn_id': answer['turn_id'], 'room_id': ROOM, 'message_id': answer['messages'][0]['message_id']})

    def test_depends_on_video_rev_stored_as_video_dependent_with_evidence(self):
        self.prepare()
        first = self.ask('dùng sao vậy', 'x1')
        facts = json.loads(self.engine.delivery_store.get(first['turn_id'])['fact_revision'])
        self.assertTrue(facts['video_dependent'])  # no video_context at all: only depends_on says so
        self.assertEqual(facts['depends_on'], {'video_rev': 'v1'})
        self.assertEqual(facts['evidence'], [{'source': 'catalog', 'ref': 'p1'}])

    def test_stale_dependency_recomposed_exactly_once_through_delivery_claim(self):
        self.prepare()
        self.engine.video_positions['tiktok'].update({'position_s': 5, 'lock_id': 'A', 'revision': 1})
        first = self.ask('dùng sao vậy', 'x2')
        calls = len(self.responder.calls)
        self.engine.video_positions['tiktok'].update({'position_s': 9, 'lock_id': 'B', 'revision': 2})
        self.assertEqual(self.claim(first)['error'], 'revision_changed')
        self.assertEqual(len(self.responder.calls), calls + 1)
        second = self.engine.queue_operation('tiktok', 'next', {'room_id': ROOM})['result']
        self.assertEqual(self.engine.delivery_store.get(second['turn_id'])['recompose_count'], 1)
        self.engine.video_positions['tiktok'].update({'position_s': 12, 'lock_id': 'C', 'revision': 3})
        self.assertEqual(self.claim(second)['error'], 'revision_stale')
        self.assertEqual(len(self.responder.calls), calls + 1)  # never a second recompose

    def test_malformed_schema_still_replies(self):
        self.prepare()
        self.responder.reply = lambda *a, **k: {'action': 'reply', 'reply': 'Dạ ổn ạ.',
            'evidence': 'garbage', 'depends_on': 'x', 'awaiting_fields': 7, 'fields': []}
        answer = self.ask('dùng sao vậy', 'x3')
        self.assertTrue(answer['should_send'])
        facts = json.loads(self.engine.delivery_store.get(answer['turn_id'])['fact_revision'])
        self.assertFalse(facts['video_dependent'])


def test_shopee_depends_on_video_rev_and_evidence_stored(tmp_path):
    class R(tsa.FakeResponder):
        def reply(self, *a, **k):
            return {'action': 'reply', 'reply': 'Dạ dùng theo nhãn nha', 'evidence': EVIDENCE,
                    'depends_on': {'video_rev': 'v9'}, 'metrics': {}}
    owner = SimpleNamespace(responder=R(), knowledge=tsa.FakeKnowledge(),
        shopee_mapping_state=lambda: {'catalog': tsa.priced_catalog(), 'videos': {'v': {
            'mappings': {'tc01': {'shop_id': '10', 'product_id': '20'}}}}})
    engine = ShopeeAiEngine(owner, tmp_path / 'm.sqlite3', tmp_path / 'j')
    try:
        payload = {'session_id': '123', 'video_id': 'v', 'comment_id': 'c1', 'user_id': '77',
                   'nickname': 'Mai', 'comment': 'TC01 dùng sao', 'shop_id': '10', 'product_id': '20',
                   'title': 'Kích Rễ TC01'}
        result = engine.reply(payload)
        assert result['should_send']
        facts = json.loads(engine.delivery_store.get(result['turn_id'])['fact_revision'])
        assert facts['video_dependent'] is True
        assert facts['depends_on'] == {'video_rev': 'v9'}
        assert facts['evidence'] == [{'source': 'catalog', 'ref': 'p1'}]
        owner.responder = SimpleNamespace(reply=lambda *a, **k: {
            'action': 'reply', 'reply': 'Dạ ổn nha', 'evidence': 5, 'depends_on': [], 'metrics': 3})
        again = engine.reply({**payload, 'comment_id': 'c2'})
        assert again['should_send']  # malformed schema never blocks
    finally:
        engine.close()


def test_shopee_secondary_model_text_is_tolerant(tmp_path):
    owner = SimpleNamespace(responder=SimpleNamespace(reply=lambda *a, **k: {
        'action': 'reply', 'messages': ['Dạ bạn chọn loại nào nha']}), knowledge=tsa.FakeKnowledge(),
        shopee_mapping_state=lambda: {'catalog': tsa.priced_catalog(), 'videos': {}})
    engine = ShopeeAiEngine(owner, tmp_path / 'm.sqlite3', tmp_path / 'j')
    try:
        assert engine._model_text({}, 'k', 'Mai', 'hỏi', {}) == 'Dạ bạn chọn loại nào nha'
    finally:
        engine.close()


def test_schema_helpers_are_additive():
    schema = reply_schema.safe_normalize(schema_reply())
    rev = reply_schema.annotate_revision({'v': 1, 'video_dependent': False}, schema)
    assert rev['video_dependent'] is True and rev['depends_on'] == {'video_rev': 'v1'}
    assert reply_schema.safe_normalize('nope') is None
    assert reply_schema.annotate_revision({'video_dependent': True}, None) == {'video_dependent': True}
    assert reply_schema.audit_metrics(None, None) == {}
    assert reply_schema.audit_metrics({'a': 1}, schema)['reply_evidence'] == [{'source': 'catalog', 'ref': 'p1'}]


# ------------------------------------------------------------------ D01 TikTok
class Responder:
    def __init__(self):
        self.calls = []

    def reply(self, nickname, comment, context, max_chars, **kwargs):
        self.calls.append(context)
        if context.get('ask') == 'select_dose_rule':  # D05: model picks an approved rule_id
            return {'action': 'reply', 'reply': '', 'rule_id': context['dose_rule_options'][0]['rule_id'],
                    'params': {'volume': 16, 'unit': 'l'}}
        return {'action': 'reply', 'reply': 'Dạ câu trả lời đủ dữ kiện ạ.'}


@pytest.fixture
def nodose_engine(tmp_path, monkeypatch):
    monkeypatch.setattr(server_module, 'LIVE_CATALOG_PATH', tmp_path / 'live.json')
    catalog = LiveCatalogStore()
    catalog.sync = lambda snap: {'room_id': snap.get('room_id', ''), 'pin_id': snap.get('pin_id', ''),
                                 'products': snap.get('products', []), 'product_count': 1, 'synced_at': 'now'}
    reporter = MissingReporter()
    reporter.path = tmp_path / 'missing.jsonl'
    memory = CustomerMemory(tmp_path / 'memory.sqlite3', tmp_path / 'journals', session_id='s1')
    data = {'nhom_san_pham': [{'san_pham': [{
        'san_pham_id': 'SP-03', 'ten_san_pham': 'Kích Rễ Giâm Cành TC01', 'approved_for_live': True,
        'shop_product_ids': ['p1'], 'listing_aliases': 'TC01', 'cong_dung': 'Hỗ trợ kích rễ'}]}]}
    (tmp_path / 'k.json').write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    (tmp_path / 'p.json').write_text('{"products":{}}', encoding='utf-8')
    eng = ReplyEngine(responder=Responder(), memory=memory,
                      knowledge=KnowledgeBase(tmp_path / 'k.json', tmp_path / 'p.json'),
                      catalog_store=catalog, missing_reporter=reporter)
    yield eng
    eng.close()


def _payload(cid):
    return {'comment': 'TC01 pha bình 16 lít bao nhiêu', 'comment_id': cid, 'user_id': 'u1',
            'unique_id': 'useru1', 'nickname': 'Nguyễn Thị Hoa', 'live_context': {
                'room_id': 'room-1', 'pin_id': 'p1', 'products': [{
                    'product_id': 'p1', 'title': 'Kích Rễ Giâm Cành TC01', 'original_price': '120.000₫'}]}}


RULE = {'sku': 'SP-03', 'amount': 33, 'unit': 'ml', 'per_volume': 16, 'source_doc': 'nhan.pdf'}


def _import(eng, approve):
    with eng.workspace._db() as db:
        ids = dose_rules.import_rules(db, [RULE])['rule_ids']
        if approve:
            dose_rules.review_rule(db, ids[0], 'approve')


def test_no_rule_no_dose_text_is_soft_hint(nodose_engine):
    out = nodose_engine.handle(_payload('a'))
    assert out['should_send'] is True, out
    assert 'kien_thuc_chung' in nodose_engine.responder.calls[-1]


def test_candidate_rule_does_not_remove_soft_hint(nodose_engine):
    _import(nodose_engine, approve=False)
    out = nodose_engine.handle(_payload('b'))
    assert out['should_send'] is True, out
    assert 'kien_thuc_chung' in nodose_engine.responder.calls[-1]


def test_approved_rule_removes_missing_data_on_tiktok(nodose_engine):
    _import(nodose_engine, approve=True)
    out = nodose_engine.handle(_payload('c'))
    assert out['should_send'] is True, out
    assert '33' in json.dumps(nodose_engine.responder.calls[-1].get('verified_dose_calculations'))
