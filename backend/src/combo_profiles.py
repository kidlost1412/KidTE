"""AI proposes component identities; technical facts remain sourced and human-reviewed."""

from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
import uuid

from ai_context import system_with_brief
from ai_providers import AIProviderError, GeminiProvider
from listing_match import describe_sizes, is_combo, parse_sizes, size_state
from product_ingest import (
    FIELD_ORDER,
    ExtractionInvalid,
    IngestError,
    _webp_800,
    has_price,
    normalize_image,
    parse_json,
    thumbnail_data_url,
    validate_extraction,
)


def revision(record):
    fields = {k: record.get(k) for k in FIELD_ORDER}
    fields["listing_aliases"] = record.get("listing_aliases")
    return hashlib.sha256(
        json.dumps(fields, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def compose_fields(name, records, notes="", *, components=None, combo=True, package=""):
    """Keep concentrations and doses per component; never calculate a mixed dose."""
    if not combo and len(records) == 1:
        fields = {
            k: copy.deepcopy(records[0].get(k))
            for k in FIELD_ORDER
            if records[0].get(k) not in (None, "", [], {})
        }
        fields["ten_san_pham"] = name
        fields["quy_cach_dong_goi"] = (
            package or records[0].get("quy_cach_dong_goi") or ""
        )
        fields["mo_ta_ngan"] = (
            name
            + " — cùng hồ sơ công thức nguồn; quy cách bán: "
            + fields["quy_cach_dong_goi"]
        )
        if notes:
            fields["luu_y"] = str(fields.get("luu_y") or "") + "\n" + notes[:2000]
        return {
            k: v
            for k, v in validate_extraction(fields)["fields"].items()
            if v not in (None, "", [], {})
        }
    fields = {
        "ten_san_pham": name,
        "mo_ta_ngan": "Gồm: " + "; ".join(r["ten_san_pham"] for r in records),
        "quy_cach_dong_goi": "; ".join(
            r["ten_san_pham"]
            + ": "
            + str(r.get("quy_cach_dong_goi") or "chưa rõ quy cách")
            for r in records
        ),
        "thanh_phan_dang_ky": {},
        "cong_dung": [],
        "huong_dan_su_dung": [],
        "doi_tuong_cay_trong": [],
        "luu_y": "Thông tin được giữ riêng cho từng sản phẩm lẻ. Chưa có xác nhận dùng/pha chung; không cộng liều hoặc hàm lượng giữa các sản phẩm.",
    }
    if components:
        fields["quy_cach_dong_goi"] = "; ".join(
            f"{c['name']}: {c.get('quantity') or 'chưa rõ số lượng'} × {c.get('package') or 'chưa rõ quy cách'}"
            for c in components
        )
        fields["mo_ta_ngan"] = "Gồm: " + fields["quy_cach_dong_goi"]
    for r in records:
        label = r["ten_san_pham"]
        ingredients = r.get("thanh_phan_dang_ky") or {}
        if isinstance(ingredients, dict):
            fields["thanh_phan_dang_ky"].update(
                {f"{label} — {k}": str(v) for k, v in ingredients.items()}
            )
        for key in ("cong_dung", "huong_dan_su_dung"):
            lines = r.get(key) or []
            if isinstance(lines, str):
                lines = [lines]
            fields[key].extend(f"{label}: {line}" for line in lines)
        if not r.get("huong_dan_su_dung"):
            fields["luu_y"] += (
                f"\n{label}: nguồn chưa có liều dùng; cần bổ sung nhãn/tài liệu."
            )
        if r.get("luu_y"):
            fields["luu_y"] += f"\n{label}: {r['luu_y']}"
        crops = r.get("doi_tuong_cay_trong") or []
        if isinstance(crops, list):
            fields["doi_tuong_cay_trong"].extend(str(c) for c in crops)
    fields["doi_tuong_cay_trong"] = list(dict.fromkeys(fields["doi_tuong_cay_trong"]))
    if notes:
        fields["luu_y"] += "\nChủ shop ghi chú: " + notes[:2000]
    normalized = validate_extraction(fields)["fields"]
    return {k: v for k, v in normalized.items() if v not in (None, "", [], {})}


class ComboProfiles:
    def __init__(self, engine, ingest, clock=time.time):
        self.engine, self.ingest, self.clock = engine, ingest, clock
        self._lock = threading.RLock()
        self._proposals = {}

    def _listing(self, pid):
        return next(
            (
                p
                for p in self.engine.mapping_state().get("products", [])
                if str(p.get("product_id")) == pid
            ),
            None,
        )

    def propose(self, payload):
        pid = str(payload.get("product_id") or "")[:160]
        listing = self._listing(pid)
        if not listing:
            raise IngestError("Sản phẩm không còn trong danh sách đang bán.")
        records = {
            str(r.get("san_pham_id")): r
            for r in self.engine.knowledge.all_records()
            if r.get("ten_san_pham") and r.get("is_combo") is not True
        }
        if not records:
            raise IngestError(
                "Chưa có hồ sơ sản phẩm lẻ. Hãy đọc nhãn/tài liệu và tạo hồ sơ trước."
            )
        image = None
        try:
            image = normalize_image(self.engine.product_thumb_path(pid).read_bytes())
        except (ValueError, OSError):
            pass
        wanted = str(payload.get("provider") or "auto")
        if wanted not in ("auto", "gemini", "deepseek"):
            raise IngestError("Chọn Gemini hoặc DeepSeek.")
        provider = self.ingest._pick_provider(wanted, [])
        if provider.name == "gemini" and "gemini_model" in payload:
            model = str(payload.get("gemini_model") or "").strip()
            import re

            if model and not re.fullmatch(r"gemini-[A-Za-z0-9._-]{1,100}", model):
                raise IngestError("Model Gemini không hợp lệ.")
            provider = GeminiProvider(self.ingest.gemini.api_key, model)
        catalog = [
            {
                "id": rid,
                **{k: r.get(k) for k in FIELD_ORDER},
                "listing_aliases": r.get("listing_aliases"),
            }
            for rid, r in records.items()
        ]
        user = json.dumps(
            {
                "listing_title": listing["title"],
                "shop_note": str(payload.get("text") or "")[:6000],
                "available_single_product_profiles": catalog,
            },
            ensure_ascii=False,
        )
        if len(user) > 200_000:
            raise IngestError(
                "Danh mục hồ sơ quá lớn cho một lượt; hãy thu gọn các hồ sơ trước."
            )
        system = system_with_brief("""NHIỆM VỤ: đề xuất hồ sơ cho listing sản phẩm/combo để chủ shop duyệt.
Đọc tên listing, ghi chú và ảnh nếu có. Xác định sản phẩm lẻ cấu thành dựa trên tên/nhãn thực tế
và hồ sơ được cung cấp. KHÔNG chọn chỉ vì công dụng giống nhau. Combo 5 gói có thể là 5 gói CÙNG sản phẩm.
Không giả định mọi sản phẩm cùng tên có cùng quy cách. Không đoán thành phần, số lượng, liều pha hay khả năng pha chung.
Quà tặng/khuyến mãi xuất hiện trên ảnh không phải thành phần cố định: đánh dấu kind="gift", không coi là thành phần chính.
Chỉ đề xuất ID nằm trong available_single_product_profiles. Thiếu/không chắc: để components rỗng hoặc ghi questions,
không bịa ra ID. Không ghi giá/khuyến mãi. Mỗi thành phần có reason ngắn về bằng chứng từ listing/ảnh.
Trả JSON duy nhất: {"suggested_name":"...","components":[{"profile_id":"...","reason":"...","kind":"core|gift|uncertain","quantity":1,"package":"quy cách mỗi món (vd 250ml)"}],
"questions":["điều cần chủ shop xác nhận"],"summary":"cách đề xuất hồ sơ và phần còn thiếu"}.
Không tạo liều dùng mới: backend sẽ giữ riêng thành phần và liều nguyên bản từng hồ sơ nguồn.""")
        fallback_note = ""

        def ask(chosen):
            files = (
                [{"mime": "image/jpeg", "data": image}]
                if image and chosen.supports_files
                else []
            )
            for attempt in range(2):
                raw = chosen.generate(
                    system,
                    user if not attempt else user + "\nChỉ trả JSON đúng cấu trúc.",
                    files,
                )
                try:
                    out = parse_json(raw)
                    if not isinstance(out.get("components"), list):
                        raise ExtractionInvalid("components phải là danh sách")
                    return out
                except ExtractionInvalid:
                    if attempt:
                        raise IngestError(
                            "AI trả về đề xuất không đúng dạng. Hãy thử lại."
                        ) from None

        try:
            try:
                raw = ask(provider)
            except AIProviderError as exc:
                if (
                    wanted != "auto"
                    or provider.name != "gemini"
                    or not self.ingest.deepseek
                    or not self.ingest.deepseek.configured
                ):
                    raise
                fallback_note = (
                    "Gemini chưa đọc được: "
                    + str(exc)
                    + " Đã dùng DeepSeek đọc phần chữ."
                )
                provider = self.ingest.deepseek
                raw = ask(provider)
        except AIProviderError as exc:
            raise IngestError(str(exc)) from exc
        return self._remember(listing, records, image, raw, provider, fallback_note)

    def _remember(self, listing, records, image, raw, provider, fallback_note):
        pid = str(listing["product_id"])
        ids, reasons, questions, details = [], {}, [], {}
        for row in raw["components"][:8]:
            rid = str(row.get("profile_id") or "") if isinstance(row, dict) else ""
            if rid not in records:
                questions.append(
                    "AI nêu thành phần chưa có hồ sơ nguồn hợp lệ; cần bổ sung/xác nhận."
                )
                continue
            reason = str(row.get("reason") or "Cần đối chiếu với nhãn")[:800]
            if row.get("kind") in ("gift", "uncertain") or any(
                marker in reason.lower()
                for marker in ("quà tặng", "tặng kèm", "khuyến mãi")
            ):
                questions.append(
                    records[rid]["ten_san_pham"]
                    + ": "
                    + reason
                    + " — chưa chọn làm thành phần cố định; chủ shop cần xác nhận."
                )
                continue
            if rid not in ids:
                ids.append(rid)
                reasons[rid] = reason
                quantity = row.get("quantity")
                if (
                    not isinstance(quantity, int)
                    or isinstance(quantity, bool)
                    or not 1 <= quantity <= 999
                ):
                    quantity = None
                package = str(row.get("package") or "")[:160]
                if has_price(package):
                    package = ""
                details[rid] = {
                    "quantity": quantity,
                    "package": package or records[rid].get("quy_cach_dong_goi") or "",
                }
        questions += (
            [str(q)[:800] for q in raw.get("questions", []) if isinstance(q, str)]
            if isinstance(raw.get("questions"), list)
            else []
        )
        name = str(raw.get("suggested_name") or listing["title"])[:300]
        if has_price(name) or len(name.strip()) < 2:
            name = listing["title"][:300]
        combo = is_combo(listing["title"]) or len(ids) > 1
        listing_sizes = parse_sizes(listing["title"])
        package = describe_sizes(listing_sizes) if not combo and listing_sizes else ""
        variant = bool(
            not combo
            and len(ids) == 1
            and package
            and size_state(
                listing_sizes, parse_sizes(records[ids[0]].get("quy_cach_dong_goi"))
            )
            is False
        )
        if not combo and len(listing_sizes) > 1:
            questions.append(
                "Listing có nhiều phân loại dung tích: "
                + package
                + ". Hỏi khách quy cách cụ thể; không mặc định một chai duy nhất."
            )
        if variant:
            questions.append(
                "Tạo quy cách riêng "
                + package
                + "; xác nhận cùng công thức với hồ sơ nguồn. Giữ nguyên liều theo lượng dùng, không nhân liều theo dung tích chai."
            )
        proposal_id = uuid.uuid4().hex
        with self._lock:
            now = self.clock()
            self._proposals = {
                k: v for k, v in self._proposals.items() if now - v["at"] < 3600
            }
            if len(self._proposals) >= 256:
                del self._proposals[
                    min(self._proposals, key=lambda k: self._proposals[k]["at"])
                ]
            self._proposals[proposal_id] = {
                "at": now,
                "pid": pid,
                "image": image,
                "records": records,
                "revisions": {rid: revision(r) for rid, r in records.items()},
                "questions": questions,
                "details": details,
                "package": package,
                "combo": combo,
                "variant": variant,
                "saved": None,
            }

        def card(rid):
            r = records[rid]
            return {
                "id": rid,
                "name": r["ten_san_pham"],
                "package": r.get("quy_cach_dong_goi") or "",
                "image_url": self.engine._profile_image_url(r),
                "fields": {k: r.get(k) for k in FIELD_ORDER},
                "reason": reasons.get(rid, ""),
                "suggested": rid in ids,
                "quantity": details.get(rid, {}).get("quantity"),
                "proposed_package": details.get(rid, {}).get("package", ""),
            }

        return {
            "ok": True,
            "proposal_id": proposal_id,
            "product_id": pid,
            "name": name,
            "variant": variant,
            "package": package,
            "is_combo": combo,
            "provider": provider.name,
            "model": getattr(provider, "last_model", "")
            or getattr(provider, "model", ""),
            "summary": str(raw.get("summary") or "")[:1500],
            "questions": list(dict.fromkeys(questions)),
            "fallback_note": fallback_note,
            "read_image": bool(image and provider.supports_files),
            "image_thumb": thumbnail_data_url(image) if image else "",
            "components": [card(rid) for rid in ids],
            "available_profiles": [card(rid) for rid in records],
            "preview": compose_fields(
                name, [records[rid] for rid in ids], combo=combo, package=package
            )
            if ids
            else {},
        }

    def propose_batch(self, payload):
        requested = payload.get("product_ids")
        if not isinstance(requested, list) or not requested or len(requested) > 60:
            raise IngestError("Chọn từ 1 đến 60 sản phẩm cho một đợt đề xuất.")
        ids = list(dict.fromkeys(str(pid) for pid in requested))
        listings = {
            str(p.get("product_id")): p
            for p in self.engine.mapping_state().get("products", [])
        }
        if any(pid not in listings for pid in ids):
            raise IngestError(
                "Một sản phẩm đã không còn trong danh sách. Hãy tải lại danh sách."
            )
        records = {
            str(r.get("san_pham_id")): r
            for r in self.engine.knowledge.all_records()
            if r.get("ten_san_pham") and r.get("is_combo") is not True
        }
        if not records:
            raise IngestError("Chưa có hồ sơ sản phẩm lẻ. Hãy tạo hồ sơ nguồn trước.")
        wanted = str(payload.get("provider") or "auto")
        if wanted not in ("auto", "gemini", "deepseek"):
            raise IngestError("Chọn Gemini hoặc DeepSeek.")
        provider = self.ingest._pick_provider(wanted, [])
        if provider.name == "gemini" and "gemini_model" in payload:
            import re

            model = str(payload.get("gemini_model") or "").strip()
            if model and not re.fullmatch(r"gemini-[A-Za-z0-9._-]{1,100}", model):
                raise IngestError("Model Gemini không hợp lệ.")
            provider = GeminiProvider(self.ingest.gemini.api_key, model)
        catalog = [
            {
                "id": rid,
                **{k: r.get(k) for k in FIELD_ORDER},
                "listing_aliases": r.get("listing_aliases"),
            }
            for rid, r in records.items()
        ]
        shared = {
            "shop_note": str(payload.get("text") or "")[:6000],
            "available_single_product_profiles": catalog,
        }
        if len(json.dumps(shared, ensure_ascii=False)) > 180_000:
            raise IngestError(
                "Danh mục hồ sơ quá lớn cho một lượt; hãy thu gọn hồ sơ trước."
            )
        images = {}
        groups, group, byte_count, text_count = [], [], 0, 0
        for pid in ids:
            try:
                images[pid] = normalize_image(
                    self.engine.product_thumb_path(pid).read_bytes(), max_side=800
                )
            except (ValueError, OSError):
                images[pid] = None
            size = len(images[pid] or b"")
            title_size = len(str(listings[pid].get("title") or ""))
            # Shared catalog once per group; 20 items keeps structured output bounded.
            if group and (
                len(group) >= 20
                or byte_count + size > 12 * 1024 * 1024
                or text_count + title_size > 12_000
            ):
                groups.append(group)
                group, byte_count, text_count = [], 0, 0
            group.append(pid)
            byte_count += size
            text_count += title_size
        if group:
            groups.append(group)
        system = system_with_brief("""Đề xuất HÀNG LOẠT hồ sơ sản phẩm/combo để chủ shop duyệt.
Đọc từng listing và ảnh gắn với image_index (đánh số ảnh từ 1 theo thứ tự file gửi).
Nhận diện sản phẩm lẻ bằng tên/nhãn, không chỉ vì công dụng giống nhau. Chỉ dùng ID trong
available_single_product_profiles. Không đoán thành phần, quy cách, số lượng, liều hoặc khả năng pha chung.
Combo nhiều gói có thể là cùng một sản phẩm. Quà tặng đánh kind=gift; không chắc đánh uncertain.
Không ghi giá/khuyến mãi. Giữ riêng thành phần và liều gốc của nguồn; backend sẽ tạo bản nháp.
Trả JSON duy nhất {"products":[{"product_id":"đúng ID listing đầu vào","suggested_name":"...",
"components":[{"profile_id":"...","reason":"bằng chứng tên/ảnh","kind":"core|gift|uncertain","quantity":1,"package":"quy cách mỗi món (vd 250ml)"}],
"questions":["điều cần xác nhận"],"summary":"đề xuất hồ sơ và phần còn thiếu"}]}.
Trả đúng một kết quả cho MỖI product_id đầu vào. Thiếu bằng chứng thì components rỗng và ghi questions.
Không đổi hay trộn ID giữa listing.
Sản phẩm lẻ cùng công thức nhưng khác dung tích (Đạm Cá 1L/5L, Nano Đồng 250ml/500ml):
chọn hồ sơ công thức nguồn và đề xuất tên/quy cách RIÊNG của listing; không bỏ qua chỉ vì khác cỡ.
Quantity là số chai/gói của từng thành phần combo, chỉ ghi khi thấy bằng chứng. Không rõ trả null.
Package là dung tích/khối lượng MỖI chai/gói, không lấy tổng combo. Không nhân liều theo số lượng hoặc dung tích.""")
        results, errors, calls = [], [], 0
        for group in groups:
            fallback_note = ""

            def ask(chosen, group=tuple(group)):
                nonlocal calls
                files, rows = [], []
                for pid in group:
                    index = None
                    if images[pid] and chosen.supports_files:
                        files.append({"mime": "image/jpeg", "data": images[pid]})
                        index = len(files)
                    rows.append(
                        {
                            "product_id": pid,
                            "listing_title": listings[pid]["title"],
                            "image_index": index,
                        }
                    )
                calls += 1
                raw = parse_json(
                    chosen.generate(
                        system,
                        json.dumps({**shared, "listings": rows}, ensure_ascii=False),
                        files,
                    )
                )
                if not isinstance(raw, dict) or not isinstance(
                    raw.get("products"), list
                ):
                    raise IngestError(
                        "AI trả về danh sách không đúng dạng. Hãy thử lại nhóm chưa đọc được."
                    )
                return raw["products"]

            try:
                try:
                    raw = ask(provider)
                except AIProviderError as exc:
                    if (
                        wanted != "auto"
                        or provider.name != "gemini"
                        or not self.ingest.deepseek
                        or not self.ingest.deepseek.configured
                    ):
                        raise
                    fallback_note = (
                        "Gemini chưa đọc được: "
                        + str(exc)
                        + " Đã dùng DeepSeek đọc phần chữ."
                    )
                    provider = self.ingest.deepseek
                    raw = ask(provider)
                by_id, duplicates = {}, set()
                for row in raw:
                    if not isinstance(row, dict):
                        continue
                    pid = str(row.get("product_id") or "")
                    if pid in by_id:
                        duplicates.add(pid)
                    by_id[pid] = row
                for pid in group:
                    row = by_id.get(pid)
                    if (
                        pid in duplicates
                        or not row
                        or not isinstance(row.get("components"), list)
                    ):
                        errors.append(
                            {
                                "product_id": pid,
                                "error": "AI thiếu/trùng kết quả cho sản phẩm này. Chọn đọc lại mục lỗi.",
                            }
                        )
                        continue
                    results.append(
                        self._remember(
                            listings[pid],
                            records,
                            images[pid],
                            row,
                            provider,
                            fallback_note,
                        )
                    )
            except (AIProviderError, IngestError, ExtractionInvalid) as exc:
                errors.extend({"product_id": pid, "error": str(exc)} for pid in group)
                # Preserve completed groups; stop on overload rather than waste more calls.
                remaining = ids[ids.index(group[-1]) + 1 :]
                errors.extend(
                    {"product_id": pid, "error": "Chưa đọc vì nhóm trước gặp lỗi."}
                    for pid in remaining
                )
                break
        return {
            "ok": True,
            "products": results,
            "errors": errors,
            "ai_calls": calls,
            "groups": len(groups),
        }

    def create_draft(self, payload):
        with self._lock:
            proposal = self._proposals.get(str(payload.get("proposal_id") or ""))
            if not proposal or self.clock() - proposal["at"] >= 3600:
                raise IngestError("Đề xuất đã hết hạn. Hãy cho AI đọc lại.")
            if proposal["saved"]:
                return proposal["saved"]
            listing = self._listing(proposal["pid"])
            if not listing:
                raise IngestError("Sản phẩm không còn trong danh sách đang bán.")
            ids = payload.get("component_ids")
            if not isinstance(ids, list) or not ids or len(ids) > 8:
                raise IngestError("Chọn từ 1 đến 8 sản phẩm lẻ cấu thành.")
            ids = list(dict.fromkeys(str(i) for i in ids))
            current = {
                str(r.get("san_pham_id")): r
                for r in self.engine.knowledge.all_records()
            }
            for rid in ids:
                if (
                    rid not in proposal["records"]
                    or rid not in current
                    or revision(current[rid]) != proposal["revisions"][rid]
                ):
                    raise IngestError(
                        "Hồ sơ nguồn đã đổi/không hợp lệ. Hãy cho AI đọc lại trước khi lưu."
                    )
            name = str(payload.get("name") or listing["title"]).strip()[:300]
            if len(name) < 2 or has_price(name):
                raise IngestError("Đặt tên hồ sơ rõ ràng, không chứa giá/khuyến mãi.")
            records = [current[rid] for rid in ids]
            questions = "\n".join(
                "Cần xác nhận: " + q for q in proposal["questions"] if not has_price(q)
            )
            notes = questions + "\n" + str(payload.get("notes") or "")
            supplied = payload.get("component_details") or {}
            if not isinstance(supplied, dict):
                raise IngestError("Chi tiết thành phần không hợp lệ.")
            metadata = []
            for rid in ids:
                detail = supplied.get(rid) or proposal.get("details", {}).get(rid) or {}
                if not isinstance(detail, dict):
                    raise IngestError("Chi tiết từng thành phần phải là một đối tượng.")
                quantity = detail.get("quantity")
                if quantity is not None and (
                    not isinstance(quantity, int)
                    or isinstance(quantity, bool)
                    or not 1 <= quantity <= 999
                ):
                    raise IngestError(
                        "Số lượng mỗi thành phần phải từ 1 đến 999, hoặc để trống nếu chưa biết."
                    )
                component_package = str(
                    detail.get("package") or current[rid].get("quy_cach_dong_goi") or ""
                )[:160]
                if has_price(component_package):
                    raise IngestError("Quy cách không chứa giá/khuyến mãi.")
                metadata.append(
                    {
                        "profile_id": rid,
                        "source_revision": revision(current[rid]),
                        "name": current[rid]["ten_san_pham"],
                        "quantity": quantity,
                        "package": component_package,
                    }
                )
            combo = len(ids) > 1 or is_combo(listing["title"])
            sizes = parse_sizes(listing["title"])
            package = str(payload.get("package") or proposal.get("package") or "")[:160]
            if not combo and sizes:
                package = describe_sizes(sizes)
            if not combo and package:
                metadata[0]["package"] = package
                metadata[0]["quantity"] = 1
            fields = compose_fields(
                name,
                records,
                notes.strip(),
                components=metadata,
                combo=combo,
                package=package,
            )
            image = (
                _webp_800(proposal["image"])
                if proposal["image"] and payload.get("use_image") is not False
                else None
            )
            out = self.engine.workspace.create_composed_profile(
                fields, proposal["pid"], metadata, combo
            )
            if image:
                self.ingest.staged_image_path(out["id"], create=True).write_bytes(image)
                self.engine.workspace._touch("profiles", "products", "dashboard")
            out.update(
                {
                    "image_saved": bool(image),
                    "product_id": proposal["pid"],
                    "needs_approval": True,
                }
            )
            proposal["saved"] = out
            return out
