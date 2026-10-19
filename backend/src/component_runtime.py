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
    return _verified_file(app_root, selected, active["generation"], record)


def _verified_file(app_root: Path, selected: dict, generation, record: dict) -> Path:
    relative_path = record["path"]
    target = app_root / selected["path"] / relative_path
    _check_ancestry(target)
    if not target.is_file() or target.stat().st_size != record["size"]:
        raise ComponentStoreError("component resource is missing or damaged")
    before = target.stat()
    stamp = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
    cache_key = (str(target), record["sha256"], generation)
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


_CODE_SUFFIXES = {".exe", ".dll", ".pyd", ".ps1", ".bat", ".cmd", ".vbs", ".js", ".py", ".sys", ".com", ".scr"}


def verify_component_tree(component_id: str, prefix: str) -> Path | None:
    """Hash-check EVERY signed-inventory file under ``prefix`` and refuse planted code.

    Used before running a component with elevated rights: the executable alone
    is not enough, because it loads DLLs/.pyd/scripts from the same writable
    tree. Returns the package directory (``<app>/<path>/<prefix>``), or None when
    the component is not installed from a signed package. Raises
    ComponentStoreError on any missing/changed file or any unlisted code file.
    """
    prefix = prefix.strip("/")
    root = os.environ.get("KIDTE_APP_ROOT", "").strip()
    if not root or not _safe_path(prefix):
        return None
    app_root = Path(root).absolute()
    active = ComponentStore(app_root).load_active()
    selected = (active or {}).get("components", {}).get(component_id)
    if selected is None:
        return None
    records = {row["path"]: row for row in selected["files"]
               if row["path"] == prefix or row["path"].startswith(prefix + "/")}
    if not records:
        raise ComponentStoreError("component resource is not in signed inventory")
    wanted = set(records)
    for relative in sorted(records):
        _verified_file(app_root, selected, active["generation"], records[relative])
    base = app_root / selected["path"] / prefix
    known = {os.path.normcase(str(app_root / selected["path"] / item)) for item in wanted}
    for found in base.rglob("*"):
        if found.suffix.lower() in _CODE_SUFFIXES and os.path.normcase(str(found)) not in known:
            raise ComponentStoreError("component folder contains a file that is not in the signed inventory")
    return base
