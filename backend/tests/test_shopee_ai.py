from types import SimpleNamespace

from shopee_ai import ShopeeAiEngine
from customer_memory import CustomerMemory
import pytest
import time
import sqlite3

def priced_catalog():
    return {"session_id":"123", "updated_at":time.time(), "products":[{"shop_id":"10","product_id":"20","title":"Kích Rễ TC01", "price_data":{"currency":"VND","price":"80000","price_before_discount":"80000"}}]}


def test_forget_while_model_runs_does_not_recreate_delivery(tmp_path):
    owner = SimpleNamespace(responder=FakeResponder(), knowledge=FakeKnowledge(),
        shopee_mapping_state=lambda: {"catalog": priced_catalog(), "videos": {"v": {
            "mappings": {"tc01": {"shop_id": "10", "product_id": "20"}}}}})
    engine = ShopeeAiEngine(owner, tmp_path / 'memory.sqlite3', tmp_path / 'journal')
    class ForgettingResponder(FakeResponder):
        def reply(self, *args, **kwargs):
            engine.memory.forget_if_requested('xóa dữ liệu của tôi', platform_user_id='77',
                display_name='Mai')
            return super().reply(*args, **kwargs)
    owner.responder = ForgettingResponder()
    try:
        result = engine.reply({'session_id': '123', 'video_id': 'v', 'comment_id': 'c',
            'user_id': '77', 'nickname': 'Mai', 'comment': 'TC01 dùng sao?',
            'shop_id': '10', 'product_id': '20', 'title': 'Kích Rễ TC01'})
        assert result['should_send'] is False
        assert result['reason'] == 'memory_disabled'
        assert engine.delivery_store._db.execute('SELECT COUNT(*) FROM reply_turns').fetchone()[0] == 0
    finally:
        engine.close()


def test_slow_model_cannot_return_sendable_expired_reply(tmp_path, monkeypatch):
    now = [time.time()]
    monkeypatch.setattr(time, 'time', lambda: now[0])
    class SlowResponder(FakeResponder):
        def reply(self, *args, **kwargs):
            now[0] += 120
            return super().reply(*args, **kwargs)
    owner = SimpleNamespace(responder=SlowResponder(), knowledge=FakeKnowledge(),
        shopee_mapping_state=lambda: {'catalog': priced_catalog(), 'videos': {'v': {
            'mappings': {'tc01': {'shop_id': '10', 'product_id': '20'}}}}})
    engine = ShopeeAiEngine(owner, tmp_path / 'memory.sqlite3', tmp_path / 'journal')
    try:
        result = engine.reply({'session_id': '123', 'video_id': 'v', 'comment_id': 'slow',
            'received_at_ms': now[0] * 1000, 'user_id': '77', 'nickname': 'Mai',
            'comment': 'TC01 dùng sao?', 'shop_id': '10', 'product_id': '20', 'title': 'Kích Rễ TC01'})
        assert result == {'ok': True, 'should_send': False, 'reason': 'expired'}
        assert engine.memory.data_summary()['delivery_statuses']['expired'] == 1
    finally:
        engine.close()


class FakeResponder:
    def reply(self, nickname, comment, context, max_chars, **kwargs):
        assert context["platform"] == "Shopee LIVE"
        from ai_context import APP_BRIEF
        system = kwargs["system_override"]
        assert system.startswith(APP_BRIEF.rstrip())          # mọi lời gọi DeepSeek bắt đầu bằng APP_BRIEF
        assert "Bạn là người đang tư vấn trong Shopee LIVE" in system and "[NHIỆM VỤ: Trả lời bình luận Shopee LIVE]" in system
        return {"action": "reply", "reply": "TC01 giá " + context["price"]["quotable_price"] + " nha" if "giá" in comment.lower() else "Dạ dùng theo hướng dẫn trên nhãn nha", "metrics": {}}


class FakeKnowledge:
    def match(self, title, comment=""):
        return {"ten_san_pham": title} if title == "Kích Rễ TC01" else None

    def context(self, record):
        return {"ten_san_pham": record["ten_san_pham"]}


def test_shopee_reply_uses_separate_identity_and_requires_receipt(tmp_path):
    owner = SimpleNamespace(
        responder=FakeResponder(), knowledge=FakeKnowledge(),
        shopee_mapping_state=lambda: {"videos": {"video1": {"mappings": {
            "tc01": {"shop_id": "10", "product_id": "20"}}, "alternates": {}}}},
    )
    mapping = owner.shopee_mapping_state()
    owner.shopee_mapping_state = lambda: {**mapping, "catalog":priced_catalog()}
    engine = ShopeeAiEngine(owner, tmp_path / "shopee.sqlite3", tmp_path / "journal")
    try:
        payload = {"session_id": "123", "video_id": "video1", "comment_id": "c1",
                   "user_id": "77", "nickname": "Anh Bảy", "comment": "TC01 dùng sao?",
                   "shop_id": "10", "product_id": "20", "title": "Kích Rễ TC01"}
        answer = engine.reply(payload)
        assert answer["should_send"] is True
        assert answer["reply"].startswith("@Anh Bảy ")
        assert engine.reply(payload)["reason"] == "duplicate"
        assert engine.memory.customer_key(platform_user_id="77", platform="shopee")[0] == "shopee:id:77"
        assert engine.delivery({"session_id": "123", "turn_id": answer["turn_id"],
                                "message_id": "m123"}) == {"ok": True, "sent": True}
        assert engine.delivery({"session_id": "123", "turn_id": answer["turn_id"],
                                "message_id": "m123"}) == {"ok": True, "sent": True}
        assert engine.delivery({"session_id": "123", "turn_id": answer["turn_id"],
                                "message_id": "different"})["ok"] is False
        price = engine.reply({**payload, "comment_id": "c2", "comment": "Giá bao nhiêu?"})
        assert price["should_send"] is True
        assert price["reply_source"] == "deepseek"
        assert "80.000đ" in price["reply"]
        assert "xem giá" not in price["reply"]
        assert len(price["reply"]) <= 150
        assert engine.delivery({"session_id":"123", "turn_id":price["turn_id"], "message_id":"price1"})["sent"]
        assert engine.reply({**payload, "comment_id":"c2", "comment":"Giá bao nhiêu?", "retry_unanswered":True})["reason"] == "duplicate"
        assert engine.reply({**payload, "comment_id": "c3", "product_id": "99"})["reason"] == "product_not_mapped"
        draft = engine.reply({**payload, "comment_id": "c4", "comment": "TC01 dùng thế nào?"})
        assert draft["should_send"] is True
        assert engine.delivery({"session_id": "123", "turn_id": draft["turn_id"],
                                "error": "Preview only"}) == {"ok": True, "sent": False}
        assert engine.memory.data_summary()["delivery_statuses"]["cancelled"] == 1
        expired = engine.reply({**payload, "comment_id": "expired", "comment": "TC01 dùng lúc nào?"})
        assert engine.delivery({"session_id": "123", "turn_id": expired["turn_id"],
                                "error": "expired before send"}) == {"ok": True, "sent": False}
        assert engine.memory.data_summary()["delivery_statuses"]["expired"] == 1
        owner.responder = type("UnsafeResponder", (), {
            "reply": lambda *_args, **_kwargs: {"action": "reply", "reply": "Giá 289k nha"}
        })()
        assert engine.reply({**payload, "comment_id": "c5", "comment": "TC01 để làm gì?"})["should_send"] is True
    finally:
        engine.close()


def test_shopee_comment_is_not_reprocessed_after_backend_restart(tmp_path):
    owner = SimpleNamespace(
        responder=FakeResponder(), knowledge=FakeKnowledge(),
        shopee_mapping_state=lambda: {"videos": {"v": {"mappings": {
            "tc01": {"shop_id": "10", "product_id": "20"}}, "alternates": {}}}},
    )
    db = tmp_path / "shopee.sqlite3"
    payload = {"session_id": "123", "video_id": "v", "comment_id": "c1",
               "user_id": "77", "nickname": "Mai", "comment": "TC01 dùng sao?",
               "shop_id": "10", "product_id": "20", "title": "Kích Rễ TC01"}
    first = ShopeeAiEngine(owner, db, tmp_path / "journal")
    assert first.reply(payload)["should_send"] is True
    first.close()
    second = ShopeeAiEngine(owner, db, tmp_path / "journal")
    try:
        assert second.reply(payload)["reason"] == "duplicate"
    finally:
        second.close()


def test_shopee_receipt_can_be_persisted_after_backend_restart(tmp_path):
    owner = SimpleNamespace(responder=FakeResponder(), knowledge=FakeKnowledge(),
        shopee_mapping_state=lambda: {'videos': {'v': {'mappings': {
            'tc01': {'shop_id': '10', 'product_id': '20'}}}}, 'catalog': priced_catalog()})
    path = tmp_path/'memory.sqlite3'
    first = ShopeeAiEngine(owner, path, tmp_path/'journal')
    answer = first.reply({'session_id': '123', 'video_id': 'v', 'comment_id': 'c1',
        'user_id': '77', 'nickname': 'Mai', 'comment': 'TC01 dùng sao?',
        'shop_id': '10', 'product_id': '20', 'title': 'Kích Rễ TC01'})
    first.close()
    second = ShopeeAiEngine(owner, path, tmp_path/'journal')
    payload = {'session_id': '123', 'turn_id': answer['turn_id'], 'message_id': 'platform-receipt'}
    try:
        assert second.delivery({**payload, 'session_id': '999'})['ok'] is False
        assert second.delivery(payload) == {'ok': True, 'sent': True}
        assert second.delivery(payload) == {'ok': True, 'sent': True}
        assert not second.delivery({**payload, 'message_id': 'wrong'})['ok']
        assert second.memory.data_summary()['delivery_statuses']['sent'] == 1
    finally:
        second.close()


def test_expired_shopee_comment_is_not_generated(tmp_path):
    engine = ShopeeAiEngine(SimpleNamespace(), tmp_path/'db.sqlite3', tmp_path/'journal')
    try:
        assert engine.reply({'received_at_ms': time.time() * 1000 - 121000})['reason'] == 'expired'
        assert engine.reply({'received_at_ms': float('nan')})['error'] == 'received_at_invalid'
    finally:
        engine.close()


@pytest.mark.parametrize('bad_code', [None, False, '', 0.0, 500])
def test_tiktok_receipt_requires_exact_success_and_room(tmp_path, bad_code):
    memory = CustomerMemory(tmp_path/'memory.sqlite3', tmp_path/'journal')
    try:
        ref = memory.record_comment('Giá?', comment_id='c1', room_id='room-1',
            platform_user_id='77', display_name='Mai')
        memory.record_reply(ref['interaction_id'], 'Giá theo giỏ hàng', turn_id='turn', complete=False)
        assert not memory.confirm_reply_sent(ref['interaction_id'], 'turn',
            {'response_code': bad_code, 'room_id': 'room-1'})
        assert not memory.confirm_reply_sent(ref['interaction_id'], 'turn',
            {'response_code': 0, 'room_id': 'room-2'})
        assert memory.confirm_reply_sent(ref['interaction_id'], 'turn',
            {'response_code': '0', 'room_id': 'room-1'})
    finally:
        memory.close()


def test_recover_real_price_comment_that_previous_version_left_unanswered(tmp_path):
    owner = SimpleNamespace(responder=FakeResponder(), knowledge=FakeKnowledge(),
        shopee_mapping_state=lambda: {"videos": {"v": {"mappings": {
            "tc01": {"shop_id": "10", "product_id": "20"}}}}})
    mapping = owner.shopee_mapping_state()
    owner.shopee_mapping_state = lambda: {**mapping, "catalog":priced_catalog()}
    engine = ShopeeAiEngine(owner, tmp_path / "memory.sqlite3", tmp_path / "journal")
    try:
        payload = {"session_id":"123", "video_id":"v", "comment_id":"c1", "user_id":"77",
            "nickname":"Mai", "comment":"giá nhiu em", "shop_id":"10", "product_id":"20",
            "title":"Kích Rễ TC01"}
        engine.memory.record_comment(payload["comment"], comment_id="shopee:123:c1",
            platform_user_id="77", display_name="Mai", data_source="shopee-live")
        assert engine.reply(payload)["reason"] == "duplicate"
        recovered = engine.reply({**payload, "retry_unanswered":True})
        assert recovered["should_send"]
        assert "80.000đ" in recovered["reply"]
        assert engine.delivery({"session_id":"123", "turn_id":recovered["turn_id"], "message_id":"real-receipt"})["sent"]
        assert not engine.memory.can_retry_unanswered("shopee:123:c1", "77", payload["comment"])
        assert engine.reply({**payload, "retry_unanswered":True})["reason"] == "duplicate"
    finally:
        engine.close()


def test_shopee_uses_shared_customer_store_and_shared_lessons(tmp_path):
    main = CustomerMemory(tmp_path / "shared.sqlite3", tmp_path / "tiktok-journal", session_id="tiktok_test")
    captured = {}
    class Responder:
        def reply(self, nickname, comment, context, max_chars, **kwargs):
            captured.update(context)
            return {"action": "reply", "reply": "TC01 hỗ trợ rễ nha"}
    learning = SimpleNamespace(persona=lambda: {"persona_id": "p1"},
        relevant=lambda *args, **kwargs: [{"id": "lesson1", "noi_dung": "Bài học chung"}],
        mark_used=lambda ids: captured.update(used=ids))
    owner = SimpleNamespace(responder=Responder(), knowledge=FakeKnowledge(), learning=learning,
        _persona_context=lambda *args: {"quy_tac_da_hoc": ["Quy tắc chung"]},
        shopee_mapping_state=lambda: {"videos": {"v": {"mappings": {
            "tc01": {"shop_id": "10", "product_id": "20"}}}}})
    shopee = ShopeeAiEngine(owner, main.db_path, tmp_path / "shopee-journal")
    try:
        main.record_comment("Tôi trồng xoài", platform_user_id="77", display_name="Mai")
        result = shopee.reply({"session_id": "123", "video_id": "v", "comment_id": "c1",
            "user_id": "77", "nickname": "Mai", "comment": "TC01 cho sầu riêng?",
            "shop_id": "10", "product_id": "20", "title": "Kích Rễ TC01"})
        assert result["should_send"]
        assert captured["kien_thuc_da_hoc"][0]["id"] == "lesson1"
        assert captured["quy_tac_da_hoc"] == ["Quy tắc chung"]
        assert {row["customer_key"] for row in main.fact_rows()} == {"tiktok:id:77", "shopee:id:77"}
    finally:
        shopee.close()
        main.close()


def test_legacy_shopee_memory_import_is_idempotent(tmp_path):
    legacy = CustomerMemory(tmp_path / "shopee_customers.sqlite3", tmp_path / "legacy-journal", platform="shopee")
    legacy.record_comment("Tôi trồng xoài", platform_user_id="123", display_name="Mai", comment_id="old1")
    legacy.close()
    owner = SimpleNamespace()
    for _ in range(2):
        engine = ShopeeAiEngine(owner, tmp_path / "shared.sqlite3", tmp_path / "journal")
        try:
            assert engine.memory.has_comment_id("old1")
            assert {r["customer_key"] for r in engine.memory.fact_rows()} == {"shopee:id:123"}
        finally:
            engine.close()
    assert (tmp_path / "shopee_customers.sqlite3").is_file()


def test_delivery_can_retry_a_database_failure_without_sending_again(tmp_path, monkeypatch):
    owner = SimpleNamespace(responder=FakeResponder(), knowledge=FakeKnowledge(),
        shopee_mapping_state=lambda: {'videos': {}, 'catalog': priced_catalog()})
    engine = ShopeeAiEngine(owner, tmp_path / 'memory.sqlite3', tmp_path / 'journal')
    try:
        answer = engine.reply({'session_id': '123', 'video_id': 'v', 'comment_id': 'c1',
            'user_id': '77', 'nickname': 'Mai', 'comment': 'TC01 giá bao nhiêu?'})
        receipt = {'session_id': '123', 'turn_id': answer['turn_id'], 'message_id': 'receipt1'}
        original = engine.memory.confirm_reply_sent
        def fail_once(*args, **kwargs):
            monkeypatch.setattr(engine.memory, 'confirm_reply_sent', original)
            raise sqlite3.OperationalError('database temporarily unavailable')
        monkeypatch.setattr(engine.memory, 'confirm_reply_sent', fail_once)
        with pytest.raises(sqlite3.OperationalError):
            engine.delivery(receipt)
        assert engine.delivery(receipt) == {'ok': True, 'sent': True}
        assert engine.delivery(receipt) == {'ok': True, 'sent': True}
        assert engine.memory.data_summary()['delivery_statuses']['sent'] == 1
    finally:
        engine.close()


def test_raw_history_keeps_platform_identities_separate(tmp_path):
    from customer_history import CustomerHistory
    history = CustomerHistory(tmp_path)
    base = {"ts": "2026-09-30T10:00:00+07:00", "user_id": "77", "nickname": "Mai"}
    history._add({**base, "text": "Tôi trồng xoài", "room_id": "tt1"})
    history._add({**base, "text": "Tôi trồng sầu riêng", "room_id": "shopee:123", "platform": "shopee"})
    assert list(history._by_customer) == ["tiktok:id:77", "shopee:id:77"]
    assert history.for_customer("shopee:id:77")["phien_truoc"][0]["binh_luan"][0]["khach"] == "Tôi trồng sầu riêng"


def test_shared_lessons_expand_canonical_profile_to_other_platform_listings():
    from knowledge import shared_learning_product_ids
    knowledge = SimpleNamespace(by_shop_id={"tt1": {"san_pham_id": "tc01"}, "tt2": {"san_pham_id": "tc01"}, "tt3": {"san_pham_id": "other"}})
    assert shared_learning_product_ids(knowledge, {"san_pham_id": "tc01"}, "shopee:20") == ["shopee:20", "tc01", "tt1", "tt2"]


def test_named_product_reply_during_unpinned_timeline_and_transient_model_failure(tmp_path):
    calls = []
    class Responder(FakeResponder):
        def reply(self, *args, **kwargs):
            calls.append(True)
            if len(calls) == 1:
                raise TimeoutError('temporary model outage')
            return super().reply(*args, **kwargs)
    owner = SimpleNamespace(responder=Responder(), knowledge=FakeKnowledge(),
        shopee_mapping_state=lambda: {'videos':{}, 'catalog':priced_catalog()})
    engine = ShopeeAiEngine(owner, tmp_path/'memory.sqlite3', tmp_path/'journal')
    payload = {'session_id':'123','video_id':'v','comment_id':'c1','user_id':'77',
        'nickname':'Mai','comment':'TC01 giá bao nhiêu?', 'shop_id':'','product_id':'','title':''}
    try:
        with pytest.raises(TimeoutError):
            engine.reply(payload)
        answer = engine.reply({**payload,'retry_unanswered':True})
        assert answer['should_send'] and '80.000đ' in answer['reply']
        assert engine.delivery({'session_id':'123','turn_id':answer['turn_id'],'message_id':'receipt'})['sent']
        assert engine.reply({**payload,'retry_unanswered':True})['reason'] == 'duplicate'
    finally:
        engine.close()


@pytest.mark.parametrize('delivery', ['generated','cancelled','failed','sent'])
def test_history_only_includes_confirmed_delivered_replies(tmp_path, delivery):
    from customer_history import CustomerHistory
    memory = CustomerMemory(tmp_path/'memory.sqlite3', tmp_path/'journal')
    try:
        ref = memory.record_comment('TC01?', comment_id='c1', platform_user_id='77', display_name='Mai')
        memory.record_reply(ref['interaction_id'],'TC01 hỗ trợ rễ',turn_id='turn',complete=False)
        if delivery == 'sent':
                memory.confirm_reply_sent(ref['interaction_id'],'turn',receipt={'response_code': 0})
        elif delivery != 'generated':
            memory.mark_reply_delivery(ref['interaction_id'],'turn',delivery)
        history = CustomerHistory(tmp_path, memory)
        history._add({'ts':'2026-10-01T00:00:00+07:00','room_id':'room','msg_id':'c1','user_id':'77','nickname':'Mai','text':'TC01?'})
        reply = history.for_customer('tiktok:id:77',current_room='room')['phien_nay'][0]['tra_loi']
        assert reply == ('AI: TC01 hỗ trợ rễ' if delivery == 'sent' else 'chưa ai trả lời')
    finally:
        memory.close()

@pytest.mark.parametrize('comment', ['giá nhiu em', 'bao nhiu đó', 'bao tiền', 'giá bao nhiêu'])
def test_verified_price_answers_even_when_model_skips(tmp_path, comment):
    owner = SimpleNamespace(responder=SimpleNamespace(reply=lambda *a, **kw: {'action':'skip','missing_fields':['price']}),
        knowledge=SimpleNamespace(match=lambda *_: None),
        shopee_mapping_state=lambda:{'videos':{'v':{'mappings':{'tc01':{'shop_id':'10','product_id':'20'}}}},'catalog':priced_catalog()})
    engine=ShopeeAiEngine(owner,tmp_path/'memory.db',tmp_path/'journal')
    try:
        answer=engine.reply({'session_id':'123','video_id':'v','comment_id':'1','user_id':'77',
            'nickname':'Mai','comment':comment,'shop_id':'10','product_id':'20'})
        assert answer['should_send'] is True
        assert answer['reply_source']=='verified_price'
        assert '80.000đ' in answer['reply']
    finally:
        engine.close()


def test_shopee_uses_the_same_approved_product_promotion_as_tiktok(tmp_path):
    knowledge=FakeKnowledge()
    knowledge.context=lambda record:{'ten_san_pham':'Kích Rễ TC01', 'live_promotion':'Mua 10 tặng 2 chai',
        'ho_so_da_duyet':{'promotion':'Mua 10 tặng 3 chai'}}
    owner=SimpleNamespace(responder=SimpleNamespace(reply=lambda *a, **kw: pytest.fail('Approved promotion needs no model')),
        knowledge=knowledge,shopee_mapping_state=lambda:{'videos':{'v':{'mappings':{'tc01':{'shop_id':'10','product_id':'20'}}}},'catalog':priced_catalog()})
    engine=ShopeeAiEngine(owner,tmp_path/'memory.db',tmp_path/'journal')
    try:
        result=engine.reply({'session_id':'123','video_id':'v','comment_id':'1','user_id':'77',
            'nickname':'Mai','comment':'10 chai tặng gì ko','shop_id':'10','product_id':'20'})
        assert result['should_send']
        assert result['reply_source']=='verified_promotion'
        assert 'Mua 10 tặng 2 chai' in result['reply']
        assert 'tặng 3' not in result['reply']
    finally:
        engine.close()

@pytest.mark.parametrize('known,expected', [(True,'verified_promotion'),(False,'promotion_clarification')])
def test_ten_bottle_gift_uses_approved_shared_product_profile(tmp_path,known,expected):
    knowledge=FakeKnowledge()
    if known:
        knowledge.context=lambda record:{'ten_san_pham':'Kích Rễ TC01',
            'live_promotion':'Mua 10 chai TC01 tặng thêm 1 chai TC01 nha.'}
    owner=SimpleNamespace(responder=SimpleNamespace(reply=lambda *a, **kw:pytest.fail('Exact gift uses confirmed fact')),
        knowledge=knowledge,shopee_mapping_state=lambda:{'videos':{'v':{'mappings':{'tc01':{'shop_id':'10','product_id':'20'}}}},
            'catalog':priced_catalog()})
    engine=ShopeeAiEngine(owner,tmp_path/'memory.db',tmp_path/'journal')
    try:
        result=engine.reply({'session_id':'123','video_id':'v','comment_id':'1','user_id':'77','nickname':'Mai',
            'comment':'10 chai tặng gì ko','shop_id':'10','product_id':'20'})
        assert result['should_send']
        assert result['reply_source']==expected
        assert ('tặng thêm 1 chai' in result['reply']) == (expected=='verified_promotion')
        if not known:
            assert '10 chai' in result['reply']
            assert '?' not in result['reply']
        assert engine.delivery({'session_id':'123','turn_id':result['turn_id'],'message_id':'gift-receipt'})['sent']
    finally:
        engine.close()
