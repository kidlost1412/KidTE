import base64
import hashlib
import json
from datetime import datetime, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from update_catalog import (
    CORE_DATA_SCHEMA,
    CatalogError,
    canonical_json,
    resolve_components,
    verify_catalog,
)

NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)


def package(component, version, *, files=None, dependencies=None):
    files = files or [("KidTE.exe" if component == "core" else "gpu-runtime.json", b"payload")]
    inv = [{"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()} for name, data in files]
    return {
        "id": component, "kind": "core" if component == "core" else "gpu-runtime", "version": version, "platform": "windows",
        "architecture": "x86_64", "core_api": ">=1.0.0,<2.0.0",
        "dependencies": dependencies if dependencies is not None else ({"core": ">=1.0.0,<2.0.0"} if component == "gpu-cuda12" else {}),
        "url": f"https://github.com/kidlost1412/KidTE/releases/download/p2-test/{component}-{version}.zip",
        "archive_sha256": "a" * 64, "archive_bytes": 123, "unpacked_bytes": sum(x["size"] for x in inv),
        "files": inv, "entrypoint": "KidTE.exe" if component == "core" else "gpu-runtime.json",
        "restart_required": True, "migration": "none", "source_commit": "b" * 40,
        "compatibility": {"data_schema": CORE_DATA_SCHEMA} if component == "core" else {"python_abi": "cp311", "ctranslate2_version": "4.8.2", "cuda_major": 12, "cudnn_major": 9},
    }


def signed_catalog(components=None, channel="stable", sequence=4):
    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    signed = {
        "schema_version": 2, "repository": "kidlost1412/KidTE", "channel": channel, "sequence": sequence,
        "issued_at": "2026-10-09T00:00:00Z", "expires_at": "2026-10-11T00:00:00Z",
        "components": components or [package("core", "1.2.0"), package("gpu-cuda12", "1.0.0")],
    }
    env = {"schema_version": 2, "key_id": "release-1", "signed": signed, "signature": base64.b64encode(key.sign(canonical_json(signed))).decode()}
    return json.dumps(env), {"release-1": base64.b64encode(pub).decode()}, key


def test_verify_catalog_checks_signature_channel_expiry_and_rollback():
    raw, keys, _ = signed_catalog()
    result = verify_catalog(raw, keys, now=NOW, minimum_sequence=4)
    assert result["sequence"] == 4
    with pytest.raises(CatalogError, match="invalid_signature"):
        verify_catalog(raw.replace('"stable"', '"beta"'), keys, now=NOW)
    with pytest.raises(CatalogError, match="rollback"):
        verify_catalog(raw, keys, now=NOW, minimum_sequence=5)
    with pytest.raises(CatalogError, match="invalid_catalog_metadata"):
        verify_catalog(raw, keys, channel="beta", now=NOW)


def test_duplicate_json_keys_and_unsafe_archive_inventory_are_rejected():
    raw, keys, _ = signed_catalog()
    with pytest.raises(CatalogError, match="duplicate_json_key"):
        verify_catalog(raw.replace('"schema_version": 2', '"schema_version": 2, "schema_version": 2', 1), keys, now=NOW)
    pkg = package("core", "1.2.0", files=[("../KidTE.exe", b"bad")])
    raw, keys, _ = signed_catalog([pkg])
    with pytest.raises(CatalogError, match="invalid_file_inventory"):
        verify_catalog(raw, keys, now=NOW)


def test_core_rejects_unpinned_data_schema_and_user_data_paths():
    pkg = package("core", "1.2.0")
    pkg["compatibility"] = {"data_schema": "0" * 64}
    raw, keys, _ = signed_catalog([pkg])
    with pytest.raises(CatalogError, match="invalid_core_package"):
        verify_catalog(raw, keys, now=NOW)


def test_audited_opencv_initialization_code_is_not_customer_data():
    from update_catalog import _contains_user_data

    assert not _contains_user_data("Captcha/_internal/cv2/data/__init__.py")
    assert _contains_user_data("Captcha/_internal/cv2/data/customer.json")
    assert _contains_user_data("data/__init__.py")
    pkg = package("core", "1.2.0", files=[("KidTE.exe", b"ok"), ("data/customer.json", b"private")])
    raw, keys, _ = signed_catalog([pkg])
    with pytest.raises(CatalogError, match="invalid_file_inventory"):
        verify_catalog(raw, keys, now=NOW)


@pytest.mark.parametrize("paths", [("A", "a/b"), ("a/b", "A")])
def test_file_directory_collisions_are_rejected_in_either_order(paths):
    pkg = package("core", "1.2.0", files=[("KidTE.exe", b"ok"), *((path, b"x") for path in paths)])
    raw, keys, _ = signed_catalog([pkg])
    with pytest.raises(CatalogError, match="colliding_file_paths"):
        verify_catalog(raw, keys, now=NOW)


def test_resolver_selects_dependency_closure_and_rejects_cycles_or_conflicts():
    catalog = {"components": [package("core", "1.2.0"), package("gpu-cuda12", "1.0.0")]}
    result = resolve_components(catalog, {"gpu-cuda12": None})
    assert set(result) == {"core", "gpu-cuda12"}
    assert result["core"]["version"] == "1.2.0"
    with pytest.raises(CatalogError, match="version_unavailable"):
        resolve_components(catalog, {"gpu-cuda12": "2.0.0"})
    cycle = {"components": [
        {**package("core", "1.0.0"), "dependencies": {"gpu-cuda12": ">=1.0.0"}},
        {**package("gpu-cuda12", "1.0.0"), "dependencies": {"core": ">=1.0.0"}},
    ]}
    with pytest.raises(CatalogError, match="dependency_cycle"):
        resolve_components(cycle, {"core": None})


def test_resolver_keeps_compatible_installed_dependency_without_forced_download():
    installed_core = package("core", "1.4.0")
    current_gpu = package("gpu-cuda12", "1.0.0", dependencies={"core": ">=1.0.0,<2.0.0"})
    catalog = {"components": [
        package("core", "1.5.0"),
        package("gpu-cuda12", "2.0.0", dependencies={"core": ">=1.4.0,<2.0.0"}),
    ]}
    result = resolve_components(catalog, {"gpu-cuda12": None}, installed={"core": installed_core, "gpu-cuda12": current_gpu})
    assert set(result) == {"gpu-cuda12"}


def test_resolver_updates_retained_gpu_only_when_new_core_breaks_its_constraint():
    old_gpu = package("gpu-cuda12", "1.0.0", dependencies={"core": ">=1.0.0,<2.0.0"})
    installed = {"core": package("core", "1.5.0"), "gpu-cuda12": old_gpu}
    catalog = {"components": [
        package("core", "2.0.0"),
        package("gpu-cuda12", "2.0.0", dependencies={"core": ">=2.0.0,<3.0.0"}),
    ]}
    result = resolve_components(catalog, {"core": "2.0.0"}, installed=installed)
    assert set(result) == {"core", "gpu-cuda12"}
    assert result["gpu-cuda12"]["version"] == "2.0.0"

    incompatible = {"components": [package("core", "2.0.0")]}
    with pytest.raises(CatalogError, match="dependency_conflict"):
        resolve_components(incompatible, {"core": "2.0.0"}, installed=installed)


def test_resolver_rejects_retained_package_with_missing_dependency():
    installed_gpu = package("gpu-cuda12", "1.0.0", dependencies={"core": ">=1.0.0,<2.0.0"})
    with pytest.raises(CatalogError, match="component_version_unavailable"):
        resolve_components({"components": []}, {}, installed={"gpu-cuda12": installed_gpu})
