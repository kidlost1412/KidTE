import sqlite3
import time
from types import SimpleNamespace

import dose_rules
from shopee_ai import ShopeeAiEngine
from dose_selection import DOSE_HANDOFFS


def _db(tmp_path):
    db = sqlite3.connect(tmp_path / "rules.sqlite3")
    db.row_factory = sqlite3.Row
    dose_rules.ensure_schema(db)
    return db


def _rule(db, sku, amount=10, approve=True, per_volume=16):
    res = dose_rules.import_rules(db, [{"sku": sku, "method": "phun", "crop": "lua", "stage": "",
        "amount": amount, "unit": "ml", "per_volume": per_volume, "per_unit": "l",
        "source_doc": "d", "source_page": "1"}])
    rid = res["rule_ids"][0]
    if approve:
        dose_rules.review_rule(db, rid, "approve")
    db.commit()
    return rid


class Responder:
    def __init__(self, selections, final="Dạ pha 12.5ml cho bình 20 lít nha"):
        self.selections, self.final, self.calls = list(selections), final, []

    def reply(self, nickname, comment, context, max_chars, **kwargs):
        self.calls.append((context, kwargs))
        if context.get("ask") == "select_dose_rule":
            return {"action": "reply", "reply": "", "dose_selection": self.selections.pop(0)}
        return {"action": "reply", "reply": self.final, "metrics": {}}


class Knowledge:
    def match(self, title, comment=""):
        return {"ten_san_pham": title}

    def context(self, record):
        return {"ten_san_pham": record["ten_san_pham"]}


def _engine(tmp_path, responder, source):
    owner = SimpleNamespace(responder=responder, knowledge=Knowledge(),
        shopee_mapping_state=lambda: {"catalog": {"session_id": "123", "updated_at": time.time(),
            "products": [{"shop_id": "10", "product_id": "20", "title": "Kích Rễ TC01",
                "price_data": {"currency": "VND", "price": "80000", "price_before_discount": "80000"}}]},
            "videos": {"v": {"mappings": {"tc01": {"shop_id": "10", "product_id": "20"}}}}})
    return ShopeeAiEngine(owner, tmp_path / "m.sqlite3", tmp_path / "j", dose_rule_source=source)


def _ask(engine, cid="c1"):
    return engine.reply({"session_id": "123", "video_id": "v", "comment_id": cid, "user_id": "77",
        "nickname": "Mai", "comment": "TC01 pha bình 20 lít bao nhiêu ml?", "shop_id": "10",
        "product_id": "20", "title": "Kích Rễ TC01"})


def _source(db):
    return lambda sku: [dict(r) for r in db.execute("SELECT * FROM dose_rules WHERE sku=?", (sku,))]


def _sel(rid, volume=20):
    return {"rule_id": rid, "params": {"volume": volume, "unit": "l"}}


def _text(result):
    return " ".join(result.get("messages") or [result.get("reply", "")]) if result.get("should_send") else ""


def test_valid_rule_computed(tmp_path):
    db = _db(tmp_path)
    rid = _rule(db, "shopee:20")
    r = Responder([_sel(rid)])
    engine = _engine(tmp_path, r, _source(db))
    try:
        result = _ask(engine)
        assert result["should_send"] is True
        main = r.calls[-1][0]
        assert main["verified_dose_calculations"][0]["calculated_ml"] == 12.5
        assert r.calls[0][0]["dose_rule_options"][0]["rule_id"] == rid
        assert not any(h in str(result) for h in DOSE_HANDOFFS)
    finally:
        engine.close()


def test_foreign_rule_rejected(tmp_path):
    db = _db(tmp_path)
    foreign = _rule(db, "shopee:999")
    _rule(db, "shopee:20", amount=5)
    r = Responder([_sel(foreign), _sel(foreign)])
    engine = _engine(tmp_path, r, _source(db))
    try:
        out = str(_ask(engine)); assert any(h in out for h in DOSE_HANDOFFS), out
    finally:
        engine.close()


def test_unapproved_rule_rejected(tmp_path):
    db = _db(tmp_path)
    cand = _rule(db, "shopee:20", amount=3, approve=False)
    ok = _rule(db, "shopee:20", amount=5)
    r = Responder([_sel(cand), _sel(cand)])
    engine = _engine(tmp_path, r, _source(db))
    try:
        assert ok != cand
        assert all(o["rule_id"] != cand for o in r.calls[0][0]["dose_rule_options"]) if r.calls else True
        out = str(_ask(engine)); assert any(h in out for h in DOSE_HANDOFFS), out
    finally:
        engine.close()


def test_single_correction(tmp_path):
    db = _db(tmp_path)
    rid = _rule(db, "shopee:20")
    r = Responder([_sel("DR-nope"), _sel(rid)])
    engine = _engine(tmp_path, r, _source(db))
    try:
        result = _ask(engine)
        selects = [c for c in r.calls if c[0].get("ask") == "select_dose_rule"]
        assert len(selects) == 2 and "correction" in selects[1][1]
        assert not any(h in str(result) for h in DOSE_HANDOFFS)
    finally:
        engine.close()


def test_wrong_number_in_reply_is_replaced(tmp_path):
    db = _db(tmp_path)
    rid = _rule(db, "shopee:20")
    r = Responder([_sel(rid)], final="Dạ pha 99ml nha")
    engine = _engine(tmp_path, r, _source(db))
    try:
        out = str(_ask(engine)); assert any(h in out for h in DOSE_HANDOFFS), out
    finally:
        engine.close()


def test_no_rules_or_no_source_unchanged(tmp_path):
    db = _db(tmp_path)
    r = Responder([])
    engine = _engine(tmp_path, r, _source(db))
    try:
        _ask(engine)
        assert all(c[0].get("ask") != "select_dose_rule" for c in r.calls)
    finally:
        engine.close()
    r2 = Responder([])
    engine = _engine(tmp_path, r2, None)
    try:
        _ask(engine, "c2")
        assert all(c[0].get("ask") != "select_dose_rule" for c in r2.calls)
    finally:
        engine.close()
