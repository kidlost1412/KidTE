"""Gói C: thẻ khách `the_khach` — dựng thuần, đọc có chặn, riêng tư, fail-open."""
import json
from datetime import datetime, timedelta

import pytest

import context_budget as cb
import customer_card as cc
from customer_memory import CustomerMemory

NOW = datetime(2026, 10, 5, 20, 0).astimezone()


def row(session, days_ago, comment="giá đạm cá", product="", hours=0):
    at = (NOW - timedelta(days=days_ago, hours=hours)).isoformat(timespec="seconds")
    return {"session_id": session, "happened_at": at, "comment": comment, "product_title": product}


def source(rows, facts=(), session="now"):
    return {"session_id": session, "rows": list(rows), "facts": list(facts)}


# ------------------------------------------------------------------ dựng thuần
def test_new_customer_is_la_and_tiny():
    card = cc.build_card(source([]), NOW)
    assert card == {"quen_la": "lạ", "so_lan_ghe": 1}


def test_same_session_history_is_still_a_stranger():
    card = cc.build_card(source([row("now", 0, "hello")]), NOW)
    assert card["quen_la"] == "lạ" and card["so_lan_ghe"] == 1 and "lan_gan_nhat" not in card


def test_returning_and_regular_by_sessions_and_count():
    one = cc.build_card(source([row("s1", 1, "giá đạm cá")]), NOW)
    assert one["quen_la"] == "gặp lại" and one["so_lan_ghe"] == 2 and one["lan_gan_nhat"] == "hôm qua"
    two = cc.build_card(source([row("s2", 3), row("s1", 10)]), NOW)
    assert two["quen_la"] == "quen" and two["so_lan_ghe"] == 3 and two["lan_gan_nhat"] == "3 ngày trước"
    many = cc.build_card(source([row("s1", 20, f"câu {i}") for i in range(8)]), NOW)
    assert many["quen_la"] == "quen"  # một phiên nhưng nói nhiều


@pytest.mark.parametrize("days,label", [(0, "hôm nay (live trước)"), (1, "hôm qua"), (5, "5 ngày trước"),
                                        (21, "3 tuần trước"), (90, "3 tháng trước")])
def test_relative_time_labels(days, label):
    assert cc.relative_time(NOW - timedelta(days=days, hours=1), NOW) == label or days == 0
    if days:
        assert cc.relative_time(NOW - timedelta(days=days), NOW) == label


def test_products_facts_bought_topic_and_bounds():
    rows = [row("s1", 2, "hello", "Đạm Cá"),                      # xã giao: bỏ qua sản phẩm
            row("s1", 2, "giá đạm cá", "Đạm Cá Hữu Cơ Amino Giúp Cây Bật Mầm Xanh Lá Rất Dài Tên"),
            row("s1", 3, "kích rễ", "Kích Rễ TC01"), row("s1", 4, "nano", "Nano Tonic"),
            row("s1", 5, "combo", "Combo X"), row("s1", 6, "mua rồi nhận hàng ok", "")]
    facts = [("cay_trong", "sầu riêng"), ("quy_mo", "2 héc ta"), ("dien_tich", "5 sào"), ("van_de", "vàng lá"),
             ("san_pham_quan_tam", "Đạm Cá"), ("cach_xung_ho", "chú"), ("khac", "x" * 200)]
    card = cc.build_card(source(rows, facts), NOW)
    assert len(card["san_pham_da_hoi"]) == cc.MAX_PRODUCTS
    assert all(len(name) <= 40 and not name.endswith(("(", "+")) for name in card["san_pham_da_hoi"])
    assert card["da_mua"] is True and card["cach_xung_ho"] == "chú" and card["cay_trong"] == "sầu riêng"
    assert 1 + len(card["ghi_nho"]) <= cc.MAX_FACTS                # cay_trong + ghi_nho <= 4
    assert "san_pham_quan_tam" not in card["ghi_nho"] and all(len(v) <= 40 for v in card["ghi_nho"].values())
    assert card["last_topic"] == "giá đạm cá"                     # bỏ câu xã giao, lấy câu thật mới nhất
    assert len(json.dumps(card, ensure_ascii=False)) < 600


def test_build_card_rejects_garbage_and_never_raises():
    assert cc.build_card(None) is None and cc.build_card("x") is None
    assert cc.build_card({"rows": [None, 5, {"happened_at": "bad"}], "facts": [(None, "x"), ("a", None)]})["quen_la"]


# ------------------------------------------------------------------ đọc từ CustomerMemory
@pytest.fixture
def mem(tmp_path):
    made = []

    def make(session):
        memory = CustomerMemory(tmp_path / "m.sqlite3", tmp_path / "j", session_id=session)
        made.append(memory)
        return memory
    yield make
    for memory in made:
        memory.close()


def say(memory, text, cid, uid="u1", product="", reply=""):
    ref = memory.record_comment(text, comment_id=cid, room_id="r", platform_user_id=uid, unique_id=f"user{uid}",
                                display_name="Hoa")
    if reply:
        memory.record_reply(ref["interaction_id"], reply, complete=True, product={"title": product,
                                                                                  "product_id": "p"})
    return ref


def test_card_from_real_memory_new_returning_regular(mem):
    first = mem("s1")
    ref = say(first, "hello", "c1")
    assert cc.for_customer(first, ref["customer_key"], ref["interaction_id"]) == {"quen_la": "lạ", "so_lan_ghe": 1}
    say(first, "giá đạm cá", "c2", reply="Gốc 289k nha", product="Đạm Cá 5L")
    second = mem("s2")
    ref = say(second, "hello", "c3")
    card = cc.for_customer(second, ref["customer_key"], ref["interaction_id"])
    assert card["quen_la"] == "gặp lại" and card["so_lan_ghe"] == 2 and card["san_pham_da_hoi"] == ["Đạm Cá 5L"]
    third = mem("s3")
    ref = say(third, "hello", "c4")
    card = cc.for_customer(third, ref["customer_key"], ref["interaction_id"])
    assert card["quen_la"] == "quen" and card["so_lan_ghe"] == 3
    assert card["last_topic"] == "giá đạm cá"


def test_card_contains_no_foreign_customer_data(mem):
    memory = mem("s1")
    say(memory, "bí mật của người khác: vàng lá sầu riêng", "o1", uid="other", product="SP Riêng Tư")
    ref = say(memory, "hello", "m1", uid="me")
    card = cc.for_customer(memory, ref["customer_key"], ref["interaction_id"])
    assert card == {"quen_la": "lạ", "so_lan_ghe": 1}
    blob = json.dumps(card, ensure_ascii=False)
    assert "bí mật" not in blob and "Riêng Tư" not in blob and "sầu riêng" not in blob


def test_opt_out_tombstone_and_weak_identity_give_no_card(mem):
    memory = mem("s1")
    ref = say(memory, "giá đạm cá", "a1")
    other = mem("s2")
    key = ref["customer_key"]
    assert cc.for_customer(other, key, "") is not None
    with other._lock, other._db:
        other._db.execute("INSERT INTO memory_opt_outs(customer_key, display_name, opted_out_at) VALUES (?, '', 'x')",
                          (key,))
    assert cc.for_customer(other, key, "") is None and other.card_source(key) is None
    weak = other.record_comment("hello", comment_id="w1", display_name="Chỉ có tên")
    assert ":name:" in weak["customer_key"]
    assert cc.for_customer(other, weak["customer_key"], weak["interaction_id"]) is None
    other.privacy = type("P", (), {"is_forgotten": lambda self, k: True})()
    fresh = say(memory, "x", "z1", uid="zz")
    assert cc.for_customer(other, fresh["customer_key"], "") is None


def test_queries_are_bounded(mem):
    memory = mem("s1")
    for i in range(120):
        say(memory, f"câu {i}", f"b{i}")
    ref = say(memory, "mới", "bn")
    source_rows = memory.card_source(ref["customer_key"], ref["interaction_id"])
    assert len(source_rows["rows"]) == 60 and len(source_rows["facts"]) <= 8
    plan = " ".join(str(r[-1]) for r in memory._db.execute(
        "EXPLAIN QUERY PLAN SELECT session_id FROM interactions WHERE customer_key = ? AND event_type = 'comment' "
        "AND interaction_id != ? ORDER BY happened_at DESC LIMIT 60", (ref["customer_key"], "x")).fetchall())
    assert "idx_interactions_customer_time" in plan


def test_flag_off_and_failures_fail_open(mem, monkeypatch, tmp_path):
    memory = mem("s1")
    ref = say(memory, "hello", "f1")
    path = tmp_path / "flags.json"
    path.write_text(json.dumps({"customer_card": False}), encoding="utf-8")
    monkeypatch.setattr(cb, "FLAGS_PATH", path)
    assert cc.for_customer(memory, ref["customer_key"], ref["interaction_id"]) is None
    monkeypatch.setattr(cb, "FLAGS_PATH", tmp_path / "none.json")
    assert cc.for_customer(memory, ref["customer_key"], ref["interaction_id"]) is not None
    monkeypatch.setattr(memory, "card_source", lambda *a, **k: 1 / 0)
    assert cc.for_customer(memory, ref["customer_key"], ref["interaction_id"]) is None
    assert cc.for_customer(None, "k") is None


def test_recent_turns_view_is_compact_and_only_confirmed_replies():
    turns = [{"comment": "hello", "response": "Hello anh nha", "delivery_status": "sent"},
             {"comment": "x" * 300, "response": "chưa gửi", "delivery_status": "generated"},
             {"comment": "", "response": "a"}, "junk", {"comment": "d", "resolved_question": "câu đã giải mã",
                                                        "response": "r" * 400, "delivery_status": "sent"},
             {"comment": "thứ tư"}]
    view = cc.recent_turns_view(turns, limit=3)
    assert len(view) == 3 and view[0] == {"khach": "hello", "tra_loi": "Hello anh nha"}
    assert "tra_loi" not in view[1] and len(view[1]["khach"]) == 80
    assert view[2]["khach"] == "câu đã giải mã" and len(view[2]["tra_loi"]) == 100
    assert cc.recent_turns_view(None) == [] and cc.recent_turns_view(5) == []
