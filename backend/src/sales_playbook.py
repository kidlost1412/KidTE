"""Sổ tay tình huống bán hàng (sales playbook): cách xử lý khéo cho các câu "khó đỡ".

File data/shared/sales_playbook.json do chủ shop sửa được (GET/PUT /api/workspace/sales-playbook).
Cả sổ tay đi vào PHẦN CỐ ĐỊNH của prompt (sắp theo id, byte-identical => cache);
mỗi lượt chỉ thêm `tinh_huong_phu_hop: [id]` rất nhỏ khi câu khách khớp `dau_hieu`.
Thiếu/hỏng file => sổ tay rỗng (hành vi cũ), không bao giờ chặn câu trả lời.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import unicodedata
from pathlib import Path

from config import SALES_PLAYBOOK_DEFAULT_PATH, SALES_PLAYBOOK_PATH
from smalltalk import fold_text

PLAYBOOK_PATH = SALES_PLAYBOOK_PATH
DEFAULT_PATH = SALES_PLAYBOOK_DEFAULT_PATH
MAX_ENTRIES = 30
MAX_BYTES = 48_000
_ID = re.compile(r"^[a-z0-9_]{1,40}$")
_LISTS = {"dau_hieu": (16, 40), "du_kien_duoc_dung": (6, 160), "cam": (6, 120)}
_OBJECTION_ACTS = frozenset({"objection", "complaint"})
_log = logging.getLogger("kid-te.core")
_lock = threading.Lock()
_cache = {"by_key": {}}


class PlaybookError(ValueError):
    """Dữ liệu sổ tay không hợp lệ (thông điệp là mã lỗi ngắn, an toàn trả cho UI)."""


def _text(value, limit, code):
    if not isinstance(value, str):
        raise PlaybookError(code)
    value = " ".join(value.split())
    if not value or len(value) > limit:
        raise PlaybookError(code)
    return value


def _list(value, max_items, max_len, code, *, allow_empty):
    if isinstance(value, str):
        value = [value] if value.strip() else []
    if not isinstance(value, list) or len(value) > max_items:
        raise PlaybookError(code)
    out = []
    for item in value:
        item = _text(item, max_len, code)
        if item not in out:
            out.append(item)
    if not out and not allow_empty:
        raise PlaybookError(code)
    return out


def validate(data):
    """{"entries":[...]} (hoặc list thẳng) -> danh sách mục đã chuẩn hóa, sắp theo id. Sai => PlaybookError."""
    entries = data.get("entries") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        raise PlaybookError("playbook_entries_invalid")
    if len(entries) > MAX_ENTRIES:
        raise PlaybookError("playbook_too_many_entries")
    clean, seen = [], set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise PlaybookError("playbook_entry_invalid")
        item = {"id": _text(entry.get("id"), 40, "playbook_id_invalid"),
                "tinh_huong": _text(entry.get("tinh_huong"), 100, "playbook_tinh_huong_invalid"),
                "huong_xu_ly": _text(entry.get("huong_xu_ly"), 500, "playbook_huong_xu_ly_invalid")}
        if not _ID.match(item["id"]):
            raise PlaybookError("playbook_id_invalid")
        if item["id"] in seen:
            raise PlaybookError("playbook_id_duplicate")
        seen.add(item["id"])
        item["dau_hieu"] = _list(entry.get("dau_hieu"), *_LISTS["dau_hieu"], "playbook_dau_hieu_invalid",
                                 allow_empty=False)
        item["du_kien_duoc_dung"] = _list(entry.get("du_kien_duoc_dung", []), *_LISTS["du_kien_duoc_dung"],
                                          "playbook_du_kien_invalid", allow_empty=True)
        item["cam"] = _list(entry.get("cam", []), *_LISTS["cam"], "playbook_cam_invalid", allow_empty=True)
        clean.append(item)
    clean.sort(key=lambda row: row["id"])
    if len(json.dumps(clean, ensure_ascii=False).encode("utf-8")) > MAX_BYTES:
        raise PlaybookError("playbook_too_large")
    return clean


def _read(path):
    """Mục của MỘT file: None nếu thiếu file; ném lỗi nếu hỏng. Cache theo mtime."""
    path = Path(path)
    try:
        stat = path.stat()
    except OSError:
        return None
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    with _lock:
        if key in _cache["by_key"]:
            return list(_cache["by_key"][key])
    entries = validate(json.loads(path.read_text(encoding="utf-8-sig")))
    with _lock:
        if len(_cache["by_key"]) > 8:
            _cache["by_key"].clear()
        _cache["by_key"][key] = entries
    return list(entries)


def load(path=None):
    """Mục hiện hành (đã validate, sắp theo id).

    path=None: file chủ shop (data/shared) nếu có và hợp lệ; thiếu hoặc hỏng => bản mặc định đi kèm
    app (prompts/sales_playbook.default.json, chỉ đọc); cả hai hỏng => []. path cụ thể: chỉ file đó.
    """
    if path is not None:
        try:
            return _read(path) or []
        except Exception:  # noqa: BLE001
            _log.warning("sales playbook %s không hợp lệ; bỏ qua", path, exc_info=True)
            return []
    try:
        entries = _read(PLAYBOOK_PATH)
        if entries is not None:
            return entries
    except Exception:  # noqa: BLE001
        _log.warning("sales_playbook.json không hợp lệ; dùng bản mặc định", exc_info=True)
    try:
        return _read(DEFAULT_PATH) or []
    except Exception:  # noqa: BLE001
        _log.warning("sales_playbook.default.json không đọc được; sổ tay rỗng", exc_info=True)
        return []


def save(data, path=None):
    """Validate rồi ghi nguyên tử (tmp + replace). Trả danh sách mục đã chuẩn hóa."""
    entries = validate(data)
    path = Path(path or PLAYBOOK_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps({"version": 1, "entries": entries}, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    os.replace(temp, path)
    with _lock:
        _cache["by_key"] = {}
    return entries


def stable_view(entries=None):
    """Bản gọn đưa vào nền cố định: chỉ id + cách xử lý + dữ kiện được dùng + điều cấm.

    Bỏ tinh_huong/dau_hieu (chỉ để người sửa và mã khớp đọc), sắp theo id, bỏ trường rỗng,
    khoá ngắn để tiết kiệm token. Byte-identical giữa các lượt (cache tiền tố).
    """
    rows = []
    for entry in sorted(load() if entries is None else entries, key=lambda e: e["id"]):
        row = {"id": entry["id"], "xu_ly": entry["huong_xu_ly"]}
        if entry.get("du_kien_duoc_dung"):
            row["duoc_dung"] = list(entry["du_kien_duoc_dung"])
        if entry.get("cam"):
            row["cam"] = list(entry["cam"])
        rows.append(row)
    return rows


def _norm(text):
    return unicodedata.normalize("NFC", str(text or "")).casefold()


def _phrase_in(text, phrase):
    return bool(re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", text))


def match(text, acts=(), entries=None, limit=3):
    """id các tình huống hợp với câu khách (tối đa `limit`), theo thứ tự id.

    Cụm `dau_hieu` CÓ dấu chỉ khớp chữ có dấu (đắt != đặt); cụm không dấu ("ban chiu") khớp cả
    chữ đã bỏ dấu (khách gõ không dấu). Khớp nguyên từ. Khi router báo objection/complaint,
    nới thêm: mọi từ của một cụm nhiều từ cùng xuất hiện (không cần liền kề).
    """
    try:
        raw = _norm(text)
        if not raw.strip():
            return []
        folded = fold_text(raw)
        loose = bool(_OBJECTION_ACTS & set(acts or ()))
        raw_words = set(re.findall(r"\w+", raw))
        ids = []
        for entry in sorted(load() if entries is None else entries, key=lambda e: e["id"]):
            for phrase in entry.get("dau_hieu") or ():
                phrase = _norm(phrase).strip()
                if not phrase:
                    continue
                plain = fold_text(phrase) == phrase
                parts = phrase.split()
                if (_phrase_in(raw, phrase) or (plain and _phrase_in(folded, phrase)) or (
                        loose and len(parts) > 1
                        and all(part in raw_words or (plain and part in set(re.findall(r"\w+", folded)))
                                for part in parts))):
                    ids.append(entry["id"])
                    break
            if len(ids) >= limit:
                break
        return ids
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------------------
# Tích hợp vào ngữ cảnh gửi model
# ---------------------------------------------------------------------------
# Chỉ đưa vào ngữ cảnh khi chủ shop dùng lời nhắc hệ thống RIÊNG (lời nhắc mặc định đã có mục này).
POLICY_TEXT = (
    "the_khach là thẻ của ĐÚNG khách này (quen_la: quen/gặp lại/lạ, so_lan_ghe, lan_gan_nhat, "
    "san_pham_da_hoi, da_mua, cay_trong, cach_xung_ho, last_topic). Khách quen/gặp lại: chào ấm áp, nhắc tự nhiên "
    "MỘT chi tiết ngắn nếu hợp, không đọc lại lịch sử. Khách lạ: đón tự nhiên, không theo kịch bản, không giả quen. "
    "Không bao giờ nhắc tới khách khác. sales_playbook là cách xử lý khéo các câu khó; tinh_huong_phu_hop (nếu có) "
    "là id đang hợp. Gặp phản đối/thử thách: đừng đáp cụt 'không'; nhận thiện chí, xoay vui và khéo về hướng "
    "mua bằng dữ kiện thật trong duoc_dung/thong_tin_shop, tuân thủ cam, mỗi lần một cách nói."
)


def stable_context(override_present=False):
    """Phần CỐ ĐỊNH của sổ tay cho nền prompt: {"sales_playbook": [...]} (+ chính sách khi có lời nhắc riêng).

    Cờ playbook/customer_card tắt hoặc lỗi => {} (hành vi cũ).
    """
    out = {}
    try:
        import context_budget
        if context_budget.enabled("playbook"):
            view = stable_view()
            if view:
                out["sales_playbook"] = view
        if override_present and (out or context_budget.enabled("customer_card")):
            out["the_khach_so_tay_policy"] = POLICY_TEXT
    except Exception:  # noqa: BLE001
        _log.warning("sales playbook context failed", exc_info=True)
        return {}
    return out


def hint_ids(texts, acts=()):
    """tinh_huong_phu_hop cho lượt này: id khớp ở BẤT KỲ câu nào trong `texts` (gốc + đã giải mã)."""
    try:
        import context_budget
        if not context_budget.enabled("playbook"):
            return []
        ids = []
        for text in texts:
            for found in match(text, acts):
                if found not in ids:
                    ids.append(found)
        return ids[:3]
    except Exception:  # noqa: BLE001
        return []


_PROMO_PITCH = re.compile(r"(?:tiktok|shopee)\s+(?:dang\s+)?ho tro ma|ma (?:giam|uu dai|cua (?:tiktok|shopee))|ma lon", re.I)


def promo_already_pitched(turns):
    """Đã nhắc mã nền tảng cho khách này (trong các lượt gần đây đã gửi) -> không nhắc lại."""
    try:
        from knowledge import fold
        return any(isinstance(t, dict) and t.get("delivery_status") == "sent"
                   and _PROMO_PITCH.search(fold(t.get("response") or "")) for t in turns or [])
    except Exception:  # noqa: BLE001
        return False


def add_card_and_hint(context, card, texts, acts=(), turns=None):
    """Phần theo lượt nhỏ xíu cho MỌI làn/nền tảng: the_khach + tinh_huong_phu_hop. Không bao giờ ném lỗi."""
    try:
        if card:
            context["the_khach"] = card
        hint = hint_ids(texts, acts)
        if hint:
            context["tinh_huong_phu_hop"] = hint
        if promo_already_pitched(turns):
            context["da_nhac_ma"] = "Đã nhắc mã nền tảng với khách này rồi: lần này đừng nhắc lại, chỉ nói giá/chốt đơn."
    except Exception:  # noqa: BLE001
        _log.warning("card/hint failed", exc_info=True)
    return context
