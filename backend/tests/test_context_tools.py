"""Công cụ đo/so sánh Package A: chạy với client giả, từ chối dữ liệu live."""
import json
import sys
from pathlib import Path

import pytest

import context_budget as cb
import ctx_harness as h
import server as server_module
import smalltalk

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
import context_ab  # noqa: E402
import sim_common as sc  # noqa: E402
import token_budget_sim  # noqa: E402


def test_resolve_root_refuses_live_data(tmp_path):
    with pytest.raises(SystemExit) as caught:
        sc.resolve_root(r"D:\KidTE\DuLieu\data")
    assert "TỪ CHỐI" in str(caught.value) or "live" in str(caught.value).lower() or caught.value.code
    with pytest.raises(SystemExit):
        sc.resolve_root(r"D:\KidTE\DuLieu")


def test_resolve_root_requires_a_data_copy(tmp_path):
    with pytest.raises(SystemExit):
        sc.resolve_root(str(tmp_path))
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "live_products_current.json").write_text("{}", encoding="utf-8")
    assert sc.resolve_root(str(tmp_path)) == tmp_path.resolve()
    assert sc.resolve_root(str(tmp_path / "data")) == tmp_path.resolve()


def test_message_parts_and_prefix_helpers():
    system = {"role": "system", "content": "S" * 100}
    stable = {"role": "system", "content": "NỀN\n" + json.dumps({"a": [1, 2, 3]})}
    turn = {"role": "user", "content": "LƯỢT\n" + json.dumps({"b": "x"}) + "\nKHÁCH: hi"}
    parts = sc.message_parts([system, stable, turn])
    assert parts["system_prompt"] == 100 and parts["stable.a"] > 0 and parts["turn.b"] > 0
    shared, total = sc.common_prefix_chars([system, stable, turn], [system, stable, {**turn, "content": "khác"}])
    assert shared > 100 and total > shared


def test_context_ab_replays_with_fake_client_and_writes_report(tmp_path, monkeypatch):
    monkeypatch.setattr(server_module, "LIVE_CATALOG_PATH", tmp_path / "live.json")
    engine, captured = h.make_engine(tmp_path)
    try:
        (tmp_path / "on").mkdir()
        (tmp_path / "off").mkdir()
        flags = {True: sc.write_flags(True, tmp_path / "on"), False: sc.write_flags(False, tmp_path / "off")}
        monkeypatch.setattr(cb, "FLAGS_PATH", flags[True])

        def set_flags(on):
            monkeypatch.setattr(cb, "FLAGS_PATH", flags[on])

        rows = context_ab.replay(engine, h.live_snapshot(), ["hello", "TC01 giá bao nhiêu"], set_flags)
    finally:
        engine.close()
    assert [r["comment"] for r in rows] == ["hello", "TC01 giá bao nhiêu"]
    assert rows[0]["on"]["lane"] == "smalltalk" and rows[0]["off"]["lane"] == "full"
    assert rows[0]["off"]["usage"] == {"prompt_tokens": 100, "prompt_cache_hit_tokens": 40,
                                       "prompt_cache_miss_tokens": 60, "completion_tokens": 10}
    assert rows[1]["on"]["sent"] and rows[1]["off"]["reply"]
    totals = context_ab.write_report(rows, tmp_path / "ab")
    assert totals["off"]["prompt_tokens"] == 200
    report = json.loads((tmp_path / "ab.json").read_text(encoding="utf-8"))
    assert len(report["rows"]) == 2
    assert "| 1 | hello |" in (tmp_path / "ab.md").read_text(encoding="utf-8")
    # Mỗi phía có số đo phần prompt (context_part_chars) để so sánh.
    assert rows[1]["on"]["part_chars"] and rows[1]["off"]["part_chars"]


def test_token_budget_sim_default_questions_cover_required_cases():
    for needed in ("hello", "ok", "giá bao nhiêu ạ?", "ok lấy 2 chai", "[laugh]", "có bán chịu không em"):
        assert needed in token_budget_sim.QUESTIONS


def test_previous_turn_open():
    assert smalltalk.previous_turn_open([]) is False
    assert smalltalk.previous_turn_open([{"comment": "giá bao nhiêu", "delivery_status": "failed"}]) is True
    assert smalltalk.previous_turn_open([{"comment": "giá bao nhiêu", "delivery_status": "sent"}]) is False
    assert smalltalk.previous_turn_open([{"comment": "hello", "delivery_status": "failed"}]) is False
    assert smalltalk.previous_turn_open(["junk"]) is True
