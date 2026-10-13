import hashlib
import zipfile
from multiprocessing import Event, Process

import pytest
from component_store import ComponentStore, ComponentStoreError
from update_catalog import CORE_DATA_SCHEMA


def _lock_worker(root, acquired, release):
    store = ComponentStore(root)
    with store._locked():
        acquired.set()
        release.wait(5)


def _package(archive_path, *, version="1.0.0", content=b"core binary", members=None):
    members = members or {"KidTE.exe": content}
    rows = [{"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in members.items()]
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    raw = archive_path.read_bytes()
    return {
        "id": "core", "kind": "core", "version": version, "platform": "windows",
        "architecture": "x86_64", "core_api": ">=1.0.0,<2.0.0", "dependencies": {},
        "url": "https://github.com/kidlost1412/KidTE/releases/download/v1.0.0/core.zip",
        "archive_sha256": hashlib.sha256(raw).hexdigest(), "archive_bytes": len(raw),
        "unpacked_bytes": sum(row["size"] for row in rows), "files": rows,
        "entrypoint": "KidTE.exe", "restart_required": True, "migration": "none",
        "source_commit": "a" * 40, "compatibility": {"data_schema": CORE_DATA_SCHEMA},
    }


def _staged(root, version="1.0.0", data=b"core binary"):
    archive = root / f"{version}.zip"
    package = _package(archive, version=version, content=data)
    store = ComponentStore(root / "install")
    installed = store.stage_package(package, archive)
    return store, package, installed


def test_stage_verifies_signed_inventory_and_is_immutable(tmp_path):
    store, package, installed = _staged(tmp_path)
    assert (installed / "KidTE.exe").read_bytes() == b"core binary"
    assert store.stage_package(package, tmp_path / "1.0.0.zip") == installed

    (installed / "KidTE.exe").write_bytes(b"changed")
    with pytest.raises(ComponentStoreError, match="different contents"):
        store.stage_package(package, tmp_path / "1.0.0.zip")
    assert (installed / "KidTE.exe").read_bytes() == b"changed"


@pytest.mark.parametrize("bad_name", ["../evil", "C:/evil", "safe/file:stream", "CON.txt", "a\\b"])
def test_stage_rejects_windows_unsafe_archive_paths(tmp_path, bad_name):
    archive = tmp_path / "bad.zip"
    package = _package(archive, members={bad_name: b"x", "KidTE.exe": b"x"})
    with pytest.raises(ComponentStoreError):
        ComponentStore(tmp_path / "install").stage_package(package, archive)


def test_stage_rejects_unlisted_archive_files_even_when_archive_digest_matches(tmp_path):
    archive = tmp_path / "extra.zip"
    package = _package(archive, members={"KidTE.exe": b"x", "unlisted.txt": b"other"})
    package["files"] = [row for row in package["files"] if row["path"] == "KidTE.exe"]
    package["unpacked_bytes"] = 1
    with pytest.raises(ComponentStoreError, match="extra or missing"):
        ComponentStore(tmp_path / "install").stage_package(package, archive)


def test_stage_rejects_file_digest_mismatch(tmp_path):
    archive = tmp_path / "digest.zip"
    package = _package(archive)
    package["files"][0]["sha256"] = "0" * 64
    with pytest.raises(ComponentStoreError, match="digest mismatch"):
        ComponentStore(tmp_path / "install").stage_package(package, archive)


def test_stage_rejects_case_collisions_and_symlink_zip_entries(tmp_path):
    duplicate = tmp_path / "duplicate.zip"
    with zipfile.ZipFile(duplicate, "w") as archive:
        archive.writestr("KidTE.exe", b"a")
        archive.writestr("kidte.EXE", b"b")
    package = _package(tmp_path / "expected.zip", members={"KidTE.exe": b"a"})
    package["archive_bytes"] = duplicate.stat().st_size
    package["archive_sha256"] = hashlib.sha256(duplicate.read_bytes()).hexdigest()
    with pytest.raises(ComponentStoreError, match="duplicate or case"):
        ComponentStore(tmp_path / "install").stage_package(package, duplicate)

    linkzip = tmp_path / "link.zip"
    with zipfile.ZipFile(linkzip, "w") as archive:
        info = zipfile.ZipInfo("KidTE.exe")
        info.external_attr = (0o120777 << 16)
        archive.writestr(info, "target")
    package = _package(tmp_path / "expected2.zip", members={"KidTE.exe": b"target"})
    package["archive_bytes"] = linkzip.stat().st_size
    package["archive_sha256"] = hashlib.sha256(linkzip.read_bytes()).hexdigest()
    with pytest.raises(ComponentStoreError, match="non-regular"):
        ComponentStore(tmp_path / "install2").stage_package(package, linkzip)


def test_prepare_then_confirmed_activation_persists_generation_and_highwater(tmp_path):
    store, package, _ = _staged(tmp_path)
    pending = store.prepare({"core": package}, 4)
    assert store.load_active() is None
    assert store.load_pending() == pending
    assert store.read_highwater() == 4
    activated = store.activate_pending()
    assert activated["generation"] == pending["generation"]
    assert activated["components"]["core"]["package"] == package
    assert store.confirm_active(activated["generation"]) == activated
    assert store.recover() == activated
    assert store.load_pending() is None


@pytest.mark.parametrize("checkpoint", ["previous_written", "journal_prepared",
                                          "before_replace:active.json", "active_written", "probing"])
def test_recovery_rolls_back_any_unconfirmed_activation_crash(tmp_path, checkpoint):
    store, package, _ = _staged(tmp_path)
    initial = store.prepare({"core": package}, 1)
    store.activate_pending()
    store.confirm_active(initial["generation"])

    next_archive = tmp_path / "2.0.0.zip"
    next_package = _package(next_archive, version="2.0.0", content=b"new core")
    store.stage_package(next_package, next_archive)
    second = store.prepare({"core": next_package}, 2)

    def crash_at(name):
        if name == checkpoint:
            raise RuntimeError("simulated power loss")

    crash_store = ComponentStore(tmp_path / "install", crash_hook=crash_at)
    with pytest.raises(RuntimeError, match="power loss"):
        crash_store.activate_pending()
    recovered = ComponentStore(tmp_path / "install").recover()
    assert recovered == initial
    assert ComponentStore(tmp_path / "install").read_highwater() == 2
    assert second["generation"] != recovered["generation"]
    pending_after_recovery = ComponentStore(tmp_path / "install").load_pending()
    if checkpoint == "previous_written":
        assert pending_after_recovery == second
        assert ComponentStore(tmp_path / "install").recover() == initial
        assert ComponentStore(tmp_path / "install").load_pending() == second
    else:
        assert pending_after_recovery is None


def test_committed_generation_survives_restart_after_commit_checkpoint(tmp_path):
    store, package, _ = _staged(tmp_path)
    pending = store.prepare({"core": package}, 3)
    active = store.activate_pending()

    def crash_at(name):
        if name == "committed":
            raise RuntimeError("power loss after commit")

    with pytest.raises(RuntimeError):
        ComponentStore(tmp_path / "install", crash_hook=crash_at).confirm_active(active["generation"])
    assert ComponentStore(tmp_path / "install").recover() == pending
    assert ComponentStore(tmp_path / "install").load_pending() is None


def test_read_and_recovery_apis_do_not_rehash_large_payloads(tmp_path, monkeypatch):
    store, package, _ = _staged(tmp_path)
    pending = store.prepare({"core": package}, 3)
    store.activate_pending()
    store.confirm_active(pending["generation"])

    def unexpected_hash(*_args, **_kwargs):
        raise AssertionError("metadata read rehashed installed payload")

    monkeypatch.setattr(store, "_verify_existing", unexpected_hash)
    assert store.load_active()["generation"] == pending["generation"]
    assert store.recover()["generation"] == pending["generation"]


def test_corrupt_pending_package_is_rejected_before_active_mapping_changes(tmp_path):
    store, first_package, installed = _staged(tmp_path)
    first = store.prepare({"core": first_package}, 1)
    store.activate_pending()
    store.confirm_active(first["generation"])

    second_archive = tmp_path / "2.0.0.zip"
    second_package = _package(second_archive, version="2.0.0", content=b"second")
    second_dir = store.stage_package(second_package, second_archive)
    second = store.prepare({"core": second_package}, 2)
    (second_dir / "KidTE.exe").write_bytes(b"tampered")

    with pytest.raises(ComponentStoreError, match="inventory"):
        store.activate_pending()
    assert store.load_active() == first
    assert store.load_pending() is None
    assert installed.exists()
    assert second["generation"] != first["generation"]


def test_recovery_after_rolled_back_journal_preserves_next_pending_generation(tmp_path):
    store, first_package, _ = _staged(tmp_path)
    first = store.prepare({"core": first_package}, 1)
    store.activate_pending()
    store.confirm_active(first["generation"])

    second_archive = tmp_path / "2.0.0.zip"
    second_package = _package(second_archive, version="2.0.0", content=b"second")
    store.stage_package(second_package, second_archive)
    store.prepare({"core": second_package}, 2)

    def fail_after_switch(name):
        if name == "probing":
            raise RuntimeError("simulated startup failure")

    with pytest.raises(RuntimeError):
        ComponentStore(tmp_path / "install", crash_hook=fail_after_switch).activate_pending()
    assert ComponentStore(tmp_path / "install").recover() == first
    assert ComponentStore(tmp_path / "install").load_pending() is None

    third = store.prepare({"core": second_package}, 2)

    def fail_before_new_journal(name):
        if name == "previous_written":
            raise RuntimeError("simulated power loss")

    with pytest.raises(RuntimeError):
        ComponentStore(tmp_path / "install", crash_hook=fail_before_new_journal).activate_pending()
    restarted = ComponentStore(tmp_path / "install")
    assert restarted.recover() == first
    assert restarted.load_pending() == third


def test_rollback_is_queued_for_next_launcher_and_never_lowers_sequence(tmp_path):
    store, package, _ = _staged(tmp_path)
    first = store.prepare({"core": package}, 6)
    store.activate_pending()
    store.confirm_active(first["generation"])
    newer_archive = tmp_path / "2.0.0.zip"
    newer = _package(newer_archive, version="2.0.0", content=b"new")
    store.stage_package(newer, newer_archive)
    store.prepare({"core": newer}, 9)
    store.activate_pending()
    store.confirm_active(store.load_active()["generation"])

    rollback = store.request_rollback()
    assert rollback["components"]["core"]["package"]["version"] == "1.0.0"
    assert store.load_active()["components"]["core"]["package"]["version"] == "2.0.0"
    assert store.read_highwater() == 9
    assert store.activate_pending()["components"]["core"]["package"]["version"] == "1.0.0"
    assert store.read_highwater() == 9
    with pytest.raises(ComponentStoreError, match="rollback"):
        store.prepare({"core": package}, 8)


def test_prepare_rejects_missing_staged_package_and_cancel_keeps_versions(tmp_path):
    archive = tmp_path / "missing.zip"
    package = _package(archive)
    store = ComponentStore(tmp_path / "install")
    with pytest.raises(ComponentStoreError, match="Gói core 1.0.0"):
        store.prepare({"core": package}, 1)
    store.stage_package(package, archive)
    store.prepare({"core": package}, 1)
    store.cancel_pending()
    assert store.load_pending() is None
    assert (store.packages_dir / "core" / package["version"] / "KidTE.exe").exists()


def test_stage_fails_before_writing_when_disk_budget_is_insufficient(tmp_path, monkeypatch):
    import component_store

    archive = tmp_path / "budget.zip"
    package = _package(archive)
    store = ComponentStore(tmp_path / "install")
    monkeypatch.setattr(component_store.shutil, "disk_usage",
                        lambda _: type("Usage", (), {"free": 0})())
    with pytest.raises(ComponentStoreError, match="insufficient disk space"):
        store.stage_package(package, archive)
    assert not (store.packages_dir / "core" / package["version"]).exists()


def test_process_lock_serializes_two_store_instances(tmp_path):
    root = tmp_path / "install"
    first = ComponentStore(root)
    acquired, release = Event(), Event()
    worker = Process(target=_lock_worker, args=(root, acquired, release))
    with first._locked():
        worker.start()
        assert acquired.wait(0.25) is False
    try:
        assert acquired.wait(3)
    finally:
        release.set()
        worker.join(3)
    assert worker.exitcode == 0


def test_store_refuses_reparse_point_in_app_ancestry(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlink creation unavailable")
    with pytest.raises(ComponentStoreError, match="reparse point"):
        ComponentStore(alias / "install")


def test_inspect_package_classifies_extra_missing_and_modified(tmp_path):
    store, package, installed = _staged(tmp_path)
    assert store.inspect_package(package)["state"] == "ok"
    (installed / "svc.out").write_bytes(b"")
    report = store.inspect_package(package)
    assert report["state"] == "extra" and report["extra"] == ["svc.out"]
    (installed / "KidTE.exe").write_bytes(b"changed")
    assert store.inspect_package(package)["state"] == "damaged"
    (installed / "KidTE.exe").unlink()
    report = store.inspect_package(package)
    assert report["missing"] == ["KidTE.exe"] and "KidTE.exe" in store.describe_problem(package, report)


def test_quarantine_extras_moves_files_into_staging_trash(tmp_path):
    store, package, installed = _staged(tmp_path)
    (installed / "plugins").mkdir()
    (installed / "plugins" / "svc.out").write_bytes(b"log")
    report = store.inspect_package(package)
    trash = store.quarantine_extras(package, report["extra"])
    assert trash.parent == store.staging_dir and trash.name.startswith(".trash-")
    assert (trash / "plugins" / "svc.out").read_bytes() == b"log"
    assert not (installed / "plugins" / "svc.out").exists()
    assert store.inspect_package(package)["state"] == "ok"
    store.prepare({"core": package}, 1)


def test_prepare_error_names_component_and_file(tmp_path):
    store, package, installed = _staged(tmp_path)
    (installed / "plugins").mkdir()
    (installed / "plugins" / "svc.out").write_bytes(b"")
    with pytest.raises(ComponentStoreError) as caught:
        store.prepare({"core": package}, 1)
    message = str(caught.value)
    assert "core 1.0.0" in message and "plugins/svc.out" in message and "thừa" in message


def test_restage_package_replaces_damaged_dir_and_restores_on_failure(tmp_path):
    store, package, installed = _staged(tmp_path)
    archive = tmp_path / "1.0.0.zip"
    (installed / "KidTE.exe").write_bytes(b"changed")
    store.restage_package(package, archive)
    assert store.inspect_package(package)["state"] == "ok"
    (installed / "KidTE.exe").write_bytes(b"changed")
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not a zip")
    with pytest.raises(ComponentStoreError):
        store.restage_package(package, bad)
    assert (installed / "KidTE.exe").read_bytes() == b"changed"  # old directory put back
