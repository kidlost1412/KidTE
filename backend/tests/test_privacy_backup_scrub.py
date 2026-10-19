"""Quên khách → làm sạch bản sao lưu V1/V2 + schema_backups (an toàn: lỗi thì giữ bản cũ)."""
import hashlib
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import backup_policy
from backup import create_snapshot, restore_backup, verify_backup
from backup_v2 import BackupV2
from privacy import PrivacyService, hmac_for_key, load_secret_from_path

ALICE, BOB = "tiktok:id:111222", "tiktok:id:333444"
ALICE_NICK, BOB_NICK = "AliceNickXYZ", "BobNickQRS"
META = {"app_version": "t", "schema_version": 2, "solver_hash": "t", "coverage": ["data"]}
PASSWORD = "mat-khau-test-123"


def _make_memory_db(path):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE customers(customer_key TEXT PRIMARY KEY, display_name TEXT)")
        db.execute("CREATE TABLE interactions(interaction_id INTEGER PRIMARY KEY, customer_key TEXT, comment TEXT)")
        db.execute("CREATE TABLE customer_facts(customer_key TEXT, fact TEXT)")
        db.execute("CREATE TABLE memory_opt_outs(customer_key TEXT PRIMARY KEY, display_name TEXT NOT NULL DEFAULT '',"
                   " opted_out_at TEXT NOT NULL)")
        for key, nick in ((ALICE, ALICE_NICK), (BOB, BOB_NICK)):
            db.execute("INSERT INTO customers VALUES(?,?)", (key, nick))
            for i in range(30):
                db.execute("INSERT INTO interactions(customer_key,comment) VALUES(?,?)",
                           (key, f"{nick} hỏi giá lần {i} " + "x" * 200))
            db.execute("INSERT INTO customer_facts VALUES(?,?)", (key, f"{nick} ở Cần Thơ"))
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return path


def _make_workspace_db(path, secret):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE corrections(correction_id TEXT PRIMARY KEY, changes_json TEXT,"
                   " customer_key_hmac TEXT NOT NULL DEFAULT '')")
        db.execute("CREATE TABLE operator_snapshots(snapshot_id TEXT PRIMARY KEY, resource TEXT, record_id TEXT,"
                   " data_json TEXT)")
        for key, nick, uid in ((ALICE, ALICE_NICK, "111222"), (BOB, BOB_NICK, "333444")):
            db.execute("INSERT INTO corrections VALUES(?,?,?)",
                       ("c-" + uid, json.dumps({"reply": f"Chào {nick}"}), hmac_for_key(secret, key)))
            db.execute("INSERT INTO operator_snapshots VALUES(?,?,?,?)",
                       ("s-" + uid, "conversations", uid,
                        json.dumps({"user_id": uid, "nickname": nick, "comment": f"{nick} mua"})))
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "DuLieu"
    data, backups = root / "data", root / "backups"
    (data / "machine").mkdir(parents=True)
    backups.mkdir()
    key_path = data / "machine" / "privacy_key"
    mem = _make_memory_db(data / "customer_memory.sqlite3")
    svc = PrivacyService(mem, key_path)
    secret = load_secret_from_path(key_path)
    ws = _make_workspace_db(data / "operator_workspace.sqlite3", secret)
    schema_dir = data / "schema_backups"
    schema_dir.mkdir()
    _make_memory_db(schema_dir / "customer_memory-before-delivery-v2.sqlite3")
    for name, value in (("BACKUP_DIR", backups), ("DATA_DIR", data), ("MEMORY_DB", mem),
                        ("OPERATOR_WORKSPACE_DB", ws), ("AI_LEARNING_DB", data / "ai_learning.sqlite3"),
                        ("PRIVACY_KEY_PATH", key_path)):
        monkeypatch.setattr(backup_policy, name, value)
    yield {"root": root, "data": data, "backups": backups, "mem": mem, "ws": ws, "svc": svc,
           "secret": secret, "key": key_path, "schema": schema_dir}
    svc.close()


def _v1(env, name="20261001T000000Z"):
    from datetime import datetime, timezone
    shop = env["data"] / "shop.json"
    shop.write_text('{"ten": "shop"}', encoding="utf-8")
    return create_snapshot(backup_root=env["backups"], now=datetime(2026, 10, 1, tzinfo=timezone.utc),
                           sqlite_files={"customer_memory.sqlite3": env["mem"],
                                         "operator_workspace.sqlite3": env["ws"]},
                           json_files={"thong_tin_shop.json": shop},
                           extra_manifest={"scope": "full", "reason": "auto"})


def _v2(env):
    store = BackupV2(env["backups"] / "v2")
    sources = {"data/customer_memory.sqlite3": {"source": env["mem"], "kind": "sqlite"},
               "data/operator_workspace.sqlite3": {"source": env["ws"], "kind": "sqlite"}}
    bundle = json.dumps({"schema_version": 1, "files": []}).encode()
    manifest = store.create(sources, metadata=META, full=True, secrets=bundle, password=PASSWORD,
                            privacy_db=env["mem"])
    return store, manifest["id"]


def _forget_alice(env):
    env["svc"].forget(ALICE)


def _tree_bytes(folder):
    return b"".join(p.read_bytes() for p in sorted(Path(folder).rglob("*")) if p.is_file())


def _tree_digest(folder):
    h = hashlib.sha256()
    for p in sorted(Path(folder).rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(folder)).encode() + b"\0" + p.read_bytes())
    return h.hexdigest()


def _keys(path):
    with closing(sqlite3.connect(path)) as db, db:
        return sorted(r[0] for r in db.execute("SELECT customer_key FROM customers"))


def _contains(blob, *needles):
    return any(n.encode() in blob for n in needles)


def test_v1_v2_schema_scrubbed_bob_kept_and_restorable(env, tmp_path):
    backup_dir = _v1(env)
    store, backup_id = _v2(env)
    assert _contains(_tree_bytes(backup_dir), ALICE_NICK, ALICE)
    _forget_alice(env)

    result = backup_policy.scrub_backups_for_privacy()
    assert not result["pending"] and not result.get("error")
    assert set(result["cleaned"]) == {"v1:" + backup_dir.name, "v2:" + backup_id,
                                      "schema:customer_memory-before-delivery-v2.sqlite3"}

    # V1: không còn chuỗi nickname/khoá của Alice ở bất kỳ byte nào; Bob còn nguyên.
    blob = _tree_bytes(backup_dir)
    assert not _contains(blob, ALICE_NICK, ALICE, "111222")
    assert _contains(blob, BOB_NICK, BOB)
    verify_backup(backup_dir)
    out_mem, out_ws = tmp_path / "r" / "mem.sqlite3", tmp_path / "r" / "ws.sqlite3"
    restore_backup(backup_dir, {"customer_memory.sqlite3": out_mem, "operator_workspace.sqlite3": out_ws})
    assert _keys(out_mem) == [BOB]
    with closing(sqlite3.connect(out_mem)) as db:
        assert db.execute("SELECT COUNT(*) FROM interactions").fetchone()[0] == 30
    with closing(sqlite3.connect(out_ws)) as db:
        assert [r[0] for r in db.execute("SELECT correction_id FROM corrections")] == ["c-333444"]
        assert [r[0] for r in db.execute("SELECT snapshot_id FROM operator_snapshots")] == ["s-333444"]

    # V2: mọi đối tượng còn lại không chứa Alice; bó bí mật vẫn giải mã được (id giữ nguyên).
    objects = _tree_bytes(store.objects)
    assert not _contains(objects, ALICE_NICK, ALICE, "111222")
    assert _contains(objects, BOB_NICK)
    store.verify(backup_id, password=PASSWORD, deep=True)
    stage = tmp_path / "stage"
    store.stage_restore(backup_id, stage, password=PASSWORD, current_privacy_db=env["mem"],
                        privacy_key_path=env["key"])
    assert _keys(stage / "data" / "customer_memory.sqlite3") == [BOB]

    # schema_backups
    schema = env["schema"] / "customer_memory-before-delivery-v2.sqlite3"
    assert not _contains(schema.read_bytes(), ALICE_NICK, ALICE)
    assert _keys(schema) == [BOB]
    assert not list(env["schema"].glob(".scrub-*"))
    assert not [p for p in env["backups"].iterdir() if p.name.startswith(".scrub")]


def test_second_run_is_idempotent(env):
    backup_dir = _v1(env)
    store, backup_id = _v2(env)
    _forget_alice(env)
    backup_policy.scrub_backups_for_privacy()
    before = (_tree_digest(backup_dir), _tree_digest(store.root), _tree_digest(env["schema"]))
    again = backup_policy.scrub_backups_for_privacy()
    assert again["cleaned"] == [] and again["skipped"] == 3 and not again["pending"]
    assert (_tree_digest(backup_dir), _tree_digest(store.root), _tree_digest(env["schema"])) == before
    # Mất tệp trạng thái: chạy lại vẫn không đổi gì (0 dòng xoá → không ghi lại).
    (env["backups"] / backup_policy.SCRUB_STATE_NAME).unlink()
    third = backup_policy.scrub_backups_for_privacy()
    assert third["cleaned"] == [] and third["unchanged"] == 3
    assert (_tree_digest(backup_dir), _tree_digest(store.root), _tree_digest(env["schema"])) == before
    verify_backup(backup_dir)
    store.verify(backup_id, password=PASSWORD)


def test_not_run_while_live(env):
    backup_dir = _v1(env)
    _forget_alice(env)
    before = _tree_digest(backup_dir)
    result = backup_policy.scrub_backups_for_privacy(should_stop=lambda: True)
    assert result["deferred"] and result["cleaned"] == []
    assert _tree_digest(backup_dir) == before
    scheduler = backup_policy.PrivacyScrubScheduler(live_provider=lambda: True)
    assert scheduler.run_once() == {"deferred": True}
    broken = backup_policy.PrivacyScrubScheduler(live_provider=lambda: 1 / 0)
    assert broken.run_once() == {"deferred": True}            # không biết → coi như LIVE
    assert _tree_digest(backup_dir) == before


def test_backup_lock_busy_defers(env):
    backup_dir = _v1(env)
    _forget_alice(env)
    before = _tree_digest(backup_dir)
    import threading
    held, release = threading.Event(), threading.Event()

    def hold():
        with backup_policy.OPERATIONAL_BACKUP_LOCK:
            held.set()
            release.wait(5)

    t = threading.Thread(target=hold)
    t.start()
    held.wait(5)
    try:
        assert backup_policy.scrub_backups_for_privacy()["deferred"]
    finally:
        release.set()
        t.join()
    assert _tree_digest(backup_dir) == before


def test_missing_key_keeps_everything_and_marks_pending(env):
    backup_dir = _v1(env)
    store, backup_id = _v2(env)
    _forget_alice(env)
    before = (_tree_digest(backup_dir), _tree_digest(store.root), _tree_digest(env["schema"]))
    env["key"].unlink()
    result = backup_policy.scrub_backups_for_privacy()
    assert set(result["pending"].values()) == {"privacy_secret_missing"} and len(result["pending"]) == 3
    assert (_tree_digest(backup_dir), _tree_digest(store.root), _tree_digest(env["schema"])) == before
    status = backup_policy.scrub_status()
    assert status["clean"] == 0 and len(status["pending"]) == 3


def test_verify_failure_keeps_old_and_retries_later(env, monkeypatch):
    backup_dir = _v1(env)
    store, backup_id = _v2(env)
    _forget_alice(env)
    before = (_tree_digest(backup_dir), _tree_digest(store.root), _tree_digest(env["schema"]))
    monkeypatch.setattr(backup_policy, "scrub_remaining", lambda *a, **k: 1)   # kiểm "còn sót"
    result = backup_policy.scrub_backups_for_privacy()
    assert set(result["pending"].values()) == {"backup_scrub_incomplete"} and len(result["pending"]) == 3
    assert (_tree_digest(backup_dir), _tree_digest(store.root), _tree_digest(env["schema"])) == before
    assert not [p for p in env["backups"].iterdir() if p.name.startswith(".scrub")]
    assert not list(store.root.glob(".scrub-*")) and not list(env["schema"].glob(".scrub-*"))
    monkeypatch.undo()
    for name, value in (("BACKUP_DIR", env["backups"]), ("DATA_DIR", env["data"]), ("MEMORY_DB", env["mem"]),
                        ("OPERATOR_WORKSPACE_DB", env["ws"]), ("PRIVACY_KEY_PATH", env["key"]),
                        ("AI_LEARNING_DB", env["data"] / "ai_learning.sqlite3")):
        monkeypatch.setattr(backup_policy, name, value)
    retry = backup_policy.scrub_backups_for_privacy()
    assert len(retry["cleaned"]) == 3 and not retry["pending"]
    assert not _contains(_tree_bytes(backup_dir), ALICE_NICK)


def test_v1_swap_failure_rolls_back(env, monkeypatch):
    backup_dir = _v1(env)
    _forget_alice(env)
    before = _tree_digest(backup_dir)
    real = os.replace

    def failing(src, dst):
        if Path(src).name.startswith(".scrub-new-") and Path(dst).name == backup_dir.name:
            raise OSError(28, "No space left on device")
        return real(src, dst)

    monkeypatch.setattr(backup_policy.os, "replace", failing)
    result = backup_policy.scrub_backups_for_privacy()
    monkeypatch.setattr(backup_policy.os, "replace", real)
    assert "v1:" + backup_dir.name in result["pending"]
    assert _tree_digest(backup_dir) == before
    verify_backup(backup_dir)
    assert not [p for p in env["backups"].iterdir() if p.name.startswith(".scrub")]


def test_v2_write_failure_keeps_manifest_and_objects(env, monkeypatch):
    store, backup_id = _v2(env)
    _forget_alice(env)
    before = _tree_digest(store.root / "manifests"), sorted(p.name for p in store.objects.rglob("*") if p.is_file())

    def disk_full(self, *a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(BackupV2, "_publish_object", disk_full)
    result = backup_policy.scrub_backups_for_privacy()
    monkeypatch.undo()
    assert result["pending"].get("v2:" + backup_id) == "OSError"
    after = _tree_digest(store.root / "manifests"), sorted(p.name for p in store.objects.rglob("*") if p.is_file())
    assert after == before
    store.verify(backup_id, password=PASSWORD, deep=True)
    assert not list(store.root.glob(".scrub-stage-*"))


def test_v2_shared_object_kept_until_unreferenced(env):
    store, first = _v2(env)
    _, second = _v2(env)                       # cùng nội dung DB → dùng chung đối tượng
    old_digests = {r["sha256"] for r in store._load(first)["files"] if r["kind"] == "sqlite"}
    _forget_alice(env)
    secret, tombs = env["secret"], __import__("privacy").read_tombstones(env["mem"])
    scrub = lambda p: backup_policy.scrub_sqlite_copy(p, tombs, secret)   # noqa: E731
    check = lambda p: backup_policy.scrub_remaining(p, tombs, secret)     # noqa: E731
    assert store.scrub_privacy(first, scrub, check, tombstones=tombs)["changed"]
    assert all(store._object_path(d).is_file() for d in old_digests)       # bản thứ hai còn dùng
    store.verify(second, password=PASSWORD, deep=True)
    assert store.scrub_privacy(second, scrub, check, tombstones=tombs)["changed"]
    assert not any(store._object_path(d).is_file() for d in old_digests)
    assert not _contains(_tree_bytes(store.objects), ALICE_NICK)
    for identity in (first, second):
        store.verify(identity, password=PASSWORD, deep=True)


def test_v2_gc_journal_left_by_crash_is_collected(env):
    store, backup_id = _v2(env)
    stray = store.root / "objects" / "ab" / ("ab" + "0" * 62)
    stray.parent.mkdir(exist_ok=True)
    stray.write_bytes(b"AliceNickXYZ ban sao mo coi")
    journal = store.root / (".scrub-gc-" + "1" * 32 + ".json")
    referenced = store._load(backup_id)["files"][0]["sha256"]
    journal.write_text(json.dumps({"objects": [stray.name, referenced]}), encoding="utf-8")
    with store._write():
        assert store._collect_scrub_garbage() == 1
    assert not stray.exists() and store._object_path(referenced).is_file() and not journal.exists()


def test_recover_interrupted_v1_swap(env):
    backup_dir = _v1(env)
    before = _tree_digest(backup_dir)
    old = env["backups"] / (".scrub-old-" + "a" * 32 + "-" + backup_dir.name)
    os.replace(backup_dir, old)                # mất điện giữa hai lần đổi tên
    backup_policy._recover_scrub_leftovers()
    assert backup_dir.is_dir() and not old.exists()
    assert _tree_digest(backup_dir) == before
    # Bản đang dùng còn tốt → bản dở là rác, xoá.
    stray = env["backups"] / (".scrub-new-" + "b" * 32 + "-" + backup_dir.name)
    stray.mkdir()
    (stray / "x.sqlite3").write_bytes(b"AliceNickXYZ")
    backup_policy._recover_scrub_leftovers()
    assert not stray.exists() and _tree_digest(backup_dir) == before


def test_recover_interrupted_schema_swap(env):
    schema = env["schema"] / "customer_memory-before-delivery-v2.sqlite3"
    data = schema.read_bytes()
    prev = env["schema"] / (".scrub-prev-" + "c" * 32 + "-" + schema.name + ".tmp")
    os.replace(schema, prev)
    backup_policy._recover_scrub_leftovers()
    assert schema.read_bytes() == data and not prev.exists()


def test_new_tombstone_triggers_rescrub(env):
    backup_dir = _v1(env)
    _forget_alice(env)
    backup_policy.scrub_backups_for_privacy()
    env["svc"].forget(BOB)
    result = backup_policy.scrub_backups_for_privacy()
    assert "v1:" + backup_dir.name in result["cleaned"]
    assert not _contains(_tree_bytes(backup_dir), BOB_NICK, ALICE_NICK)
    verify_backup(backup_dir)


def test_vacuum_live_and_secure_delete(env, tmp_path):
    from customer_memory import CustomerMemory
    memory = CustomerMemory(tmp_path / "m.sqlite3", tmp_path / "j")
    try:
        assert memory._db.execute("PRAGMA secure_delete").fetchone()[0] == 1
        assert memory._db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        memory.close()
    assert env["svc"]._db.execute("PRAGMA secure_delete").fetchone()[0] == 1
    _forget_alice(env)
    with closing(sqlite3.connect(env["mem"])) as db, db:          # mô phỏng purge trên DB đang chạy
        db.execute("DELETE FROM interactions WHERE customer_key=?", (ALICE,))
        db.execute("DELETE FROM customers WHERE customer_key=?", (ALICE,))
        db.execute("DELETE FROM customer_facts WHERE customer_key=?", (ALICE,))
    result = backup_policy.scrub_backups_for_privacy()
    assert result["vacuum"]["failed"] == {} and "customer_memory.sqlite3" in result["vacuum"]["done"]
    assert not _contains(env["mem"].read_bytes(), ALICE_NICK)
    wal = Path(str(env["mem"]) + "-wal")
    assert not wal.exists() or not _contains(wal.read_bytes(), ALICE_NICK)
    # Lượt sau không VACUUM lại khi tập tombstone không đổi.
    assert backup_policy.scrub_backups_for_privacy()["vacuum"] is None


def test_restore_v1_holds_backup_lock(env, monkeypatch):
    import threading
    seen = {}

    def other_thread_try():
        got = backup_policy.OPERATIONAL_BACKUP_LOCK.acquire(blocking=False)
        if got:
            backup_policy.OPERATIONAL_BACKUP_LOCK.release()
        seen["other_got_lock"] = got

    def fake(backup_id, parts):
        t = threading.Thread(target=other_thread_try)
        t.start()
        t.join()
        return {"ok": True}

    monkeypatch.setattr(backup_policy, "_restore_operational_backup", fake)
    assert backup_policy.restore_operational_backup("x") == {"ok": True}
    assert seen == {"other_got_lock": False}    # bộ làm sạch không chen vào lúc khôi phục
