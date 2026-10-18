from __future__ import annotations

import json
import pickle
import re
import threading
import time
import unicodedata
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

from config import PLUGINS_DIR
from processor_loader import load_processor


# "giá trị dinh dưỡng" không phải hỏi giá.
_PRICE = re.compile(r"\b(giá(?!\s+trị)|nhiêu tiền|bao nhiêu tiền|giá gốc|giá bán)\b", re.I)
_DOSE = re.compile(r"\b(liều|pha|bao nhiêu ml|bình \d+|phuy|cách dùng|sử dụng sao)\b", re.I)
# "combo" KHÔNG nằm đây: shop bán 6 SKU tên sẵn là "Combo ...", nên hỏi
# "combo này pha bình 16 lít sao" là hỏi liều, không phải hỏi khuyến mãi.
# Để nguyên thì mọi câu có chữ combo đều bị chặn vì thiếu chuong_trinh.
# "mua 2 chai" là tín hiệu MUA, không phải hỏi khuyến mãi (rà soát 17/10 P1).
_PROMO = re.compile(r"\b(khuyến mãi|ưu đãi|tặng|quà|chương trình|giảm giá|được giảm|bớt giá|"
                    r"mua \d+[^.?!]{0,24}(?:giảm|bớt|rẻ hơn))\b", re.I)
# Bản KHÔNG DẤU chỉ dùng cho chữ khách gõ không dấu, và được thiết kế để không
# đụng chữ có dấu khác: "tang" có thể là "tăng" nên chỉ nhận cụm rõ nghĩa quà;
# "gia" không nhận "gia đình"/"giá trị".
_PRICE_PLAIN = re.compile(r"\b(gia(?!\s+(?:dinh|tri|su|vi|han|lai|hang|nhap))|nhieu tien|bao nhieu tien)\b")
_PROMO_PLAIN = re.compile(r"\b(khuyen mai|uu dai|qua tang|tang kem|tang qua|tang gi|co qua|km|giam gia|duoc giam|"
                          r"mua \d+[^.?!]{0,24}(?:giam|bot|re hon))\b")
_MIX = re.compile(
    r"\b(pha chung|trộn|phun chung|phun chúng|chung với|cùng với|kết hợp)\b",
    re.I,
)
_RECOMMEND = re.compile(r"\b(có loại nào|nên dùng|dùng gì|loại gì)\b", re.I)
_AFTER_PLANTING = re.compile(
    r"\b(sau (?:khi )?trồng|mới trồng|khi nào.*dùng|"
    r"(?:trồng|xuống giống).{0,30}(?:bao nhiêu|mấy) ngày|"
    r"(?:bao nhiêu|mấy) ngày.{0,30}(?:sau trồng|sau khi trồng))\b",
    re.I,
)
_TECH = re.compile(
    r"\b(công dụng|trị|diệt|dùng cho|phun|tưới|thành phần|hoạt chất|"
    r"pha chung|cách ly|an toàn|hiệu quả|loại này)\b",
    re.I,
)


def clean(value, limit=1000):
    return " ".join(str(value or "").split())[:limit]


def _has_content(value):
    """Có chữ thật (không tính '[]', danh sách rỗng hay toàn khoảng trắng)."""
    if isinstance(value, dict):
        return any(_has_content(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_has_content(item) for item in value)
    return bool(clean(value))


def profile_completeness(record):
    """Bốn mục chủ shop cần điền trước khi hồ sơ được AI dùng."""
    record = record or {}
    return {
        "thanh_phan": _has_content(record.get("thanh_phan_dang_ky")),
        "cong_dung": _has_content(record.get("cong_dung")),
        "huong_dan": _has_content(record.get("huong_dan_su_dung")),
        "luu_y": _has_content(record.get("luu_y")),
    }


def is_approvable(record):
    """Hồ sơ chỉ được lên live khi có tên và ít nhất công dụng HOẶC hướng dẫn.

    Hồ sơ rỗng mà vẫn được duyệt nghĩa là AI "có hồ sơ" nhưng không có gì để
    nói — nó sẽ tự suy ra liều và công dụng.
    """
    record = record or {}
    return bool(clean(record.get("ten_san_pham"))) and (
        _has_content(record.get("cong_dung")) or _has_content(record.get("huong_dan_su_dung")))


def shared_learning_product_ids(knowledge, record, *listing_ids):
    """Include the approved profile and every listing linked to it, across platforms."""
    profile_id = clean((record or {}).get("san_pham_id"), 160)
    ids = [clean(value, 160) for value in listing_ids if value]
    if profile_id:
        ids.append(profile_id)
        ids.extend(pid for pid, profile in getattr(knowledge, "by_shop_id", {}).items()
                   if profile.get("san_pham_id") == profile_id)
    return list(dict.fromkeys(ids))


def fold(value):
    text = unicodedata.normalize(
        "NFD", clean(value).casefold().replace("đ", "d")
    )
    return "".join(ch for ch in text if unicodedata.category(ch) != "Mn")


_INTENT_PATTERNS = {name: re.compile(fold(pattern.pattern), re.I) for name, pattern in {
    "price": _PRICE, "dose": _DOSE, "promotion": _PROMO, "mixing": _MIX,
    "recommendation": _RECOMMEND, "after_planting": _AFTER_PLANTING, "technical": _TECH,
}.items()}


def _accent_aware(comment, accented, plain):
    text = clean(comment).casefold()
    if accented.search(text):
        return True
    folded = fold(text)
    if len(folded) != len(text):
        return bool(plain.search(folded))
    # Chỉ nhận khớp không dấu khi đoạn đó trong câu gốc cũng không dấu.
    return any(text[m.start():m.end()] == m.group(0) for m in plain.finditer(folded))


_IGNORED = {"san", "pham", "chai", "loai", "cong", "nghe", "giup", "cay"}

# Từ chung trong tiêu đề SEO của shop. Dùng RIÊNG cho việc dò tên hồ sơ: ở đó
# một token như "bo" (từ "Bổ sung", "Canxi Bo", "phân bón") từng đủ để gán nhầm
# hồ sơ Bọ Trĩ cho một sản phẩm khác dòng. Không dùng cho việc đoán khách đang
# nói tới SKU nào — ở đó "bọ trĩ" lại là từ khoá quan trọng.
GENERIC_NAME_TOKENS = _IGNORED | {
    "bo", "bon", "phan", "toan", "cau", "dang", "sua", "moi", "phun", "tuoi",
    "lit", "ml", "kg", "gr", "cho", "va", "voi", "cac", "tat", "ca", "sieu",
    "cuc", "manh", "hieu", "qua", "tri", "che", "nong", "duoc", "gia", "re",
    "hang", "vuon", "trong", "combo", "goi", "tang", "mua", "the", "he", "tot",
}


_VOLUME = re.compile(r"(\d+(?:[.,]\d+)?)\s*(lit|l|ml|kg|gr|g)(?![a-z0-9])")
_VOLUME_UNITS = {"l": "lit", "lit": "lit", "ml": "ml",
                 "kg": "kg", "gr": "gr", "g": "gr"}


def _volume_tokens(folded):
    """Gộp '5L', '5 lít', 'can 5 lit' về cùng một token '5lit'.

    Chai 1L và can 5L là hai mức giá khác hẳn nhau nên việc này đáng làm.
    """
    found = set()
    for amount, unit in _VOLUME.findall(folded):
        amount = amount.replace(",", ".")
        if "." in amount:
            amount = amount.rstrip("0").rstrip(".")
        if amount:
            found.add(f"{amount}{_VOLUME_UNITS[unit]}")
    return found


def tokens(value):
    folded = fold(value)
    plain = {
        item for item in re.findall(r"[a-z0-9]+", folded)
        if len(item) >= 2 and item not in _IGNORED
    }
    return plain | _volume_tokens(folded)


def name_tokens(value):
    """Token dùng để dò tên hồ sơ; đã bỏ hết từ chung của tiêu đề shop."""
    return {item for item in tokens(value) if item not in GENERIC_NAME_TOKENS}


_LOAD_JSON_CACHE = OrderedDict()
_LOAD_JSON_LOCK = threading.Lock()
_LOAD_JSON_MAX = 64


def load_json(path, default):
    """Đọc JSON, nhớ theo (đường dẫn, mtime_ns, cỡ) (F4).

    mapping_state/dashboard đọc lại catalog, overrides, mapping nhiều lần mỗi
    lượt poll. Cache giữ bản pickle; mỗi lần gọi trả một bản sao mới hoàn toàn
    nên caller sửa thoải mái mà không làm bẩn cache. Tệp đổi -> chữ ký đổi ->
    đọc lại. Lỗi đọc/parse không được cache.
    """
    try:
        target = Path(path)
        stat = target.stat()
        key = str(target)
        signature = (stat.st_mtime_ns, stat.st_size)
        # "Racy" như git: tệp vừa ghi (< 2 giây) có thể bị ghi lại cùng cỡ
        # trong cùng nhịp đồng hồ mtime -> không tin cache, không lưu cache.
        racy = time.time_ns() - stat.st_mtime_ns < 2_000_000_000
        with _LOAD_JSON_LOCK:
            entry = _LOAD_JSON_CACHE.get(key)
            if entry is not None and entry[0] == signature and not racy:
                _LOAD_JSON_CACHE.move_to_end(key)
                blob = entry[1]
            else:
                blob = None
        if blob is not None:
            return pickle.loads(blob)
        value = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return default
    if racy:
        return value
    try:
        blob = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
    except Exception:
        return value
    with _LOAD_JSON_LOCK:
        _LOAD_JSON_CACHE[key] = (signature, blob)
        _LOAD_JSON_CACHE.move_to_end(key)
        while len(_LOAD_JSON_CACHE) > _LOAD_JSON_MAX:
            _LOAD_JSON_CACHE.popitem(last=False)
    return value


class KnowledgeBase:
    def __init__(self, catalog_path, profiles_path, mapping_path=None):
        self.catalog_path = Path(catalog_path)
        self.profiles_path = Path(profiles_path)
        self.mapping_path = Path(mapping_path) if mapping_path else None
        # Shared knowledge remains immutable. Operator edits live in a local
        # overlay next to the machine data and are merged only in memory.
        data_root = (self.catalog_path.parent.parent
                     if self.catalog_path.parent.name == "shared"
                     else self.catalog_path.parent)
        self.overrides_path = data_root / "local_product_overrides.json"
        self._load()

    def reload(self):
        """Nạp lại hồ sơ ngay giữa phiên LIVE, không cần khởi động lại.

        Dựng xong toàn bộ rồi mới gán một lượt: nếu gán dần, một bình luận rơi
        đúng lúc nạp sẽ thấy danh sách hồ sơ rỗng và bị bỏ qua oan.
        """
        fresh = KnowledgeBase.__new__(KnowledgeBase)
        fresh.catalog_path = self.catalog_path
        fresh.profiles_path = self.profiles_path
        fresh.mapping_path = self.mapping_path
        fresh.overrides_path = self.overrides_path
        fresh._load()
        self.records = fresh.records
        self.profiles = fresh.profiles
        self.by_shop_id = fresh.by_shop_id
        self.excluded_shop_ids = fresh.excluded_shop_ids
        self.profile_overrides = fresh.profile_overrides

    @staticmethod
    def _catalog_records(raw):
        """Đọc được catalog cũ và gói shared có manifest cùng lúc."""
        content = raw.get("content") if isinstance(raw, dict) else None
        source = content if isinstance(content, dict) else raw
        for group in source.get("nhom_san_pham", []):
            for product in group.get("san_pham", []):
                if isinstance(product, dict) and product.get("san_pham_id"):
                    yield product

    def all_records(self):
        """Mọi hồ sơ trong file, kể cả chưa duyệt — dùng cho công cụ gán."""
        raw = load_json(self.catalog_path, {})
        records = [dict(record) for record in self._catalog_records(raw)]
        overlay = load_json(self.overrides_path, {})
        overrides = overlay.get("overrides") if isinstance(overlay, dict) else {}
        if not isinstance(overrides, dict):
            overrides = {}
        for record in records:
            record_id = clean(record.get("san_pham_id"), 80)
            values = overrides.get(record_id)
            if isinstance(values, dict):
                if values.get("hidden") is True:
                    record["hidden"] = True
                    continue
                record.update({key: value for key, value in values.items()
                               if key not in {"san_pham_id", "updated_at", "updated_by", "hidden"}})
                record["local_override"] = True
                record["local_override_updated_at"] = values.get("updated_at")
        # Hồ sơ chủ shop TỰ TẠO trên máy này (máy mới cài không có kho dùng chung).
        # Không còn tự động duyệt: hồ sơ nháp (rỗng) KHÔNG được AI dùng.
        shared_ids = {clean(record.get("san_pham_id"), 80) for record in records}
        legacy_mapped = None
        for record_id, values in overrides.items():
            if (isinstance(values, dict) and values.get("created_local") is True
                    and values.get("hidden") is not True and clean(record_id, 80) not in shared_ids):
                record = {**{key: value for key, value in values.items()
                             if key not in {"updated_by", "hidden", "created_local"}},
                          "san_pham_id": clean(record_id, 80),
                          "local_override": True, "created_local": True}
                if "approved_for_live" in values:
                    approved = values.get("approved_for_live") is True
                else:
                    # Hồ sơ tạo trước bản này chưa có cờ riêng: giữ nguyên hiệu
                    # lực cũ nếu đang được gắn SKU VÀ có nội dung; rỗng -> nháp.
                    if legacy_mapped is None:
                        legacy_mapped = self._machine_mapped_codes()
                    approved = clean(record_id, 80) in legacy_mapped
                record["approved_for_live"] = bool(approved and is_approvable(record))
                record["profile_status"] = "approved" if record["approved_for_live"] else "draft"
                records.append(record)
        return [record for record in records if record.get("hidden") is not True]

    def _machine_mapped_codes(self):
        """Mã hồ sơ đang được gắn SKU trong lớp ánh xạ của máy (không đọc all_records)."""
        if not self.mapping_path:
            return set()
        assignments = load_json(self.mapping_path, {}).get("assignments")
        if not isinstance(assignments, dict):
            return set()
        return {clean(item.get("product_code"), 80) for item in assignments.values()
                if isinstance(item, dict) and item.get("approved_for_live") is True
                and clean(item.get("product_code"), 80)}

    def legacy_approval_report(self):
        """Không ghi gì: đếm hồ sơ tự tạo cũ giữ duyệt / chuyển nháp."""
        raw = load_json(self.overrides_path, {})
        overrides = raw.get("overrides") if isinstance(raw, dict) else {}
        mapped = self._machine_mapped_codes()
        report = {"created_local": 0, "giu_duyet": 0, "thanh_nhap": 0,
                  "thanh_nhap_dang_gan_sku_rong": 0, "da_co_co_rieng": 0}
        for record_id, values in (overrides or {}).items():
            if not isinstance(values, dict) or values.get("created_local") is not True:
                continue
            report["created_local"] += 1
            if "approved_for_live" in values:
                report["da_co_co_rieng"] += 1
                continue
            approvable = is_approvable(values)
            if clean(record_id, 80) in mapped and approvable:
                report["giu_duyet"] += 1
            else:
                report["thanh_nhap"] += 1
                if clean(record_id, 80) in mapped:
                    report["thanh_nhap_dang_gan_sku_rong"] += 1
        return report

    def approve_local_profile(self, record_id):
        """Đánh dấu hồ sơ tự tạo là đã duyệt. Chỉ khi đủ nội dung tối thiểu."""
        from catalog_layers import atomic_write_json
        record_id = clean(record_id, 80)
        raw = load_json(self.overrides_path, {})
        if not isinstance(raw, dict):
            raw = {}
        overrides = raw.get("overrides") if isinstance(raw.get("overrides"), dict) else {}
        values = overrides.get(record_id)
        if not isinstance(values, dict) or values.get("created_local") is not True:
            raise ValueError(f"Không phải hồ sơ tự tạo: {record_id}")
        if not is_approvable(values):
            raise ValueError(f"profile_not_approvable:{record_id}")
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        overrides[record_id] = {**values, "approved_for_live": True, "approved_at": stamp,
                                "approved_by": "Chủ dự án Kid TE"}
        raw["overrides"] = overrides
        raw.setdefault("schema_version", 1)
        atomic_write_json(self.overrides_path, raw)
        return True

    def hidden_ids(self):
        """Mã hồ sơ chủ shop đã xoá (vào Thùng rác) trên máy này."""
        raw = load_json(self.overrides_path, {})
        overrides = raw.get("overrides") if isinstance(raw, dict) else {}
        return {clean(rid, 80) for rid, values in (overrides or {}).items()
                if isinstance(values, dict) and values.get("hidden") is True}

    def mappable_records(self):
        """Hồ sơ có mã bền; không cho UI gán SKU mới vào dữ liệu lịch sử."""
        return [record for record in self.all_records()
                if not clean(record.get("san_pham_id"), 80).startswith("LEGACY-")
                and (record.get("reference_only") is not True
                     or record.get("approved_for_live") is True)]

    def mapping_assignments(self):
        """Trả shop_product_id -> product code từ đúng lớp dữ liệu đang dùng."""
        if self.mapping_path:
            raw = load_json(self.mapping_path, {})
            assignments = raw.get("assignments")
            if not isinstance(assignments, dict):
                return {}
            return {
                clean(shop_id, 160): clean(item.get("product_code"), 80)
                for shop_id, item in assignments.items()
                if isinstance(item, dict) and clean(shop_id, 160)
                and clean(item.get("product_code"), 80)
            }
        assigned = {}
        for record in self.all_records():
            for shop_id in record.get("shop_product_ids") or []:
                assigned[clean(shop_id, 160)] = clean(record.get("san_pham_id"), 80)
        return assigned

    def mapping_history_count(self):
        if not self.mapping_path:
            return 0
        history = load_json(self.mapping_path, {}).get("history")
        return len(history) if isinstance(history, list) else 0

    def apply_mapping(self, assignments):
        """Ghi ánh xạ product_id -> hồ sơ, và bật cờ duyệt cho hồ sơ được gán.

        Mỗi product_id chỉ thuộc đúng một hồ sơ: gán sang hồ sơ mới thì gỡ
        khỏi hồ sơ cũ, nếu không hai hồ sơ cùng nhận một SKU.
        """
        if self.mapping_path:
            return self._apply_machine_mapping(assignments)
        raw = load_json(self.catalog_path, {})
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        changed = 0
        for shop_id, target in assignments.items():
            shop_id = clean(shop_id, 160)
            target = clean(target, 80)
            if not shop_id:
                continue
            touched = False
            for group in raw.get("nhom_san_pham", []):
                for product in group.get("san_pham", []):
                    if not isinstance(product, dict):
                        continue
                    ids = [clean(item, 160) for item in
                           (product.get("shop_product_ids") or [])]
                    has = shop_id in ids
                    wants = clean(product.get("san_pham_id"), 80) == target
                    if has and not wants:
                        remaining = [item for item in ids if item != shop_id]
                        product["shop_product_ids"] = remaining
                        if not remaining:
                            # Hồ sơ không còn gắn SKU nào thì không được phép
                            # lên live nữa; để cờ duyệt treo lại là gây nhầm.
                            product["approved_for_live"] = False
                        touched = True
                    elif wants and not has:
                        product["shop_product_ids"] = sorted(set(ids + [shop_id]))
                        product["approved_for_live"] = True
                        product["approved_by"] = "Chủ dự án Kid TE"
                        product["approved_at"] = stamp
                        touched = True
            if touched:
                changed += 1
        if changed:
            temp = self.catalog_path.with_suffix(".json.tmp")
            temp.write_text(
                json.dumps(raw, ensure_ascii=False, indent=4), encoding="utf-8",
            )
            temp.replace(self.catalog_path)
        return changed

    def _apply_machine_mapping(self, assignments):
        """Sửa duy nhất local mapping; shared knowledge không được chạm tới."""
        from catalog_layers import atomic_write_json

        raw = load_json(self.mapping_path, {})
        current = raw.get("assignments")
        if not isinstance(current, dict):
            current = {}
        mappable = {clean(record.get("san_pham_id"), 80): record
                    for record in self.mappable_records()}
        valid_targets = set(mappable)
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        changed = 0
        updated = dict(current)
        for shop_id, target in assignments.items():
            shop_id = clean(shop_id, 160)
            target = clean(target, 80)
            if not shop_id:
                continue
            if target and target not in valid_targets:
                raise ValueError(f"Hồ sơ không tồn tại hoặc không được gán: {target}")
            if target and not is_approvable(mappable[target]):
                # Lưu ánh xạ = cho AI dùng hồ sơ này; hồ sơ rỗng thì không.
                raise ValueError(f"profile_not_approvable:{target}")
            if target and mappable[target].get("created_local") and                     mappable[target].get("approved_for_live") is not True:
                self.approve_local_profile(target)
                mappable[target] = {**mappable[target], "approved_for_live": True}
            previous = updated.get(shop_id)
            if not target:
                if shop_id in updated:
                    updated.pop(shop_id)
                    changed += 1
                continue
            record = dict(previous) if isinstance(previous, dict) else {}
            desired = dict(record)
            # `profile_overrides` (cảnh báo quy cách, giá riêng...) được curate
            # cho ĐÚNG product_code cũ. Gán shop_id sang mã khác thì bỏ nó đi,
            # không thì cảnh báo/quy cách của sản phẩm cũ dính nhầm sang sản
            # phẩm mới — coi như bịa dữ liệu cho khách.
            if desired.get("product_code") != target:
                desired.pop("profile_overrides", None)
            desired["product_code"] = target
            desired["approved_for_live"] = True
            desired["approval"] = {
                "approved_by": "Chủ dự án Kid TE",
                "approved_at": stamp,
            }
            if desired != previous:
                updated[shop_id] = desired
                changed += 1
        if changed:
            history = raw.get("history") if isinstance(raw.get("history"), list) else []
            history.append({"changed_at": stamp, "reason": "manual_mapping_ui",
                            "assignments": current})
            raw["history"] = history[-50:]
            raw["assignments"] = updated
            atomic_write_json(self.mapping_path, raw)
        return changed

    def undo_last_mapping(self):
        if not self.mapping_path:
            raise ValueError("Hoàn tác chỉ hỗ trợ lớp ánh xạ theo máy")
        from catalog_layers import atomic_write_json
        raw = load_json(self.mapping_path, {})
        history = raw.get("history") if isinstance(raw.get("history"), list) else []
        if not history:
            return False
        entry = history.pop()
        assignments = entry.get("assignments")
        if not isinstance(assignments, dict):
            raise ValueError("Lịch sử ánh xạ bị hỏng")
        raw["assignments"] = assignments
        raw["history"] = history
        atomic_write_json(self.mapping_path, raw)
        return True

    def _load(self):
        profiles_path = self.profiles_path
        all_records = self.all_records()
        self.records = []
        profiles = load_json(profiles_path, {})
        self.profiles = profiles.get("products", {})
        # Chỉ mục cứng theo product_id của shop. Đây là đường khớp chính:
        # tên tiêu đề TikTok không đủ tin cậy để quyết định hồ sơ kỹ thuật.
        self.by_shop_id = {}
        self.excluded_shop_ids = set()
        # `[T4]` Quy cách/cảnh báo/giá riêng của TỪNG SKU — nằm trong machine
        # assignment (profile_overrides), không nằm trong hồ sơ kỹ thuật dùng
        # chung. context() ghép chúng lại theo đúng shop_id đang hỏi.
        self.profile_overrides = {}
        if self.mapping_path:
            machine = load_json(self.mapping_path, {})
            assignments = machine.get("assignments")
            if not isinstance(assignments, dict):
                assignments = {}
            records_by_id = {
                clean(record.get("san_pham_id"), 80): record for record in all_records
            }
            active_ids = set()
            for raw_shop_id, assignment in assignments.items():
                if not isinstance(assignment, dict):
                    continue
                shop_id = clean(raw_shop_id, 160)
                target = clean(assignment.get("product_code"), 80)
                record = records_by_id.get(target)
                if (shop_id and record and assignment.get("approved_for_live") is True
                        and is_approvable(record)
                        and (not record.get("created_local")
                             or record.get("approved_for_live") is True)):
                    self.by_shop_id[shop_id] = record
                    active_ids.add(target)
                    overrides = assignment.get("profile_overrides")
                    if isinstance(overrides, dict) and overrides:
                        self.profile_overrides[shop_id] = overrides
            self.records = [record for record in all_records
                            if clean(record.get("san_pham_id"), 80) in active_ids]
        else:
            for record in all_records:
                # Tên file không phải là quyền phát ngôn. Chỉ hồ sơ đã
                # được chủ dự án bật cờ duyệt mới được đưa vào runtime.
                if record.get("approved_for_live") is True:
                    self.records.append(record)
            for record in self.records:
                for raw_id in record.get("shop_product_ids") or []:
                    shop_id = clean(raw_id, 160)
                    if shop_id:
                        self.by_shop_id[shop_id] = record
                for raw_id in record.get("excluded_shop_product_ids") or []:
                    shop_id = clean(raw_id, 160)
                    if shop_id:
                        self.excluded_shop_ids.add(shop_id)
        # "Không phải SKU của hồ sơ NÀY" không có nghĩa là "không SKU nào
        # nhận". Combo Đạm Cá + Humic bị hồ sơ Đạm Cá 5L loại trừ, mà chính
        # nó lại có hồ sơ combo riêng — dùng chung một tập loại trừ toàn cục
        # thì SKU đó thành vô chủ và AI im lặng về nó.
        self.excluded_shop_ids -= set(self.by_shop_id)

    def approved_product_codes(self):
        """Các mã kỹ thuật hiện được ít nhất một SKU cục bộ duyệt dùng."""
        return {
            clean(record.get("san_pham_id"), 80) for record in self.records
        }

    def match(self, title, comment="", product_id=""):
        """Chọn hồ sơ đã duyệt cho một SKU đang bán.

        Khi biết ``product_id``, chỉ chấp nhận ánh xạ cứng. Không có ánh xạ thì
        trả None — im lặng an toàn hơn nhiều so với gán nhầm hồ sơ của một sản
        phẩm khác rồi đưa thành phần và liều lượng của nó cho khách.
        """
        product_id = clean(product_id, 160)
        if product_id:
            if product_id in self.excluded_shop_ids:
                return None
            return self.by_shop_id.get(product_id)
        title_tokens = name_tokens(title)
        comment_tokens = name_tokens(comment)
        ranked = []
        for record in self.records:
            name = " ".join((
                clean(record.get("ten_san_pham")),
                clean(record.get("listing_aliases")),
            ))
            candidate_tokens = name_tokens(name)
            title_score = len(title_tokens & candidate_tokens)
            comment_score = len(comment_tokens & candidate_tokens)
            exact = fold(record.get("ten_san_pham")) in fold(title) if title else False
            score = title_score * 3 + comment_score * 2 + (8 if exact else 0)
            if score:
                ranked.append((score, record))
        ranked.sort(key=lambda item: item[0], reverse=True)
        # Không có product_id thì đòi hỏi cao hơn hẳn: phải trùng ít nhất hai
        # token đặc trưng, hoặc trùng nguyên tên sản phẩm.
        if not ranked or ranked[0][0] < 8:
            return None
        return ranked[0][1]

    # Chỉ những trường thuộc về CÔNG THỨC mới được kế thừa. Giá, khuyến mãi,
    # quy cách là của riêng từng SKU — kế thừa chúng là bịa dữ liệu.
    INHERITABLE_PROFILE_FIELDS = (
        "technical_profile", "uses", "directions", "note",
    )

    def context(self, record, shop_id=""):
        if not record:
            return {}
        product_id = clean(record.get("san_pham_id"), 80)
        result = dict(record)
        # Ho so sinh tu website co san_pham_id dang LIVE-<shop_id>, nhung
        # overlay approved_live_profiles.json van khoa theo SP-xx. Giu duong
        # noi qua legacy_profile_id, neu khong phan ke thua cong thuc va cac
        # du kien theo nhan se am tham mat.
        legacy_id = clean(record.get("legacy_profile_id"), 80)
        profile = self.profiles.get(product_id) or (
            self.profiles.get(legacy_id) if legacy_id else None)
        if profile:
            result["ho_so_da_duyet"] = self._resolve_inheritance(profile)
        # `[T4]` Quy cách/cảnh báo/giá riêng của SKU này (vd chai 500ml bán ra
        # nhưng hồ sơ web ghi 250ml) nằm ở machine assignment, theo shop_id —
        # không phải theo mã kỹ thuật dùng chung nên không có trong `record`.
        shop_id = clean(shop_id, 160)
        overrides = self.profile_overrides.get(shop_id) if shop_id else None
        if overrides:
            result.update(overrides)
        return result

    def _resolve_inheritance(self, profile, depth=0):
        """Hồ sơ 1L khai 'cùng công thức, khác dung tích' với 5L.

        Trường `inherits_technical_profile_from` tồn tại trong dữ liệu từ đầu
        nhưng chưa bao giờ được đọc, nên chai 1L mất phần thành phần và liều
        đã được duyệt theo nhãn. Ở đây nối lại, có chặn vòng lặp.
        """
        parent_id = clean(profile.get("inherits_technical_profile_from"), 80)
        if not parent_id or depth >= 3:
            return profile
        parent = self.profiles.get(parent_id)
        if not parent:
            return profile
        parent = self._resolve_inheritance(parent, depth + 1)
        merged = dict(profile)
        for field in self.INHERITABLE_PROFILE_FIELDS:
            if not merged.get(field) and parent.get(field):
                merged[field] = parent[field]
        merged["inherited_from"] = parent_id
        return merged

    def recommendations(self, comment, live_products, limit=3):
        """Tìm SKU đang bán có hồ sơ phù hợp với vấn đề khách mô tả."""
        wanted = tokens(comment)
        ranked = []
        for item in live_products or []:
            if not isinstance(item, dict):
                continue
            item_shop_id = item.get("product_id", "")
            record = self.match(item.get("title", ""), product_id=item_shop_id)
            if not record:
                continue
            approved = self.context(record, shop_id=item_shop_id)
            evidence = json.dumps({
                "ten_san_pham": approved.get("ten_san_pham"),
                "phan_khuc": approved.get("phan_khuc"),
                "mo_ta_ngan": approved.get("mo_ta_ngan"),
                "cong_dung": approved.get("cong_dung"),
                "doi_tuong_cay_trong": approved.get("doi_tuong_cay_trong"),
                "hieu_qua_chinh": approved.get("hieu_qua_chinh"),
            }, ensure_ascii=False)
            matched = sorted(wanted & tokens(evidence))
            if len(matched) < 2:
                continue
            title_overlap = len(wanted & tokens(item.get("title")))
            score = len(matched) * 3 + title_overlap * 2
            ranked.append((score, clean(item.get("title"), 220), {
                "live_product": item,
                "approved_product_knowledge": approved,
                "matched_terms": matched,
            }))
        ranked.sort(key=lambda row: (row[0], row[1]), reverse=True)
        return [row[2] for row in ranked[:max(1, int(limit))]]

    @staticmethod
    def intents(comment):
        text = fold(comment)
        found = {name: bool(pattern.search(text)) for name, pattern in _INTENT_PATTERNS.items()}
        # Giá/khuyến mãi: so trên chữ CÓ DẤU trước ("tăng" khác "tặng", "gia đình"
        # khác "giá"); mẫu không dấu chỉ nhận đoạn khách thật sự gõ không dấu.
        found["price"] = _accent_aware(comment, _PRICE, _PRICE_PLAIN)
        found["promotion"] = _accent_aware(comment, _PROMO, _PROMO_PLAIN)
        return found

    @staticmethod
    def missing_fields(comment, live_product, knowledge):
        intent = KnowledgeBase.intents(comment)
        missing = []
        if intent["price"]:
            # Giá gốc là con số muốn nói. Nhưng nếu API không trả giá gốc thì
            # vẫn còn giá niêm yết của shop; im lặng ở câu hỏi giá là mất đơn.
            quotable = (
                clean((knowledge or {}).get("gia_goc_duyet"))
                or clean((live_product or {}).get("original_price"))
                or clean((live_product or {}).get("sale_price"))
                or clean((live_product or {}).get("price"))
            )
            if not quotable:
                missing.append("gia_goc")
        if intent["promotion"]:
            live_program = clean((live_product or {}).get("promotion"))
            approved_program = clean((knowledge or {}).get("live_promotion"))
            profile_program = clean(
                ((knowledge or {}).get("ho_so_da_duyet") or {}).get("promotion")
            )
            if not (live_program or approved_program or profile_program):
                missing.append("chuong_trinh")
        return sorted(set(missing))

    @staticmethod
    def general_knowledge_gaps(comment, live_product, knowledge):
        """Chủ đề kỹ thuật hồ sơ shop chưa có: KHÔNG chặn trả lời, chỉ báo model dùng kiến thức chung."""
        intent = KnowledgeBase.intents(comment)
        gaps = []
        if intent["dose"]:
            directions = (knowledge or {}).get("huong_dan_su_dung")
            inherited = ((knowledge or {}).get("ho_so_da_duyet") or {}).get("directions")
            if not clean(directions) and not clean(inherited):
                gaps.append("lieu_dung")
        if intent["after_planting"]:
            verified = fold(json.dumps({
                "huong_dan_su_dung": (knowledge or {}).get("huong_dan_su_dung"),
                "luu_y": (knowledge or {}).get("luu_y"),
            }, ensure_ascii=False))
            if not any(term in verified for term in (
                "sau khi trong", "sau trong", "moi trong", "ngay sau trong",
            )):
                gaps.append("thoi_diem_sau_trong")
        if intent["mixing"]:
            verified = fold(json.dumps({
                "huong_dan_su_dung": (knowledge or {}).get("huong_dan_su_dung"),
                "luu_y": (knowledge or {}).get("luu_y"),
            }, ensure_ascii=False))
            if not re.search(
                r"\b(?:pha chung|tron|phun chung|ket hop|tuong thich)\b",
                verified,
            ):
                gaps.append("pha_chung")
        if intent["technical"] and not knowledge:
            gaps.append("ho_so_san_pham")
        return sorted(set(gaps))


_GAP_RULES = {
    "pha_chung": "pha chung: hồ sơ shop không có; suy từ hoạt chất/độ pH/đồng/kỵ đã biết, dặn thử một ít trước",
    "lieu_dung": "liều: hồ sơ shop không có; suy từ loại sản phẩm, nồng độ, dung tích (ml/bình 16-25L), dặn xem thêm nhãn",
    "thoi_diem_sau_trong": "sau trồng: hồ sơ shop không có; trả lời theo kinh nghiệm nông học, nói vừa phải",
    "ho_so_san_pham": "chưa có hồ sơ sản phẩm: dùng hiểu biết chung về loại sản phẩm, đừng bịa số cụ thể",
    "san_pham_phu_hop": ("khách hỏi nên dùng gì mà mã chưa khớp hồ sơ: gợi ý 1-2 sản phẩm hợp nhất trong "
                         "all_live_products theo cây/triệu chứng khách nói, hoặc hỏi thêm cây gì/bệnh gì; không bịa giá"),
    "gia_goc": ("sản phẩm này chưa có giá trong dữ liệu: KHÔNG nói con số nào; trả lời phần khác khách hỏi "
                "và mời xem giá trong giỏ hàng; không khẳng định có mã/ưu đãi"),
}


def general_knowledge_hint(gaps):
    """Gợi ý nhỏ cho model: chủ đề shop chưa có dữ liệu -> trả lời bằng kiến thức chung, hồ sơ shop (nếu có) ưu tiên."""
    lines = [_GAP_RULES[g] for g in gaps or [] if g in _GAP_RULES]
    if not lines:
        return None
    return "Dữ liệu shop (nếu có) ưu tiên. Thiếu thì trả lời như người bán nhiều kinh nghiệm, nói tự nhiên, không từ chối. " + "; ".join(lines)


# Chọn SKU chạy từ plugin chon-san-pham (nạp theo đường dẫn).
_selection_processor = load_processor(
    PLUGINS_DIR / "chon-san-pham" / "processor.py", "chon-san-pham",
)
select_live_product = _selection_processor.select_live_product
has_explicit_product_reference = _selection_processor.has_explicit_product_reference
