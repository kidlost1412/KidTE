"""Cài đặt người vận hành cho từng bộ xử lý của AI (bật/tắt, danh sách, ngưỡng).

Mỗi bộ xử lý tự khai ``SETTINGS_SCHEMA`` (khoá, kiểu, mặc định, giới hạn) và
hàm ``configure(values)``. File này chỉ lo đọc/ghi/kiểm tra giá trị, lưu ở
``DuLieu/data/cai_dat_bo_xu_ly.json`` để đi theo bản sao lưu.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path


def _atomic_write(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _coerce(field, value):
    """Ép một giá trị người dùng gửi lên về đúng kiểu; sai thì ValueError."""
    kind = field.get("type")
    if kind == "bool":
        if isinstance(value, bool):
            return value
        raise ValueError(f"{field.get('label') or field['key']}: cần bật/tắt")
    if kind == "number":
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{field.get('label') or field['key']}: cần một con số") from None
        low, high = field.get("min"), field.get("max")
        if (low is not None and number < low) or (high is not None and number > high):
            raise ValueError(f"{field.get('label') or field['key']}: phải trong khoảng {low}–{high}")
        return int(number) if field.get("integer") else round(number, 3)
    if kind == "list":
        items = value if isinstance(value, list) else str(value or "").splitlines()
        seen, cleaned = set(), []
        for item in items:
            text = " ".join(str(item or "").split())[:120]
            if text and text.casefold() not in seen:
                seen.add(text.casefold())
                cleaned.append(text)
        if len(cleaned) > 400:
            raise ValueError(f"{field.get('label') or field['key']}: tối đa 400 mục")
        if field.get("required") and not cleaned:
            raise ValueError(f"{field.get('label') or field['key']}: không được để trống")
        return cleaned
    if kind in {"text", "select"}:
        text = str(value or "").strip()[: int(field.get("max_length") or 2000)]
        if kind == "select" and text not in {option[0] for option in field.get("options") or []}:
            raise ValueError(f"{field.get('label') or field['key']}: lựa chọn không hợp lệ")
        if field.get("required") and not text:
            raise ValueError(f"{field.get('label') or field['key']}: không được để trống")
        return text
    raise ValueError(f"{field.get('label') or field.get('key')}: kiểu cài đặt không hỗ trợ")


def defaults(schema):
    return {field["key"]: (list(field["default"]) if isinstance(field["default"], (list, tuple))
                           else field["default"]) for field in schema}


class ProcessorSettings:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        self._data = data if isinstance(data, dict) else {}

    def values(self, plugin_id, schema):
        """Mặc định + phần người dùng đã đổi (bỏ qua giá trị hỏng hay khoá lạ)."""
        merged = defaults(schema)
        saved = self._data.get(plugin_id) or {}
        for field in schema:
            if field["key"] in saved:
                try:
                    merged[field["key"]] = _coerce(field, saved[field["key"]])
                except ValueError:
                    pass
        return merged

    def changed_keys(self, plugin_id, schema):
        base, current = defaults(schema), self.values(plugin_id, schema)
        return sorted(key for key in current if current[key] != base[key])

    def save(self, plugin_id, schema, updates):
        by_key = {field["key"]: field for field in schema}
        clean = {}
        for key, value in (updates or {}).items():
            if key not in by_key:
                raise ValueError(f"{key}: không phải cài đặt của bộ xử lý này")
            clean[key] = _coerce(by_key[key], value)
        with self._lock:
            current = dict(self._data.get(plugin_id) or {})
            current.update(clean)
            base = defaults(schema)
            # Chỉ lưu phần KHÁC mặc định: sau này đổi mặc định trong mã thì
            # máy chưa từng chỉnh vẫn nhận mặc định mới.
            current = {key: value for key, value in current.items()
                       if key in by_key and value != base[key]}
            if current:
                self._data[plugin_id] = current
            else:
                self._data.pop(plugin_id, None)
            _atomic_write(self.path, self._data)
        return self.values(plugin_id, schema)

    def reset(self, plugin_id, schema):
        with self._lock:
            self._data.pop(plugin_id, None)
            _atomic_write(self.path, self._data)
        return self.values(plugin_id, schema)

    def reload(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        with self._lock:
            self._data = data if isinstance(data, dict) else {}
