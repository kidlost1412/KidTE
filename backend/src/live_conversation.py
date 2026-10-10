"""Resolve a customer's short answer to the question still being discussed."""
import re
from knowledge import KnowledgeBase, clean, fold

AWAITING_FIELDS = frozenset({'product', 'variant', 'quantity', 'crop', 'stage', 'method', 'tank_volume'})


def _with_clarify(state, clarify_count):
    counts = {clean(k, 160): v for k, v in (clarify_count or {}).items() if isinstance(v, int) and v > 0} if isinstance(clarify_count, dict) else {}
    if counts:
        state['clarify_count'] = counts
    return state


def conversation_state(awaiting_fields=None, *, product_id='', observed_pin='', product_family='', candidate_skus=None, clarify_count=None):
    fields = awaiting_fields if isinstance(awaiting_fields, list) else []
    return _with_clarify({'awaiting_fields': list(dict.fromkeys(field for field in fields
        if isinstance(field, str) and field in AWAITING_FIELDS)),
        'selected_sku': clean(product_id, 160), 'observed_pin': clean(observed_pin, 160),
        'product_family': clean(product_family, 160),
        'candidate_skus': list(dict.fromkeys(clean(item, 160) for item in (candidate_skus or [])
            if isinstance(item, str) and item.strip()))[:100] if isinstance(candidate_skus, list) else []}, clarify_count)


def resolve_question(comment, turns):
    """Inherit intent only from the latest unanswered/clarifying turn.

    Callers supply turns from the same customer and LIVE room. A new question
    takes precedence; greetings and acknowledgements never resume an old one.
    """
    current = clean(comment, 600)
    normalized = fold(current)
    # A fresh request for a product range must not inherit an earlier price
    # question merely because it is short and omits punctuation.
    if re.search(r"\b(?:co|ban)\b.{0,80}\b(?:loai nao|nhung loai|cac loai|loai gi)\b", normalized):
        return current, None
    if turns and len(current.split()) <= 6:
        state = turns[0].get('conversation_state') or {}
        volume_followup = re.match(r'^(?:(?:loai|chai|can)\s+)?\d+(?:[.,]\d+)?\s*(?:ml|l|lit|kg|g)\b(.*)$', normalized)
        followup_words = {'gia', 'bao', 'nhieu', 'nhiu', 'bn', 'sao', 'the', 'nao', 'a', 'ha',
                          'nha', 'em', 'anh', 'chi', 'minh', 'lay', 'do', 'nhe', 'di', 'vay'}
        short_variant = volume_followup and not (set(re.findall(r'[a-z0-9]+', volume_followup[1])) - followup_words)
        if (isinstance(state, dict) and isinstance(state.get('candidate_skus'), list) and state['candidate_skus'] and
                isinstance(state.get('product_family'), str) and state['product_family'] and
                short_variant):
            return f"{clean(state['product_family'], 160)} {current}", turns[0]
    if not turns or len(current.split()) > 6 or any(KnowledgeBase.intents(current).values()):
        return current, None
    # Câu chào/cảm ơn ("chào shop", "ok em nhé") không mở lại câu hỏi cũ.
    ack_words = {'ok', 'oke', 'okie', 'da', 'vang', 'cam', 'on', 'hello', 'hi', 'chao', 'xin',
                 'shop', 'sop', 'em', 'anh', 'chi', 'ban', 'moi', 'nguoi', 'ca', 'nha', 'nhe', 'a', 'ah'}
    ack_tokens = re.findall(r'[a-z0-9]+', fold(current))
    if ack_tokens and not set(ack_tokens) - ack_words and set(ack_tokens) & {'ok', 'oke', 'okie', 'da', 'vang', 'cam', 'hello', 'hi', 'chao'}:
        return current, None
    previous = turns[0]
    pending = clean(previous.get('resolved_question') or previous.get('comment'), 1000)
    answered = previous.get('delivery_status') == 'sent'
    state = previous.get('conversation_state') or {}
    clarification = isinstance(state, dict) and bool(conversation_state(
        state.get('awaiting_fields'))['awaiting_fields'])
    if pending and any(KnowledgeBase.intents(pending).values()) and (not answered or clarification):
        return f'{pending} — {current}', previous
    return current, None
