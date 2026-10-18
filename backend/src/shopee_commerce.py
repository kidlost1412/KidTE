"""Shopee product selection and quotable prices. No TikTok prices are reused."""
from __future__ import annotations

import functools
import re
import time
from config import PLUGINS_DIR
from knowledge import fold
from processor_loader import load_processor
from live_conversation import resolve_question


@functools.lru_cache(maxsize=4)
def module(name):
    return load_processor(PLUGINS_DIR / name / "processor.py", name)


def processor(owner, name):
    return getattr(getattr(owner, "processors", None), "modules", {}).get(name) or module(name)


def product_family_candidates(comment, products):
    question = fold(comment).strip(' .!?')
    patterns = [
        r'\b(?:co|ban)\b\s+(.{2,60}?)\s+\b(?:loai nao|nhung loai|cac loai|loai gi)\b',
        r'^(.{2,60}?)\s+(?:co|gom)\s+(?:may loai|nhung loai|cac loai)',
        r'\bco\s+(?:nhung|cac)\s+loai\s+(.{2,60}?)\s+nao\b',
    ]
    match = next((found for pattern in patterns if (found := re.search(pattern, question))), None)
    if not match:
        return '', []
    family = re.sub(r'\b(?:em|shop|minh|anh|chi)\b', ' ', match.group(1))
    family = ' '.join(family.split())
    if not family or len(family.split()) > 4:
        return '', []
    candidates, seen = [], set()
    for item in products:
        title = str(item.get('title', ''))
        identity = (item.get('shop_id'), item.get('product_id'))
        if family in fold(title[:70]) and identity not in seen:
            candidates.append(item)
            seen.add(identity)
    return family, candidates


def select_product(owner, comment, products, pinned, turns):
    selector = processor(owner, "chon-san-pham")
    result = selector.explain_selection(comment, products, pinned)
    ranked = result.get('ranked', [])
    if result['reason'] == 'ambiguous_keeps_pinned' and re.search(r'\b(?:(?:chai|can|loai|san pham|sp) nay|dang ghim)\b', fold(comment)):
        return result['product'], 'currently_pinned'
    if result['reason'] == 'enough_evidence' and len(ranked) > 1 and abs(ranked[0]['score'] - ranked[1]['score']) < 0.2:
        candidates = {row['product_id'] for row in ranked if row['score'] >= ranked[0]['score'] - 0.2}
        for turn in turns:
            for item in products:
                if item['product_id'] in candidates and item['title'] == turn.get('product_title'):
                    return item, 'customer_conversation'
        # Hoà điểm mà sản phẩm đang ghim nằm trong nhóm: khách gần như chắc nói về món đang ghim.
        for item in products:
            if pinned and str(item.get('product_id')) == str(pinned) and item['product_id'] in candidates:
                return item, 'currently_pinned'
        return None, 'ambiguous_product'
    if result["reason"] in {"enough_evidence", "deictic_keeps_pinned"}:
        return result["product"], "explicit_or_pointing"
    # Only a structured pending question or an explicit reference to the
    # previous product can borrow the customer's topic. A fresh weakly matched
    # question must not silently become the last SKU or the observed pin.
    _, pending = resolve_question(comment, turns)
    refers_back = bool(re.search(r'\b(?:loai|chai|can|san pham|sp) (?:do|vua roi|truoc)\b', fold(comment)))
    # A generic commerce question refers to the current conversation/pin,
    # whereas named families such as "đạm cá" must supply their own evidence.
    generic_words = {'gia', 'bao', 'nhieu', 'nhiu', 'tien', 'do', 'em', 'anh', 'chi',
        'shop', 'minh', 'a', 'ha', 'nha', 'oi', 'co', 'khong', 'ko', 'hong', 'khuyen',
        'mai', 'uu', 'dai', 'tang', 'gi', 'qua', 'mua', 'lay', 'chai', 'can', 'lo',
        'duoc', 'them', 'mot', 'may', 'nao', 'thi', 'sao', 'vay'}
    commerce = bool(re.search(r'\b(?:gia|tien|tang|khuyen mai|uu dai|bao nhiu|bao nhieu)\b', fold(comment)))
    words = set(re.findall(r'[a-z]+', fold(comment)))
    generic_commerce = commerce and not (words - generic_words)
    for turn in turns[:1] if pending or refers_back or generic_commerce else []:
        if turn.get("product_title") and turn.get("delivery_status") == "sent":
            exact = [p for p in products if p["title"] == turn["product_title"]]
            if len(exact) == 1:
                return exact[0], "customer_conversation"
        prior = selector.explain_selection(turn.get("comment", ""), products, "")
        if prior["reason"] == "enough_evidence":
            return prior["product"], "customer_conversation"
    if result["reason"] == "ambiguous_keeps_pinned" and (generic_commerce or re.search(
            r'\b(?:(?:chai|can|loai|san pham|sp|phan) nay|dang ghim)\b', fold(comment))):
        return result["product"], "currently_pinned"
    if (pinned and result["reason"] == "ambiguous_keeps_pinned" and not names_other_product(result, pinned)
            and not asks_recommendation(comment)):
        # "pha mấy lít", "còn hàng không", "lấy 3": câu không chỉ SP nào khác
        # thì khách đang nói món đang ghim — trả lời luôn thay vì hỏi lại.
        return result["product"], "currently_pinned"
    listed = [p for p in products or [] if isinstance(p, dict) and p.get('product_id')]
    if len(listed) == 1:
        # Chỉ có một SKU thì không có gì để hỏi lại.
        return listed[0], 'single_product'
    return None, "ambiguous_product"


_RECOMMEND_ASK = re.compile(
    r'\b(?:dung gi|xai gi|bon gi|phun gi|xit gi|nen dung|nen xai|loai nao|thuoc gi|phan gi|'
    r'san pham nao|sp nao|co loai nao|tri gi)\b')


def asks_recommendation(comment):
    """Câu XIN GỢI Ý ("cây mai vàng lá dùng gì", "nên dùng loại nào", "có thuốc gì trị…"):
    không tự gán SP đang ghim — để router/gợi ý xử lý (R8)."""
    return bool(_RECOMMEND_ASK.search(fold(comment)))


def names_other_product(result, pinned):
    """Câu có chữ khớp riêng một SP khác (điểm cao hơn SP ghim) thì mới coi là chỉ SP khác."""
    ranked = result.get('ranked') or []
    pinned_score = next((row['score'] for row in ranked if str(row.get('product_id')) == str(pinned)), 0)
    return any(row['score'] > pinned_score and row['score'] > 0
               for row in ranked if str(row.get('product_id')) != str(pinned))


def price_context(product, catalog, session_id):
    try:
        age = time.time() - float(catalog.get("updated_at", 0))
    except (TypeError, ValueError, OverflowError):
        return {}
    if catalog.get("session_id") != session_id or not 0 <= age < 90:
        return {}
    raw = product.get("price_data") or {}
    if raw.get("currency") != "VND":
        return {}
    # Confirmed against the LIVE catalog API: these fields are VND decimal strings,
    # unlike scaled prices in other Shopee APIs. Never guess scales by magnitude.
    def amount(value):
        text = str(value or "")
        return int(text) if text.isdigit() and 0 < int(text) < 100_000_000 else None
    original = amount(raw.get("price_before_discount"))
    low = amount(raw.get("price_min_before_discount")) if original else amount(raw.get("price_min"))
    high = amount(raw.get("price_max_before_discount")) if original else amount(raw.get("price_max"))
    base = original or amount(raw.get("price"))
    if not base or low and high and low != high:
        return {"needs_variant": bool(low and high and low != high)} if low and high else {}
    return {"quotable_price": f"{base:,}đ".replace(',', '.'), "quotable_price_vnd":base,
            "quotable_price_source":"shopee_live_original_price" if original else "shopee_live_listed_price",
            "rule":"Nói đúng giá này của đúng SKU. Không tự tính giá sau mã, tổng nhiều chai/can hay lấy giá TikTok.",
            "voucher_pitch":"Sau khi nói giá, có thể nhắc khách kiểm tra mã Shopee theo tài khoản; không khẳng định có mã hoặc tự dựng ưu đãi."}


def price_violation(owner, reply, price, signal):
    policy = processor(owner, "chinh-sach-gia")
    for violation in (policy.violates_price_policy(reply, signal), policy.asserts_voucher(reply),
                      policy.pushy_after_purchase(reply, signal)):
        if violation:
            return "Không bác giá khách, không khẳng định chắc có mã, không thúc mua nếu khách đã mua."
    said = []
    for match in re.finditer(r"(?<!\w)(\d+(?:[.,]\d+)*)\s*(k|đ|₫|vnd|nghìn|ngàn|triệu|tr)(?!\w)", reply, re.I):
        value = policy.price_to_vnd(match.group(1) + (match.group(2) if match.group(2).casefold() in {'k','nghìn','ngàn','triệu','tr'} else ''))
        if value:
            said.append(value)
    allowed = {price.get("quotable_price_vnd"), signal.get("reported_price_vnd")}
    if any(value not in allowed for value in said):
        return "Chỉ được nói số tiền trong price.quotable_price hoặc nhắc lại số tiền chính khách vừa báo; không tự tính."
    # Bảo khách bấm xem giá chỉ sai khi ĐÃ có giá để nói; chưa có giá thì đó là
    # câu đúng (không bịa số). Khớp theo từ để "nhắn" vẫn khớp nhưng chỉ cạnh "xem giá".
    if price.get("quotable_price") and re.search(r"\b(?:bam|nhan|click)\b[^.!?]{0,30}\bxem gia\b", fold(reply)):
        return "Trả lời giá trực tiếp bằng price.quotable_price; không bảo khách nhấn vào xem giá."
    return ""
