"""Kiem doc lap tung anh xa SKU LIVE <-> ho so website, truoc khi cho len live.

Khong tin san file mapping. Voi moi cap, doi chieu TU RIENG cua ten san pham
tren website voi tieu de SKU dang ban:

  - tu rieng  = tu chi xuat hien o 1-2 ten san pham trong toan bo 41 ho so
                (vd "furagin", "rydoxin", "trichoderma", "tonic")
  - dat       = co it nhat MOT tu rieng cua ho so xuat hien trong tieu de SKU
  - can nguoi = khong co tu rieng nao trung, hoac quy cach lech

Cap khong dat se duoc ghi `review_note` vao mapping, nho vay lenh
--approve-high cua sync_official_catalog.py tu dong bo qua.

`[SUA 13/09/2026]` Truoc day cong cu nay doc/ghi data/company_catalog_mapping.json
(file legacy). Runtime chi doc data/machine/catalog_mapping.json nen ket qua
kiem cua ban cu khong bao gio anh huong toi app dang chay. Da tro sang dung
file runtime; website_product_ids duoc doi nguoc tu product_code.

    python tools/verify_catalog_mapping.py --date 2026-09-11
    python tools/verify_catalog_mapping.py --date 2026-09-11 --write-notes
"""
import argparse
import json
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from catalog_layers import (  # noqa: E402
    CatalogLayerError, atomic_write_json, read_json, website_ids_from_product_code,
)
from config import MACHINE_CATALOG_MAPPING_PATH  # noqa: E402
from console_io import force_utf8_console  # noqa: E402

force_utf8_console()

ROOT = Path(__file__).resolve().parent.parent
GENERIC = {
    "san", "pham", "phan", "bon", "thuoc", "nong", "duoc", "toan", "cau",
    "cay", "trong", "la", "re", "hoa", "qua", "trai", "cu", "hat", "dat",
    "sieu", "cuc", "manh", "chuyen", "dac", "tri", "giup", "cho", "va",
    "voi", "cac", "moi", "dang", "sua", "nuoc", "combo", "goi", "chai",
    "can", "tui", "hop", "ml", "kg", "gr", "lit", "gram", "cong", "nghe",
    "bao", "hanh", "nha", "xuat", "ngoai", "troi", "meocham", "npk",
    "xanh", "bong", "chong", "rung", "day", "lui", "nam", "benh", "vi",
    "khuan", "gay", "hai", "phuc", "hoi", "bung", "vot", "dot", "chuoi",
    "tot", "deu", "mau", "dep", "nang", "ki", "sach", "tay", "rua", "vuon",
    "sat", "dinh", "duong", "thiet", "yeu", "an", "o", "1", "2", "3", "4", "5",
}


def fold(value):
    text = unicodedata.normalize(
        "NFD", str(value or "").casefold().replace("đ", "d"))
    return "".join(ch for ch in text if unicodedata.category(ch) != "Mn")


_SIZE_TOKEN = re.compile(r"^\d+(?:[.,]\d+)?(?:ml|lit|kg|gr|g|l)?$")


def tokens(value):
    # Bo token chi la con so/quy cach: quy cach da co phep kiem rieng,
    # neu de lot vao day thi "5kg" se thanh "tu rieng" va bao dong gia.
    return {item for item in re.findall(r"[a-z0-9]+", fold(value))
            if len(item) >= 2 and item not in GENERIC
            and not _SIZE_TOKEN.match(item)}


def packages_of(value):
    found = set()
    for amount, unit in re.findall(
        r"(\d+(?:[.,]\d+)?)\s*(ml|lit|lít|l|kg|gr|g)\b", fold(value), re.I
    ):
        unit = {"l": "lit", "lít": "lit", "g": "gr"}.get(unit, unit)
        amount = amount.replace(",", ".")
        if "." in amount:  # chi cat so 0 thua o phan thap phan, 500 KHONG thanh 5
            amount = amount.rstrip("0").rstrip(".")
        found.add(f"{amount}{unit}")
    return found


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True)
    parser.add_argument("--write-notes", action="store_true",
                        help="ghi review_note vao mapping_audit cho cap can nguoi xem")
    parser.add_argument("--machine", default=str(MACHINE_CATALOG_MAPPING_PATH),
                        help="đường dẫn machine/catalog_mapping.json (mặc định: runtime thật)")
    args = parser.parse_args()

    data = ROOT / "data"
    profiles = {
        item["website_product_id"]: item
        for item in json.loads((data / "sources"
                                / f"company_website_profiles_{args.date}.json"
                                ).read_text(encoding="utf-8-sig"))["products"]
    }
    mapping_path = Path(args.machine)
    mapping = read_json(mapping_path)
    assignments = mapping.get("assignments") or {}

    # Tu nao chi co o it ho so thi moi la tu rieng.
    spread = Counter()
    for item in profiles.values():
        for token in tokens(item["name"]):
            spread[token] += 1
    distinctive = {token for token, count in spread.items() if count <= 2}

    passed, flagged = [], []
    for shop_id, assignment in assignments.items():
        title = (assignment.get("listing") or {}).get("title", "")
        audit = assignment.get("mapping_audit") or {}
        try:
            website_ids = website_ids_from_product_code(assignment.get("product_code"))
        except CatalogLayerError as exc:
            flagged.append((shop_id, str(exc), title))
            continue
        sku_tokens = tokens(title)
        sku_packages = packages_of(title)
        reasons, names = [], []
        for website_id in website_ids:
            profile = profiles.get(website_id)
            if not profile:
                reasons.append(f"web {website_id} không tồn tại")
                continue
            names.append(profile["name"])
            own = tokens(profile["name"]) & distinctive
            shared = own & sku_tokens
            if own and not shared:
                reasons.append(
                    f"không từ riêng nào của \"{profile['name'][:34]}\""
                    f" xuất hiện trong tiêu đề SKU (từ riêng: {sorted(own)})")
            web_packages = packages_of(" ".join(profile.get("packages") or []))
            if web_packages and sku_packages and not (web_packages & sku_packages):
                reasons.append(
                    f"quy cách lệch: SKU {sorted(sku_packages)}"
                    f" vs web {sorted(web_packages)}")
        if audit.get("combo"):
            reasons.append("combo — liều của từng thành phần, phải người xem")
        if audit.get("confidence") != "high":
            reasons.append(f"mapping tự đánh giá {audit.get('confidence') or 'không rõ'}")
        if audit.get("review_note"):
            reasons.append("đã có cảnh báo sẵn trong mapping")
        if reasons:
            flagged.append((shop_id, " · ".join(reasons), title))
            if args.write_notes:
                existing = audit.get("review_note", "")
                note = "Kiểm độc lập: " + " · ".join(reasons)
                audit["review_note"] = (existing + " || " + note) if existing else note
                assignment["mapping_audit"] = audit
        else:
            passed.append((shop_id, names[0] if names else "", title))

    print(f"Đối chiếu {len(assignments)} ánh xạ · từ riêng nhận diện được:"
          f" {len(distinctive)}\n")
    print(f"ĐẠT — có từ riêng trùng, quy cách khớp ({len(passed)}):")
    for shop_id, name, title in passed:
        print(f"  {shop_id}  {name[:46]:<46} <- {title[:44]}")
    print(f"\nCẦN NGƯỜI XEM ({len(flagged)}):")
    for shop_id, reason, title in flagged:
        print(f"  {shop_id}  {title[:44]}")
        print(f"      {reason}")
    if args.write_notes:
        atomic_write_json(mapping_path, mapping)
        print(f"\nĐã ghi review_note vào {mapping_path.name} cho"
              f" {len(flagged)} cặp — lệnh --approve-high sẽ tự bỏ qua chúng.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
