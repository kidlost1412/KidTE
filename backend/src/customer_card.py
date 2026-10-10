"""Thẻ khách `the_khach`: vài chục token cho biết khách này QUEN hay LẠ để chào đúng kiểu.

Chỉ dùng dữ liệu của CHÍNH khách này (customer_key), cùng cổng riêng tư với bộ nhớ
khách (opt-out, bia mộ/tombstone, định danh yếu => không có thẻ). `build_card` thuần,
không I/O; `for_customer` đọc qua CustomerMemory.card_source (truy vấn có chặn) và
FAIL OPEN: lỗi bất kỳ => None, câu trả lời vẫn đi tiếp không có thẻ.
"""
from __future__ import annotations

import logging
from datetime import datetime

import smalltalk
from customer_history import _BOUGHT

_log = logging.getLogger("kid-te.core")
MAX_PRODUCTS = 3
MAX_FACTS = 4
_SKIP_FACTS = {"san_pham_quan_tam"}     # đã có trong san_pham_da_hoi
_REGULAR_SESSIONS = 2                   # đã ghé từ 2 phiên LIVE khác trở lên
_REGULAR_OTHER_COMMENTS = 8             # hoặc >= 8 bình luận ở các phiên khác


def _short(value, limit):
    return " ".join(str(value or "").split())[:limit]


def _parse(value):
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def relative_time(then, now):
    """"hôm nay", "hôm qua", "3 ngày trước"... theo NGÀY LỊCH."""
    if then is None or now is None:
        return ""
    try:
        days = (now.astimezone().date() - then.astimezone().date()).days
    except (TypeError, ValueError, OverflowError):
        return ""
    if days <= 0:
        return "hôm nay (live trước)"
    if days == 1:
        return "hôm qua"
    if days < 14:
        return f"{days} ngày trước"
    if days < 60:
        return f"{days // 7} tuần trước"
    return f"{max(2, days // 30)} tháng trước"


def _cap_name(name, limit=40):
    """Cắt tên sản phẩm theo từ, không để lại ngoặc/dấu treo."""
    name = _short(name, 200)
    cut = name
    if len(name) > limit:
        cut = name[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-·(+")
    if "(" in cut and ")" not in cut:
        cut = cut.rsplit("(", 1)[0].rstrip(" ,;:-·+")
    return cut or name[:limit]


def build_card(source, now=None, shortener=None):
    """source = CustomerMemory.card_source(...) -> thẻ (dict gọn) hoặc None."""
    if not isinstance(source, dict):
        return None
    now = now or datetime.now().astimezone()
    session_id = source.get("session_id")
    rows = [r for r in source.get("rows") or [] if isinstance(r, dict)]
    other = [r for r in rows if r.get("session_id") != session_id]
    other_sessions = {r.get("session_id") for r in other}
    if not other_sessions:
        tier = "lạ"
    elif len(other_sessions) >= _REGULAR_SESSIONS or len(other) >= _REGULAR_OTHER_COMMENTS:
        tier = "quen"
    else:
        tier = "gặp lại"
    card = {"quen_la": tier, "so_lan_ghe": len(other_sessions) + 1}
    if other:
        last = relative_time(_parse(other[0].get("happened_at")), now)
        if last:
            card["lan_gan_nhat"] = last
    shorten = shortener or (lambda text: text)
    products = []
    for row in rows:
        title = _short(row.get("product_title"), 120)
        if not title or smalltalk.is_small_talk(row.get("comment")):
            continue
        try:
            name = _cap_name(shorten(title))
        except Exception:  # noqa: BLE001
            name = _cap_name(title)
        if name and name not in products:
            products.append(name)
        if len(products) >= MAX_PRODUCTS:
            break
    if products:
        card["san_pham_da_hoi"] = products
    texts = [str(r.get("comment") or "").casefold() for r in rows[:30]]
    facts = [(str(k), _short(v, 40)) for k, v in source.get("facts") or [] if k and v]
    fact_map = dict(facts)
    bought = any(word in text for text in texts for word in _BOUGHT)
    if bought or fact_map.get("da_mua", "").casefold() in {"1", "true", "co", "có", "da mua", "đã mua"}:
        card["da_mua"] = True
    for key in ("cach_xung_ho", "xung_ho"):
        if fact_map.get(key):
            card["cach_xung_ho"] = fact_map[key][:20]
            break
    if fact_map.get("cay_trong"):
        card["cay_trong"] = fact_map["cay_trong"]
    skip = _SKIP_FACTS | {"cay_trong", "cach_xung_ho", "xung_ho", "da_mua"}
    others = {k: v for k, v in facts if k not in skip}
    room = MAX_FACTS - (1 if "cay_trong" in card else 0)
    if others and room > 0:
        card["ghi_nho"] = dict(list(others.items())[:room])
    topic = next((_short(r.get("comment"), 60) for r in rows
                  if str(r.get("comment") or "").strip() and not smalltalk.is_small_talk(r.get("comment"))), "")
    if topic:
        card["last_topic"] = topic
    return card


def for_customer(memory, customer_key, interaction_id="", shortener=None):
    """Thẻ của khách; tắt cờ / không có dữ liệu / lỗi => None (fail open)."""
    try:
        import context_budget
        if not context_budget.enabled("customer_card"):
            return None
        source = memory.card_source(customer_key, interaction_id)
        return build_card(source, shortener=shortener) if source is not None else None
    except Exception:  # noqa: BLE001 - không có thẻ còn hơn chặn câu trả lời
        _log.warning("customer card failed", exc_info=True)
        return None


def recent_turns_view(turns, limit=3):
    """2-3 lượt gần nhất của CHÍNH khách này (từ recent_live_turns), rất gọn, mới nhất trước.

    Chỉ nêu câu đã gửi thật (delivery_status == 'sent'); lượt chưa gửi chỉ có lời khách.
    """
    out = []
    try:
        for turn in turns or []:
            if not isinstance(turn, dict):
                continue
            ask = _short(turn.get("resolved_question") or turn.get("comment"), 80)
            if not ask:
                continue
            item = {"khach": ask}
            if turn.get("delivery_status") == "sent" and turn.get("response"):
                item["tra_loi"] = _short(turn["response"], 100)
            out.append(item)
            if len(out) >= limit:
                break
    except Exception:  # noqa: BLE001
        return []
    return out


def _norm_question(text):
    import re
    import unicodedata
    folded = unicodedata.normalize("NFD", str(text or "").casefold().replace("đ", "d"))
    folded = "".join(ch for ch in folded if unicodedata.category(ch) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", folded).strip()


def recent_duplicate(turns, text, window_s=180, now=None):
    """Cùng khách hỏi lại đúng câu đó (đã chuẩn hóa) trong `window_s` giây -> True (trả lời một lần)."""
    try:
        wanted = _norm_question(text)
        if len(wanted) < 4:
            return False
        now = now or datetime.now().astimezone()
        for turn in turns or []:
            if not isinstance(turn, dict):
                continue
            at = _parse(turn.get("happened_at"))
            if not str(turn.get("response") or "").strip() or turn.get("delivery_status") != "sent":
                continue  # câu trước chưa trả lời/gửi xong: không chặn câu hỏi lại
            state = turn.get("conversation_state")
            if isinstance(state, dict) and state.get("awaiting_fields"):
                continue  # lượt trước là câu hỏi làm rõ: khách lặp lại câu là để trả lời tiếp
            if at is None or not 0 <= (now - at.astimezone()).total_seconds() <= window_s:
                continue
            if wanted in {_norm_question(turn.get("comment")), _norm_question(turn.get("resolved_question"))}:
                return True
    except Exception:  # noqa: BLE001 - fail open: thà trả lời hai lần còn hơn bỏ sót
        _log.warning("duplicate check failed", exc_info=True)
    return False
