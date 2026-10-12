"""Keep the stable Chrome extension folders in step with the active package.

Chrome's "Load unpacked" points at ``App\\extension`` and ``App\\extension-shopee``.
Those paths never change; the launcher refreshes their *contents* from the
active ``extensions`` package before it starts Kid TE. When the package did not
change nothing is written, so Chrome needs no reload.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

from component_store import _check_ancestry, _is_reparse

MARKER = "extensions-sync.json"
NOTICE = "extensions-notice.json"  # pending "reload in chrome://extensions" notice, cleared by the app
TOP_DIRS = ("extension", "extension-shopee")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_marker(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _matches(target: Path, row: dict, *, deep: bool) -> bool:
    try:
        info = target.stat()
    except OSError:
        return False
    if not stat.S_ISREG(info.st_mode) or info.st_size != row["size"]:
        return False
    return _sha256(target) == row["sha256"] if deep else True


def sync_extensions(app_root, active: dict | None) -> dict:
    """Refresh ``App\\extension*`` from the active extensions package (idempotent)."""
    app_root = Path(app_root)
    component = ((active or {}).get("components") or {}).get("extensions")
    if not isinstance(component, dict):
        return {"status": "no_extensions_package", "changed": False}
    rows = component["package"]["files"]
    source_root = app_root / Path(*component["path"].split("/"))
    _check_ancestry(source_root)
    wanted = {"version": component["version"], "record_sha256": component["record_sha256"]}
    marker_path = app_root / MARKER
    marked = _read_marker(marker_path) == wanted
    if marked and all(_matches(app_root / Path(*row["path"].split("/")), row, deep=False) for row in rows):
        return {"status": "current", "changed": False}

    for top in TOP_DIRS:
        if _is_reparse(app_root / top):
            return {"status": "reparse_point_skipped", "changed": False}
    copied, removed = [], []
    # manifest.json goes last so a half-finished copy never looks complete.
    ordered = sorted(rows, key=lambda r: (r["path"].endswith("/manifest.json"), r["path"]))
    for row in ordered:
        target = app_root / Path(*row["path"].split("/"))
        if _matches(target, row, deep=True):
            continue
        source = source_root / Path(*row["path"].split("/"))
        _check_ancestry(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        if _is_reparse(target):
            raise OSError(f"refusing to write through a reparse point: {row['path']}")
        temp = target.with_name(f".{target.name}.kidte-tmp")
        data = source.read_bytes()
        if hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise OSError(f"extension package file is damaged: {row['path']}")
        with temp.open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, target)
        copied.append(row["path"])
    expected = {row["path"].casefold() for row in rows}
    for top in TOP_DIRS:
        base = app_root / top
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*"), reverse=True):
            if _is_reparse(path):
                continue
            rel = path.relative_to(app_root).as_posix()
            if path.is_file() and rel.casefold() not in expected:
                path.unlink()
                removed.append(rel)
            elif path.is_dir():
                try:
                    path.rmdir()  # only when empty
                except OSError:
                    pass
    tmp = marker_path.with_name(f".{MARKER}.tmp")
    tmp.write_text(json.dumps(wanted, sort_keys=True), encoding="utf-8")
    os.replace(tmp, marker_path)
    changed = bool(copied or removed)
    if changed:
        _write_notice(app_root, wanted["version"], len(copied) + len(removed))
    return {"status": "updated", "changed": changed, "copied": copied, "removed": removed}


def _write_notice(app_root: Path, version: str, files: int) -> None:
    from datetime import datetime, timezone
    body = {"version": version, "files": files,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    tmp = app_root / f".{NOTICE}.tmp"
    tmp.write_text(json.dumps(body, sort_keys=True), encoding="utf-8")
    os.replace(tmp, app_root / NOTICE)


def read_notice(app_root) -> dict | None:
    data = _read_marker(Path(app_root) / NOTICE)
    return data if isinstance(data, dict) and data.get("version") else None


def clear_notice(app_root) -> bool:
    try:
        (Path(app_root) / NOTICE).unlink()
        return True
    except OSError:
        return False
