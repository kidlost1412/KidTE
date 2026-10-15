"""Chống lặp câu và chống tự nhận lỗi: kiểm tra rẻ bằng code, chỉ kích hoạt sinh lại khi thật sự cần."""
from __future__ import annotations

import difflib
import re
import unicodedata

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
# Lời xin lỗi / nhận lỗi công khai: khung chat có người xem khác đọc, xin lỗi trước = nhận shop sai.
_FAULT = re.compile(
    r"xin\s+lỗi|em\s+(?:rất\s+)?hiểu\s+(?:anh|chị|bạn|mình)(?:/chị)?\s+(?:đang\s+)?(?:bực|giận|tức)|"
    r"hiểu\s+(?:anh|chị|bạn)\s+bực|"
    r"lỗi\s+(?:của|bên|do)\s+(?:em|shop|bên\s+em)|(?:em|shop)\s+sai\s+rồi|sai\s+sót\s+(?:của|bên)|"
    r"bên\s+em\s+(?:có\s+)?lỗi",
    re.IGNORECASE)


def _norm(text):
    text = unicodedata.normalize("NFC", str(text or "")).lower()
    return " ".join(_PUNCT.sub(" ", text).split())


def opening(reply, words=3):
    """Vài chữ đầu của một câu (đã chuẩn hóa), để biết cách mở câu đã dùng."""
    parts = _norm(reply).split()
    return " ".join(parts[:words])


def openings(replies, words=3, limit=6):
    """Cách mở câu (nguyên văn, vài chữ đầu) của các câu đã gửi gần đây, không trùng, tối đa ``limit``."""
    out, seen = [], set()
    for reply in replies or []:
        raw = " ".join(str(reply or "").split()[:words]).strip(" ,.!?…")
        key = _norm(raw)
        if raw and key not in seen:
            seen.add(key)
            out.append(raw)
    return out[-limit:]


def admits_fault(reply):
    """Trả cụm nhận lỗi/xin lỗi tìm thấy trong câu, hoặc ''. """
    found = _FAULT.search(str(reply or ""))
    return found.group(0) if found else ""


# Từ đệm/xưng hô rất hay gặp trong câu bán hàng: cụm 3 từ mà phần lớn là các từ này không tính là lặp.
_FILLER = frozenset("""nha nhé nè á ạ à ơi dạ vâng ok luôn liền đó thì là mà với cho của có không hông ko được
anh chị em mình bạn shop bên nhà này kia rồi cũng vẫn đang sẽ thôi lắm nhiều ít""".split())


def _ngrams(text, n):
    """Cụm ``n`` từ có nghĩa: bỏ cụm chứa số/giá, chứa từ viết hoa (tên sản phẩm, trừ chữ đầu câu)
    hoặc có từ 2 từ đệm trở lên — các cụm này lặp tự nhiên, không phải lặp khuôn."""
    raw = _PUNCT.sub(" ", unicodedata.normalize("NFC", str(text or ""))).split()
    out = set()
    for i in range(len(raw) - n + 1):
        chunk = raw[i:i + n]
        if any(any(ch.isdigit() for ch in w) for w in chunk):
            continue
        if any(w[:1].isupper() for j, w in enumerate(chunk) if i + j > 0):
            continue
        low = tuple(w.lower() for w in chunk)
        if sum(w in _FILLER for w in low) >= 2:
            continue
        out.add(low)
    return out


def repeats_recent(reply, recents, *, ratio=0.8, opening_words=3, opening_window=3,
                   closing_words=3, ngram=3, ngram_window=6, ngram_limit=2):
    """Trả câu cũ mà ``reply`` lặp lại, hoặc ''. Lặp = một trong:
    gần y nguyên; cùng ``opening_words`` chữ mở với ``opening_window`` câu gần nhất; cùng
    ``closing_words`` chữ khép với ``ngram_window`` câu gần nhất; hoặc chung từ ``ngram_limit``
    cụm ``ngram`` từ có nghĩa (xem ``_ngrams``) trở lên với một câu trong ``ngram_window`` câu gần nhất.
    Chỉ dùng để nhắc sinh lại, không bỏ lượt. Câu quá ngắn (< 2 chữ) không xét."""
    mine = _norm(reply)
    words = mine.split()
    if len(words) < 2:
        return ""
    recents = [r for r in (recents or []) if _norm(r)]
    for old in recents:
        if difflib.SequenceMatcher(None, mine, _norm(old)).ratio() >= ratio:
            return old
    head = " ".join(words[:opening_words])
    if len(head.split()) >= opening_words:
        for old in recents[-opening_window:]:
            if " ".join(_norm(old).split()[:opening_words]) == head:
                return old
    window = recents[-ngram_window:] if ngram_window else []
    if closing_words and len(words) >= closing_words + 2:
        tail = words[-closing_words:]
        for old in window:
            if _norm(old).split()[-closing_words:] == tail:
                return old
    if ngram and len(words) >= ngram:
        grams = _ngrams(reply, ngram)
        for old in window:
            if len(grams & _ngrams(old, ngram)) >= ngram_limit:
                return old
    return ""
