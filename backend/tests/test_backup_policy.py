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
