"""Chính sách sao lưu: sao lưu/khôi phục cả bộ hoặc TỪNG MỤC, và xem bên trong."""
from __future__ import annotations

import json
import prompt_view
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from backup import BackupError, create_snapshot, prune_backups, retention_class, restore_backup, verify_backup
from config import (
    APPROVED_CATALOG_PATH, APPROVED_PROFILES_PATH, BACKUP_DIR,
    CATALOG_MAPPING_PATH, DATA_DIR,
    LIVE_CATALOG_PATH, LIVE_RAW_CATALOG_PATH, MACHINE_CATALOG_MAPPING_PATH,
    SHOPEE_LIVE_MAPPING_PATH,
    MEMORY_DB, OPERATOR_WORKSPACE_DB, RAW_STREAM_DIR, SHOP_INFO_PATH,
    AI_LEARNING_DB, VIDEO_LIBRARY_DIR, PROCESSOR_SETTINGS_PATH, SYSTEM_PROMPT_OVERRIDE_PATH, AI_NOTES_PATH,
    LOCAL_PRODUCT_OVERRIDES_PATH, DOCUMENTS_DIR, SALES_PLAYBOOK_PATH,
)

# (mã, tên hiện cho người dùng, mô tả ngắn). Thứ tự = thứ tự trên màn hình.
PARTS = (
    ("khach_hang", "Khách hàng", "Hồ sơ khách, bình luận đã xử lý, điều AI nhớ về từng khách"),
    ("hop_thu", "Hộp thư & chỉnh sửa", "Câu trả lời đã sửa, câu mẫu đã duyệt, thùng rác"),
    ("tri_nho_ai", "Trí nhớ AI", "Mọi điều chủ shop đã dạy, phong cách người trực, lịch sử học"),
    ("lich_ghim", "Lịch ghim video", "Mốc ghim sản phẩm của từng video phát lại"),
    ("san_pham", "Hồ sơ sản phẩm", "Hồ sơ kỹ thuật, giá gốc duyệt, ánh xạ giỏ hàng ↔ hồ sơ"),
    ("tai_lieu", "Tài liệu cho AI", "Tài liệu về công ty, sản phẩm, kiến thức nông nghiệp đã tải lên"),
    ("cai_dat", "Thông tin shop & cài đặt AI", "Địa chỉ/hotline, cài đặt bộ xử lý, lời nhắc hệ thống tự sửa"),
    ("luong_binh_luan", "Luồng bình luận thô", "Mọi bình luận từng phiên LIVE — nguồn của Hộp thư"),
)
PART_IDS = tuple(part[0] for part in PARTS)


def _sources():
    """Mỗi mục → {sqlite|json|text|jsonl: {tên trong bản sao lưu: file thật}}.

    Đọc các hằng số lúc GỌI (không lúc import) để test đổi được đường dẫn.
    File chưa tồn tại trên máy này được bỏ qua.
    """
    def existing(mapping):
        return {name: Path(path) for name, path in mapping.items() if Path(path).is_file()}

    ingest_dir = Path(DATA_DIR) / "ingest_images"
    ingest_images = ({f"ingest_images/{p.name}": p for p in sorted(ingest_dir.iterdir())
                      if p.is_file() and p.suffix not in {".tmp", ".incoming"}}
                     if ingest_dir.is_dir() else {})
    videos = {f"videos/{t.parent.name}/timeline.json": t
              for t in sorted(Path(VIDEO_LIBRARY_DIR).glob("*/timeline.json"))}
    return {
        "khach_hang": {"sqlite": existing({"customer_memory.sqlite3": MEMORY_DB,
                                         "shopee_customers.sqlite3": MEMORY_DB.with_name("shopee_customers.sqlite3")})},
        "hop_thu": {"sqlite": existing({"operator_workspace.sqlite3": OPERATOR_WORKSPACE_DB})},
        "tri_nho_ai": {"sqlite": existing({"ai_learning.sqlite3": AI_LEARNING_DB})},
        "lich_ghim": {"json": videos},
        "san_pham": {"json": existing({
            "approved_product_knowledge.json": APPROVED_CATALOG_PATH,
            "approved_live_profiles.json": APPROVED_PROFILES_PATH,
            "company_catalog_mapping.json": CATALOG_MAPPING_PATH,
            "machine/catalog_mapping.json": MACHINE_CATALOG_MAPPING_PATH,
            "machine/shopee_live_mappings.json": SHOPEE_LIVE_MAPPING_PATH,
            "live_products_current.json": LIVE_CATALOG_PATH,
            "live_products_raw_current.json": LIVE_RAW_CATALOG_PATH,
            "local_product_overrides.json": LOCAL_PRODUCT_OVERRIDES_PATH,
            "machine/removed_products.json": Path(DATA_DIR) / "machine" / "removed_products.json",
            "product_setup_choice.json": Path(DATA_DIR) / "product_setup_choice.json",
        }), "file": ingest_images},
        "cai_dat": {"json": existing({"thong_tin_shop.json": SHOP_INFO_PATH,
                                      "cai_dat_bo_xu_ly.json": PROCESSOR_SETTINGS_PATH,
                                      "sales_playbook.json": SALES_PLAYBOOK_PATH,
                                      "quy_tac_ban_hang.meta.json": prompt_view.META_PATH}),
                    "text": existing({"loi_nhac_he_thong.txt": SYSTEM_PROMPT_OVERRIDE_PATH,
                                          "ghi_chu_ai.txt": AI_NOTES_PATH,
                                          "quy_tac_ban_hang.txt": prompt_view.OVERRIDE_PATH})},
        "tai_lieu": {"file": {f"tai_lieu/{p.relative_to(DOCUMENTS_DIR).as_posix()}": p
                              for p in sorted(Path(DOCUMENTS_DIR).rglob("*"))
                              if p.is_file() and not p.name.endswith(".tmp")}},
        "luong_binh_luan": {"jsonl": {"raw_stream": Path(RAW_STREAM_DIR)}
                            if Path(RAW_STREAM_DIR).is_dir() else {}},
    }


def part_of(path):
    """File trong bản sao lưu thuộc mục nào (kể cả bản cũ chưa ghi 'parts')."""
    path = str(path)
    if path.startswith("raw_stream/"):
        return "luong_binh_luan"
    if path.startswith("videos/"):
        return "lich_ghim"
    if path.startswith("tai_lieu/"):
        return "tai_lieu"
    return {
        "customer_memory.sqlite3": "khach_hang", "shopee_customers.sqlite3": "khach_hang",
        "operator_workspace.sqlite3": "hop_thu",
        "ai_learning.sqlite3": "tri_nho_ai", "thong_tin_shop.json": "cai_dat",
        "cai_dat_bo_xu_ly.json": "cai_dat", "loi_nhac_he_thong.txt": "cai_dat", "ghi_chu_ai.txt": "cai_dat",
        "quy_tac_ban_hang.txt": "cai_dat", "quy_tac_ban_hang.meta.json": "cai_dat",
        "sales_playbook.json": "cai_dat",
    }.get(path, "san_pham")


def _restore_target(path):
    """File trong bản sao lưu → file thật sẽ bị ghi đè khi khôi phục (None = bỏ qua)."""
    parts = str(path).split("/")
    if len(parts) == 3 and parts[0] == "videos" and parts[2] == "timeline.json" and parts[1] not in {"", ".", ".."}:
        return Path(VIDEO_LIBRARY_DIR) / parts[1] / "timeline.json"
    if len(parts) == 2 and parts[0] == "raw_stream" and parts[1].startswith("stream_") and parts[1].endswith(".jsonl"):
        return Path(RAW_STREAM_DIR) / parts[1]
    if (len(parts) == 3 and parts[0] == "tai_lieu" and parts[1].isalnum()
            and parts[2] and parts[2] not in {".", ".."} and "\\" not in parts[2]):
        return Path(DOCUMENTS_DIR) / parts[1] / parts[2]
    if len(parts) == 2 and parts[0] == "ingest_images" and parts[1] not in {"", ".", ".."} and "\\" not in parts[1]:
        return Path(DATA_DIR) / "ingest_images" / parts[1]
    return {
        "machine/removed_products.json": Path(DATA_DIR) / "machine" / "removed_products.json",
        "product_setup_choice.json": Path(DATA_DIR) / "product_setup_choice.json",
        "customer_memory.sqlite3": MEMORY_DB,
        "shopee_customers.sqlite3": MEMORY_DB.with_name("shopee_customers.sqlite3"),
        "operator_workspace.sqlite3": OPERATOR_WORKSPACE_DB,
        "ai_learning.sqlite3": AI_LEARNING_DB,
        "approved_product_knowledge.json": APPROVED_CATALOG_PATH,
        "approved_live_profiles.json": APPROVED_PROFILES_PATH,
        "thong_tin_shop.json": SHOP_INFO_PATH,
        "company_catalog_mapping.json": CATALOG_MAPPING_PATH,
        "machine/catalog_mapping.json": MACHINE_CATALOG_MAPPING_PATH,
        "machine/shopee_live_mappings.json": SHOPEE_LIVE_MAPPING_PATH,
        "local_product_overrides.json": LOCAL_PRODUCT_OVERRIDES_PATH,
        "cai_dat_bo_xu_ly.json": PROCESSOR_SETTINGS_PATH,
        "sales_playbook.json": SALES_PLAYBOOK_PATH,
        "loi_nhac_he_thong.txt": SYSTEM_PROMPT_OVERRIDE_PATH,
        "ghi_chu_ai.txt": AI_NOTES_PATH,
        "quy_tac_ban_hang.txt": prompt_view.OVERRIDE_PATH,
        "quy_tac_ban_hang.meta.json": prompt_view.META_PATH,
    }.get(str(path))


def _manifest(backup_dir):
    return json.loads((Path(backup_dir) / "manifest.json").read_text(encoding="utf-8"))


def latest_backup_time(backup_root: Path, *, verify=False):
    """Latest published full snapshot; polling reads manifests only.

    This is a timestamp lookup, not a health certificate. Explicit integrity
    checks and restore still use full checksum verification.
    """
    latest = None
    for manifest in Path(backup_root).glob("*/manifest.json"):
        if manifest.parent.name.startswith('.'):
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            if (not isinstance(data, dict) or data.get('schema_version') != 1 or
                    not isinstance(data.get('files'), list) or not data['files']):
                continue
            if data.get("scope", "full") != "full":
                continue
            stamp = datetime.fromisoformat(data["created_at"])
            if verify:
                verify_backup(manifest.parent)
        except (OSError, KeyError, ValueError, BackupError, json.JSONDecodeError):
            continue
        latest = stamp if latest is None or stamp > latest else latest
    return latest


RETENTION_KEEP = {"auto": 20, "manual": 5, "before_restore": 40}

# Re-entrant: the automatic scheduler holds it non-blocking, then calls create.
OPERATIONAL_BACKUP_LOCK = threading.RLock()


def create_operational_backup(*, now=None, keep=None, parts=None, reason="manual"):
    with OPERATIONAL_BACKUP_LOCK:
        return _create_operational_backup(now=now, keep=keep, parts=parts, reason=reason)


def _create_operational_backup(*, now=None, keep=None, parts=None, reason="manual"):
    """Sao lưu cả bộ (``parts=None``) hoặc chỉ những mục được chọn."""
    now = now or datetime.now(timezone.utc)
    wanted = list(PART_IDS) if parts is None else [p for p in PART_IDS if p in set(parts)]
    if not wanted:
        raise ValueError("backup_parts_required")
    sources = _sources()
    merged = {"sqlite": {}, "json": {}, "text": {}, "file": {}, "jsonl": {}}
    included = []
    for part in wanted:
        found = sources[part]
        if any(found.get(kind) for kind in merged):
            included.append(part)
        for kind in merged:
            merged[kind].update(found.get(kind) or {})
    if not included:
        raise ValueError("backup_nothing_to_save")
    backup_dir = create_snapshot(
        backup_root=BACKUP_DIR, sqlite_files=merged["sqlite"], json_files=merged["json"],
        text_files=merged["text"], binary_files=merged["file"], jsonl_dirs=merged["jsonl"], now=now,
        extra_manifest={"scope": "full" if parts is None else "partial",
                        "parts": included, "reason": reason},
    )
    verify_backup(backup_dir)
    if keep is None:
        keep = RETENTION_KEEP[retention_class(reason)]
    # Chỉ dọn trong đúng nhóm của lần này: sao lưu tay không xóa bản tự động.
    retention = prune_backups(BACKUP_DIR, keep=keep, reason=reason)
    return {"created": True, "backup_dir": str(backup_dir), "backup_id": Path(backup_dir).name,
            "parts": included, "retention": retention}


def ensure_startup_backup(*, now=None, max_age_hours=12, keep=20, reason="startup"):
    now = now or datetime.now(timezone.utc)
    # Cài mới chưa có database người dùng để sao lưu. CustomerMemory sẽ tạo
    # database sau khi core khởi tạo; lần mở kế tiếp mới bắt đầu chu kỳ backup.
    if not MEMORY_DB.exists():
        return {"created": False, "reason": "first_run_no_user_database"}
    latest = latest_backup_time(BACKUP_DIR)
    if latest is not None and (now - latest.astimezone(timezone.utc)).total_seconds() < max_age_hours * 3600:
        return {"created": False, "reason": "recent_backup", "latest": latest.isoformat()}
    return create_operational_backup(now=now, keep=keep, reason=reason)


_V2_STARTUP_LOCK = threading.Lock()


def has_recent_v2_auto_backup(service, *, now=None, max_age_hours=24):
    """True/False from BackupV2 manifests under service.root; None when it cannot be determined."""
    root = getattr(service, "root", None)
    if root is None:
        return None
    now = now or datetime.now(timezone.utc)
    manifests = Path(root) / "backups" / "v2" / "manifests"
    if not manifests.is_dir():
        return False
    for path in manifests.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            stamp = datetime.fromisoformat(str(data["created_at"]))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
        except (OSError, KeyError, ValueError, TypeError):
            continue
        age = (now - stamp.astimezone(timezone.utc)).total_seconds()
        if 0 <= age < max_age_hours * 3600 and data.get("reason", "auto") != "manual":
            return True
    return False


def run_startup_auto_backup(v1_result, service_provider, *, logger=None, background=False, now=None, startup_delay_s=0, cancel_event=None):
    """F04a: make the v2 (partial, DPAPI password) backup at startup.

    Runs when v1 just created a backup OR when no v2 backup exists from the last 24h
    (independent of the v1 throttle). Never raises, never runs two v2 creates at once; goes
    through the engine's ``_guarded_v2_create`` when the provider is its bound method.
    Returns the result dict (or the started thread when ``background``).
    """
    v1_created = isinstance(v1_result, dict) and bool(v1_result.get("created"))

    def log(level, message, event):
        if logger is not None:
            try:
                getattr(logger, level)(message, extra={"event": event})
            except Exception:
                pass

    def work():
        if background and startup_delay_s:
            if (cancel_event or threading.Event()).wait(startup_delay_s):
                return {'created':False, 'reason':'startup_backup_cancelled'}
        if not _V2_STARTUP_LOCK.acquire(blocking=False):
            return {"created": False, "reason": "v2_busy"}
        try:
            service = service_provider()
            if not v1_created and has_recent_v2_auto_backup(service, now=now) is not False:
                return {"created": False, "reason": "v1_not_due"}
            engine = getattr(service_provider, "__self__", None)
            guarded = getattr(engine, "_guarded_v2_create", None)
            if callable(guarded) and hasattr(service, "auto_password"):
                job = guarded(service.auto_password(), False)
                log("info", str((job or {}).get("job_id", "")), "startup_backup_v2")
                return {"created": True, "job_id": (job or {}).get("job_id", ""), "scope": "partial"}
            result = service.create_auto()
            log("info", str(result.get("backup_id", "")), "startup_backup_v2")
            return {"created": True, "backup_id": result.get("backup_id", ""),
                    "scope": result.get("scope", "")}
        except Exception as exc:
            log("error", f"{type(exc).__name__}: {exc}", "startup_backup_v2_failed")
            return {"created": False, "reason": "v2_failed", "error": type(exc).__name__}
        finally:
            _V2_STARTUP_LOCK.release()

    if background:
        thread = threading.Thread(target=work, name="startup-backup-v2", daemon=True)
        thread.start()
        return thread
    return work()


def _backup_dir(backup_id):
    name = str(backup_id or "").strip()
    target_dir = (BACKUP_DIR / name).resolve()
    if not name or name.startswith('.') or target_dir.parent != BACKUP_DIR.resolve() or not target_dir.is_dir():
        raise ValueError("backup_not_found")
    return target_dir


def restore_operational_backup(backup_id: str, parts=None):
    """Khôi phục cả bản hoặc từng mục — LUÔN sao lưu những mục sắp bị ghi đè trước."""
    target_dir = _backup_dir(backup_id)
    manifest = _manifest(target_dir)
    available = {part_of(record.get("path")) for record in manifest.get("files") or []}
    wanted = available if parts is None else available & set(parts)
    if not wanted:
        raise ValueError("backup_parts_not_in_backup")
    safety = create_operational_backup(keep=40, parts=sorted(wanted), reason="before_restore")
    targets = {}
    for record in manifest.get("files") or []:
        path = str(record.get("path") or "")
        destination = _restore_target(path)
        if destination is not None and part_of(path) in wanted:
            targets[path] = destination
    privacy_state = _capture_privacy_state(manifest, targets)
    result = restore_backup(target_dir, targets)
    purged = _reapply_privacy_state(privacy_state)
    return {**result, "parts": sorted(wanted), "safety_backup": safety.get("backup_id", ""),
            "privacy_reapplied": purged}


# Khoá HMAC máy (privacy.py). KHÔNG đưa vào bản V1: V1 chép tệp trần, không mã
# hoá — để khoá nằm cạnh dữ liệu sẽ làm tombstone (HMAC) dò ngược được.
PRIVACY_KEY_PATH = DATA_DIR / "machine" / "privacy_key"


def _read_opt_outs(db_path):
    path = Path(db_path)
    if not path.is_file():
        return []
    db = sqlite3.connect(path, timeout=10)
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_opt_outs'").fetchone():
            return []
        return db.execute("SELECT customer_key,display_name,opted_out_at FROM memory_opt_outs").fetchall()
    finally:
        db.close()


def _capture_privacy_state(manifest, targets):
    """Trước khi khôi phục: đọc tombstone + opt-out HIỆN TẠI để áp lại sau đó,
    tránh khách đã yêu cầu xoá/không ghi nhớ hiện lại từ bản cũ (giống V2)."""
    import privacy
    sqlite_paths = {record.get("path") for record in manifest.get("files") or []
                    if record.get("kind") == "sqlite"}
    dbs = [Path(dest) for rel, dest in targets.items() if rel in sqlite_paths]
    if not dbs:
        return None
    tombstones = privacy.merge_tombstones(privacy.read_tombstones(MEMORY_DB),
                                          *(privacy.read_tombstones(db) for db in dbs))
    opt_outs = {db: _read_opt_outs(db) for db in dbs}
    secret = privacy.load_secret_from_path(PRIVACY_KEY_PATH)
    if tombstones and secret is None:
        # Không áp lại được tombstone → không được khôi phục (sẽ làm khách đã xoá hiện lại).
        raise ValueError("backup_privacy_secret_required")
    return {"dbs": dbs, "tombstones": tombstones, "opt_outs": opt_outs, "secret": secret}


def _reapply_privacy_state(state):
    if not state:
        return {"deleted": 0, "tombstones": 0}
    import privacy
    deleted = 0
    for db_path in state["dbs"]:
        rows = state["opt_outs"].get(db_path) or []
        if rows:
            db = sqlite3.connect(db_path, timeout=10)
            try:
                with db:
                    db.execute("CREATE TABLE IF NOT EXISTS memory_opt_outs (customer_key TEXT PRIMARY KEY, "
                               "display_name TEXT NOT NULL DEFAULT '', opted_out_at TEXT NOT NULL)")
                    db.executemany("INSERT OR IGNORE INTO memory_opt_outs VALUES (?,?,?)", rows)
            finally:
                db.close()
        if state["tombstones"]:
            deleted += privacy.purge_db_by_tombstones(db_path, state["tombstones"], state["secret"])["deleted"]
    return {"deleted": deleted, "tombstones": len(state["tombstones"])}


# ------------------------------------------------------------------ xem chi tiết
def _sqlite_counts(path, queries):
    try:
        connection = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return {}
    out = {}
    try:
        for key, sql in queries.items():
            try:
                out[key] = connection.execute(sql).fetchone()[0]
            except sqlite3.Error:
                out[key] = None
    finally:
        connection.close()
    return out


_COUNTS = {
    "customer_memory.sqlite3": {
        "khách": "SELECT COUNT(*) FROM customers",
        "bình luận đã xử lý": "SELECT COUNT(*) FROM interactions",
        "điều AI nhớ về khách": "SELECT COUNT(*) FROM customer_facts",
    },
    "operator_workspace.sqlite3": {
        "câu đã sửa": "SELECT COUNT(*) FROM corrections",
        "câu mẫu đã duyệt": "SELECT COUNT(*) FROM approved_reply_examples",
        "mục trong thùng rác": "SELECT COUNT(*) FROM trash_items",
    },
    "ai_learning.sqlite3": {
        "điều AI đang dùng": "SELECT COUNT(*) FROM knowledge_items WHERE status='active'",
        "điều chờ duyệt": "SELECT COUNT(*) FROM knowledge_items WHERE status='suggested'",
        "người trực": "SELECT COUNT(*) FROM personas",
    },
}


def _json_count(path, relative):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    if relative.startswith("videos/"):
        return {"mốc ghim": len(data.get("events") or [])}
    if relative == "approved_product_knowledge.json":
        content = data.get("content") if isinstance(data.get("content"), dict) else data
        groups = content.get("nhom_san_pham") or []
        return {"hồ sơ sản phẩm": sum(len(g.get("san_pham") or []) for g in groups if isinstance(g, dict))}
    if relative == "approved_live_profiles.json":
        return {"hồ sơ LIVE đã duyệt": len(data.get("products") or [])}
    if relative in {"live_products_current.json"}:
        return {"sản phẩm trong giỏ": len(data.get("products") or [])}
    if relative in {"machine/catalog_mapping.json"}:
        return {"sản phẩm đã ánh xạ": len(data.get("assignments") or {})}
    if relative.startswith("tai_lieu/") and relative.endswith("/meta.json"):
        return {"tài liệu": 1}
    if relative == "cai_dat_bo_xu_ly.json":
        return {"bộ xử lý đã chỉnh": len(data) if isinstance(data, dict) else 0}
    if relative == "sales_playbook.json":
        return {"tình huống trong sổ tay": len(data.get("entries") or []) if isinstance(data, dict) else 0}
    return {}


def _counts_for(path, relative, kind):
    if kind == "sqlite" and relative in _COUNTS:
        return _sqlite_counts(path, _COUNTS[relative])
    if kind == "json" or (kind == "file" and relative.endswith("/meta.json")):
        return _json_count(path, relative)
    if kind == "jsonl":
        try:
            with Path(path).open("r", encoding="utf-8") as handle:
                return {"bình luận": sum(1 for line in handle if line.strip())}
        except OSError:
            return {}
    return {}


def _sum_counts(rows):
    total = {}
    for row in rows:
        for key, value in (row or {}).items():
            if isinstance(value, int):
                total[key] = total.get(key, 0) + value
    return total


def backup_detail(backup_id):
    """Bên trong một bản sao lưu có gì, và so với dữ liệu HIỆN TẠI thì khác gì."""
    target_dir = _backup_dir(backup_id)
    manifest = _manifest(target_dir)
    try:
        verify_backup(target_dir)
        integrity = ""
    except BackupError as exc:
        integrity = str(exc)
    grouped = {}
    for record in manifest.get("files") or []:
        path = str(record.get("path") or "")
        grouped.setdefault(part_of(path), []).append(record)
    sources = _sources()
    parts = []
    for part_id, name, description in PARTS:
        records = grouped.get(part_id) or []
        if not records:
            continue
        in_backup = _sum_counts(_counts_for(target_dir / r["path"], r["path"], r["kind"]) for r in records)
        current_rows = []
        for kind, mapping in sources[part_id].items():
            for relative, path in mapping.items():
                if kind == "jsonl":
                    current_rows.extend(_counts_for(file, f"raw_stream/{file.name}", "jsonl")
                                        for file in sorted(Path(path).glob("*.jsonl")))
                else:
                    current_rows.append(_counts_for(path, relative, kind))
        current = _sum_counts(current_rows)
        files = []
        for record in records:
            target = _restore_target(record["path"])
            files.append({"path": record["path"], "bytes": record.get("bytes", 0),
                          "changed_since": bool(target and target.is_file() and target.stat().st_size != record.get("bytes")),
                          "missing_now": bool(target and not target.is_file())})
        parts.append({
            "id": part_id, "name": name, "description": description,
            "bytes": sum(r.get("bytes", 0) for r in records), "files": files,
            "counts": [{"label": key, "backup": value, "current": current.get(key)}
                       for key, value in in_backup.items()],
            "restorable": any(_restore_target(r["path"]) is not None for r in records),
        })
    return {"ok": True, "id": target_dir.name, "created_at": manifest.get("created_at"),
            "scope": manifest.get("scope", "full"), "reason": manifest.get("reason", ""),
            "integrity_error": integrity, "parts": parts}


def parts_catalog():
    """Danh sách mục + dung lượng hiện tại (cho hộp chọn "Sao lưu từng mục")."""
    sources = _sources()
    rows = []
    for part_id, name, description in PARTS:
        size = 0
        for kind, mapping in sources[part_id].items():
            for path in mapping.values():
                path = Path(path)
                size += (sum(f.stat().st_size for f in path.glob("*.jsonl")) if kind == "jsonl"
                         else path.stat().st_size if path.is_file() else 0)
        rows.append({"id": part_id, "name": name, "description": description,
                     "bytes": size, "available": size > 0})
    return rows
