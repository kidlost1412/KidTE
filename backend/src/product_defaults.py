"""Portable owner-reviewed profiles, source references and local photos; no shop IDs/keys."""

from __future__ import annotations

import base64
import copy
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from combo_profiles import revision
from knowledge_package import apply_package, content_hash, read_source, validate_package
from product_ingest import FIELD_ORDER, _webp_800, normalize_image
from workspace import _atomic_json, _serialized_write

_COMPONENT_FIELDS = {"profile_id", "name", "source_revision", "quantity", "package"}

_ALLOWED = (set(FIELD_ORDER) - {"gia_goc_duyet", "live_promotion"}) | {
    "san_pham_id",
    "is_combo",
    "combo_components",
    "formula_source_id",
    "variant_note",
    "approved_for_live",
    "nguon_chinh_thuc",
    "source_kind",
    "featured_image_url",
    "reference_only",
}


def _sanitize_incoming(row, existing_ids):
    """Hồ sơ nạp từ bộ: bỏ giá/khuyến mãi/trường lạ và vào ở trạng thái chờ duyệt.

    Hồ sơ trùng mã đã có trên máy không nhận cờ duyệt từ bộ (bỏ khoá), nên cờ
    duyệt chủ shop đặt trước đó được giữ nguyên khi gộp.
    """
    out = {k: copy.deepcopy(v) for k, v in row.items() if k in _ALLOWED}
    if isinstance(out.get("combo_components"), list):
        out["combo_components"] = [
            {k: v for k, v in c.items() if k in _COMPONENT_FIELDS}
            for c in out["combo_components"]
            if isinstance(c, dict)
        ]
    if out["san_pham_id"] in existing_ids:
        out.pop("approved_for_live", None)
    else:
        out["approved_for_live"] = False
    return out


def audit(records):
    by_id = {r["san_pham_id"]: r for r in records}
    errors = []
    for r in records:
        if not r.get("is_combo"):
            continue
        refs = r.get("combo_components") or []
        if not refs:
            errors.append(
                {"id": r["san_pham_id"], "error": "Combo chưa có hồ sơ nguồn."}
            )
        for c in refs:
            source = by_id.get(c.get("profile_id"))
            if not source or source.get("is_combo"):
                errors.append(
                    {"id": r["san_pham_id"], "error": "Thiếu hồ sơ sản phẩm lẻ nguồn."}
                )
            elif c.get("source_revision") != revision(source):
                errors.append(
                    {
                        "id": r["san_pham_id"],
                        "error": "Hồ sơ nguồn đã thay đổi; cần kiểm tra lại combo.",
                    }
                )
            if (
                not isinstance(c.get("quantity"), int)
                or isinstance(c.get("quantity"), bool)
                or not 1 <= c["quantity"] <= 999
            ):
                errors.append(
                    {
                        "id": r["san_pham_id"],
                        "error": "Chưa xác nhận số chai/gói từng thành phần.",
                    }
                )
            if not c.get("package"):
                errors.append(
                    {
                        "id": r["san_pham_id"],
                        "error": "Chưa có quy cách từng thành phần.",
                    }
                )
    return errors


class DefaultProfiles:
    def __init__(self, engine, ingest, seed_path=None):
        self.engine, self.ingest = engine, ingest
        self.root = Path(engine.workspace.root)
        self.db_path = engine.workspace.db_path
        self.export_path = self.root / "exports" / "bo-ho-so-mac-dinh.json"
        self.choice_path = self.root / "data" / "product_setup_choice.json"
        self.seed_path = (
            Path(seed_path)
            if seed_path
            else Path(__file__).resolve().parent.parent
            / "prompts"
            / "product_defaults.json"
        )

    def _bundle(self, source=""):
        path = source or (
            self.export_path if self.export_path.is_file() else self.seed_path
        )
        payload = json.loads(read_source(path).decode("utf-8-sig"))
        validate_package(payload)
        groups = payload["content"]["nhom_san_pham"]
        if any(
            not isinstance(g, dict) or not isinstance(g.get("san_pham"), list)
            for g in groups
        ):
            raise ValueError("Nhóm hồ sơ mặc định không hợp lệ.")
        rows = [r for g in groups for r in g["san_pham"]]
        if any(
            not isinstance(r, dict) or not isinstance(r.get("san_pham_id"), str)
            for r in rows
        ):
            raise ValueError("Hồ sơ mặc định thiếu mã sản phẩm hợp lệ.")
        if any(
            not isinstance(r.get("combo_components", []), list)
            or any(not isinstance(c, dict) for c in r.get("combo_components", []))
            for r in rows
        ):
            raise ValueError("Thành phần combo mặc định không hợp lệ.")
        if not isinstance(payload["content"].get("images", {}), dict):
            raise ValueError("Danh sách ảnh mặc định không hợp lệ.")
        return payload

    def status(self):
        try:
            choice = json.loads(self.choice_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            choice = {}
        if not isinstance(choice, dict):
            choice = {}
        rows = self.engine.knowledge.all_records()
        try:
            bundle = self._bundle()
            bundled = [
                r
                for g in bundle["content"]["nhom_san_pham"]
                for r in g.get("san_pham", [])
            ]
            count = sum(r.get("approved_for_live") is True for r in bundled)
            version, images = (
                bundle["version"],
                len(bundle["content"].get("images") or {}),
            )
        except (OSError, ValueError):
            count, version, images = 0, "", 0
        return {
            "ok": True,
            "needs_choice": not rows and not choice.get("mode"),
            "mode": choice.get("mode") or "existing",
            "products": count,
            "images": images,
            "version": version,
            "saved_path": str(self.export_path) if self.export_path.exists() else "",
        }

    @_serialized_write
    def save(self, payload=None):
        records = self.engine.knowledge.all_records()
        errors = audit(records)
        blocked = {e["id"] for e in errors}
        selected = {
            r["san_pham_id"]
            for r in records
            if r.get("approved_for_live") is True and r["san_pham_id"] not in blocked
        }
        if not selected:
            raise ValueError("Chưa có hồ sơ đã duyệt để lưu mặc định.")
        by_id = {r["san_pham_id"]: r for r in records}
        needed = set(selected)
        for rid in list(selected):
            needed.update(
                c["profile_id"]
                for c in by_id[rid].get("combo_components", [])
                if c.get("profile_id") in by_id
            )
        for rid in list(needed):
            parent = by_id[rid].get("formula_source_id")
            if parent in by_id:
                needed.add(parent)
        rows, images = [], {}
        for rid in sorted(needed):
            r = by_id[rid]
            row = {k: copy.deepcopy(v) for k, v in r.items() if k in _ALLOWED}
            if "combo_components" in row:
                row["combo_components"] = [
                    {k: v for k, v in c.items() if k in _COMPONENT_FIELDS}
                    for c in row["combo_components"]
                ]
            row["reference_only"] = rid not in selected
            row["approved_for_live"] = rid in selected
            # Preserve exact source revisions (they hash aliases); source IDs stay portable.
            row["source_revision"] = revision(r)
            rows.append(row)
            try:
                data = self.ingest.staged_image_path(rid).read_bytes()
                images[rid] = base64.b64encode(data).decode("ascii")
            except (OSError, ValueError):
                pass
        # Revisions must refer to the exported sanitized source, not machine-only aliases.
        exported = {r["san_pham_id"]: r for r in rows}
        for row in rows:
            for c in row.get("combo_components") or []:
                if c.get("profile_id") in exported:
                    c["source_revision"] = revision(exported[c["profile_id"]])
        content = {
            "nhom_san_pham": [{"nhom": "Hồ sơ mặc định đã chốt", "san_pham": rows}],
            "images": images,
        }
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        bundle = {
            "schema_version": 1,
            "version": stamp,
            "publisher": "Chủ shop Kid TE",
            "content": content,
            "content_sha256": content_hash(content),
        }
        validate_package(bundle)
        raw = json.dumps(bundle, ensure_ascii=False).encode("utf-8")
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError(
                "Bộ hồ sơ kèm ảnh vượt 16 MB; giảm kích thước ảnh trước khi lưu."
            )
        _atomic_json(self.export_path, bundle)
        return {
            "ok": True,
            "path": str(self.export_path),
            "products": len(selected),
            "reference_profiles": len(needed - selected),
            "images": len(images),
            "excluded": errors,
        }

    @_serialized_write
    def choose(self, payload):
        mode = payload.get("mode")
        if mode not in ("default", "custom"):
            raise ValueError("Chọn dùng bộ mặc định hoặc tự thêm sản phẩm.")
        if getattr(self.engine, "_is_live_running", lambda: False)():
            raise ValueError("Dừng phiên LIVE trước khi chọn bộ hồ sơ.")
        target = Path(self.engine.knowledge.catalog_path)
        if mode == "custom":
            if not target.exists():
                _atomic_json(
                    target, {"schema_version": 1, "content": {"nhom_san_pham": []}}
                )
            _atomic_json(self.choice_path, {"mode": mode})
            self.engine.knowledge.reload()
            return {"ok": True, "mode": mode}
        bundle = self._bundle(str(payload.get("source") or "").strip())
        existing_ids = {
            r.get("san_pham_id") for r in self.engine.knowledge.all_records()
        }
        for g in bundle["content"]["nhom_san_pham"]:
            g["san_pham"] = [
                _sanitize_incoming(r, existing_ids) for r in g.get("san_pham", [])
            ]
        bundle["content_sha256"] = content_hash(bundle["content"])
        rows = [
            r for g in bundle["content"]["nhom_san_pham"] for r in g.get("san_pham", [])
        ]
        ids = {r.get("san_pham_id") for r in rows}
        if len(ids) != len(rows) or any(
            not isinstance(i, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", i)
            for i in ids
        ):
            raise ValueError("Mã hồ sơ mặc định không hợp lệ/trùng.")
        errors = audit(rows)
        if errors:
            raise ValueError("Bộ combo chưa qua kiểm tra: " + errors[0]["error"])
        photos = {}
        for rid, encoded in (bundle["content"].get("images") or {}).items():
            if (
                rid not in ids
                or not isinstance(encoded, str)
                or len(encoded) > 2_000_000
            ):
                raise ValueError("Ảnh trong bộ hồ sơ không hợp lệ.")
            data = base64.b64decode(encoded, validate=True)
            photos[rid] = _webp_800(normalize_image(data, max_side=800))
        before = target.read_bytes() if target.exists() else None
        previous_photos = {}
        try:
            for rid, data in photos.items():
                path = self.ingest.staged_image_path(rid, create=True)
                if path.exists():
                    continue  # owner photos on an existing machine win
                previous_photos[path] = None
                tmp = path.with_suffix(".incoming")
                tmp.write_bytes(data)
                tmp.replace(path)
            apply_package(
                json.dumps(bundle, ensure_ascii=False).encode("utf-8"), target
            )
            self.engine.knowledge.reload()
            _atomic_json(self.choice_path, {"mode": mode, "version": bundle["version"]})
        except Exception:
            if before is None:
                target.unlink(missing_ok=True)
            else:
                tmp = target.with_suffix(".rollback")
                tmp.write_bytes(before)
                tmp.replace(target)
            for path in previous_photos:
                path.unlink(missing_ok=True)
            self.engine.knowledge.reload()
            raise
        self.engine.workspace._touch("profiles", "products", "dashboard")
        return {
            "ok": True,
            "mode": mode,
            "products": sum(r.get("reference_only") is not True for r in rows),
            "pending_approval": sum(
                r.get("approved_for_live") is False for r in rows
            ),
            "images": len(photos),
        }
