"""Ghép listing đang bán với hồ sơ sản phẩm: quy cách, combo, độ giống tên.

Module này thuần tính toán (không đọc đĩa, không import server) nên dùng được
cả trong ứng dụng lẫn công cụ dòng lệnh. Nguyên tắc an toàn:

  * Khác quy cách (1L với 5L, 100ml với 500ml) là rào cứng: không bao giờ
    được xếp "chắc chắn", vì AI sẽ nói sai liều/giá cho khách.
  * Combo luôn cần người xem (không bao giờ "chắc chắn").
  * Gợi ý sai tệ hơn không gợi ý: thiếu từ riêng của hồ sơ thì "không có".
"""
from __future__ import annotations

import math
import re
import unicodedata
from difflib import SequenceMatcher

TIER_SURE = "chac_chan"
TIER_REVIEW = "can_xem"
TIER_NONE = "khong_co"

SURE_SCORE = 0.75       # điểm tối thiểu để "chắc chắn"
SURE_MARGIN = 0.10      # hồ sơ tốt nhất phải hơn hồ sơ nhì ít nhất chừng này
REVIEW_SCORE = 0.45     # dưới mức này là "không có"

PACKAGE_RE = re.compile(
    r"(?<![a-z0-9])(\d+(?:[.,]\d+)?)\s*(ml|lit|l|kg|gr|g)(?![a-z0-9])")
_TO_BASE = {"ml": ("v", 1.0), "l": ("v", 1000.0), "lit": ("v", 1000.0),
            "g": ("m", 1.0), "gr": ("m", 1.0), "kg": ("m", 1000.0)}

# Từ chung của tiêu đề shop / tên công ty: không dùng để nhận diện sản phẩm.
STOP = {
    "san", "pham", "phan", "bon", "nong", "duoc", "toan", "cau", "giup", "cay",
    "trong", "chai", "goi", "can", "hu", "tui", "loai", "cong", "nghe", "moi",
    "dang", "cho", "va", "cac", "the", "he", "cuc", "manh", "chuyen", "dung",
    "dac", "thuoc", "npk", "che", "lit", "ml", "kg", "gr", "mua", "tang",
    "combo", "gia", "re", "hang", "vuon", "tot", "hieu", "nha", "sx", "bao",
    "hanh", "cua", "ngoai", "troi", "meocham", "tren", "voi", "mot", "nhu",
    "cung", "cap", "bang", "duoc", "tu", "co", "huu",
}

_COMBO_RE = re.compile(r"\bcombo\b|\bcb\b|\s\+\s|tang kem|mua \d+ tang|\d+ goi\b|bo \d+ ")
_NAME_SPLIT = re.compile(r"\s[–—-]\s|\s[–—-]|[–—]|\s-\s|\|")


def _plain(value) -> str:
    text = unicodedata.normalize("NFD", str(value or "").casefold().replace("đ", "d"))
    return "".join(ch for ch in text if unicodedata.category(ch) != "Mn")


def fold(value) -> str:
    return re.sub(r"[^a-z0-9+]+", " ", _plain(value)).strip()


_RANGE_RE = re.compile(r"(?<![a-z0-9])(\d+(?:[.,]\d+)?)\s*-\s*(\d+(?:[.,]\d+)?)\s*(ml|lit|l|kg|gr|g)(?![a-z0-9])")


def parse_sizes(value) -> set:
    """Mọi quy cách trong chuỗi, chuẩn hoá về (loại, số lượng gốc).

    5L = 5 lít = 5000ml; 50g = 50gr. Khác loại (thể tích/khối lượng) thì không
    bao giờ bằng nhau.
    """
    found = set()
    text = fold(value)
    ranged = re.sub(r"[^a-z0-9.,\-]+", " ", _plain(value))
    # "250-500ml" là hai cỡ, không phải chỉ 500ml.
    for first, second, unit in _RANGE_RE.findall(ranged):
        for amount in (first, second):
            kind, factor = _TO_BASE[unit]
            found.add((kind, round(float(amount.replace(",", ".")) * factor, 3)))
    for amount, unit in PACKAGE_RE.findall(ranged):
        kind, factor = _TO_BASE[unit]
        number = float(amount.replace(",", "."))
        found.add((kind, round(number * factor, 3)))
    return found


def describe_sizes(sizes) -> str:
    parts = []
    for kind, value in sorted(sizes):
        if kind == "v":
            parts.append(f"{value / 1000:g} lít" if value >= 1000 else f"{value:g}ml")
        else:
            parts.append(f"{value / 1000:g}kg" if value >= 1000 else f"{value:g}g")
    return " / ".join(parts)


def size_state(listing_sizes, profile_sizes):
    """True = cùng cỡ, False = khác cỡ (chặn), None = không đủ dữ kiện."""
    if not listing_sizes or not profile_sizes:
        return None
    return bool(listing_sizes & profile_sizes)


def is_combo(value) -> bool:
    return bool(_COMBO_RE.search(" " + fold(value) + " "))


def _tokens(value) -> set:
    return {t for t in fold(value).split()
            if len(t) >= 2 and t not in STOP and not PACKAGE_RE.fullmatch(t)
            and not re.fullmatch(r"\d+(ml|l|lit|kg|g|gr)", t)}


def head_name(value) -> str:
    """Phần tên đứng trước lời quảng cáo ('NANO ĐỒNG – Tẩy rửa vườn…')."""
    text = str(value or "").strip()
    parts = _NAME_SPLIT.split(text, maxsplit=1)
    return parts[0].strip() if parts and parts[0].strip() else text


def short_name(title, limit=80) -> str:
    """Tên ngắn cho hồ sơ nháp lấy từ tiêu đề listing dài dòng."""
    text = " ".join(unicodedata.normalize("NFC", str(title or "")).split())
    head = head_name(text)
    head = re.split(r"[,;(]", head, maxsplit=1)[0].strip() or head
    cut = re.split(r"\s+(?:giup|công nghệ|chất|dạng|nông dược|chuyên dùng|cải tiến|giúp|phân thuốc)\b", head, maxsplit=1, flags=re.I)[0].strip()
    if len(cut) >= 4:
        head = cut
    head = " ".join(head.split()[:8])
    if len(head) < 4:
        head = text
    return head[:limit].strip()


class ProfileIndex:
    """Chỉ mục token RIÊNG của từng hồ sơ (idf theo tập hồ sơ truyền vào)."""

    def __init__(self, records):
        self.rows = []
        by_id = {str(r.get("san_pham_id") or ""): r for r in records}
        df = {}
        for record in records:
            pid = str(record.get("san_pham_id") or "").strip()
            if not pid or pid.startswith("LEGACY-"):
                continue
            names = self._names(record, by_id)
            identity = set()
            extra = set()
            sizes = set()
            for name in names:
                identity |= _tokens(head_name(name))
                extra |= _tokens(name)
                sizes |= parse_sizes(name)
            sizes |= parse_sizes(record.get("quy_cach_dong_goi"))
            extra -= identity
            combo = record.get("is_combo") is True
            self.rows.append({"id": pid, "name": str(record.get("ten_san_pham") or ""),
                              "identity": identity, "extra": extra, "sizes": sizes,
                              "combo": combo, "heads": [head_name(n) for n in names]})
            if not combo:      # combo lặp lại token của thành phần, làm lệch idf
                for token in identity:
                    df[token] = df.get(token, 0) + 1
        total = max(1, sum(1 for r in self.rows if not r["combo"]))
        self.weight = {t: math.log(1 + total / n) for t, n in df.items()}
        self.df = df

    @staticmethod
    def _names(record, by_id):
        aliases = record.get("listing_aliases") or []
        if isinstance(aliases, str):
            aliases = [aliases]
        if record.get("is_combo") is True:
            # Tên combo là mã kỹ thuật ("Combo WEB-828 + WEB-830"): dùng tên
            # các thành phần để nhận diện.
            components = [str(c.get("ten_san_pham") or c.get("name") or by_id.get(str(c.get("profile_id") or ""), {}).get("ten_san_pham") or "")
                          for c in record.get("combo_components") or []
                          if isinstance(c, dict)]
            own_name = str(record.get("ten_san_pham") or "")
            names = [own_name] if own_name and not re.search(r"WEB-\d", own_name) else []
            return names + [n for n in components if n] + [str(a) for a in aliases if a]
        return [str(record.get("ten_san_pham") or "")] + [str(a) for a in aliases if a]


def _score(index, row, title_tokens, lead_tokens, folded_title):
    weight = index.weight
    ident_total = sum(weight.get(t, 1.0) for t in row["identity"]) or 1.0
    hits = row["identity"] & title_tokens
    cover = sum(weight.get(t, 1.0) for t in hits) / ident_total
    # Tên sản phẩm thường nằm ở đầu tiêu đề; phần sau là chữ quảng cáo dễ
    # trùng tên hồ sơ khác ("ra hoa đậu quả" nằm trong mô tả Canxi Bo).
    union = row["identity"] | lead_tokens
    union_w = sum(weight.get(t, 1.0) for t in union) or 1.0
    lead_hit = sum(weight.get(t, 1.0) for t in row["identity"] & lead_tokens)
    # Nửa là độ phủ của tên hồ sơ ở phần đầu tiêu đề, nửa là độ "gọn" (Jaccard)
    # để tiêu đề không dấu/không có gạch ngang (lead dài hơn) vẫn khớp.
    lead = 0.5 * (lead_hit / union_w) + 0.5 * (lead_hit / ident_total)
    extra_hits = row["extra"] & title_tokens
    extra = len(extra_hits) / max(1, len(row["extra"])) if row["extra"] else 0.0
    head = fold(row["heads"][0]) if row["heads"] else ""
    seq = SequenceMatcher(None, head, folded_title[:max(len(head) * 2, 40)]).ratio() if head else 0
    score = 0.40 * cover + 0.35 * lead + 0.10 * extra + 0.15 * seq
    unique_hits = [t for t in hits if index.df.get(t) == 1]
    return round(score, 4), sorted(hits), unique_hits


def match_listing(title, index, *, limit=3):
    """Hồ sơ tốt nhất cho một tiêu đề listing, kèm bậc tin cậy."""
    folded = fold(title)
    title_tokens = _tokens(title)
    lead_words = fold(head_name(title)).split()[:10]
    lead_tokens = _tokens(" ".join(lead_words))
    listing_sizes = parse_sizes(title)
    combo = is_combo(title)
    ranked = []
    for row in index.rows:
        score, hits, unique_hits = _score(index, row, title_tokens, lead_tokens, folded)
        if not hits:
            continue
        state = size_state(listing_sizes, row["sizes"])
        ranked.append({"row": row, "score": score, "hits": hits,
                       "unique": unique_hits, "size": state})
    # Cùng quy cách (hoặc chưa biết) xếp trước; khác quy cách chỉ làm dự phòng.
    ranked.sort(key=lambda c: (c["size"] is not False, c["score"]), reverse=True)
    if not ranked:
        return _result(title, None, 0.0, None, ["Không có từ nào trùng tên hồ sơ"],
                       TIER_NONE, combo, [])
    best = ranked[0]
    # Hồ sơ đúng tên nhưng khác quy cách vượt trội: vẫn cho người xem thấy,
    # kèm cảnh báo, thay vì giấu nó sau một hồ sơ sai tên.
    overall = max(ranked, key=lambda c: c["score"])
    if overall is not best and overall["size"] is False and overall["score"] - best["score"] >= 0.15:
        best = overall
    # Margin so với hồ sơ cùng nhóm quy cách (không bị rào cứng loại).
    rivals = [c for c in ranked[1:] if c["size"] is not False
              and (combo or not c["row"]["combo"])]
    second = rivals[0]["score"] if rivals else 0.0
    margin = round(best["score"] - second, 4)
    reasons = []
    if best["hits"]:
        reasons.append("Trùng tên: " + ", ".join(best["hits"]))
    if best["size"] is True:
        reasons.append("Cùng quy cách " + describe_sizes(listing_sizes & best["row"]["sizes"]))
    elif best["size"] is False:
        reasons.append(f"KHÁC quy cách: listing {describe_sizes(listing_sizes)} "
                       f"≠ hồ sơ {describe_sizes(best['row']['sizes'])}")
    else:
        reasons.append("Không đủ dữ kiện quy cách để đối chiếu")
    if combo:
        reasons.append("Listing là combo/quà tặng — cần người xem")
    if best["row"]["combo"] and not combo:
        reasons.append("Hồ sơ là combo nhưng listing không ghi combo")
    if margin < SURE_MARGIN:
        reasons.append("Có hồ sơ khác gần giống ngang")

    if best["score"] < REVIEW_SCORE or not best["unique"] and best["score"] < SURE_SCORE:
        tier = TIER_NONE if best["score"] < REVIEW_SCORE else TIER_REVIEW
    elif (best["score"] >= (SURE_SCORE + 0.10 if (best["size"] is None and listing_sizes) else SURE_SCORE)
          and margin >= SURE_MARGIN and best["size"] is not False
          and not combo and not best["row"]["combo"] and best["unique"]):
        tier = TIER_SURE
    else:
        tier = TIER_REVIEW
    alternatives = [{"id": c["row"]["id"], "name": c["row"]["name"], "score": c["score"]}
                    for c in ranked[1:limit]]
    return _result(title, best, best["score"], best["size"], reasons, tier, combo, alternatives)


def _result(title, best, score, size, reasons, tier, combo, alternatives):
    return {"profile_id": best["row"]["id"] if best else "",
            "profile_name": best["row"]["name"] if best else "",
            "score": score, "size_match": size, "reasons": reasons,
            "tier": tier, "is_combo": combo, "alternatives": alternatives}


def auto_match(listings, records):
    """[{product_id, title}] × hồ sơ -> danh sách kết quả cùng thứ tự."""
    index = ProfileIndex(records)
    out = []
    for item in listings:
        title = str(item.get("title") or "")
        out.append({"product_id": str(item.get("product_id") or ""), "title": title,
                    **match_listing(title, index)})
    return out
