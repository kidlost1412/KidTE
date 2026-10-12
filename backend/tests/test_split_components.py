"""Split of the monolithic core into runtime / app / extensions, and local garbage collection."""
import hashlib
import os
import zipfile

import pytest

import update_launcher as launcher
from component_store import ComponentStore, ComponentStoreError
from extension_sync import MARKER, sync_extensions
from update_catalog import (CORE_DATA_SCHEMA, CatalogError, check_generation, provided,
                            resolve_components, _validate_package)

APP_ENTRY = "backend/src/app_entry.py"
GRACE0 = {"grace_seconds": 0}


def _record(component, version, members, *, runtime_api=1):
    rows = [{"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in members.items()]
    entry = {"runtime": "KidTE.exe", "app": APP_ENTRY, "extensions": "extension/manifest.json"}[component]
    deps = {"app": {"runtime": f">={runtime_api}.0.0,<{runtime_api + 1}.0.0"}}.get(component, {})
    compat = {"runtime": {"runtime_api": runtime_api},
              "app": {"runtime_api": runtime_api, "data_schema": CORE_DATA_SCHEMA},
              "extensions": {"extension_api": 1}}[component]
    return {"id": component, "kind": component, "version": version, "platform": "windows",
            "architecture": "x86_64", "core_api": ">=1.0.0,<2.0.0", "dependencies": deps,
            "url": f"https://github.com/kidlost1412/KidTE/releases/download/t/{component}-{version}.zip",
            "archive_sha256": "0" * 64, "archive_bytes": 100, "unpacked_bytes": sum(r["size"] for r in rows),
            "files": rows, "entrypoint": entry, "restart_required": True, "migration": "none",
            "source_commit": "a" * 40, "compatibility": compat}, members


def runtime(version="1.0.0", marker=b"rt", **kw):
    return _record("runtime", version, {"KidTE.exe": marker, "_internal/lib.dll": b"dll"}, **kw)[0]


def app(version="1.15.0", marker=b"app", **kw):
    return _record("app", version, {APP_ENTRY: marker, "VERSION": version.encode()}, **kw)[0]


def extensions(version="1.0.0", marker=b"{}"):
    return _record("extensions", version, {"extension/manifest.json": marker, "extension-shopee/manifest.json": b"{s}"})[0]


def optional(component="media-tools"):
    data = b"ffmpeg"
    return {"id": component, "kind": "media-tools", "version": "1.0.0", "platform": "windows",
            "architecture": "x86_64", "core_api": ">=1.0.0,<2.0.0",
            "dependencies": {"core": ">=1.14.255,<2.0.0"},
            "url": "https://github.com/kidlost1412/KidTE/releases/download/t/media.zip",
            "archive_sha256": "1" * 64, "archive_bytes": 10, "unpacked_bytes": 12,
            "files": [{"path": "tools/ffmpeg.exe", "size": 6, "sha256": hashlib.sha256(data).hexdigest()},
                      {"path": "tools/ffprobe.exe", "size": 6, "sha256": hashlib.sha256(data).hexdigest()}],
            "entrypoint": "tools/ffmpeg.exe", "restart_required": True, "migration": "none",
            "source_commit": "a" * 40, "compatibility": {"component_api": 1}}


def legacy_core(version="1.14.259"):
    return {"id": "core", "kind": "core", "version": version, "platform": "windows",
            "architecture": "x86_64", "core_api": ">=1.0.0,<2.0.0", "dependencies": {},
            "url": "https://github.com/kidlost1412/KidTE/releases/download/t/core.zip",
            "archive_sha256": "2" * 64, "archive_bytes": 10, "unpacked_bytes": 3,
            "files": [{"path": "KidTE.exe", "size": 3, "sha256": hashlib.sha256(b"old").hexdigest()}],
            "entrypoint": "KidTE.exe", "restart_required": True, "migration": "none",
            "source_commit": "a" * 40, "compatibility": {"data_schema": CORE_DATA_SCHEMA}}


def zip_of(tmp_path, record, members):
    archive = tmp_path / f"{record['id']}-{record['version']}.zip"
    with zipfile.ZipFile(archive, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    record = dict(record, archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
                  archive_bytes=archive.stat().st_size)
    return archive, record


def stage(store, tmp_path, record, members):
    archive, signed = zip_of(tmp_path, record, members)
    store.stage_package(signed, archive)
    return signed


def members_of(record, marker):
    return {row["path"]: (marker if row["path"] == record["entrypoint"] else
                          (record["version"].encode() if row["path"] == "VERSION" else
                           {"_internal/lib.dll": b"dll", "extension-shopee/manifest.json": b"{s}"}[row["path"]]))
            for row in record["files"]}


def make_pkg(store, tmp_path, component, version, marker):
    builders = {"runtime": runtime, "app": app, "extensions": extensions}
    record = builders[component](version, marker)
    return stage(store, tmp_path, record, members_of(record, marker))


def catalog_of(*packages, sequence=2):
    return {"sequence": sequence, "components": list(packages)}


# ---------------------------------------------------------------- catalog rules
def test_split_records_are_valid_and_old_core_still_is():
    for record in (runtime(), app(), extensions(), legacy_core()):
        _validate_package(record, "kidlost1412/KidTE")


def test_runtime_api_is_the_runtime_major_and_app_must_depend_on_it():
    bad_runtime = runtime("2.0.0")  # major 2 but declares runtime_api 1
    with pytest.raises(CatalogError):
        _validate_package(bad_runtime, "kidlost1412/KidTE")
    bad_app = app()
    bad_app["dependencies"] = {"runtime": ">=1.0.0"}  # must be the exact ABI range
    with pytest.raises(CatalogError):
        _validate_package(bad_app, "kidlost1412/KidTE")
    _validate_package(app("1.16.0", runtime_api=2), "kidlost1412/KidTE")


def test_generation_needs_runtime_and_app_with_matching_abi():
    check_generation({"runtime": runtime(), "app": app(), "extensions": extensions()})
    check_generation({"core": legacy_core()})
    with pytest.raises(CatalogError, match="split_generation_incomplete"):
        check_generation({"app": app()})
    with pytest.raises(CatalogError, match="split_generation_incomplete"):
        check_generation({"runtime": runtime(), "app": app(), "core": legacy_core()})
    with pytest.raises(CatalogError, match="runtime_api_mismatch"):
        check_generation({"runtime": runtime(), "app": app("1.16.0", runtime_api=2)})


def test_app_stands_in_for_core_dependency():
    records = {"runtime": runtime(), "app": app("1.15.0")}
    assert provided(records, "core")["id"] == "app"
    assert provided({"core": legacy_core()}, "core")["id"] == "core"


def test_app_only_update_resolves_to_just_the_app():
    installed = {"runtime": runtime(), "app": app("1.15.0"), "extensions": extensions(),
                 "media-tools": optional()}
    catalog = catalog_of(runtime(), app("1.15.1", b"new"), extensions(), optional())
    selection = resolve_components(catalog, {"app": "1.15.1"}, installed)
    assert set(selection) == {"app"}


def test_runtime_abi_bump_without_matching_app_is_a_conflict():
    installed = {"runtime": runtime(), "app": app("1.15.0")}
    new_runtime = runtime("2.0.0", runtime_api=2)
    catalog = catalog_of(new_runtime, app("1.15.0"))
    with pytest.raises(CatalogError, match="dependency_conflict|component_version_unavailable"):
        resolve_components(catalog, {"runtime": "2.0.0"}, installed)
    # with an app built for ABI 2 in the same catalog the pair moves together
    catalog = catalog_of(new_runtime, app("1.16.0", runtime_api=2))
    selection = resolve_components(catalog, {"runtime": "2.0.0"}, installed)
    assert {key: value["version"] for key, value in selection.items()} == {"runtime": "2.0.0", "app": "1.16.0"}


def test_monolith_migrates_to_split_set_and_keeps_optional_components():
    installed = {"core": legacy_core(), "media-tools": optional()}
    catalog = catalog_of(runtime(), app(), extensions(), optional())
    selection = resolve_components(catalog, {"runtime": None, "app": None, "extensions": None}, installed)
    assert set(selection) == {"runtime", "app", "extensions"}  # the optional pack is untouched


def test_optional_component_installs_against_split_generation_without_a_core_record():
    installed = {"runtime": runtime(), "app": app()}
    catalog = catalog_of(runtime(), app(), optional())
    assert set(resolve_components(catalog, {"media-tools": None}, installed)) == {"media-tools"}


# ---------------------------------------------------------------------- store
def _generation(store, tmp_path, versions):
    """Stage + prepare + activate + confirm a generation of runtime/app/extensions."""
    components = {name: make_pkg(store, tmp_path, name, ver, f"{name}-{ver}".encode())
                  for name, ver in versions.items()}
    pending = store.prepare(components, catalog_sequence=1)
    store.activate_pending()
    store.confirm_active(pending["generation"])
    return pending


def _versions(store):
    found = set()
    for id_dir in store.packages_dir.iterdir():
        for version_dir in id_dir.iterdir():
            found.add(f"{id_dir.name}/{version_dir.name}")
    return found


def test_store_rejects_runtime_without_app(tmp_path):
    store = ComponentStore(tmp_path / "App")
    only_runtime = make_pkg(store, tmp_path, "runtime", "1.0.0", b"rt")
    with pytest.raises(ComponentStoreError, match="runtime/app"):
        store.prepare({"runtime": only_runtime}, catalog_sequence=1)


def test_store_selects_runtime_as_launch_record(tmp_path):
    store = ComponentStore(tmp_path / "App")
    _generation(store, tmp_path, {"runtime": "1.0.0", "app": "1.15.0", "extensions": "1.0.0"})
    assert store.selected_record()["package"]["id"] == "runtime"


def _ext_generation(store, tmp_path, version, manifest):
    pkg = make_pkg(store, tmp_path, "extensions", version, manifest)
    return {"components": {"extensions": {"package": pkg, "path": f"packages/extensions/{version}",
                                          "version": version,
                                          "record_sha256": hashlib.sha256(version.encode()).hexdigest()}}}


def test_extension_sync_is_stable_and_only_touches_changed_files(tmp_path):
    root = tmp_path / "App"
    store = ComponentStore(root)
    first = _ext_generation(store, tmp_path, "1.0.0", b'{"v":1}')
    assert sync_extensions(root, first)["status"] == "updated"
    assert (root / "extension" / "manifest.json").read_bytes() == b'{"v":1}'
    assert (root / MARKER).is_file()
    before = (root / "extension" / "manifest.json").stat().st_mtime_ns
    again = sync_extensions(root, first)
    assert again == {"status": "current", "changed": False}  # no write -> no Chrome reload needed
    assert (root / "extension" / "manifest.json").stat().st_mtime_ns == before
    (root / "extension" / "stray.js").write_text("old file", encoding="utf-8")
    second = _ext_generation(store, tmp_path, "1.0.1", b'{"v":2}')
    result = sync_extensions(root, second)
    assert result["changed"] and result["copied"] == ["extension/manifest.json"]
    assert result["removed"] == ["extension/stray.js"]
    assert (root / "extension" / "manifest.json").read_bytes() == b'{"v":2}'
    assert (root / "extension-shopee" / "manifest.json").read_bytes() == b"{s}"


def test_extension_sync_repairs_a_damaged_folder(tmp_path):
    root = tmp_path / "App"
    store = ComponentStore(root)
    generation = _ext_generation(store, tmp_path, "1.0.0", b'{"v":1}')
    sync_extensions(root, generation)
    (root / "extension" / "manifest.json").write_bytes(b"tampered!!")
    assert sync_extensions(root, generation)["status"] == "updated"
    assert (root / "extension" / "manifest.json").read_bytes() == b'{"v":1}'


def test_extension_sync_without_package_is_a_noop(tmp_path):
    assert sync_extensions(tmp_path, {"components": {}})["status"] == "no_extensions_package"
    assert sync_extensions(tmp_path, None)["changed"] is False


# ------------------------------------------------------------------- launcher
def test_launcher_runs_runtime_and_points_it_at_the_verified_app(tmp_path):
    root = tmp_path / "App"
    store = ComponentStore(root)
    _generation(store, tmp_path, {"runtime": "1.0.0", "app": "1.15.0", "extensions": "1.0.0"})
    active = store.load_active()
    assert launcher._core_component(active)["package"]["id"] == "runtime"
    assert launcher._select_app(root, active) == (root / "packages" / "app" / "1.15.0").resolve()
    env = launcher._environment(root, tmp_path / "D", 48997, {"KIDTE_APP_PACKAGE_DIR": "stale"}, "g", "n",
                                None, None, launcher._select_app(root, active))
    assert env["KIDTE_APP_PACKAGE_DIR"].endswith(os.path.join("packages", "app", "1.15.0"))
    assert env["KIDTE_LAUNCHER_CAPS"] == launcher.LAUNCHER_CAPS
    legacy_env = launcher._environment(root, tmp_path / "D", 48997, {"KIDTE_APP_PACKAGE_DIR": "stale"}, "g", "n",
                                       None, None, None)
    assert "KIDTE_APP_PACKAGE_DIR" not in legacy_env


def test_launcher_refuses_a_tampered_app_entry_point(tmp_path):
    root = tmp_path / "App"
    store = ComponentStore(root)
    _generation(store, tmp_path, {"runtime": "1.0.0", "app": "1.15.0", "extensions": "1.0.0"})
    (root / "packages" / "app" / "1.15.0" / "backend" / "src" / "app_entry.py").write_bytes(b"evil")
    with pytest.raises(launcher.LauncherError, match="digest"):
        launcher._select_app(root, store.load_active())


def test_baseline_fallback_is_allowed_only_with_the_pinned_data_schema(tmp_path):
    store = ComponentStore(tmp_path / "App")
    _generation(store, tmp_path, {"runtime": "1.0.0", "app": "1.15.0", "extensions": "1.0.0"})
    component = launcher._core_component(store.load_active())
    assert launcher._baseline_compatible(component)
    component["_app"]["package"]["compatibility"] = {"runtime_api": 1, "data_schema": "other"}
    assert not launcher._baseline_compatible(component)


class _Proc:
    pid = 4321

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        pass


class _Job:
    def attach(self, process):
        pass

    def close(self):
        pass


def _launch(root, store, *, ready=True, gc_env=None):
    seen = {}

    def popen(command, cwd, env, creationflags):
        seen.update(command=command, env=env)
        seen["nonce"] = env["KIDTE_LAUNCH_NONCE"]
        return _Proc()

    def probe(port):
        if not ready or "nonce" not in seen:
            return None
        return {"ok": True, "pid": _Proc.pid, "launcher_nonce": seen["nonce"],
                "launcher_generation": seen["env"]["KIDTE_LAUNCH_GENERATION"],
                "data_root": str(root.parent / "DuLieu")}

    app = launcher.Launcher(store_factory=lambda _: store, popen=popen, ready_probe=probe,
                            occupied_probe=lambda _: False, job_factory=_Job, sleep=lambda _: None,
                            startup_timeout=1, environ={"CHAT_PORT": "48997", **(gc_env or {})})
    return app, seen


def test_installer_bootstrap_catalog_can_be_split(tmp_path):
    # baseline_matches/stage_baseline accept split packages laid down by the installer
    root = tmp_path / "App"
    store = ComponentStore(root)
    record, members = _record("app", "1.15.0", {APP_ENTRY: b"entry", "VERSION": b"1.15.0"})
    for name, data in members.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    assert store.baseline_matches(record)
    staged = store.stage_baseline(record)
    assert (staged / APP_ENTRY).read_bytes() == b"entry"
    assert staged == root / "packages" / "app" / "1.15.0"
    (root / APP_ENTRY).write_bytes(b"changed")
    assert not store.baseline_matches(record)


def test_prepare_installer_bootstrap_stages_split_baseline(tmp_path, monkeypatch):
    import base64
    from datetime import datetime, timedelta, timezone

    import update_trust
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from update_catalog import canonical_json

    root = tmp_path / "App"
    root.mkdir()
    store = ComponentStore(root)
    records = []
    for builder, name in ((runtime, "runtime"), (app, "app"), (extensions, "extensions")):
        record = builder()
        records.append(record)
        for row in record["files"]:
            target = root / row["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(members_of(record, record["entrypoint"] and
                                          {"runtime": b"rt", "app": b"app", "extensions": b"{}"}[name])[row["path"]])
    key = Ed25519PrivateKey.generate()
    now = datetime.now(timezone.utc)
    payload = {"schema_version": 2, "repository": "kidlost1412/KidTE", "channel": "beta", "sequence": 7,
               "issued_at": (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "expires_at": (now + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "components": records}
    envelope = {"schema_version": 2, "key_id": "boot", "signed": payload,
                "signature": base64.b64encode(key.sign(canonical_json(payload))).decode()}
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    monkeypatch.setitem(update_trust.TRUSTED_KEYS, "boot", base64.b64encode(public).decode())
    (root / "bootstrap-request.json").write_bytes(canonical_json(envelope))
    launcher._prepare_installer_core(root, store)
    pending = store.load_pending()
    assert set(pending["components"]) == {"runtime", "app", "extensions"}
    assert not (root / "bootstrap-request.json").exists()
    assert (root / "packages" / "app" / "1.15.0" / APP_ENTRY).is_file()


def test_extension_change_leaves_a_reload_notice_until_acknowledged(tmp_path):
    from extension_sync import NOTICE, clear_notice, read_notice
    root = tmp_path / "App"
    store = ComponentStore(root)
    first = _ext_generation(store, tmp_path, "1.0.0", b'{"v":1}')
    sync_extensions(root, first)
    assert read_notice(root)["version"] == "1.0.0"  # files were written -> Chrome must reload
    assert clear_notice(root) and read_notice(root) is None and not (root / NOTICE).exists()
    sync_extensions(root, first)  # nothing changed -> no new notice
    assert read_notice(root) is None
    sync_extensions(root, _ext_generation(store, tmp_path, "1.0.1", b'{"v":2}'))
    assert read_notice(root)["version"] == "1.0.1"
