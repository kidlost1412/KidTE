import hashlib
import json
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packaging"))
import update_release
from update_catalog import CORE_DATA_SCHEMA, verify_catalog


def payload():
    data = b"core exe"
    return {
        "schema_version": 2, "repository": "kidlost1412/KidTE", "channel": "stable", "sequence": 1,
        "issued_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "components": [{
            "id": "core", "kind": "core", "version": "1.2.3", "platform": "windows", "architecture": "x86_64",
            "core_api": ">=1.0.0,<2.0.0", "dependencies": {},
            "url": "https://github.com/kidlost1412/KidTE/releases/download/p2-test/core-1.2.3.zip",
            "archive_sha256": "a" * 64, "archive_bytes": 100, "unpacked_bytes": len(data),
            "files": [{"path": "KidTE.exe", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}],
            "entrypoint": "KidTE.exe", "restart_required": True, "migration": "none",
            "source_commit": "b" * 40, "compatibility": {"data_schema": CORE_DATA_SCHEMA},
        }],
    }


def test_keygen_and_sign_use_protected_key_and_verify_before_writing(tmp_path, monkeypatch):
    private = tmp_path / "external" / "release.dpapi"
    public = tmp_path / "public.json"
    monkeypatch.setattr(update_release, "_dpapi", lambda raw, decrypt=False: raw[5:] if decrypt else b"DPAPI" + raw)
    key_id = update_release.keygen(private, public)
    metadata = json.loads(public.read_text(encoding="utf-8"))
    assert metadata["key_id"] == key_id
    assert private.read_bytes().startswith(b"DPAPI")
    payload_path = tmp_path / "payload.json"
    payload_path.write_text(json.dumps(payload()), encoding="utf-8")
    signed_path = tmp_path / "kidte-catalog-v2.json"
    update_release.sign_catalog(payload_path, private, signed_path)
    raw = signed_path.read_bytes()
    assert verify_catalog(raw, {key_id: metadata["public_key"]})["sequence"] == 1
    with pytest.raises(FileExistsError, match="refuses to overwrite"):
        update_release.keygen(private, public)


def test_keygen_refuses_private_key_inside_checkout(tmp_path):
    inside = ROOT / "release-key-test.dpapi"
    with pytest.raises(ValueError, match="outside_checkout"):
        update_release.keygen(inside, tmp_path / "pub.json")


def test_packer_excludes_stable_launcher_and_writes_deterministic_verified_zip(tmp_path, monkeypatch):
    source = tmp_path / "dist"
    source.mkdir()
    (source / "KidTE.exe").write_bytes(b"core")
    (source / "KidTELauncher.exe").write_bytes(b"stable launcher")
    (source / "release-manifest.json").write_text("{}", encoding="utf-8")
    (source / "extension").mkdir()
    (source / "extension" / "manifest.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(update_release.stage_runtime, "validate", lambda *_args, **_kwargs: {"ok": True})
    one, two = tmp_path / "one.zip", tmp_path / "two.zip"
    first = update_release.package_directory(source, one, component="core")
    second = update_release.package_directory(source, two, component="core")
    assert first == second
    assert one.read_bytes() == two.read_bytes()
    with zipfile.ZipFile(one) as archive:
        assert archive.namelist() == ["extension/manifest.json", "KidTE.exe"]


def test_core_packer_rejects_dirty_component_store_state(tmp_path, monkeypatch):
    source = tmp_path / "dist"
    source.mkdir()
    (source / "KidTE.exe").write_bytes(b"core")
    (source / "active.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(update_release.stage_runtime, "validate", lambda *_args, **_kwargs: {"ok": True})
    with pytest.raises(ValueError, match="dirty_package_source"):
        update_release.package_directory(source, tmp_path / "out.zip", component="core")


def test_packer_rejects_private_data_without_replacing_previous_output(tmp_path):
    source = tmp_path / "gpu"
    (source / "data").mkdir(parents=True)
    (source / "data" / "private.json").write_text("secret", encoding="utf-8")
    output = tmp_path / "release.zip"
    output.write_bytes(b"previous artifact")
    with pytest.raises(ValueError, match="unsafe_windows_path"):
        update_release.package_directory(source, output, component="gpu-cuda12")
    assert output.read_bytes() == b"previous artifact"
