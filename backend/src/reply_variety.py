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


def repeats_recent(reply, recents, *, ratio=0.8, opening_words=3, opening_window=3):
    """Trả câu cũ mà ``reply`` lặp lại (gần y nguyên, hoặc cùng ``opening_words`` chữ mở với một trong
    ``opening_window`` câu gần nhất), hoặc ''. Câu quá ngắn (< 2 chữ) không xét."""
    mine = _norm(reply)
    if len(mine.split()) < 2:
        return ""
    recents = [r for r in (recents or []) if _norm(r)]
    for old in recents:
        if difflib.SequenceMatcher(None, mine, _norm(old)).ratio() >= ratio:
            return old
    head = " ".join(mine.split()[:opening_words])
    if len(head.split()) >= opening_words:
        for old in recents[-opening_window:]:
            if " ".join(_norm(old).split()[:opening_words]) == head:
                return old
    return ""

