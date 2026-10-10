"""Mô hình tham chiếu sản phẩm thống nhất cho TikTok và Shopee (hàm thuần)."""
from __future__ import annotations

import re
import unicodedata

AMBIGUITY_GAP = 0.1
SELECT_MIN_SCORE = 0.6


def fold(value):
    text = unicodedata.normalize('NFD', str(value or '').replace('đ', 'd').replace('Đ', 'D'))
    text = ''.join(ch for ch in text if unicodedata.category(ch) != 'Mn')
    return ' '.join(re.sub(r'[^a-z0-9]+', ' ', text.casefold()).split())


def _contains(haystack, needle):
    return bool(needle) and f' {needle} ' in f' {haystack} '


def _sku_of(item):
    return str(item.get('sku') or item.get('product_id') or '').strip()


def match_alias(text, catalog):
    """Trả danh sách (sku, score, alias) cho sản phẩm có tên/bí danh xuất hiện trong text."""
    low = fold(text)
    found = []
    for item in catalog or []:
        sku = _sku_of(item)
        if not sku:
            continue
        best, best_alias = 0.0, ''
        aliases = [a for a in (item.get('aliases') or []) if isinstance(a, str)]
        for alias in aliases:
            folded = fold(alias)
            if _contains(low, folded) and 1.0 > best:
                best, best_alias = 1.0, alias
        title = fold(item.get('title'))
        if title and _contains(low, title) and best < 1.0:
            best, best_alias = 1.0, str(item.get('title'))
        if not best_alias and title:
            tokens = [t for t in title.split() if len(t) > 1 and not t.isdigit()]
            hits = [t for t in tokens if _contains(low, t)]
            if tokens and hits:
                score = round(0.9 * len(hits) / len(tokens), 4)
                if score > best:
                    best, best_alias = score, ' '.join(hits)
        if best > 0:
            found.append((sku, best, best_alias))
    return found


def build_product_ref(platform, catalog, comment, pin=None, prior_state=None, customer_facts=None):
    prior = prior_state if isinstance(prior_state, dict) else {}
    matches = sorted(match_alias(comment, catalog), key=lambda row: (-row[1], row[0]))
    candidates = [{'sku': sku, 'score': score, 'source': 'comment_alias'} for sku, score, _ in matches]
    aliases = []
    for _, _, alias in matches:
        if alias and alias not in aliases:
            aliases.append(alias)
    selected = None
    if candidates:
        top = candidates[0]
        runner = candidates[1]['score'] if len(candidates) > 1 else None
        if top['score'] >= SELECT_MIN_SCORE and (runner is None or top['score'] - runner > AMBIGUITY_GAP):
            selected = top['sku']
    elif prior.get('selected_sku'):
        selected = prior['selected_sku']
    observed = None
    if isinstance(pin, dict) and pin.get('sku'):
        observed = {'sku': str(pin['sku']), 'observed_at': pin.get('observed_at'),
                    'confidence': pin.get('confidence')}
    family_id = prior.get('family_id') or ''
    if not family_id:
        try:
            from shopee_commerce import product_family_candidates
            family_id = product_family_candidates(comment, catalog or [])[0]
        except Exception:
            family_id = ''
    return {'platform': str(platform or ''), 'family_id': family_id, 'selected_sku': selected,
            'candidates': candidates, 'observed_pin': observed, 'aliases_matched': aliases,
            'customer_facts': dict(customer_facts or {})}
