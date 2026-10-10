"""Đọc nhãn cũ thành các dose rule candidate; mỗi tỷ lệ tìm thấy là một candidate cần duyệt.

Không bao giờ chọn một tỷ lệ thay người duyệt và không bao giờ tự approved.
"""
from __future__ import annotations

import re

import dose_rules

_TANK = r"(?:bình|binh|phuy|thùng|thung|bồn|bon)"
_NUM = r"(\d+(?:[.,]\d+)?)"
_UNIT = r"(ml|kg|gr(?:am)?|g)(?![a-zA-Z])"
_LITRE = rf"{_NUM}(?:\s*[-–—]\s*{_NUM})?\s*l(?:ít|it)?(?![a-zA-Z0-9])"
_AMOUNT = rf"{_NUM}(?:\s*[-–—]\s*{_NUM})?\s*{_UNIT}"
_FORWARD = re.compile(rf"{_AMOUNT}[^\d]{{0,24}}?(?:{_TANK}\s*)?{_LITRE}", re.I)
_REVERSED = re.compile(rf"{_TANK}\s*{_LITRE}[^\d]{{0,12}}?{_AMOUNT}", re.I)
_UNIT_MAP = {"gr": "g", "gram": "g", "g": "g", "ml": "ml", "kg": "kg"}


def normalize_unit(unit):
    return _UNIT_MAP.get(str(unit or "ml").lower())


def _f(value):
    return float(value.replace(",", ".")) if value else None


def _ratios(line):
    """Mọi tỷ lệ trong một dòng, theo thứ tự xuất hiện, không trùng lặp vùng."""
    found, spans = [], []
    for regex, reverse in ((_FORWARD, False), (_REVERSED, True)):
        for m in regex.finditer(line):
            if any(m.start() < e and s < m.end() for s, e in spans):
                continue
            spans.append((m.start(), m.end()))
            g = m.groups()
            lit_lo, lit_hi, lo, hi, unit = (g if reverse else (g[3], g[4], g[0], g[1], g[2]))
            found.append((m.start(), _f(lo), _f(hi), normalize_unit(unit),
                          _f(lit_lo), _f(lit_hi), m.group(0)))
    return [item[1:] for item in sorted(found)]


def parse_label(text, sku, source_doc="", source_page="", method="", crop="", stage=""):
    """Trả danh sách rule (dict) cho dose_rules.import_rules, kèm needs_review=True."""
    out, seen = [], set()
    for line in re.split(r"[\n;•]+", str(text or "")):
        for lo, hi, unit, lit_lo, lit_hi, raw in _ratios(line):
            if not unit or not lo or not lit_lo:
                continue
            for amount in sorted({lo, hi or lo}):
                for litres in sorted({lit_lo, lit_hi or lit_lo}):
                    key = (amount, unit, litres)
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append({
                        "sku": sku, "method": method, "crop": crop, "stage": stage,
                        "amount": amount, "unit": unit, "per_volume": litres,
                        "per_unit": "lít", "source_doc": source_doc,
                        "source_page": source_page, "needs_review": True,
                        "source_text": " ".join(raw.split())[:200],
                        "ambiguous": len(_ratios(line)) > 1 or hi is not None or lit_hi is not None,
                    })
    return out


def import_label(db, text, sku, **meta):
    """Parse rồi nhập toàn bộ tỷ lệ ở trạng thái candidate; không trả về/chọn tỷ lệ nào."""
    rules = parse_label(text, sku, **meta)
    if not rules:
        return {"ok": True, "imported": 0, "status": "candidate", "rule_ids": [],
                "needs_review": False}
    result = dose_rules.import_rules(db, rules)
    result["needs_review"] = True
    result["candidates"] = len(rules)
    return result
