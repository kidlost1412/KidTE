"""Y1: TikTok D05 (model picks approved rule_id, code verifies) and A04 (model-phrased SKU clarify, cap 2)."""
import json

import pytest

import dose_rules
import dose_selection
import server as server_module
from customer_memory import CustomerMemory
from knowledge import KnowledgeBase
from server import LiveCatalogStore, MissingReporter, ReplyEngine

RULE = {"sku": "SP-03", "amount": 33, "unit": "ml", "per_volume": 16, "source_doc": "nhan.pdf"}


class Responder:
    """Scripted fake model; no network."""

    def __init__(self):
        self.calls, self.selections, self.clarify, self.final = [], [], [], "Dạ câu trả lời đủ dữ kiện ạ."
        self.fail_clarify = False

    def reply(self, nickname, comment, context, max_chars, **kwargs):
        self.calls.append({"context": context, **kwargs})
        if context.get("ask") == "select_dose_rule":
            pick = self.selections.pop(0) if self.selections else None
            if pick is None:
                pick = {"rule_id": context["dose_rule_options"][0]["rule_id"], "params": {"volume": 16, "unit": "l"}}
            return {"action": "reply", "reply": "", **pick}
        if context.get("ask") == "clarify_sku":
            if self.fail_clarify:
                raise RuntimeError("model down")
            return {"action": "reply", "reply": self.clarify.pop(0) if self.clarify else "Mình lấy loại nào ạ?"}
        return {"action": "reply", "reply": self.final}


def _live(pin="p1", two=False):
    products = [{"product_id": "p1", "title": "Kích Rễ Giâm Cành TC01", "original_price": "120.000₫"}]
    if two:
        products = [{"product_id": "a1", "title": "Humic Mai Vàng 1L", "original_price": "50.000₫"},
                    {"product_id": "a2", "title": "Humic Mai Vàng 5L", "original_price": "200.000₫"},
                    {"product_id": "a3", "title": "Humic Mai Vàng 20L", "original_price": "700.000₫"}]
    return {"room_id": "room-1", "pin_id": pin, "products": products}


def _payload(text, cid, live):
    return {"comment": text, "comment_id": cid, "user_id": "u1", "unique_id": "useru1",
            "nickname": "Hoa", "live_context": live}


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setattr(server_module, "LIVE_CATALOG_PATH", tmp_path / "live.json")
    catalog = LiveCatalogStore()
    catalog.sync = lambda snap: {"room_id": snap.get("room_id", ""), "pin_id": snap.get("pin_id", ""),
                                 "products": snap.get("products", []), "product_count": len(snap.get("products", [])),
                                 "synced_at": "now"}
    reporter = MissingReporter()
    reporter.path = tmp_path / "missing.jsonl"
    memory = CustomerMemory(tmp_path / "memory.sqlite3", tmp_path / "journals", session_id="s1")
    data = {"nhom_san_pham": [{"san_pham": [{
        "san_pham_id": "SP-03", "ten_san_pham": "Kích Rễ Giâm Cành TC01", "approved_for_live": True,
        "shop_product_ids": ["p1"], "listing_aliases": "TC01",
        "huong_dan_su_dung": "Cây mới trồng: pha 10ml cho bình 16 lít", "cong_dung": "Hỗ trợ kích rễ"}]}]}
    (tmp_path / "k.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "p.json").write_text('{"products":{}}', encoding="utf-8")
    eng = ReplyEngine(responder=Responder(), memory=memory,
                      knowledge=KnowledgeBase(tmp_path / "k.json", tmp_path / "p.json"),
                      catalog_store=catalog, missing_reporter=reporter)
    yield eng
    eng.close()


def _rule(engine, approve, **extra):
    with engine.workspace._db() as db:
        rid = dose_rules.import_rules(db, [{**RULE, **extra}])["rule_ids"][0]
        if approve:
            dose_rules.review_rule(db, rid, "approve")
    return rid


def _dose(engine, cid):
    return engine.handle(_payload("TC01 pha bình 16 lít bao nhiêu", cid, _live()))


def _asks(engine):
    return [c for c in engine.responder.calls if c["context"].get("ask") == "select_dose_rule"]


# ------------------------------------------------------------------ D05
def test_valid_rule_computed(engine):
    _rule(engine, True)
    out = _dose(engine, "d1")
    assert out["should_send"] and out["reply"] == "Dạ câu trả lời đủ dữ kiện ạ."
    assert len(_asks(engine)) == 1
    final = engine.responder.calls[-1]["context"]
    assert "33" in json.dumps(final["verified_dose_calculations"]) and final["dose_rule_selected"]["rule_id"]


def test_unapproved_rule_never_offered(engine):
    _rule(engine, False, amount=77)
    out = _dose(engine, "d2")
    assert out["should_send"] and not _asks(engine)
    assert "77" not in json.dumps(engine.responder.calls[-1]["context"].get("verified_dose_calculations"))


def test_no_rules_unchanged_behaviour(engine):
    out = _dose(engine, "d3")
    assert out["should_send"] and not _asks(engine)
    assert "10" in json.dumps(engine.responder.calls[-1]["context"]["verified_dose_calculations"])


def test_foreign_rule_rejected_then_handoff(engine):
    _rule(engine, True)
    foreign = _rule(engine, True, sku="SP-99", amount=99)
    engine.responder.selections = [{"rule_id": foreign, "params": {"volume": 16, "unit": "l"}}] * 2
    out = _dose(engine, "d4")
    assert out["should_send"] and out["reply"] == dose_selection.DOSE_HANDOFF
    assert len(_asks(engine)) == 2


def test_single_correction_then_valid(engine):
    good = _rule(engine, True)
    engine.responder.selections = [{"rule_id": "nope", "params": {"volume": 16, "unit": "l"}},
                                   {"rule_id": good, "params": {"volume": 16, "unit": "l"}}]
    out = _dose(engine, "d5")
    assert out["reply"] == "Dạ câu trả lời đủ dữ kiện ạ."
    asks = _asks(engine)
    assert len(asks) == 2 and "correction" in asks[1]


def test_wrong_volume_rejected(engine):
    good = _rule(engine, True)
    engine.responder.selections = [{"rule_id": good, "params": {"volume": 20, "unit": "l"}}] * 2
    assert _dose(engine, "d6")["reply"] == dose_selection.DOSE_HANDOFF


def test_wrong_number_in_reply_is_replaced(engine):
    _rule(engine, True)
    engine.responder.final = "Pha 50ml cho bình 16 lít nha."
    assert _dose(engine, "d7")["reply"] == dose_selection.DOSE_HANDOFF


def test_shared_functions_are_the_shopee_ones():
    import shopee_ai
    assert shopee_ai.usable_dose_rules is dose_selection.usable_dose_rules
    assert shopee_ai.DOSE_HANDOFF == dose_selection.DOSE_HANDOFF


# ------------------------------------------------------------------ A04
def _ask(engine, cid, text="giá bao nhiêu vậy shop"):
    return engine.handle(_payload(text, cid, _live(pin="", two=True)))


def _clarify_calls(engine):
    return [c for c in engine.responder.calls if c["context"].get("ask") == "clarify_sku"]


def test_ambiguous_sku_uses_model_text(engine):
    engine.responder.clarify = ["Mình muốn chai 1L hay 5L ạ?"]
    out = _ask(engine, "a1")
    assert out["should_send"] and out["reply"] == "Mình muốn chai 1L hay 5L ạ?"
    ctx = _clarify_calls(engine)[0]["context"]
    assert len(ctx["catalog_candidates"]) == 3 and "giá" in ctx["conversation"]["current_message"]


def test_clarify_model_error_falls_back_to_named_listing(engine):
    engine.responder.fail_clarify = True
    out = _ask(engine, "a2")
    assert out["should_send"] and out["reply"].startswith("Shop đang có:")


def test_clarify_model_price_text_rejected(engine):
    engine.responder.clarify = ["Giá 999.000đ nha"]
    assert _ask(engine, "a3")["reply"].startswith("Shop đang có:")


def test_clarify_always_asks_model(engine):
    for n in range(2):
        out = _ask(engine, f"c{n}")
        assert out["should_send"]
        engine.memory.confirm_reply_sent(out["interaction_id"], out["reply_id"], receipt={"response_code": 0, "room_id": "room-1"}, allow_previous_session=True)
    assert len(_clarify_calls(engine)) == 2
    third = _ask(engine, "c2")
    assert third["should_send"] and len(_clarify_calls(engine)) == 3


def test_pinned_or_explicit_product_does_not_clarify(engine):
    out = engine.handle(_payload("giá bao nhiêu vậy shop", "p1", _live(pin="a2", two=True)))
    assert out["should_send"] and not _clarify_calls(engine)
    out = engine.handle(_payload("Humic Mai Vàng 5L giá bao nhiêu", "p2", _live(pin="", two=True)))
    assert not _clarify_calls(engine)


def test_unknown_recommendation_stays_silent(engine):
    out = _ask(engine, "r1", "Có loại nào phòng nấm rễ và tuyến trùng không?")
    assert not out["should_send"] and not _clarify_calls(engine)
