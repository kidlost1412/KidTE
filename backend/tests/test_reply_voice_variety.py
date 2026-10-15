"""AI LIVE bớt "một màu": góc tiếp cận theo lượt, chống lặp cách khép, penalty, câu cố định có biến thể."""
import json
import random
import sys
from pathlib import Path
from types import SimpleNamespace

SRC = Path(__file__).resolve().parents[1] / "src"
ROOT = SRC.parents[1]
sys.path.insert(0, str(SRC))

import dose_selection
import reply_angles
import reply_variety
from processor_loader import load_processor


def test_angle_file_loads_and_picks_never_repeat_back_to_back():
    options = reply_angles.load(ROOT / "backend" / "prompts" / "reply_angles.json")
    assert len(options["goc_tiep_can"]) >= 5 and len(options["kieu_mo_khep"]) >= 4
    picker = reply_angles.AnglePicker(options, rng=random.Random(7))
    ids = [picker.pick()["_ids"] for _ in range(60)]
    for group in (0, 1):
        seq = [row[group] for row in ids]
        for i in range(3, len(seq)):
            assert seq[i] not in seq[i - 3:i]
    assert "có thể bỏ" in picker.hint()


def test_angle_file_broken_falls_back_to_defaults(tmp_path):
    bad = tmp_path / "a.json"
    bad.write_text("{hỏng", encoding="utf-8")
    assert reply_angles.load(bad)["goc_tiep_can"]


def test_repeats_recent_catches_same_closing_and_shared_trigrams():
    recents = ["Gốc 289k đó anh, xem tài khoản có mã rồi đặt liền nha"]
    assert reply_variety.repeats_recent("Phun buổi chiều là đẹp nhất, anh đặt liền nha", recents)
    assert reply_variety.repeats_recent("Nay anh xem tài khoản có mã không nhé, ổn lắm", recents)
    assert not reply_variety.repeats_recent("Đạm cá bón gốc 10 ngày một lần là vừa", recents)


def _responder(plugin, captured):
    def create(**kwargs):
        captured.append(json.loads(json.dumps(kwargs, ensure_ascii=False, default=str)))
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content='{"action":"reply","reply":"Phun chiều mát nha anh"}'),
            finish_reason="stop")], usage=None)
    responder = plugin.DeepSeekResponder.__new__(plugin.DeepSeekResponder)
    responder.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return responder


def test_deepseek_penalties_only_in_non_thinking_and_static_prefix_stable():
    plugin = load_processor(ROOT / "plugins" / "model-deepseek" / "processor.py", "model-variety")
    captured = []
    responder = _responder(plugin, captured)
    picker = reply_angles.AnglePicker(rng=random.Random(1))
    responder.disable_thinking = True
    for _ in range(2):
        ctx = {"thong_tin_shop": {"ten": "shop"}, "price_policy": "x", "goi_y_luot_nay": picker.hint()}
        responder.reply("an", "phun lúc nào", ctx, 120)
    first, second = captured[0], captured[1]
    assert 0.3 <= first["presence_penalty"] <= 0.4 and 0.3 <= first["frequency_penalty"] <= 0.4
    assert first["messages"][:2] == second["messages"][:2]           # phần tĩnh giữ cache
    assert "goi_y_luot_nay" in first["messages"][-1]["content"]
    assert first["messages"][-1]["content"] != second["messages"][-1]["content"]
    responder.disable_thinking = False
    responder.reply("an", "phun lúc nào", {"thong_tin_shop": {"ten": "shop"}}, 120)
    assert "presence_penalty" not in captured[-1] and "frequency_penalty" not in captured[-1]


def test_fixed_sentences_have_natural_variants():
    import server
    for pool in (dose_selection.DOSE_HANDOFFS, server.FORGET_REPLIES):
        assert 4 <= len(set(pool)) <= 6
        assert not any("tư vấn viên" in text for text in pool)
    seen = {dose_selection.dose_handoff(random.Random(i)) for i in range(40)}
    assert len(seen) >= 3 and seen <= set(dose_selection.DOSE_HANDOFFS)


def test_voice_and_voucher_defaults_have_no_copyable_sample():
    import learning
    assert "289k" not in learning.DEFAULT_VOICE
    plugin = load_processor(ROOT / "plugins" / "chinh-sach-gia" / "processor.py", "price-variety")
    assert "289k" not in plugin.DEFAULT_VOUCHER_PITCH and "{gia}" in plugin.DEFAULT_VOUCHER_PITCH


def test_ngram_check_ignores_prices_product_names_and_fillers():
    recents = ["Đạm Cá Amino giá gốc 289k, phun chiều mát là đẹp", "Dạ anh ơi bên em có liền nha anh"]
    assert not reply_variety.repeats_recent("Bón Đạm Cá Amino giá gốc 289k cho sầu riêng ổn lắm", recents[:1])
    assert not reply_variety.repeats_recent("Thử hỏi bên em có liền nha anh, mua sớm", recents[1:])
