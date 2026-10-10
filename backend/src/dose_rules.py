"""Bảng dose_rules: liều có cấu trúc, nhập ở trạng thái candidate, chỉ approved mới được tính."""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone

UNITS = {"ml", "g", "kg"}
LITRE_UNITS = {"", "l", "lit", "lít", "litre", "liter"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS dose_rules (
    rule_id TEXT PRIMARY KEY, sku TEXT NOT NULL, method TEXT NOT NULL DEFAULT '',
    crop TEXT NOT NULL DEFAULT '', stage TEXT NOT NULL DEFAULT '',
    amount REAL NOT NULL, unit TEXT NOT NULL, per_volume REAL NOT NULL,
    per_unit TEXT NOT NULL DEFAULT 'lít', source_doc TEXT NOT NULL DEFAULT '',
    source_page TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'candidate'
        CHECK (status IN ('candidate','approved','rejected')),
    created_at TEXT NOT NULL, reviewed_at TEXT, reviewer TEXT
);
CREATE INDEX IF NOT EXISTS idx_dose_rules_sku ON dose_rules(sku, status);
"""


class DoseRuleConflict(ValueError):
    """Quy tắc đã được duyệt hoặc loại trước đó."""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_schema(db):
    db.executescript(SCHEMA)
    cols = {row[1] for row in db.execute("PRAGMA table_info(dose_rules)")}
    if "combo_components" not in cols:  # migration an toàn cho DB cũ
        db.execute("ALTER TABLE dose_rules ADD COLUMN combo_components TEXT")


def _text(value, limit=200):
    return " ".join(str(value or "").split())[:limit]


def _number(value, field):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"dose_rule_{field}_invalid") from None
    if not number > 0 or number > 1_000_000:
        raise ValueError(f"dose_rule_{field}_invalid")
    return number


def _component(item):
    """Một thành phần combo giữ nguyên phương pháp và đơn vị của chính nó."""
    if not isinstance(item, dict):
        raise ValueError("dose_rule_component_invalid")
    unit = _text(item.get("unit"), 10).lower()
    if unit not in UNITS:
        raise ValueError("dose_rule_unit_invalid")
    name = _text(item.get("name") or item.get("ten_san_pham"), 120)
    if not name:
        raise ValueError("dose_rule_component_invalid")
    return {"name": name, "method": _text(item.get("method")),
            "amount": _number(item.get("amount"), "amount"), "unit": unit,
            "per_volume": _number(item.get("per_volume"), "per_volume"),
            "per_unit": _text(item.get("per_unit") or "lít", 20).lower()}


def _normalise(rule):
    if not isinstance(rule, dict):
        raise ValueError("dose_rule_invalid")
    comps = rule.get("combo_components")
    if comps:
        if not isinstance(comps, list) or len(comps) < 2 or len(comps) > 10:
            raise ValueError("dose_rule_components_invalid")
        comps = [_component(c) for c in comps]
        first = comps[0]  # chỉ để đại diện; không cộng gộp các thành phần
        rule = dict(rule, amount=first["amount"], unit=first["unit"],
                    per_volume=first["per_volume"], per_unit=first["per_unit"])
    sku = _text(rule.get("sku"), 80)
    if not sku:
        raise ValueError("dose_rule_sku_invalid")
    unit = _text(rule.get("unit"), 10).lower()
    if unit not in UNITS:
        raise ValueError("dose_rule_unit_invalid")
    return {
        "sku": sku, "method": _text(rule.get("method")), "crop": _text(rule.get("crop")),
        "stage": _text(rule.get("stage")), "amount": _number(rule.get("amount"), "amount"),
        "unit": unit, "per_volume": _number(rule.get("per_volume"), "per_volume"),
        "per_unit": _text(rule.get("per_unit") or "lít", 20).lower(),
        "source_doc": _text(rule.get("source_doc"), 300),
        "source_page": _text(rule.get("source_page"), 40),
        **({"combo_components": comps} if comps else {}),
    }


def _rule_id(rule):
    key = json.dumps(rule, sort_keys=True, ensure_ascii=False)
    return "DR-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def import_rules(db, rules):
    """Mọi quy tắc nhập vào đều là candidate; client không chọn được status."""
    if not isinstance(rules, list) or not rules or len(rules) > 500:
        raise ValueError("dose_rules_invalid")
    prepared = [_normalise(rule) for rule in rules]
    imported, ids = 0, []
    for rule in prepared:
        rule_id = _rule_id(rule)
        ids.append(rule_id)
        combo = rule.get("combo_components")
        cursor = db.execute(
            "INSERT OR IGNORE INTO dose_rules(rule_id,sku,method,crop,stage,amount,unit,"
            "per_volume,per_unit,source_doc,source_page,status,created_at,combo_components) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,'candidate',?,?)",
            (rule_id, rule["sku"], rule["method"], rule["crop"], rule["stage"],
             rule["amount"], rule["unit"], rule["per_volume"], rule["per_unit"],
             rule["source_doc"], rule["source_page"], _now(),
             json.dumps(combo, ensure_ascii=False) if combo else None))
        imported += cursor.rowcount
    return {"ok": True, "imported": imported, "status": "candidate", "rule_ids": ids}


def list_rules(db, sku=None):
    sql, args = "SELECT * FROM dose_rules", []
    if sku:
        sql += " WHERE sku=?"
        args.append(sku)
    rows = db.execute(sql + " ORDER BY created_at, rule_id", args).fetchall()
    rules = []
    for row in rows:
        item = dict(row)
        raw = item.get("combo_components")
        item["combo_components"] = json.loads(raw) if raw else None
        rules.append(item)
    return {"ok": True, "rules": rules}


def review_rule(db, rule_id, decision, reviewer="operator"):
    status = {"approve": "approved", "approved": "approved",
              "reject": "rejected", "rejected": "rejected"}.get(str(decision or "").lower())
    if not status:
        raise ValueError("dose_rule_decision_invalid")
    cursor = db.execute(
        "UPDATE dose_rules SET status=?, reviewed_at=?, reviewer=? "
        "WHERE rule_id=? AND status='candidate'",
        (status, _now(), _text(reviewer, 80) or "operator", rule_id))
    if cursor.rowcount:
        return {"ok": True, "rule_id": rule_id, "status": status}
    if db.execute("SELECT 1 FROM dose_rules WHERE rule_id=?", (rule_id,)).fetchone():
        raise DoseRuleConflict("dose_rule_already_reviewed")
    raise ValueError("dose_rule_not_found")


def approved_directions(db, sku):
    """Chỉ rule approved, viết lại thành dòng hướng dẫn bộ tính liều hiện có đọc được."""
    lines = []
    for row in db.execute(
            "SELECT * FROM dose_rules WHERE sku=? AND status='approved' "
            "ORDER BY created_at, rule_id", (sku,)):
        if row["combo_components"]:
            continue  # combo đi qua approved_combo_components, không gộp một dòng
        if (row["per_unit"] or "").lower() not in LITRE_UNITS:
            continue
        lines.append(f"Pha {row['amount']:g}{row['unit']} cho bình {row['per_volume']:g} lít nước")
    return lines


def approved_combo_components(db, sku):
    """Thành phần combo của rule approved, theo dạng processor đọc (mỗi thành phần
    một dòng hướng dẫn với đơn vị riêng, kèm method riêng)."""
    out = []
    for row in db.execute(
            "SELECT combo_components FROM dose_rules WHERE sku=? AND status='approved' "
            "AND combo_components IS NOT NULL ORDER BY created_at, rule_id", (sku,)):
        for c in json.loads(row["combo_components"]):
            if c["per_unit"] not in LITRE_UNITS:
                continue
            out.append({"ten_san_pham": c["name"], "method": c["method"],
                        "huong_dan_su_dung": [
                            f"Pha {c['amount']:g}{c['unit']} cho bình {c['per_volume']:g} lít nước"]})
    return out


def profile_with_approved_rules(db, profile, sku):
    """Bản sao hồ sơ có thêm rule approved để đưa vào verified_dose_calculations."""
    merged = copy.deepcopy(profile or {})
    combos = approved_combo_components(db, sku)
    if combos:
        merged["combo_components"] = list(merged.get("combo_components") or []) + combos
    extra = approved_directions(db, sku)
    if extra:
        current = merged.get("huong_dan_su_dung")
        base = [current] if isinstance(current, str) else list(current or [])
        merged["huong_dan_su_dung"] = base + extra
    return merged
