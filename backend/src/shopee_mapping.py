"""Bảng ánh xạ sản phẩm timeline sang Shopee, lưu trong dữ liệu máy Kid TE."""
from __future__ import annotations

import json
import os
import re
import threading
import time
from knowledge import fold
from pathlib import Path


_ID = re.compile(r"^[0-9]{1,30}$")


class ShopeeMappingStore:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()

    def _read(self):
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"schema_version": 1, "videos": {}}
        if not isinstance(value, dict) or not isinstance(value.get("videos"), dict):
            raise ValueError("shopee_mapping_file_invalid")
        return value

    def all(self):
        with self.lock:
            value = self._read()
            return {"ok": True, "videos": value["videos"], "catalog": value.get("catalog", {})}

    @staticmethod
    def _name(value):
        name = re.sub(r"[^a-z0-9]+", " ", fold(value)).strip()
        return re.sub(r"\b(\d+)\s*(?:lit|liter|l)\b", r"\1l", name)

    def sync(self, payload, timelines):
        """Resolve every existing script against the current bag without popup setup."""
        if not isinstance(payload, dict) or not _ID.fullmatch(str(payload.get("session_id") or "")):
            return {"ok": False, "error": "shopee_session_invalid"}
        rows = payload.get("products")
        if not isinstance(rows, list) or len(rows) > 500:
            return {"ok": False, "error": "shopee_catalog_invalid"}
        catalog = {}
        for row in rows:
            if not isinstance(row, dict):
                return {"ok": False, "error": "shopee_catalog_invalid"}
            shop, product = str(row.get("shop_id") or ""), str(row.get("product_id") or "")
            if not _ID.fullmatch(shop) or not _ID.fullmatch(product):
                return {"ok": False, "error": "shopee_product_id_invalid"}
            catalog[(shop, product)] = {"shop_id": shop, "product_id": product, "title": str(row.get("title") or "")[:500]}
            entry = catalog[(shop, product)]
            entry["price"] = str(row.get("price") or "")[:100]
            raw = row.get("price_data")
            if isinstance(raw, dict) and len(json.dumps(raw, ensure_ascii=False)) <= 20000:
                entry["price_data"] = raw

        def target_kind(key, spec):
            name = self._name(str(spec.get("title") or spec.get("short_name") or key))
            if "combo" in name and "dam ca" in name and "humic" in name:
                return "fish5"
            if "nano dong" in name:
                return "nano_dong"
            if key in {"topone", "soil", "sachsau"} or any(term in name for term in ("bo tri", "top bo one", "top one", "sach sau")):
                return "tc01"
            if "tc01" in name or key == "tc01":
                return "tc01"
            if "dam ca" in name or key in {"fish1", "fish5"}:
                if "5l" in name or key == "fish5":
                    return "fish5"
                if "1l" in name or key == "fish1":
                    return "fish1"
            return ""

        def select(kind, spec):
            matches = []
            for row in catalog.values():
                name = self._name(row["title"])
                if "khong ban" in name or "hang tang kem" in name:
                    continue
                if kind == "tc01":
                    matched = bool(re.search(r"\btc01\b", name))
                elif kind == "nano_dong":
                    matched = "nano dong" in name
                elif kind in {"fish1", "fish5"}:
                    matched = "dam ca" in name and ("1l" if kind == "fish1" else "5l") in name.split() and "combo" not in name
                else:
                    includes = spec.get("include_all") or []
                    excludes = spec.get("exclude_any") or []
                    matched = bool(includes) and all(self._name(term) in name for term in includes) and not any(self._name(term) in name for term in excludes)
                    matched = matched or (bool(spec.get("title")) and name == self._name(spec["title"]))
                if matched:
                    matches.append({k: row[k] for k in ("shop_id", "product_id")})
            return matches[0] if len(matches) == 1 else None

        with self.lock:
            data = self._read()
            issues = {}
            resolved = {}
            for video_id, timeline in timelines.items():
                old = data["videos"].get(video_id, {})
                keys = {str(event[field]) for event in timeline.get("events", []) for field in ("product", "alternate_with") if event.get(field)}
                mappings, alternates, missing = {}, {}, []
                for key in keys:
                    spec = timeline.get("products", {}).get(key, {})
                    saved = old.get("mappings", {}).get(key)
                    # Preserve a saved exact listing while it is still in this bag.
                    valid_saved = isinstance(saved, dict) and (saved.get("shop_id"), saved.get("product_id")) in catalog
                    kind = target_kind(key, spec)
                    if valid_saved and kind:
                        name = self._name(catalog[(saved["shop_id"], saved["product_id"])]["title"])
                        valid_saved = "khong ban" not in name and "hang tang kem" not in name and (
                            bool(re.search(r"\btc01\b", name)) if kind == "tc01" else
                            "nano dong" in name if kind == "nano_dong" else
                            "dam ca" in name and ("1l" if kind == "fish1" else "5l") in name.split() and "combo" not in name)
                    chosen = saved if valid_saved else select(kind, spec)
                    if chosen:
                        mappings[key] = {k: chosen[k] for k in ("shop_id", "product_id")}
                    else:
                        missing.append(key)
                for event in timeline.get("events", []):
                    base, other = event.get("product"), event.get("alternate_with")
                    if not base or not other or mappings.get(base) != mappings.get(other) or base not in mappings:
                        continue
                    spec = timeline.get("products", {}).get(base, {})
                    name = self._name(spec.get("title") or spec.get("short_name") or base)
                    if "dam ca" not in name or "humic" not in name:
                        continue
                    saved = old.get("alternates", {}).get(base)
                    alternate = saved if isinstance(saved, dict) and (saved.get("shop_id"), saved.get("product_id")) in catalog else None
                    if alternate:
                        title = self._name(catalog[(alternate["shop_id"], alternate["product_id"])]["title"])
                        if alternate == mappings[base] or "dam ca" not in title or "1l" not in title.split() or "combo" in title or "khong ban" in title or "hang tang kem" in title:
                            alternate = None
                    if not alternate and "dam ca" in name and "humic" in name:
                        alternate = select("fish1", {})
                    if alternate:
                        alternates[base] = {k: alternate[k] for k in ("shop_id", "product_id")}
                    elif "dam ca" in name and "humic" in name:
                        missing.append(f"{base}:luan_phien_1l")
                record = {"mappings": mappings, "alternates": alternates, "unresolved": sorted(missing)}
                resolved[video_id] = record
                if missing:
                    issues[video_id] = sorted(missing)
                data["videos"][video_id] = record
            data["catalog"] = {"session_id": str(payload["session_id"]), "updated_at": time.time(), "products": list(catalog.values())}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, self.path)
        return {"ok": True, "videos": resolved, "issues": issues, "product_count": len(catalog)}

    def save(self, payload, timelines):
        if not isinstance(payload, dict):
            return {"ok": False, "error": "mapping_payload_invalid"}
        video_id = str(payload.get("video_id") or "").strip()
        timeline = timelines.get(video_id) if isinstance(timelines, dict) else None
        if not timeline:
            return {"ok": False, "error": "video_timeline_not_found"}
        keys = set()
        for event in timeline.get("events") or []:
            for name in ("product", "alternate_with"):
                if event.get(name):
                    keys.add(str(event[name]))
        mappings = payload.get("mappings")
        alternates = payload.get("alternates") or {}
        if not isinstance(mappings, dict) or set(mappings) != keys:
            return {"ok": False, "error": "shopee_mapping_keys_incomplete"}
        if not isinstance(alternates, dict) or not set(alternates).issubset(keys):
            return {"ok": False, "error": "shopee_alternate_keys_invalid"}

        def clean_items(items):
            result = {}
            for key, row in items.items():
                if not isinstance(row, dict):
                    return None
                shop = str(row.get("shop_id") or "").strip()
                product = str(row.get("product_id") or "").strip()
                if not _ID.fullmatch(shop) or not _ID.fullmatch(product):
                    return None
                result[key] = {"shop_id": shop, "product_id": product}
            return result

        clean_mappings = clean_items(mappings)
        clean_alternates = clean_items(alternates)
        if clean_mappings is None or clean_alternates is None:
            return {"ok": False, "error": "shopee_product_id_invalid"}
        # Nhiều mục timeline được phép trỏ cùng một sản phẩm Shopee.
        with self.lock:
            data = self._read()
            data["videos"][video_id] = {
                "mappings": clean_mappings,
                "alternates": clean_alternates,
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, self.path)
        return {"ok": True, "video_id": video_id, "mapped_count": len(clean_mappings),
                "alternate_count": len(clean_alternates)}
