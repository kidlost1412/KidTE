import base64
import hashlib
import json
import time
import zipfile
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
import server
from component_store import ComponentStore
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from update_catalog import CORE_DATA_SCHEMA, canonical_json
from update_manager import UpdateManager, select_active_gpu


def package(tmp_path, component="core", version="1.0.0"):
    name = "KidTE.exe" if component == "core" else "gpu-runtime.json"
    content = b"fixture" if component == "core" else b'{"fixture":true}'
    archive = tmp_path / f"{component}-{version}.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr(name, content)
    return archive, {"id": component, "kind": "core" if component == "core" else "gpu-runtime",
                     "version": version, "platform": "windows", "architecture": "x86_64",
                     "core_api": ">=1.0.0,<2.0.0", "dependencies": {},
                     "url": f"https://github.com/kidlost1412/KidTE/releases/download/test/{archive.name}",
                     "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                     "archive_bytes": archive.stat().st_size, "unpacked_bytes": len(content),
                     "files": [{"path": name, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}],
                     "entrypoint": name, "restart_required": True, "migration": "none",
                     "source_commit": "a" * 40,
                     "compatibility": {"data_schema": CORE_DATA_SCHEMA} if component == "core" else
                     {"python_abi": "cp311", "ctranslate2_version": "4.8.2", "cuda_major": 12, "cudnn_major": 9}}


def signed(packages, *, expired=False):
    key = Ed25519PrivateKey.generate()
    now = datetime.now(timezone.utc)
    payload = {"schema_version": 2, "repository": "kidlost1412/KidTE", "channel": "beta",
               "sequence": 1, "issued_at": (now - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "expires_at": (now + timedelta(days=-1 if expired else 1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "components": packages}
    envelope = {"schema_version": 2, "key_id": "test", "signed": payload,
                "signature": base64.b64encode(key.sign(canonical_json(payload))).decode()}
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return canonical_json(envelope), {"test": base64.b64encode(public).decode()}


class Transport:
    def __init__(self, raw, archives):
        self.raw = raw
        self.archives = archives
        self.calls = []

    def fetch_catalog(self, channel="stable"):
        self.calls.append("check")
        return self.raw

    def download(self, url, destination, *, expected_sha256, expected_bytes, progress=None):
        self.calls.append(url)
        destination.write_bytes(self.archives[url].read_bytes())
        if progress:
            progress(expected_bytes, expected_bytes)
        return destination


def done(manager):
    deadline = time.monotonic() + 5
    while manager.state()["busy"] and time.monotonic() < deadline:
        time.sleep(.01)
    state = manager.state()
    assert not state["busy"]
    return state


def test_gpu_preparation_retains_active_core_until_launcher_activation(tmp_path, monkeypatch):
    core_archive, core = package(tmp_path)
    gpu_archive, gpu = package(tmp_path, "gpu-cuda12")
    root = tmp_path / "App"
    store = ComponentStore(root)
    store.stage_package(core, core_archive)
    old = store.prepare({"core": core}, 1)
    store.activate_pending()
    store.confirm_active(old["generation"])
    raw, keys = signed([core, gpu])
    transport = Transport(raw, {gpu["url"]: gpu_archive})
    manager = UpdateManager(root, transport=transport, trusted_keys=keys, store=store)
    assert transport.calls == []
    manager.action("check", {})
    assert done(manager)["error"] == ""
    manager.action("install", {"components": {"gpu-cuda12": "1.0.0"}})
    state = done(manager)
    assert state["error"] == ""
    assert state["restart_required"]
    assert state["installed"] == {"core": "1.0.0"}
    assert state["pending"] == {"core": "1.0.0", "gpu-cuda12": "1.0.0"}
    assert store.load_active()["generation"] == old["generation"]
    assert len([call for call in transport.calls if call != "check"]) == 1
    store.recover()
    assert store.load_pending() is not None  # an older committed journal must not erase the next update
    active = store.activate_pending()
    store.confirm_active(active["generation"])
    monkeypatch.setenv("KIDTE_GPU_RUNTIME_ROOT", "")
    monkeypatch.setenv("KIDTE_GPU_RUNTIME_SHA256", "")
    select_active_gpu(root)
    assert __import__("os").environ["KIDTE_GPU_RUNTIME_SHA256"] == gpu["files"][0]["sha256"]


def test_installing_current_core_is_a_noop_and_later_feature_install_still_works(tmp_path):
    core_archive, core = package(tmp_path)
    gpu_archive, gpu = package(tmp_path, "gpu-cuda12")
    root = tmp_path / "App"
    store = ComponentStore(root)
    store.stage_package(core, core_archive)
    initial = store.prepare({"core": core}, 1)
    store.activate_pending()
    store.confirm_active(initial["generation"])
    raw, keys = signed([core, gpu])
    transport = Transport(raw, {gpu["url"]: gpu_archive})
    manager = UpdateManager(root, transport=transport, trusted_keys=keys, store=store)

    manager.action("check", {})
    assert done(manager)["error"] == ""
    plan = manager.action("plan", {"components": {"core": core["version"]}})
    assert plan["download_bytes"] == 0
    assert plan["packages"] == []
    manager.action("install", {"components": {"core": core["version"]}})
    assert done(manager)["error"] == ""
    assert store.load_active()["generation"] == initial["generation"]
    assert store.load_pending() is None
    assert transport.calls == ["check"]

    manager.action("install", {"components": {"gpu-cuda12": gpu["version"]}})
    state = done(manager)
    assert state["error"] == ""
    assert state["restart_required"]
    assert store.load_active()["generation"] == initial["generation"]
    assert set(store.load_pending()["components"]) == {"core", "gpu-cuda12"}
    assert transport.calls == ["check", gpu["url"]]


def test_batch_update_prepares_one_generation_without_intermediate_restart(tmp_path):
    old_archive, old_core = package(tmp_path, version="1.0.0")
    new_archive, new_core = package(tmp_path, version="1.0.1")
    gpu_archive, gpu = package(tmp_path, "gpu-cuda12")
    store = ComponentStore(tmp_path / "App")
    store.stage_package(old_core, old_archive)
    initial = store.prepare({"core": old_core}, 1)
    store.activate_pending()
    store.confirm_active(initial["generation"])
    raw, keys = signed([new_core, gpu])
    transport = Transport(raw, {new_core["url"]: new_archive, gpu["url"]: gpu_archive})
    manager = UpdateManager(store.app_root, transport=transport, trusted_keys=keys, store=store)
    manager.action("check", {})
    assert done(manager)["error"] == ""
    components = {"core": "1.0.1", "gpu-cuda12": "1.0.0"}
    plan = manager.action("plan", {"components": components})
    assert plan["download_bytes"] == new_core["archive_bytes"] + gpu["archive_bytes"]
    assert store.load_pending() is None
    manager.action("install", {"components": components})
    state = done(manager)
    assert state["error"] == ""
    assert state["pending"] == components
    assert state["installed"] == {"core": "1.0.0"}
    assert store.load_active()["generation"] == initial["generation"]
    assert transport.calls == ["check", new_core["url"], gpu["url"]]
    activated = store.activate_pending()
    assert {k: v["package"]["version"] for k, v in activated["components"].items()} == components


def test_first_install_auto_detects_gpu_and_does_not_reinstall_after_activation(tmp_path, monkeypatch):
    import initial_components
    core_archive, core = package(tmp_path)
    gpu_archive, gpu = package(tmp_path, "gpu-cuda12")
    store = ComponentStore(tmp_path / "App")
    store.stage_package(core, core_archive)
    initial = store.prepare({"core": core}, 1)
    store.activate_pending()
    store.confirm_active(initial["generation"])
    raw, keys = signed([core, gpu])
    transport = Transport(raw, {gpu["url"]: gpu_archive})
    marker = store.app_root / initial_components.MARKER
    marker.write_text('{"schema_version":1,"status":"requested"}')
    monkeypatch.setattr(initial_components, "detect_gpu", lambda: {"eligible": True, "name": "Test GPU"})
    manager = UpdateManager(store.app_root, transport=transport, trusted_keys=keys, store=store)
    assert manager.initial_setup()
    state = done(manager)
    assert state["error"] == ""
    assert state["setup_hardware"]["eligible"]
    assert state["pending"] == {"core": "1.0.0", "gpu-cuda12": "1.0.0"}
    assert json.loads(marker.read_text())["status"] == "prepared"
    assert not manager.initial_setup()
    active = store.activate_pending()
    store.confirm_active(active["generation"])
    reopened = UpdateManager(store.app_root, transport=transport, trusted_keys=keys, store=store)
    assert not reopened.initial_setup()
    assert json.loads(marker.read_text())["status"] == "complete"
    assert transport.calls == ["check", gpu["url"]]


def test_first_install_network_failure_keeps_request_for_next_start(tmp_path, monkeypatch):
    import initial_components
    marker = tmp_path / initial_components.MARKER
    marker.write_text('{"schema_version":1,"status":"requested"}')
    monkeypatch.setattr(initial_components, "detect_gpu", lambda: {"eligible": False})
    manager = UpdateManager(tmp_path, transport=SimpleNamespace(fetch_catalog=lambda **kw: (_ for _ in ()).throw(OSError("network down"))))
    assert manager.initial_setup()
    assert done(manager)["error"] == "network down"
    assert json.loads(marker.read_text())["status"] == "requested"
    assert manager._get_store().load_pending() is None


def test_expired_catalog_and_unsigned_payload_never_prepare(tmp_path):
    archive, core = package(tmp_path)
    raw, keys = signed([core], expired=True)
    store = ComponentStore(tmp_path / "App")
    manager = UpdateManager(store.app_root, transport=Transport(raw, {core["url"]: archive}), trusted_keys=keys)
    manager.action("check", {})
    assert done(manager)["error"] == "catalog_expired_or_not_yet_valid"
    with pytest.raises(ValueError, match="update_check_required"):
        manager.action("install", {"components": {"core": None}})
    assert store.load_active() is None and store.load_pending() is None


def test_ready_and_update_routes_are_local_only_and_do_not_call_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "ROOT", tmp_path / "DuLieu")
    monkeypatch.setenv("KIDTE_LAUNCH_NONCE", "nonce")
    monkeypatch.setenv("KIDTE_LAUNCH_GENERATION", "generation")
    seen = []
    manager = SimpleNamespace(state=lambda: {"ok": True, "enabled": True},
                              action=lambda action, body: seen.append((action, body)) or {"ok": True, "accepted": True})
    engine = SimpleNamespace(updates=manager, health=lambda: pytest.fail("provider health must not be called"))

    class Handler(server.Handler):
        pass

    Handler.engine = engine
    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=http.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
    worker.start()
    origin = f"http://127.0.0.1:{http.server_port}"

    def call(path, body=None, request_origin=origin):
        request = Request(origin + path, data=None if body is None else json.dumps(body).encode(),
                          headers={"Origin": request_origin, "Content-Type": "application/json"})
        with urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read())

    try:
        _, ready = call("/api/update-ready")
        assert ready["launcher_nonce"] == "nonce" and ready["launcher_generation"] == "generation"
        assert ready["data_root"] == str(tmp_path / "DuLieu")
        assert call("/api/updates")[1]["enabled"]
        assert call("/api/updates/check", {})[0] == 202
        for route, body in (("/api/updates", None), ("/api/update-ready", None), ("/api/updates/install", {})):
            with pytest.raises(HTTPError) as error:
                call(route, body, "chrome-extension://foreign")
            assert error.value.code == 403
        assert seen == [("check", {})]
    finally:
        http.shutdown()
        http.server_close()
        worker.join(2)


def test_default_update_channel_is_beta_until_stable_catalog_exists(tmp_path):
    manager = UpdateManager(tmp_path / "app", trusted_keys={"k": object()})
    assert manager._channel == "beta"
    seen = []
    manager.transport = SimpleNamespace(fetch_catalog=lambda channel: seen.append(channel) or (_ for _ in ()).throw(ValueError("stop")))
    manager._get_store = lambda: SimpleNamespace(read_highwater=lambda: 0)
    manager._run_async = lambda *a, **k: None
    import inspect
    src = inspect.getsource(UpdateManager.action)
    assert 'payload.get("channel", "beta")' in src
    assert '{"stable", "beta"}' in src            # stable stays selectable
    with pytest.raises(ValueError, match="update_channel_invalid"):
        manager.action("check", {"channel": "nightly"})


def test_updates_page_defaults_to_beta_and_keeps_stable_option():
    from pathlib import Path
    src = (Path(server.__file__).parent / "web/app/pages/updates.js").read_text(encoding="utf-8")
    assert "state.channel || 'beta'" in src
    assert "['stable'," in src and "['beta'," in src


def _carried_setup(tmp_path):
    core_archive, core = package(tmp_path)
    gpu_archive, gpu = package(tmp_path, "gpu-cuda12")
    root = tmp_path / "App"
    store = ComponentStore(root)
    store.stage_package(core, core_archive)
    old = store.prepare({"core": core}, 1)
    store.activate_pending()
    store.confirm_active(old["generation"])
    raw, keys = signed([core, gpu])
    transport = Transport(raw, {gpu["url"]: gpu_archive, core["url"]: core_archive})
    manager = UpdateManager(root, transport=transport, trusted_keys=keys, store=store)
    manager.action("check", {})
    assert done(manager)["error"] == ""
    return manager, store, transport, core, gpu, root / "packages" / "core" / core["version"]


def test_install_trims_stray_extra_file_in_unchanged_component(tmp_path):
    manager, store, transport, core, gpu, core_dir = _carried_setup(tmp_path)
    (core_dir / "svc.out").write_bytes(b"")
    manager.action("install", {"components": {"gpu-cuda12": "1.0.0"}})
    state = done(manager)
    assert state["error"] == "" and state["pending"] == {"core": "1.0.0", "gpu-cuda12": "1.0.0"}
    assert not (core_dir / "svc.out").exists()
    assert any(p.name.startswith(".trash-") for p in store.staging_dir.iterdir())
    assert core["url"] not in transport.calls  # nothing re-downloaded for extras


def test_install_redownloads_unchanged_component_with_modified_or_missing_file(tmp_path):
    manager, store, transport, core, gpu, core_dir = _carried_setup(tmp_path)
    (core_dir / "KidTE.exe").write_bytes(b"tampered")
    manager.action("install", {"components": {"gpu-cuda12": "1.0.0"}})
    assert done(manager)["error"] == ""
    assert (core_dir / "KidTE.exe").read_bytes() == b"fixture"
    assert transport.calls.count(core["url"]) == 1
    store.cancel_pending()
    (core_dir / "KidTE.exe").unlink()
    manager.action("install", {"components": {"gpu-cuda12": "1.0.0"}})
    assert done(manager)["error"] == ""
    assert (core_dir / "KidTE.exe").read_bytes() == b"fixture"


def test_install_fails_with_precise_message_when_repair_impossible(tmp_path):
    manager, store, transport, core, gpu, core_dir = _carried_setup(tmp_path)
    (core_dir / "KidTE.exe").write_bytes(b"tampered")
    transport.archives.pop(core["url"])  # download of the same version fails
    manager.action("install", {"components": {"gpu-cuda12": "1.0.0"}})
    state = done(manager)
    assert "core 1.0.0" in state["error"] and "KidTE.exe" in state["error"]
    assert store.load_pending() is None
    assert (core_dir / "KidTE.exe").read_bytes() == b"tampered"  # untouched when repair fails


def test_state_reports_launcher_rollback_and_ack_clears_it(tmp_path):
    root = tmp_path / "App"
    root.mkdir()
    (root / "update-failure.json").write_text(json.dumps({
        "failed_version": "1.15.16", "restored_version": "1.15.15",
        "reason_code": "startup_timeout", "at": "2026-10-18T01:00:00Z"}), encoding="utf-8")
    manager = UpdateManager(root, trusted_keys={}, launcher_caps="split-components")
    failure = manager.state()["last_update_failure"]
    assert failure["failed_version"] == "1.15.16" and failure["restored_version"] == "1.15.15"
    assert "Bản 1.15.16" in failure["message"] and "1.15.15" in failure["message"]
    assert manager.action("ack_update_failure", {})["ok"] is True
    assert manager.state()["last_update_failure"] is None
