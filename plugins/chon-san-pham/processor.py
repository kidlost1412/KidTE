"""Chọn SKU thuần và tất định; không I/O, không import plugin khác."""
from __future__ import annotations

import math
import re

from knowledge import clean, fold, tokens


# Từ để hỏi, không phải từ chỉ sản phẩm. Tiêu đề shop nhồi rất nhiều từ khoá
# nên "bao nhiêu", "chai này", "còn không" từng đủ để chọn nhầm cả một SKU.
QUERY_STOPWORDS = frozenset({
    "gia", "bao", "nhieu", "la", "nay", "do", "kia", "co", "khong", "ko",
    "minh", "em", "anh", "chi", "oi", "a", "the", "sao", "nao", "voi", "cho",
    "hoi", "ship", "ban", "shop", "nha", "ne", "vay", "duoc", "the nao",
    "con", "hang", "mua", "dat", "tien", "ma", "va", "roi", "chua", "di",
    "toi", "can", "giup", "xin", "tu", "khi", "lam", "gi", "dung", "cai",
    "loai", "thu", "xem", "coi", "biet", "noi", "hay", "nen", "moi",
    "thuoc", "phan", "bon", "san", "pham", "nuoc", "chai",
    # Tu vung CAU HOI LIEU. Tieu de TikTok nhoi SEO nen nhieu tieu de co san
    # "pha", "binh", "gam", "lit" — "a6 pha bình 16 lít bao nhiêu gam" tung
    # chon ra Magie Kem thay vi A6 chi vi may tu nay.
    "pha", "binh",
    "phuy", "thung", "bon nuoc", "lieu", "gam", "gr", "g", "lit", "ml",
    "kg", "phun", "tuoi", "tren", "duoi", "mot", "lan",
})
MIN_PRODUCT_EVIDENCE = 1.6
# Promotional wording is not a product name, even if a saved custom stopword
# list predates this rule. LIVE titles often contain "mua 1 tặng 1".
COMMERCE_QUESTION_WORDS = frozenset({"tang", "qua", "khuyen", "mai", "uu", "dai", "km"})
_DEICTIC = re.compile(r"\b(?:phan|chai|loai|cai|san pham|sp|thuoc|goi|can|hu|lo|bich|hop|mon|dong|con)\s+(?:nay|do|kia|nay a)\b")
TITLE_HEAD_CHARS = 70
# Từ chỉ quy cách: giúp phân biệt yếu, và một mình nó thì không được phép
# quyết định sản phẩm ("5 lít" khớp cả đạm cá lẫn humic).
_VOLUME_TOKEN = re.compile(r"^\d+(?:\.\d+)?(?:lit|ml|kg|gr)$")
UNIT_TOKENS = frozenset({"lit", "lita", "ml", "kg", "gr", "chai", "can", "goi", "tui", "hop", "bich"})

# Bỏ dấu làm tên cây trùng với chữ khác: "sầu riêng" → "sau" khớp "SẠCH SÂU ĐẤT"
# (thấy thật 24/09: "đạm cá bón sầu riêng được không" chọn nhầm thuốc sâu),
# "cà phê" → "ca" khớp "đạm CÁ". Gộp tên cây ghép thành MỘT chữ trước khi chấm
# điểm, cho cả câu khách (gõ có hay không dấu) lẫn tiêu đề sản phẩm.
_COMPOUNDS = re.compile(
    r"\b(sau|ca|dua|thanh|ho|mai|bap|khoai)[\s-]+"
    r"(rieng|phe|chua|tim|rot|phao|hau|leo|luoi|long|tieu|vang|cai|lang|mi)\b")
_COMPOUND_OK = {"saurieng", "caphe", "cachua", "catim", "carot", "caphao", "duahau", "dualeo",
                "dualuoi", "thanhlong", "hotieu", "maivang", "bapcai", "khoailang", "khoaimi"}


def _join(match):
    word = match.group(1) + match.group(2)
    return word if word in _COMPOUND_OK else match.group(0)


def _tok(value):
    folded = fold(value).replace("cafe", "caphe")
    return tokens(_COMPOUNDS.sub(_join, folded))


SETTINGS_SCHEMA = [
    {"key": "uu_tien_ten_goi_da_hoc", "type": "bool", "default": True, "group": "Cách chọn",
     "label": "Ưu tiên tên gọi khác chủ shop đã dạy (vd \"win gold 20\" = Đạm Cá)",
     "help": "Tên gọi khác nằm ở Trí nhớ AI. Tắt thì chỉ đoán theo tên trên giỏ hàng."},
    {"key": "giu_ghim_khi_chi_tro", "type": "bool", "default": True, "group": "Cách chọn",
     "label": "Khách nói \"chai này / loại đó\" thì giữ sản phẩm đang ghim",
     "help": "Chỉ đổi khi khách nói một mã riêng của sản phẩm khác (tc01, 1L…)."},
    {"key": "nguong_chac_chan", "type": "number", "default": MIN_PRODUCT_EVIDENCE, "min": 0.5, "max": 5,
     "step": 0.1, "group": "Cách chọn", "label": "Mức bằng chứng tối thiểu để đổi khỏi sản phẩm đang ghim",
     "help": "Thấp → AI dễ đổi sang sản phẩm khác theo vài chữ trùng. Cao → AI bám sản phẩm đang ghim "
             "khi câu hỏi mơ hồ. Mặc định 1,6."},
    {"key": "tu_bo_qua", "type": "list", "default": sorted(QUERY_STOPWORDS), "group": "Từ không dùng để đoán",
     "label": "Từ bỏ qua khi đoán sản phẩm (viết không dấu)",
     "help": "Từ để hỏi / đơn vị (giá, bao nhiêu, bình, lít…). Tiêu đề TikTok nhồi các từ này nên để "
             "chúng quyết định sản phẩm là chọn nhầm."},
]
SETTINGS = {field["key"]: (list(field["default"]) if isinstance(field["default"], list)
                           else field["default"]) for field in SETTINGS_SCHEMA}
_STOPWORDS = [QUERY_STOPWORDS]


def configure(values):
    SETTINGS.update(values or {})
    _STOPWORDS[0] = frozenset(fold(word) for word in SETTINGS["tu_bo_qua"])


def inverse_document_frequency(products):
    """Token xuất hiện ở nhiều tiêu đề thì gần như vô giá trị để phân biệt.

    Ví dụ trong phiên hiện tại "bo", "phan", "cay" nằm ở hàng chục tiêu đề nên
    không được phép quyết định sản phẩm; "tc01" chỉ có ở một tiêu đề nên nó
    gần như chắc chắn là thứ khách đang nói tới.
    """
    total, frequency = 0, {}
    for item in products or []:
        if not isinstance(item, dict):
            continue
        total += 1
        for token in _tok(item.get("title")):
            frequency[token] = frequency.get(token, 0) + 1
    return (frequency, total) if total else ({}, 0)


def evidence_score(wanted, title, frequency, total):
    title_tokens = _tok(title)
    # Tên sản phẩm thật nằm ở đầu tiêu đề; phần đuôi là từ khoá SEO nhồi thêm
    # ("... meocham cay npk kíchrễ đạm tinh"). Trùng ở đầu đáng tin hơn hẳn.
    head_tokens = _tok(clean(title, TITLE_HEAD_CHARS))
    score = 0.0
    for token in wanted & title_tokens:
        appears_in = frequency.get(token, 1)
        if appears_in >= max(3, total * .45):
            continue
        # Small LIVE bags still need meaningful evidence for a named product;
        # log(1/1) used to erase even an exact TC01 reference when only one SKU existed.
        weight = math.log(max(total, 3) / appears_in)
        if token in UNIT_TOKENS or _VOLUME_TOKEN.match(token):
            # Quy cách chỉ là bổ nghĩa: "5 lít" không được lấn át tên sản phẩm.
            score += weight * .8 if _VOLUME_TOKEN.match(token) else weight * .4
        elif token in head_tokens:
            score += weight * 2
        else:
            # ĐUÔI TIÊU ĐỀ LÀ TỪ KHOÁ SEO CỦA SẢN PHẨM KHÁC, KHÔNG PHẢI TÊN NÓ.
            # Tiêu đề HUMIC nhồi sẵn "đạm cá cô đặc ... kíchrễ", nên câu hỏi
            # "pha bao nhiêu kích rễ với đạm cá" bị Humic cướp mất (12/09).
            # Trùng ở đuôi vẫn tính, nhưng chỉ đủ để phá hoà, không đủ để
            # lật một sản phẩm có tên trùng ở ĐẦU tiêu đề.
            score += weight * .35
    # Tiêu đề combo gom từ khoá của nhiều sản phẩm nên rất dễ ăn điểm nhờ. Chỉ
    # ưu tiên combo khi khách thật sự nói tới combo.
    if score and "combo" in title_tokens and "combo" not in wanted:
        score *= .6
    return score


def select_live_product(comment, products, pin_id):
    """Chọn SKU khách đang nói tới; không đủ bằng chứng thì giữ sản phẩm ghim."""
    return explain_selection(comment, products, pin_id)["product"]


def explain_selection(comment, products, pin_id):
    """Như select_live_product nhưng trả thêm bảng điểm và lý do (cho màn Thử ngay)."""
    # Số trần ("bình 20 lít" -> token "20") không phải dấu hiệu sản phẩm:
    # nó khớp với "20%" trong tiêu đề Đạm Cá 1L và cướp mất SKU đang ghim.
    # Dung tích thật đã có token riêng dạng "20lit" nên không mất gì.
    wanted = {item for item in _tok(comment)
              if item not in _STOPWORDS[0] and item not in COMMERCE_QUESTION_WORDS and not item.isdigit()}
    frequency, total = inverse_document_frequency(products)
    package = re.search(r'\b\d+\s*(chai|can|goi|tui|hop)\b', fold(comment))
    ranked = []
    for index, item in enumerate(products or []):
        if not isinstance(item, dict):
            continue
        product_id = clean(item.get("product_id"), 160)
        pinned = bool(product_id and product_id == pin_id)
        score = evidence_score(wanted, item.get("title"), frequency, total)
        if package and score >= SETTINGS['nguong_chac_chan']:
            # Quantity itself is not SKU evidence. Its container can break a
            # name tie: bottled root stimulant versus a bag of root stimulant.
            package_text = ' '.join([item.get('title', ''),
                *[str(sku.get('title', '')) for sku in item.get('skus', []) if isinstance(sku, dict)]])
            if re.search(r'\b' + package.group(1) + r'\b', fold(package_text)):
                score += 1.6
        ranked.append((score, 1 if pinned else 0, -index, item))
    ranked.sort(key=lambda row: row[:3], reverse=True)

    def result(product, reason):
        return {"product": product, "reason": reason, "words": sorted(wanted),
                "threshold": SETTINGS["nguong_chac_chan"],
                "ranked": [{"product_id": clean(row[3].get("product_id"), 160),
                            "title": clean(row[3].get("title"), 160), "score": round(row[0], 2),
                            "pinned": bool(row[1])} for row in ranked[:6]]}

    if not ranked:
        return result(None, "no_products")
    # "phân NÀY", "chai NÀY", "loại đó": khách chỉ vào thứ đang ghim. Chữ trùng
    # tiêu đề sản phẩm khác (vd tên cây "mai" trong "Humic Mai Vàng") không đủ
    # để đổi — chỉ đổi khi khách nói một mã riêng của sản phẩm kia (tc01, 1l…).
    pinned_row = next((row for row in ranked if row[1]), None)
    if (SETTINGS["giu_ghim_khi_chi_tro"] and pinned_row and ranked[0] is not pinned_row
            and _DEICTIC.search(fold(comment))):
        best_title = _tok(ranked[0][3].get("title"))
        strong = {t for t in wanted & best_title
                  if any(ch.isdigit() for ch in t) or (frequency.get(t, 0) == 1 and len(t) >= 4)}
        if not strong:
            return result(pinned_row[3], "deictic_keeps_pinned")
    if ranked[0][0] >= SETTINGS["nguong_chac_chan"]:
        return result(ranked[0][3], "enough_evidence")
    # Mơ hồ thì bám sản phẩm đang ghim: đó là thứ khách đang nhìn thấy.
    if pinned_row:
        return result(pinned_row[3], "ambiguous_keeps_pinned")
    return result(ranked[0][3], "ambiguous_first")


def has_explicit_product_reference(comment, product):
    if not product:
        return False
    overlap = _tok(comment) & _tok(product.get("title"))
    distinctive = {"tc01", "1l", "5l", "1kg", "top", "one", "nano", "furagin", "mig", "a6", "roots"}
    return len(overlap) >= 2 or bool(overlap & distinctive)
