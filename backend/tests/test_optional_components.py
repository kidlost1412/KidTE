"""Contract tests for optional update packages and baseline core reuse."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import shutil
import sys
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from component_store import ComponentStore
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from update_catalog import (
    COMPONENT_POLICIES,
    CORE_DATA_SCHEMA,
    CatalogError,
    canonical_json,
    resolve_components,
    verify_catalog,
)
from update_manager import UpdateManager

PROJECT = Path(__file__).resolve().parents[2]


def _load_stage_runtime():
    path = PROJECT / "packaging" / "stage_runtime.py"
    spec = importlib.util.spec_from_file_location("optional_stage_runtime", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


stage_runtime = _load_stage_runtime()


def _package(tmp_path, component, version="1.0.0", *, files=None, dependencies=None,
             compatibility=None):
    default_files = {
        "core": {"KidTE.exe": f"core-{version}".encode()},
        "gpu-cuda12": {"gpu-runtime.json": b'{"fixture":true}'},
        "media-tools": {"tools/ffmpeg.exe": b"ffmpeg", "tools/ffprobe.exe": b"ffprobe"},
        "captcha-solver": {
            "Captcha/KidTECaptcha.exe": b"solver",
            "Captcha/_internal/captcha-monitor/monitor.ps1": b"monitor",
        },
        "video-sync": {"KidTEVideo.exe": b"video-sync"},
        "video-analysis-cpu": {"KidTETranscription.exe": b"transcription"},
    }
    contents = files or default_files[component]
    kind, entrypoint = COMPONENT_POLICIES[component]
    if dependencies is None:
        dependencies = {
            "core": {},
            "gpu-cuda12": {"core": ">=1.0.0,<2.0.0"},
            "media-tools": {"core": ">=1.14.255,<2.0.0"},
            "captcha-solver": {"core": ">=1.14.255,<2.0.0",
                               "media-tools": ">=1.0.0,<2.0.0"},
            "video-sync": {"core": ">=1.14.255,<2.0.0"},
            "video-analysis-cpu": {"core": ">=1.14.255,<2.0.0",
                                    "media-tools": ">=1.0.0,<2.0.0"},
        }[component]
    if compatibility is None:
        compatibility = {
            "core": {"data_schema": CORE_DATA_SCHEMA},
            "gpu-cuda12": {"python_abi": "cp311", "ctranslate2_version": "4.8.2",
                            "cuda_major": 12, "cudnn_major": 9},
            "video-analysis-cpu": {"protocol": 1, "python_abi": "cp311",
                                   "ctranslate2_version": "4.8.2"},
        }.get(component, {"component_api": 1})

    archive = tmp_path / f"{component}-{version}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for relative, data in contents.items():
            output.writestr(relative, data)
    rows = [{"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in contents.items()]
    return archive, {
        "id": component,
        "kind": kind,
        "version": version,
        "platform": "windows",
        "architecture": "x86_64",
        "core_api": ">=1.0.0,<2.0.0",
        "dependencies": dict(dependencies),
        "url": f"https://github.com/kidlost1412/KidTE/releases/download/p3-test/{archive.name}",
        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "archive_bytes": archive.stat().st_size,
        "unpacked_bytes": sum(row["size"] for row in rows),
        "files": rows,
        "entrypoint": entrypoint,
        "restart_required": True,
        "migration": "none",
        "source_commit": "a" * 40,
        "compatibility": dict(compatibility),
    }


def _signed_catalog(packages, *, sequence=1):
    key = Ed25519PrivateKey.generate()
    now = datetime.now(timezone.utc)
    payload = {
        "schema_version": 2,
        "repository": "kidlost1412/KidTE",
        "channel": "stable",
        "sequence": sequence,
        "issued_at": (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "components": packages,
    }
    envelope = {
        "schema_version": 2,
        "key_id": "optional-test",
        "signed": payload,
        "signature": base64.b64encode(key.sign(canonical_json(payload))).decode("ascii"),
    }
    public = key.public_key().public_bytes(serialization.Encoding.Raw,
                                           serialization.PublicFormat.Raw)
    return canonical_json(envelope), {"optional-test": base64.b64encode(public).decode("ascii")}


def _verify(packages):
    raw, keys = _signed_catalog(packages)
    return verify_catalog(raw, keys)


class _Transport:
    def __init__(self, catalog, archives):
        self.catalog = catalog
        self.archives = archives
        self.downloads = []

    def fetch_catalog(self, channel="stable"):
        return self.catalog

    def download(self, url, destination, *, expected_sha256, expected_bytes, progress=None):
        self.downloads.append(url)
        shutil.copyfile(self.archives[url], destination)
        if progress:
            progress(expected_bytes, expected_bytes)
        return destination


def _wait(manager):
    deadline = time.monotonic() + 5
    while manager.state()["busy"] and time.monotonic() < deadline:
        time.sleep(0.01)
    state = manager.state()
    assert not state["busy"]
    return state


def _active_component_set(tmp_path, component_ids):
    """Create a confirmed active generation without invoking the updater or network."""
    app_root = tmp_path / "InstalledApp"
    app_root.mkdir()
    store = ComponentStore(app_root)
    packages = {}
    for component_id in component_ids:
        archive, package = _package(
            tmp_path, component_id,
            "1.14.255" if component_id == "core" else "1.0.0",
        )
        store.stage_package(package, archive)
        packages[component_id] = package
    store.prepare(packages, catalog_sequence=1)
    active = store.activate_pending()
    store.confirm_active(active["generation"])
    return app_root, store, store.load_active()


@pytest.mark.parametrize(("edit", "error"), [
    (lambda pkg: pkg.update(id="captcha"), "unsupported_component"),
    (lambda pkg: pkg.update(kind="worker"), "unsupported_component"),
    (lambda pkg: pkg.update(extra="not allowed"), "invalid_package_fields"),
    (lambda pkg: pkg.update(compatibility={"component_api": 2}), "invalid_optional_component"),
    (lambda pkg: pkg["dependencies"].pop("core"), "invalid_optional_component"),
    (lambda pkg: pkg["dependencies"].update({"media-tools": ">=1.0.0"}),
     "invalid_optional_component"),
])
def test_optional_component_catalog_schema_is_strict(tmp_path, edit, error):
    _, solver = _package(tmp_path, "captcha-solver")
    edit(solver)
    with pytest.raises(CatalogError, match=error):
        _verify([solver])


def test_resolver_changes_only_requested_feature_and_retains_core_gpu(tmp_path):
    _, core = _package(tmp_path, "core", "1.14.255")
    _, gpu = _package(tmp_path, "gpu-cuda12")
    _, media = _package(tmp_path, "media-tools")
    _, old_solver = _package(tmp_path, "captcha-solver", dependencies={
        "core": ">=1.14.255,<2.0.0", "media-tools": ">=1.0.0,<2.0.0"})
    _, next_solver = _package(tmp_path, "captcha-solver", "1.1.0", dependencies={
        "core": ">=1.14.255,<2.0.0", "media-tools": ">=1.0.0,<2.0.0"})
    installed = {"core": core, "gpu-cuda12": gpu, "media-tools": media,
                 "captcha-solver": old_solver}
    selected = resolve_components({"components": [next_solver]},
                                  {"captcha-solver": "1.1.0"}, installed=installed,
                                  core_api="1.14.255")
    assert set(selected) == {"captcha-solver"}
    resulting = {**installed, **selected}
    assert resulting["core"] is core
    assert resulting["gpu-cuda12"] is gpu
    assert resulting["captcha-solver"]["version"] == "1.1.0"


def test_captcha_selection_adds_media_dependency_and_fails_if_unavailable(tmp_path):
    _, core = _package(tmp_path, "core", "1.14.255")
    _, solver = _package(tmp_path, "captcha-solver")
    _, media = _package(tmp_path, "media-tools")
    catalog = {"components": [core, solver, media]}
    selected = resolve_components(catalog, {"captcha-solver": None}, core_api="1.14.255")
    assert set(selected) == {"core", "media-tools", "captcha-solver"}
    with pytest.raises(CatalogError, match="component_version_unavailable"):
        resolve_components({"components": [core, solver]}, {"captcha-solver": None},
                           core_api="1.14.255")


def test_baseline_stage_copies_files_without_hardlinks(tmp_path):
    app_root = tmp_path / "App"
    app_root.mkdir()
    source = app_root / "KidTE.exe"
    source.write_bytes(b"signed baseline")
    _, core = _package(tmp_path, "core", "1.14.255", files={"KidTE.exe": b"signed baseline"})
    store = ComponentStore(app_root)

    assert store.baseline_matches(core)
    staged = store.stage_baseline(core)
    installed = staged / "KidTE.exe"
    assert installed.read_bytes() == b"signed baseline"
    assert not installed.samefile(source)
    source.write_bytes(b"later installer mutation")
    assert installed.read_bytes() == b"signed baseline"


def test_update_plan_reuses_local_core_and_install_downloads_only_optional_packages(tmp_path):
    app_root = tmp_path / "App"
    app_root.mkdir()
    (app_root / "KidTE.exe").write_bytes(b"baseline core")
    core_archive, core = _package(tmp_path, "core", "1.14.255",
                                  files={"KidTE.exe": b"baseline core"})
    media_archive, media = _package(tmp_path, "media-tools")
    solver_archive, solver = _package(tmp_path, "captcha-solver")
    raw, keys = _signed_catalog([core, media, solver])
    transport = _Transport(raw, {media["url"]: media_archive, solver["url"]: solver_archive,
                                 core["url"]: core_archive})
    store = ComponentStore(app_root)
    manager = UpdateManager(app_root, transport=transport, trusted_keys=keys, store=store)
    manager.action("check", {"channel": "stable"})
    assert _wait(manager)["error"] == ""

    plan = manager.action("plan", {"components": {"captcha-solver": "1.0.0"}})
    by_id = {row["id"]: row for row in plan["packages"]}
    assert by_id["core"]["local_reuse"] is True
    assert by_id["core"]["download_bytes"] == 0
    assert plan["download_bytes"] == media["archive_bytes"] + solver["archive_bytes"]

    manager.action("install", {"components": {"captcha-solver": "1.0.0"}})
    assert _wait(manager)["error"] == ""
    assert set(transport.downloads) == {media["url"], solver["url"]}
    pending = store.load_pending()
    assert set(pending["components"]) == {"core", "media-tools", "captcha-solver"}
    copied_core = app_root / pending["components"]["core"]["path"] / "KidTE.exe"
    assert copied_core.read_bytes() == b"baseline core"
    assert not copied_core.samefile(app_root / "KidTE.exe")


def test_tampered_baseline_is_not_reused_and_core_is_downloaded(tmp_path):
    app_root = tmp_path / "App"
    app_root.mkdir()
    (app_root / "KidTE.exe").write_bytes(b"tampered")
    core_archive, core = _package(tmp_path, "core", "1.14.255",
                                  files={"KidTE.exe": b"signed core"})
    media_archive, media = _package(tmp_path, "media-tools")
    solver_archive, solver = _package(tmp_path, "captcha-solver")
    raw, keys = _signed_catalog([core, media, solver])
    transport = _Transport(raw, {core["url"]: core_archive, media["url"]: media_archive,
                                 solver["url"]: solver_archive})
    store = ComponentStore(app_root)
    manager = UpdateManager(app_root, transport=transport, trusted_keys=keys, store=store)
    manager.action("check", {"channel": "stable"})
    assert _wait(manager)["error"] == ""

    plan = manager.action("plan", {"components": {"captcha-solver": "1.0.0"}})
    core_plan = next(row for row in plan["packages"] if row["id"] == "core")
    assert core_plan["local_reuse"] is False
    assert core_plan["download_bytes"] == core["archive_bytes"]
    assert store.baseline_matches(core) is False

    manager.action("install", {"components": {"captcha-solver": "1.0.0"}})
    assert _wait(manager)["error"] == ""
    assert set(transport.downloads) == {core["url"], media["url"], solver["url"]}
    pending = store.load_pending()
    installed_core = app_root / pending["components"]["core"]["path"] / "KidTE.exe"
    assert installed_core.read_bytes() == b"signed core"


def _minimal_core_tree(root):
    required = [
        "KidTE.exe", "VERSION",
        "_internal/backend/src/web/operator.html",
        "_internal/backend/src/web/kid-te-icon.png",
        "_internal/backend/src/web/app/bootstrap.js",
        "_internal/backend/src/web/styles/tokens.css",
        "_internal/backend/prompts/system_prompt.txt",
        "_internal/backend/prompts/sales_playbook.default.json",
        "_internal/backend/prompts/knowledge_seed.json",
        "_internal/certifi/cacert.pem",
    ]
    for plugin_id, names in stage_runtime.PLUGIN_FILES.items():
        required.extend(f"plugins/{plugin_id}/{name}" for name in names)
    required.extend(f"extension/{name}" for name in stage_runtime.EXTENSION_FILES)
    required.extend(f"extension-shopee/{name}" for name in stage_runtime.SHOPEE_EXTENSION_FILES)
    required.extend(f"docs/{name}" for name in stage_runtime.DOC_FILES)
    for relative in required:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")


def test_minimal_core_validation_excludes_heavy_runtime_and_requires_baseline_files(tmp_path):
    _minimal_core_tree(tmp_path)
    result = stage_runtime.validate(tmp_path, profile="cpu", minimal=True)
    assert result["ok"] is True
    assert not (tmp_path / "_internal" / "ctranslate2").exists()
    assert not (tmp_path / "_internal" / "faster_whisper").exists()

    seed = tmp_path / "_internal/backend/prompts/knowledge_seed.json"
    seed.unlink()
    with pytest.raises(RuntimeError, match="missing") as error:
        stage_runtime.validate(tmp_path, profile="cpu", minimal=True)
    assert "_internal/backend/prompts/knowledge_seed.json" in str(error.value)


@pytest.mark.parametrize("component_id", ["gpu-cuda12", "captcha-solver"])
def test_remove_optional_package_only_changes_pending_mapping(tmp_path, component_id):
    app_root, store, active_before = _active_component_set(
        tmp_path, ["core", "gpu-cuda12", "media-tools", "captcha-solver", "video-sync"]
    )
    package_dirs = {
        key: app_root / value["path"]
        for key, value in active_before["components"].items()
    }
    manager = UpdateManager(app_root, store=store, transport=object(), trusted_keys={})

    accepted = manager.action("remove", {"component": component_id})
    assert accepted == {"ok": True, "accepted": True}
    assert store.load_active() == active_before
    state = _wait(manager)
    assert state["error"] == ""

    pending = store.load_pending()
    assert set(pending["components"]) == set(active_before["components"]) - {component_id}
    assert store.load_active() == active_before
    assert all(
        (package_dirs[key] / row["path"]).is_file()
        for key, installed in active_before["components"].items()
        for row in installed["files"]
    )


def test_remove_media_tools_is_blocked_while_solver_depends_on_it(tmp_path):
    app_root, store, active_before = _active_component_set(
        tmp_path, ["core", "media-tools", "captcha-solver", "video-analysis-cpu"]
    )
    manager = UpdateManager(app_root, store=store, transport=object(), trusted_keys={})

    with pytest.raises(ValueError, match="component_in_use"):
        manager.action("remove", {"component": "media-tools"})

    assert store.load_pending() is None
    assert store.load_active() == active_before
    assert (app_root / active_before["components"]["media-tools"]["path"]).is_dir()


def test_component_in_use_has_actionable_friendly_ui_error():
    api_source = (PROJECT / "backend" / "src" / "web" / "app" / "core" / "api.js").read_text(
        encoding="utf-8"
    )
    assert "component_in_use: 'Gói đang được một tính năng khác sử dụng. Gỡ tính năng phụ thuộc trước.'" in api_source
    assert "if (MESSAGES[code]) return MESSAGES[code];" in api_source
