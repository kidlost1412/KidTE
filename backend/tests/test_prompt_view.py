"""Xem toàn bộ lời nhắc + sửa luật bán hàng gốc an toàn khi cập nhật."""
import inspect
import json

import pytest

import ctx_harness as h
import context_budget as cb
import prompt_view as pv
import server as server_module
import sales_playbook as sp
from ai_context import APP_BRIEF
from learning import DEFAULT_VOICE

PROC = server_module._model_processor


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr(server_module, "LIVE_CATALOG_PATH", tmp_path / "live.json")
    monkeypatch.setattr(cb, "FLAGS_PATH", tmp_path / "no_flags.json")
    monkeypatch.setattr(sp, "PLAYBOOK_PATH", tmp_path / "shared" / "sales_playbook.json")
    monkeypatch.setattr(PROC, "AI_NOTES_PATH", tmp_path / "ghi_chu_ai.txt")
    monkeypatch.setattr(PROC, "SYSTEM_PROMPT_OVERRIDE_PATH", tmp_path / "loi_nhac_he_thong.txt")
    monkeypatch.setattr(pv, "OVERRIDE_PATH", tmp_path / "quy_tac_ban_hang.txt")
    monkeypatch.setattr(pv, "META_PATH", tmp_path / "quy_tac_ban_hang.meta.json")
    engine, _captured = h.make_engine(tmp_path)
    yield engine, tmp_path
    engine.close()


def shipped():
    return PROC.default_system_prompt()


def test_shipped_rules_contain_every_protected_line():
    assert pv.missing_protected(shipped()) == []


def test_assembly_order_brief_rules_notes(rig):
    _engine, tmp = rig
    (tmp / "ghi_chu_ai.txt").write_text("Luôn nhắc xem nhãn.\n", encoding="utf-8")
    system = PROC.compose_system()
    brief, rules, notes = (system.index(APP_BRIEF.strip()[:40]), system.index("ĐỘ DÀI VÀ ĐỊNH DẠNG"),
                           system.index(PROC.NOTES_HEADER))
    assert brief < rules < notes < system.index("Luôn nhắc xem nhãn.")


def test_view_sections_in_order_with_persona_voice(rig):
    engine, _ = rig
    view = engine.learning_prompt_view("")
    assert [s["id"] for s in view["sections"]] == ["brief", "rules", "notes", "persona"]
    assert [s["title"][:2] for s in view["sections"]] == ["1)", "2)", "3)", "4)"]
    brief, rules, _notes, persona = view["sections"]
    assert brief["text"] == APP_BRIEF.rstrip() and not brief["editable"]
    assert rules["text"] == shipped().strip() and not rules["customized"]
    assert persona["is_default"] and persona["text"].startswith(DEFAULT_VOICE)
    assert view["notice"] is None
    male = engine.learning.save_persona({"name": "Nam", "style_guide": "- Xưng anh"})
    view = engine.learning_prompt_view(male["persona_id"])
    assert view["sections"][3]["text"] == "- Xưng anh" and not view["sections"][3]["is_default"]


def test_override_is_used_stamped_and_reset(rig):
    engine, tmp = rig
    mine = shipped() + "\n- Luật riêng: luôn hỏi tên cây."
    out = engine.base_rules_action({"action": "save", "text": mine})
    assert out["saved"] and out["appended"] == 0
    assert "Luật riêng" in PROC.compose_system()
    meta = json.loads((tmp / "quy_tac_ban_hang.meta.json").read_text(encoding="utf-8"))
    assert meta["base_hash"] == pv.text_hash(shipped())
    view = engine.learning_prompt_view("")
    assert view["sections"][1]["customized"] and not view["rules_status"]["stale"]
    engine.base_rules_action({"action": "reset"})
    assert "Luật riêng" not in PROC.compose_system()
    assert not (tmp / "quy_tac_ban_hang.txt").exists() and not (tmp / "quy_tac_ban_hang.meta.json").exists()


def test_update_notice_and_three_choices(rig, monkeypatch):
    engine, _tmp = rig
    engine.base_rules_action({"action": "save", "text": shipped() + "\n- Luật riêng A"})
    new_base = shipped() + "\n- Luật mới của bản cập nhật"
    monkeypatch.setattr(PROC, "default_system_prompt", lambda: new_base)
    view = engine.learning_prompt_view("")
    assert view["rules_status"]["stale"] and view["notice"]["title"] == "Quy tắc gốc vừa được cập nhật"
    assert {r["kind"] for r in view["notice"]["rows"]} - {"same"}
    assert "Luật riêng A" in PROC.compose_system()                     # trong lúc chờ vẫn dùng bản của chủ shop
    out = engine.base_rules_action({"action": "keep_mine"})            # Giữ bản của tôi
    assert out["notice"] is None and "Luật riêng A" in PROC.compose_system()
    newer = new_base + "\n- Thêm nữa"
    monkeypatch.setattr(PROC, "default_system_prompt", lambda: newer)
    assert engine.learning_prompt_view("")["notice"]
    out = engine.base_rules_action({"action": "use_new"})              # Dùng bản mới
    assert out["notice"] is None and not out["rules_status"]["customized"]
    assert "Luật mới của bản cập nhật" in PROC.compose_system() and "Luật riêng A" not in PROC.compose_system()
    engine.base_rules_action({"action": "save", "text": newer + "\n- Gộp tay"})   # Gộp tay = lưu bản đã sửa
    assert not engine.learning_prompt_view("")["rules_status"]["stale"]


def test_override_without_meta_counts_as_stale(rig):
    _engine, tmp = rig
    (tmp / "quy_tac_ban_hang.txt").write_text(shipped(), encoding="utf-8")
    assert pv.status(shipped())["stale"]


def test_protected_lines_are_restored_when_deleted(rig):
    engine, _ = rig
    stripped = shipped().split("ĐỘ DÀI VÀ ĐỊNH DẠNG")[0] + "\nChỉ trả lời ngắn."
    assert pv.missing_protected(stripped)
    out = engine.base_rules_action({"action": "save", "text": stripped})
    assert out["appended"] == 4
    system = PROC.system_prompt()
    assert pv.missing_protected(system) == [] and pv.APPENDED_HEADER in system
    again = engine.base_rules_action({"action": "save", "text": out["sections"][1]["text"]})
    assert again["appended"] == 0                                         # không nối trùng lần hai
    pv.OVERRIDE_PATH.write_text(stripped, encoding="utf-8")               # file sửa tay ngoài app cũng được bảo vệ
    assert pv.missing_protected(PROC.system_prompt()) == []


def test_length_validation_and_bad_actions(rig):
    engine, _ = rig
    for text, code in (("ngắn", "base_rules_too_short"), ("x" * (pv.MAX_CHARS + 1), "base_rules_too_long")):
        with pytest.raises(ValueError, match=code):
            engine.base_rules_action({"action": "save", "text": text})
    with pytest.raises(ValueError, match="base_rules_no_override"):
        engine.base_rules_action({"action": "keep_mine"})
    with pytest.raises(ValueError, match="base_rules_action_unknown"):
        engine.base_rules_action({"action": "boom"})


def test_try_accepts_persona_without_changing_active(rig):
    engine, _ = rig
    male = engine.learning.save_persona({"name": "Nam", "style_guide": "- Xưng anh, gọi khách bà con"})
    assert engine.learning.persona(male["persona_id"])["persona_id"] == male["persona_id"]
    assert "persona_id" in inspect.signature(engine._reply_context).parameters
    assert engine.learning.persona()["persona_id"] == "default"


def test_diff_rows_side_by_side():
    rows = pv.diff_rows("a\nb\nc", "a\nB\nc\nd")
    assert [r["kind"] for r in rows] == ["same", "changed", "same", "added"]
    assert rows[1]["left"] == "b" and rows[1]["right"] == "B"


def test_default_voice_fits_style_limit_so_it_can_be_saved_as_own(rig):
    from learning import MAX_STYLE_CHARS
    engine, _ = rig
    assert MAX_STYLE_CHARS == 2400 and len(DEFAULT_VOICE) < MAX_STYLE_CHARS
    voice = engine.learning_prompt_view("")["sections"][3]
    assert voice["max"] == MAX_STYLE_CHARS
    own = engine.learning.save_persona({"name": "Của tôi", "style_guide": voice["text"]})
    assert own["style_guide"] == voice["text"].strip()
