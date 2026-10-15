"""D1/D2/D3/A1: không mất dữ liệu khi đọc lỗi, khôi phục V1, gói thiếu."""
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

import backup_policy
import privacy
import update_launcher
from component_store import ComponentStore
from test_component_store import _package
from test_system_panels import _point_backup_at
from workspace import StoreReadError, _read_json_strict


# ---------------------------------------------------------------- D2
def test_read_json_strict_missing_is_default_corrupt_raises_and_keeps_file(tmp_path):
    path = tmp_path / "local_product_overrides.json"
    assert _read_json_strict(path, {"x": 1}) == {"x": 1}
    path.write_text('{"overrides": {"A": ', encoding="utf-8")
    with pytest.raises(StoreReadError):
        _read_json_strict(path, {}, attempts=2, delay=0)
    assert path.read_text(encoding="utf-8") == '{"overrides": {"A": '
    assert list(tmp_path.glob("local_product_overrides.json.corrupt-*"))


def test_read_json_strict_retries_transient_lock(tmp_path, monkeypatch):
    path = tmp_path / "o.json"
    path.write_text('{"ok": 1}', encoding="utf-8")
    real, calls = Path.read_text, []

    def flaky(self, *a, **k):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError("locked by AV")
        return real(self, *a, **k)
    monkeypatch.setattr(Path, "read_text", flaky)
    assert _read_json_strict(path, {}, delay=0) == {"ok": 1}


# ---------------------------------------------------------------- D3
def test_privacy_key_only_created_when_absent(tmp_path):
    key = tmp_path / "machine" / "privacy_key"
    first = privacy.PrivacyService(tmp_path / "a.sqlite3", key)
    secret = key.read_bytes()
    first.close()
    assert len(secret) == 32
    key.write_bytes(b"short")
    service = privacy.PrivacyService(tmp_path / "b.sqlite3", key)
    try:
        assert key.read_bytes() == b"short"  # không ghi đè
        assert service.unavailable_reason
        assert service.is_forgotten("tiktok:abc") is False  # không chặn trả lời
        assert service.memory_available is False
        with pytest.raises(privacy.PrivacyUnavailableError):
            service.key_hmac("tiktok:abc")
    finally:
        service.close()


# ---------------------------------------------------------------- D1
def test_v1_restore_keeps_customers_forgotten_after_backup(tmp_path, monkeypatch):
    data = _point_backup_at(tmp_path, monkeypatch)
    key = data / "machine" / "privacy_key"
    monkeypatch.setattr(backup_policy, "PRIVACY_KEY_PATH", key)
    db = data / "memory.sqlite3"
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE customer_facts (customer_key TEXT, fact TEXT)")
        c.executemany("INSERT INTO customer_facts VALUES (?,?)",
                      [("tiktok:an", "x"), ("tiktok:binh", "y")])
    made = backup_policy.create_operational_backup(parts=["khach_hang"])
    service = privacy.PrivacyService(db, key)
    try:
        service.forget("tiktok:an")
    finally:
        service.close()
    with sqlite3.connect(db) as c:
        c.execute("DELETE FROM customer_facts WHERE customer_key='tiktok:an'")
    result = backup_policy.restore_operational_backup(made["backup_id"], parts=["khach_hang"])
    assert result["privacy_reapplied"]["tombstones"] == 1
    with sqlite3.connect(db) as c:
        keys = {r[0] for r in c.execute("SELECT customer_key FROM customer_facts")}
        assert keys == {"tiktok:binh"}
        assert c.execute("SELECT COUNT(*) FROM privacy_tombstones").fetchone()[0] == 1


def test_v1_restore_refuses_without_privacy_key_when_tombstones_exist(tmp_path, monkeypatch):
    data = _point_backup_at(tmp_path, monkeypatch)
    key = data / "machine" / "privacy_key"
    monkeypatch.setattr(backup_policy, "PRIVACY_KEY_PATH", key)
    made = backup_policy.create_operational_backup(parts=["khach_hang"])
    service = privacy.PrivacyService(data / "memory.sqlite3", key)
    service.forget("tiktok:an")
    service.close()
    key.unlink()
    with pytest.raises(ValueError, match="backup_privacy_secret_required"):
        backup_policy.restore_operational_backup(made["backup_id"], parts=["khach_hang"])


# ---------------------------------------------------------------- A1
def _optional(archive):
    pkg = _package(archive, members={"tools/ffmpeg.exe": b"ff", "tools/ffprobe.exe": b"fp"})
    pkg.update({"id": "media-tools", "kind": "media-tools", "entrypoint": "tools/ffmpeg.exe",
                "compatibility": {"component_api": 1}, "dependencies": {"core": ">=1.14.255,<2.0.0"}})
    return pkg


def _activate(store, packages, seq):
    store.prepare(packages, seq)
    active = store.activate_pending()
    store.confirm_active(active["generation"])
    return active


def test_launcher_drops_missing_optional_package_and_keeps_core(tmp_path):
    store = ComponentStore(tmp_path / "install")
    core = _package(tmp_path / "core.zip", version="1.15.0")
    store.stage_package(core, tmp_path / "core.zip")
    media = _optional(tmp_path / "media.zip")
    store.stage_package(media, tmp_path / "media.zip")
    _activate(store, {"core": core, "media-tools": media}, 2)
    shutil.rmtree(tmp_path / "install" / "packages" / "media-tools")
    update_launcher._recover_store(tmp_path / "install", store)
    active = store.load_active()
    assert set(active["components"]) == {"core"}
    log = (tmp_path / "install" / "launcher-startup.log").read_text(encoding="utf-8")
    assert "media-tools" in log


def test_launcher_rolls_back_when_active_core_dir_missing(tmp_path):
    store = ComponentStore(tmp_path / "install")
    old = _package(tmp_path / "1.zip", version="1.0.0", content=b"one")
    new = _package(tmp_path / "2.zip", version="1.1.0", content=b"two")
    store.stage_package(old, tmp_path / "1.zip")
    store.stage_package(new, tmp_path / "2.zip")
    first = _activate(store, {"core": old}, 2)
    _activate(store, {"core": new}, 3)
    shutil.rmtree(tmp_path / "install" / "packages" / "core" / "1.1.0")
    update_launcher._recover_store(tmp_path / "install", store)
    assert store.load_active()["generation"] == first["generation"]


def test_launcher_missing_core_without_previous_falls_back_or_reports(tmp_path):
    root = tmp_path / "install"
    store = ComponentStore(root)
    core = _package(tmp_path / "1.zip")
    store.stage_package(core, tmp_path / "1.zip")
    _activate(store, {"core": core}, 2)
    (root / "previous.json").unlink()
    shutil.rmtree(root / "packages" / "core")
    with pytest.raises(update_launcher.LauncherError, match="cài lại"):
        update_launcher._recover_store(root, store)  # chưa có KidTE.exe gốc
    (root / "KidTE.exe").write_bytes(b"baseline")
    update_launcher._recover_store(root, store)
    assert store.load_active() is None


def test_broken_privacy_key_keeps_replies_but_disables_customer_memory(tmp_path):
    from comment_queue import CommentQueue
    from customer_memory import CustomerMemory
    from delivery_store import DeliveryStore
    import server

    db = tmp_path / "db.sqlite3"
    key = tmp_path / "machine" / "privacy_key"
    key.parent.mkdir(parents=True)
    key.write_bytes(b"bad")
    service = privacy.PrivacyService(db, key)
    memory = CustomerMemory(db, tmp_path / "journal", privacy=service)
    queue = CommentQueue(db, privacy=service)
    delivery = DeliveryStore(db, privacy=service)
    try:
        ck = memory.customer_key(platform_user_id="9")[0]
        assert queue.enqueue("tiktok", "room", ck, {"comment_id": "1", "comment": "Giá?"})["state"] == "queued"
        ref = memory.record_comment("tôi ở Cần Thơ, giá bao nhiêu?", platform_user_id="9")
        assert ref["interaction_id"] and not ref.get("opted_out")
        assert memory.db_path.exists()
        with sqlite3.connect(db) as c:
            assert c.execute("SELECT COUNT(*) FROM customer_facts").fetchone()[0] == 0
        assert memory.context_for_reply(ck) == ""
        assert memory.card_source(ck) is None
        delivery.prepare("t1", "tiktok", "room", ref["interaction_id"], ["Dạ giá 100k ạ"])
        assert memory.forget_if_requested("xóa dữ liệu của tôi", platform_user_id="9")
        fake = type("E", (), {"privacy": service})()
        status = server.ReplyEngine._privacy_status(fake)
        assert status["memory_available"] is False and "Bộ nhớ khách tạm tắt" in status["warning"]
    finally:
        delivery.close(); queue.close(); memory.close(); service.close()
