"""Phân loại "xã giao" (chào, ok, cười, cảm ơn, emoji...) — hàm thuần, không I/O.

Nguyên tắc: BẢO THỦ. Chỉ coi là xã giao khi MỌI từ trong câu đều nằm trong
danh sách trắng bên dưới (đã bỏ dấu). Một từ lạ, một con số, một dấu hiệu hỏi
giá/liều/ship/mua hoặc tên sản phẩm là đủ để trả về False và đi đường đầy đủ.
Sai về phía False chỉ tốn token; sai về phía True mới mất thông tin.
"""
from __future__ import annotations

import re
import unicodedata

MAX_CHARS = 48
MAX_WORDS = 8

# Từ xã giao (đã bỏ dấu, chữ thường). KHÔNG đưa vào đây: có/không/ko (trả lời
# câu hỏi của bot), giá, bao, nhiêu, mua, lấy, chốt, đặt, ship, cây, chai...
_LEXICON = frozenset("""
hello helo hi hey hihi alo yo xin chao
good morning afternoon evening night
cam on camon thanks thank you thankyou tks thx ty
ok oke okie oki okay okla
da vang uh uhm um u
roi nha nhe nghe ne nhi a ah ha oi
anh chi em chu bac minh
shop sop admin host live
ca moi nguoi cac
haha hehe hix huhu wow oa woa
laugh smile cool love like
dep xinh hay tuyet voi qua rat vui ve de thuong cute
tot ngon gioi
buoi sang trua chieu toi ngay lanh
chuc mung may man thanh cong
tam biet bye ngu gap lai
duoc
""".replace("\n", " ").split())

_EMOTE_TAG = re.compile(r"\[[A-Za-z_]{2,14}\]")
_WORD = re.compile(r"[a-z0-9]+")


def fold_text(text):
    """Chữ thường, bỏ dấu tiếng Việt, đ -> d (độc lập với module khác)."""
    value = unicodedata.normalize("NFD", str(text or "").lower().replace("đ", "d"))
    return "".join(ch for ch in value if unicodedata.category(ch) != "Mn")


def _collapse(word):
    # "hellooo" -> "hello", "kkkkk" -> "kk": gộp chữ lặp từ 3 lần trở lên về 2.
    return re.sub(r"(.)\1{2,}", r"\1\1", word)


def _is_emoji_or_symbol(ch):
    return ord(ch) > 0x2000 and unicodedata.category(ch)[0] in {"S", "C"} or ch in "❤♥"


_LEXICON_FOLDED = frozenset(fold_text(w) for w in _LEXICON)


def _lexical(word):
    if word in _LEXICON_FOLDED:
        return True
    collapsed = _collapse(word)
    if collapsed in _LEXICON_FOLDED:
        return True
    squeezed = re.sub(r"(.)\1+", r"\1", word)
    if squeezed in _LEXICON_FOLDED:
        return True
    # tiếng cười: ha/he/hi/ke/ka lặp, k lặp, "kkkk", "hahaha", "hehehe"
    return bool(re.fullmatch(r"(?:ha|he|hi|hu)+h?|(?:ke|ka){2,}|kk+", word)) and len(word) <= 12


def catalog_terms(titles):
    """Tập từ đặc trưng (>= 3 ký tự, đã bỏ dấu) trong tên sản phẩm/bí danh."""
    terms = set()
    for title in titles or ():
        for word in _WORD.findall(fold_text(title)):
            if len(word) >= 3 and not word.isdigit():
                terms.add(word)
    return frozenset(terms)


def is_small_talk(text, known_terms=()):
    """True nếu câu chỉ là chào/ack/cười/cảm ơn/emoji, không dính hỏi mua bán."""
    try:
        if not isinstance(text, str):
            return False
        raw = text.strip()
        if not raw or len(raw) > MAX_CHARS:
            return False
        if any(ch.isdigit() for ch in raw):
            return False
        rest = _EMOTE_TAG.sub(" ", raw)
        folded = fold_text(rest)
        words = _WORD.findall(folded)
        if len(words) > MAX_WORDS:
            return False
        symbols = [ch for ch in rest if _is_emoji_or_symbol(ch)]
        has_tag = rest != raw
        # Chỉ có dấu câu ("?", "...", "!!") -> chưa rõ ý khách, đi đường đầy đủ.
        if not words and not symbols and not has_tag:
            return False
        # Có dấu hỏi kèm từ -> thường là một câu hỏi thật; "ok?" vẫn để model xử.
        if "?" in raw and words:
            return False
        terms = set(known_terms or ())
        for word in words:
            if len(word) >= 3 and (word in terms or _collapse(word) in terms):
                return False
        return all(_lexical(w) for w in words)
    except Exception:  # noqa: BLE001 - phân loại hỏng thì coi như KHÔNG phải xã giao
        return False


def previous_turn_open(turns):
    """Lượt trước của khách còn dở (chưa gửi được câu trả lời) và không phải xã giao.

    Khi đó "ok"/"dạ" có thể là tiếp nối câu hỏi cũ nên không được rút ngữ cảnh.
    """
    try:
        if not turns:
            return False
        previous = turns[0] if isinstance(turns[0], dict) else {}
        if previous.get("delivery_status") == "sent":
            return False
        return not is_small_talk(previous.get("comment"))
    except Exception:  # noqa: BLE001
        return True
