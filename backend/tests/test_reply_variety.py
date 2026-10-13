"""Chống lặp câu + không tự nhận lỗi công khai (kiểm bằng code, sinh lại 1 lần)."""
import json
import pytest

import ctx_harness as h
import reply_variety as rv
import server as server_module
import context_budget as cb
import sales_playbook as sp


@pytest.mark.parametrize("text", [
    "Xin lỗi anh, em hiểu anh bực.", "Em hiểu anh đang bực lắm", "Em xin lỗi vì sự bất tiện", "Là lỗi của shop rồi anh",
    "Sai sót bên em, em đền anh"])
def test_admits_fault_catches_apology_and_confession(text):
    assert rv.admits_fault(text)


@pytest.mark.parametrize("text", [
    "Ủa thiếu quà hả anh, nhắn số đơn qua TikTok Shop em tra liền nha", "Anh pha 20ml cho bình 16 lít nha",
    "Hello anh nha", "Anh nhắn shop qua TikTok em kiểm tra giúp liền"])
def test_admits_fault_ignores_normal_replies(text):
    assert rv.admits_fault(text) == ""


def test_repeats_recent_detects_near_copy_and_same_opening():
    recents = ["Ơ anh ơi, em bất ngờ quá, anh gặp chuyện gì vậy?", "Hello anh nha, cần gì cứ hỏi em"]
    assert rv.repeats_recent("Ơ anh ơi, em bất ngờ quá, anh gặp chuyện gì vậy ạ?", recents)       # gần y nguyên
    assert rv.repeats_recent("Hello anh nha, ghé chơi hả", recents)                               # cùng chữ mở
    assert rv.repeats_recent("Nghe anh nói em giật mình, chuyện sao anh?", recents) == ""
    assert rv.repeats_recent("ok", recents) == "" and rv.repeats_recent("Hello anh nha", []) == ""


def test_openings_are_short_unique_and_bounded():
    got = rv.openings(["Ơ anh ơi, em tra liền", "Ơ anh ơi, em xem giúp", "Hello anh nha, cần gì", ""], limit=2)
    assert got == ["Ơ anh ơi", "Hello anh nha"]


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr(server_module, "LIVE_CATALOG_PATH", tmp_path / "live.json")
    monkeypatch.setattr(cb, "FLAGS_PATH", tmp_path / "no_flags.json")
    monkeypatch.setattr(sp, "PLAYBOOK_PATH", tmp_path / "shared" / "sales_playbook.json")
    engine, captured = h.make_engine(tmp_path)
    yield engine, captured
    engine.close()


def test_engine_regenerates_once_when_reply_apologises(rig):
    engine, captured = rig
    answers = iter(["Xin lỗi anh, em hiểu anh bực, anh nhắn TikTok Shop nha.",
                    "Ủa anh gặp chuyện gì vậy? Nhắn đơn qua TikTok Shop em tra liền nha."])
    original = engine.responder.client.chat.completions.create

    def create(**kw):
        original(**kw)
        text = next(answers)
        import types
        msg = types.SimpleNamespace(content=json.dumps({"action": "reply", "reply": text, "messages": [text]},
                                                       ensure_ascii=False), reasoning_content=None)
        usage = types.SimpleNamespace(prompt_tokens=1, completion_tokens=1, prompt_cache_hit_tokens=0,
                                      prompt_cache_miss_tokens=1)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg, finish_reason="stop")], usage=usage)
    engine.responder.client.chat.completions.create = create
    out = engine.handle(h.payload("Bán lừa ngta ko tặng", cid="x1", uid="x1"))
    assert "Xin lỗi" not in out["reply"] and "tra liền" in out["reply"]
    assert len(captured) == 2 and "công khai nhận lỗi" in json.dumps(captured[-1]["messages"], ensure_ascii=False)


def test_variety_correction_mentions_old_reply(rig):
    engine, _ = rig
    engine._sent_replies.append((__import__("time").monotonic(), "Hello anh nha, cần gì cứ hỏi em"))
    note = engine._variety_correction({"action": "reply", "reply": "Hello anh nha, ghé chơi hả"})
    assert "Hello anh nha, cần gì cứ hỏi em" in note
    assert engine._variety_correction({"action": "skip"}) == ""
