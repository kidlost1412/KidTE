import hashlib
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packaging"))
import build_catalog
from update_catalog import CORE_DATA_SCHEMA


def _entry(cid, version, url):
    return {"id": cid, "kind": "core" if cid == "core" else "media-tools", "version": version,
            "platform": "windows", "architecture": "x86_64", "core_api": ">=1.0.0,<2.0.0",
            "dependencies": {}, "url": url, "archive_sha256": "a" * 64, "archive_bytes": 1,
            "unpacked_bytes": 1, "files": [{"path": "x", "size": 1, "sha256": "b" * 64}],
            "entrypoint": "x", "restart_required": True, "migration": "none",
            "source_commit": "c" * 40, "compatibility": {"data_schema": CORE_DATA_SCHEMA}}


def _published():
    return {"schema_version": 2, "repository": "kidlost1412/KidTE", "channel": "beta", "sequence": 14,
            "issued_at": "2026-10-11T04:00:27Z", "expires_at": "2027-01-09T04:00:27Z",
            "components": [_entry("core", "1.14.257", "https://github.com/x/old-core.zip"),
                           _entry("media-tools", "1.0.0", "https://github.com/x/media.zip")]}


def _zip(tmp_path):
    path = tmp_path / "core.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("KidTE.exe", b"new")
        z.writestr("b/y.txt", b"yy")
    return path


def test_only_changed_component_changes_and_sequence_increments(tmp_path):
    old = _published()
    now = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)
    zp = _zip(tmp_path)
    out = build_catalog.build_payload(
        {"signed": old, "signature": "ignored"},
        {"core": {"zip": zp, "url": "https://github.com/x/new-core.zip", "version": "1.14.258",
                  "source_commit": "d" * 40}},
        now=now, days=90)
    assert out["sequence"] == 15
    assert out["issued_at"] == "2026-10-12T12:00:00Z"
    assert out["expires_at"] == "2027-01-10T12:00:00Z"
    assert out["components"][1] == old["components"][1]
    core = out["components"][0]
    assert core["version"] == "1.14.258" and core["url"].endswith("new-core.zip")
    assert core["archive_sha256"] == hashlib.sha256(zp.read_bytes()).hexdigest()
    assert core["archive_bytes"] == zp.stat().st_size and core["unpacked_bytes"] == 5
    assert {f["path"] for f in core["files"]} == {"KidTE.exe", "b/y.txt"}
    assert core["source_commit"] == "d" * 40
    assert core["compatibility"] == old["components"][0]["compatibility"]
    assert old["sequence"] == 14


def test_rejects_unknown_unchanged_or_empty(tmp_path):
    zp = _zip(tmp_path)
    old = _published()
    with pytest.raises(ValueError, match="unknown_component"):
        build_catalog.build_payload(old, {"nope": {"zip": zp, "url": "u", "version": "9"}})
    with pytest.raises(ValueError, match="component_not_changed"):
        build_catalog.build_payload(old, {"core": {"zip": zp, "url": old["components"][0]["url"], "version": "2.0.0"}})
    with pytest.raises(ValueError, match="no_component_changes"):
        build_catalog.build_payload(old, {})
