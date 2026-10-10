"""Sản phẩm chủ shop không muốn AI tư vấn (dù vẫn nằm trong giỏ live).

File data/shared/ai_excluded_products.json:
  {"title_contains": ["ca hoi na uy"], "product_ids": []}
So khớp tiêu đề theo dạng bỏ dấu, chữ thường. Thiếu/hỏng file => không loại gì.
"""
from __future__ import annotations

import json

from config import DATA_DIR
from product_ref import fold

PATH = DATA_DIR / "shared" / "ai_excluded_products.json"


def _rules():
    try:
        data = json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], set()
    terms = [fold(t) for t in data.get("title_contains") or [] if isinstance(t, str) and t.strip()]
    ids = {str(i) for i in data.get("product_ids") or [] if str(i).strip()}
    return terms, ids


def is_excluded(product, rules=None):
    terms, ids = rules or _rules()
    if not isinstance(product, dict):
        return False
    if str(product.get("product_id") or "") in ids:
        return True
    title = fold(product.get("title") or "")
    return any(term in title for term in terms)


def filter_products(products):
    rules = _rules()
    if not rules[0] and not rules[1]:
        return list(products or [])
    return [p for p in products or [] if not is_excluded(p, rules)]
