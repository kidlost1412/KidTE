"""Gói C "giữ tính cách, biết khách": hai làn cùng nền cố định, thẻ khách, sổ tay, cờ, fail-open, Shopee."""
import json
import time
from types import SimpleNamespace

import pytest

import context_budget as cb
import ctx_harness as h
import customer_card as cc
import sales_playbook as sp
import server as server_module
from customer_memory import CustomerMemory
from learning import LearningStore
from shopee_ai import ShopeeAiEngine
from server import ReplyEngine

PLAYBOOK = {"entries": [
    {"id": "ban_chiu", "tinh_huong": "Hỏi bán chịu", "dau_hieu": ["bán chịu", "cho nợ"],
     "huong_xu_ly": "Đùa duyên rồi chuyển COD, xem hàng trước khi nhận.",
     "du_kien_duoc_dung": ["thong_tin_shop.giao_hang"], "cam": ["không hứa nợ thật"]},
    {"id": "che_dat", "tinh_huong": "Chê đắt", "dau_hieu": ["đắt quá"], "huong_xu_ly": "Nhắc mã, mời chốt.",
     "du_kien_duoc_dung": [], "cam": ["không bịa số giảm"]}]}
PERSONA = {"name": "Cô Hoa", "description": "Vui, lanh", "style_guide": "Xưng em, gọi anh, nói ngắn", "activate": True}
RULE = "Khách chỉ chào thì chào lại ngắn, đừng hỏi bán hàng."
SHOP = {"ten_cong_ty": "Nông Dược TC", "giao_hang": "giao toàn quốc 3 - 4 ngày, xem hàng trước khi nhận"}


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr(server_module, "LIVE_CATALOG_PATH", tmp_path / "live.json")
    monkeypatch.setattr(cb, "FLAGS_PATH", tmp_path / "no_flags.json")
    monkeypatch.setattr(sp, "PLAYBOOK_PATH", tmp_path / "shared" / "sales_playbook.json")
    sp.save(PLAYBOOK)
    engine, captured = h.make_engine(tmp_path)
    engine.shop_info = dict(SHOP)
    engine.learning.save_persona(PERSONA)
    engine.learning.add_item(kind="rule", statement=RULE, status="active")
    yield engine, captured, tmp_path, monkeypatch
    engine.close()


def flags(monkeypatch, tmp_path, **values):
    path = tmp_path / "flags.json"
    path.write_text(json.dumps(values), encoding="utf-8")
    monkeypatch.setattr(cb, "FLAGS_PATH", path)


def ask(engine, captured, text, cid, uid):
    before = len(captured)
    out = engine.handle(h.payload(text, cid=cid, uid=uid))
    return out, captured[before:]


def parts(call):
    messages = call["messages"]
    stable = json.loads(messages[1]["content"].split("\n", 1)[1])
    turn = json.loads(messages[-1]["content"].split("\n", 1)[1].split("\nKHÁCH")[0])
    return stable, turn


def lane(out):
    return (out.get("metrics") or {}).get("context_lane", "full")


def old_session(tmp_path, uid, sessions, text="giá đạm cá"):
    """Khách có sẵn lịch sử ở `sessions` phiên LIVE trước (cùng file DB với engine)."""
    for number in range(sessions):
        memory = CustomerMemory(tmp_path / "memory.sqlite3", tmp_path / "journals", session_id=f"old{number}")
        ref = memory.record_comment(text, comment_id=f"o{uid}{number}", room_id="r", platform_user_id=uid,
                                    unique_id=f"user{uid}", display_name="Hoa")
        memory.record_reply(ref["interaction_id"], "Gốc 289k nha", complete=True,
                            product={"title": "Đạm Cá Hữu Cơ Amino (Can 5 Lít)", "product_id": "p3"})
        memory.close()


# ------------------------------------------------------------------ TikTok: hai làn
def test_both_lanes_carry_persona_rules_playbook_and_card(rig):
    engine, captured, _tmp, _mp = rig
    out_small, calls_small = ask(engine, captured, "hello", "a1", "n1")
    out_full, calls_full = ask(engine, captured, "TC01 giá bao nhiêu", "a2", "n2")
    assert lane(out_small) == "smalltalk" and lane(out_full) == "full"
    for call in (calls_small[0], calls_full[0]):
        stable, turn = parts(call)
        assert stable["phong_cach_nguoi_truc"]["ten"] == "Cô Hoa"
        assert stable["quy_tac_da_hoc"] == [RULE]
        assert [row["id"] for row in stable["sales_playbook"]] == ["ban_chiu", "che_dat"]
        assert stable["thong_tin_shop"] == SHOP and stable["all_live_products"] and "price_policy" in stable
        assert turn["the_khach"] == {"quen_la": "lạ", "so_lan_ghe": 1}
    # phần theo lượt của làn xã giao vẫn nhỏ: không hồ sơ/giá/liều
    _stable, small_turn = parts(calls_small[0])
    for absent in ("approved_product_knowledge", "current_product", "relevant_approved_products", "price"):
        assert absent not in small_turn
    assert len(calls_small[0]["messages"][-1]["content"]) < 900


def test_stable_block_byte_identical_across_lanes_turns_and_customers(rig):
    engine, captured, _tmp, _mp = rig
    texts = ["hello", "TC01 giá bao nhiêu", "ok", "kích rễ pha như nào", "có bán chịu không em", "[laugh]"]
    stables, lanes = [], set()
    for index, text in enumerate(texts):
        out, calls = ask(engine, captured, text, f"b{index}", f"u{index % 3}")
        lanes.add(lane(out))
        stables.append(calls[0]["messages"][1]["content"])
    assert lanes == {"smalltalk", "full"}
    assert len(set(stables)) == 1
    first_system = {c["messages"][0]["content"] for c in captured}
    assert len(first_system) == 1


def test_regular_customer_is_recognised_new_one_is_not(rig):
    engine, captured, tmp_path, _mp = rig
    old_session(tmp_path, "reg", 2)
    old_session(tmp_path, "back", 1)
    _o, calls = ask(engine, captured, "hello", "r1", "reg")
    card = parts(calls[0])[1]["the_khach"]
    assert card["quen_la"] == "quen" and card["so_lan_ghe"] == 3 and card["san_pham_da_hoi"] == ["Đạm Cá Hữu Cơ Amino"]
    assert "lan_gan_nhat" in card and card["last_topic"] == "giá đạm cá"
    _o, calls = ask(engine, captured, "hello", "r2", "back")
    assert parts(calls[0])[1]["the_khach"]["quen_la"] == "gặp lại"
    _o, calls = ask(engine, captured, "hello", "r3", "nobody")
    assert parts(calls[0])[1]["the_khach"] == {"quen_la": "lạ", "so_lan_ghe": 1}


def test_card_never_leaks_other_customers(rig):
    engine, captured, tmp_path, _mp = rig
    old_session(tmp_path, "someone", 3, text="bí mật vàng lá sầu riêng")
    _o, calls = ask(engine, captured, "hello", "l1", "stranger")
    body = json.dumps(calls[0]["messages"], ensure_ascii=False)
    assert "bí mật" not in body and "sầu riêng" not in body
    assert parts(calls[0])[1]["the_khach"]["quen_la"] == "lạ"


def test_lane_turn_part_has_recent_turns_examples_and_recent_replies(rig):
    engine, captured, _tmp, monkeypatch = rig
    monkeypatch.setattr(ReplyEngine, "_lane_examples", lambda self, comment, persona_id="": [
        {"question": "chào", "approved_answer": "Hello anh nha"}])
    engine._sent_replies.append((time.monotonic(), "Hello anh nha"))
    ask(engine, captured, "TC01 giá bao nhiêu", "t1", "same")
    with engine.memory._lock, engine.memory._db:     # lượt trước đã gửi xong => "hello" không phải câu nối dở
        engine.memory._db.execute("UPDATE interactions SET delivery_status='sent' WHERE response != ''")
    out, calls = ask(engine, captured, "hello", "t2", "same")
    assert lane(out) == "smalltalk"
    _stable, turn = parts(calls[0])
    assert turn["operator_approved_reply_examples"] == [{"question": "chào", "approved_answer": "Hello anh nha"}]
    assert turn["cau_da_gui_gan_day"] == ["Hello anh nha"]
    assert turn["luot_gan_day"][0]["khach"] == "TC01 giá bao nhiêu"
    assert "customer_memory" not in turn            # đã có thẻ + lượt gần nhất thay cho cắt 420 ký tự


def test_playbook_hint_only_on_matching_turns(rig):
    engine, captured, _tmp, _mp = rig
    out, calls = ask(engine, captured, "có bán chịu không em", "p1", "p1")
    assert parts(calls[0])[1]["tinh_huong_phu_hop"] == ["ban_chiu"] and out["metrics"]["playbook_hint"] == ["ban_chiu"]
    _o, calls = ask(engine, captured, "TC01 giá bao nhiêu", "p2", "p2")
    assert "tinh_huong_phu_hop" not in parts(calls[0])[1]


def test_legacy_prompt_override_is_ignored_not_a_full_replacement(rig):
    """File ghi đè kiểu cũ KHÔNG còn thay lời nhắc: luật gốc luôn được dùng, cập nhật luật tới được AI."""
    engine, captured, tmp_path, monkeypatch = rig
    override = tmp_path / "override.txt"
    override.write_text("Lời nhắc riêng của chủ shop", encoding="utf-8")
    monkeypatch.setattr(server_module._model_processor, "SYSTEM_PROMPT_OVERRIDE_PATH", override)
    _o, small = ask(engine, captured, "hello", "o1", "o1")
    _o, full = ask(engine, captured, "TC01 giá bao nhiêu", "o2", "o2")
    assert small[0]["messages"][1]["content"] == full[0]["messages"][1]["content"]
    system = full[0]["messages"][0]["content"]
    assert "Lời nhắc riêng của chủ shop" not in system
    assert server_module._model_processor.default_system_prompt() in system
    assert server_module._model_processor.has_prompt_override() is False


def test_default_system_prompt_documents_card_and_playbook():
    text = server_module._model_processor.default_system_prompt()
    assert "the_khach" in text and "sales_playbook" in text and "tinh_huong_phu_hop" in text
    assert "đừng đáp cụt" in text and "khách lạ" in text.lower()


# ------------------------------------------------------------------ cờ
def test_flags_off_restore_current_behaviour(rig):
    engine, captured, tmp_path, monkeypatch = rig
    flags(monkeypatch, tmp_path, persona_all_lanes=False, customer_card=False, playbook=False)
    out, calls = ask(engine, captured, "hello", "f1", "f1")
    assert lane(out) == "smalltalk"
    body = json.dumps(calls[0]["messages"][1:], ensure_ascii=False)       # bỏ lời nhắc hệ thống (tĩnh)
    for absent in ("all_live_products", "quy_tac_da_hoc", "sales_playbook", "the_khach", "luot_gan_day"):
        assert absent not in body, absent
    assert "phong_cach_nguoi_truc" in body and "thong_tin_shop" in body       # hành vi Gói A giữ nguyên
    _o, calls = ask(engine, captured, "có bán chịu không em", "f2", "f2")
    body = json.dumps(calls[0]["messages"][1:], ensure_ascii=False)
    assert "sales_playbook" not in body and "tinh_huong_phu_hop" not in body and "the_khach" not in body


@pytest.mark.parametrize("off,gone,kept", [
    ("customer_card", "the_khach", "sales_playbook"), ("playbook", "sales_playbook", "the_khach")])
def test_each_flag_is_independent(rig, off, gone, kept):
    engine, captured, tmp_path, monkeypatch = rig
    flags(monkeypatch, tmp_path, **{off: False})
    for text, cid in (("hello", "i1"), ("TC01 giá bao nhiêu", "i2")):
        _o, calls = ask(engine, captured, text, cid, cid)
        body = json.dumps(calls[0]["messages"][1:], ensure_ascii=False)
        assert gone not in body and kept in body


# ------------------------------------------------------------------ fail-open
@pytest.mark.parametrize("target,name", [
    (cc, "for_customer"), (sp, "hint_ids"), (sp, "stable_context"), (sp, "match"), (cc, "build_card"),
    (cc, "recent_turns_view")])
def test_failures_never_block_the_reply(rig, target, name):
    engine, captured, _tmp, monkeypatch = rig
    monkeypatch.setattr(target, name, lambda *a, **k: 1 / 0)
    for text, cid in (("hello", "x1"), ("có bán chịu không em", "x2")):
        out, calls = ask(engine, captured, text, cid, cid)
        assert out["should_send"] is True and calls


def test_card_source_failure_and_lane_examples_failure_fail_open(rig):
    engine, captured, _tmp, monkeypatch = rig
    monkeypatch.setattr(engine.memory, "card_source", lambda *a, **k: 1 / 0)
    monkeypatch.setattr(engine.workspace, "reviewed_examples", lambda *a, **k: 1 / 0)
    out, calls = ask(engine, captured, "hello", "y1", "y1")
    assert out["should_send"] and lane(out) == "smalltalk" and "the_khach" not in json.dumps(calls[0]["messages"][1:])


def test_persona_lane_failure_falls_back_to_full_context(rig):
    engine, captured, _tmp, monkeypatch = rig
    monkeypatch.setattr(ReplyEngine, "_lane_stable_context", lambda *a, **k: 1 / 0)
    out, calls = ask(engine, captured, "hello", "z1", "z1")
    assert out["should_send"] and lane(out) != "smalltalk"
    assert "all_live_products" in json.dumps(calls[0]["messages"], ensure_ascii=False)


def test_preview_context_includes_playbook_in_stable(rig):
    engine, _captured, _tmp, _mp = rig
    live = h.live_snapshot()
    sel = engine._select_product("TC01 giá bao nhiêu", live)
    context, _signal = engine._reply_context("TC01 giá bao nhiêu", live, sel, "", mark_used=False)
    assert [row["id"] for row in context["sales_playbook"]] == ["ban_chiu", "che_dat"]


# ------------------------------------------------------------------ Shopee
import test_router as tr  # noqa: E402  (dùng lại client giả + router)


def shopee(tmp_path, monkeypatch, route_answer, flags_path=None):
    monkeypatch.setattr(cb, "FLAGS_PATH", flags_path or tmp_path / "none.json")
    monkeypatch.setattr(sp, "PLAYBOOK_PATH", tmp_path / "shared" / "sales_playbook.json")
    sp.save(PLAYBOOK)
    tr.write_cfg(monkeypatch, tmp_path, enabled=True)
    learning = LearningStore(tmp_path / "learn.sqlite3")
    learning.save_persona(PERSONA)
    learning.add_item(kind="rule", statement=RULE, status="active")
    products = [{"shop_id": "10", "product_id": pid, "title": title,
                 "price_data": {"currency": "VND", "price": "80000", "price_before_discount": "80000"}}
                for pid, title in (("20", "Kích Rễ TC01"), ("21", "Đạm Cá Amino 5 Lít"))]
    catalog = {"session_id": "123", "updated_at": time.time(), "products": products}
    client = tr.Client(route_answer)
    owner = SimpleNamespace(
        responder=tr.Recorder(client), knowledge=tr.Knowledge(), learning=learning, shop_info=dict(SHOP),
        _persona_context=ReplyEngine._persona_context, _lane_examples=lambda comment, persona_id="": [],
        shopee_mapping_state=lambda: {"catalog": catalog, "videos": {
            "v": {"mappings": {"tc01": {"shop_id": "10", "product_id": "20"}}}}})
    return owner, ShopeeAiEngine(owner, tmp_path / "m.sqlite3", tmp_path / "j")


def sask(engine, text, cid, user="77"):
    return engine.reply({"session_id": "123", "video_id": "v", "comment_id": cid, "user_id": user,
                         "nickname": "Mai", "comment": text, "shop_id": "", "product_id": "", "title": ""})


def stable_of(context):
    stable, turn = server_module._model_processor.split_context(context)
    return json.dumps(stable, ensure_ascii=False, separators=(",", ":"), sort_keys=True), stable, turn


def test_shopee_both_lanes_share_stable_and_carry_card_persona_playbook(tmp_path, monkeypatch):
    owner, engine = shopee(tmp_path, monkeypatch, tr.answer(acts=["banter"]))
    try:
        assert sask(engine, "hihi haha", "s1")["should_send"]                                # làn xã giao (router)
        assert sask(engine, "Kích Rễ TC01 dùng sao?", "s2", user="88")["should_send"]        # làn đầy đủ
        assert sask(engine, "Kích Rễ TC01 có bán chịu không em", "s3", user="99")["should_send"]
        small, full, hint = owner.responder.contexts
        small_json, small_stable, small_turn = stable_of(small)
        full_json, full_stable, full_turn = stable_of(full)
        assert "current_product" not in small and "current_product" in full
        assert small_json == full_json == stable_of(hint)[0]
        assert full_stable["phong_cach_nguoi_truc"]["ten"] == "Cô Hoa" and full_stable["quy_tac_da_hoc"] == [RULE]
        assert [row["id"] for row in full_stable["sales_playbook"]] == ["ban_chiu", "che_dat"]
        assert full_stable["thong_tin_shop"] == SHOP and "price_policy" in full_stable
        assert small_turn["the_khach"] == {"quen_la": "lạ", "so_lan_ghe": 1} == full_turn["the_khach"]
        assert "approved_product_knowledge" not in small_turn
        assert small_turn["doc_y_khach"]["acts"] == ["banter"]
        assert stable_of(hint)[2]["tinh_huong_phu_hop"] == ["ban_chiu"]
        assert "tinh_huong_phu_hop" not in small_turn and "tinh_huong_phu_hop" not in full_turn
    finally:
        engine.close()


def test_shopee_regular_customer_card(tmp_path, monkeypatch):
    owner, engine = shopee(tmp_path, monkeypatch, tr.answer(acts=["banter"]))
    try:
        for number in range(2):
            memory = CustomerMemory(engine.memory.db_path, tmp_path / "jold", session_id=f"old{number}",
                                    platform="shopee")
            ref = memory.record_comment("giá kích rễ", comment_id=f"shopee:old{number}", room_id="shopee:9",
                                        platform_user_id="55", display_name="Mai")
            memory.record_reply(ref["interaction_id"], "80k nha", complete=True,
                                product={"title": "Kích Rễ TC01", "product_id": "20"})
            memory.close()
        assert sask(engine, "hihi haha", "r1", user="55")["should_send"]
        card = stable_of(owner.responder.contexts[-1])[2]["the_khach"]
        assert card["quen_la"] == "quen" and card["san_pham_da_hoi"] == ["Kích Rễ TC01"]
        assert card["last_topic"] == "giá kích rễ"
    finally:
        engine.close()


def test_shopee_flags_off_restore_old_lane_and_fail_open(tmp_path, monkeypatch):
    flags_file = tmp_path / "flags.json"
    flags_file.write_text(json.dumps({"persona_all_lanes": False, "customer_card": False, "playbook": False}))
    owner, engine = shopee(tmp_path, monkeypatch, tr.answer(acts=["banter"]), flags_path=flags_file)
    try:
        assert sask(engine, "hihi haha", "o1")["should_send"]
        context = owner.responder.contexts[-1]
        assert set(context) >= {"phong_cach_nguoi_truc", "thong_tin_shop"}
        assert not {"sales_playbook", "the_khach", "quy_tac_da_hoc", "price_policy", "luot_gan_day"} & set(context)
        monkeypatch.setattr(cb, "FLAGS_PATH", tmp_path / "none.json")
        monkeypatch.setattr(cc, "for_customer", lambda *a, **k: 1 / 0)
        monkeypatch.setattr(sp, "stable_context", lambda *a, **k: 1 / 0)
        monkeypatch.setattr(sp, "hint_ids", lambda *a, **k: 1 / 0)
        assert sask(engine, "hihi haha", "o2", user="12")["should_send"]
        assert sask(engine, "Kích Rễ TC01 dùng sao?", "o3", user="13")["should_send"]
    finally:
        engine.close()


def test_shopee_lane_failure_keeps_full_context(tmp_path, monkeypatch):
    owner, engine = shopee(tmp_path, monkeypatch, tr.answer(acts=["banter"]))
    try:
        monkeypatch.setattr(sp, "stable_context", lambda *a, **k: 1 / 0)
        monkeypatch.setattr(owner, "_persona_context", lambda *a, **k: 1 / 0)
        assert sask(engine, "hihi haha", "q1")["should_send"]
    finally:
        engine.close()
