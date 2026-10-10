import json
import sqlite3
import sys
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from backup import BackupError, create_backup, prune_backups, verify_backup
from backup import _safe_relative, _copy_raw_stream


def test_raw_stream_junction_is_rejected_before_descending(tmp_path):
    root = tmp_path / 'raw'
    root.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    payload = outside / 'private.jsonl'
    payload.write_text('{"fixture":"outside"}\n', encoding='utf-8')
    link = root / 'linked'
    if os.name == 'nt':
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)], capture_output=True)
        assert result.returncode == 0
    else:
        link.symlink_to(outside, target_is_directory=True)
    target = tmp_path / 'snapshot'
    try:
        with pytest.raises(BackupError, match='junction'):
            _copy_raw_stream(root, target)
        assert not (target / 'linked' / 'private.jsonl').exists()
        assert payload.read_text(encoding='utf-8') == '{"fixture":"outside"}\n'
    finally:
        link.rmdir() if os.name == 'nt' else link.unlink()


@pytest.mark.parametrize('path', ['../out', '/absolute', 'C:/outside', 'C:relative',
    'dir\\file', 'dir/../file', 'dir//file', './file', 'dir/file.', 'dir/file ', 'CON.txt', 'dir/NUL', 'dir/COM1.txt'])
def test_backup_paths_are_portable_and_cannot_use_windows_aliases(path):
    with pytest.raises(BackupError):
        _safe_relative(path)


def test_duplicate_manifest_paths_are_rejected(tmp_path):
    backup_dir, _ = _create_snapshot(tmp_path)
    manifest_path = backup_dir / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest['files'].append(dict(manifest['files'][0]))
    _write_json(manifest_path, manifest)
    with pytest.raises(BackupError, match='trùng'):
        verify_backup(backup_dir)


def test_verify_rejects_junction_before_reading_external_payload(tmp_path):
    backup_dir, _ = _create_snapshot(tmp_path)
    outside = tmp_path / 'outside'
    outside.mkdir()
    payload = outside / 'payload'
    payload.write_bytes(b'fixture')
    link = backup_dir / 'linked'
    if os.name == 'nt':
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)], capture_output=True)
        assert result.returncode == 0
    else:
        link.symlink_to(outside, target_is_directory=True)
    try:
        manifest_path = backup_dir / 'manifest.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        manifest['files'] = [{'path': 'linked/payload', 'kind': 'file', 'bytes': 7, 'sha256': 'not-used'}]
        _write_json(manifest_path, manifest)
        with pytest.raises(BackupError, match='junction'):
            verify_backup(backup_dir)
        assert payload.read_bytes() == b'fixture'
    finally:
        if os.name == 'nt':
            os.rmdir(link)
        else:
            link.unlink()


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _seed_sqlite(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, text TEXT)")
        connection.execute("INSERT INTO messages (text) VALUES (?)", ("khách hỏi giá",))
        connection.commit()
    finally:
        connection.close()


def _create_snapshot(tmp_path: Path):
    data = tmp_path / "data"
    raw_stream = tmp_path / "logs" / "raw_stream"
    backup_root = tmp_path / "backups"
    data.mkdir()
    raw_stream.mkdir(parents=True)
    memory_db = data / "customer_memory.sqlite3"
    _seed_sqlite(memory_db)
    catalog = data / "approved_product_knowledge.json"
    profiles = data / "approved_live_profiles.json"
    shop = data / "thong_tin_shop.json"
    mapping = data / "company_catalog_mapping.json"
    _write_json(catalog, {"nhom_san_pham": []})
    _write_json(profiles, {"products": {}})
    _write_json(shop, {"dia_chi_mua_hang": "Cần Thơ"})
    _write_json(mapping, {"mapping": {}})
    (raw_stream / "stream.jsonl").write_text(
        json.dumps({"comment_id": "c1", "comment": "giá bao nhiêu"}) + "\n",
        encoding="utf-8",
    )
    backup_dir = create_backup(
        backup_root=backup_root,
        memory_db=memory_db,
        raw_stream_dir=raw_stream,
        approved_catalog=catalog,
        approved_profiles=profiles,
        shop_info=shop,
        catalog_mapping=mapping,
        now=datetime(2026, 9, 12, tzinfo=timezone.utc),
    )
    return backup_dir, backup_root


def test_backup_is_verified_and_sqlite_is_restoreable(tmp_path):
    backup_dir, _ = _create_snapshot(tmp_path)

    result = verify_backup(backup_dir)

    assert result["ok"] is True
    assert result["files_checked"] == 6
    assert (backup_dir / "manifest.json").is_file()
    assert (backup_dir / "raw_stream" / "stream.jsonl").is_file()
    connection = sqlite3.connect(backup_dir / "customer_memory.sqlite3")
    try:
        assert connection.execute("SELECT text FROM messages").fetchone()[0] == "khách hỏi giá"
    finally:
        connection.close()


def test_backup_is_not_published_when_json_is_invalid(tmp_path):
    backup_dir, backup_root = _create_snapshot(tmp_path)
    source = tmp_path / "data" / "thong_tin_shop.json"
    source.write_text("{không phải json", encoding="utf-8")

    with pytest.raises(BackupError, match="JSON không hợp lệ"):
        create_backup(
            backup_root=backup_root,
            memory_db=tmp_path / "data" / "customer_memory.sqlite3",
            raw_stream_dir=tmp_path / "logs" / "raw_stream",
            approved_catalog=tmp_path / "data" / "approved_product_knowledge.json",
            approved_profiles=tmp_path / "data" / "approved_live_profiles.json",
            shop_info=source,
            catalog_mapping=tmp_path / "data" / "company_catalog_mapping.json",
            now=datetime(2026, 9, 12, 0, 0, 1, tzinfo=timezone.utc),
        )

    assert backup_dir.is_dir()
    assert not list(backup_root.glob(".staging-*"))
    assert not (backup_root / "20260912T000001Z").exists()


def test_verification_detects_tampered_file(tmp_path):
    backup_dir, _ = _create_snapshot(tmp_path)
    (backup_dir / "company_catalog_mapping.json").write_text("{}", encoding="utf-8")

    with pytest.raises(BackupError, match="Sai"):
        verify_backup(backup_dir)


def test_backup_keeps_layered_data_and_rejects_unsafe_extra_name(tmp_path):
    backup_dir, backup_root = _create_snapshot(tmp_path)
    shared = tmp_path / "data" / "shared.json"
    machine = tmp_path / "data" / "machine.json"
    _write_json(shared, {"content": {"nhom_san_pham": []}})
    _write_json(machine, {"assignments": {}})

    layered = create_backup(
        backup_root=backup_root,
        memory_db=tmp_path / "data" / "customer_memory.sqlite3",
        raw_stream_dir=tmp_path / "logs" / "raw_stream",
        approved_catalog=tmp_path / "data" / "approved_product_knowledge.json",
        approved_profiles=tmp_path / "data" / "approved_live_profiles.json",
        shop_info=tmp_path / "data" / "thong_tin_shop.json",
        catalog_mapping=tmp_path / "data" / "company_catalog_mapping.json",
        additional_json_files={"shared/product_knowledge.json": shared,
                               "machine/catalog_mapping.json": machine},
        now=datetime(2026, 9, 12, 0, 0, 1, tzinfo=timezone.utc),
    )
    assert verify_backup(layered)["files_checked"] == 8
    assert (layered / "shared" / "product_knowledge.json").is_file()
    with pytest.raises(BackupError, match="không an toàn"):
        create_backup(
            backup_root=backup_root,
            memory_db=tmp_path / "data" / "customer_memory.sqlite3",
            raw_stream_dir=tmp_path / "logs" / "raw_stream",
            approved_catalog=tmp_path / "data" / "approved_product_knowledge.json",
            approved_profiles=tmp_path / "data" / "approved_live_profiles.json",
            shop_info=tmp_path / "data" / "thong_tin_shop.json",
            catalog_mapping=tmp_path / "data" / "company_catalog_mapping.json",
            additional_json_files={"../outside.json": shared},
            now=datetime(2026, 9, 12, 0, 0, 2, tzinfo=timezone.utc),
        )


def test_backup_keeps_additional_sqlite_database(tmp_path):
    _, backup_root = _create_snapshot(tmp_path)
    operator_db = tmp_path / "data" / "operator.sqlite3"
    _seed_sqlite(operator_db)
    backup_dir = create_backup(
        backup_root=backup_root,
        memory_db=tmp_path / "data" / "customer_memory.sqlite3",
        raw_stream_dir=tmp_path / "logs" / "raw_stream",
        approved_catalog=tmp_path / "data" / "approved_product_knowledge.json",
        approved_profiles=tmp_path / "data" / "approved_live_profiles.json",
        shop_info=tmp_path / "data" / "thong_tin_shop.json",
        catalog_mapping=tmp_path / "data" / "company_catalog_mapping.json",
        additional_sqlite_files={"operator_workspace.sqlite3": operator_db},
        now=datetime(2026, 9, 12, 0, 0, 3, tzinfo=timezone.utc),
    )
    assert verify_backup(backup_dir)["files_checked"] == 7
    with sqlite3.connect(backup_dir / "operator_workspace.sqlite3") as connection:
        assert connection.execute("SELECT text FROM messages").fetchone()[0] == "khách hỏi giá"


def test_retention_removes_only_old_verified_backups(tmp_path):
    first, backup_root = _create_snapshot(tmp_path)
    second = create_backup(
        backup_root=backup_root,
        memory_db=tmp_path / "data" / "customer_memory.sqlite3",
        raw_stream_dir=tmp_path / "logs" / "raw_stream",
        approved_catalog=tmp_path / "data" / "approved_product_knowledge.json",
        approved_profiles=tmp_path / "data" / "approved_live_profiles.json",
        shop_info=tmp_path / "data" / "thong_tin_shop.json",
        catalog_mapping=tmp_path / "data" / "company_catalog_mapping.json",
        now=datetime(2026, 9, 12, 0, 0, 1, tzinfo=timezone.utc),
    )
    broken = backup_root / "not-a-valid-backup"
    broken.mkdir(); (broken / "keep.txt").write_text("do not delete")
    result = prune_backups(backup_root, keep=1)
    assert result["removed"] == [first.name]
    assert second.exists() and broken.exists()
    assert result["skipped"] == [broken.name]


def test_restore_writes_sqlite_and_json_back_and_skips_unmapped(tmp_path):
    from backup import restore_backup
    backup_dir, _ = _create_snapshot(tmp_path)
    live_db = tmp_path / "data" / "customer_memory.sqlite3"
    shop = tmp_path / "data" / "thong_tin_shop.json"
    # Sau khi sao lưu, dữ liệu bị sửa hỏng…
    connection = sqlite3.connect(str(live_db))
    connection.execute("CREATE TABLE IF NOT EXISTS junk(x)")
    connection.commit()
    connection.close()
    shop.write_text('{"hong": true}', encoding="utf-8")
    out = restore_backup(backup_dir, {"customer_memory.sqlite3": live_db, "thong_tin_shop.json": shop})
    assert sorted(out["restored"]) == ["customer_memory.sqlite3", "thong_tin_shop.json"]
    assert "junk" not in {r[0] for r in sqlite3.connect(str(live_db)).execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "hong" not in shop.read_text(encoding="utf-8")
    assert out["skipped"], "file không có đích (raw stream…) phải được bỏ qua"
