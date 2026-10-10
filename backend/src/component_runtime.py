"""Resolve installed optional executables from the committed generation only."""
from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path

from component_store import ComponentStore, ComponentStoreError, _check_ancestry
from update_catalog import COMPONENT_POLICIES, _safe_path

_VERIFIED = {}
_LOCK = threading.RLock()


def component_path(component_id: str, relative_path: str) -> Path | None:
    if component_id not in COMPONENT_POLICIES or not _safe_path(relative_path):
        raise ComponentStoreError("invalid component resource")
    root = os.environ.get("KIDTE_APP_ROOT", "").strip()
    if not root:
        return None
    app_root = Path(root).absolute()
    active = ComponentStore(app_root).load_active()
    selected = (active or {}).get("components", {}).get(component_id)
    if selected is None:
        return None
    record = next((row for row in selected["files"] if row["path"] == relative_path), None)
    if record is None:
        raise ComponentStoreError("component resource is not in signed inventory")
    target = app_root / selected["path"] / relative_path
    _check_ancestry(target)
    if not target.is_file() or target.stat().st_size != record["size"]:
        raise ComponentStoreError("component resource is missing or damaged")
    before = target.stat()
    stamp = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
    cache_key = (str(target), record["sha256"], active["generation"])
    with _LOCK:
        if _VERIFIED.get(cache_key) == stamp:
            return target
    digest = hashlib.sha256()
    with target.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != record["sha256"]:
        raise ComponentStoreError("component resource digest mismatch")
    after = target.stat()
    if stamp != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise ComponentStoreError("component resource changed during verification")
    with _LOCK:
        _VERIFIED[cache_key] = stamp
    return target
