"""Sản phẩm chủ shop không muốn AI tư vấn (dù vẫn nằm trong giỏ live).

File data/shared/ai_excluded_products.json:
  {"title_contains": ["ca hoi na uy"], "product_ids": []}
So khớp tiêu đề theo dạng bỏ dấu, chữ thường. Thiếu/hỏng file => không loại gì.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone

from config import DATA_DIR
from product_ref import fold

PATH = DATA_DIR / "shared" / "ai_excluded_products.json"
REMOVED_PATH = DATA_DIR / "machine" / "removed_products.json"
_lock = threading.RLock()
_log = logging.getLogger(__name__)
# path -> (mtime_ns, size, value); value giữ bản đọc được gần nhất.
_cache = {}


def _cached(path, parse, empty):
    """Đọc JSON theo mtime; tệp hỏng => giữ bản tốt gần nhất và cảnh báo."""
    try:
        st = path.stat()
    except FileNotFoundError:
        _cache.pop(path, None)
        return empty
    except OSError:
        st = None
    key = (st.st_mtime_ns, st.st_size) if st else None
    hit = _cache.get(path)
    if hit and key and hit[0] == key:
        return hit[1]
    try:
        value = parse(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, AttributeError, KeyError, TypeError) as exc:
        _log.warning("Không đọc được %s (%s); dùng bản đọc được gần nhất.", path, exc)
        return hit[1] if hit else empty
    if key:
        _cache[path] = (key, value)
    return value


def _parse_removed(data):
    rows = data["products"]
    if not isinstance(rows, dict):
        raise ValueError("products không phải dict")
    return rows


def removed_products():
    return dict(_cached(REMOVED_PATH, _parse_removed, {}))


def set_removed(product, removed=True):
    """Persist a local listing tombstone; no shop deletion or profile deletion."""
    pid = str(product.get("product_id") or "").strip()
    if not pid or len(pid) > 160:
        raise ValueError("product_id_required")
    with _lock:
        if REMOVED_PATH.exists():
            try:
                current = json.loads(REMOVED_PATH.read_text(encoding="utf-8"))
                rows = current["products"]
                if not isinstance(rows, dict):
                    raise ValueError()
            except (OSError, ValueError, KeyError):
                raise ValueError("Danh sách sản phẩm đã xóa bị hỏng; chưa ghi thay đổi.") from None
        else:
            rows = {}
        if removed:
            rows[pid] = {"product_id": pid, "title": str(product.get("title") or pid)[:500],
                         "removed_at": datetime.now(timezone.utc).isoformat()}
        else:
            rows.pop(pid, None)
        REMOVED_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = REMOVED_PATH.with_suffix(".json.tmp")
        temporary.write_text(json.dumps({"schema_version": 1, "products": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(REMOVED_PATH)
        _cache.pop(REMOVED_PATH, None)


def _rules():
    data = _cached(PATH, lambda d: d if isinstance(d, dict) else {}, {})
    terms = [fold(t) for t in data.get("title_contains") or [] if isinstance(t, str) and t.strip()]
    ids = {str(i) for i in data.get("product_ids") or [] if str(i).strip()}
    ids.update(removed_products())
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
