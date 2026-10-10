import base64
import hashlib
import os
import subprocess
import zipfile
from datetime import datetime, timedelta, timezone

import component_runtime
import pytest
from component_store import ComponentStore, ComponentStoreError
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from update_catalog import CORE_DATA_SCHEMA, canonical_json, verify_catalog


def _signed_media_package(tmp_path):
    files = {
        "tools/ffmpeg.exe": b"ffmpeg-fixture-binary",
        "tools/ffprobe.exe": b"ffprobe-fixture-binary",
    }
    archive = tmp_path / "media-tools.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        for relative, content in files.items():
            zipped.writestr(relative, content)
    inventory = [
        {"path": relative, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
        for relative, content in files.items()
    ]
    now = datetime.now(timezone.utc)
    package = {
        "id": "media-tools",
        "kind": "media-tools",
        "version": "1.0.0",
        "platform": "windows",
        "architecture": "x86_64",
        "core_api": ">=1.0.0,<2.0.0",
        "dependencies": {"core": ">=1.14.255,<2.0.0"},
        "url": "https://github.com/kidlost1412/KidTE/releases/download/test/media-tools.zip",
        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "archive_bytes": archive.stat().st_size,
        "unpacked_bytes": sum(row["size"] for row in inventory),
        "files": inventory,
        "entrypoint": "tools/ffmpeg.exe",
        "restart_required": True,
        "migration": "none",
        "source_commit": "a" * 40,
        "compatibility": {"component_api": 1},
    }
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    signed = {
        "schema_version": 2,
        "repository": "kidlost1412/KidTE",
        "channel": "stable",
        "sequence": 1,
        "issued_at": (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "components": [package],
    }
    envelope = {
        "schema_version": 2,
        "key_id": "test-key",
        "signed": signed,
        "signature": base64.b64encode(private_key.sign(canonical_json(signed))).decode("ascii"),
    }
    raw = canonical_json(envelope)
    trusted = {"test-key": base64.b64encode(public_key).decode("ascii")}
    verified = verify_catalog(raw, trusted, now=now)["components"][0]
    return verified, archive, files


def _signed_core_package(tmp_path):
    content = b"core-fixture-binary"
    archive = tmp_path / "core.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("KidTE.exe", content)
    package = {
        "id": "core",
        "kind": "core",
        "version": "1.14.255",
        "platform": "windows",
        "architecture": "x86_64",
        "core_api": ">=1.0.0,<2.0.0",
        "dependencies": {},
        "url": "https://github.com/kidlost1412/KidTE/releases/download/test/core.zip",
        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "archive_bytes": archive.stat().st_size,
        "unpacked_bytes": len(content),
        "files": [{"path": "KidTE.exe", "size": len(content),
                   "sha256": hashlib.sha256(content).hexdigest()}],
        "entrypoint": "KidTE.exe",
        "restart_required": True,
        "migration": "none",
        "source_commit": "a" * 40,
        "compatibility": {"data_schema": CORE_DATA_SCHEMA},
    }
    return package, archive


def _store_with_pending_media(tmp_path, monkeypatch):
    app_root = tmp_path / "App"
    store = ComponentStore(app_root)
    core, core_archive = _signed_core_package(tmp_path)
    package, archive, files = _signed_media_package(tmp_path)
    store.stage_package(core, core_archive)
    store.stage_package(package, archive)
    pending = store.prepare({"core": core, "media-tools": package}, catalog_sequence=1)
    monkeypatch.setenv("KIDTE_APP_ROOT", str(app_root))
    return app_root, store, pending, package, files


def _activate(store, pending):
    active = store.activate_pending()
    assert active["generation"] == pending["generation"]
    store.confirm_active(active["generation"])


def test_component_path_returns_none_when_media_tools_is_not_active(tmp_path, monkeypatch):
    app_root = tmp_path / "App"
    ComponentStore(app_root)
    monkeypatch.setenv("KIDTE_APP_ROOT", str(app_root))

    assert component_runtime.component_path("media-tools", "tools/ffmpeg.exe") is None


def test_component_path_ignores_pending_media_tools_until_activation(tmp_path, monkeypatch):
    app_root, _store, _pending, _package, _files = _store_with_pending_media(tmp_path, monkeypatch)

    assert component_runtime.component_path("media-tools", "tools/ffmpeg.exe") is None
    assert (app_root / ".staging").is_dir()


def test_component_path_returns_active_signed_media_binary(tmp_path, monkeypatch):
    _app_root, store, pending, package, files = _store_with_pending_media(tmp_path, monkeypatch)
    _activate(store, pending)

    selected = component_runtime.component_path("media-tools", "tools/ffmpeg.exe")
    assert selected == _app_root / "packages" / "media-tools" / package["version"] / "tools" / "ffmpeg.exe"
    assert selected.read_bytes() == files["tools/ffmpeg.exe"]


def test_component_path_rejects_damaged_active_binary(tmp_path, monkeypatch):
    app_root, store, pending, package, files = _store_with_pending_media(tmp_path, monkeypatch)
    _activate(store, pending)
    selected = app_root / "packages" / "media-tools" / package["version"] / "tools" / "ffmpeg.exe"
    original = files["tools/ffmpeg.exe"]
    selected.write_bytes(original[:-1] + bytes([original[-1] ^ 0xFF]))

    with pytest.raises(ComponentStoreError, match="digest mismatch"):
        component_runtime.component_path("media-tools", "tools/ffmpeg.exe")


def test_component_path_rechecks_same_size_binary_when_mtime_changes(tmp_path, monkeypatch):
    app_root, store, pending, package, files = _store_with_pending_media(tmp_path, monkeypatch)
    _activate(store, pending)
    selected = app_root / "packages" / "media-tools" / package["version"] / "tools" / "ffmpeg.exe"
    assert component_runtime.component_path("media-tools", "tools/ffmpeg.exe") == selected
    before = selected.stat()
    original = files["tools/ffmpeg.exe"]
    selected.write_bytes(original[:-1] + bytes([original[-1] ^ 0xFF]))
    os.utime(selected, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
    assert selected.stat().st_size == before.st_size

    with pytest.raises(ComponentStoreError, match="digest mismatch"):
        component_runtime.component_path("media-tools", "tools/ffmpeg.exe")


@pytest.mark.parametrize("relative", ["../ffmpeg.exe", "tools/../ffmpeg.exe", "tools\\ffmpeg.exe"])
def test_component_path_rejects_traversal_and_noncanonical_paths(relative, tmp_path, monkeypatch):
    app_root = tmp_path / "App"
    ComponentStore(app_root)
    monkeypatch.setenv("KIDTE_APP_ROOT", str(app_root))

    with pytest.raises(ComponentStoreError, match="invalid component resource"):
        component_runtime.component_path("media-tools", relative)


def test_component_path_rejects_reparse_point_in_active_package(tmp_path, monkeypatch):
    app_root, store, pending, package, files = _store_with_pending_media(tmp_path, monkeypatch)
    _activate(store, pending)
    package_tools = app_root / "packages" / "media-tools" / package["version"] / "tools"
    for path in package_tools.iterdir():
        path.unlink()
    package_tools.rmdir()
    external_tools = tmp_path / "external-tools"
    external_tools.mkdir()
    (external_tools / "ffmpeg.exe").write_bytes(files["tools/ffmpeg.exe"])
    (external_tools / "ffprobe.exe").write_bytes(files["tools/ffprobe.exe"])
    try:
        os.symlink(external_tools, package_tools, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        if os.name != "nt":
            pytest.skip(f"directory symlinks unavailable: {exc}")
        junction = subprocess.run(
            ["cmd.exe", "/c", "mklink", "/J", os.fspath(package_tools), os.fspath(external_tools)],
            capture_output=True, text=True, check=False)
        if junction.returncode != 0:
            pytest.skip(f"directory symlink/junction unavailable: {exc}; {junction.stderr or junction.stdout}")

    with pytest.raises(ComponentStoreError, match="reparse point"):
        component_runtime.component_path("media-tools", "tools/ffmpeg.exe")
