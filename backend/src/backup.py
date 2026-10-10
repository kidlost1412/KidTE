"""Sao lưu vận hành của Kid TE.

Mục tiêu là tạo một snapshot có thể kiểm chứng và phục hồi, không phải chỉ copy
file. SQLite dùng API backup của SQLite; JSON/JSONL được kiểm tra cú pháp; mọi
file trong snapshot có SHA-256 trong manifest. Thư mục backup chỉ được publish
sau khi toàn bộ kiểm tra thành công.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
import re
import math
from backup_io import account_io


BACKUP_SCHEMA_VERSION = 1
_COPY_BUFFER_SIZE = 1024 * 1024


class BackupError(RuntimeError):
    """Snapshot không an toàn để publish."""


def _safe_relative(value):
    if (not isinstance(value, str) or not value or '\\' in value or ':' in value
        or '\x00' in value or value.startswith('/') or any(part in {'', '.', '..'} for part in value.split('/'))
        or any(part.endswith((' ', '.')) or re.match(r'(?i)^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)', part)
               for part in value.split('/'))):
        raise BackupError('Tên file backup không an toàn')
    return Path(*PurePosixPath(value).parts)


def _reject_links(path, stop):
    while True:
        if path.is_symlink() or (path.exists() and getattr(path.lstat(), 'st_file_attributes', 0) & 0x400):
            raise BackupError('Backup chứa symlink/junction không an toàn')
        if path == stop:
            break
        if not path.is_relative_to(stop):
            raise BackupError('Backup trỏ ra ngoài thư mục')
        path = path.parent


def _utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_COPY_BUFFER_SIZE), b""):
            digest.update(block)
            account_io(len(block))
    return digest.hexdigest()


def _file_record(path: Path, relative_path: str, kind: str) -> dict:
    return {
        "path": relative_path.replace("\\", "/"),
        "kind": kind,
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _strict_json_loads(text):
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate_json_key')
            result[key] = value
        return result

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError('nonfinite_json_number')
        return number

    def invalid_constant(value):
        raise ValueError('nonfinite_json_number')

    return json.loads(text, object_pairs_hook=object_pairs, parse_float=finite_float,
                      parse_constant=invalid_constant)


def _validate_json(path: Path) -> None:
    try:
        _strict_json_loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise BackupError(f"JSON không hợp lệ: {path.name}: {exc}") from exc


def _validate_jsonl(path: Path) -> None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            for _line_no, line in enumerate(handle, start=1):
                if line.strip():
                    _strict_json_loads(line)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise BackupError(f"JSONL không hợp lệ: {path.name}: {exc}") from exc


def _copy_stable_file(source: Path, destination: Path, retries: int = 3) -> None:
    """Copy file thường chỉ khi kích thước/mtime không đổi suốt lần copy.

    Raw stream có thể đang được append. Nếu thay đổi liên tục, dừng thay vì
    publish một snapshot mà manifest không thể cam kết là nhất quán.
    """
    if not source.is_file() or source.is_symlink():
        raise BackupError(f"Không phải file thường: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(max(1, retries)):
        before = source.stat()
        temporary = destination.with_name(destination.name + ".copying")
        try:
            with source.open("rb") as reader, temporary.open("wb") as writer:
                for block in iter(lambda: reader.read(_COPY_BUFFER_SIZE), b''):
                    writer.write(block)
                    account_io(len(block) * 2)
                writer.flush()
                os.fsync(writer.fileno())
            after = source.stat()
            if (
                before.st_size == after.st_size
                and before.st_mtime_ns == after.st_mtime_ns
            ):
                os.replace(temporary, destination)
                return
        finally:
            if temporary.exists():
                temporary.unlink()
    raise BackupError(f"File đổi liên tục trong lúc sao lưu: {source}")


def _backup_sqlite(source: Path, destination: Path) -> None:
    """Tạo snapshot SQLite nhất quán, kể cả khi WAL đang được dùng."""
    if not source.is_file():
        raise BackupError(f"Không thấy SQLite: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".copying")
    try:
        source_connection = sqlite3.connect(str(source), timeout=10.0)
        destination_connection = sqlite3.connect(str(temporary))
        try:
            source_connection.backup(destination_connection)
            destination_connection.commit()
        finally:
            destination_connection.close()
            source_connection.close()
        _validate_sqlite(temporary)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def _validate_sqlite(path: Path) -> None:
    try:
        connection = sqlite3.connect(str(path))
        try:
            result = connection.execute("PRAGMA integrity_check").fetchone()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise BackupError(f"Không mở được SQLite backup: {exc}") from exc
    if not result or result[0] != "ok":
        raise BackupError(f"SQLite integrity_check thất bại: {result}")


def _validate_text(path: Path) -> None:
    try:
        path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise BackupError(f"Văn bản không hợp lệ: {path.name}: {exc}") from exc


def _copy_raw_stream(source_dir: Path, destination_dir: Path, prefix: str = "raw_stream") -> list[dict]:
    if not source_dir.exists():
        return []
    if not source_dir.is_dir():
        raise BackupError(f"raw_stream không phải thư mục: {source_dir}")

    records = []
    for source in _walk_regular_files(source_dir):
        relative = source.relative_to(source_dir)
        destination = destination_dir / relative
        _copy_stable_file(source, destination)
        _validate_jsonl(destination)
        records.append(
            _file_record(destination, f"{prefix}/{relative.as_posix()}", "jsonl")
        )
    return records


def _walk_regular_files(root):
    """Check directories before descending, including Windows junctions."""
    root = Path(root).absolute()
    _reject_links(root, root)
    pending = [root]
    while pending:
        directory = pending.pop()
        _reject_links(directory, root)
        for entry in sorted(directory.iterdir(), key=lambda path: path.name):
            _reject_links(entry, root)
            if entry.is_dir():
                pending.append(entry)
            elif entry.is_file():
                yield entry
            else:
                raise BackupError('Backup chứa file không phải file thường')


def _snapshot_json(source: Path, destination: Path, relative_path: str) -> dict:
    if not source.exists():
        raise BackupError(f"Không thấy dữ liệu bắt buộc: {source}")
    _copy_stable_file(source, destination)
    _validate_json(destination)
    return _file_record(destination, relative_path, "json")


def _safe_remove_tree(path: Path, required_parent: Path) -> None:
    try:
        path.relative_to(required_parent)
    except ValueError as exc:
        raise BackupError(f"Từ chối xóa ngoài thư mục backup: {path}") from exc
    if path.exists():
        shutil.rmtree(path)


def _new_backup_name(now: datetime, backup_root: Path) -> str:
    base = now.strftime("%Y%m%dT%H%M%SZ")
    candidate = base
    suffix = 1
    while (backup_root / candidate).exists():
        candidate = f"{base}-{suffix:02d}"
        suffix += 1
    return candidate


def create_backup(
    *,
    backup_root: Path,
    memory_db: Path,
    raw_stream_dir: Path,
    approved_catalog: Path,
    approved_profiles: Path,
    shop_info: Path,
    catalog_mapping: Path,
    additional_json_files: dict[str, Path] | None = None,
    additional_sqlite_files: dict[str, Path] | None = None,
    now: datetime | None = None,
) -> Path:
    """Bản sao lưu đầy đủ với bộ file bắt buộc cố định (giữ cho công cụ cũ)."""
    json_files = {
        "approved_product_knowledge.json": Path(approved_catalog),
        "approved_live_profiles.json": Path(approved_profiles),
        "thong_tin_shop.json": Path(shop_info),
        "company_catalog_mapping.json": Path(catalog_mapping),
        **(additional_json_files or {}),
    }
    return create_snapshot(
        backup_root=backup_root,
        sqlite_files={"customer_memory.sqlite3": Path(memory_db), **(additional_sqlite_files or {})},
        json_files=json_files, jsonl_dirs={"raw_stream": Path(raw_stream_dir)}, now=now,
    )


def create_snapshot(
    *,
    backup_root: Path,
    sqlite_files: dict[str, Path] | None = None,
    json_files: dict[str, Path] | None = None,
    text_files: dict[str, Path] | None = None,
    binary_files: dict[str, Path] | None = None,
    jsonl_dirs: dict[str, Path] | None = None,
    now: datetime | None = None,
    extra_manifest: dict | None = None,
) -> Path:
    """Tạo và publish một backup vận hành đã được kiểm tra.

    Chỉ dữ liệu cần khôi phục vận hành được đưa vào snapshot. API key, .env,
    log console và dữ liệu hỗ trợ từ xa không nằm ở đây. Mọi file được liệt kê
    đều bắt buộc phải có (người gọi tự bỏ file không tồn tại).
    """
    backup_root = Path(backup_root)
    backup_root.mkdir(parents=True, exist_ok=True)
    now = now or _utc_now()
    if now.tzinfo is None:
        raise BackupError("Thời điểm backup phải có timezone")

    name = _new_backup_name(now.astimezone(timezone.utc), backup_root)
    final_dir = backup_root / name
    stage_dir = backup_root / f".staging-{name}-{uuid.uuid4().hex}"
    if final_dir.exists():
        raise BackupError(f"Backup đã tồn tại: {final_dir.name}")

    def safe(relative_path):
        return _safe_relative(relative_path)

    try:
        stage_dir.mkdir()
        records = []
        for relative_path, source_path in sorted((sqlite_files or {}).items()):
            relative = safe(relative_path)
            destination = stage_dir / relative
            _backup_sqlite(Path(source_path), destination)
            records.append(_file_record(destination, relative.as_posix(), "sqlite"))
        for relative_path, source_path in sorted((json_files or {}).items()):
            relative = safe(relative_path)
            records.append(_snapshot_json(Path(source_path), stage_dir / relative, relative.as_posix()))
        for relative_path, source_path in sorted((text_files or {}).items()):
            relative = safe(relative_path)
            destination = stage_dir / relative
            _copy_stable_file(Path(source_path), destination)
            _validate_text(destination)
            records.append(_file_record(destination, relative.as_posix(), "text"))
        for relative_path, source_path in sorted((binary_files or {}).items()):
            relative = safe(relative_path)
            destination = stage_dir / relative
            _copy_stable_file(Path(source_path), destination)
            records.append(_file_record(destination, relative.as_posix(), "file"))
        for prefix, source_dir in sorted((jsonl_dirs or {}).items()):
            records.extend(_copy_raw_stream(Path(source_dir), stage_dir / safe(prefix), prefix))
        if not records:
            raise BackupError("Không có dữ liệu nào để sao lưu")

        manifest = {
            "schema_version": BACKUP_SCHEMA_VERSION,
            "created_at": now.astimezone(timezone.utc).isoformat(),
            **(extra_manifest or {}),
            "files": records,
        }
        manifest_path = stage_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        _validate_json(manifest_path)
        os.replace(stage_dir, final_dir)
        return final_dir
    except Exception:
        _safe_remove_tree(stage_dir, backup_root)
        raise


def verify_backup(backup_dir: Path) -> dict:
    """Kiểm tra lại manifest, checksum và khả năng đọc dữ liệu backup."""
    backup_dir = Path(backup_dir)
    manifest_path = backup_dir / "manifest.json"
    _reject_links(manifest_path, backup_dir)
    if not manifest_path.is_file():
        raise BackupError(f"Không thấy manifest: {manifest_path}")
    try:
        manifest = _strict_json_loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise BackupError(f"Manifest không hợp lệ: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != BACKUP_SCHEMA_VERSION:
        raise BackupError("Schema manifest backup không hỗ trợ")

    records = manifest.get("files")
    if not isinstance(records, list) or not records:
        raise BackupError("Manifest không có file backup")

    checked = 0
    seen = set()
    for record in records:
        if not isinstance(record, dict):
            raise BackupError('Manifest có record không hợp lệ')
        relative = record.get("path")
        safe = _safe_relative(relative)
        if relative.casefold() in seen:
            raise BackupError('Manifest có đường dẫn trùng')
        seen.add(relative.casefold())
        file_path = backup_dir / safe
        _reject_links(file_path, backup_dir)
        try:
            file_path.resolve().relative_to(backup_dir.resolve())
        except ValueError as exc:
            raise BackupError(f"Manifest trỏ ra ngoài backup: {relative}") from exc
        if not file_path.is_file():
            raise BackupError(f"Thiếu file backup: {relative}")
        if file_path.stat().st_size != record.get("bytes"):
            raise BackupError(f"Sai kích thước backup: {relative}")
        if _sha256(file_path) != record.get("sha256"):
            raise BackupError(f"Sai checksum backup: {relative}")
        kind = record.get("kind")
        if kind == "sqlite":
            _validate_sqlite(file_path)
        elif kind == "json":
            _validate_json(file_path)
        elif kind == "jsonl":
            _validate_jsonl(file_path)
        elif kind == "text":
            _validate_text(file_path)
        elif kind == "file":
            pass                    # file gốc (pdf, docx…): kiểm bằng checksum ở trên là đủ
        else:
            raise BackupError(f"Loại file backup không hỗ trợ: {kind}")
        checked += 1

    return {
        "ok": True,
        "created_at": manifest.get("created_at"),
        "files_checked": checked,
        "backup_dir": str(backup_dir),
    }


def prune_backups(backup_root: Path, keep: int = 20) -> dict:
    """Chỉ xóa backup cũ đã verify, nằm trực tiếp dưới backup_root."""
    backup_root = Path(backup_root).resolve()
    keep = max(1, int(keep))
    groups = {}
    skipped = []
    if not backup_root.exists():
        return {"removed": [], "skipped": []}
    for candidate in backup_root.iterdir():
        if not candidate.is_dir() or candidate.name.startswith("."):
            continue
        try:
            verified = verify_backup(candidate)
            scope = json.loads((candidate / "manifest.json").read_text(encoding="utf-8")).get("scope", "full")
            groups.setdefault(scope, []).append((verified.get("created_at") or "", candidate.resolve()))
        except (BackupError, OSError, json.JSONDecodeError):
            skipped.append(candidate.name)
    removed = []
    # Bản sao lưu từng mục giữ riêng số lượng: sao lưu lẻ nhiều lần không được
    # đẩy mất các bản đầy đủ.
    for valid in groups.values():
        valid.sort(key=lambda item: (item[0], item[1].name), reverse=True)
        for _, candidate in valid[keep:]:
            _safe_remove_tree(candidate, backup_root)
            removed.append(candidate.name)
    return {"removed": removed, "skipped": skipped}


def restore_backup(backup_dir: Path, targets: dict) -> dict:
    """Ghi dữ liệu của một bản backup ĐÃ VERIFY trở lại chỗ đang dùng.

    ``targets``: {đường dẫn trong manifest: file đích}. File không có trong
    ``targets`` (raw stream, danh mục LIVE tạm thời) bị bỏ qua. SQLite ghi bằng
    backup API (an toàn khi ứng dụng đang mở kết nối); JSON ghi nguyên tử.
    """
    backup_dir = Path(backup_dir)
    verify_backup(backup_dir)
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    restored, skipped = [], []
    for record in manifest.get("files") or []:
        relative = record.get("path")
        destination = targets.get(relative)
        if destination is None:
            skipped.append(relative)
            continue
        source = backup_dir / relative
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if record.get("kind") == "sqlite":
            source_connection = sqlite3.connect(str(source), timeout=10.0)
            destination_connection = sqlite3.connect(str(destination), timeout=30.0)
            try:
                source_connection.backup(destination_connection)
                destination_connection.commit()
            finally:
                destination_connection.close()
                source_connection.close()
        else:
            temporary = destination.with_name(destination.name + ".restoring")
            shutil.copyfile(source, temporary)
            os.replace(temporary, destination)
        restored.append(relative)
    return {"ok": True, "restored": restored, "skipped": skipped}
