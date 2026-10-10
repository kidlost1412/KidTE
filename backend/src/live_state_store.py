"""Crash-safe persistence of live room/lock hints. A restored lock is never trusted."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

PLATFORMS = ('tiktok', 'shopee')


def default_path():
    from config import ROOT
    return Path(ROOT) / 'data' / 'machine' / 'live_state.json'


def _empty():
    return {'version': 1, 'platform': {}}


def load(path=None):
    path = Path(path) if path else default_path()
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if (not isinstance(data, dict) or data.get('version') != 1
                or not isinstance(data.get('platform'), dict)):
            return _empty()
        return {'version': 1, 'platform': {k: v for k, v in data['platform'].items()
                                           if k in PLATFORMS and isinstance(v, dict)}}
    except (OSError, ValueError):
        return _empty()


def _write(data, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f'.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    os.replace(tmp, path)


def save_platform(platform, entry, path=None):
    """Replace one platform entry, keeping the others."""
    path = Path(path) if path else default_path()
    data = load(path)
    data['platform'][platform] = entry
    _write(data, path)


def get_platform(platform, path=None):
    return load(path)['platform'].get(platform) or {}


def fresh_lock(entry, library_revision, max_age_s=600, now=None):
    """Return the persisted lock only if recent, same library revision and well formed."""
    now = time.time() if now is None else now
    try:
        lock = entry.get('lock')
        if not lock or not entry.get('locked'):
            return None
        age = now - float(entry['locked_at'])
        if age < 0 or age > max_age_s:
            return None
        if str(lock.get('library_revision')) != str(library_revision):
            return None
        if not lock.get('video_id') or float(lock['duration_s']) <= 0:
            return None
        return {**lock, 'age_s': age}
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def fresh_obs_binding(entry, input_name, identity, max_age_s=600, now=None):
    """Persisted Shopee OBS binding if same input + same (path,size,mtime_ns,library_revision) and recent."""
    now = time.time() if now is None else now
    try:
        bind = entry.get('obs_binding')
        if not isinstance(bind, dict) or not bind.get('verified_video_id'):
            return None
        if bind.get('input_name') != input_name:
            return None
        saved = bind.get('identity')
        if not isinstance(saved, (list, tuple)) or [str(x) for x in saved] != [str(x) for x in identity]:
            return None
        age = now - float(bind['measured_at'])
        if age < 0 or age > max_age_s:
            return None
        return bind
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
