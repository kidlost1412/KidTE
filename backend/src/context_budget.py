"""Giảm token gửi model: cờ bật/tắt, chiếu hồ sơ theo ý định, danh mục gọn.

Mọi hàm ở đây là thuần hoặc chỉ đọc một file cờ nhỏ. Cờ tắt = hành vi cũ
y nguyên; lỗi bất kỳ = nhật ký + trả về hành vi cũ, không chặn câu trả lời.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from config import DATA_DIR

FLAGS_PATH = DATA_DIR / "machine" / "context_budget.json"
# Gói C: persona_all_lanes = làn xã giao dùng CÙNG nền cố định như làn đầy đủ (giữ giọng/quy tắc);
# customer_card = thẻ khách `the_khach`; playbook = sổ tay tình huống (sales_playbook).
DEFAULT_FLAGS = {"smalltalk_lane": True, "intent_fields": True, "compact_catalog": True,
                 "persona_all_lanes": True, "customer_card": True, "playbook": True}
_log = logging.getLogger("kid-te.core")
_cache = {"key": None, "flags": dict(DEFAULT_FLAGS)}


def flags():
    """Cờ hiện hành; thiếu file hoặc file hỏng thì mặc định bật hết."""
    path = Path(FLAGS_PATH)
    try:
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return dict(DEFAULT_FLAGS)
    if _cache["key"] == key:
        return dict(_cache["flags"])
    result = dict(DEFAULT_FLAGS)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for name in DEFAULT_FLAGS:
                if name in raw:
                    result[name] = bool(raw[name])
    except Exception:  # noqa: BLE001
        _log.warning("context_budget.json không đọc được; dùng mặc định", exc_info=True)
    _cache.update(key=key, flags=result)
    return dict(result)


def enabled(name):
    return bool(flags().get(name, DEFAULT_FLAGS.get(name, False)))


# ---------------------------------------------------------------------------
# Chiếu hồ sơ sản phẩm theo ý định
# ---------------------------------------------------------------------------
# Luôn có: tên, quy cách/đóng gói, cảnh báo quy cách, giá/ưu đãi đã duyệt (nhỏ).
_ALWAYS = ("ten_san_pham", "quy_cach_dong_goi", "canh_bao_quy_cach", "variant_note",
           "source_discrepancy", "is_combo", "gia_goc_duyet", "live_promotion")
_DOSE = ("huong_dan_su_dung", "luu_y", "combo_components", "dose_extrapolation_rule")
_USAGE = ("cong_dung", "thanh_phan_dang_ky", "huong_dan_su_dung", "luu_y",
          "doi_tuong_cay_trong", "combo_components", "hieu_qua_chinh", "phan_khuc",
          "dose_extrapolation_rule")
# Trường trong ho_so_da_duyet (hồ sơ duyệt) và trường cấp trên tương ứng.
_PROFILE_FALLBACK = {"directions": "huong_dan_su_dung", "uses": "cong_dung",
                     "technical_profile": "thanh_phan_dang_ky", "note": "luu_y"}


def intent_groups(intents):
    """Ý định -> tập nhóm trường. Không rõ ý định thì mặc định nhóm 'usage'."""
    intents = intents or {}
    groups = set()
    if intents.get("dose"):
        groups.add("dose")
    if intents.get("mixing") or intents.get("technical") or intents.get("after_planting") \
            or intents.get("recommendation"):
        groups.add("usage")
    if (intents.get("price") or intents.get("promotion")) and not groups:
        groups.add("price")
    return groups or {"usage"}


def _short_summary(profile):
    for key in ("mo_ta_ngan", "hieu_qua_chinh"):
        value = profile.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:200]
    uses = profile.get("cong_dung")
    if isinstance(uses, list) and uses and isinstance(uses[0], str):
        return uses[0].strip()[:160]
    if isinstance(uses, str) and uses.strip():
        return uses.strip()[:160]
    return ""


def project_profile(profile, intents):
    """Chiếu hồ sơ đã duyệt xuống đúng nhóm trường theo ý định.

    Luôn bỏ trường meta/rác (website_product_ids, featured_image_*, nguon...).
    Lỗi => trả nguyên hồ sơ (hành vi cũ).
    """
    if not isinstance(profile, dict) or not profile:
        return profile
    try:
        groups = intent_groups(intents)
        wanted = list(_ALWAYS)
        if "dose" in groups:
            wanted += _DOSE
        if "usage" in groups:
            wanted += _USAGE
        out = {}
        for key in dict.fromkeys(wanted):
            value = profile.get(key)
            if value not in (None, "", [], {}):
                out[key] = value
        if "usage" not in groups:
            summary = _short_summary(profile)
            if summary:
                out["tom_tat_cong_dung"] = summary
        approved = profile.get("ho_so_da_duyet")
        if isinstance(approved, dict):
            slim = {}
            if approved.get("package"):
                slim["package"] = approved["package"]
            if approved.get("promotion"):
                slim["promotion"] = approved["promotion"]
            for src, top in _PROFILE_FALLBACK.items():
                # Chỉ giữ bản trong hồ sơ duyệt khi cấp trên thiếu (liều kế thừa...).
                if src == "directions" and ("dose" in groups or "usage" in groups) and not out.get(top):
                    slim[src] = approved.get(src)
                elif src in {"uses", "technical_profile", "note"} and "usage" in groups and not out.get(top):
                    slim[src] = approved.get(src)
            slim = {k: v for k, v in slim.items() if v not in (None, "", [], {})}
            if slim:
                out["ho_so_da_duyet"] = slim
        return out
    except Exception:  # noqa: BLE001
        _log.warning("project_profile failed", exc_info=True)
        return profile


def promo_text(value):
    """Chỉ giữ chương trình dạng chữ. Bản ghi JSON hoạt động của sàn (status, activity_id,
    format_activity_price = GIÁ SALE) không phải lời khuyến mãi và không được phép nói ra."""
    text = str(value or "").strip()
    return "" if text[:1] in "{[" else text[:600]


def reduced_live_product(item, short_name):
    item = item or {}
    return {"product_id": str(item.get("product_id") or "")[:160],
            "short_name": short_name,
            "original_price": str(item.get("original_price") or "")[:80],
            "promotion": promo_text(item.get("promotion"))}


def brief_profile(profile):
    """Sản phẩm chỉ để GỢI Ý thêm: tên + công dụng chính + cây phù hợp."""
    out = {key: profile[key] for key in ("ten_san_pham", "quy_cach_dong_goi", "canh_bao_quy_cach",
                                         "gia_goc_duyet", "live_promotion", "is_combo")
           if profile.get(key) not in (None, "", [], {})}
    summary = _short_summary(profile)
    if summary:
        out["tom_tat_cong_dung"] = summary
    uses = profile.get("cong_dung")
    if isinstance(uses, list) and uses:
        out["cong_dung"] = [str(x)[:140] for x in uses[:3]]
    crops = profile.get("doi_tuong_cay_trong")
    if isinstance(crops, list) and crops:
        out["doi_tuong_cay_trong"] = [str(x)[:100] for x in crops[:4]]
    return out


def trim_recommendations(recommendations, current_id, intents, shortener, limit=2):
    """Tối đa 2 gợi ý, bỏ sản phẩm đang xét, live_product gọn, hồ sơ theo ý định."""
    rows = []
    current_id = str(current_id or "")
    for item in recommendations or []:
        live = item.get("live_product") or {}
        if current_id and str(live.get("product_id") or "") == current_id:
            continue
        rows.append({
            "live_product": reduced_live_product(live, shortener(live)),
            "approved_product_knowledge": (
                project_profile(item.get("approved_product_knowledge"), {"recommendation": True, **(intents or {})})
                if (intents or {}).get("recommendation")
                else brief_profile(item.get("approved_product_knowledge") or {})),
            "matched_terms": item.get("matched_terms", []),
        })
        if len(rows) >= limit:
            break
    return rows


# ---------------------------------------------------------------------------
# Danh mục gọn cho phần tiền tố cố định
# ---------------------------------------------------------------------------
def cap_name(name, limit=60):
    name = re.sub(r"\s+", " ", str(name or "")).strip()
    if len(name) > limit:
        name = name[:limit].rsplit(" ", 1)[0] or name[:limit]
    # "Combo Nano Tonic + 4" (từ bị cắt dở sau dấu +) -> bỏ phần treo.
    return re.sub(r"[\s,;:\-·(+]*\+\s*\d*$", "", name).rstrip(" ,;:-·(+")


def compact_catalog(products, shortener, limit=60):
    """[{product_id, name, original_price, promotion}] sắp theo product_id (ổn định)."""
    rows = []
    for item in products or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "")
        rows.append({"product_id": str(item.get("product_id") or "")[:160],
                     "name": cap_name(shortener(item), limit) or cap_name(title, limit),
                     "original_price": str(item.get("original_price") or "")[:80],
                     "promotion": promo_text(item.get("promotion"))})
    rows.sort(key=lambda row: row["product_id"])
    return rows


# Chính sách đã có sẵn trong lời nhắc hệ thống mặc định (system_prompt.txt):
# GIÁ, kien_thuc_da_hoc/quy_tac/phong_cach/cau_da_gui_gan_day, ví dụ chủ shop
# duyệt, và "không bao giờ nói shop không có hàng".
DUPLICATE_POLICY_KEYS = ("kien_thuc_policy", "approved_example_policy")
# Hướng dẫn thẻ khách/sổ tay chỉ đưa vào ngữ cảnh khi chủ shop có lời nhắc riêng
# (lời nhắc mặc định đã có sẵn mục này) — xem server._stable_policy_extras.


def drop_duplicate_policies(context, override_present):
    """Bỏ chuỗi chính sách trùng lời nhắc hệ thống — chỉ khi dùng lời nhắc mặc định."""
    if override_present:
        return context
    for key in DUPLICATE_POLICY_KEYS:
        context.pop(key, None)
    return context


# ---------------------------------------------------------------------------
# Làn xã giao
# ---------------------------------------------------------------------------
def short_memory(text, limit=420):
    """Ký ức khách rút rất gọn: tên + dữ kiện + vài lượt gần nhất, cắt theo dòng."""
    text = str(text or "").strip()
    if not text:
        return ""
    keep, used = [], 0
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("- Chỉ dùng", "- Không trộn", "NGỮ CẢNH")):
            continue
        line = line[:140]
        if used + len(line) > limit:
            break
        keep.append(line)
        used += len(line) + 1
    return "\n".join(keep)


def short_shop_info(info):
    info = info if isinstance(info, dict) else {}
    out = {key: info[key] for key in ("ten_cong_ty", "giao_hang") if info.get(key)}
    if info.get("cach_noi"):
        out["cach_noi"] = str(info["cach_noi"])[:200]
    return out
