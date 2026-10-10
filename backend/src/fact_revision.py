"""Facts a reply depended on, re-checked when the first message is really sent.

Pure helpers: no store, platform or model access. The hash covers only the
catalog rows the reply named, so unrelated catalog churn never invalidates it.
"""
from __future__ import annotations

import hashlib
import json

VERSION = 1
# Change constantly and are removed from the model prompt too.
VOLATILE_KEYS = {'stock', 'stock_num', 'is_pinned', 'synced_at'}


def rows_hash(rows):
    clean = sorted(
        (json.dumps({k: v for k, v in row.items() if k not in VOLATILE_KEYS},
                    ensure_ascii=False, sort_keys=True, default=str)
         for row in rows if isinstance(row, dict)))
    return hashlib.sha256('\n'.join(clean).encode('utf-8')).hexdigest()


def build(rows_by_id, *, product_ids, pin_id='', pin_dependent=False, video_dependent=False,
          explicit=False, lock_id='', revision=0):
    ids = sorted({str(i) for i in product_ids if i in rows_by_id})
    return {
        'v': VERSION, 'hash': rows_hash([rows_by_id[i] for i in ids]), 'product_ids': ids,
        'pin_id': pin_id or '', 'pin_dependent': bool(pin_dependent),
        'video_dependent': bool(video_dependent), 'explicit': bool(explicit),
        'lock_id': lock_id or '', 'revision': revision or 0,
    }


def current(stored, rows_by_id, *, pin_id='', lock_id='', revision=0):
    rows = [rows_by_id[i] for i in stored.get('product_ids', []) if i in rows_by_id]
    return {'hash': rows_hash(rows), 'pin_id': pin_id or '',
            'lock_id': lock_id or '', 'revision': revision or 0}


def stale_reason(stored, now):
    """'' when the reply is still valid, else why it matters."""
    if not isinstance(stored, dict) or stored.get('v') != VERSION or not isinstance(now, dict):
        return ''
    if stored.get('hash') != now.get('hash'):
        return 'catalog'
    if stored.get('pin_dependent') and stored.get('pin_id') != now.get('pin_id'):
        return 'pin'
    # A product the customer named survives the host moving to another video.
    if (stored.get('video_dependent') and not stored.get('explicit')
            and (stored.get('lock_id'), stored.get('revision')) != (now.get('lock_id'), now.get('revision'))):
        return 'video'
    return ''
