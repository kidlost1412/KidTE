"""Package A: cắt token prompt — cờ, chiếu hồ sơ theo ý định, danh mục gọn, làn xã giao."""
import json
from pathlib import Path

import pytest

import context_budget as cb
import ctx_harness as h
import server as server_module
import smalltalk

FIXTURE = Path(__file__).parent / "fixtures" / "context_budget_old_messages.json"
JUNK = ("website_product_ids", "featured_image_url", "featured_image_alt", "source_modified", "source_kind",
        "nguon_chinh_thuc", "legacy_profile_id", "website_categories", "catalog_status", "ten_tren_website")


# ------------------------------------------------------------------ cờ
def set_flags(monkeypatch, tmp_path, **values):
    path = tmp_path / "context_budget.json"
    path.write_text(json.dumps(values), encoding="utf-8")
    monkeypatch.setattr(cb, "FLAGS_PATH", path)


def test_flags_default_all_true_when_file_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(cb, "FLAGS_PATH", tmp_path / "nope.json")
    # Gói C thêm 3 cờ mới (mặc định bật): persona_all_lanes, customer_card, playbook.
    assert cb.flags() == {"smalltalk_lane": True, "intent_fields": True, "compact_catalog": True,
                          "persona_all_lanes": True, "customer_card": True, "playbook": True}


def test_flags_partial_corrupt_and_non_dict_files(monkeypatch, tmp_path):
    set_flags(monkeypatch, tmp_path, intent_fields=False)
    assert cb.flags() == {"smalltalk_lane": True, "intent_fields": False, "compact_catalog": True,
                          "persona_all_lanes": True, "customer_card": True, "playbook": True}
    (tmp_path / "context_budget.json").write_text("{not json", encoding="utf-8")
    assert all(cb.flags().values())
    (tmp_path / "context_budget.json").write_text("[1,2]", encoding="utf-8")
    assert all(cb.flags().values())


def test_flags_reload_when_file_changes(monkeypatch, tmp_path):
    off = dict(smalltalk_lane=False, intent_fields=False, compact_catalog=False,
               persona_all_lanes=False, customer_card=False, playbook=False)
    set_flags(monkeypatch, tmp_path, **off)
    assert not any(cb.flags().values())
    set_flags(monkeypatch, tmp_path, **{k: True for k in off}, extra=1)
    assert all(cb.flags().values())


# ------------------------------------------------------------------ chiếu hồ sơ
PROFILE = {**{k: "x" for k in JUNK}, "ten_san_pham": "Kích Rễ TC01", "san_pham_id": "WEB-1",
           "cong_dung": ["a", "b"], "thanh_phan_dang_ky": ["N 10%"], "huong_dan_su_dung": ["pha 10ml/16 lít"],
           "luu_y": "lưu ý", "doi_tuong_cay_trong": ["cây ăn trái"], "mo_ta_ngan": "kích rễ mạnh",
           "gia_goc_duyet": "120.000₫", "live_promotion": "Mua 10 tặng 1", "quy_cach_dong_goi": "Chai 100ml",
           "canh_bao_quy_cach": "hồ sơ 250ml", "combo_components": [{"ten": "x"}], "is_combo": True,
           "dose_rules": [{"fact_id": "1"}], "uu_diem_website": ["tốt"]}


def test_project_profile_drops_junk_for_every_intent():
    for intents in ({}, {"price": True}, {"dose": True}, {"technical": True}, {"promotion": True, "dose": True}):
        out = cb.project_profile(PROFILE, intents)
        assert not set(JUNK) & set(out), intents
        assert out["ten_san_pham"] == "Kích Rễ TC01" and out["quy_cach_dong_goi"] == "Chai 100ml"
        assert out["gia_goc_duyet"] == "120.000₫" and out["live_promotion"] == "Mua 10 tặng 1"


def test_project_profile_price_intent_is_price_block_only():
    out = cb.project_profile(PROFILE, {"price": True})
    assert "huong_dan_su_dung" not in out and "cong_dung" not in out and "thanh_phan_dang_ky" not in out
    assert out["tom_tat_cong_dung"] == "kích rễ mạnh"


def test_project_profile_dose_intent_keeps_directions_and_notes():
    out = cb.project_profile(PROFILE, {"dose": True})
    assert out["huong_dan_su_dung"] == ["pha 10ml/16 lít"] and out["luu_y"] == "lưu ý"
    assert out["combo_components"] and "cong_dung" not in out and "thanh_phan_dang_ky" not in out


def test_project_profile_unclear_intent_defaults_to_usage_group():
    for intents in ({}, None, {"technical": True}, {"mixing": True}, {"after_planting": True}):
        out = cb.project_profile(PROFILE, intents)
        for key in ("cong_dung", "thanh_phan_dang_ky", "huong_dan_su_dung", "luu_y", "doi_tuong_cay_trong"):
            assert key in out, (intents, key)


def test_project_profile_keeps_inherited_directions_from_approved_profile():
    profile = {"ten_san_pham": "Đạm cá", "huong_dan_su_dung": [], "ho_so_da_duyet": {
        "package": "Can 5 lít", "directions": ["30–50ml/20 lít"], "uses": ["u"], "promotion": "Mua 1 tặng 1"}}
    out = cb.project_profile(profile, {"dose": True})
    assert out["ho_so_da_duyet"]["directions"] == ["30–50ml/20 lít"]
    assert out["ho_so_da_duyet"]["package"] == "Can 5 lít" and out["ho_so_da_duyet"]["promotion"] == "Mua 1 tặng 1"
    price = cb.project_profile(profile, {"price": True})
    assert price["ho_so_da_duyet"] == {"package": "Can 5 lít", "promotion": "Mua 1 tặng 1"}


def test_project_profile_fails_open_on_bad_input():
    assert cb.project_profile(None, {}) is None
    assert cb.project_profile({}, {}) == {}
    assert cb.project_profile({"ten_san_pham": "x"}, "not-a-dict")  # lỗi => trả nguyên hoặc chiếu mặc định, không ném


def test_trim_recommendations_limits_excludes_current_and_slims_live_product():
    rows = [{"live_product": {"product_id": f"p{i}", "title": f"Sản phẩm {i} chuyên trị", "original_price": "1₫",
                              "promotion": '{"activity_id":"1"}', "stock_num": 5, "sale_price": "1"},
             "approved_product_knowledge": PROFILE, "matched_terms": ["a", "b"]} for i in range(1, 5)]
    out = cb.trim_recommendations(rows, "p1", {}, lambda item: "ngắn " + item["product_id"])
    assert [r["live_product"]["product_id"] for r in out] == ["p2", "p3"]
    assert set(out[0]["live_product"]) == {"product_id", "short_name", "original_price", "promotion"}
    assert out[0]["live_product"]["promotion"] == ""  # blob JSON của sàn (giá sale) bị bỏ
    assert not set(JUNK) & set(out[0]["approved_product_knowledge"])
    assert "huong_dan_su_dung" not in out[0]["approved_product_knowledge"]  # gợi ý phụ chỉ rút gọn
    full = cb.trim_recommendations(rows, "p1", {"recommendation": True}, lambda item: "x")
    assert "huong_dan_su_dung" in full[0]["approved_product_knowledge"]


def test_compact_catalog_is_sorted_short_and_deterministic():
    products = [{"product_id": "9", "title": "Z " * 80, "original_price": "1₫", "promotion": "Mua 2 tặng 1"},
                {"product_id": "10", "title": "Combo Nano Tonic + 4 gói", "original_price": "", "promotion": '{"a":1}'}]
    rows = cb.compact_catalog(products, lambda item: item["title"][:70])
    assert [r["product_id"] for r in rows] == ["10", "9"]
    assert all(len(r["name"]) <= 60 for r in rows)
    assert set(rows[0]) == {"product_id", "name", "original_price", "promotion"}
    assert rows[0]["promotion"] == "" and rows[1]["promotion"] == "Mua 2 tặng 1"
    assert rows == cb.compact_catalog(list(reversed(products)), lambda item: item["title"][:70])


def test_cap_name_removes_dangling_plus():
    assert cb.cap_name("Combo Phân thuốc Nano Tonic + 4") == "Combo Phân thuốc Nano Tonic"
    assert cb.cap_name("Đạm Cá 5L") == "Đạm Cá 5L"


def test_short_memory_is_short_and_drops_boilerplate():
    text = "NGỮ CẢNH RIÊNG CỦA ĐÚNG KHÁCH NÀY\n- Khách: Hoa\n- Chỉ dùng để hiểu\n" + "\n".join(
        f"  + Khách: câu {i} " + "x" * 200 for i in range(10))
    out = cb.short_memory(text)
    assert "Khách: Hoa" in out and "NGỮ CẢNH" not in out and "Chỉ dùng" not in out and len(out) <= 600
    assert cb.short_memory("") == ""


# ------------------------------------------------------------------ engine end-to-end
@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr(server_module, "LIVE_CATALOG_PATH", tmp_path / "live.json")
    monkeypatch.setattr(cb, "FLAGS_PATH", tmp_path / "no_flags.json")
    engine, captured = h.make_engine(tmp_path)
    yield engine, captured, tmp_path, monkeypatch
    engine.close()


def total_chars(calls):
    return sum(len(m["content"]) for call in calls for m in call["messages"])


def test_flags_off_reproduces_old_messages_exactly(rig):
    engine, captured, tmp_path, monkeypatch = rig
    # Gói C: tắt cả ba cờ mới để quay về đúng hành vi cũ.
    set_flags(monkeypatch, tmp_path, smalltalk_lane=False, intent_fields=False, compact_catalog=False,
              persona_all_lanes=False, customer_card=False, playbook=False)
    got = h.run_fixed(engine, captured)
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    # Lời nhắc hệ thống (tệp tĩnh, không theo cờ) đổi nên bỏ băm của nó. Giọng host nay nằm ở phong cách
    # người trực (người trực mặc định luôn có giọng dựng sẵn) => nền cố định có thêm phong_cach_nguoi_truc;
    # fixture đã được tạo lại cho đúng điều đó, mọi thứ còn lại y nguyên bản cũ.
    for rows in (got, expected):
        for row in rows:
            for call in row["calls"]:
                call.pop("system_sha", None)
    assert got == expected


def test_flags_on_shrinks_prompt_and_keeps_hard_facts(rig):
    engine, captured, tmp_path, monkeypatch = rig
    set_flags(monkeypatch, tmp_path, smalltalk_lane=False, intent_fields=False, compact_catalog=False)
    h.run_fixed(engine, captured)
    old_total = total_chars([{"messages": c["messages"]} for c in captured])
    captured.clear()
    monkeypatch.setattr(cb, "FLAGS_PATH", tmp_path / "no_flags.json")  # mặc định: bật hết
    result = h.run_fixed(engine, captured, [q + " " for q in h.QUESTIONS])  # câu hỏi y hệt (khác khoảng trắng)
    new_total = total_chars([{"messages": c["messages"]} for c in captured])
    assert new_total < old_total * 0.8
    blob = json.dumps(result, ensure_ascii=False)
    for junk in JUNK:
        assert junk not in blob, junk


def messages_for(engine, captured, text, cid="m1", uid="m1"):
    before = len(captured)
    out = engine.handle(h.payload(text, cid=cid, uid=uid))
    return out, captured[before:]


def test_hard_facts_survive_per_intent(rig):
    engine, captured, _tmp, _mp = rig
    out, calls = messages_for(engine, captured, "TC01 giá bao nhiêu")
    user = calls[0]["messages"][-1]["content"]
    assert '"quotable_price":"120.000₫"' in user and out["should_send"]
    out, calls = messages_for(engine, captured, "pha 5 lít nước bao nhiêu ml", "m2", "m2")
    user = calls[0]["messages"][-1]["content"]
    assert "verified_dose_calculations" in user and "huong_dan_su_dung" in user
    out, calls = messages_for(engine, captured, "mua 10 chai có tặng không", "m3", "m3")
    assert "Mua 10 tặng 1" in calls[0]["messages"][-1]["content"]


def test_stable_block_is_byte_identical_across_turns_and_compact(rig):
    engine, captured, _tmp, _mp = rig
    stables = []
    for index, text in enumerate(["TC01 giá bao nhiêu", "kích rễ pha như nào", "kích rễ xịt lên lá được không"]):
        _out, calls = messages_for(engine, captured, text, f"s{index}", f"s{index}")
        stables.append(calls[0]["messages"][1]["content"])
    assert stables[0] == stables[1] == stables[2]
    stable = json.loads(stables[0].split("\n", 1)[1])
    assert set(stable["all_live_products"][0]) == {"product_id", "name", "original_price", "promotion"}
    ids = [row["product_id"] for row in stable["all_live_products"]]
    assert ids == sorted(ids)
    assert all(len(row["name"]) <= 60 for row in stable["all_live_products"])
    assert "kien_thuc_policy" not in stable and "approved_example_policy" not in stable
    assert "catalog_scope_warning" in stable  # giữ: an toàn "không nói shop không có hàng"
    assert "234.090" not in stables[0]  # blob JSON giá sale của sàn không lọt vào prompt


def test_legacy_override_file_no_longer_changes_policy_dedupe(rig):
    """Lời nhắc ghi đè kiểu cũ không còn được dùng => chuỗi chính sách trùng vẫn bị bỏ như bản gốc."""
    engine, captured, tmp_path, monkeypatch = rig
    override = tmp_path / "override.txt"
    override.write_text("Lời nhắc riêng của chủ shop", encoding="utf-8")
    monkeypatch.setattr(server_module._model_processor, "SYSTEM_PROMPT_OVERRIDE_PATH", override)
    _out, calls = messages_for(engine, captured, "TC01 giá bao nhiêu", "o1", "o1")
    stable = calls[0]["messages"][1]["content"]
    assert "kien_thuc_policy" not in stable and "approved_example_policy" not in stable
    assert "Lời nhắc riêng của chủ shop" not in calls[0]["messages"][0]["content"]


def test_product_ref_not_sent_but_recorded_in_metrics(rig):
    engine, captured, _tmp, _mp = rig
    out, calls = messages_for(engine, captured, "TC01 giá bao nhiêu", "r1", "r1")
    assert "product_ref" not in json.dumps(calls[0]["messages"], ensure_ascii=False)
    assert "candidates" in out["metrics"]["product_ref"]
    assert out["metrics"]["context_part_chars"]["system_prompt"] > 1000
    assert any(k.startswith("stable.") for k in out["metrics"]["context_part_chars"])


def test_smalltalk_lane_sends_minimal_context(rig):
    # Gói C: mặc định làn xã giao giữ cả nền cố định (persona_all_lanes); bản tối giản cũ chỉ còn khi cờ TẮT.
    engine, captured, tmp_path, monkeypatch = rig
    set_flags(monkeypatch, tmp_path, persona_all_lanes=False)
    out, calls = messages_for(engine, captured, "hello", "t1", "t1")
    assert out["should_send"] and out["metrics"]["context_lane"] == "smalltalk"
    body = json.dumps(calls[0]["messages"][1:], ensure_ascii=False)
    for absent in ("all_live_products", "approved_product_knowledge", "relevant_approved_products",
                   "quy_tac_da_hoc", "operator_approved_reply_examples", "kien_thuc_da_hoc", "current_product"):
        assert absent not in body, absent
    assert len(body) < 3300 and "thong_tin_shop" in body  # ngoài lời nhắc hệ thống


@pytest.mark.parametrize("text", ["ok lấy 2 chai", "giá bao nhiêu ạ?", "TC01 ok", "kích rễ xịt lên lá được không",
                                  "pha như nào", "có bán chịu không em", "ok 2 chai"])
def test_purchase_price_and_product_comments_never_use_smalltalk_lane(rig, text):
    engine, captured, _tmp, _mp = rig
    out, calls = messages_for(engine, captured, text, "n1", "n1")
    assert (out.get("metrics") or {}).get("context_lane") != "smalltalk"
    if calls:
        assert "all_live_products" in json.dumps(calls[0]["messages"], ensure_ascii=False)


def test_ack_after_open_question_does_not_use_smalltalk_lane(rig):
    engine, captured, _tmp, _mp = rig
    messages_for(engine, captured, "TC01 giá bao nhiêu", "q1", "same")  # chưa gửi => còn câu hỏi treo
    out, _calls = messages_for(engine, captured, "ok", "q2", "same")
    # Có câu hỏi giá treo: "ok" không được rút xuống ngữ cảnh tối thiểu (còn price/catalog).
    assert out["metrics"].get("context_lane") != "smalltalk"
    assert out["metrics"]["context_part_chars"]


def test_smalltalk_disabled_flag_uses_full_context(rig):
    engine, captured, tmp_path, monkeypatch = rig
    set_flags(monkeypatch, tmp_path, smalltalk_lane=False)
    out, calls = messages_for(engine, captured, "hello", "d1", "d1")
    assert (out["metrics"].get("context_lane") or "") == ""
    assert "all_live_products" in json.dumps(calls[0]["messages"], ensure_ascii=False)


# ------------------------------------------------------------------ fail open
def boom(*_a, **_k):
    raise RuntimeError("boom")


@pytest.mark.parametrize("target", ["project_profile", "trim_recommendations", "compact_catalog",
                                    "drop_duplicate_policies"])
def test_context_budget_failures_fall_back_to_old_behaviour(rig, target):
    engine, captured, _tmp, monkeypatch = rig
    monkeypatch.setattr(cb, target, boom)
    out, calls = messages_for(engine, captured, "kích rễ xịt lên lá được không", "f1", "f1")
    assert out["should_send"] is True and calls
    if target in {"project_profile", "trim_recommendations"}:
        assert "featured_image_url" in json.dumps(calls[0]["messages"], ensure_ascii=False)  # hồ sơ nguyên bản


def test_smalltalk_classifier_failure_falls_back(rig):
    engine, captured, _tmp, monkeypatch = rig
    monkeypatch.setattr(smalltalk, "is_small_talk", boom)
    out, calls = messages_for(engine, captured, "hello", "f2", "f2")
    assert out["should_send"] is True
    assert "all_live_products" in json.dumps(calls[0]["messages"], ensure_ascii=False)


def test_smalltalk_context_failure_falls_back(rig):
    engine, captured, _tmp, monkeypatch = rig
    monkeypatch.setattr(type(engine), "_smalltalk_context", boom)
    out, calls = messages_for(engine, captured, "hello", "f3", "f3")
    assert out["should_send"] is True
    assert "all_live_products" in json.dumps(calls[0]["messages"], ensure_ascii=False)


def test_bad_flag_file_never_blocks_reply(rig):
    engine, captured, tmp_path, monkeypatch = rig
    (tmp_path / "bad.json").write_text("\x00\x01 not json", encoding="utf-8")
    monkeypatch.setattr(cb, "FLAGS_PATH", tmp_path / "bad.json")
    out, _calls = messages_for(engine, captured, "TC01 giá bao nhiêu", "f4", "f4")
    assert out["should_send"] is True
