import json
import time
from types import SimpleNamespace

import pytest

from shopee_ai import ShopeeAiEngine
from shopee_commerce import product_family_candidates
from live_conversation import resolve_question


CATALOG = [
    {'shop_id':'10','product_id':'20','title':'Kích Rễ TC01'},
    {'shop_id':'10','product_id':'21','title':'Đạm Cá Amino 1L'},
    {'shop_id':'10','product_id':'22','title':'Đạm Cá Amino 5L'},
]


@pytest.mark.parametrize('question', ['em có đạm cá loại nào', 'đạm cá có mấy loại',
    'đạm cá gồm những loại gì', 'có những loại đạm cá nào'])
def test_family_candidates_never_include_unrelated_pin(question):
    family, candidates = product_family_candidates(question, CATALOG)
    assert family == 'dam ca'
    assert [item['product_id'] for item in candidates] == ['21','22']


def make_engine(tmp_path, responder):
    owner = SimpleNamespace(responder=responder, knowledge=SimpleNamespace(match=lambda *_: None),
        shopee_mapping_state=lambda:{'catalog':{'session_id':'123','updated_at':time.time(),'products':CATALOG},
            'videos':{'v':{'mappings':{'tc01':{'shop_id':'10','product_id':'20'}}}}})
    return ShopeeAiEngine(owner, tmp_path / 'memory.sqlite3', tmp_path / 'journal')


def payload(comment='em có đạm cá loại nào', comment_id='c1'):
    return {'session_id':'123','video_id':'v','comment_id':comment_id,'user_id':'77',
        'nickname':'Khách thử','comment':comment,'shop_id':'10','product_id':'20',
        'protocol_version':2}


def test_range_is_naturally_generated_and_persisted_without_fabricating_selected_sku(tmp_path):
    calls = []
    def reply(nickname, comment, context, *args, **kwargs):
        calls.append(context)
        assert comment == 'em có đạm cá loại nào'
        assert context['intent'] == 'product_range'
        assert context['product_selection']['selected_sku'] == ''
        assert [item['product_id'] for item in context['product_selection']['candidate_skus']] == ['21','22']
        return {'action':'reply','reply':'Đạm cá hiện có chai 1L và can 5L, anh dùng ít hay dùng nhiều để em tư vấn nhé.',
                'awaiting_fields':['variant']}
    engine = make_engine(tmp_path, SimpleNamespace(reply=reply))
    try:
        answer = engine.reply(payload())
        assert answer['should_send'] and answer['reply_source'] == 'deepseek_catalog_range'
        assert len(calls) == 1
        with engine.memory._lock:
            row = engine.memory._db.execute('SELECT product_id,metadata_json FROM interactions WHERE interaction_id=?',
                (answer['interaction_id'],)).fetchone()
        assert row[0] == ''
        state = json.loads(row[1])['conversation_state']
        assert state['product_family'] == 'dam ca'
        assert state['candidate_skus'] == ['shopee:21','shopee:22']
        assert state['observed_pin'] == 'shopee:20'
        assert state['selected_sku'] == ''
        turn = {'delivery_status':'sent','comment':'em có đạm cá loại nào','conversation_state':state}
        assert resolve_question('5l giá sao', [turn])[0] == 'dam ca 5l giá sao'
        assert resolve_question('loại 5l nhé', [turn])[0] == 'dam ca loại 5l nhé'
        assert resolve_question('5l TC01 giá sao', [turn]) == ('5l TC01 giá sao', None)
    finally:
        engine.close()


def test_range_invented_price_is_corrected_once_then_refused(tmp_path):
    calls = []
    def reply(*args, **kwargs):
        calls.append(kwargs)
        return {'action':'reply','reply':'Đạm cá giá 999.000đ nhé'}
    engine = make_engine(tmp_path, SimpleNamespace(reply=reply))
    try:
        answer = engine.reply(payload())
        assert not answer['should_send'] and answer['reason'] == 'range_facts_invalid'
        assert len(calls) == 2
        assert calls[-1]['correction']
        assert engine.delivery_store._db.execute('SELECT COUNT(*) FROM reply_turns').fetchone()[0] == 0
    finally:
        engine.close()
