import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import dose_rules
import label_parser



@pytest.fixture
def db(tmp_path):
    with closing(sqlite3.connect(str(tmp_path / "l.sqlite3"))) as conn:
        conn.row_factory = sqlite3.Row
        dose_rules.ensure_schema(conn)
        yield conn


def test_multi_ratio_all_candidates(db):
    text = "Pha 20ml cho bình 16 lít; pha 50gr vào bình 25 lít\nPhuy 200 lít: 240ml"
    result = label_parser.import_label(db, text, "SP-1")
    rows = dose_rules.list_rules(db, "SP-1")["rules"]
    assert result["needs_review"] and len(rows) == 3
    assert {(r["amount"], r["unit"], r["per_volume"]) for r in rows} == {
        (20, "ml", 16), (50, "g", 25), (240, "ml", 200)}


def test_never_auto_approve(db):
    label_parser.import_label(db, "20ml cho 16 lít", "SP-1")
    assert {r["status"] for r in dose_rules.list_rules(db)["rules"]} == {"candidate"}
    assert dose_rules.approved_directions(db, "SP-1") == []


def test_unit_normalization():
    rules = label_parser.parse_label("50gram cho 20 lit; 1kg pha 1500 lít", "S")
    assert [(r["amount"], r["unit"]) for r in rules] == [(50, "g"), (1, "kg")]


def test_processor_does_not_pick_first_of_several_ratios():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "lieu_processor", Path(__file__).resolve().parents[2] / "plugins/lieu-dung/processor.py")
    proc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(proc)
    rows = proc.dose_rows_from_directions(
        ["Pha 20ml cho bình 16 lít hoặc 50ml cho bình 25 lít"], 16)
    assert rows == []
    assert proc.dose_rows_from_directions(["Pha 20ml cho bình 16 lít"], 16)
    # Cùng nồng độ cho nhiều cỡ bình không phải mâu thuẫn: vẫn tính được.
    assert proc.dose_rows_from_directions(
        ["Pha 20ml cho bình 16 lít hoặc 25ml cho bình 20 lít"], 16)
