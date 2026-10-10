import base64
import hashlib
import zipfile
from datetime import datetime, timedelta, timezone

import pytest
import update_launcher
import update_trust
from component_store import ComponentStore
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from update_catalog import CORE_DATA_SCHEMA, CatalogError, canonical_json


def _package(tmp_path, component, version, files, dependencies=None):
    archive = tmp_path / f"{component}-{version}.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        for relative, content in files.items():
            zipped.writestr(relative, content)
    inventory = [
        {"path": relative, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
        for relative, content in files.items()
    ]
    is_core = component == "core"
    return {
        "id": component,
        "kind": "core" if is_core else "gpu-runtime",
        "version": version,
        "platform": "windows",
        "architecture": "x86_64",
        "core_api": ">=1.0.0,<2.0.0",
        "dependencies": dependencies or {},
        "url": f"https://github.com/kidlost1412/KidTE/releases/download/test/{archive.name}",
        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "archive_bytes": archive.stat().st_size,
        "unpacked_bytes": sum(row["size"] for row in inventory),
        "files": inventory,
        "entrypoint": "KidTE.exe" if is_core else "gpu-runtime.json",
        "restart_required": True,
        "migration": "none",
        "source_commit": "b" * 40,
        "compatibility": {"data_schema": CORE_DATA_SCHEMA} if is_core else {
            "python_abi": "cp311", "ctranslate2_version": "4.8.2",
            "cuda_major": 12, "cudnn_major": 9,
        },
    }, archive


def _signed_request(package, *, bad_signature=False, expired=False, future_issued=False,
                    sequence=2):
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    now = datetime.now(timezone.utc)
    signed = {
        "schema_version": 2,
        "repository": "kidlost1412/KidTE",
        "channel": "beta",
        "sequence": sequence,
        "issued_at": (now + timedelta(days=1) if future_issued else
                      now - timedelta(days=91 if expired else 1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": (now + timedelta(days=2) if future_issued else
                       now - timedelta(days=1) if expired else
                       now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "components": [package],
    }
    envelope = {
        "schema_version": 2,
        "key_id": "bootstrap-test",
        "signed": signed,
        "signature": base64.b64encode(private_key.sign(canonical_json(signed))).decode("ascii"),
    }
    if bad_signature:
        envelope["signature"] = base64.b64encode(b"x" * 64).decode("ascii")
    trusted = {
        "bootstrap-test": base64.b64encode(public_key).decode("ascii"),
    }
    return canonical_json(envelope), trusted


def _active_with_pending_old_generation(tmp_path, monkeypatch, core_version="1.14.254",
                                        pending_core_version=None, pending_gpu_version=None):
    app_root = tmp_path / "App"
    app_root.mkdir()
    store = ComponentStore(app_root)
    old_core, core_archive = _package(
        tmp_path, "core", core_version, {"KidTE.exe": b"old-core"})
    gpu, gpu_archive = _package(
        tmp_path, "gpu-cuda12", "1.0.0", {"gpu-runtime.json": b'{"fixture":true}'},
        dependencies={"core": ">=1.0.0,<2.0.0"})
    store.stage_package(old_core, core_archive)
    store.stage_package(gpu, gpu_archive)
    store.prepare({"core": old_core, "gpu-cuda12": gpu}, catalog_sequence=1)
    active = store.activate_pending()
    store.confirm_active(active["generation"])
    pending_core = old_core
    if pending_core_version and pending_core_version != core_version:
        pending_core, archive = _package(
            tmp_path, "core", pending_core_version,
            {"KidTE.exe": f"pending-core-{pending_core_version}".encode()})
        store.stage_package(pending_core, archive)
    pending_gpu = gpu
    if pending_gpu_version and pending_gpu_version != gpu["version"]:
        pending_gpu, archive = _package(
            tmp_path, "gpu-cuda12", pending_gpu_version,
            {"gpu-runtime.json": f'{{"fixture": "{pending_gpu_version}"}}'.encode()},
            dependencies={"core": ">=1.0.0,<2.0.0"})
        store.stage_package(pending_gpu, archive)
    queued_pending = store.prepare(
        {"core": pending_core, "gpu-cuda12": pending_gpu}, catalog_sequence=1)
    return app_root, store, old_core, gpu, active, queued_pending


def _install_bootstrap_request(app_root, package, monkeypatch, *, bad_signature=False,
                               expired=False, future_issued=False, sequence=2):
    raw, trusted = _signed_request(package, bad_signature=bad_signature,
                                   expired=expired, future_issued=future_issued,
                                   sequence=sequence)
    monkeypatch.setattr(update_trust, "TRUSTED_KEYS", trusted)
    request = app_root / "bootstrap-request.json"
    request.write_bytes(raw)
    return request


def _baseline_package(tmp_path, app_root):
    files = {"KidTE.exe": b"installer-core-1.14.255"}
    package, _archive = _package(tmp_path, "core", "1.14.255", files)
    for relative, content in files.items():
        (app_root / relative).write_bytes(content)
    return package, files


def test_installer_bootstrap_stages_new_core_retains_gpu_and_only_prepares(tmp_path, monkeypatch):
    app_root, store, _old_core, gpu, old_active, old_pending = _active_with_pending_old_generation(
        tmp_path, monkeypatch)
    new_core, _files = _baseline_package(tmp_path, app_root)
    request = _install_bootstrap_request(app_root, new_core, monkeypatch)

    update_launcher._prepare_installer_core(app_root, store)

    active = store.load_active()
    pending = store.load_pending()
    assert active["generation"] == old_active["generation"]
    assert active["components"]["core"]["version"] == "1.14.254"
    assert active["components"]["gpu-cuda12"]["version"] == gpu["version"]
    assert pending["generation"] != old_pending["generation"]
    assert pending["components"]["core"]["package"]["version"] == "1.14.255"
    assert pending["components"]["gpu-cuda12"]["package"]["version"] == gpu["version"]
    assert (app_root / "packages" / "core" / "1.14.255" / "KidTE.exe").read_bytes() == b"installer-core-1.14.255"
    assert not request.exists()


def test_installer_bootstrap_invalid_signature_leaves_pending_and_request(tmp_path, monkeypatch):
    app_root, store, _old_core, _gpu, old_active, old_pending = _active_with_pending_old_generation(
        tmp_path, monkeypatch)
    new_core, _files = _baseline_package(tmp_path, app_root)
    request = _install_bootstrap_request(app_root, new_core, monkeypatch, bad_signature=True)
    cancelled = []
    cancel_pending = store.cancel_pending

    def tracked_cancel():
        cancelled.append(True)
        cancel_pending()

    monkeypatch.setattr(store, "cancel_pending", tracked_cancel)

    with pytest.raises(CatalogError, match="invalid_signature"):
        update_launcher._prepare_installer_core(app_root, store)

    assert cancelled == []
    assert store.load_active()["generation"] == old_active["generation"]
    assert store.load_pending()["generation"] == old_pending["generation"]
    assert request.is_file()


def test_installer_bootstrap_changed_baseline_leaves_pending_and_request(tmp_path, monkeypatch):
    app_root, store, _old_core, _gpu, old_active, old_pending = _active_with_pending_old_generation(
        tmp_path, monkeypatch)
    new_core, _files = _baseline_package(tmp_path, app_root)
    request = _install_bootstrap_request(app_root, new_core, monkeypatch)
    baseline = app_root / "KidTE.exe"
    baseline.write_bytes(b"modified-core-1.14.255")
    cancelled = []
    cancel_pending = store.cancel_pending

    def tracked_cancel():
        cancelled.append(True)
        cancel_pending()

    monkeypatch.setattr(store, "cancel_pending", tracked_cancel)

    with pytest.raises(update_launcher.LauncherError, match="do not match"):
        update_launcher._prepare_installer_core(app_root, store)

    assert cancelled == []
    assert store.load_active()["generation"] == old_active["generation"]
    assert store.load_pending()["generation"] == old_pending["generation"]
    assert request.is_file()


def test_expired_offline_bootstrap_is_accepted_for_exact_local_baseline(tmp_path, monkeypatch):
    app_root, store, _old_core, _gpu, old_active, old_pending = _active_with_pending_old_generation(
        tmp_path, monkeypatch)
    new_core, _files = _baseline_package(tmp_path, app_root)
    request = _install_bootstrap_request(app_root, new_core, monkeypatch, expired=True)

    update_launcher._prepare_installer_core(app_root, store)

    assert store.load_active()["generation"] == old_active["generation"]
    pending = store.load_pending()
    assert pending["generation"] != old_pending["generation"]
    assert pending["components"]["core"]["package"]["version"] == "1.14.255"
    assert not request.exists()


def test_network_catalog_verification_still_rejects_expired_catalog(tmp_path):
    from update_catalog import verify_catalog

    package, _archive = _package(tmp_path, "core", "1.14.255", {"KidTE.exe": b"core"})
    raw, trusted = _signed_request(package, expired=True)

    with pytest.raises(CatalogError, match="catalog_expired_or_not_yet_valid"):
        verify_catalog(raw, trusted, channel="beta")


def test_future_issued_bootstrap_is_rejected_and_keeps_pending(tmp_path, monkeypatch):
    app_root, store, _old_core, _gpu, old_active, old_pending = _active_with_pending_old_generation(
        tmp_path, monkeypatch)
    new_core, _files = _baseline_package(tmp_path, app_root)
    request = _install_bootstrap_request(app_root, new_core, monkeypatch, future_issued=True)

    with pytest.raises(CatalogError, match="catalog_expired_or_not_yet_valid"):
        update_launcher._prepare_installer_core(app_root, store)

    assert store.load_active()["generation"] == old_active["generation"]
    assert store.load_pending()["generation"] == old_pending["generation"]
    assert request.is_file()


def test_bootstrap_rejects_sequence_below_local_highwater(tmp_path, monkeypatch):
    app_root, store, _old_core, _gpu, old_active, old_pending = _active_with_pending_old_generation(
        tmp_path, monkeypatch)
    store.accept_sequence(3)
    new_core, _files = _baseline_package(tmp_path, app_root)
    request = _install_bootstrap_request(app_root, new_core, monkeypatch, sequence=2)

    with pytest.raises(CatalogError, match="catalog_rollback"):
        update_launcher._prepare_installer_core(app_root, store)

    assert store.load_active()["generation"] == old_active["generation"]
    assert store.load_pending()["generation"] == old_pending["generation"]
    assert request.is_file()


def test_bootstrap_rejects_core_version_older_than_active(tmp_path, monkeypatch):
    app_root, store, _old_core, gpu, old_active, _old_pending = _active_with_pending_old_generation(
        tmp_path, monkeypatch, core_version="1.14.300")
    new_core, _files = _baseline_package(tmp_path, app_root)
    request = _install_bootstrap_request(app_root, new_core, monkeypatch, sequence=2)

    with pytest.raises(update_launcher.LauncherError, match="(?i)(older|downgrade|version)"):
        update_launcher._prepare_installer_core(app_root, store)

    pending = store.load_pending()
    assert store.load_active()["generation"] == old_active["generation"]
    assert pending["components"]["core"]["package"]["version"] == "1.14.300"
    assert pending["components"]["gpu-cuda12"]["package"]["version"] == gpu["version"]
    assert request.is_file()


def test_bootstrap_rejects_core_version_older_than_pending(tmp_path, monkeypatch):
    app_root, store, _old_core, _gpu, old_active, old_pending = _active_with_pending_old_generation(
        tmp_path, monkeypatch, core_version="1.14.254", pending_core_version="1.14.300")
    new_core, _files = _baseline_package(tmp_path, app_root)
    request = _install_bootstrap_request(app_root, new_core, monkeypatch)

    with pytest.raises(update_launcher.LauncherError, match="(?i)(older|downgrade|version)"):
        update_launcher._prepare_installer_core(app_root, store)

    pending = store.load_pending()
    assert store.load_active()["generation"] == old_active["generation"]
    assert pending["generation"] == old_pending["generation"]
    assert pending["components"]["core"]["package"]["version"] == "1.14.300"
    assert request.is_file()


def test_bootstrap_preserves_pending_feature_selection(tmp_path, monkeypatch):
    app_root, store, _old_core, _gpu, old_active, _old_pending = _active_with_pending_old_generation(
        tmp_path, monkeypatch, pending_gpu_version="2.0.0")
    new_core, _files = _baseline_package(tmp_path, app_root)
    request = _install_bootstrap_request(app_root, new_core, monkeypatch)

    update_launcher._prepare_installer_core(app_root, store)

    active = store.load_active()
    pending = store.load_pending()
    assert active["generation"] == old_active["generation"]
    assert pending["components"]["core"]["package"]["version"] == "1.14.255"
    assert pending["components"]["gpu-cuda12"]["package"]["version"] == "2.0.0"
    assert not request.exists()


def test_bootstrap_prepare_failure_preserves_existing_pending(tmp_path, monkeypatch):
    app_root, store, _old_core, _gpu, old_active, old_pending = _active_with_pending_old_generation(
        tmp_path, monkeypatch)
    new_core, _files = _baseline_package(tmp_path, app_root)
    request = _install_bootstrap_request(app_root, new_core, monkeypatch)

    def fail_prepare(*_args, **_kwargs):
        raise OSError("simulated pending write failure")

    monkeypatch.setattr(store, "prepare", fail_prepare)
    with pytest.raises(OSError, match="simulated pending write failure"):
        update_launcher._prepare_installer_core(app_root, store)

    assert store.load_active()["generation"] == old_active["generation"]
    assert store.load_pending()["generation"] == old_pending["generation"]
    assert request.is_file()
