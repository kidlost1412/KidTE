"""Local garbage collection of superseded package versions (keep active + one rollback)."""
import os
import time

import pytest

import update_launcher as launcher
from component_store import ComponentStore

from test_split_components import GRACE0, _generation, _launch, _versions, make_pkg


def test_gc_keeps_exactly_active_plus_one_rollback(tmp_path):
    store = ComponentStore(tmp_path / "App")
    _generation(store, tmp_path, {"runtime": "1.0.0", "app": "1.15.0", "extensions": "1.0.0"})
    assert store.collect_garbage(**GRACE0)["removed"] == []
    _generation(store, tmp_path, {"runtime": "1.0.0", "app": "1.15.1", "extensions": "1.0.0"})
    assert store.collect_garbage(**GRACE0)["removed"] == []  # 1.15.0 is the rollback target
    assert _versions(store) == {"runtime/1.0.0", "extensions/1.0.0", "app/1.15.0", "app/1.15.1"}
    _generation(store, tmp_path, {"runtime": "1.0.0", "app": "1.15.2", "extensions": "1.0.0"})
    report = store.collect_garbage(**GRACE0)
    assert report["removed"] == ["app/1.15.0"]
    assert _versions(store) == {"runtime/1.0.0", "extensions/1.0.0", "app/1.15.1", "app/1.15.2"}
    assert store.load_active()["components"]["app"]["version"] == "1.15.2"
    # the kept rollback is still a valid, activatable generation
    store.request_rollback()


def test_gc_does_not_delete_young_versions_or_anything_during_pending(tmp_path):
    store = ComponentStore(tmp_path / "App")
    _generation(store, tmp_path, {"runtime": "1.0.0", "app": "1.15.0", "extensions": "1.0.0"})
    _generation(store, tmp_path, {"runtime": "1.0.0", "app": "1.15.1", "extensions": "1.0.0"})
    young = make_pkg(store, tmp_path, "app", "1.15.9", b"staged-not-prepared")
    assert young
    _generation(store, tmp_path, {"runtime": "1.0.0", "app": "1.15.2", "extensions": "1.0.0"})
    # default grace keeps recently staged versions: an install may be about to prepare them
    assert store.collect_garbage()["removed"] == []
    old = time.time() - 7200
    for path in [store.packages_dir / "app" / "1.15.0", store.packages_dir / "app" / "1.15.9"]:
        os.utime(path, (old, old))
    assert sorted(store.collect_garbage()["removed"]) == ["app/1.15.0", "app/1.15.9"]
    # pending generation (update or user rollback in progress): GC does nothing at all
    store.request_rollback()
    report = store.collect_garbage(**GRACE0)
    assert report["skipped"] == "pending_generation" and report["removed"] == []


def test_gc_skips_unconfirmed_activation_and_damaged_metadata(tmp_path):
    store = ComponentStore(tmp_path / "App")
    _generation(store, tmp_path, {"runtime": "1.0.0", "app": "1.15.0", "extensions": "1.0.0"})
    components = {name: make_pkg(store, tmp_path, name, ver, f"{name}-{ver}".encode())
                  for name, ver in {"runtime": "1.0.0", "app": "1.15.1", "extensions": "1.0.0"}.items()}
    store.prepare(components, catalog_sequence=2)
    store.activate_pending()  # journal: probing (no health confirmation yet)
    store.pending_path.unlink(missing_ok=True)
    assert store.collect_garbage(**GRACE0)["skipped"] == "activation_in_progress"
    store.rollback()  # unhealthy -> rolled back
    store.previous_path.write_text("{not json", encoding="utf-8")
    assert store.collect_garbage(**GRACE0)["skipped"].startswith("metadata_unreadable")
    assert _versions(store) >= {"app/1.15.0", "app/1.15.1"}


def test_gc_is_crash_safe_and_next_run_sweeps_the_trash(tmp_path):
    def crash(name):
        if name == "gc_renamed":
            raise RuntimeError("power cut")

    root = tmp_path / "App"
    store = ComponentStore(root)
    for version in ("1.15.0", "1.15.1", "1.15.2"):
        _generation(store, tmp_path, {"runtime": "1.0.0", "app": version, "extensions": "1.0.0"})
    crashing = ComponentStore(root, crash_hook=crash)
    with pytest.raises(RuntimeError):
        crashing.collect_garbage(**GRACE0)
    # app/1.15.0 is gone from packages/ (renamed away) and sits in staging; mappings are intact
    assert "app/1.15.0" not in _versions(store)
    assert list(store.staging_dir.glob(".trash-*"))
    assert store.load_active()["components"]["app"]["version"] == "1.15.2"
    store.request_rollback()
    store.cancel_pending()
    store.collect_garbage(**GRACE0)
    assert not list(store.staging_dir.glob(".trash-*"))


def test_gc_sweeps_stale_staging_but_not_fresh_or_recent_downloads(tmp_path):
    store = ComponentStore(tmp_path / "App")
    _generation(store, tmp_path, {"runtime": "1.0.0", "app": "1.15.0", "extensions": "1.0.0"})
    old, fresh = time.time() - 3 * 86400, time.time()
    stale_dir = store.staging_dir / "app-1.0.0-dead"
    (stale_dir / "sub").mkdir(parents=True)
    (stale_dir / "sub" / "f.bin").write_bytes(b"x")
    fresh_dir = store.staging_dir / "app-1.0.1-live"
    fresh_dir.mkdir()
    (fresh_dir / "f.bin").write_bytes(b"x")
    downloads = store.download_root()
    (downloads / "old.zip").write_bytes(b"x")
    (downloads / "new.zip").write_bytes(b"x")
    for path in (stale_dir / "sub" / "f.bin", stale_dir / "sub", stale_dir, downloads / "old.zip"):
        os.utime(path, (old, old))
    os.utime(fresh_dir, (fresh, fresh))
    report = store.collect_garbage(**GRACE0)
    assert "app-1.0.0-dead" in report["staging_removed"] and "downloads/old.zip" in report["staging_removed"]
    assert not stale_dir.exists() and fresh_dir.exists()
    assert (downloads / "new.zip").exists() and not (downloads / "old.zip").exists()


def test_gc_ignores_baseline_without_active_generation(tmp_path):
    store = ComponentStore(tmp_path / "App")
    make_pkg(store, tmp_path, "app", "1.15.0", b"app")
    report = store.collect_garbage(**GRACE0)
    assert report["skipped"] == "no_active_generation" and _versions(store) == {"app/1.15.0"}


# ------------------------------------------------------------- extension sync
def test_launcher_activates_split_generation_syncs_extensions_and_collects_garbage(tmp_path, monkeypatch):
    root = tmp_path / "App"
    root.mkdir()
    (root / "KidTE.exe").write_bytes(b"baseline")
    store = ComponentStore(root)
    for version in ("1.15.0", "1.15.1"):
        _generation(store, tmp_path, {"runtime": "1.0.0", "app": version, "extensions": "1.0.0"})
    components = {name: make_pkg(store, tmp_path, name, ver, f"{name}-{ver}".encode())
                  for name, ver in {"runtime": "1.0.0", "app": "1.15.2", "extensions": "1.0.0"}.items()}
    store.prepare(components, catalog_sequence=3)
    # make the (about to be superseded) 1.15.0 look old enough for GC
    old = time.time() - 7200
    os.utime(store.packages_dir / "app" / "1.15.0", (old, old))
    monkeypatch.setattr(launcher, "_preflight", lambda *a, **k: None)
    app, seen = _launch(root, store)
    assert app.run(root / "KidTE.exe") == 0
    assert seen["command"][0].endswith(os.path.join("packages", "runtime", "1.0.0", "KidTE.exe"))
    assert seen["env"]["KIDTE_APP_PACKAGE_DIR"].endswith(os.path.join("packages", "app", "1.15.2"))
    assert (root / "extension" / "manifest.json").is_file()  # stable Chrome path populated
    assert store.load_active()["components"]["app"]["version"] == "1.15.2"
    assert _versions(store) == {"runtime/1.0.0", "extensions/1.0.0", "app/1.15.1", "app/1.15.2"}


def test_launcher_does_not_collect_when_the_new_generation_never_became_ready(tmp_path, monkeypatch):
    root = tmp_path / "App"
    root.mkdir()
    (root / "KidTE.exe").write_bytes(b"baseline")
    store = ComponentStore(root)
    for version in ("1.15.0", "1.15.1"):
        _generation(store, tmp_path, {"runtime": "1.0.0", "app": version, "extensions": "1.0.0"})
    components = {name: make_pkg(store, tmp_path, name, ver, f"{name}-{ver}".encode())
                  for name, ver in {"runtime": "1.0.0", "app": "1.15.2", "extensions": "1.0.0"}.items()}
    store.prepare(components, catalog_sequence=3)
    monkeypatch.setattr(launcher, "_preflight", lambda *a, **k: None)
    app, seen = _launch(root, store, ready=False)
    with pytest.raises(launcher.LauncherError):
        app.run(root / "KidTE.exe")
    # rolled back to 1.15.1; nothing deleted (the 1.15.2 package stays for a retry)
    assert store.load_active()["components"]["app"]["version"] == "1.15.1"
    assert {"app/1.15.0", "app/1.15.1", "app/1.15.2"} <= _versions(store)
