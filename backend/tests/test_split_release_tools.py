"""Packaging, catalog building and update flow for the split runtime/app/extensions components."""
import base64
import importlib
import sys
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from component_store import ComponentStore
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from update_catalog import canonical_json, verify_catalog
from update_manager import UpdateManager

PACKAGING = Path(__file__).resolve().parents[2] / "packaging"
sys.path.insert(0, str(PACKAGING))
import build_catalog  # noqa: E402
import stage_runtime  # noqa: E402
import update_release  # noqa: E402

from test_split_components import (APP_ENTRY, app, extensions, legacy_core, make_pkg,  # noqa: E402
                                   members_of, optional, runtime, zip_of)


def _signed(components, sequence):
    key = Ed25519PrivateKey.generate()
    now = datetime.now(timezone.utc)
    payload = {"schema_version": 2, "repository": "kidlost1412/KidTE", "channel": "beta", "sequence": sequence,
               "issued_at": (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "expires_at": (now + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "components": components}
    envelope = {"schema_version": 2, "key_id": "t", "signed": payload,
                "signature": base64.b64encode(key.sign(canonical_json(payload))).decode()}
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return canonical_json(envelope), {"t": base64.b64encode(public).decode()}


# --------------------------------------------------------- stage / ownership
@pytest.mark.parametrize("path,owner", [
    ("KidTE.exe", "runtime"), ("_internal/base_library.zip", "runtime"), ("_internal/certifi/cacert.pem", "runtime"),
    ("backend/src/app_entry.py", "app"), ("backend/prompts/system_prompt.txt", "app"),
    ("plugins/lieu-dung/processor.py", "app"), ("docs/CAI-DAT-WINDOWS.md", "app"), ("VERSION", "app"),
    ("extension/manifest.json", "extensions"), ("extension-shopee/popup.js", "extensions"),
    ("KidTELauncher.exe", None), ("release-manifest.json", None), ("bootstrap-request.json", None),
    ("tools/ffmpeg.exe", None), ("Captcha/KidTECaptcha.exe", None), ("KidTEVideo.exe", None),
])
def test_every_installed_file_belongs_to_exactly_one_package_or_none(path, owner):
    assert stage_runtime.component_for(path) == owner


def _merged_tree(tmp_path):
    tree = tmp_path / "KidTE"
    stage_runtime.stage(tree, "cpu", minimal=True, app_only=True)
    (tree / "KidTE.exe").write_bytes(b"MZ-runtime")
    (tree / "_internal" / "certifi").mkdir(parents=True)
    (tree / "_internal" / "certifi" / "cacert.pem").write_text("pem", encoding="utf-8")
    (tree / "_internal" / "base_library.zip").write_bytes(b"lib")
    (tree / "KidTELauncher.exe").write_bytes(b"launcher")  # never part of any package
    return tree


def test_split_packages_partition_the_tree_without_overlap_or_loss(tmp_path):
    tree = _merged_tree(tmp_path)
    stage_runtime.validate(tree, profile="cpu", minimal=True)
    seen = {}
    for component in ("runtime", "app", "extensions"):
        facts = update_release.package_directory(tree, tmp_path / f"{component}.zip", component=component)
        seen[component] = {row["path"] for row in facts["files"]}
        with zipfile.ZipFile(tmp_path / f"{component}.zip") as archive:
            assert {info.filename for info in archive.infolist()} == seen[component]
    assert not (seen["runtime"] & seen["app"] or seen["app"] & seen["extensions"] or seen["runtime"] & seen["extensions"])
    everything = {path.relative_to(tree).as_posix() for path in tree.rglob("*") if path.is_file()}
    assert everything - set().union(*seen.values()) == {"KidTELauncher.exe"}
    assert "KidTE.exe" in seen["runtime"] and APP_ENTRY in seen["app"] and "extension/manifest.json" in seen["extensions"]
    assert not any(name.endswith(".pyc") or "__pycache__" in name for name in seen["app"])


def test_app_only_stage_needs_no_runtime_and_packages_just_app_and_extensions(tmp_path):
    tree = tmp_path / "app-only"
    stage_runtime.stage(tree, "cpu", minimal=True, app_only=True)
    assert not (tree / "KidTE.exe").exists()
    assert (tree / APP_ENTRY).is_file() and (tree / "VERSION").is_file()
    update_release.package_directory(tree, tmp_path / "a.zip", component="app")
    update_release.package_directory(tree, tmp_path / "e.zip", component="extensions")
    with pytest.raises(RuntimeError, match="missing"):
        update_release.package_directory(tree, tmp_path / "r.zip", component="runtime")


def test_package_refuses_dirty_trees(tmp_path):
    tree = _merged_tree(tmp_path)
    (tree / "packages").mkdir()
    (tree / "packages" / "x").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="dirty_package_source"):
        update_release.package_directory(tree, tmp_path / "a.zip", component="app")


def test_app_package_has_no_third_party_code_and_is_small(tmp_path):
    tree = tmp_path / "app-only"
    stage_runtime.stage(tree, "cpu", minimal=True, app_only=True)
    facts = update_release.package_directory(tree, tmp_path / "a.zip", component="app")
    assert facts["archive_bytes"] < 5 * 1024 * 1024
    assert all(row["path"].split("/")[0] in {"backend", "plugins", "docs", "VERSION"} for row in facts["files"])


def test_runtime_bootstrap_loads_app_from_package_dir(tmp_path, monkeypatch):
    bootstrap = importlib.import_module("runtime_bootstrap")
    package = tmp_path / "pkg"
    (package / "backend" / "src").mkdir(parents=True)
    (package / "backend" / "src" / "app_entry.py").write_text("def main():\n    return 7\n", encoding="utf-8")
    monkeypatch.setenv("KIDTE_APP_PACKAGE_DIR", str(package))
    monkeypatch.delitem(sys.modules, "app_entry", raising=False)
    saved = list(sys.path)
    try:
        assert bootstrap.main() == 7
        assert sys.dont_write_bytecode is True
    finally:
        sys.path[:] = saved
        sys.modules.pop("app_entry", None)
        sys.dont_write_bytecode = False
    monkeypatch.setenv("KIDTE_APP_PACKAGE_DIR", str(tmp_path / "missing"))
    monkeypatch.setattr(bootstrap, "_fail", lambda message: 3)
    assert bootstrap.main() == 3


def test_frozen_spec_keeps_own_modules_out_of_the_runtime_archive():
    spec = (PACKAGING / "KidTE.spec").read_text(encoding="utf-8")
    assert "runtime_bootstrap.py" in spec and "_is_own_module" in spec and "core.pure = [" in spec
    assert 'core_datas = []' in spec and "windows_version_info(\"Kid TE\", RUNTIME_VERSION)" in spec


# ----------------------------------------------------------------- build_catalog
def _published(tmp_path):
    packages = {}
    components = []
    for component, builder in (("runtime", runtime), ("app", app), ("extensions", extensions)):
        record = builder()
        archive, signed = zip_of(tmp_path, record, members_of(record, record["entrypoint"] and
                                                              {"runtime": b"rt", "app": b"app", "extensions": b"{}"}[component]))
        packages[component] = archive
        components.append(signed)
    components.append(optional())
    return components, packages


def test_build_catalog_adds_split_components_and_drops_the_monolith(tmp_path):
    published = {"schema_version": 2, "repository": "kidlost1412/KidTE", "channel": "beta", "sequence": 15,
                 "issued_at": "2026-10-11T00:00:00Z", "expires_at": "2027-01-10T00:00:00Z",
                 "components": [legacy_core(), optional()]}
    _, packages = _published(tmp_path)
    additions = {name: {"zip": str(path), "url": f"https://github.com/kidlost1412/KidTE/releases/download/v1/{path.name}",
                        "version": {"runtime": "1.0.0", "app": "1.15.0", "extensions": "1.0.0"}[name],
                        "source_commit": "b" * 40} for name, path in packages.items()}
    payload = build_catalog.build_payload(published, {}, additions=additions, removals=("core",))
    assert payload["sequence"] == 16
    assert [c["id"] for c in payload["components"]] == ["media-tools", "runtime", "app", "extensions"]
    app_record = next(c for c in payload["components"] if c["id"] == "app")
    assert app_record["dependencies"] == {"runtime": ">=1.0.0,<2.0.0"}
    raw, keys = _signed(payload["components"], 16)
    verify_catalog(raw, keys, channel="beta")  # the whole catalog is valid schema-2
    with pytest.raises(ValueError, match="component_already_published"):
        build_catalog.build_payload(payload, {}, additions=additions)


def test_build_catalog_refuses_unchanged_content_so_app_only_releases_stay_small(tmp_path):
    components, packages = _published(tmp_path)
    published = {"schema_version": 2, "repository": "kidlost1412/KidTE", "channel": "beta", "sequence": 20,
                 "issued_at": "2026-10-11T00:00:00Z", "expires_at": "2027-01-10T00:00:00Z", "components": components}
    same = {"zip": str(packages["runtime"]), "url": "https://github.com/kidlost1412/KidTE/releases/download/v2/rt.zip",
            "version": "1.0.1"}
    with pytest.raises(ValueError, match="component_content_unchanged:runtime"):
        build_catalog.build_payload(published, {"runtime": same})
    new_record = app("1.15.1", b"changed")
    archive, _ = zip_of(tmp_path, new_record, members_of(new_record, b"changed"))
    payload = build_catalog.build_payload(published, {"app": {
        "zip": str(archive), "url": "https://github.com/kidlost1412/KidTE/releases/download/v2/app.zip",
        "version": "1.15.1", "source_commit": "c" * 40}})
    by_id = {c["id"]: c for c in payload["components"]}
    assert by_id["app"]["version"] == "1.15.1" and by_id["runtime"] == next(c for c in components if c["id"] == "runtime")


# ------------------------------------------------------------------ manager flow
class Transport:
    def __init__(self, raw, archives):
        self.raw, self.archives, self.downloads = raw, archives, []

    def fetch_catalog(self, channel="stable"):
        return self.raw

    def download(self, url, destination, *, expected_sha256, expected_bytes, progress=None):
        self.downloads.append(url.rsplit("/", 1)[1])
        destination.write_bytes(self.archives[url].read_bytes())
        return destination


def _wait(manager):
    deadline = time.monotonic() + 10
    while manager.state()["busy"] and time.monotonic() < deadline:
        time.sleep(0.01)
    state = manager.state()
    assert not state["busy"], state
    return state


def _catalog(tmp_path, specs, sequence):
    """specs: [(builder, version, marker)] -> raw signed catalog + {url: zip}."""
    components, archives = [], {}
    for builder, version, marker in specs:
        record = builder(version, marker)
        archive, signed = zip_of(tmp_path, record, members_of(record, marker))
        components.append(signed)
        archives[signed["url"]] = archive
    raw, keys = _signed(components, sequence)
    return raw, keys, archives, components


def _install_generation(app_root, tmp_path, versions):
    store = ComponentStore(app_root)
    packages = {name: make_pkg(store, tmp_path, name, ver, f"{name}-{ver}".encode()) for name, ver in versions.items()}
    pending = store.prepare(packages, catalog_sequence=1)
    store.activate_pending()
    store.confirm_active(pending["generation"])
    return store


def test_app_only_update_downloads_only_the_app_archive(tmp_path):
    root = tmp_path / "App"
    _install_generation(root, tmp_path, {"runtime": "1.0.0", "app": "1.15.0", "extensions": "1.0.0"})
    (tmp_path / "rel").mkdir()
    raw, keys, archives, _ = _catalog(tmp_path / "rel", [(runtime, "1.0.0", b"runtime-1.0.0"), (app, "1.15.1", b"app-1.15.1"),
                                                         (extensions, "1.0.0", b"extensions-1.0.0")], 5)
    transport = Transport(raw, archives)
    manager = UpdateManager(root, transport=transport, trusted_keys=keys, launcher_caps="")
    manager.action("check", {"channel": "beta"})
    _wait(manager)
    plan = manager.action("plan", {"components": {"app": "1.15.1"}})
    assert [p["id"] for p in plan["packages"]] == ["app"]
    manager.action("install", {"components": {"app": "1.15.1"}})
    state = _wait(manager)
    assert transport.downloads == ["app-1.15.1.zip"]
    assert state["pending"] == {"runtime": "1.0.0", "app": "1.15.1", "extensions": "1.0.0"}
    assert state["restart_required"] is True


def test_monolith_install_needs_a_split_capable_launcher_then_migrates_in_one_generation(tmp_path):
    root = tmp_path / "App"
    store = ComponentStore(root)
    core_dir = root / "packages" / "core" / "1.14.259"
    core_dir.mkdir(parents=True)
    (core_dir / "KidTE.exe").write_bytes(b"old")
    core = legacy_core()
    store.prepare({"core": core}, catalog_sequence=1)
    store.activate_pending()
    store.confirm_active(store.load_active()["generation"])
    (tmp_path / "rel").mkdir()
    raw, keys, archives, _ = _catalog(tmp_path / "rel", [(runtime, "1.0.0", b"rt"), (app, "1.15.0", b"app"),
                                                         (extensions, "1.0.0", b"ext")], 9)
    transport = Transport(raw, archives)
    legacy_launcher = UpdateManager(root, transport=transport, trusted_keys=keys, launcher_caps="")
    legacy_launcher.action("check", {"channel": "beta"})
    _wait(legacy_launcher)
    with pytest.raises(ValueError, match="update_launcher_upgrade_required"):
        legacy_launcher.action("install", {"components": {"app": "1.15.0"}})
    assert transport.downloads == []
    manager = UpdateManager(root, transport=transport, trusted_keys=keys, launcher_caps="split-components")
    manager.action("check", {"channel": "beta"})
    _wait(manager)
    manager.action("install", {"components": {"app": None}})
    state = _wait(manager)
    assert sorted(transport.downloads) == ["app-1.15.0.zip", "extensions-1.0.0.zip", "runtime-1.0.0.zip"]
    assert state["pending"] == {"runtime": "1.0.0", "app": "1.15.0", "extensions": "1.0.0"}  # core is gone
    assert state["installed"] == {"core": "1.14.259"}  # and the old generation stays active until the restart


def test_split_components_cannot_be_removed(tmp_path):
    root = tmp_path / "App"
    _install_generation(root, tmp_path, {"runtime": "1.0.0", "app": "1.15.0", "extensions": "1.0.0"})
    manager = UpdateManager(root, transport=SimpleNamespace(), trusted_keys={"t": "x"})
    for component in ("runtime", "app", "extensions", "core"):
        with pytest.raises(ValueError, match="update_selection_invalid"):
            manager.action("remove", {"component": component})
