"""Chọn quy tắc liều (D05) và làm rõ SKU (A04): hàm thuần dùng chung cho Shopee và TikTok."""
from __future__ import annotations

import json
import re

DOSE_UNITS = {"ml", "g", "kg"}
LITRE_WORDS = {"l", "lit", "lít", "litre", "liter"}
DOSE_HANDOFFS = (
    "Liều này em muốn hỏi lại chủ vườn cho chắc rồi báo mình liền nha.",
    "Phần liều em kiểm lại cho chuẩn rồi trả lời mình ngay, đợi em xíu nha.",
    "Liều cụ thể em xác nhận lại cho đúng rồi nhắn mình liền nha.",
    "Cái liều này em coi lại kỹ rồi báo mình sau ít phút nha.",
    "Để em hỏi lại liều chính xác cho mình, chút xíu em trả lời liền nha.",
)
DOSE_HANDOFF = DOSE_HANDOFFS[0]


def dose_handoff(rng=None):
    """Một biến thể câu chuyển liều, chọn ngẫu nhiên để không gửi một câu nguyên văn mãi (C14)."""
    import random
    return (rng or random).choice(DOSE_HANDOFFS)
_DOSE_NUM = re.compile(r"(\d+(?:[.,]\d+)?)(?:\s*[-–]\s*(\d+(?:[.,]\d+)?))?\s*(?:ml|kg|gam|gram|g)\b", re.I)


def _rule_text(rule):
    return f"Pha {float(rule['amount']):g}{rule['unit']} cho bình {float(rule['per_volume']):g} lít nước"


def usable_dose_rules(rules, sku):
    """Only approved, same-SKU, single-component, per-litre rules may be offered or used."""
    out = {}
    for rule in rules or []:
        try:
            if (rule.get("status") == "approved" and rule.get("sku") == sku and not rule.get("combo_components")
                    and str(rule.get("unit", "")).lower() in DOSE_UNITS
                    and str(rule.get("per_unit") or "").lower() in (LITRE_WORDS | {""})
                    and float(rule["amount"]) > 0 and float(rule["per_volume"]) > 0):
                out[rule["rule_id"]] = rule
        except (KeyError, TypeError, ValueError):
            continue
    return out


def verify_dose_selection(result, usable, comment_liters):
    """Return (rule, volume, error) for the model's {rule_id, params:{volume, unit}}."""
    sel = result.get("dose_selection") if isinstance(result, dict) and isinstance(result.get("dose_selection"), dict) else result
    if not isinstance(sel, dict):
        return None, 0, "Không có rule_id."
    rule = usable.get(sel.get("rule_id"))
    if rule is None:
        return None, 0, "rule_id không nằm trong danh sách rule đã duyệt của sản phẩm này."
    params = sel.get("params") if isinstance(sel.get("params"), dict) else {}
    unit = str(params.get("unit", "")).strip().lower()
    if unit not in LITRE_WORDS:
        return None, 0, "params.unit phải là lít (l)."
    try:
        volume = float(str(params.get("volume")).replace(",", "."))
    except (TypeError, ValueError):
        return None, 0, "params.volume không phải số."
    if volume <= 0 or abs(volume - comment_liters) > 1e-9:
        return None, 0, f"params.volume phải đúng bình khách hỏi ({comment_liters:g} lít)."
    return rule, volume, ""


def reply_dose_numbers_ok(reply, rows):
    allowed = set()
    for row in rows:
        for key in ("calculated_ml_min", "calculated_ml_max"):
            allowed.add(round(float(row[key]), 2))
    for match in _DOSE_NUM.finditer(reply or ""):
        for part in match.groups():
            if part is not None and round(float(part.replace(",", ".")), 2) not in allowed:
                return False
    return True


DOSE_RULE_NOTE = ('Chọn đúng một rule_id trong dose_rule_options. Trả JSON {"rule_id":..., '
                  '"params":{"volume":<số lít bình khách hỏi>,"unit":"l"}}. Không tự tính liều.')


def select_dose_rule(call, proc, usable, liters, platform, conversation, context):
    """Model picks rule_id; code verifies and computes. One correction, then handoff.

    call(ask, correction) -> model result dict. Returns {'rows': [...]} or {'handoff': True}.
    """
    options = [{'rule_id': rid, 'text': _rule_text(rule)} for rid, rule in usable.items()]
    ask = {'platform': platform, 'ask': 'select_dose_rule', 'dose_rule_options': options,
           'conversation': conversation, 'rule': DOSE_RULE_NOTE}
    correction = ''
    if len(usable) == 1:
        # Rà soát 17/10 C2: chỉ MỘT quy tắc dùng được thì không cần model chọn (tiết kiệm 1–4 giây).
        # Vẫn qua verify_dose_selection + tính bằng mã như đường model.
        only = next(iter(usable))
        rule, volume, _error = verify_dose_selection(
            {'rule_id': only, 'params': {'volume': liters, 'unit': 'l'}}, usable, liters)
        rows = proc.verified_dose_calculations(f"bình {volume:g} lít",
            {'huong_dan_su_dung': [_rule_text(rule)], 'dose_extrapolation_rule': 'nearest_endpoint'},
            dose_intent=True) if rule else None
        if rows:
            context['verified_dose_calculations'] = rows
            context['dose_rule_selected'] = {'rule_id': rule['rule_id'], 'text': _rule_text(rule)}
            return {'rows': rows, 'auto_selected': True}
    for _attempt in range(2):
        result = call(ask, correction)
        if isinstance(result, dict) and isinstance(result.get('reply'), str) and 'rule_id' not in result:
            try:
                parsed = json.loads(result['reply'])
                if isinstance(parsed, dict):
                    result = {**result, **parsed}
            except ValueError:
                pass
        rule, volume, error = verify_dose_selection(result, usable, liters)
        if rule:
            rows = proc.verified_dose_calculations(f"bình {volume:g} lít",
                {'huong_dan_su_dung': [_rule_text(rule)], 'dose_extrapolation_rule': 'nearest_endpoint'}, dose_intent=True)
            if rows:
                context['verified_dose_calculations'] = rows
                context['dose_rule_selected'] = {'rule_id': rule['rule_id'], 'text': _rule_text(rule)}
                return {'rows': rows}
            error = 'Không tính được liều từ rule này.'
        correction = error + ' Chọn lại từ dose_rule_options.'
    context.pop('verified_dose_calculations', None)
    return {'handoff': True}


def clarify_count_for(counts, family_id):
    value = (counts or {}).get(family_id, 0) if isinstance(counts, dict) else 0
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def clarify_listing(titles, tag_len, template, limit=150):
    """Top-3 list that fits one message; falls back to the template when even one title is too long."""
    from delivery_store import utf16_length
    from knowledge import clean
    raw = [clean(t, 160) for t in titles if clean(t, 160)][:3]

    def text(rows):
        if len(rows) == 1:
            return f"Mình hỏi {rows[0]} phải không ạ?"
        return f"Shop đang có: {'; '.join(rows)}. Mình chọn loại nào ạ?"

    def short(title, width):
        if len(title) <= width:
            return title
        cut = title[:width].rsplit(' ', 1)[0]
        return (cut or title[:width]).rstrip(' ,-')
    # Rút gọn tên trước khi bớt ứng viên: khách cần thấy 2–3 lựa chọn cụ thể,
    # câu mẫu trơn chỉ là đường cùng khi không còn tên nào vừa khung.
    for width in (40, 28, 20, 14):
        rows = [short(t, width) for t in raw]
        while len(rows) > 2 and utf16_length(text(rows)) + tag_len > limit:
            rows = rows[:-1]
        if rows and utf16_length(text(rows)) + tag_len <= limit:
            return text(rows)
    return template
