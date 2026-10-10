import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import eval_conversations as ev  # noqa: E402


@pytest.fixture(scope="module")
def tmp_root():
    with tempfile.TemporaryDirectory(prefix="kid-te-eval-test-", ignore_cleanup_errors=True) as tmp:
        yield Path(tmp)


def test_fake_model_end_to_end_passes_and_writes_reports(tmp_root):
    report = ev.evaluate(ev.FakeModel(), tmp=tmp_root)
    assert report["turns"] >= 40 and report["aborted"] == ""
    assert report["severe_count"] == 0, report["severe"]
    assert report["pass_rate"] >= 0.9, ev.render_md(report)
    assert report["overall_pass"] is True
    js, md = ev.write_reports(report, tmp_root / "out")
    assert js.is_file() and md.is_file() and "PASS" in md.read_text(encoding="utf-8")


def test_fake_severe_errors_are_detected(tmp_root):
    fixtures = [c for c in ev.load_fixtures() if c["id"] in ("01-tiktok", "02-shopee", "05-tiktok", "06-shopee")]
    for fault in ("wrong_price", "invent_product"):
        report = ev.evaluate(ev.FakeModel(fault=fault), fixtures=fixtures, tmp=tmp_root)
        assert report["severe_count"] >= 1, fault
        assert report["overall_pass"] is False


def test_score_turn_flags_wrong_dose_and_length_directly():
    cat = ev.load_catalog()
    turn = {"expect": {"intent": "dose"}}
    obs = {"bodies": ["Dạ pha 99ml cho bình nha"], "tag": "@a",
           "dose_rows": [{"calculated_ml_min": 10, "calculated_ml_max": 10}], "allowed_prices": set()}
    assert any(s.startswith("dose_not_verified") for s in ev.score_turn(turn, obs, cat)["severe"])
    ok = {**obs, "bodies": ["Dạ pha 10ml cho bình nha"]}
    assert ev.score_turn(turn, ok, cat)["passed"]
    long = {**ok, "bodies": ["x" * 200]}
    assert "length>150" in ev.score_turn(turn, long, cat)["problems"]
    price = {"bodies": ["Dạ giá 50.000₫ nha"], "tag": "@a", "allowed_prices": {120000}}
    assert ev.score_turn({"expect": {"intent": "price"}}, price, cat)["severe"] == ["wrong_price:50000"]


def test_call_budget_aborts():
    report = ev.evaluate(ev.FakeModel(), fixtures=ev.load_fixtures()[:2], max_calls=1)
    assert report["aborted"] and report["overall_pass"] is False


def test_real_requires_yes():
    assert ev.main(["--real"]) == 2
