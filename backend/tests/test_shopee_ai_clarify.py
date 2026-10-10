import time
from types import SimpleNamespace

from shopee_ai import ShopeeAiEngine


def catalog():
    return {"session_id": "123", "updated_at": time.time(), "products": [
        {"shop_id": "10", "product_id": "20", "title": "Kích Rễ TC01"},
        {"shop_id": "10", "product_id": "21", "title": "Phân Bón NPK 20"},
        {"shop_id": "10", "product_id": "22", "title": "Thuốc Sâu ABC"}]}


class Knowledge:
    def match(self, title, comment=""):
        return None

    def context(self, record):
        return {}


class Model:
    def __init__(self, text="Mình cần loại nào ạ, em gửi gợi ý nha", fail=False):
        self.calls, self.text, self.fail = [], text, fail

    def reply(self, nickname, comment, context, max_chars, **kwargs):
        self.calls.append(context)
        if self.fail:
            raise TimeoutError('down')
        return {"action": "reply", "reply": self.text, "metrics": {}}


def make(tmp_path, model):
    owner = SimpleNamespace(responder=model, knowledge=Knowledge(),
        shopee_mapping_state=lambda: {'videos': {}, 'catalog': catalog()})
    return ShopeeAiEngine(owner, tmp_path / 'm.sqlite3', tmp_path / 'j')


def payload(n, comment):
    return {'session_id': '123', 'video_id': 'v', 'comment_id': f'c{n}', 'user_id': '77',
            'nickname': 'Mai', 'comment': comment, 'shop_id': '', 'product_id': '', 'title': ''}


def send(engine, result):
    assert engine.delivery({'session_id': '123', 'turn_id': result['turn_id'], 'message_id': 'r'})['sent']


def test_no_sku_uses_model_text(tmp_path):
    model = Model()
    engine = make(tmp_path, model)
    try:
        r = engine.reply(payload(1, 'giá bao nhiêu vậy'))
        assert r['should_send'] and 'gợi ý' in r['reply']
        assert model.calls[0]['ask'] == 'clarify_sku' and model.calls[0]['catalog_candidates']
    finally:
        engine.close()


def test_model_error_falls_back_to_named_listing(tmp_path):
    engine = make(tmp_path, Model(fail=True))
    try:
        r = engine.reply(payload(1, 'giá bao nhiêu vậy'))
        assert r['should_send'] and 'Shop đang có' in r['reply']
    finally:
        engine.close()


def test_clarify_always_asks_model_never_canned_loop(tmp_path):
    model = Model()
    engine = make(tmp_path, model)
    try:
        for n in (1, 2):
            r = engine.reply(payload(n, 'giá bao nhiêu vậy'))
            assert r['should_send']
            send(engine, r)
        r = engine.reply(payload(3, 'giá bao nhiêu vậy'))
        assert r['should_send'] and len(model.calls) == 3
        assert model.calls[-1].get('recent_turns'), 'model must see the recent conversation'
    finally:
        engine.close()


def test_promotion_uses_model(tmp_path):
    model = Model(text="Em chưa có thông tin quà tặng nha")
    owner = SimpleNamespace(responder=model, knowledge=Knowledge(),
        shopee_mapping_state=lambda: {'videos': {}, 'catalog': catalog()})
    engine = ShopeeAiEngine(owner, tmp_path / 'm.sqlite3', tmp_path / 'j')
    try:
        r = engine.reply(payload(1, 'Kích Rễ TC01 có tặng gì không'))
        assert r['should_send'] and r['reply_source'] == 'promotion_clarification'
        assert 'quà tặng' in r['reply']
        assert any(c.get('ask') == 'promotion_unknown' for c in model.calls)
    finally:
        engine.close()


def test_product_ref_added_to_model_context(tmp_path):
    model = Model()
    engine = make(tmp_path, model)
    try:
        engine.reply(payload(1, 'giá bao nhiêu vậy'))
    finally:
        engine.close()


def test_product_ref_added_to_main_model_context(tmp_path, monkeypatch):
    import context_budget
    real_enabled = context_budget.enabled
    monkeypatch.setattr(context_budget, "enabled",
                        lambda name: False if name == "intent_fields" else real_enabled(name))
    seen = []

    class Spy:
        def reply(self, nickname, comment, context, max_chars, **kwargs):
            seen.append(context)
            return {"action": "reply", "reply": "Dạ dùng theo nhãn nha", "metrics": {}}
    owner = SimpleNamespace(responder=Spy(), knowledge=Knowledge(),
        shopee_mapping_state=lambda: {'videos': {}, 'catalog': catalog()})
    engine = ShopeeAiEngine(owner, tmp_path / 'm.sqlite3', tmp_path / 'j')
    try:
        engine.reply(payload(1, 'xin chào Kích Rễ TC01'))
    finally:
        engine.close()
    assert seen and 'product_ref' in seen[0] and 'candidates' in seen[0]['product_ref']
