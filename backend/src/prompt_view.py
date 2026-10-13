"""Xem toàn bộ lời nhắc gửi cho AI + sửa luật bán hàng gốc một cách an toàn khi cập nhật.

Thứ tự thật của system prompt: (1) APP_BRIEF  (2) luật bán hàng gốc  (3) ghi chú thêm  (4) phong cách người trực
(phong cách đi kèm dữ liệu từng lượt, nhưng hiển thị cùng để chủ shop thấy toàn cảnh).

Luật gốc đi kèm app (prompts/system_prompt.txt) luôn được cập nhật. Chủ shop có thể lưu BẢN CỦA MÌNH
(data/quy_tac_ban_hang.txt) kèm mã băm của bản gốc mà bản đó dựa trên (quy_tac_ban_hang.meta.json). Khi app cập
nhật làm bản gốc đổi mã băm, bản của chủ shop vẫn được dùng nhưng giao diện hiện thông báo để chọn:
dùng bản mới / giữ bản của tôi / gộp tay. Các dòng hợp đồng JSON/định dạng đầu ra không xóa được:
thiếu dòng nào thì tự nối lại để câu trả lời không hỏng.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
from datetime import datetime, timezone

from ai_context import APP_BRIEF
from config import DATA_DIR

OVERRIDE_PATH = DATA_DIR / "quy_tac_ban_hang.txt"
META_PATH = DATA_DIR / "quy_tac_ban_hang.meta.json"
MIN_CHARS = 300
MAX_CHARS = 40000
APPENDED_HEADER = "ĐỊNH DẠNG BẮT BUỘC (ứng dụng tự thêm lại vì bị xóa):"

# (dòng dùng để kiểm tra, văn bản nối lại nếu thiếu). Kiểm tra bỏ qua khác biệt khoảng trắng.
PROTECTED = (
    ("- Luôn trả đúng một JSON object. Đủ căn cứ:", "- Luôn trả đúng một JSON object. Đủ căn cứ:"),
    ('{"action":"reply","reply":"..."}', '  {"action":"reply","reply":"..."}'),
    ('{"action":"skip","missing_fields":["..."]}',
     '  Thiếu dữ kiện cam kết (giá, quà tặng/khuyến mãi, mã giảm giá, giao hàng):\n'
     '  {"action":"skip","missing_fields":["..."]}'),
    ("- Phần reply phải nằm trong giới hạn ký tự được cấp và không chứa @username.",
     "- Phần reply phải nằm trong giới hạn ký tự được cấp và không chứa @username."),
)


def norm(text):
    return str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def text_hash(text):
    return hashlib.sha256(norm(text).encode("utf-8")).hexdigest()


def _squash(text):
    return re.sub(r"\s+", "", text)


def missing_protected(text):
    squashed = _squash(text)
    return [append for check, append in PROTECTED if _squash(check) not in squashed]


def ensure_protected(text):
    """(text, số dòng hợp đồng đã nối lại)."""
    text = norm(text)
    missing = missing_protected(text)
    if not missing:
        return text, 0
    return text + "\n\n" + APPENDED_HEADER + "\n" + "\n".join(missing), len(missing)


def validate(text):
    text = norm(text)
    if len(text) < MIN_CHARS:
        raise ValueError("base_rules_too_short")
    if len(text) > MAX_CHARS:
        raise ValueError("base_rules_too_long")
    return text


def _read(path):
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def load_override():
    """{"text", "base_hash", "saved_at"} hoặc None nếu chưa có bản của chủ shop."""
    text = norm(_read(OVERRIDE_PATH))
    if not text:
        return None
    try:
        meta = json.loads(_read(META_PATH) or "{}")
    except ValueError:
        meta = {}
    meta = meta if isinstance(meta, dict) else {}
    return {"text": text, "base_hash": str(meta.get("base_hash") or ""), "saved_at": str(meta.get("saved_at") or "")}


def status(shipped):
    current = text_hash(shipped)
    mine = load_override()
    return {"customized": bool(mine), "base_hash": current,
            "derived_hash": mine["base_hash"] if mine else "",
            "stale": bool(mine) and mine["base_hash"] != current,
            "saved_at": mine["saved_at"] if mine else ""}


def base_rules_text(shipped):
    """Luật gốc THỰC DÙNG: bản của chủ shop (đã bảo đảm có dòng hợp đồng) hoặc bản đi kèm app."""
    mine = load_override()
    if not mine:
        return norm(shipped)
    return ensure_protected(mine["text"])[0]


def _write_meta(base_hash):
    META_PATH.parent.mkdir(parents=True, exist_ok=True)
    META_PATH.write_text(json.dumps({"base_hash": base_hash,
                                     "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
                                    ensure_ascii=False), encoding="utf-8")


def save_override(text, shipped):
    """Lưu bản của chủ shop, đóng dấu mã băm bản gốc hiện tại. Trả {"text", "appended"}."""
    text = validate(text)
    final, appended = ensure_protected(text)
    if len(final) > MAX_CHARS:
        raise ValueError("base_rules_too_long")
    OVERRIDE_PATH.parent.mkdir(parents=True, exist_ok=True)
    OVERRIDE_PATH.write_text(final + "\n", encoding="utf-8")
    _write_meta(text_hash(shipped))
    return {"text": final, "appended": appended}


def reset():
    for path in (OVERRIDE_PATH, META_PATH):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def resolve(action, shipped):
    """Xử lý thông báo 'Quy tắc gốc vừa được cập nhật'. 'Gộp tay' = lưu bản đã sửa qua save_override."""
    if action == "use_new":
        reset()
    elif action == "keep_mine":
        if load_override() is None:
            raise ValueError("base_rules_no_override")
        _write_meta(text_hash(shipped))
    else:
        raise ValueError("base_rules_action_unknown")


def diff_rows(mine, new):
    """So sánh hai bên theo dòng: [{kind: same|changed|removed|added, left, right}] (trái = của tôi, phải = bản mới)."""
    a, b = norm(mine).split("\n"), norm(new).split("\n")
    rows = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            rows += [{"kind": "same", "left": x, "right": x} for x in a[i1:i2]]
            continue
        left, right = a[i1:i2], b[j1:j2]
        for k in range(max(len(left), len(right))):
            l = left[k] if k < len(left) else None
            r = right[k] if k < len(right) else None
            kind = "changed" if l is not None and r is not None else "removed" if l is not None else "added"
            rows.append({"kind": kind, "left": l or "", "right": r or ""})
    return rows


def build_view(*, shipped, notes, notes_header, notes_max, persona, voice, builtin_voice, style_max):
    """Các phần lời nhắc theo đúng thứ tự gửi. `voice` = phong cách THỰC GỬI của persona."""
    st = status(shipped)
    mine = load_override()
    effective = base_rules_text(shipped)
    is_default = bool(persona) and persona.get("persona_id") == "default"
    sections = [
        {"id": "brief", "title": "1) Bối cảnh Kid TE", "text": APP_BRIEF.rstrip(), "editable": False,
         "note": "Cố định trong ứng dụng, luôn đứng đầu mọi lời gọi AI. Không sửa được."},
        {"id": "rules", "title": "2) Quy tắc bán hàng", "text": effective, "editable": True,
         "customized": st["customized"], "stale": st["stale"], "max": MAX_CHARS, "min": MIN_CHARS,
         "protected": [check for check, _ in PROTECTED],
         "note": "Bản của bạn đang được dùng." if st["customized"]
                 else "Bản gốc đi kèm ứng dụng, được cập nhật cùng ứng dụng."},
        {"id": "notes", "title": "3) Ghi chú thêm", "text": notes, "header": notes_header, "editable": True,
         "max": notes_max, "note": "Nối sau luật bán hàng; không được trái luật giá và dữ kiện cứng."},
        {"id": "persona", "title": "4) Phong cách người trực", "text": voice, "editable": True, "max": style_max,
         "persona_id": (persona or {}).get("persona_id", ""), "persona_name": (persona or {}).get("name", ""),
         "is_default": is_default, "builtin_voice": builtin_voice if is_default else "",
         "note": ("Giọng dựng sẵn + phần đã học. Sửa xong sẽ lưu thành phong cách riêng của bạn (giọng mặc định không bị đổi)."
                  if is_default else "Giọng của người này; sửa và lưu ngay tại đây."),
         "delivery": "Phong cách đi kèm dữ liệu của từng lượt (phong_cach_nguoi_truc), không nằm trong system."},
    ]
    notice = None
    if st["stale"] and mine:
        notice = {"title": "Quy tắc gốc vừa được cập nhật", "rows": diff_rows(mine["text"], shipped),
                  "new_text": norm(shipped), "mine_text": mine["text"]}
    return {"ok": True, "sections": sections, "rules_status": st, "notice": notice}
