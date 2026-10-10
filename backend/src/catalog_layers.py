"""Tách kiến thức dùng chung khỏi trạng thái vận hành theo từng máy.

Module này chỉ hiểu dữ liệu và kiểm tra bất biến. Nó không biết HTTP hay UI,
để migration có thể chạy thử, được test, rồi mới được runtime sử dụng.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


SHARED_SCHEMA_VERSION = 1
MACHINE_SCHEMA_VERSION = 1

# Những trường này thuộc một lần bán/giỏ hàng hoặc lịch sử mapping cũ. Chúng
# tuyệt đối không được xuất hiện trong gói kiến thức có thể mang sang máy khác.
MACHINE_ONLY_FIELDS = frozenset({
    "shop_product_ids", "shop_sku_ids", "excluded_shop_product_ids",
    "excluded_shop_sku_ids", "seller_skus", "listing_aliases",
    "quy_cach_dong_goi", "gia_goc_duyet", "live_promotion",
    "listing_source", "canh_bao_quy_cach", "source_discrepancy",
    "variant_note",
    "approved_for_live", "approved_by", "approved_at", "cho_nguoi_duyet",
    "mapping_confidence", "superseded_by_official_sync",
})

# Các khác biệt này được tạo ra bởi việc dựng một listing LIVE từ hồ sơ WEB:
# nguồn là list thay vì URL đơn, nhãn nguồn khác, và tên có thể gắn dung tích.
# Chúng không phải nội dung kỹ thuật và không được dùng để gộp/đánh giá xung
# đột. Mọi trường dùng chung khác mà khác nhau sẽ chặn migration.
EXPECTED_DERIVATION_DIFFERENCES = frozenset({
    "san_pham_id", "ten_san_pham", "website_product_ids", "nguon_chinh_thuc",
    "source_kind", "is_combo",
})

_WEB_CODE = re.compile(r"^WEB-(\d+)$")


class CatalogLayerError(ValueError):
    """Dữ liệu không đủ an toàn để xuất bản lớp mới."""


def website_ids_from_product_code(product_code: str) -> list[str]:
    """Đảo ngược ``canonical_product_code``: mã bền -> danh sách website id.

    Cần cho các công cụ đọc ``machine/catalog_mapping.json`` (chỉ có
    ``product_code``) mà vẫn phải tra lại đúng hồ sơ website nguồn, ví dụ
    "WEB-150" -> ["150"], "COMBO-WEB-10-WEB-2" -> ["10", "2"].
    """
    code = str(product_code or "").strip()
    if not code:
        raise CatalogLayerError("product_code rỗng, không đảo ngược được")
    prefix = "COMBO-" if code.startswith("COMBO-") else ""
    body = code[len(prefix):]
    parts = body.split("-WEB-") if prefix else [body]
    ids = []
    for index, part in enumerate(parts):
        # Phần đầu tiên giữ nguyên tiền tố "WEB-"; các phần sau đã bị tách bởi
        # "-WEB-" nên chỉ còn số.
        if index == 0:
            match = _WEB_CODE.match(part)
            if not match:
                raise CatalogLayerError(f"product_code không hợp lệ: {product_code!r}")
            ids.append(match.group(1))
        elif part.isdigit():
            ids.append(part)
        else:
            raise CatalogLayerError(f"product_code không hợp lệ: {product_code!r}")
    return ids


def canonical_product_code(website_product_ids: list[str] | tuple[str, ...]) -> str:
    """Trả mã không phụ thuộc TikTok từ một hoặc nhiều mã website.

    Combo được sắp theo giá trị số rồi mới theo chuỗi, để ``[10, 2]`` không
    tùy thuộc thứ tự nguồn và luôn tạo cùng một mã trên mọi máy.
    """
    ids = [str(item).strip() for item in website_product_ids or [] if str(item).strip()]
    if not ids:
        raise CatalogLayerError("Không có website_product_id để tạo mã bền")
    if len(set(ids)) != len(ids):
        raise CatalogLayerError(f"website_product_id bị lặp: {ids}")
    ids.sort(key=lambda item: (0, int(item)) if item.isdigit() else (1, item))
    codes = [f"WEB-{item}" for item in ids]
    return codes[0] if len(codes) == 1 else "COMBO-" + "-".join(codes)


def read_json(path: Path) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CatalogLayerError(f"Không đọc được JSON {Path(path).name}: {exc}") from exc
    if not isinstance(value, dict):
        raise CatalogLayerError(f"JSON gốc phải là object: {Path(path).name}")
    return value


def json_sha256(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _records(catalog: dict):
    for group in catalog.get("nhom_san_pham", []):
        if not isinstance(group, dict):
            continue
        for record in group.get("san_pham", []):
            if isinstance(record, dict) and record.get("san_pham_id"):
                yield group, record


def _scrub_shared(record: dict) -> dict:
    """Copy record, loại toàn bộ trường máy và không đổi dữ liệu kỹ thuật."""
    result = {
        key: copy.deepcopy(value)
        for key, value in record.items()
        if key not in MACHINE_ONLY_FIELDS
    }
    # Các mã LIVE-* là mã của SKU TikTok, không được biến thành mã kiến thức.
    if str(result.get("san_pham_id", "")).startswith("LIVE-"):
        result.pop("san_pham_id", None)
    return result


def _machine_listing(live: dict) -> dict:
    """Snapshot thương mại tối thiểu; không sao chép hồ sơ kỹ thuật."""
    keep = (
        "title", "original_price", "price", "sale_price", "promotion",
        "bag_index", "sku_id", "skus", "stock_num", "is_pinned",
    )
    return {key: copy.deepcopy(live[key]) for key in keep if key in live}


def _mapping_audit(rule: dict) -> dict:
    keep = (
        "confidence", "combo", "combo_da_duyet", "xac_nhan_boi",
        "xac_nhan_ngay", "xac_nhan_loi_nguoi_duyet", "variant_note",
        "review_note",
    )
    return {key: copy.deepcopy(rule[key]) for key in keep if key in rule}


# `[T4]` Các trường này do live_record() tự tính ra CHO TỪNG SKU (không phải
# cho mã kỹ thuật dùng chung) — quy cách/cảnh báo/giá riêng của chai đang
# bán, không phải công thức. Chúng thuộc MACHINE_ONLY_FIELDS nên bị
# _scrub_shared() loại khỏi shared; phải giữ lại ở đây, nếu không AI sẽ
# không còn biết quy cách chai khách đang cầm khác quy cách trên nhãn web.
PROFILE_OVERRIDE_FIELDS = (
    "canh_bao_quy_cach", "quy_cach_dong_goi", "gia_goc_duyet",
    "live_promotion", "listing_source", "source_discrepancy",
)


def _profile_overrides(live_record: dict) -> dict:
    return {key: copy.deepcopy(live_record[key])
            for key in PROFILE_OVERRIDE_FIELDS if live_record.get(key)}


def _merge_missing(target: dict, source: dict) -> None:
    """Chỉ bổ sung metadata kỹ thuật không có trong hồ sơ website.

    Không ghi đè dữ liệu website vì đó là nguồn chính thức hiện tại. Các
    trường curated như ``dose_rules`` không có ở web sẽ được giữ nguyên.
    """
    for key, value in source.items():
        if key in MACHINE_ONLY_FIELDS or key in {"san_pham_id", "legacy_profile_id"}:
            continue
        if key not in target or target[key] in (None, "", [], {}):
            target[key] = copy.deepcopy(value)


def _assert_compatible_shared_data(code: str, existing: dict, candidate: dict) -> None:
    """Từ chối gộp hai hồ sơ có dữ liệu kỹ thuật khác nhau.

    Điều này đặc biệt bảo vệ hai SKU có cùng website_product_id nhưng quy cách
    hoặc listing khác nhau: listing được tách sang machine, còn công thức phải
    thực sự giống nhau mới được dùng chung một mã WEB.
    """
    differences = []
    for key in sorted(set(existing) & set(candidate)):
        if key in EXPECTED_DERIVATION_DIFFERENCES:
            continue
        if existing[key] != candidate[key]:
            differences.append(key)
    if differences:
        raise CatalogLayerError(
            f"Mã {code} có dữ liệu kỹ thuật mâu thuẫn: {', '.join(differences)}"
        )


def ambiguous_reasons(assignment: dict) -> list[str]:
    """`[T5]` Vì sao một assignment ĐANG DUYỆT lại không đáng tin.

    Mơ hồ mà đang duyệt là trạng thái nguy hiểm nhất: AI sẽ trả lời kỹ thuật
    cho một SKU chưa ai xác nhận chắc chắn. Mơ hồ mà KHÔNG duyệt thì không
    sao — đó là bị chặn đúng cách. Chỉ gọi hàm này khi
    ``approved_for_live is True``.
    """
    audit = assignment.get("mapping_audit") or {}
    reasons = []
    if audit.get("review_note"):
        reasons.append(f"còn review_note: {audit['review_note']!r}")
    if audit.get("combo") and not audit.get("combo_da_duyet"):
        reasons.append("combo chưa được xác nhận combo_da_duyet")
    if audit.get("confidence") != "high":
        reasons.append(f"mapping_confidence = {audit.get('confidence')!r}, không phải 'high'")
    return reasons


def find_ambiguous_approved_assignments(machine: dict) -> dict[str, list[str]]:
    """Quét mọi assignment ĐANG DUYỆT mà vẫn còn mơ hồ chưa được xử lý.

    Trả `{shop_id: [lý do, ...]}` — rỗng nghĩa là sạch, publish được.
    """
    found = {}
    for shop_id, assignment in (machine.get("assignments") or {}).items():
        if not isinstance(assignment, dict) or assignment.get("approved_for_live") is not True:
            continue
        reasons = ambiguous_reasons(assignment)
        if reasons:
            found[shop_id] = reasons
    return found


def _identity(record: dict) -> str:
    return str(record.get("san_pham_id", "")).strip()


def build_catalog_layers(
    legacy_catalog: dict,
    live_snapshot: dict,
    mapping_document: dict,
    *,
    published_at: datetime | None = None,
) -> tuple[dict, dict, dict]:
    """Tạo payload shared/machine hoàn toàn trong bộ nhớ.

    Hàm không ghi file. Một lỗi kiểm tra dừng toàn bộ để người vận hành không
    bao giờ nhận một nửa migration.
    """
    published_at = published_at or datetime.now(timezone.utc)
    if published_at.tzinfo is None:
        raise CatalogLayerError("published_at phải có timezone")
    live_products = live_snapshot.get("products")
    rules = mapping_document.get("rules")
    if not isinstance(live_products, list) or not isinstance(rules, list):
        raise CatalogLayerError("Thiếu products hoặc rules trong dữ liệu nguồn")

    live_by_id = {}
    for item in live_products:
        product_id = str((item or {}).get("product_id", "")).strip()
        if not product_id or product_id in live_by_id:
            raise CatalogLayerError(f"SKU LIVE trống hoặc trùng: {product_id!r}")
        live_by_id[product_id] = item
    rule_by_id = {}
    for rule in rules:
        product_id = str((rule or {}).get("shop_product_id", "")).strip()
        if not product_id or product_id in rule_by_id:
            raise CatalogLayerError(f"Rule shop_product_id trống hoặc trùng: {product_id!r}")
        rule_by_id[product_id] = rule
    missing = sorted(set(live_by_id) - set(rule_by_id))
    extra = sorted(set(rule_by_id) - set(live_by_id))
    if missing or extra:
        raise CatalogLayerError(f"Rule không phủ đúng catalog: missing={missing}, extra={extra}")

    records = list(_records(legacy_catalog))
    by_id = {_identity(record): record for _, record in records}
    if len(by_id) != len(records):
        raise CatalogLayerError("san_pham_id trùng trong catalog cũ")

    # Các LIVE record là trạng thái kỹ thuật thật đang được dùng. Một SKU LIVE
    # phải có đúng một record như vậy; nếu không thì migration không được đoán.
    live_records = {}
    for _, record in records:
        for shop_id in record.get("shop_product_ids") or []:
            shop_id = str(shop_id).strip()
            if str(record.get("san_pham_id", "")).startswith("LIVE-"):
                if shop_id in live_records:
                    raise CatalogLayerError(f"Hai LIVE record cùng giữ SKU {shop_id}")
                live_records[shop_id] = record
    absent_live_records = sorted(set(live_by_id) - set(live_records))
    if absent_live_records:
        raise CatalogLayerError(
            "Không có LIVE record kỹ thuật cho SKU: " + ", ".join(absent_live_records)
        )

    # Bắt đầu từ thư viện WEB hiện có: tên, URL và dữ liệu website là chuẩn
    # dùng chung. Combo không có WEB record nên dùng LIVE record đã scrub.
    shared_by_code = {}
    source_ids = defaultdict(list)
    for _, record in records:
        record_id = _identity(record)
        match = _WEB_CODE.match(record_id)
        if match:
            shared_by_code[record_id] = _scrub_shared(record)
            source_ids[record_id].append(record_id)

    assignments = {}
    legacy_profile_to_code = {}
    for shop_id in sorted(live_by_id):
        rule = rule_by_id[shop_id]
        website_ids = rule.get("website_product_ids") or []
        code = canonical_product_code(website_ids)
        live_record = live_records[shop_id]
        shared = shared_by_code.get(code)
        if shared is None:
            shared = _scrub_shared(live_record)
            shared["san_pham_id"] = code
            shared["website_product_ids"] = sorted(str(item) for item in website_ids)
            # Combo không có một trang WEB đơn lẻ để làm tên chuẩn. Tiêu đề
            # LIVE là SEO theo máy, nên dùng nhãn trung tính thay vì làm rò
            # tiêu đề đó sang gói dùng chung.
            if code.startswith("COMBO-"):
                ordered_ids = sorted(
                    (str(item) for item in website_ids),
                    key=lambda item: (0, int(item)) if item.isdigit() else (1, item),
                )
                shared["ten_san_pham"] = "Combo " + " + ".join(
                    f"WEB-{item}" for item in ordered_ids
                )
            shared_by_code[code] = shared
        else:
            candidate = _scrub_shared(live_record)
            _assert_compatible_shared_data(code, shared, candidate)
            _merge_missing(shared, candidate)
        source_ids[code].append(_identity(live_record))

        legacy_id = str(live_record.get("legacy_profile_id", "")).strip()
        if legacy_id:
            old_code = legacy_profile_to_code.setdefault(legacy_id, code)
            if old_code != code:
                raise CatalogLayerError(
                    f"legacy_profile_id {legacy_id} trỏ tới hai mã: {old_code}, {code}"
                )
        assignments[shop_id] = {
            "product_code": code,
            "approved_for_live": live_record.get("approved_for_live") is True,
            "approval": {
                key: copy.deepcopy(live_record[key])
                for key in ("approved_by", "approved_at") if live_record.get(key)
            },
            "listing": _machine_listing(live_by_id[shop_id]),
            "mapping_audit": _mapping_audit(rule),
            "profile_overrides": _profile_overrides(live_record),
        }

    # Merge kỹ thuật curated từ năm legacy profile đã được LIVE record tham
    # chiếu. Những record SP khác được giữ ở trạng thái lịch sử, không còn SKU.
    # Nhờ vậy migration không âm thầm làm mất công soạn cũ.
    migrated_legacy_ids = set()
    code_to_legacy: dict[str, str] = {}
    for legacy_id, code in legacy_profile_to_code.items():
        # Ghi lại ĐÚNG hồ sơ legacy đã ràng buộc với mã này. Thiếu dòng này
        # thì lớp shared mất đường nối tới approved_live_profiles.json:
        # _merge_missing() cố ý bỏ qua `legacy_profile_id`, nên bằng chứng
        # ràng buộc bị vứt đi ngay tại bước gộp. Hậu quả quan sát được trên
        # máy vận hành (F13): WEB-1236/WEB-958/WEB-1212 mất liên kết, và
        # knowledge.context() không còn nạp được `ho_so_da_duyet` —
        # AI trả lời liều dùng mà không có hồ sơ kỹ thuật đã duyệt.
        # Ràng buộc lấy thẳng từ live record đã duyệt, KHÔNG suy đoán theo tên.
        da_gan = code_to_legacy.setdefault(code, legacy_id)
        if da_gan != legacy_id:
            raise CatalogLayerError(
                f"Mã {code} bị hai hồ sơ legacy tranh nhau: {da_gan}, {legacy_id}"
            )
        shared_by_code[code]["legacy_profile_id"] = legacy_id
        legacy = by_id.get(legacy_id)
        if legacy:
            _merge_missing(shared_by_code[code], _scrub_shared(legacy))
            migrated_legacy_ids.add(legacy_id)
            source_ids[code].append(legacy_id)

    legacy_unbound = []
    for _group, record in records:
        record_id = _identity(record)
        if record_id.startswith("LIVE-") or _WEB_CODE.match(record_id) or record_id in migrated_legacy_ids:
            continue
        preserved = _scrub_shared(record)
        preserved["san_pham_id"] = f"LEGACY-{record_id}"
        preserved["catalog_status"] = "legacy_unbound_not_for_live"
        legacy_unbound.append(preserved)

    codes = sorted(shared_by_code, key=lambda item: (not item.startswith("WEB-"), item))
    shared_products = [shared_by_code[code] for code in codes]
    # Bất biến an toàn: bản shared không được rò bất kỳ khóa máy nào, kể cả
    # nested accidental (ví dụ một field mới được nhét vào object con).
    forbidden_keys = set()
    def scan(value):
        if isinstance(value, dict):
            forbidden_keys.update(set(value) & MACHINE_ONLY_FIELDS)
            for child in value.values():
                scan(child)
        elif isinstance(value, list):
            for child in value:
                scan(child)
    scan(shared_products)
    scan(legacy_unbound)
    if forbidden_keys:
        raise CatalogLayerError("Shared còn trường theo máy: " + ", ".join(sorted(forbidden_keys)))

    content = {
        "nhom_san_pham": [
            {
                "nhom": "Thư viện kỹ thuật dùng chung",
                "managed_by": "shared_product_knowledge",
                "san_pham": shared_products,
            },
            {
                "nhom": "Hồ sơ lịch sử chưa gắn SKU",
                "managed_by": "legacy_unbound",
                "san_pham": legacy_unbound,
            },
        ],
    }
    shared = {
        "schema_version": SHARED_SCHEMA_VERSION,
        "published_at": published_at.astimezone(timezone.utc).isoformat(),
        "publisher": "Kid TE migration G02",
        "content": content,
    }
    shared["content_sha256"] = json_sha256(content)
    machine = {
        "schema_version": MACHINE_SCHEMA_VERSION,
        "created_at": published_at.astimezone(timezone.utc).isoformat(),
        "machine_scope": "local_only_do_not_sync",
        "assignments": assignments,
    }
    # `[T5]` Cổng cuối trước khi publish: một SKU đang duyệt mà vẫn còn mơ hồ
    # (review_note chưa xử lý, combo chưa xác nhận, hoặc mapping không phải
    # confidence cao) là lỗi nguy hiểm hơn hẳn một SKU bị chặn đúng cách — AI
    # sẽ trả lời kỹ thuật cho thứ chưa ai chắc chắn là đúng. Dừng cứng, kèm
    # báo cáo đủ để người vận hành sửa ngay, không publish một nửa.
    ambiguous = find_ambiguous_approved_assignments(machine)
    if ambiguous:
        lines = [
            f"  {shop_id} (mã {assignments[shop_id]['product_code']}): "
            + "; ".join(reasons)
            for shop_id, reasons in sorted(ambiguous.items())
        ]
        raise CatalogLayerError(
            "TỪ CHỐI PUBLISH — còn assignment ĐANG DUYỆT nhưng vẫn mơ hồ, "
            "chưa được đánh dấu chặn đúng cách:\n" + "\n".join(lines)
            + "\nSửa mapping_audit (xoá review_note đã xử lý, xác nhận "
            "combo_da_duyet, hoặc nâng confidence) hoặc hạ approved_for_live "
            "về false rồi publish lại."
        )
    report = {
        "legacy_record_count": len(records),
        "legacy_shop_product_id_references": sum(
            len(record.get("shop_product_ids") or []) for _, record in records
        ),
        "live_sku_count": len(live_by_id),
        "machine_assignment_count": len(assignments),
        "approved_assignment_count": sum(
            item["approved_for_live"] for item in assignments.values()
        ),
        "unapproved_assignment_count": sum(
            not item["approved_for_live"] for item in assignments.values()
        ),
        "shared_product_count": len(shared_products),
        "legacy_unbound_count": len(legacy_unbound),
        "shared_shop_product_id_references": 0,
        "codes_with_multiple_legacy_sources": {
            code: ids for code, ids in sorted(source_ids.items()) if len(ids) > 2
        },
    }
    return shared, machine, report


def atomic_write_json(path: Path, value: dict) -> None:
    """Ghi JSON qua file tạm cùng volume rồi thay nguyên tử."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        # Parse chính file staging trước khi publish, không tin object trong RAM.
        read_json(temporary)
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()
