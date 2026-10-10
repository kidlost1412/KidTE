import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import dose_rules

from processor_loader import load_processor

# Tên module riêng: không chiếm tên "processor" của plugin khác trong cùng phiên pytest.
processor = load_processor(Path(__file__).resolve().parents[2] / "plugins" / "lieu-dung" / "processor.py",
                           "lieu-dung-combo-test")

FIX = sorted((Path(__file__).parent / "fixtures" / "combos").glob("*.json"))


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _approved(db, fx):
    result = dose_rules.import_rules(db, [fx])
    dose_rules.review_rule(db, result["rule_ids"][0], "approve")
    return dose_rules.profile_with_approved_rules(db, {}, fx["sku"])


def _db(tmp_path):
    conn = sqlite3.connect(tmp_path / "d.db")
    conn.row_factory = sqlite3.Row
    dose_rules.ensure_schema(conn)
    return conn


def test_combo_fixture_count():
    assert len(FIX) >= 5
    units = {c["unit"] for p in FIX for c in _load(p)["combo_components"]}
    methods = {c["method"] for p in FIX for c in _load(p)["combo_components"]}
    assert {"ml", "g", "kg"} <= units and {"phun", "tưới", "ngâm"} <= methods


@pytest.mark.parametrize("path", FIX, ids=lambda p: p.stem)
def test_each_component_unit_kept(tmp_path, path):
    fx = _load(path)
    with closing(_db(tmp_path)) as db:
        profile = _approved(db, fx)
        stored = dose_rules.list_rules(db, fx["sku"])["rules"][0]["combo_components"]
    assert [(c["unit"], c["method"]) for c in stored] == \
        [(c["unit"], c["method"]) for c in fx["combo_components"]]
    rows = processor.verified_dose_calculations("bình 16 lít pha sao", profile)
    assert len(rows) == len(fx["combo_components"])
    for r in rows:
        comp = next(c for c in fx["combo_components"] if c["name"] == r["combo_component"])
        # processor chuẩn hoá kg -> g (có chủ đích) nhưng vẫn theo từng thành phần
        want_unit, want = ("g", comp["amount"] * 1000) if comp["unit"] == "kg"             else (comp["unit"], comp["amount"])
        assert r["don_vi"] == want_unit
        assert r["calculated_ml"] == pytest.approx(want)
        assert r["combo_method"] == comp["method"]


@pytest.mark.parametrize("path", FIX, ids=lambda p: p.stem)
def test_no_unit_merge_across_components(tmp_path, path):
    fx = _load(path)
    with closing(_db(tmp_path)) as db:
        profile = _approved(db, fx)
    rows = processor.verified_dose_calculations("bình 16 lít pha sao", profile)
    names = [r["combo_component"] for r in rows]
    assert len(names) == len(set(names))  # mỗi thành phần đúng một dòng riêng
    assert len(rows) == len(fx["combo_components"])
    total = sum(c["amount"] for c in fx["combo_components"])
    assert all(r["calculated_ml"] != total or len(rows) == 1 for r in rows)


def test_migration_adds_column_to_old_db(tmp_path):
    conn = sqlite3.connect(tmp_path / "old.db")
    conn.execute("CREATE TABLE dose_rules (rule_id TEXT PRIMARY KEY, sku TEXT NOT NULL, "
                 "method TEXT, crop TEXT, stage TEXT, amount REAL, unit TEXT, per_volume REAL, "
                 "per_unit TEXT, source_doc TEXT, source_page TEXT, status TEXT, "
                 "created_at TEXT, reviewed_at TEXT, reviewer TEXT)")
    dose_rules.ensure_schema(conn)
    dose_rules.ensure_schema(conn)
    assert "combo_components" in {r[1] for r in conn.execute("PRAGMA table_info(dose_rules)")}
    conn.close()


def test_candidate_combo_not_used(tmp_path):
    fx = _load(FIX[0])
    with closing(_db(tmp_path)) as db:
        dose_rules.import_rules(db, [fx])
        assert dose_rules.profile_with_approved_rules(db, {}, fx["sku"]) == {}
