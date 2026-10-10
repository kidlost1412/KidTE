"""Dựng thư viện sản phẩm chính thức và hồ sơ riêng cho từng SKU LIVE.

Nguồn kỹ thuật là snapshot website do import_company_website.py tạo. Ánh xạ
SKU nằm trong data/machine/catalog_mapping.json (assignments) và phải bao phủ
đúng catalog LIVE; script sẽ dừng nếu thiếu, trùng hoặc trỏ tới sản phẩm web
không tồn tại.

`[SUA 13/09/2026]` Truoc day cong cu nay doc anh xa tu data/company_catalog_mapping.json
va ghi toan bo ket qua vao data/approved_product_knowledge.json — ca hai deu
la file legacy ma runtime khong doc. Da doi:
  - Doc anh xa tu data/machine/catalog_mapping.json (website_product_ids duoc
    doi nguoc tu product_code qua catalog_layers.website_ids_from_product_code).
  - Xuat ban qua catalog_layers.build_catalog_layers() vao
    data/shared/product_knowledge.json + data/machine/catalog_mapping.json,
    khong con ghi file legacy nao.
  - "Da duyet truoc do" va cac truong CURATED (vd dose_rules, gia_goc_duyet)
    duoc doc lai tu CHINH hai file runtime nay (truoc khi ghi de), khong con
    doc tu approved_product_knowledge.json.

`[SUA 13/09/2026 — T4]` canh_bao_quy_cach / quy_cach_dong_goi / gia_goc_duyet /
live_promotion / listing_source / source_discrepancy gio co cho chua trong
machine: assignment["profile_overrides"] (xem
catalog_layers.PROFILE_OVERRIDE_FIELDS + build_catalog_layers()). Cong cu vAn
giu MOT chot tu kiem sau khi build_catalog_layers() chay: neu mot truong nao
do live_record() tinh ra ma KHONG khop voi profile_overrides tuong ung (vd co
nguoi them truong moi ma quen cap nhat PROFILE_OVERRIDE_FIELDS), TU CHOI xuat
ban thay vi am tham mat canh bao.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from console_io import force_utf8_console  # noqa: E402
from backup_policy import create_operational_backup  # noqa: E402

force_utf8_console()

from import_company_website import packages  # noqa: E402
from catalog_layers import (  # noqa: E402
    CatalogLayerError, atomic_write_json, build_catalog_layers,
    canonical_product_code, read_json, website_ids_from_product_code,
)
from config import MACHINE_CATALOG_MAPPING_PATH, SHARED_PRODUCT_KNOWLEDGE_PATH  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
OFFICIAL_GROUP_KEY = "company_website_official"
LIVE_GROUP_KEY = "live_sku_official"


def _relative_label(path):
    """Đường dẫn tương đối để ghi vào bản ghi, dùng đường tuyệt đối nếu path
    (vd fixture trong test) nằm ngoài ROOT."""
    try:
        return Path(path).relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)

# Cac truong nay do sync_official_catalog.py hoac live_record() tu tinh ra,
# nhung machine/catalog_mapping.json hom nay CHUA co cho chua chung (xem T4).
# Neu de lot qua build_catalog_layers(), chung se bi am tham xoa mat vi
# _scrub_shared() loai chung khoi shared con machine thi khong co slot nao
# giu lai. Tu choi xuat ban con hon xoa mat mot canh bao quy cach.
UNSAFE_TO_DROP_FIELDS = (
    "canh_bao_quy_cach", "quy_cach_dong_goi", "gia_goc_duyet",
    "live_promotion", "listing_source", "source_discrepancy",
)


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


FOOTER_NOISE = (
    "nong duoc toan cau", "di dau chat luong", "nongduoctoancaugroup.com",
    "http://", "https://", "hotline", "lien he", "fanpage", "website",
)


def clean_lines(values):
    """Bo dong chan trang website lot vao phan huong dan/cong dung.

    Ban chep tu website keo ca "Nong Duoc Toan Cau - Di Dau Chat Luong" va
    dia chi web vao muc huong dan su dung. De nguyen thi model coi do la mot
    dong huong dan va co the doc ra cho khach.
    """
    import unicodedata

    def fold(value):
        text = unicodedata.normalize(
            "NFD", str(value or "").casefold())
        return "".join(ch for ch in text if unicodedata.category(ch) != "Mn")

    kept = []
    for item in values or []:
        text = " ".join(str(item).split())
        if not text:
            continue
        folded = fold(text)
        if any(noise in folded for noise in FOOTER_NOISE):
            continue
        kept.append(text)
    return kept


def joined(values):
    return " | ".join(str(item).strip() for item in values or [] if str(item).strip())


def base_fields(profile):
    return {
        "ten_san_pham": profile["name"],
        "listing_aliases": [profile["name"]],
        "quy_cach_dong_goi": ", ".join(profile.get("packages") or []),
        "thanh_phan_dang_ky": clean_lines(profile["ingredients"]),
        "cong_dung": clean_lines(profile["uses"]),
        "uu_diem_website": clean_lines(profile.get("advantages")),
        "huong_dan_su_dung": clean_lines(profile["directions"]),
        "luu_y": joined(clean_lines(profile.get("notes"))),
        "nguon_chinh_thuc": profile["url"],
        "website_product_ids": [profile["website_product_id"]],
        "source_modified": profile.get("source_modified", ""),
        "source_kind": "company_website",
        "website_categories": profile.get("categories") or [],
        "featured_image_url": profile.get("featured_image_url", ""),
        "featured_image_alt": profile.get("featured_image_alt", ""),
    }


_PACKAGE_TAIL = re.compile(
    r"\s*(?:[–—-]\s*)?\(?\s*\d+(?:[.,]\d+)?\s*"
    r"(?:ml|l|lit|lít|kg|g|gr|gram)\s*\)?\s*$",
    re.I,
)


def name_for_live_sku(website_name, website_packages, live_packages):
    """Ten phai dung quy cach cua SKU dang ban, khong phai cua trang web.

    Chai 1L va can 5L dung chung mot trang website; de nguyen ten trang la
    model se noi "5 lit" voi khach dang hoi chai 1L.
    """
    if not live_packages:
        return website_name, False
    same = {str(item).strip().casefold() for item in website_packages or []}
    wanted = {str(item).strip().casefold() for item in live_packages}
    if same == wanted:
        return website_name, False
    stem = _PACKAGE_TAIL.sub("", str(website_name)).rstrip(" -–—(,")
    return f"{stem} ({', '.join(live_packages)})", True


def component_view(profile):
    return {
        "website_product_id": profile["website_product_id"],
        "ten_san_pham": profile["name"],
        "thanh_phan_dang_ky": clean_lines(profile["ingredients"]),
        "cong_dung": clean_lines(profile["uses"]),
        "huong_dan_su_dung": clean_lines(profile["directions"]),
        "luu_y": clean_lines(profile.get("notes")),
        "nguon_chinh_thuc": profile["url"],
        "featured_image_url": profile.get("featured_image_url", ""),
    }


# Nhung truong nay do NGUOI soan, website khong co. Dong bo lai ma khong
# chuyen sang la xoa cong da bo ra (vd dose_rules cua TC01: lieu theo tung
# loai cay va duong dung). `dose_rules` la truong KY THUAT dung chung nen doc
# lai tu shared/product_knowledge.json.
CURATED_SHARED_FIELDS = ("dose_rules",)
# `[T4]` gia_goc_duyet/live_promotion/listing_source la MACHINE_ONLY nhung
# GIO DA co cho chua: assignment["profile_overrides"] (xem
# catalog_layers.PROFILE_OVERRIDE_FIELDS). Doc lai tu do de khong mat qua
# moi lan dong bo.
CURATED_PROFILE_OVERRIDE_FIELDS = ("gia_goc_duyet", "live_promotion", "listing_source")
# Cac truong nay VAN CHUA co cho chua trong machine schema (khac loai voi
# profile_overrides — day la danh sach SKU/khoa do nguoi soan tay, khong
# phai canh bao/gia tinh tu live_record()). Cac truong nay co tinh chua lam.
# Giu ten o day de khong quen, nhung carry-over
# se luon rong cho toi khi co migration + test rieng.
CURATED_MACHINE_FIELDS_NOT_YET_SUPPORTED = (
    "seller_skus", "shop_sku_ids", "excluded_shop_product_ids", "excluded_shop_sku_ids",
)
CURATED_FIELDS = (CURATED_SHARED_FIELDS + CURATED_PROFILE_OVERRIDE_FIELDS
                  + CURATED_MACHINE_FIELDS_NOT_YET_SUPPORTED)


def live_record(live, rule, profiles, commercial, approve_high,
                previously_approved):
    selected = [profiles[item] for item in rule["website_product_ids"]]
    is_combo = bool(rule.get("combo") or len(selected) > 1)
    if is_combo:
        record = {
            "ten_san_pham": live["title"].split("–", 1)[0].strip(),
            "listing_aliases": [live["title"]] + [item["name"] for item in selected],
            "thanh_phan_dang_ky": {
                item["name"]: clean_lines(item["ingredients"]) for item in selected
            },
            "cong_dung": [
                f'{item["name"]}: {line}'
                for item in selected for line in clean_lines(item["uses"])
            ],
            "huong_dan_su_dung": [
                f'{item["name"]}: {line}'
                for item in selected for line in clean_lines(item["directions"])
            ],
            "luu_y": (
                "Đây là combo thương mại. Hai sản phẩm ĐÁNH CHUNG MỘT BÌNH, mỗi loại "
                "giữ nguyên liều của chính nó trên cùng thể tích nước — không cộng "
                "liều hai loại lại thành một con số, cũng không chia đôi. "
                "(Chủ dự án xác nhận 11/09/2026.)"
            ),
            "combo_components": [component_view(item) for item in selected],
            "is_combo": True,
        }
    else:
        record = base_fields(selected[0])
        record["listing_aliases"] = [live["title"], selected[0]["name"]]

    live_packages = packages(live["title"])
    if live_packages:
        record["quy_cach_dong_goi"] = ", ".join(live_packages)
    if not is_combo:
        website_packages = selected[0].get("packages") or []
        fixed_name, differs = name_for_live_sku(
            selected[0]["name"], website_packages, live_packages)
        record["ten_tren_website"] = selected[0]["name"]
        record["ten_san_pham"] = fixed_name
        if differs:
            record["canh_bao_quy_cach"] = (
                f"SKU đang bán là {', '.join(live_packages)} nhưng hồ sơ kỹ thuật"
                f" lấy từ trang {', '.join(website_packages) or 'khác quy cách'}"
                " (cùng công thức). Nói với khách đúng quy cách của SKU đang bán;"
                " liều theo nhãn vẫn tính trên lượng nước, không theo dung tích chai."
            )
    old = commercial.get(str(live["product_id"]), {})
    # Cong duyet phai do NGUOI mo. Mac dinh khong duyet; chi duyet neu SKU nay
    # von da duoc duyet tay tu truoc, hoac nguoi van hanh chay --approve-high
    # sau khi doc bang soat (va ngay ca luc do cung bo qua combo va cac cap
    # co canh bao sai khac).
    # Mot canh bao moi (vd verify_catalog_mapping.py vua phat hien lech quy
    # cach) PHAI ha duyet ca ban da duyet truoc do — neu khong, cai duyet
    # nham cua lan chay truoc se song mai va khong ai thay nua.
    # Combo mac dinh KHONG duoc tu duyet. Chi mo khi nguoi that da doc va
    # danh dau `combo_da_duyet` trong mapping — combo moi nhap ve sau nay
    # van dung lai cho nguoi xem, khong an theo lan duyet nay.
    combo_blocked = bool(rule.get("combo")) and not rule.get("combo_da_duyet")
    blocked = bool(combo_blocked or rule.get("review_note")
                   or rule["confidence"] != "high")
    was_approved = (str(live["product_id"]) in previously_approved
                    and not blocked)
    auto_ok = (
        approve_high
        and rule["confidence"] == "high"
        and not combo_blocked
        and not rule.get("review_note")
    )
    approved = bool(was_approved or auto_ok)
    if was_approved:
        approved_by = old.get("approved_by") or "Chủ dự án Kid TE"
        approved_at = old.get("approved_at") or datetime.now().astimezone().isoformat(timespec="seconds")
    elif auto_ok:
        approved_by = "Chủ dự án Kid TE (lệnh --approve-high)"
        approved_at = datetime.now().astimezone().isoformat(timespec="seconds")
    else:
        approved_by = ""
        approved_at = ""
    record.update({
        "san_pham_id": f'LIVE-{live["product_id"]}',
        "shop_product_ids": [str(live["product_id"])],
        "approved_for_live": approved,
        "approved_by": approved_by,
        "approved_at": approved_at,
        "cho_nguoi_duyet": not approved,
        "mapping_confidence": rule["confidence"],
        "website_product_ids": rule["website_product_ids"],
        "nguon_chinh_thuc": [item["url"] for item in selected],
        "source_kind": "company_website_and_live_catalog",
    })
    if rule.get("variant_note"):
        record["variant_note"] = rule["variant_note"]
    if rule.get("review_note"):
        record["source_discrepancy"] = rule["review_note"]
    for field in CURATED_FIELDS:
        if old.get(field):
            record[field] = old[field]
    # Giu duong noi toi overlay approved_live_profiles.json (khoa SP-xx).
    legacy = old.get("legacy_profile_id") or old.get("san_pham_id")
    if legacy and str(legacy).startswith("SP-"):
        record["legacy_profile_id"] = legacy
    return record


def _rules_from_machine(assignments):
    """Dựng lại 'rule' dạng cũ (website_product_ids, confidence, ...) từ
    assignments của machine/catalog_mapping.json — nguồn ánh xạ duy nhất."""
    rules = {}
    for shop_id, assignment in assignments.items():
        audit = assignment.get("mapping_audit") or {}
        rules[shop_id] = {
            "shop_product_id": shop_id,
            "website_product_ids": website_ids_from_product_code(assignment["product_code"]),
            "confidence": audit.get("confidence", ""),
            "combo": bool(audit.get("combo")),
            "combo_da_duyet": bool(audit.get("combo_da_duyet")),
            "review_note": audit.get("review_note", ""),
            "variant_note": audit.get("variant_note", ""),
            "xac_nhan_boi": audit.get("xac_nhan_boi", ""),
            "xac_nhan_ngay": audit.get("xac_nhan_ngay", ""),
            "xac_nhan_loi_nguoi_duyet": audit.get("xac_nhan_loi_nguoi_duyet", ""),
        }
    return rules


def _current_shared_by_code(shared_path):
    if not Path(shared_path).exists():
        return {}
    try:
        doc = read_json(shared_path)
    except CatalogLayerError:
        return {}
    by_code = {}
    for group in doc.get("content", {}).get("nhom_san_pham", []):
        for item in group.get("san_pham") or []:
            code = item.get("san_pham_id")
            if code:
                by_code[code] = item
    return by_code


def _build_commercial(assignments, rules, shared_by_code):
    """'old' state cho từng shop_id: duyệt trước đó + trường CURATED hiện có.

    Nguồn duy nhất bây giờ là hai file runtime — machine cho trạng thái duyệt,
    shared cho trường kỹ thuật do người soạn thêm (vd dose_rules).
    """
    commercial = {}
    previously_approved = set()
    for shop_id, assignment in assignments.items():
        approval = assignment.get("approval") or {}
        old = {}
        try:
            code = canonical_product_code(rules[shop_id]["website_product_ids"])
        except CatalogLayerError:
            code = None
        shared_record = shared_by_code.get(code, {}) if code else {}
        overrides = assignment.get("profile_overrides") or {}
        for field in CURATED_SHARED_FIELDS:
            if shared_record.get(field):
                old[field] = shared_record[field]
        for field in CURATED_PROFILE_OVERRIDE_FIELDS:
            if overrides.get(field):
                old[field] = overrides[field]
        for field in CURATED_MACHINE_FIELDS_NOT_YET_SUPPORTED:
            if assignment.get(field):
                old[field] = assignment[field]
        if approval.get("approved_by"):
            old["approved_by"] = approval["approved_by"]
        if approval.get("approved_at"):
            old["approved_at"] = approval["approved_at"]
        legacy_id = shared_record.get("legacy_profile_id") or shared_record.get("san_pham_id")
        if legacy_id:
            old["legacy_profile_id"] = legacy_id
        commercial[shop_id] = old
        if assignment.get("approved_for_live") is True:
            previously_approved.add(shop_id)
    return commercial, previously_approved


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True)
    parser.add_argument("--approve-high", action="store_true",
                        help="duyet cac cap mapping high, khong combo, khong canh bao")
    parser.add_argument("--machine", default=str(MACHINE_CATALOG_MAPPING_PATH),
                        help="đường dẫn machine/catalog_mapping.json (mặc định: runtime thật)")
    parser.add_argument("--shared", default=str(SHARED_PRODUCT_KNOWLEDGE_PATH),
                        help="đường dẫn shared/product_knowledge.json (mặc định: runtime thật)")
    parser.add_argument("--source", default=None,
                        help="đường dẫn snapshot company_website_profiles (mặc định: theo --date trong data/sources/)")
    parser.add_argument("--live", default=None,
                        help="đường dẫn live_products_current.json (mặc định: runtime thật)")
    args = parser.parse_args()
    data = ROOT / "data"
    source_path = Path(args.source) if args.source else (
        data / "sources" / f"company_website_profiles_{args.date}.json")
    live_path = Path(args.live) if args.live else (data / "live_products_current.json")
    machine_path = Path(args.machine)
    shared_path = Path(args.shared)

    source = read(source_path)
    profiles = {item["website_product_id"]: item for item in source["products"]}
    live_products = read(live_path)["products"]
    live = {str(item["product_id"]): item for item in live_products}

    machine_doc = read_json(machine_path)
    assignments = machine_doc.get("assignments") or {}
    rules = _rules_from_machine(assignments)

    missing = sorted(set(live) - set(rules))
    extra = sorted(set(rules) - set(live))
    if missing or extra:
        raise RuntimeError(f"Ánh xạ không phủ catalog: missing={missing}, extra={extra}")
    bad_refs = sorted({wid for item in rules.values() for wid in item["website_product_ids"]
                       if wid not in profiles})
    if bad_refs:
        raise RuntimeError(f"Website product id không tồn tại: {bad_refs}")
    incomplete = [item["website_product_id"] for item in profiles.values()
                  if not item["ingredients"] or not item["uses"] or not item["directions"]]
    if incomplete:
        raise RuntimeError(f"Hồ sơ website chưa đủ trường cốt lõi: {incomplete}")

    shared_by_code = _current_shared_by_code(shared_path)
    commercial, previously_approved = _build_commercial(assignments, rules, shared_by_code)

    official_records = []
    for website_id in sorted(profiles, key=int):
        record = base_fields(profiles[website_id])
        record.update({
            "san_pham_id": f"WEB-{website_id}",
            "approved_for_live": False,
            "catalog_status": "official_profile_not_directly_bound",
        })
        official_records.append(record)
    runtime_records = [
        live_record(live[shop_id], rules[shop_id], profiles, commercial,
                    args.approve_high, previously_approved)
        for shop_id in live
    ]

    catalog = {
        "nhom_san_pham": [
            {
                "nhom": "E. Thư viện đầy đủ từ website chính thức",
                "managed_by": OFFICIAL_GROUP_KEY,
                "source_snapshot": _relative_label(source_path),
                "san_pham": official_records,
            },
            {
                "nhom": "F. Hồ sơ chính thức theo từng SKU LIVE",
                "managed_by": LIVE_GROUP_KEY,
                "mapping_file": _relative_label(machine_path),
                "san_pham": runtime_records,
            },
        ],
    }
    live_snapshot = {"products": live_products}
    mapping_document = {"rules": list(rules.values())}
    try:
        shared, machine, report = build_catalog_layers(catalog, live_snapshot, mapping_document)
    except CatalogLayerError as exc:
        # `[T5]` build_catalog_layers() tự chặn khi có assignment đang duyệt
        # mà vẫn mơ hồ (xem catalog_layers.find_ambiguous_approved_assignments).
        # In báo cáo gọn thay vì để traceback thô, và không ghi gì cả.
        print(f"TỪ CHỐI XUẤT BẢN — {exc}")
        return 1

    # `[T4]` Chốt tự kiểm: mọi trường quy cách/cảnh báo/giá do live_record()
    # tính ra cho một SKU phải có mặt nguyên vẹn trong profile_overrides của
    # đúng SKU đó sau khi build_catalog_layers() chạy xong. Trước T4, các
    # trường này không có chỗ chứa và bị build_catalog_layers() âm thầm bỏ —
    # giữ lại chốt này để nếu sau này có ai thêm trường mới vào live_record()
    # mà quên thêm vào catalog_layers.PROFILE_OVERRIDE_FIELDS, công cụ TỪ
    # CHỐI xuất bản thay vì lặng lẽ mất cảnh báo quy cách.
    lost = []
    for item in runtime_records:
        shop_id = item["shop_product_ids"][0]
        overrides = machine["assignments"].get(shop_id, {}).get("profile_overrides", {})
        missing_here = [field for field in UNSAFE_TO_DROP_FIELDS
                        if item.get(field) and overrides.get(field) != item.get(field)]
        if missing_here:
            lost.append((shop_id, item["ten_san_pham"], missing_here))
    if lost:
        print("TỪ CHỐI XUẤT BẢN — các trường sau bị mất khi ghép vào"
              f" {machine_path.name} (kiểm tra lại catalog_layers.PROFILE_OVERRIDE_FIELDS):")
        for shop_id, name, fields in lost:
            print(f"  {shop_id}  {name[:50]}  mất: {', '.join(fields)}")
        print("\nKhông ghi gì cả.")
        return 1

    backup = create_operational_backup()
    atomic_write_json(shared_path, shared)
    atomic_write_json(machine_path, machine)

    medium = [shop_id for shop_id, rule in rules.items() if rule["confidence"] != "high"]
    combos = [shop_id for shop_id, rule in rules.items() if rule.get("combo")]
    approved = [item for item in runtime_records if item["approved_for_live"]]
    waiting = [item for item in runtime_records if not item["approved_for_live"]]
    print(f"\nHo so website: {len(official_records)}"
          f" · ho so theo SKU LIVE: {len(runtime_records)}"
          f" · combo: {len(combos)} · mapping medium: {len(medium)}")
    print(f"Da duyet cho len live: {len(approved)}"
          f" · CHO NGUOI DUYET: {len(waiting)}")
    print(f"Sao lưu đã xác minh: {backup['backup_dir']}")
    print(f"Đã publish: {shared_path}")
    print(f"Đã publish: {machine_path}")
    if waiting:
        print("\nChua duyet (AI se im lang khi khach hoi ky thuat ve cac SKU nay):")
        for item in waiting:
            flags = []
            if item.get("is_combo"):
                flags.append("combo")
            if item.get("mapping_confidence") != "high":
                flags.append("mapping " + item["mapping_confidence"])
            if item.get("source_discrepancy"):
                flags.append("sai khac nguon")
            note = (" [" + ", ".join(flags) + "]") if flags else ""
            print(f"  {item['shop_product_ids'][0]}  {item['ten_san_pham'][:58]}{note}")
        print("\nDuyet cac cap CHAC CHAN bang: --approve-high"
              " (bo qua combo va cac cap co canh bao)")
        print("Duyet tung cai bang trang http://127.0.0.1:8793/mapping")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
