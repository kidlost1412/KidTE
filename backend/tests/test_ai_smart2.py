"""Đợt AI 2 (rà soát 17/10): tồn kho theo lượt, câu nhiều ý, góc nói theo chủ đề, y_khach,
nhiệt độ theo loại lượt, rút gọn thay vì bỏ, chi phí cộng dồn, hạn tổng một lượt."""
import json
import random
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import context_budget as cb
import ctx_harness as h
import reply_angles
import server as server_module
from processor_loader import load_processor

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = load_processor(ROOT / "plugins" / "model-deepseek" / "processor.py", "model-smart2")

PROFILE = {"ten_san_pham": "Kích Rễ TC01", "quy_cach_dong_goi": "Chai 100ml", "gia_goc_duyet": "120.000₫",
           "cong_dung": ["Kích rễ lúa", "Đẻ nhánh khỏe"], "huong_dan_su_dung": ["Pha 10ml cho bình 16 lít"],
           "doi_tuong_cay_trong": ["Lúa", "Cây ăn trái"], "luu_y": "Phun sáng sớm.", "mo_ta_ngan": "Kích rễ"}


# ------------------------------------------------------------------ P3 câu nhiều ý
def test_multi_question_keeps_usage_fields_with_price():
    comment = "giá bao nhiêu, bón cho lúa được không, có quà không"
    intents = {"price": True, "promotion": True}
    out = cb.project_profile(PROFILE, intents, comment)
    assert out["cong_dung"] and out["huong_dan_su_dung"] and out["doi_tuong_cay_trong"]
    # Câu chỉ hỏi giá vẫn gọn như cũ.
    short = cb.project_profile(PROFILE, {"price": True}, "TC01 giá sao em")
    assert "cong_dung" not in short and short["tom_tat_cong_dung"]


def test_dose_plus_price_merges_groups_and_unknown_intent_gets_basic_fields():
    both = cb.intent_groups({"dose": True, "price": True}, "pha sao, giá nhiêu")
    assert both == {"dose", "usage"}
    basic = cb.project_profile(PROFILE, {}, "cái này sao em")
    assert {"cong_dung", "huong_dan_su_dung", "quy_cach_dong_goi"} <= set(basic)


def test_projection_respects_char_budget():
    big = {**PROFILE, "cong_dung": ["x" * 2000] * 10, "luu_y": "y" * 5000}
    out = cb.project_profile(big, {"technical": True}, "")
    assert len(json.dumps(out, ensure_ascii=False)) < 6000 and out["gia_goc_duyet"] == "120.000₫"


# ------------------------------------------------------------------ H2 tồn kho
def _item(pid, stock, skus=()):
    return {"product_id": pid, "stock_num": stock, "skus": list(skus)}


def test_stock_fact_low_out_unknown_and_plenty():
    others = [_item("b", 50)]
    assert cb.stock_fact(_item("a", 3), others, 10) == {"tinh_trang": "con_it", "so_luong": 3}
    assert cb.stock_fact(_item("a", 300), others, 10) is None              # còn nhiều: không nói
    out = cb.stock_fact(_item("a", 0), [_item("a", 0)] + others, 10, ["Nano Tonic"])
    assert out == {"tinh_trang": "het_hang", "san_pham_cung_cong_dung_con_hang": ["Nano Tonic"]}
    # Nguồn không đọc được tồn (mọi SKU = 0) -> không dám nói hết hàng.
    assert cb.stock_fact(_item("a", 0), [_item("a", 0), _item("b", 0)], 10) is None
    assert cb.stock_fact({"product_id": "a"}, others, 10) is None
    assert cb.stock_fact(_item("a", 0, [{"stock_num": 2}, {"stock_num": 1}]), others, 10)["so_luong"] == 3


@pytest.fixture
def rig(monkeypatch, tmp_path):
    monkeypatch.setattr(server_module, "LIVE_CATALOG_PATH", tmp_path / "live.json")
    monkeypatch.setattr(cb, "FLAGS_PATH", tmp_path / "no_flags.json")
    engine, captured = h.make_engine(tmp_path)
    try:
        yield engine, captured
    finally:
        engine.memory.close() if hasattr(engine.memory, "close") else None


def _ask(engine, text, cid, stock=None, uid="u1"):
    payload = h.payload(text, cid=cid, uid=uid)
    if stock is not None:
        for row in payload["live_context"]["products"]:
            if row["product_id"] == "p1":
                row["stock_num"] = stock
    return engine.handle(payload)


def test_stock_goes_to_turn_part_and_static_prefix_never_changes(rig):
    engine, captured = rig
    _ask(engine, "kích rễ TC01 dùng cho cây gì", "c1", stock=100, uid="u1")
    _ask(engine, "TC01 tưới mai vàng được không", "c2", stock=4, uid="u2")
    first, second = captured[0]["messages"], captured[-1]["messages"]
    assert first[0] == second[0] and first[1] == second[1]           # system + nền cố định: cache giữ
    assert "ton_kho" not in first[1]["content"] and "stock_num" not in first[-1]["content"]
    assert '"ton_kho":{"tinh_trang":"con_it","so_luong":4}' in second[-1]["content"]
    assert "ton_kho" not in first[-1]["content"]                     # còn nhiều: không đưa


# ------------------------------------------------------------------ P4 góc nói
def test_angles_follow_topic_and_drop_conflicting_ones():
    options = reply_angles.load(ROOT / "backend" / "prompts" / "reply_angles.json")
    ids = {row["id"] for group in options.values() for row in group}
    assert not ids & {"ma_uu_dai", "so_sanh_nhe", "kinh_nghiem", "da_vang"}
    picker = reply_angles.AnglePicker(options, rng=random.Random(3))
    for _ in range(30):
        angle = picker.pick(("smalltalk",), "k")["_ids"][0]
        assert angle in {"vao_thang", "bat_y_khach"}
        assert picker.pick(("dose",), "k")["_ids"][0] in {"vao_thang", "meo_dung", "dieu_kien_ngan"}
    assert reply_angles.topics_from_intents({"price": True, "dose": True}) == ("dose", "price")
    assert reply_angles.topics_from_intents({}) == ("general",)


def test_angles_never_repeat_back_to_back_for_same_customer():
    picker = reply_angles.AnglePicker(rng=random.Random(5), avoid_last=0)
    last = None
    for _ in range(40):
        angle = picker.pick(("price",), "khach-1")["_ids"][0]
        assert angle != last
        last = angle


# ------------------------------------------------------------------ responder giả
def _responder(replies, captured):
    queue = list(replies)

    def create(**kwargs):
        captured.append(kwargs)
        content = queue.pop(0) if queue else '{"action":"reply","reply":"ok anh"}'
        usage = SimpleNamespace(prompt_tokens=100, prompt_cache_hit_tokens=80, prompt_cache_miss_tokens=20,
                                completion_tokens=10)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content),
                                                        finish_reason="stop")], usage=usage)
    responder = PLUGIN.DeepSeekResponder.__new__(PLUGIN.DeepSeekResponder)
    responder.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    responder.disable_thinking = True
    return responder


# ------------------------------------------------------------------ P6 y_khach
def test_y_khach_requested_before_reply_and_never_sent():
    captured = []
    responder = _responder(['{"action":"reply","y_khach":"muốn biết bón lúa được không",'
                            '"reply":"Bón lúa ngon luôn anh"}', '{"action":"reply","reply":"Được anh"}'],
                           captured)
    out = responder.reply("an", "bón lúa được ko", {}, 120)
    assert out["reply"] == "Bón lúa ngon luôn anh" and "y_khach" not in json.dumps(out, ensure_ascii=False)
    assert "y_khach ĐẦU TIÊN" in captured[0]["messages"][0]["content"]
    assert responder.reply("an", "bón lúa được ko", {}, 120)["reply"] == "Được anh"   # thiếu trường vẫn nhận


# ------------------------------------------------------------------ P9 nhiệt độ theo lượt
def test_precise_turns_use_low_temperature_and_no_penalty():
    captured = []
    responder = _responder([], captured)
    responder.reply("an", "pha bao nhiêu ml", {"_sampling": "precise"}, 120)
    responder.reply("an", "chào em", {}, 120)
    precise, chat = captured
    assert precise["temperature"] <= 0.4 and "frequency_penalty" not in precise
    assert "_sampling" not in json.dumps(precise["messages"], ensure_ascii=False)
    assert chat["temperature"] == PLUGIN.SETTINGS["nhiet_do"] and chat["frequency_penalty"] > 0
    kind = server_module._sampling_kind
    assert kind("thời gian cách ly mấy ngày", {}, {}) == "precise"
    assert kind("giá sao", {"price": True}, {}) == "precise"
    assert kind("live đẹp quá", {}, {}) == ""


# ------------------------------------------------------------------ P10 rút gọn thay vì bỏ
def test_too_long_reply_is_fitted_at_sentence_not_dropped(monkeypatch):
    monkeypatch.setitem(PLUGIN.SETTINGS, "viet_gon_khi_dai", False)
    captured = []
    long = "Pha 20 ml cho bình 16 lít nha anh. Phun ướt đều hai mặt lá, nhắc lại sau 7 ngày nếu bệnh còn nặng."
    responder = _responder([json.dumps({"action": "reply", "reply": long}, ensure_ascii=False)], captured)
    out = responder.reply("an", "pha sao", {}, 60)
    assert out["action"] == "reply" and out["reply"] == "Pha 20 ml cho bình 16 lít nha anh."
    assert out["metrics"]["fitted_from"] > 60


def test_fit_never_splits_number_from_unit():
    fit = PLUGIN._fit_utf16
    assert fit("Pha 20 ml cho bình 16 lít, phun ướt đều hai mặt lá nha", 25) == "Pha 20 ml cho bình…"
    assert fit("Anh pha 20 ml cho 16 lít nước nhé", 12) == "Anh pha…"
    assert fit("Liều 20-30 ml cho bình 16 lít nhé anh", 40) == "Liều 20-30 ml cho bình 16 lít nhé anh"


# ------------------------------------------------------------------ C4 chi phí cộng dồn
def test_shorten_and_main_call_costs_are_summed():
    captured = []
    long = json.dumps({"action": "reply", "reply": "rất dài " * 40}, ensure_ascii=False)
    responder = _responder([long, '{"action":"reply","reply":"Gọn rồi anh"}'], captured)
    out = responder.reply("an", "hỏi", {}, 60)
    assert out["reply"] == "Gọn rồi anh"
    m = out["metrics"]
    assert m["prompt_tokens"] == 200 and m["prompt_cache_miss_tokens"] == 40 and m["model_calls"] == 2


def test_server_sums_every_model_call_in_the_turn():
    calls = [{"prompt_tokens": 100, "completion_tokens": 5, "model_ms": 900},
             {"prompt_tokens": 120, "completion_tokens": 7, "model_ms": 800, "model_calls": 2}]
    total = server_module._sum_model_metrics(calls[-1], calls)
    assert total["prompt_tokens"] == 220 and total["model_ms"] == 1700 and total["model_calls"] == 3
    assert server_module._sum_model_metrics({"prompt_tokens": 1}, [])["prompt_tokens"] == 1


# ------------------------------------------------------------------ C8 hạn tổng
def test_turn_budget_caps_timeout_and_stops_new_calls():
    captured = []
    responder = _responder([], captured)
    with PLUGIN.turn_budget(10):
        responder.reply("an", "hi", {}, 120)
    assert 0 < captured[0]["timeout"] <= 10
    with PLUGIN.turn_budget(0.5):
        out = responder.reply("an", "hi", {}, 120)
    assert out["action"] == "skip" and out["missing_fields"] == ["deepseek_TurnDeadline"]
    assert len(captured) == 1
    responder.reply("an", "hi", {}, 120)                     # ngoài khối: hành vi cũ, không timeout riêng
    assert "timeout" not in captured[-1] and PLUGIN.turn_remaining() is None


def test_slow_first_attempt_is_not_retried_inside_budget(monkeypatch):
    class APITimeoutError(Exception):
        pass
    attempts = []
    responder = PLUGIN.DeepSeekResponder.__new__(PLUGIN.DeepSeekResponder)
    responder.disable_thinking = True

    def create(**kwargs):
        attempts.append(kwargs)
        clock[0] += 13                                   # lần gọi treo 13 giây rồi hết giờ
        raise APITimeoutError()
    responder.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    clock = [1000.0]
    monkeypatch.setattr(PLUGIN.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(PLUGIN.time, "sleep", lambda s: None)
    with PLUGIN.turn_budget(25):
        out = responder.reply("an", "hi", {}, 120)
    assert out["action"] == "skip" and len(attempts) == 1
