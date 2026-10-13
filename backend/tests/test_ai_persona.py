"""Tách LUẬT (lời nhắc gốc trung tính) khỏi GIỌNG (phong cách người trực) + APP_BRIEF cho mọi lời gọi AI."""
import json
import sqlite3
import types

import pytest

import ctx_harness as h
import context_budget as cb
import documents
import learning as learning_module
import processor_panel
import router
import server as server_module
import sales_playbook as sp
from ai_context import APP_BRIEF
from learning import (DEFAULT_PERSONA_ID, DEFAULT_PERSONA_NAME, DEFAULT_VOICE, Learner, LearningStore,
                      MAX_STYLE_CHARS, normalize_persona_proposal, style_text)

BRIEF = APP_BRIEF.rstrip()
PROC = server_module._model_processor
FEMALE_MARKERS = ("cô gái", "“nha”", "“nè”", "“anh ơi”", "“chị ơi”", "Hello anh nha", "em hỗ trợ gì", "để em coi")


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr(server_module, "LIVE_CATALOG_PATH", tmp_path / "live.json")
    monkeypatch.setattr(cb, "FLAGS_PATH", tmp_path / "no_flags.json")
    monkeypatch.setattr(sp, "PLAYBOOK_PATH", tmp_path / "shared" / "sales_playbook.json")
    monkeypatch.setattr(PROC, "AI_NOTES_PATH", tmp_path / "ghi_chu_ai.txt")
    monkeypatch.setattr(PROC, "SYSTEM_PROMPT_OVERRIDE_PATH", tmp_path / "loi_nhac_he_thong.txt")
    engine, captured = h.make_engine(tmp_path)
    yield engine, captured, tmp_path
    engine.close()


def stable_of(call):
    return json.loads(call["messages"][1]["content"].split("\n", 1)[1])


# ------------------------------------------------------------------ B: luật trung tính, giọng ở phong cách
def test_base_prompt_is_gender_and_personality_neutral():
    text = PROC.default_system_prompt()
    for marker in FEMALE_MARKERS:
        assert marker not in text, marker
    assert "phong_cach_nguoi_truc" in text           # trỏ tới nơi giữ giọng
    for rule in ("price.quotable_price", "KHÔNG BAO GIỜ nói shop không có", "da_nhac_ma", "verified_dose_calculations"):
        assert rule in text, rule                    # luật bán hàng vẫn còn đủ


def test_default_persona_is_builtin_and_keeps_todays_voice(tmp_path):
    store = LearningStore(tmp_path / "l.sqlite3")
    default = store.persona()
    assert default["persona_id"] == DEFAULT_PERSONA_ID and default["name"] == DEFAULT_PERSONA_NAME == "Mặc định (nữ, vui, lanh)"
    assert default["builtin_voice"] == DEFAULT_VOICE
    for marker in ("cô gái", "“nha”", "“nè”", "“anh ơi”", "Hello anh nha", "“em”"):
        assert marker in DEFAULT_VOICE, marker
    assert style_text(default) == DEFAULT_VOICE
    store.save_persona({"persona_id": DEFAULT_PERSONA_ID, "style_guide": "- Hay nói: ôi trời"})
    again = style_text(store.persona())
    assert again.startswith(DEFAULT_VOICE) and again.endswith("- Hay nói: ôi trời")      # đã học thêm = bổ sung, không thay


def test_no_active_persona_falls_back_to_default_and_custom_replaces_voice(tmp_path):
    store = LearningStore(tmp_path / "l.sqlite3")
    male = store.save_persona({"name": "Anh Ba", "style_guide": "- Xưng anh, gọi khách bà con", "activate": True})
    assert store.persona()["persona_id"] == male["persona_id"]
    assert style_text(store.persona()) == "- Xưng anh, gọi khách bà con"               # không dính giọng nữ
    store.delete_persona(male["persona_id"])
    assert store.persona()["persona_id"] == DEFAULT_PERSONA_ID
    with store._db() as db:
        db.execute("UPDATE personas SET is_active=0")
    assert store.persona()["persona_id"] == DEFAULT_PERSONA_ID                          # không ai active => mặc định


def test_legacy_default_persona_name_is_migrated_but_custom_name_kept(tmp_path):
    path = tmp_path / "l.sqlite3"
    LearningStore(path)
    db = sqlite3.connect(path)
    db.execute("UPDATE personas SET name='Giọng mặc định', description='Giọng host mặc định trong lời nhắc hệ thống.'")
    db.commit()
    db.close()
    assert LearningStore(path).persona()["name"] == DEFAULT_PERSONA_NAME
    db = sqlite3.connect(path)
    db.execute("UPDATE personas SET name='Cô Lan'")
    db.commit()
    db.close()
    assert LearningStore(path).persona()["name"] == "Cô Lan"


# ------------------------------------------------------------------ A: kiểm tra offline cho TikTok reply
def test_tiktok_reply_messages_start_with_app_brief_and_carry_persona(rig):
    engine, captured, _tmp = rig
    engine.handle(h.payload("TC01 giá bao nhiêu", cid="a1", uid="a1"))
    messages = captured[-1]["messages"]
    assert messages[0]["role"] == "system" and messages[0]["content"].startswith(BRIEF)
    assert PROC.default_system_prompt() in messages[0]["content"]
    assert "[NHIỆM VỤ: Trả lời bình luận LIVE]" in messages[0]["content"]
    stable = stable_of(captured[-1])
    assert stable["phong_cach_nguoi_truc"]["ten"] == DEFAULT_PERSONA_NAME                # mặc định = giọng cũ
    assert "“nha”" in stable["phong_cach_nguoi_truc"]["huong_dan"]


def test_male_persona_reaches_model_without_female_voice(rig):
    engine, captured, _tmp = rig
    engine.learning.save_persona({"name": "Anh Ba miền Tây", "style_guide": "- Xưng anh/tui, gọi khách bà con",
                                  "activate": True})
    engine.handle(h.payload("TC01 giá bao nhiêu", cid="b1", uid="b1"))
    system = captured[-1]["messages"][0]["content"]
    blob = json.dumps(stable_of(captured[-1]), ensure_ascii=False)
    assert system.startswith(BRIEF) and "bà con" in blob and "Hello anh nha" not in blob
    assert all(marker not in system for marker in FEMALE_MARKERS)


def test_ai_notes_sit_after_base_rules_and_before_persona(rig):
    engine, captured, tmp = rig
    (tmp / "ghi_chu_ai.txt").write_text("Shop giao tận vườn trong 20km.", encoding="utf-8")
    engine.handle(h.payload("TC01 giá bao nhiêu", cid="n1", uid="n1"))
    system = captured[-1]["messages"][0]["content"]
    base = PROC.default_system_prompt()
    assert system.index(base) < system.index("Shop giao tận vườn trong 20km.") < system.index("[NHIỆM VỤ:")
    assert "Shop giao tận vườn" not in captured[-1]["messages"][1]["content"]           # persona ở tin sau, không lẫn
    assert not PROC.has_prompt_override()


def test_legacy_override_file_is_never_used_as_prompt(rig):
    engine, captured, tmp = rig
    (tmp / "loi_nhac_he_thong.txt").write_text("PROMPT CŨ GHI ĐÈ", encoding="utf-8")
    engine.handle(h.payload("TC01 giá bao nhiêu", cid="l1", uid="l1"))
    system = captured[-1]["messages"][0]["content"]
    assert "PROMPT CŨ GHI ĐÈ" not in system and PROC.default_system_prompt() in system
    assert PROC.legacy_prompt_override() == "PROMPT CŨ GHI ĐÈ"


def test_every_call_site_system_starts_with_app_brief():
    dose = PROC.compose_system(server_module._DOSE_TASK, "ghi chú liều")
    clarify = PROC.compose_system(server_module._CLARIFY_TASK, server_module._CLARIFY_RULE)
    for system, task in ((dose, "Chọn quy tắc liều"), (clarify, "Làm rõ sản phẩm khách hỏi"),
                         (PROC.compose_system(server_module._TRY_TASK), "Thử AI")):
        assert system.startswith(BRIEF) and f"[NHIỆM VỤ: {task}" in system and PROC.default_system_prompt() in system
    for system in (learning_module.DISTILL_SYSTEM, learning_module.RELEARN_SYSTEM, learning_module.PERSONA_SYSTEM,
                   documents.DISTILL_SYSTEM):
        assert system.startswith("[NHIỆM VỤ:")                                          # _call đặt APP_BRIEF lên trước


def test_reply_prefixes_brief_even_for_plain_system_override():
    captured = []
    responder = PROC.DeepSeekResponder()
    responder.client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=h.FakeCompletions(captured)))
    responder.reply("Khách", "hi", {"platform": "TikTok LIVE"}, 100, system_override="Chỉ là lời nhắc thường.")
    assert captured[0]["messages"][0]["content"].startswith(BRIEF + "\n\nChỉ là lời nhắc thường.")


def test_learner_call_prepends_brief(tmp_path):
    captured = []
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=h.FakeCompletions(captured)),
                                   with_options=lambda **_: client)
    store = LearningStore(tmp_path / "l.sqlite3")
    learner = Learner(store, responder_provider=lambda: types.SimpleNamespace(client=client, model="m"),
                      catalog_provider=lambda: [])
    learner._call(learning_module.DISTILL_SYSTEM, "{}")
    system = captured[0]["messages"][0]["content"]
    assert system.startswith(BRIEF) and learning_module.DISTILL_SYSTEM in system


def test_router_brief_only_for_deepseek_not_local_custom_model():
    plain = router.build_messages("hi", [("p1", "Đạm cá")], [], "")
    with_brief = router.build_messages("hi", [("p1", "Đạm cá")], [], "", brief=True)
    assert plain[0]["content"] == router.SYSTEM_PROMPT
    assert with_brief[0]["content"].startswith(BRIEF) and with_brief[0]["content"].endswith(router.SYSTEM_PROMPT)
    seen = {}

    def call(request):
        seen.update(request)
        msg = types.SimpleNamespace(content='{"acts":["greeting"],"confidence":0.9}')
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)], usage=None)
    router.route(call, "hi", catalog_names=[("p1", "Đạm cá")], custom=False)
    assert seen["messages"][0]["content"].startswith(BRIEF)
    router.route(call, "hi", catalog_names=[("p1", "Đạm cá")], custom=True)
    assert seen["messages"][0]["content"] == router.SYSTEM_PROMPT


# ------------------------------------------------------------------ C: tạo phong cách bằng AI
def _learner(tmp_path, payload):
    captured = []
    comp = h.FakeCompletions(captured)

    def create(**req):
        captured.append(req)
        message = types.SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))
        usage = types.SimpleNamespace(prompt_tokens=1, completion_tokens=1, completion_tokens_details=None)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message, finish_reason="stop")], usage=usage)
    comp.create = create
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=comp), with_options=lambda **_: client)
    store = LearningStore(tmp_path / "l.sqlite3")
    return Learner(store, responder_provider=lambda: types.SimpleNamespace(client=client, model="m"),
                   catalog_provider=lambda: []), captured, store


MALE = {"name": "Anh Ba miền Tây", "description": "Nam, 35 tuổi, miền Tây, vui, nói thẳng",
        "style_guide": {"xung_ho": "Tự xưng anh/tui, gọi khách bà con hoặc anh chị", "tu_hay_dung": "nghe anh nè, ngon lành",
                        "do_dai_nhip": "Ngắn, dứt khoát", "cach_dua": "Đùa nhẹ", "cach_chot": "Bà con đặt liền anh giữ hàng",
                        "dieu_tranh": "Quá ngọt"},
        "samples": ["Khỏe không bà con!", "Pha 20ml một bình nghe bà con.", "Để anh coi giá cho bà con nha."]}


def test_propose_persona_returns_structured_guide_with_samples_and_brief(tmp_path):
    learner, captured, _store = _learner(tmp_path, MALE)
    proposal = learner.propose_persona("Nam 35 tuổi miền Tây, xưng anh, gọi khách bà con, vui, nói thẳng, chốt đơn dứt khoát")
    system = captured[0]["messages"][0]["content"]
    assert system.startswith(BRIEF) and "[NHIỆM VỤ: Tạo phong cách người trực bằng AI]" in system
    assert captured[0]["extra_body"] == {"thinking": {"type": "disabled"}}               # người dùng đang chờ
    user = json.loads(captured[0]["messages"][1]["content"])
    assert "miền Tây" in user["nguoi_truc_tu_ta"] and len(user["binh_luan_mau"]) == 3
    assert proposal["name"] == "Anh Ba miền Tây" and len(proposal["samples"]) == 3
    for label in ("Xưng hô", "Từ hay dùng", "Độ dài/nhịp", "Cách đùa", "Cách chốt", "Điều tránh"):
        assert f"- {label}:" in proposal["style_guide"]
    assert "bà con" in proposal["style_guide"] and len(proposal["style_guide"]) <= MAX_STYLE_CHARS


def test_propose_persona_respects_max_style_chars_and_validates(tmp_path):
    long = {**MALE, "style_guide": {key: "x" * 350 for key in MALE["style_guide"]}}
    guide = normalize_persona_proposal(long)["style_guide"]
    assert 0 < len(guide) <= MAX_STYLE_CHARS and all(line.startswith("- ") for line in guide.splitlines())
    with pytest.raises(ValueError):
        normalize_persona_proposal({"name": "x", "style_guide": ""})
    with pytest.raises(ValueError):
        normalize_persona_proposal([])
    learner, captured, _s = _learner(tmp_path, MALE)
    with pytest.raises(ValueError, match="persona_profile_required"):
        learner.propose_persona("ngắn")
    assert captured == []                                                               # không tốn tiền khi đầu vào rỗng


def test_server_persona_propose_save_and_activate(rig):
    engine, captured, tmp = rig
    learner, _cap, _s = _learner(tmp, MALE)
    engine.learner = learner
    out = engine.learning_post("persona-propose", {"profile": "Nam 35 tuổi miền Tây, xưng anh, gọi khách bà con"})
    proposal = out["proposal"]
    assert out["ok"] and proposal["samples"]
    assert not any(p["name"] == proposal["name"] for p in engine.learning.personas())  # chưa lưu cho tới khi bấm Lưu
    saved = engine.learning_post("persona", {"name": proposal["name"], "description": proposal["description"],
                                             "style_guide": proposal["style_guide"], "activate": True})["persona"]
    assert saved["is_active"] and engine.learning.persona()["persona_id"] == saved["persona_id"]
    engine.handle(h.payload("TC01 giá bao nhiêu", cid="c1", uid="c1"))
    assert "bà con" in captured[-1]["messages"][1]["content"]                           # người trực mới tới tận model


def test_server_persona_propose_without_key_is_clean_error(rig):
    engine, _captured, _tmp = rig
    engine.responder.client = None
    with pytest.raises(ValueError, match="model_not_ready"):
        engine.learning_post("persona-propose", {"profile": "Nam 35 tuổi miền Tây, xưng anh"})


# ------------------------------------------------------------------ D: ghi chú thay cho lời nhắc ghi đè
def _panel(tmp_path, monkeypatch):
    notes, legacy = tmp_path / "ghi_chu_ai.txt", tmp_path / "loi_nhac_he_thong.txt"
    monkeypatch.setattr(PROC, "AI_NOTES_PATH", notes)
    monkeypatch.setattr(PROC, "SYSTEM_PROMPT_OVERRIDE_PATH", legacy)
    monkeypatch.setattr(processor_panel, "AI_NOTES_PATH", notes)
    monkeypatch.setattr(processor_panel, "SYSTEM_PROMPT_OVERRIDE_PATH", legacy)
    panel = processor_panel.ProcessorPanel.__new__(processor_panel.ProcessorPanel)
    panel.engine = types.SimpleNamespace(
        learning=None, responder=types.SimpleNamespace(client=object(), check_balance=lambda: {"ok": True}, model="m"))
    panel.modules, panel.store = {"model-deepseek": PROC}, None
    panel.detail = lambda plugin_id: {"stats": panel._stats(plugin_id)}
    return panel, notes, legacy


def test_panel_notes_save_limit_clear_and_legacy_report(tmp_path, monkeypatch):
    panel, notes, legacy = _panel(tmp_path, monkeypatch)
    legacy.write_text("Prompt cũ dài " * 200, encoding="utf-8")
    stats = panel.detail("model-deepseek")["stats"]
    assert stats["legacy_prompt_chars"] > 1500 and stats["legacy_prompt"].startswith("Prompt cũ") and stats["notes"] == ""
    assert "prompt" not in stats and "prompt_default" not in stats                      # không còn sửa cả lời nhắc
    panel.save_notes("  Shop giao tận vườn  ")
    assert notes.read_text(encoding="utf-8").strip() == "Shop giao tận vườn" and legacy.is_file()   # lưu ghi chú không xoá file cũ
    with pytest.raises(ValueError, match="notes_too_long"):
        panel.save_notes("x" * (PROC.MAX_NOTES_CHARS + 1))
    panel.save_notes("")
    assert not notes.exists()


def test_migrating_legacy_archives_instead_of_deleting(tmp_path, monkeypatch):
    panel, notes, legacy = _panel(tmp_path, monkeypatch)
    legacy.write_text("Dặn: gọi khách là bà con", encoding="utf-8")
    panel.save_notes("Dặn: gọi khách là bà con", archive_legacy=True)
    assert notes.is_file() and not legacy.exists()
    kept = tmp_path / "loi_nhac_he_thong.txt.da-chuyen"
    assert kept.read_text(encoding="utf-8") == "Dặn: gọi khách là bà con"             # không mất dữ liệu của chủ shop
    assert panel.detail("model-deepseek")["stats"]["legacy_prompt_chars"] == 0
    legacy.write_text("lại có file", encoding="utf-8")
    panel.dismiss_legacy_prompt()
    assert not legacy.exists() and kept.read_text(encoding="utf-8") == "lại có file"


def test_backup_policy_includes_notes_file():
    import backup_policy
    assert backup_policy.part_of("ghi_chu_ai.txt") == "cai_dat"
    assert backup_policy._restore_target("ghi_chu_ai.txt") is not None


def test_base_prompt_has_hard_attack_rule_and_no_fake_familiarity():
    text = PROC.default_system_prompt()
    assert "LUẬT CỨNG: KHÁCH CÔNG KÍCH" in text and "cong_kich_lua_dao" in text
    assert text.index("LUẬT CỨNG: KHÁCH CÔNG KÍCH") < text.index("CÁCH TƯ VẤN")     # đứng trước luật bán hàng
    rule = text[text.index("LUẬT CỨNG: KHÁCH CÔNG KÍCH"):text.index("NGUYÊN TẮC LỜI NÓI")]
    assert "KHÔNG nhận lỗi" in rule and "Không cãi" in rule and "TikTok Shop" in rule and "tra liền" in rule
    assert "NGUYÊN TẮC" in rule and "KHÔNG phải câu mẫu" in rule                 # nguyên tắc, không phải khuôn câu
    assert "luot_gan_day" in rule                                                 # lượt sau xử lý theo ngữ cảnh
    assert "Câu đầu PHẢI là lời xin lỗi" not in rule and "em hiểu anh/chị bực\")" not in rule
    assert "KHÔNG giả vờ quen" in text and "KHÔNG tự so sánh giá" in text


def test_base_prompt_address_rule_and_anti_repetition():
    text = PROC.default_system_prompt()
    assert "tự xưng “em”" in text and "KHÔNG mặc định gọi “bà con”" in text and "anh chị" in text
    assert "ĐA DẠNG, KHÔNG LẶP" in text and "cau_da_gui_gan_day" in text and "lich_su_khach" in text
    assert "dưới khoảng\n  35 tuổi" in text


def test_default_voice_and_persona_task_address_rules():
    assert "Không gọi “bà con”" in DEFAULT_VOICE and "“anh chị”" in DEFAULT_VOICE
    assert "NAM DƯỚI KHOẢNG 35 TUỔI" in learning_module.PERSONA_SYSTEM
    assert "không yêu cầu rõ" in learning_module.PERSONA_SYSTEM            # bà con chỉ khi chủ shop đòi
    import json as _json, pathlib
    playbook = _json.loads((pathlib.Path(server_module.__file__).parent.parent / "prompts" / "sales_playbook.default.json")
                           .read_text(encoding="utf-8"))
    entry = next(e for e in playbook["entries"] if e["id"] == "cong_kich_lua_dao")
    assert "không xin lỗi" in entry["huong_xu_ly"] and "tra liền" in entry["huong_xu_ly"]
    assert not entry["huong_xu_ly"].startswith("Xin lỗi")
