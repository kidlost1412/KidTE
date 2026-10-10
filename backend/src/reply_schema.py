"""Normalise the main model turn into one schema; map depends_on onto fact_revision.

Schema: {intent, evidence:[{source,ref}], fields:{}, depends_on:{price_rev,catalog_rev,video_rev},
messages:[], awaiting_fields:[]}. Legacy outputs (reply/messages only) still parse.
Pure helpers; fact_revision stays the only revision mechanism.
"""
from __future__ import annotations

import fact_revision

DEPENDS_KEYS = ('price_rev', 'catalog_rev', 'video_rev')


def normalize(result):
    """Return {'ok': True, **schema} or {'ok': False, 'reason': ...}."""
    if not isinstance(result, dict):
        return {'ok': False, 'reason': 'not_object'}
    evidence = result.get('evidence', [])
    if evidence is None:
        evidence = []
    # Trích dẫn sai định dạng chỉ bị bỏ (kèm cờ), không làm mất cả câu trả lời của khách.
    raw_evidence = evidence if isinstance(evidence, list) else []
    evidence = [item for item in raw_evidence
                if isinstance(item, dict) and isinstance(item.get('source'), str) and item['source'].strip()
                and isinstance(item.get('ref'), (str, int)) and not isinstance(item.get('ref'), bool)
                and str(item['ref']).strip()]
    evidence_dropped = not isinstance(result.get('evidence', []), (list, type(None))) or len(evidence) != len(raw_evidence)
    messages = result.get('messages')
    if messages is None:
        reply = result.get('reply')
        messages = [reply] if isinstance(reply, str) and reply.strip() else []
    if not isinstance(messages, list):
        return {'ok': False, 'reason': 'invalid_messages'}
    awaiting = result.get('awaiting_fields')
    awaiting = [a for a in awaiting if isinstance(a, str)] if isinstance(awaiting, list) else []
    fields = result.get('fields') if isinstance(result.get('fields'), dict) else {}
    raw = result.get('depends_on') if isinstance(result.get('depends_on'), dict) else {}
    depends = {k: raw.get(k) for k in DEPENDS_KEYS}
    intent = result.get('intent')
    return {'ok': True, 'intent': intent if isinstance(intent, str) else '',
            'evidence': [{'source': i['source'], 'ref': str(i['ref'])} for i in evidence],
            'fields': fields, 'depends_on': depends, 'messages': messages, 'awaiting_fields': awaiting,
            'evidence_dropped': evidence_dropped}


def revision_flags(schema):
    """depends_on -> fact_revision.build kwargs (the existing revision record)."""
    dep = (schema or {}).get('depends_on') or {}
    return {'video_dependent': bool(dep.get('video_rev'))}


def stale_reason(schema, stored, now):
    """Reuse fact_revision.stale_reason; price/catalog revisions are the row hash."""
    return fact_revision.stale_reason(stored, now)


def settle(compose, stored_now):
    """compose() -> schema; stored_now(schema) -> (stored, now). One recompose max.

    Returns (schema, status) with status 'ok' or 'needs_review'.
    """
    schema = compose()
    stored, now = stored_now(schema)
    if not stale_reason(schema, stored, now):
        return schema, 'ok'
    schema = compose()
    stored, now = stored_now(schema)
    return schema, ('ok' if not stale_reason(schema, stored, now) else 'needs_review')


def safe_normalize(result):
    """Tolerant wrapper: any failure or malformed output -> None (reply path continues unchanged)."""
    try:
        schema = normalize(result)
    except Exception:
        return None
    return schema if schema.get('ok') else None


def annotate_revision(revision, schema):
    """Fold the model's depends_on into the B06 revision record (additive, tolerant).

    video_rev -> video_dependent (OR-ed, never cleared); depends_on and evidence are kept for audit.
    """
    if not isinstance(revision, dict) or not schema:
        return revision
    flags = revision_flags(schema)
    out = dict(revision)
    out['video_dependent'] = bool(out.get('video_dependent')) or flags['video_dependent']
    out['depends_on'] = {k: v for k, v in (schema.get('depends_on') or {}).items() if v}
    if schema.get('evidence'):
        out['evidence'] = list(schema['evidence'])
    return out


def audit_metrics(metrics, schema):
    """Evidence/depends_on onto interaction metrics (additive copy)."""
    out = dict(metrics) if isinstance(metrics, dict) else {}
    if schema:
        out['reply_evidence'] = list(schema.get('evidence') or [])
        out['reply_depends_on'] = {k: v for k, v in (schema.get('depends_on') or {}).items() if v}
    return out
