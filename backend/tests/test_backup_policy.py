import sys
from pathlib import Path


SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import backup_policy


def test_shopee_mapping_is_in_product_backup_and_restore(tmp_path, monkeypatch):
    mapping = tmp_path / "machine" / "shopee_live_mappings.json"
    mapping.parent.mkdir()
    mapping.write_text('{"schema_version":1,"videos":{}}', encoding="utf-8")
    monkeypatch.setattr(backup_policy, "SHOPEE_LIVE_MAPPING_PATH", mapping)
    source = backup_policy._sources()["san_pham"]["json"]
    assert source["machine/shopee_live_mappings.json"] == mapping
    assert backup_policy._restore_target("machine/shopee_live_mappings.json") == mapping


def test_startup_backup_is_quiet_on_first_install(tmp_path, monkeypatch):
    monkeypatch.setattr(backup_policy, "MEMORY_DB", tmp_path / "missing.sqlite3")
    monkeypatch.setattr(backup_policy, "BACKUP_DIR", tmp_path / "backups")
    assert backup_policy.ensure_startup_backup() == {
        "created": False, "reason": "first_run_no_user_database"
    }


def test_operational_backup_skips_missing_legacy_files(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    for name in (
        "approved.json", "profiles.json", "shop.json", "mapping.json",
    ):
        (data / name).write_text("{}", encoding="utf-8")
    import sqlite3
    db = data / "memory.sqlite3"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE sample (id INTEGER)")

    monkeypatch.setattr(backup_policy, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(backup_policy, "MEMORY_DB", db)
    monkeypatch.setattr(backup_policy, "RAW_STREAM_DIR", tmp_path / "raw")
    monkeypatch.setattr(backup_policy, "APPROVED_CATALOG_PATH", data / "approved.json")
    monkeypatch.setattr(backup_policy, "APPROVED_PROFILES_PATH", data / "profiles.json")
    monkeypatch.setattr(backup_policy, "SHOP_INFO_PATH", data / "shop.json")
    monkeypatch.setattr(backup_policy, "CATALOG_MAPPING_PATH", data / "mapping.json")
    monkeypatch.setattr(backup_policy, "MACHINE_CATALOG_MAPPING_PATH", data / "missing-machine.json")
    monkeypatch.setattr(backup_policy, "LIVE_CATALOG_PATH", data / "missing-live.json")
    monkeypatch.setattr(backup_policy, "LIVE_RAW_CATALOG_PATH", data / "missing-live-raw.json")
    monkeypatch.setattr(backup_policy, "OPERATOR_WORKSPACE_DB", data / "missing-workspace.sqlite3")

    result = backup_policy.create_operational_backup()

    assert result["created"] is True
    assert backup_policy.latest_backup_time(tmp_path / "backups") is not None


def test_manual_backup_never_deletes_automatic_backups(tmp_path, monkeypatch):
    import sqlite3
    from datetime import datetime, timedelta, timezone
    db = tmp_path / "memory.sqlite3"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE sample (id INTEGER)")
    monkeypatch.setattr(backup_policy, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(backup_policy, "MEMORY_DB", db)
    base = datetime(2026, 10, 1, tzinfo=timezone.utc)
    step = iter(range(1000))

    def make(reason):
        return backup_policy.create_operational_backup(
            now=base + timedelta(minutes=next(step)), parts=["khach_hang"], reason=reason)

    autos = [make("auto")["backup_id"] for _ in range(3)]
    restore = make("before_restore")["backup_id"]
    manuals = [make("manual")["backup_id"] for _ in range(8)]
    left = {p.name for p in (tmp_path / "backups").iterdir() if p.is_dir()}
    assert set(autos) <= left and restore in left
    assert set(manuals[-5:]) <= left and not set(manuals[:3]) & left
    assert len([m for m in manuals if m in left]) == 5
    make("auto")
    left = {p.name for p in (tmp_path / "backups").iterdir() if p.is_dir()}
    assert set(manuals[-5:]) <= left   # auto backup does not touch manual ones


def test_product_setup_files_and_ingest_images_are_backed_up(tmp_path, monkeypatch):
    (tmp_path / "machine").mkdir()
    (tmp_path / "machine" / "removed_products.json").write_text("{}", encoding="utf-8")
    (tmp_path / "product_setup_choice.json").write_text("{}", encoding="utf-8")
    (tmp_path / "ingest_images").mkdir()
    (tmp_path / "ingest_images" / "A.webp").write_bytes(b"x")
    monkeypatch.setattr(backup_policy, "DATA_DIR", tmp_path)
    src = backup_policy._sources()["san_pham"]
    assert "machine/removed_products.json" in src["json"]
    assert "product_setup_choice.json" in src["json"]
    assert src["file"]["ingest_images/A.webp"] == tmp_path / "ingest_images" / "A.webp"
    assert backup_policy._restore_target("ingest_images/A.webp") == tmp_path / "ingest_images" / "A.webp"
    assert backup_policy._restore_target("product_setup_choice.json") == tmp_path / "product_setup_choice.json"
    assert backup_policy.part_of("ingest_images/A.webp") == "san_pham"
