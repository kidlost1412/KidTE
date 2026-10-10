"""Dựng gói hồ sơ chính thức đi kèm bộ cài (backend/prompts/knowledge_seed.json).

    python backend/tools/build_knowledge_seed.py <product_knowledge.json> [--out ...]

Đầu vào là tệp shared/product_knowledge.json của máy phát triển. Đầu ra là gói
``knowledge_package`` hợp lệ (version + content_sha256) đã bỏ:
  * mọi trường riêng của máy (xuất qua ``knowledge_package.xuat_goi``),
  * cờ duyệt, mã hồ sơ cũ, quy tắc liều (``dose_rules``), trạng thái catalog,
  * hồ sơ LEGACY-* (hồ sơ kỹ thuật cũ chưa gắn, không dành cho live),
  * mọi trường ngoài danh sách cho phép (chặn dữ liệu khách/dữ liệu máy lọt vào).
Không bao giờ ghi ánh xạ, override hay approved_live_profiles vào gói.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import knowledge_package as kp  # noqa: E402
from knowledge_diff import TRUONG_RIENG_CUA_MAY  # noqa: E402

DEFAULT_OUT = ROOT / "prompts" / "knowledge_seed.json"

# Chỉ những trường này được đi vào bộ cài.
ALLOWED_FIELDS = (
    "san_pham_id", "ten_san_pham", "ten_tren_website", "thanh_phan_dang_ky",
    "cong_dung", "uu_diem_website", "huong_dan_su_dung", "luu_y",
    "phan_khuc", "mo_ta_ngan", "doi_tuong_cay_trong", "hieu_qua_chinh",
    "nguon_chinh_thuc", "website_product_ids", "website_categories",
    "source_kind", "source_modified", "featured_image_url", "featured_image_alt",
    "is_combo", "combo_components",
)
COMBO_COMPONENT_FIELDS = (
    "website_product_id", "ten_san_pham", "thanh_phan_dang_ky", "cong_dung",
    "huong_dan_su_dung", "luu_y", "nguon_chinh_thuc", "featured_image_url",
)
FORBIDDEN_FIELDS = set(TRUONG_RIENG_CUA_MAY) | {
    "dose_rules", "approved_for_live", "approved_by", "approved_at",
    "legacy_profile_id", "shop_product_ids", "excluded_shop_product_ids",
    "created_local", "local_override", "profile_overrides",
}


def version_from(source_payload):
    """Phiên bản tất định từ ngày phát hành của nguồn: 2026.09.13 (cùng nguồn -> cùng bản)."""
    stamp = str(source_payload.get("published_at") or "")[:10]
    parts = stamp.split("-")
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        return ".".join(parts)
    raise ValueError("nguồn thiếu published_at dạng YYYY-MM-DD; truyền --version")


def _keep(record):
    out = {}
    for key in ALLOWED_FIELDS:
        value = record.get(key)
        if value in (None, "", [], {}):
            continue
        if key == "combo_components":
            value = [{k: c[k] for k in COMBO_COMPONENT_FIELDS if c.get(k) not in (None, "", [], {})}
                     for c in value if isinstance(c, dict)]
        out[key] = value
    return out


def build(source_path, version=None, publisher="Kid TE — hồ sơ chính thức"):
    source_payload = json.loads(Path(source_path).read_text(encoding="utf-8-sig"))
    version = version or version_from(source_payload)
    content = source_payload.get("content", source_payload)
    groups = []
    for group in content.get("nhom_san_pham", []) or []:
        products = []
        for record in group.get("san_pham", []) or []:
            pid = str(record.get("san_pham_id") or "").strip()
            if not pid or pid.startswith("LEGACY-"):
                continue
            products.append(_keep(record))
        if products:
            kept = {k: v for k, v in group.items() if k != "san_pham"}
            groups.append({**kept, "san_pham": products})
    staging = {"nhom_san_pham": groups}
    # Đi qua xuat_goi để dùng đúng đường bỏ trường riêng của máy + kiểm hash.
    with tempfile.TemporaryDirectory(prefix="kid-te-seed-") as folder:
        tmp = Path(folder) / "staging.json"
        tmp.write_text(json.dumps({"content": staging}, ensure_ascii=False), encoding="utf-8")
        package = kp.xuat_goi(tmp, version, publisher)
    package["published_at"] = str(source_payload.get("published_at") or "")
    problems = verify(package)
    if problems:
        raise ValueError("gói hạt giống không sạch: " + "; ".join(problems))
    return package


def verify(package):
    """Danh sách lỗi; rỗng nghĩa là gói hợp lệ và không chứa trường bị cấm."""
    problems = []
    try:
        kp.validate_package(package)
    except ValueError as exc:
        problems.append(str(exc))
        return problems
    count = 0
    for group in package["content"].get("nhom_san_pham", []):
        for record in group.get("san_pham", []):
            count += 1
            bad = FORBIDDEN_FIELDS & set(record)
            if bad:
                problems.append(f"{record.get('san_pham_id')}: trường bị cấm {sorted(bad)}")
            if str(record.get("san_pham_id", "")).startswith("LEGACY-"):
                problems.append(f"{record.get('san_pham_id')}: hồ sơ LEGACY")
            if not (record.get("ten_san_pham") and (record.get("cong_dung")
                                                     or record.get("huong_dan_su_dung"))):
                problems.append(f"{record.get('san_pham_id')}: không đủ nội dung để duyệt")
    if not count:
        problems.append("gói không có hồ sơ nào")
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--version", default="")
    args = parser.parse_args(argv)
    package = build(args.source, args.version or None)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(package, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    total = sum(len(g["san_pham"]) for g in package["content"]["nhom_san_pham"])
    print(f"{args.out} version={package['version']} products={total} sha256={package['content_sha256']}")


if __name__ == "__main__":
    main()
