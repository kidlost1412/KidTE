"""Local operator workspace: inspect, correct and safely edit Kid TE data."""
from __future__ import annotations

import os
import copy
import base64
import io
import hashlib
import json
import re
import shutil
import sqlite3
import threading
from collections import OrderedDict
import time
import unicodedata
import uuid
import weakref
from functools import wraps
from contextlib import contextmanager, closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

_WRITE_LOCKS = weakref.WeakValueDictionary()
_WRITE_LOCKS_GUARD = threading.Lock()


def _serialized_write(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        if method.__name__ == 'patch' and (args[0] if args else kwargs.get('resource')) not in self.RESOURCE_PATCH_DUOC:
            raise ValueError('workspace_resource_read_only')
        key = str(self.db_path.resolve())
        with _WRITE_LOCKS_GUARD:
            lock = _WRITE_LOCKS.get(key)
            if lock is None:
                lock = threading.RLock()
                _WRITE_LOCKS[key] = lock
        with lock:
            return method(self, *args, **kwargs)
    return wrapped

from PIL import Image, ImageOps, UnidentifiedImageError

from config import (
    BACKUP_DIR, LIVE_CATALOG_PATH, LIVE_RAW_CATALOG_PATH, LOG_DIR, MEMORY_DB, ROOT,
)
from log_viewer import recent_logs
import dose_rules as _dose_rules
from write_journal import WriteJournal
from video_library import VIDEO_DIR, list_videos, load_timeline, validate_timeline


def _now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _json_hash(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _json_patch(before, after):
    """Small RFC-6902-compatible top-level diff for a readable audit trail."""
    patch = []
    for key in sorted(set(before) | set(after)):
        path = "/" + str(key).replace("~", "~0").replace("/", "~1")
        if key not in after:
            patch.append({"op": "remove", "path": path})
        elif key not in before:
            patch.append({"op": "add", "path": path, "value": after[key]})
        elif before[key] != after[key]:
            patch.append({"op": "replace", "path": path, "value": after[key]})
    return patch


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _safe_text(value, limit=2000):
    return str(value or "").strip()[:limit]


# `[F07]` Con so gan don vi do luong hoac gia tien — dau hieu cua mot cau tra
# loi mang du kien RIENG cua mot san pham, khong phai cach dien dat chung.
_DU_KIEN_SAN_PHAM = re.compile(
    r"\d[\d.,]*\s*(?:ml|cc|lit|l\u00edt|l\b|gam|gr|g\b|kg|%|"
    r"\u20ab|\u0111\b|vn\u0111|k\b|ngh\u00ecn|nghin|tri\u1ec7u|trieu)",
    re.IGNORECASE)


class WorkspaceStore:
    """Resource-oriented facade used only by the same-origin local UI."""

    PROFILE_FIELDS = {
        "ten_san_pham": "text", "phan_khuc": "text", "mo_ta_ngan": "text",
        "thanh_phan_dang_ky": "pairs", "cong_dung": "list",
        "doi_tuong_cay_trong": "list", "hieu_qua_chinh": "text",
        "huong_dan_su_dung": "list", "luu_y": "text",
        "quy_cach_dong_goi": "text", "gia_goc_duyet": "text",
        "live_promotion": "text", "listing_aliases": "list",
        "nguon_chinh_thuc": "text", "catalog_status": "text",
    }

    # `[F10]` Nguồn định nghĩa DUY NHẤT cho khả năng của từng tài nguyên.
    # catalog() công bố theo đúng ba tập này, còn patch()/rollback()/correct()
    # kiểm tra theo chính chúng — nên hợp đồng và hành vi không thể lệch nhau.
    RESOURCE_PATCH_DUOC = frozenset({"profiles", "videos", "conversations"})
    RESOURCE_ROLLBACK_DUOC = frozenset({"profiles", "videos"})
    RESOURCE_CORRECT_DUOC = frozenset({
        "conversations", "raw-events", "sessions", "interactions",
        "customers", "facts", "logs",
    })

    # Class-level defaults so stores built without __init__ (tests) still work;
    # the condition is shared by all instances (coarse but correct).
    _closed = False
    _open_count = 0
    _open_cond = threading.Condition()

    def __init__(self, engine, root=None):
        self.engine = engine
        self._fingerprint_hashes = OrderedDict()
        self._fingerprint_hash_lock = threading.Lock()
        self.root = Path(root or ROOT)
        self.db_path = self.root / "data" / "operator_workspace.sqlite3"
        self.override_path = Path(getattr(
            engine.knowledge, "overrides_path",
            self.root / "data" / "local_product_overrides.json",
        ))
        self.privacy = None     # optional PrivacyService, wired by the server (F02 provenance)
        self._create_schema()
        self.journal = WriteJournal(self.db_path.parent / "journal.jsonl")
        self.recover_journal()

    def _draft_revision_of(self, video_id):
        with self._db() as db:
            row = db.execute('SELECT revision FROM video_draft_revisions WHERE video_id=?', (video_id,)).fetchone()
        return row[0] if row else 0

    def recover_journal(self):
        """Idempotent startup replay/rollback of interrupted timeline+DB writes."""
        return self.journal.recover(self._journal_revision)

    def _change_revision(self, resource, record_id):
        with self._db() as db:
            return db.execute("SELECT COUNT(*) FROM operator_changes WHERE resource=? AND record_id=?",
                              (resource, record_id)).fetchone()[0]

    def _journal_revision(self, key):
        """Journal db_key -> monotonic DB revision. 'changes:<resource>:<id>' counts
        operator_changes rows; any other key is a video draft revision."""
        if isinstance(key, str) and key.startswith("changes:"):
            _, resource, record_id = key.split(":", 2)
            return self._change_revision(resource, record_id)
        return self._draft_revision_of(key)

    def _begin_file_first(self, kind, path, value, resource, record_id):
        """Journal + early replace for writers whose DB row (operator_changes) is
        written afterwards. Returns (op_id, prev_path). Caller must call
        _commit_file_first after the DB commit or _abort_file_first on failure."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".journal.tmp")
        prev = path.with_suffix(path.suffix + ".journal.prev")
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8")
        if path.exists():
            shutil.copyfile(path, prev)
        else:
            prev = None
        op_id = self.journal.begin(kind, [(path, tmp, prev)], "changes:%s:%s" % (resource, record_id),
                                   self._change_revision(resource, record_id))
        os.replace(tmp, path)
        return op_id, prev

    def _commit_file_first(self, op_id, prev):
        self.journal.mark_db_committed(op_id)
        if prev is not None:
            Path(prev).unlink(missing_ok=True)
        self.journal.done(op_id)

    def _abort_file_first(self, op_id, path, prev):
        if prev is not None and Path(prev).exists():
            os.replace(prev, path)
        self.journal.done(op_id)

    def _begin_guard(self, kind, path, resource, record_id, payload=None):
        """Journal a file write whose DB side (operator_changes row) commits later.
        payload: None (caller writes the file itself afterwards), bytes or JSON value
        (written here, replacing the target immediately). Returns a guard tuple."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".journal.tmp")
        prev = path.with_suffix(path.suffix + ".journal.prev")
        created = not path.exists()
        if payload is not None:
            if isinstance(payload, (bytes, bytearray)):
                tmp.write_bytes(bytes(payload))
            else:
                tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8")
        if created:
            prev = None
        else:
            shutil.copyfile(path, prev)
        op_id = self.journal.begin(kind, [(path, tmp, prev, created)], "changes:%s:%s" % (resource, record_id),
                                   self._change_revision(resource, record_id))
        if payload is not None:
            os.replace(tmp, path)
        return [op_id, path, prev, created, False]

    def _commit_guard(self, guard):
        op_id, _path, prev = guard[0], guard[1], guard[2]
        guard[4] = True
        self._commit_file_first(op_id, prev)

    def _abort_guard(self, guard):
        op_id, path, prev, created, finished = guard
        if finished:
            return
        if prev is not None and Path(prev).exists():
            os.replace(prev, path)
        elif created:
            Path(path).unlink(missing_ok=True)
        self.journal.done(op_id)

    def _begin_move_guard(self, kind, src, dst, resource, record_id):
        """Journal a file/directory rename paired with an operator_changes row."""
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        op_id = self.journal.begin_moves(kind, [(src, dst)], "changes:%s:%s" % (resource, record_id),
                                         self._change_revision(resource, record_id))
        return [op_id, Path(src), Path(dst), False]

    def _commit_move_guard(self, guard):
        guard[3] = True
        self.journal.mark_db_committed(guard[0])
        self.journal.done(guard[0])

    def _abort_move_guard(self, guard):
        op_id, src, dst, finished = guard
        if finished:
            return
        if dst.exists() and not src.exists():
            os.replace(dst, src)
        self.journal.done(op_id)

    def close(self, timeout=5.0):
        """Stop opening connections and wait for in-flight ones (restore writer close)."""
        with self._open_cond:
            self._closed = True
            deadline = time.monotonic() + timeout
            while self._open_count > 0:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise RuntimeError('workspace_close_timeout')
                self._open_cond.wait(left)
        with self._fingerprint_hash_lock:
            self._fingerprint_hashes.clear()

    @property
    def closed(self):
        return self._closed

    def _connect(self):
        with self._open_cond:
            if self._closed:
                raise RuntimeError('workspace_closed')
            self._open_count += 1
        try:
            return self._connect_raw()
        except BaseException:
            self._release_open()
            raise

    def _release_open(self):
        with self._open_cond:
            self._open_count = max(0, self._open_count - 1)
            self._open_cond.notify_all()

    def _connect_raw(self):
        connection = sqlite3.connect(str(self.db_path), timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _db(self):
        """Commit/rollback and always release the SQLite file handle on Windows."""
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()
            self._release_open()

    def _create_schema(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            _dose_rules.ensure_schema(db)
            existing = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'video_drafts' in existing and 'video_draft_revisions' not in existing:
                backup_dir = self.db_path.parent / 'schema_backups'
                backup_dir.mkdir(exist_ok=True)
                backup_path = backup_dir / ('workspace-before-draft-v2-' + uuid.uuid4().hex + '.sqlite3')
                with closing(sqlite3.connect(backup_path)) as snapshot:
                    db.backup(snapshot)
                    if snapshot.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                        raise ValueError('workspace_migration_backup_invalid')
            db.executescript("""
                CREATE TABLE IF NOT EXISTS operator_changes (
                    change_id TEXT PRIMARY KEY, resource TEXT NOT NULL,
                    record_id TEXT NOT NULL, changed_at TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '', before_json TEXT NOT NULL,
                    after_json TEXT NOT NULL, before_revision TEXT NOT NULL,
                    after_revision TEXT NOT NULL, action TEXT NOT NULL DEFAULT 'update',
                    patch_json TEXT NOT NULL DEFAULT '[]',
                    actor TEXT NOT NULL DEFAULT 'local_operator'
                );
                CREATE INDEX IF NOT EXISTS idx_operator_changes_target
                    ON operator_changes(resource, record_id, changed_at DESC);
                CREATE TABLE IF NOT EXISTS corrections (
                    correction_id TEXT PRIMARY KEY, resource TEXT NOT NULL,
                    record_id TEXT NOT NULL, created_at TEXT NOT NULL,
                    reason TEXT NOT NULL, changes_json TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS idx_corrections_target
                    ON corrections(resource, record_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS operator_snapshots (
                    snapshot_id TEXT PRIMARY KEY, resource TEXT NOT NULL,
                    record_id TEXT NOT NULL, created_at TEXT NOT NULL,
                    reason TEXT NOT NULL, data_json TEXT NOT NULL,
                    revision TEXT NOT NULL, restored_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_operator_snapshots_target
                    ON operator_snapshots(resource, record_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS trash_items (
                    trash_id TEXT PRIMARY KEY, resource TEXT NOT NULL,
                    record_id TEXT NOT NULL, deleted_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL, label TEXT NOT NULL,
                    data_json TEXT NOT NULL, snapshot_id TEXT NOT NULL,
                    restored_at TEXT, purged_at TEXT, archived_path TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_trash_active
                    ON trash_items(resource, record_id, restored_at, purged_at);
                CREATE TABLE IF NOT EXISTS video_drafts (
                    video_id TEXT PRIMARY KEY, saved_at TEXT NOT NULL,
                    data_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS video_draft_revisions (
                    video_id TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS duplicate_decisions (
                    pair_key TEXT PRIMARY KEY, first_video TEXT NOT NULL,
                    second_video TEXT NOT NULL, decision TEXT NOT NULL,
                    decided_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workspace_meta (
                    meta_key TEXT PRIMARY KEY, meta_value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS approved_reply_examples (
                    example_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    question TEXT NOT NULL, normalized_question TEXT NOT NULL,
                    question_type TEXT NOT NULL, product_id TEXT NOT NULL DEFAULT '',
                    product_revision TEXT NOT NULL DEFAULT '', approved_answer TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'active',
                    use_count INTEGER NOT NULL DEFAULT 0, last_used_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_reply_example_conversation
                    ON approved_reply_examples(conversation_id);
                CREATE TABLE IF NOT EXISTS product_images (
                    product_id TEXT PRIMARY KEY, updated_at TEXT NOT NULL,
                    image_hash TEXT NOT NULL, image_path TEXT NOT NULL
                );
            """)
            db.execute("INSERT OR IGNORE INTO workspace_meta(meta_key,meta_value) VALUES('version','1')")
            db.execute("INSERT OR IGNORE INTO workspace_meta(meta_key,meta_value) VALUES('changed_at',?)", (_now(),))
            db.execute("INSERT OR IGNORE INTO workspace_meta(meta_key,meta_value) VALUES('changed_groups','all')")
            columns = {row[1] for row in db.execute("PRAGMA table_info(operator_changes)")}
            if "patch_json" not in columns:
                db.execute("ALTER TABLE operator_changes ADD COLUMN patch_json TEXT NOT NULL DEFAULT '[]'")
            if "actor" not in columns:
                db.execute("ALTER TABLE operator_changes ADD COLUMN actor TEXT NOT NULL DEFAULT 'local_operator'")
            example_columns = {row[1] for row in db.execute("PRAGMA table_info(approved_reply_examples)")}
            if "persona_id" not in example_columns:
                db.execute("ALTER TABLE approved_reply_examples ADD COLUMN persona_id TEXT NOT NULL DEFAULT ''")
            for table in ("corrections", "approved_reply_examples"):
                have = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                if "customer_key_hmac" not in have:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN customer_key_hmac TEXT NOT NULL DEFAULT ''")
                if "legacy_excluded" not in have:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN legacy_excluded INTEGER NOT NULL DEFAULT 0")
            trash_columns = {row[1] for row in db.execute("PRAGMA table_info(trash_items)")}
            if "archived_path" not in trash_columns:
                db.execute("ALTER TABLE trash_items ADD COLUMN archived_path TEXT")

    def catalog(self):
        resources = [
            ("products", "SKU đang bán", "Sản phẩm", True),
            ("profiles", "Hồ sơ kỹ thuật", "Sản phẩm", True),
            ("videos", "Thư viện video", "Video", True),
            ("video-jobs", "Tác vụ phân tích", "Video", False),
            ("sessions", "Phiên LIVE", "Dữ liệu", False),
            ("conversations", "Bình luận & trả lời", "Phiên LIVE", True),
            ("raw-events", "Dữ liệu gốc", "Dữ liệu", False),
            ("interactions", "Tương tác", "Dữ liệu", False),
            ("customers", "Khách hàng", "Dữ liệu", False),
            ("facts", "Dữ kiện khách", "Dữ liệu", False),
            ("plugins", "Plugin", "Tự động hóa", False),
            ("audio-devices", "Thiết bị âm thanh", "Hệ thống", False),
            ("backups", "Bản sao lưu", "Hệ thống", False),
            ("logs", "Log vận hành", "Hệ thống", False),
            ("diagnostics", "Chẩn đoán", "Hệ thống", False),
        ]
        # `[F10]` Hợp đồng công bố PHẢI dựng từ đúng những hằng số mà patch()
        # và rollback() dùng để quyết định. Bản cũ suy ra actions từ cờ
        # `editable` của bảng trên, nên công bố `products` có cả patch lẫn
        # rollback trong khi patch() trả `workspace_resource_read_only` và
        # rollback() trả `rollback_not_supported`; `conversations` cũng được
        # công bố rollback mà handler không hề hỗ trợ. Giao diện vì thế bày ra
        # nút cho một thao tác chắc chắn thất bại.
        return {"ok": True, "resources": [
            {"id": rid, "label": label, "group": group, "viewable": True,
             "editable": rid in self.RESOURCE_PATCH_DUOC,
             "correctable": rid in self.RESOURCE_CORRECT_DUOC,
             "schema": self.PROFILE_FIELDS if rid == "profiles" else {},
             "actions": ([act for act, tap in (
                 ("patch", self.RESOURCE_PATCH_DUOC),
                 ("rollback", self.RESOURCE_ROLLBACK_DUOC),
                 ("correct", self.RESOURCE_CORRECT_DUOC),
             ) if rid in tap])}
            for rid, label, group, _editable in resources
        ]}

    def _snapshot(self, resource, record_id, data, reason, db=None):
        snapshot_id = uuid.uuid4().hex
        revision = _json_hash(data)
        if db is None:
            with self._db() as connection:
                return self._snapshot(resource, record_id, data, reason, connection)
        db.execute(
            "INSERT INTO operator_snapshots VALUES (?,?,?,?,?,?,?,NULL)",
            (snapshot_id, resource, record_id, _now(), _safe_text(reason, 500),
             json.dumps(data, ensure_ascii=False), revision),
        )
        return snapshot_id

    def _touch(self, *groups):
        self.invalidate_memo()
        with self._db() as db:
            row = db.execute("SELECT meta_value FROM workspace_meta WHERE meta_key='version'").fetchone()
            version = int(row[0] if row else 0) + 1
            values = {"version": str(version), "changed_at": _now(),
                      "changed_groups": ",".join(sorted(set(groups or ("all",))))}
            for key, value in values.items():
                db.execute("INSERT INTO workspace_meta(meta_key,meta_value) VALUES(?,?) "
                           "ON CONFLICT(meta_key) DO UPDATE SET meta_value=excluded.meta_value",
                           (key, value))
        return version

    def version(self):
        with self._db() as db:
            values = {row[0]: row[1] for row in db.execute(
                "SELECT meta_key,meta_value FROM workspace_meta").fetchall()}
        return {"ok": True, "version": int(values.get("version", 1)),
                "changed_at": values.get("changed_at", ""),
                "changed_groups": [x for x in values.get("changed_groups", "all").split(",") if x]}

    def _trashed_ids(self, resource):
        with self._db() as db:
            rows = db.execute(
                "SELECT record_id FROM trash_items WHERE resource=? "
                "AND restored_at IS NULL", (resource,),
            ).fetchall()
        return {str(row[0]) for row in rows}

    @staticmethod
    def _friendly_label(item):
        return _safe_text(item.get("title") or item.get("name") or item.get("label")
                          or item.get("filename")
                          or item.get("ten_san_pham") or item.get("text")
                          or item.get("comment_text") or item.get("id"), 160)

    privacy = None  # class default: stores built without __init__ (tests) stay legacy-safe
    _CUSTOMER_RESOURCES = ("conversations", "customers", "facts", "sessions", "raw-events")

    def _row_hmac(self, row):
        """HMAC provenance tag of the customer a row belongs to ('' if unknown / no privacy)."""
        if self.privacy is None or not isinstance(row, dict):
            return ""
        try:
            from customer_memory import CustomerMemory
            key, strength = CustomerMemory.customer_key(
                platform_user_id=row.get("user_id") or "", unique_id=row.get("unique_id") or "",
                display_name=row.get("nickname") or "",
                platform="shopee" if row.get("platform") == "shopee" or row.get("data_source") == "shopee-live"
                else "tiktok")
        except Exception:
            return ""
        return self.privacy.key_hmac(key) if strength >= 2 else ""

    def forget_customer(self, key_hmac, *, exclude_legacy=True, identifiers=()):
        """F02: delete corrections/examples attributed to ``key_hmac``; hide legacy ones that mention the customer."""
        from privacy import row_mentions
        marks = ",".join("?" for _ in self._CUSTOMER_RESOURCES)
        with self._db() as db:
            corr = db.execute("DELETE FROM corrections WHERE customer_key_hmac=? AND customer_key_hmac!=''",
                              (key_hmac,)).rowcount
            ex = db.execute("DELETE FROM approved_reply_examples WHERE customer_key_hmac=? "
                            "AND customer_key_hmac!=''", (key_hmac,)).rowcount
            excl = 0
            if exclude_legacy and identifiers:
                for row in db.execute(f"SELECT * FROM corrections WHERE customer_key_hmac='' "
                                      f"AND legacy_excluded=0 AND resource IN ({marks})",
                                      self._CUSTOMER_RESOURCES).fetchall():
                    if row_mentions(row, identifiers):
                        excl += db.execute("UPDATE corrections SET legacy_excluded=1 WHERE correction_id=?",
                                           (row["correction_id"],)).rowcount
                for row in db.execute("SELECT * FROM approved_reply_examples WHERE customer_key_hmac='' "
                                      "AND legacy_excluded=0").fetchall():
                    if row_mentions(row, identifiers):
                        excl += db.execute("UPDATE approved_reply_examples SET legacy_excluded=1 "
                                           "WHERE example_id=?", (row["example_id"],)).rowcount
        if corr or ex or excl:
            self._touch("conversations", "dashboard")
        return {"deleted": corr + ex, "excluded": excl}

    def _corrections(self, resource, record_id):
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM corrections WHERE resource=? AND record_id=? "
                "AND active=1 AND legacy_excluded=0 ORDER BY created_at", (resource, record_id),
            ).fetchall()
        return [{**dict(row), "changes": json.loads(row["changes_json"])} for row in rows]

    def _corrections_theo_lo(self, *resources):
        """Toan bo correction dang hieu luc cua mot so resource, mot lan doc.

        `_corrections()` mo mot ket noi SQLite RIENG cho tung ban ghi. Do
        duoc tren du lieu that (234 hoi thoai): moi lan ve dashboard mo khoang
        1450 ket noi va chay 1450 cau lenh, chiem 94% thoi gian cua
        /api/dashboard (791ms). Man hinh nay lai duoc poll moi 5 giay.
        """
        cho = ",".join("?" for _ in resources)
        with self._db() as db:
            rows = db.execute(
                f"SELECT * FROM corrections WHERE resource IN ({cho}) "
                f"AND active=1 AND legacy_excluded=0 ORDER BY created_at", tuple(resources),
            ).fetchall()
        ket = {}
        for row in rows:
            muc = dict(row)
            muc["changes"] = json.loads(row["changes_json"])
            ket.setdefault((row["resource"], str(row["record_id"])), []).append(muc)
        return ket

    def _examples_theo_lo(self):
        with self._db() as db:
            rows = db.execute("SELECT * FROM approved_reply_examples WHERE legacy_excluded=0").fetchall()
        return {str(row["conversation_id"]): dict(row) for row in rows}

    def _history(self, resource, record_id):
        with self._db() as db:
            rows = db.execute(
                "SELECT change_id, changed_at, reason, before_revision, "
                "after_revision, action, patch_json, actor FROM operator_changes "
                "WHERE resource=? AND record_id=? ORDER BY changed_at DESC LIMIT 50",
                (resource, record_id),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["patch"] = json.loads(item.pop("patch_json") or "[]")
            result.append(item)
        return result

    def recent_activity(self, limit=8):
        """Hoạt động gần đây đã dịch sang ngôn ngữ vận hành cho trang chủ."""
        try:
            limit = max(1, min(20, int(limit)))
        except (TypeError, ValueError):
            limit = 8
        with self._db() as db:
            rows = db.execute(
                "SELECT changed_at,resource,record_id,action,reason "
                "FROM operator_changes ORDER BY changed_at DESC LIMIT ?", (limit,),
            ).fetchall()
        actions = {
            "update": "Đã cập nhật", "rollback": "Đã khôi phục bản trước",
            "trash": "Đã đưa vào Thùng rác", "restore": "Đã khôi phục",
            "merge": "Đã gộp video", "correction": "Đã sửa dữ liệu lịch sử",
        }
        return [{
            "when": row["changed_at"], "resource": row["resource"],
            "record_id": row["record_id"],
            "label": actions.get(row["action"], "Đã thay đổi"),
            "description": row["reason"] or actions.get(row["action"], "Đã thay đổi"),
        } for row in rows]

    def _record_change(self, resource, record_id, before, after, reason, action="update", db=None):
        before_revision, after_revision = _json_hash(before), _json_hash(after)
        if db is None:
            with self._db() as connection:
                self._record_change(resource, record_id, before, after, reason, action, connection)
            self._touch(resource, "dashboard")
        else:
            db.execute(
                "INSERT INTO operator_changes "
                "(change_id,resource,record_id,changed_at,reason,before_json,after_json,"
                "before_revision,after_revision,action,patch_json,actor) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, resource, record_id, _now(), _safe_text(reason, 500),
                 json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False),
                 before_revision, after_revision, action,
                json.dumps(_json_patch(before, after), ensure_ascii=False), "local_operator"),
            )
        return after_revision

    # Những khoá chỉ là bản sao/dẫn xuất của dữ liệu đã có ở nơi khác trong
    # cùng bản ghi. Bỏ chúng khi tìm kiếm thì phạm vi tìm không đổi, nhưng
    # không còn phải serialize đi serialize lại cùng một nội dung.
    KHOA_BO_QUA_KHI_TIM = ("previous_customer_comments", "context", "effective",
                           "corrections", "similar_approved_examples",
                           "conversations", "transcript")

    @classmethod
    def _khop_tu_khoa(cls, item, q):
        if not isinstance(item, dict):
            return q in str(item).casefold()
        gon = {k: v for k, v in item.items() if k not in cls.KHOA_BO_QUA_KHI_TIM}
        if q in json.dumps(gon, ensure_ascii=False, default=str).casefold():
            return True
        # `effective` là bản đã áp bản sửa của người vận hành — vẫn phải tìm
        # được theo nội dung đã sửa, nên soi riêng phần chênh lệch.
        hieu_luc = item.get("effective")
        if isinstance(hieu_luc, dict):
            khac = {k: v for k, v in hieu_luc.items()
                    if k not in cls.KHOA_BO_QUA_KHI_TIM and item.get(k) != v}
            if khac and q in json.dumps(khac, ensure_ascii=False,
                                        default=str).casefold():
                return True
        return False

    @classmethod
    def _paginate(cls, items, query):
        q = _safe_text(query.get("q", ""), 200).casefold()
        if q:
            items = [item for item in items if cls._khop_tu_khoa(item, q)]
        try:
            page = max(1, int(query.get("page", 1)))
            limit = max(1, min(500 if query.get("view") == "summary" else 100, int(query.get("limit", 50))))
        except (TypeError, ValueError):
            page, limit = 1, 50
        start = (page - 1) * limit
        return {"items": items[start:start + limit], "page": page, "limit": limit,
                "total": len(items), "pages": max(1, (len(items) + limit - 1) // limit)}

    def _profiles(self):
        assigned = self.engine.knowledge.mapping_assignments()
        counts = {}
        for profile_id in assigned.values():
            counts[profile_id] = counts.get(profile_id, 0) + 1
        rows = []
        for record in self.engine.knowledge.all_records():
            item = copy.deepcopy(record)
            item["id"] = str(item.get("san_pham_id") or "")
            item["title"] = str(item.get("ten_san_pham") or item["id"])
            item["mapped_skus"] = counts.get(item["id"], 0)
            item["revision"] = _json_hash(item)
            rows.append(item)
        return rows

    def _products(self):
        image_by_id = {}
        with self._db() as db:
            for image in db.execute("SELECT product_id,image_hash FROM product_images"):
                image_by_id[str(image["product_id"])] = f"/api/product/image/{image['product_id']}?v={image['image_hash'][:12]}"
        try:
            raw = json.loads(Path(LIVE_RAW_CATALOG_PATH).read_text(encoding="utf-8"))
            for product in ((raw.get("raw_api_data") or {}).get("products") or []):
                cover = product.get("cover") or {}
                urls = cover.get("url_list") or cover.get("thumb_url_list") or []
                if isinstance(urls, str):
                    urls = [urls]
                if urls:
                    image_by_id.setdefault(str(product.get("product_id") or ""), str(urls[0]))
        except (OSError, json.JSONDecodeError, AttributeError, TypeError):
            pass
        rows = []
        for item in self.engine.mapping_state().get("products", []):
            row = copy.deepcopy(item)
            row["id"] = row.get("product_id")
            row["image_url"] = image_by_id.get(str(row["id"]), "")
            row["subtitle"] = f"Giỏ #{row.get('bag_index') or '—'} · {row.get('profile_name') or 'Chưa ánh xạ'}"
            row["revision"] = _json_hash(row)
            rows.append(row)
        return rows

    def _videos(self):
        rows, hashes, source_names = [], {}, {}
        trashed = self._trashed_ids("videos")
        for item in list_videos(check_fingerprint=True):
            if str(item.get("video_id")) in trashed:
                continue
            timeline = item.get("timeline") or {}
            fingerprint = item.get("fingerprint")
            fingerprint_hash = ""
            if fingerprint:
                try:
                    fingerprint_hash = self._fingerprint_hash(Path(fingerprint))
                except OSError:
                    pass
            if fingerprint_hash:
                hashes.setdefault(fingerprint_hash, []).append(item["video_id"])
            source_file = str(timeline.get("source_file", ""))
            source_key = "".join(ch for ch in Path(source_file).stem.casefold() if ch.isalnum())
            if source_key:
                source_names.setdefault(source_key, []).append(item["video_id"])
            transcript_path = VIDEO_DIR / item["video_id"] / "transcript.json"
            transcript_segments = 0
            if transcript_path.exists():
                try:
                    transcript_segments = len(json.loads(transcript_path.read_text(encoding="utf-8")).get("segments", []))
                except (OSError, json.JSONDecodeError, TypeError):
                    pass
            # Lúc lịch ghim được ghi lần cuối — Studio hiện "Đã áp dụng lúc …".
            try:
                applied_at = datetime.fromtimestamp(
                    (VIDEO_DIR / item["video_id"] / "timeline.json").stat().st_mtime
                ).astimezone().isoformat(timespec="seconds")
            except OSError:
                applied_at = ""
            rows.append({
                "applied_at": applied_at,
                "id": item["video_id"], "title": timeline.get("title") or item["video_id"],
                "source_file": source_file, "cycle_ms": timeline.get("cycle_ms", 0),
                "events": len(timeline.get("events") or []), "products": len(timeline.get("products") or {}),
                "transcript_segments": transcript_segments, "errors": item.get("errors") or [],
                "fingerprint_hash": fingerprint_hash, "source_key": source_key, "timeline": timeline,
                "revision": _json_hash(timeline),
            })
        with self._db() as db:
            resolved_pairs = {row[0] for row in db.execute(
                "SELECT pair_key FROM duplicate_decisions WHERE decision='keep_both'"
            ).fetchall()}
            drafts = {row[0]: row[1] for row in db.execute(
                "SELECT video_id,saved_at FROM video_drafts").fetchall()}
        for row in rows:
            row["draft_saved_at"] = drafts.get(row["id"], "")
            fingerprint_group = (hashes.get(row["fingerprint_hash"], [])
                                 if row["fingerprint_hash"] else [])
            source_group = source_names.get(row["source_key"], [])
            row["duplicates"] = sorted({
                video_id for video_id in fingerprint_group + source_group
                if video_id != row["id"]
                and "|".join(sorted((row["id"], video_id))) not in resolved_pairs
            })
            row["duplicate_reason"] = (
                "fingerprint_and_source" if len(fingerprint_group) > 1 and len(source_group) > 1
                else "fingerprint" if len(fingerprint_group) > 1
                else "normalized_source_name" if len(source_group) > 1 else ""
            )
        rows.sort(key=lambda item: (not bool(item["duplicates"]), item["id"]))
        return rows

    def _fingerprint_hash(self, path):
        """Bounded file-stat cache; never reread full fingerprints every poll."""
        stat = path.stat()
        key = (str(path.resolve()), stat.st_size, stat.st_mtime_ns)
        now = time.monotonic()
        with self._fingerprint_hash_lock:
            cached = self._fingerprint_hashes.get(key)
            if cached and now - cached[0] < 300:
                self._fingerprint_hashes.move_to_end(key)
                return cached[1]
        digest = hashlib.sha256()
        with path.open('rb') as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(block)
        after = path.stat()
        if (after.st_size, after.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
            raise OSError('fingerprint_changed_during_hash')
        value = digest.hexdigest()
        with self._fingerprint_hash_lock:
            self._fingerprint_hashes[key] = (now, value)
            while len(self._fingerprint_hashes) > 256:
                self._fingerprint_hashes.popitem(last=False)
        return value

    @staticmethod
    def _fold(value):
        value = unicodedata.normalize("NFD", str(value or "").casefold().replace("đ", "d"))
        return " ".join("".join(ch if ch.isalnum() else " " for ch in value
                                if unicodedata.category(ch) != "Mn").split())

    @classmethod
    def _question_type(cls, text):
        value = cls._fold(text)
        groups = [
            ("Giá và khuyến mãi", ("gia", "bao nhieu tien", "may k", "khuyen mai", "ma giam", "sale")),
            ("Liều lượng", ("lieu", "pha bao", "pha may", "bao nhieu ml", "binh 20", "lit nuoc", "phuy")),
            ("Pha chung", ("pha chung", "tron chung", "di chung", "ket hop")),
            ("Cách sử dụng", ("cach dung", "dung sao", "xai sao", "tuoi goc", "phun la", "bon sao")),
            ("Tồn kho và quy cách", ("con hang", "con may", "quy cach", "chai may", "goi may", "can may")),
            ("Đặt hàng", ("dat hang", "chot", "mua", "siet mot", "gio hang")),
            ("Chọn sản phẩm", ("loai nao", "san pham nao", "nen dung", "tu van", "tri ", "bi ")),
            ("Phản hồi sau mua", ("da mua", "da san", "xai roi", "dung roi", "hieu qua")),
            ("Chào hỏi", ("chao", "hello", "hi shop", "xin chao")),
        ]
        if len(value) < 2 or value in {"ok", "roi", "dr", "a", "alo"}:
            return "Không cần trả lời"
        return next((label for label, words in groups if any(word in value for word in words)),
                    "Chưa phân loại")

    @staticmethod
    def _parse_at(value):
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            return 0.0

    def _read_raw_rows(self):
        rows = []
        for path in sorted((LOG_DIR / "raw_stream").glob("stream_*.jsonl")):
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for number, line in enumerate(lines, 1):
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(item, dict):
                    continue
                stable = str(item.get("msg_id") or f"{path.name}:{number}:{line}")
                rows.append({"id": hashlib.sha256(stable.encode("utf-8")).hexdigest()[:24],
                             "source_file": path.name, "source_line": number,
                             "_at": self._parse_at(item.get("ts")), **item})
        rows.sort(key=lambda row: (row["_at"], row["source_file"], row["source_line"]))
        return rows

    def _memory_by_comment(self):
        result = {}
        if not MEMORY_DB.exists():
            return result
        try:
            with sqlite3.connect(str(MEMORY_DB), timeout=5) as db:
                db.row_factory = sqlite3.Row
                for raw in db.execute("SELECT * FROM interactions WHERE event_type='comment'"):
                    row = dict(raw)
                    key = str(row.get("runtime_comment_id") or "")
                    if key:
                        result[key] = row
        except (sqlite3.Error, OSError):
            pass
        return result

    @staticmethod
    def _same_stream(left, right):
        left_room, right_room = str(left.get("room_id") or ""), str(right.get("room_id") or "")
        if left_room and right_room:
            return left_room == right_room
        return str(left.get("session_id") or "") == str(right.get("session_id") or "")

    def _sessionize(self, rows):
        by_scope, sequence = {}, 0
        for row in rows:
            # room_id của TikTok = MỘT phiên LIVE: không cắt theo khoảng lặng (phiên
            # phát lại chat thưa, bản cũ cắt 15 phút → 71 "phiên", 22 phiên 1 câu).
            # Chỉ dữ liệu không có room_id mới đoán phiên bằng khoảng lặng.
            scope = str(row.get("room_id") or row.get("session_id") or "unknown")
            gap = 6 * 3600 if row.get("room_id") else 900
            previous = by_scope.get(scope)
            if previous is None or row["_at"] - previous["last"] > gap:
                sequence += 1
                started = datetime.fromtimestamp(row["_at"], timezone.utc).strftime("%Y%m%dT%H%M%S") if row["_at"] else str(sequence)
                previous = {"id": hashlib.sha256(f"{scope}:{started}".encode()).hexdigest()[:20],
                            "last": row["_at"]}
                by_scope[scope] = previous
            previous["last"] = max(previous["last"], row["_at"])
            row["business_session_id"] = previous["id"]

    # Bộ nhớ đệm cho các bản dựng TỐN KÉM (bình luận, khách, phiên).
    # Bản cũ dựng lại toàn bộ ở mỗi request — /api/dashboard (poll 5 giây)
    # mất ~0,6 s, trong đó dựng bình luận 2 lần và 71 kết nối SQLite. Khoá
    # là chữ ký (cỡ + mtime) của mọi file đầu vào; TTL ngắn vì trạng thái
    # "đang chờ" tự chuyển thành "bị bỏ sót" sau 180 giây.
    MEMO_TTL_S = 15.0

    def _input_signature(self):
        paths = sorted((LOG_DIR / "raw_stream").glob("stream_*.jsonl"))
        db_path = getattr(self, "db_path", None)
        extra = [MEMORY_DB, Path(str(MEMORY_DB) + "-wal"), Path(LIVE_CATALOG_PATH),
                 Path(getattr(getattr(getattr(self, "engine", None), "knowledge", None), "mapping_path", "") or LIVE_CATALOG_PATH)]
        if db_path:
            extra += [Path(db_path), Path(str(db_path) + "-wal")]
        if getattr(self, "override_path", None):
            extra.append(Path(self.override_path))
        paths += extra
        sig = []
        for path in paths:
            try:
                stat = path.stat()
                sig.append((str(path), stat.st_size, stat.st_mtime_ns))
            except OSError:
                sig.append((str(path), -1, -1))
        return tuple(sig)

    def _memo_get(self, key):
        memo = getattr(self, "_memo", None)
        if memo is None:
            return None
        entry = memo.get(key)
        if not entry or time.monotonic() - entry[1] > self.MEMO_TTL_S:
            return None
        if entry[0] != self._input_signature():
            memo.clear()
            return None
        return list(entry[2])

    def _memo_put(self, key, value, signature):
        if not hasattr(self, "_memo"):
            self._memo = {}
        self._memo[key] = (signature, time.monotonic(), list(value))

    def invalidate_memo(self):
        getattr(self, "_memo", {}).clear()

    def _raw_events(self):
        cached = self._memo_get(("raw",))
        if cached is not None:
            return cached
        signature = self._input_signature()
        rows = self._raw_events_uncached()
        self._memo_put(("raw",), rows, signature)
        return rows

    def _raw_events_uncached(self):
        raw_rows = self._read_raw_rows()
        self._sessionize(raw_rows)
        products = {str(item.get("product_id")): item for item in
                    self.engine.mapping_state().get("products", [])}
        memory = self._memory_by_comment()
        reply_for, claimed = {}, set()
        for reply_index, candidate in enumerate(raw_rows):
            if not candidate.get("is_host") or candidate.get("ai_action") == "self_echo":
                continue
            target = str(candidate.get("reply_to_username") or "").casefold()
            if not target:
                match = re.match(r"^@([^\s]+)", str(candidate.get("text") or ""))
                target = (match.group(1) if match else "").casefold()
            for customer in reversed(raw_rows[:reply_index]):
                if candidate["_at"] - customer["_at"] > 180:
                    break
                if customer.get("is_host") or customer["id"] in claimed or not self._same_stream(customer, candidate):
                    continue
                identities = {str(customer.get("unique_id") or "").casefold(),
                              str(customer.get("nickname") or "").casefold()}
                if target and target not in identities:
                    continue
                reply_for[customer["id"]] = candidate
                claimed.add(customer["id"])
                break
        customers, seen = [], set()
        # Nap mot lan cho ca lo. Truoc day moi dong mo mot ket noi SQLite
        # rieng cho correction va cho vi du da duyet.
        theo_lo = self._corrections_theo_lo("raw-events", "conversations")
        vi_du_theo_lo = self._examples_theo_lo()
        for _index, item in enumerate(raw_rows):
            if item.get("is_host"):
                continue
            message_key = str(item.get("msg_id") or item["id"])
            if message_key in seen:
                continue
            seen.add(message_key)


            reply = reply_for.get(item["id"])
            memory_row = memory.get(str(item.get("msg_id") or ""), {})
            delivery = str(memory_row.get("delivery_status") or "")
            reply_text = str(memory_row.get("response") or (reply or {}).get("text") or "")
            if reply and (reply.get("ai_action") == "ai_replied" or
                          str(reply.get("nickname") or "").casefold() in {"kid te (ai)", "live re (ai)"}):
                responder = "ai"
                status = "ai_sent" if delivery == "sent" else "ai_failed" if delivery in {"failed", "cancelled"} else "ai_unconfirmed"
            elif reply:
                responder, status = "manual", "manual_replied"
            elif reply_text and delivery == "sent":
                # Tiện ích đã XÁC NHẬN gửi (bộ nhớ khách ghi delivery=sent) nhưng
                # luồng bình luận không bắt được tin phản hồi của host. Bản cũ xếp
                # các câu này vào "bị bỏ sót" dù khách đã được trả lời.
                responder, status = "ai", "ai_sent"
            elif reply_text and delivery in {"failed", "cancelled"}:
                responder, status = "ai", "ai_failed"
            else:
                responder = "none"
                action = str(item.get("ai_action") or "")
                if self._question_type(item.get("text")) == "Không cần trả lời" or action in {"too_short", "self_echo"}:
                    status = "ignored"
                elif action in {"missing_data", "policy_blocked"}:
                    status = "skipped_safely"
                elif time.time() - item["_at"] <= 180:
                    status = "waiting"
                else:
                    status = "unanswered"
            action_reason = {
                "ai_off": "AI đang tắt khi bình luận đến",
                "clock_stopped": "Phiên LIVE chưa được xác nhận đang chạy",
                "queued_for_ai": "Đã đưa vào hàng đợi AI",
                "missing_data": "Thiếu dữ liệu đã duyệt để trả lời an toàn",
                "policy_blocked": "Cổng an toàn đã chặn câu trả lời",
                "too_short": "Nội dung quá ngắn",
            }.get(str(item.get("ai_action") or ""), "")
            pin_id = str(item.get("pin_id") or "")
            product = products.get(str(memory_row.get("product_id") or pin_id), {})
            row = {**{k: v for k, v in item.items() if k != "_at"},
                "conversation_id": item["id"], "session_id": item["business_session_id"],
                "original_text": item.get("text") or "", "corrected_text": "",
                "question_type": self._question_type(item.get("text")),
                "classification_source": "system_suggestion",
                "selected_product_id": str(memory_row.get("product_id") or pin_id),
                "selected_product_name": str(memory_row.get("product_title") or product.get("title") or ""),
                "product_image_url": str(product.get("image_url") or ""),
                "responder": responder, "conversation_status": status,
                "reply_text": reply_text, "reply_at": (reply or {}).get("ts", ""),
                "delivery_status": delivery or ("unconfirmed" if responder == "ai" else "sent" if responder == "manual" else "none"),
                "no_reply_reason": action_reason,
                "must_not_reply_again": status in {"ai_sent", "ai_unconfirmed", "manual_replied"},
                "response_quality": "unreviewed", "correct_answer": "",
                "operator_note": "", "approved_for_ai": False,
                "learning_status": "not_approved", "memory_interaction_id": memory_row.get("interaction_id", "")}
            ma = str(item["id"])
            corrections = (theo_lo.get(("raw-events", ma), [])
                           + theo_lo.get(("conversations", ma), []))
            row["corrections"] = corrections
            example = vi_du_theo_lo.get(ma)
            # Chốt revision TRƯỚC mọi bước làm giàu — xem _phien_ban_hoi_thoai.
            row["revision"] = self._phien_ban_hoi_thoai(row, corrections, example)
            effective = copy.deepcopy(row)
            for correction in corrections:
                effective.update(correction["changes"])
            if example:
                effective["approved_for_ai"] = example["status"] == "active"
                effective["learning_status"] = example["status"]
                effective["example_id"] = example["example_id"]
            row["effective"] = effective
            customers.append(row)
        # BẢN SAO RÚT GỌN — không bao giờ trỏ thẳng sang dict bản ghi khác.
        #
        # Bản cũ gán tham chiếu tới các bản ghi hàng xóm, mà mỗi bản ghi đó
        # cũng đã có ``previous_customer_comments`` của riêng nó → cây lồng
        # nhau phân nhánh 5, sâu bằng số bình luận của cùng một khách. Đo được
        # trên dữ liệu thật: một lần bấm xem chi tiết serialize 218 MB JSON,
        # bản ghi lớn nhất 105 MB — chỉ để hiển thị 5 dòng chữ.
        gan_day = {}
        for row in customers:
            identity = str(row.get("user_id") or row.get("unique_id")
                           or row.get("nickname") or "")
            if row.get("platform") == "shopee" or row.get("data_source") == "shopee-live":
                identity = "shopee:" + identity
            row["previous_customer_comments"] = list(gan_day.get(identity, ()))
            gan_day.setdefault(identity, []).append({
                "id": row["id"],
                "ts": row.get("ts", ""),
                "original_text": row.get("original_text", ""),
                "question_type": row.get("question_type", ""),
            })
            gan_day[identity] = gan_day[identity][-5:]
        customers.sort(key=lambda row: str(row.get("ts") or ""), reverse=True)
        return customers

    def _example_for(self, conversation_id):
        with self._db() as db:
            row = db.execute("SELECT * FROM approved_reply_examples WHERE conversation_id=?",
                             (conversation_id,)).fetchone()
        return dict(row) if row else None

    def _derived_live_rows(self, resource):
        key = ("derived", resource)
        cached = self._memo_get(key)
        if cached is not None:
            return cached
        signature = self._input_signature()
        result = self._derived_live_rows_uncached(resource)
        self._memo_put(key, result, signature)
        return result

    FACT_LABELS = {
        "cay_trong": "Cây trồng", "dien_tich": "Diện tích", "quy_mo": "Quy mô", "van_de": "Vấn đề đang gặp",
        "san_pham_quan_tam": "Sản phẩm quan tâm", "giai_doan": "Giai đoạn cây", "khu_vuc": "Khu vực",
    }

    @staticmethod
    def _fact_id(customer_key, fact_key):
        return hashlib.sha256(f"{customer_key}|{fact_key}".encode()).hexdigest()[:24]

    def _fact_by_id(self, record_id):
        memory = getattr(self.engine, "memory", None)
        for fact in (memory.fact_rows() if memory is not None else []):
            if self._fact_id(fact["customer_key"], fact["fact_key"]) == record_id:
                return fact
        return None

    def update_fact(self, record_id, value):
        """Sửa thông tin khách — AI dùng giá trị mới từ lượt trả lời kế tiếp."""
        fact = self._fact_by_id(record_id)
        if not fact:
            raise ValueError("fact_not_found")
        value = _safe_text(value, 200)
        if not value:
            raise ValueError("fact_value_required")
        self.engine.memory.set_fact(fact["customer_key"], fact["fact_key"], value)
        self._record_change("facts", record_id, fact, {**fact, "fact_value": value}, "Chủ shop sửa thông tin khách")
        self._touch("facts", "customers", "dashboard")
        return {"ok": True, "id": record_id, "fact_value": value}

    def _derived_live_rows_uncached(self, resource):
        comments = self._raw_events()
        theo_lo = self._corrections_theo_lo(resource) if resource in {"customers", "facts", "sessions"} else {}
        if resource == "interactions":
            return [{**row,
                     "comment_text": row.get("original_text", ""),
                     "customer_key": row.get("unique_id") or row.get("nickname"),
                     "response_status": row.get("conversation_status")}
                    for row in comments if row.get("reply_text")]
        if resource == "customers":
            grouped = {}
            for row in comments:
                key = str(row.get("user_id") or row.get("unique_id") or row.get("nickname") or "Khách")
                platform = row.get("platform") or ("shopee" if row.get("data_source") == "shopee-live" else "tiktok")
                if platform == "shopee":
                    key = f"shopee:id:{key}"
                item = grouped.setdefault(key, {"id": key, "customer_key": key,
                    "platform": platform,
                    "display_name": row.get("nickname") or row.get("unique_id") or "Khách",
                    "unique_id": row.get("unique_id") or "", "comment_count": 0,
                    "ai_reply_count": 0, "manual_reply_count": 0, "unanswered_count": 0,
                    "last_seen": row.get("ts") or "", "topics": set(), "products": set(),
                    "session_ids": set(), "product_ids": set(), "conversation_ids": [], "corrections": []})
                item["comment_count"] += 1
                item["ai_reply_count"] += int(row.get("responder") == "ai")
                item["manual_reply_count"] += int(row.get("responder") == "manual")
                item["unanswered_count"] += int(row.get("conversation_status") in {"unanswered", "waiting"})
                item["last_seen"] = max(str(item["last_seen"]), str(row.get("ts") or ""))
                item["topics"].add(row.get("question_type") or "Khác")
                if row.get("selected_product_name"):
                    item["products"].add(row["selected_product_name"])
                if row.get("selected_product_id"):
                    item["product_ids"].add(str(row["selected_product_id"]))
                item["session_ids"].add(row.get("session_id"))
                item["conversation_ids"].append(row["id"])
            result = []
            for item in grouped.values():
                item["topics"] = sorted(item["topics"])
                item["products"] = sorted(item["products"])
                item["product_ids"] = sorted(item["product_ids"])
                item["session_count"] = len({x for x in item.pop("session_ids") if x})
                corrections = theo_lo.get(("customers", str(item["id"])), [])
                item["corrections"] = corrections
                item["effective"] = copy.deepcopy(item)
                for correction in corrections:
                    item["effective"].update(correction["changes"])
                result.append(item)
            return sorted(result, key=lambda row: row["last_seen"], reverse=True)
        if resource == "facts":
            # Thông tin AI THẬT SỰ nhớ về khách (cây trồng, diện tích, vấn đề…)
            # — đọc từ trí nhớ khách. Bản cũ tự sinh "Chủ đề khách đã hỏi" từ
            # loại câu hỏi: 228 dòng "Chưa phân loại", lặp cột "Hay hỏi về".
            memory = getattr(self.engine, "memory", None)
            source = memory.fact_rows() if memory is not None and hasattr(memory, "fact_rows") else []
            rows = []
            for fact in source:
                rows.append({"id": self._fact_id(fact["customer_key"], fact["fact_key"]),
                    "customer_key": fact["customer_key"], "display_name": fact.get("display_name") or fact["customer_key"],
                    "fact_key": fact["fact_key"], "fact_label": self.FACT_LABELS.get(fact["fact_key"], fact["fact_key"]),
                    "fact_value": fact["fact_value"], "source_comment": fact.get("source_comment") or "",
                    "last_seen": fact.get("updated_at") or "", "corrections": []})
            for row in rows:
                corrections = theo_lo.get(("facts", str(row["id"])), [])
                row["corrections"] = corrections
                row["effective"] = copy.deepcopy(row)
                for correction in corrections:
                    row["effective"].update(correction["changes"])
            return rows
        if resource == "sessions":
            grouped = {}
            for row in comments:
                key = str(row.get("session_id") or row.get("room_id") or "không-rõ-phiên")
                item = grouped.setdefault(key, {"id": key, "session_id": key,
                    "room_id": row.get("room_id") or "", "started_at": row.get("ts") or "",
                    "ended_at": row.get("ts") or "", "comment_count": 0,
                    "ai_reply_count": 0, "manual_reply_count": 0, "unanswered_count": 0,
                    "failed_count": 0, "ignored_count": 0, "needs_review_count": 0, "customer_count": 0,
                    "top_topics": set(), "products": set(), "product_ids": set(), "_customers": set(),
                    "conversation_ids": [], "corrections": []})
                item["started_at"] = min(str(item["started_at"]), str(row.get("ts") or ""))
                item["ended_at"] = max(str(item["ended_at"]), str(row.get("ts") or ""))
                item["comment_count"] += 1
                item["ai_reply_count"] += int(row.get("responder") == "ai")
                item["manual_reply_count"] += int(row.get("responder") == "manual")
                item["unanswered_count"] += int(row.get("conversation_status") in {"unanswered", "waiting"})
                item["failed_count"] += int(row.get("conversation_status") == "ai_failed")
                item["ignored_count"] += int(row.get("conversation_status") in {"ignored", "skipped_safely"})
                item["needs_review_count"] += int(row.get("response_quality") in {"unreviewed", "incorrect", "partly_correct"})
                item["_customers"].add(row.get("user_id") or row.get("unique_id") or row.get("nickname"))
                item["top_topics"].add(row.get("question_type") or "Khác")
                if row.get("selected_product_name"):
                    item["products"].add(row["selected_product_name"])
                if row.get("selected_product_id"):
                    item["product_ids"].add(str(row["selected_product_id"]))
                item["conversation_ids"].append(row["id"])
            result = []
            for item in grouped.values():
                item["customer_count"] = len({x for x in item.pop("_customers") if x})
                item["top_topics"] = sorted(item["top_topics"])
                item["products"] = sorted(item["products"])
                item["product_ids"] = sorted(item["product_ids"])
                item["status"] = "ended"
                corrections = theo_lo.get(("sessions", str(item["id"])), [])
                item["corrections"] = corrections
                item["effective"] = copy.deepcopy(item)
                for correction in corrections:
                    item["effective"].update(correction["changes"])
                result.append(item)
            result.sort(key=lambda row: row["ended_at"], reverse=True)
            heartbeat = getattr(self.engine, "extension_heartbeat", None) or {}
            heartbeat_fresh = time.time() - float(heartbeat.get("received_at") or 0) <= 25
            if result and heartbeat_fresh and heartbeat.get("live_running") is True:
                result[0]["status"] = "active"
                result[0]["effective"]["status"] = "active"
            return result
        return []

    # `[F07]` Nhung truong co the lam mot cau tra loi da duyet tro nen SAI.
    # Anh, tieu de listing, phu de va vi tri gio hang khong nam o day: doi
    # chung khong lam cau tra loi sai di, nen khong duoc vo hieu hoa mau.
    # Ban cu bam _json_hash tren CA ban ghi (ke ca image_url co nhung
    # image_hash), nen chi can tai mot anh san pham moi la moi mau cua san
    # pham do bi day sang trang thai "can xem lai".
    TRUONG_ANH_HUONG_CAU_TRA_LOI = (
        "profile_id", "original_price", "price", "sale_price", "promotion",
        "quy_cach_dong_goi", "gia_goc_duyet",
    )

    @classmethod
    def _phien_ban_du_kien(cls, product):
        return _json_hash({key: product.get(key)
                           for key in cls.TRUONG_ANH_HUONG_CAU_TRA_LOI
                           if key in product})

    @staticmethod
    def _chua_du_kien_san_pham(text):
        """Cau tra loi co chua con so gan don vi hoac gia khong.

        Mot mau chi day cach dien dat ("da nhan duoc don cua anh nhe") dung
        duoc cho moi san pham. Mot mau co "50g cho binh 20 lit" hay "555k"
        thi chi dung duoc trong pham vi san pham sinh ra no.
        """
        return bool(_DU_KIEN_SAN_PHAM.search(str(text or "")))

    # Chỉ băm NHỮNG GÌ không thể tự đổi.
    #
    # Bản cũ băm cả dict sau khi đã làm giàu, trong đó có: `conversation_status`
    # (tự nhảy waiting→unanswered sau 180 giây), `delivery_status`/`reply_text`
    # (AI cập nhật bất đồng bộ), `product_image_url` (chứa image_hash nên đổi
    # ảnh sản phẩm là đổi revision của mọi hội thoại), `approved_example.use_count`,
    # `similar_approved_examples`, và `previous_customer_comments` (trỏ sang bản
    # ghi hàng xóm). Hậu quả: gõ câu trả lời quá 3 phút là chắc chắn
    # `revision_conflict`, và lần lưu thứ hai thì luôn luôn hỏng.
    TRUONG_PHIEN_BAN_HOI_THOAI = (
        "id", "msg_id", "ts", "user_id", "unique_id", "original_text",
    )

    @classmethod
    def _phien_ban_hoi_thoai(cls, row, corrections, example=None):
        """Revision của một hội thoại: chỉ đổi khi CÓ NGƯỜI thật sự sửa nó."""
        return _json_hash({
            "goc": {key: row.get(key) for key in cls.TRUONG_PHIEN_BAN_HOI_THOAI},
            # Đổi khi và chỉ khi có thêm/gỡ một bản sửa — đây mới là tín hiệu
            # "người khác vừa sửa bản ghi này", thứ mà kiểm xung đột cần.
            "corrections": [str(c.get("correction_id")) for c in corrections],
            "example": [str((example or {}).get("example_id") or ""),
                        str((example or {}).get("status") or ""),
                        str((example or {}).get("updated_at") or "")],
        })

    def _mau_het_hieu_luc(self, rows, products):
        """example_id của các mẫu đã hết hiệu lực. THUẦN — không ghi gì cả."""
        het = set()
        for row in rows:
            if not row["product_id"]:
                continue
            current = products.get(row["product_id"])
            if not current or (row["product_revision"]
                               and row["product_revision"] != self._phien_ban_du_kien(current)):
                het.add(row["example_id"])
        return het

    def danh_dau_mau_can_xem_lai(self):
        """Đường GHI riêng: đẩy các mẫu hết hiệu lực sang ``needs_review``.

        Trước đây việc này nằm ngay trong ``reviewed_examples()`` — tức là
        trong đường ĐỌC. Hậu quả: ``detail()`` không idempotent, nên hai lần
        gọi liên tiếp có thể cho kết quả khác nhau và sinh ra
        ``revision_conflict`` ngay trong cùng một thao tác lưu. Tệ hơn, một
        lần bấm Lưu bị từ chối vẫn âm thầm tắt mẫu trả lời đã duyệt của AI.

        Mẫu hết hiệu lực vẫn **không bao giờ** được chọn để trả lời (đường đọc
        lọc thuần bằng ``_mau_het_hieu_luc``); hàm này chỉ cập nhật nhãn cho
        người vận hành thấy, nên nhãn có thể trễ một nhịp.
        """
        with self._db() as db:
            rows = [dict(r) for r in db.execute(
                "SELECT * FROM approved_reply_examples WHERE status='active' AND legacy_excluded=0").fetchall()]
        het = self._mau_het_hieu_luc(
            rows, {str(r.get("id")): r for r in self._products()})
        if het:
            now = _now()
            with self._db() as db:
                db.executemany(
                    "UPDATE approved_reply_examples SET status='needs_review',"
                    "updated_at=? WHERE example_id=?",
                    [(now, example_id) for example_id in sorted(het)])
        return sorted(het)

    def reviewed_examples(self, comment, question_type="", product_id="", limit=3, mark_used=False,
                          persona_id=""):
        wanted = set(self._fold(comment).split())
        with self._db() as db:
            rows = [dict(row) for row in db.execute(
                "SELECT * FROM approved_reply_examples WHERE status='active' AND legacy_excluded=0").fetchall()]
        products = {str(row.get("id")): row for row in self._products()}
        het_hieu_luc = self._mau_het_hieu_luc(rows, products)
        scored = []
        for row in rows:
            # `[F07]` Mau chua du kien rieng (lieu, gia, quy cach) chi duoc
            # dung cho dung san pham sinh ra no. Truoc day diem so
            # .55*lexical + .25 + .20 voi nguong .35 cho phep mot mau cua san
            # pham A duoc chon cho cau hoi ve san pham B chi vi trung tu ngu —
            # ke ca khi cau tra loi do ghi ro lieu dung cua A.
            if self._chua_du_kien_san_pham(row["approved_answer"]):
                if not row["product_id"] or row["product_id"] != product_id:
                    continue
            # Mẫu hết hiệu lực không bao giờ được dùng. Việc đổi nhãn sang
            # ``needs_review`` nằm ở đường GHI (``danh_dau_mau_can_xem_lai``),
            # không phải ở đây — đường đọc phải không có tác dụng phụ.
            if row["example_id"] in het_hieu_luc:
                continue
            words = set(row["normalized_question"].split())
            lexical = len(wanted & words) / max(1, len(wanted | words))
            score = .55 * lexical + (.25 if question_type and row["question_type"] == question_type else 0) + \
                    (.20 if product_id and row["product_id"] == product_id else 0)
            if score >= .35:
                # Cùng người trực thì giọng hợp hơn: ưu tiên nhẹ, không loại mẫu của người khác.
                if persona_id and row.get("persona_id") == persona_id:
                    score += .1
                scored.append((score, row))
        scored.sort(key=lambda pair: (pair[0], pair[1]["updated_at"]), reverse=True)
        chosen = scored[:max(0, min(3, int(limit)))]
        if mark_used and chosen:
            # Đây là đường GHI (AI vừa dùng mẫu để trả lời), nên nhân tiện
            # cập nhật nhãn cho các mẫu đã hết hiệu lực.
            try:
                self.danh_dau_mau_can_xem_lai()
            except Exception:
                pass        # không để việc đổi nhãn làm hỏng lượt trả lời
            with self._db() as db:
                for _, row in chosen:
                    db.execute("UPDATE approved_reply_examples SET use_count=use_count+1,last_used_at=? WHERE example_id=?",
                               (_now(), row["example_id"]))
        return [{"example_id": row["example_id"], "question": row["question"],
                 "approved_answer": row["approved_answer"], "question_type": row["question_type"],
                 "product_id": row["product_id"], "similarity": round(score, 3),
                 **({"ghi_chu_chu_shop": row["reason"]} if row.get("reason") else {})}
                for score, row in chosen]

    def learning_examples(self, limit=120):
        """Các bản chủ shop đã dạy, kèm câu AI đã nói lúc đó — đầu vào cho "Học lại toàn bộ"."""
        with self._db() as db:
            rows = [dict(r) for r in db.execute(
                "SELECT * FROM approved_reply_examples WHERE status IN ('active','needs_review') AND legacy_excluded=0 "
                "ORDER BY updated_at DESC LIMIT ?", (int(limit),)).fetchall()]
        try:
            conversations = {str(r.get("id")): r for r in self._derived_live_rows("conversations")}
        except Exception:
            conversations = {}
        out = []
        for row in rows:
            conv = conversations.get(row["conversation_id"]) or {}
            out.append({"cau_khach": row["question"], "cau_ai_da_noi": _safe_text(conv.get("reply_text"), 400),
                        "cau_chu_shop_sua": row["approved_answer"], "ghi_chu": row["reason"],
                        "loai": row["question_type"], "san_pham_id": row["product_id"],
                        "persona_id": row.get("persona_id") or "", "luc": row["updated_at"]})
        return out

    def approve_reply_example(self, record_id, payload):
        """Save a conversation correction and its approved AI example atomically."""
        detail = self.detail("conversations", record_id)
        row = detail["data"]
        expected = _safe_text(payload.get("expected_revision"), 100)
        if expected and expected != detail["revision"]:
            raise ValueError("revision_conflict")
        changes = payload.get("changes")
        if changes is None:
            # Compatibility with a pre-module operator page.  The new page
            # always sends a form snapshot under `changes`.
            changes = {key: payload[key] for key in (
                "corrected_text", "question_type", "selected_product_id",
                "response_quality", "correct_answer", "operator_note",
                "conversation_status",
            ) if key in payload}
        if not isinstance(changes, dict):
            raise ValueError("workspace_changes_required")
        allowed = {"corrected_text", "question_type", "selected_product_id",
                   "response_quality", "correct_answer", "operator_note",
                   "conversation_status"}
        invalid = sorted(set(changes) - allowed)
        if invalid:
            raise ValueError("conversation_fields_not_editable:" + ",".join(invalid))
        clean_changes = {key: _safe_text(value, 2000 if key in {"corrected_text", "correct_answer", "operator_note"} else 200)
                         for key, value in changes.items()}
        effective = row.get("effective") or row
        answer = _safe_text(payload.get("approved_answer") or clean_changes.get("correct_answer")
                            or effective.get("correct_answer"), 2000)
        reason = _safe_text(payload.get("reason"), 500)
        question_type = _safe_text(payload.get("question_type") or clean_changes.get("question_type")
                                   or effective.get("question_type"), 100)
        product_id = _safe_text(payload.get("product_id") or clean_changes.get("selected_product_id")
                                or effective.get("selected_product_id"), 160)
        if not reason:
            # Ghi chú giờ là "điều AI cần hiểu" — không bắt buộc nữa.
            reason = "Chủ shop dạy lại trong Hộp thư"
        if not answer or not question_type:
            raise ValueError("approved_answer_question_type_reason_required")
        persona_id = _safe_text(payload.get("persona_id"), 80)
        product_revision = ""
        if product_id:
            product = next((item for item in self._products() if item["id"] == product_id), None)
            if product is None:
                raise ValueError("approved_example_product_not_found")
            product_revision = self._phien_ban_du_kien(product)
        previous = self._example_for(record_id) or {}
        example_id = previous.get("example_id") or uuid.uuid4().hex
        now = _now()
        question = _safe_text(clean_changes.get("corrected_text")
                              or effective.get("corrected_text")
                              or row.get("original_text"), 2000)
        if not question:
            question = _safe_text(row.get("original_text"), 2000)
        correction_id = uuid.uuid4().hex
        after = {**effective, **clean_changes}
        with self._db() as db:
            snapshot_id = self._snapshot("conversations", record_id, row, reason, db)
            if clean_changes:
                db.execute("INSERT INTO corrections (correction_id,resource,record_id,created_at,reason,"
                           "changes_json,active,customer_key_hmac) VALUES (?,?,?,?,?,?,1,?)",
                           (correction_id, "conversations", record_id, now, reason,
                            json.dumps(clean_changes, ensure_ascii=False), self._row_hmac(row)))
            db.execute(
                "INSERT INTO approved_reply_examples "
                "(example_id,conversation_id,created_at,updated_at,question,normalized_question,"
                "question_type,product_id,product_revision,approved_answer,reason,status,use_count,last_used_at,persona_id,"
                "customer_key_hmac) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,'active',?,?,?,?) "
                "ON CONFLICT(conversation_id) DO UPDATE SET updated_at=excluded.updated_at,"
                "question=excluded.question,normalized_question=excluded.normalized_question,"
                "question_type=excluded.question_type,product_id=excluded.product_id,"
                "product_revision=excluded.product_revision,approved_answer=excluded.approved_answer,"
                "reason=excluded.reason,status='active',persona_id=excluded.persona_id,"
                "customer_key_hmac=excluded.customer_key_hmac,legacy_excluded=0",
                (example_id, record_id, previous.get("created_at") or now, now,
                 question, self._fold(question),
                 question_type, product_id, product_revision, answer, reason,
                 int(previous.get("use_count") or 0), previous.get("last_used_at"), persona_id,
                 self._row_hmac(row)),
            )
            self._record_change("conversations", record_id, effective, after, reason,
                                action="approve_example", db=db)
        self._touch("conversations", "dashboard")
        updated = self.detail("conversations", record_id)
        # Rút kiến thức chạy NỀN: bấm "Dạy AI" trả về ngay, gợi ý hiện sau vài giây.
        run_id = ""
        learner = getattr(self.engine, "learner", None)
        if learner is not None:
            try:
                run_id = learner.submit_distill({
                    "conversation_id": record_id,
                    "question": _safe_text(row.get("original_text"), 600),
                    "corrected_question": _safe_text(clean_changes.get("corrected_text")
                                                     or effective.get("corrected_text"), 600),
                    "ai_reply": _safe_text(effective.get("reply_text") or row.get("reply_text"), 600),
                    "answer": answer,
                    "note": "" if reason == "Chủ shop dạy lại trong Hộp thư" else reason,
                    "question_type": question_type, "product_id": product_id, "persona_id": persona_id,
                    "customer_key_hmac": self._row_hmac(row),
                })
            except Exception:
                run_id = ""
        return {"ok": True, "example_id": example_id, "learning_status": "active", "learning_run_id": run_id,
                "correction_id": correction_id if clean_changes else "",
                "snapshot_id": snapshot_id, "revision": updated["revision"],
                "data": updated["data"], "message": "Đã lưu bản sửa và duyệt làm ví dụ cho AI"}

    def disable_reply_example(self, record_id, payload=None):
        previous = self._example_for(record_id)
        if not previous:
            raise ValueError("approved_example_not_found")
        reason = _safe_text((payload or {}).get("reason") or "Ngừng dùng ví dụ", 500)
        with self._db() as db:
            db.execute("UPDATE approved_reply_examples SET status='disabled',updated_at=? WHERE conversation_id=?",
                       (_now(), record_id))
        self._record_change("conversations", record_id, previous,
                            {**previous, "status": "disabled"}, reason, action="disable_example")
        return {"ok": True, "learning_status": "disabled", "message": "AI sẽ không dùng ví dụ này nữa"}

    def video_job_detail(self, job_id):
        state = self.engine.video_analysis_status(job_id, include_draft=True)
        folder = Path(self.engine.video_analysis.root) / job_id
        transcript = {"segments": []}
        try:
            transcript = json.loads((folder / "transcript.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
        draft = state.get("draft") if isinstance(state.get("draft"), dict) else {}
        events = draft.get("events") if isinstance(draft.get("events"), list) else []
        for event in events:
            event.setdefault("review_status", "pending")
            event.setdefault("context_before", "")
            event.setdefault("context_after", "")
            event.setdefault("alternate_product_id", "")
            event.setdefault("alternate_every_ms", 0)
        draft["events"] = events
        state["draft"] = draft
        state["transcript"] = transcript
        state["revision"] = _json_hash(draft)
        return state

    def save_video_job_draft(self, job_id, payload):
        current = self.video_job_detail(job_id)
        if current.get("state") != "review":
            raise ValueError("job_not_ready_for_review")
        expected = _safe_text(payload.get("expected_revision"), 100)
        if expected and expected != current["revision"]:
            raise ValueError("revision_conflict")
        draft = payload.get("draft")
        if not isinstance(draft, dict) or not isinstance(draft.get("events"), list):
            raise ValueError("video_draft_invalid")
        duration = int(draft.get("cycle_ms") or current.get("duration_s", 0) * 1000)
        cleaned = []
        for raw in draft["events"]:
            at_ms = int(raw.get("at_ms", -1))
            if at_ms < 0 or (duration and at_ms >= duration):
                raise ValueError("event_time_out_of_range")
            status = str(raw.get("review_status") or "pending")
            if status not in {"pending", "approved", "edited", "rejected"}:
                raise ValueError("review_status_invalid")
            event = {**raw, "at_ms": at_ms, "review_status": status}
            # detail() bổ sung các trường mặc định trước khi băm revision.
            # Ghi cùng dạng chuẩn để revision trả về dùng được cho lần lưu sau.
            for field, default in (("context_before", ""), ("context_after", ""),
                                   ("alternate_product_id", ""), ("alternate_every_ms", 0)):
                event.setdefault(field, default)
            cleaned.append(event)
        if len({x["at_ms"] for x in cleaned}) != len(cleaned):
            raise ValueError("duplicate_event_time")
        draft = {**draft, "events": sorted(cleaned, key=lambda x: x["at_ms"]),
                 "saved_at": _now()}
        self._snapshot("video-jobs", job_id, current.get("draft") or {},
                       _safe_text(payload.get("reason") or "Lưu bản duyệt video", 500))
        draft_path = Path(self.engine.video_analysis.root) / job_id / "draft.json"
        job_op, job_prev = self._begin_file_first("video_job_draft", draft_path, draft, "video-jobs", job_id)
        try:
            self._record_change("video-jobs", job_id, current.get("draft") or {}, draft,
                                _safe_text(payload.get("reason") or "Lưu bản duyệt video", 500))
        except Exception:
            self._abort_file_first(job_op, draft_path, job_prev)
            raise
        self._commit_file_first(job_op, job_prev)
        return {"ok": True, "saved": True,
                "revision": self.video_job_detail(job_id)["revision"], "draft": draft}

    def save_product_image(self, product_id, payload):
        if not any(row["id"] == product_id for row in self._products()):
            raise ValueError("product_not_found")
        encoded = str(payload.get("data_url") or "")
        match = re.fullmatch(r"data:image/(png|jpeg|webp);base64,(.+)", encoded, re.I | re.S)
        if not match:
            raise ValueError("image_format_not_supported")
        try:
            raw = base64.b64decode(match.group(2), validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("image_invalid") from exc
        if not raw or len(raw) > 10 * 1024 * 1024:
            raise ValueError("image_size_invalid")
        try:
            with Image.open(io.BytesIO(raw)) as probe:
                if probe.format not in {"PNG", "JPEG", "WEBP"}:
                    raise ValueError("image_content_invalid")
                probe.verify()
            with Image.open(io.BytesIO(raw)) as source:
                normalized = ImageOps.exif_transpose(source).convert("RGB")
                normalized.thumbnail((800, 800), Image.Resampling.LANCZOS)
                output = io.BytesIO()
                normalized.save(output, "WEBP", quality=88, method=6)
                raw = output.getvalue()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise ValueError("image_content_invalid") from exc
        digest = hashlib.sha256(raw).hexdigest()
        folder = self.root / "data" / "product_images"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{re.sub(r'[^A-Za-z0-9_-]', '_', product_id)}.webp"
        with self._db() as db:
            old = db.execute("SELECT * FROM product_images WHERE product_id=?", (product_id,)).fetchone()
        before = dict(old) if old else {}
        snapshot_id = self._snapshot("product-images", product_id, before,
                                     "Trước khi thay ảnh sản phẩm")
        if old and Path(old["image_path"]).is_file():
            archive = self.root / "data" / "snapshots" / "product_images" / f"{snapshot_id}.webp"
            archive.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(old["image_path"], archive)
            before["snapshot_image_path"] = str(archive)
        guard = self._begin_guard("product_image", path, "product-images", product_id, payload=raw)
        try:
            after = {"product_id": product_id, "updated_at": _now(), "image_hash": digest,
                     "image_path": str(path)}
            # product_images row + operator_changes row commit atomically so the
            # journal's change-count marker also covers the product_images row.
            with self._db() as db:
                db.execute("INSERT INTO product_images VALUES (?,?,?,?) ON CONFLICT(product_id) DO UPDATE SET "
                           "updated_at=excluded.updated_at,image_hash=excluded.image_hash,image_path=excluded.image_path",
                           (product_id, after["updated_at"], digest, str(path)))
                self._record_change("product-images", product_id, before, after,
                                    "Thay ảnh sản phẩm", db=db)
        except Exception:
            self._abort_guard(guard)
            raise
        self._commit_guard(guard)
        self._touch("product-images", "dashboard")
        return {"ok": True, "snapshot_id": snapshot_id, "undo_available": True,
                "image_url": f"/api/product/image/{product_id}?v={digest[:12]}"}

    def product_image_path(self, product_id):
        with self._db() as db:
            row = db.execute("SELECT image_path FROM product_images WHERE product_id=?", (product_id,)).fetchone()
        if not row:
            raise ValueError("product_image_not_found")
        path = Path(row["image_path"])
        allowed = (self.root / "data" / "product_images").resolve()
        if not path.is_file() or path.resolve().parent != allowed:
            raise ValueError("product_image_not_found")
        return path

    def delete_product_image(self, product_id):
        with self._db() as db:
            row = db.execute("SELECT * FROM product_images WHERE product_id=?", (product_id,)).fetchone()
        if not row:
            raise ValueError("product_image_not_found")
        before = dict(row)
        snapshot_id = self._snapshot("product-images", product_id, before,
                                     "Trước khi xóa ảnh thay thế")
        path = Path(row["image_path"])
        archive = self.root / "data" / "trash" / "product_images" / snapshot_id
        archive.parent.mkdir(parents=True, exist_ok=True)
        guard = None
        try:
            if path.is_file():
                guard = self._begin_move_guard("product_image_trash", path, archive,
                                               "product-images", product_id)
                path.replace(archive)
            with self._db() as db:
                db.execute("DELETE FROM product_images WHERE product_id=?", (product_id,))
                self._record_change("product-images", product_id, before, {},
                                    "Xóa ảnh thay thế", action="trash", db=db)
        except Exception:
            if guard is not None:
                self._abort_move_guard(guard)
            raise
        if guard is not None:
            self._commit_move_guard(guard)
        self._touch("product-images", "dashboard")
        return {"ok": True, "snapshot_id": snapshot_id, "undo_available": True,
                "message": "Đã quay về ảnh TikTok"}

    def _memory_rows(self, resource):
        if not MEMORY_DB.exists():
            return []
        table = {"sessions": "sessions", "interactions": "interactions",
                 "customers": "customers", "facts": "customer_facts"}[resource]
        identity = {"sessions": "session_id", "interactions": "interaction_id",
                    "customers": "customer_key", "facts": "customer_key || ':' || fact_key"}[resource]
        with sqlite3.connect(str(MEMORY_DB), timeout=5) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(f"SELECT *, {identity} AS _id FROM {table} LIMIT 5000").fetchall()
        result = []
        batch = self._corrections_theo_lo(resource)   # 1 truy van, khong mo ket noi moi cho tung dong
        for raw in rows:
            item = dict(raw)
            item["id"] = item.pop("_id")
            corrections = batch.get((resource, str(item["id"])), [])
            item["corrections"] = corrections
            effective = copy.deepcopy(item) if corrections else dict(item)
            for correction in corrections:
                effective.update(correction["changes"])
            item["effective"] = effective
            result.append(item)
        result.sort(key=lambda item: str(item.get("happened_at") or item.get("started_at") or item.get("last_seen") or ""), reverse=True)
        return result

    def _backups(self):
        rows = []
        for path in sorted(BACKUP_DIR.glob("*/manifest.json"), reverse=True):
            if path.parent.name.startswith('.'):
                continue
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            from backup_policy import part_of
            files = manifest.get("files", [])
            parts = manifest.get("parts") or sorted({part_of(f.get("path")) for f in files})
            rows.append({"id": path.parent.name, "title": path.parent.name,
                         "created_at": manifest.get("created_at"), "files": files,
                         "scope": manifest.get("scope", "full"), "parts": parts,
                         "reason": manifest.get("reason", ""),
                         "bytes": sum(f.get("bytes", 0) for f in files),
                         "revision": _json_hash(manifest)})
        return rows

    def list(self, resource, query=None):
        query = query or {}
        items = self._rows_for(resource, query)
        trashed = self._trashed_ids(resource)
        items = [item for item in items if str(item.get("id")) not in trashed]
        filters = {
            "status": "conversation_status", "responder": "responder",
            "session_id": "session_id", "question_type": "question_type",
            "product_id": "selected_product_id", "room_id": "room_id",
            "state": "state",
        }
        for key, field in filters.items():
            wanted = _safe_text(query.get(key), 160)
            if wanted:
                items = [row for row in items if str((row.get("effective") or row).get(field, row.get(field, ""))) == wanted]
        page = self._paginate(items, query)
        if query.get("view") == "summary":
            # Danh sách chỉ cần bản HIỆU LỰC của mỗi dòng. Bản đầy đủ gửi cả bản
            # gốc lẫn `effective` (trùng ~toàn bộ trường) và 5 bình luận trước
            # đó: 380 bình luận = 1,5 MB JSON cho một lần mở Hộp thư.
            heavy = {"effective", "previous_customer_comments", "corrections", "conversations", "conversation_ids"}
            examples = self._examples_theo_lo() if resource == "conversations" else {}
            page["items"] = [{**{k: v for k, v in (row.get("effective") or row).items() if k not in heavy},
                              "id": row.get("id"), "revision": row.get("revision"),
                              "has_corrections": bool(row.get("corrections")),
                              # Đã dạy AI từ bình luận này → Hộp thư đưa ra khỏi hàng chờ.
                              **({"taught": (examples.get(str(row.get("id"))) or {}).get("status") == "active"}
                                 if resource == "conversations" else {})}
                             for row in page["items"]]
        return {"ok": True, "resource": resource, **page}

    def _find_row(self, resource, record_id):
        """Tra thẳng một bản ghi theo id.

        Bản cũ đi vòng qua ``list()`` với bộ lọc ``q=record_id`` và một vòng
        ``while`` phân trang. Mà ``_paginate`` lọc ``q`` bằng
        ``json.dumps(item)`` trên TOÀN BỘ bản ghi — đo được 218 MB JSON được
        sinh ra cho đúng một lần bấm xem chi tiết.
        """
        if str(record_id) in self._trashed_ids(resource):
            raise ValueError("workspace_record_not_found")
        for row in self._rows_for(resource, {}):
            if str(row.get("id")) == str(record_id):
                return copy.deepcopy(row)   # detail() làm giàu tại chỗ
        raise ValueError("workspace_record_not_found")

    def _rows_for(self, resource, query):
        """Nguồn dữ liệu thô của một resource. Dùng chung cho list() và detail()."""
        if resource == "products": items = self._products()
        elif resource == "profiles": items = self._profiles()
        elif resource == "videos": items = self._videos()
        elif resource == "video-jobs": items = [{"id": row.get("job_id"), **row} for row in self.engine.video_analysis_jobs().get("jobs", [])]
        elif resource in {"raw-events", "conversations"}: items = self._raw_events()
        elif resource in {"sessions", "interactions", "customers", "facts"}:
            items = self._derived_live_rows(resource) or self._memory_rows(resource)
        elif resource == "plugins": items = [{"id": row.get("id"), **row} for row in self.engine.plugin_state().get("plugins", [])]
        elif resource == "audio-devices": items = [{"id": row.get("id"), **row} for row in self.engine.settings_state().get("audio_devices", [])]
        elif resource == "logs":
            items = []
            for row in recent_logs(LOG_DIR, severity=query.get("severity", ""), limit=500).get("rows", []):
                stable = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                items.append({"id": hashlib.sha256(stable.encode()).hexdigest()[:24], **row})
        elif resource == "backups": items = self._backups()
        elif resource == "diagnostics": items = [{"id": row.get("id"), **row} for row in self.engine.diagnostics_state().get("checks", [])]
        else: raise ValueError("workspace_resource_not_found")
        return items

    def totals(self):
        """Tong hop tren toan bo tap du lieu, doc lap voi phan trang.

        Man hinh tong quan truoc day goi list(..., limit=100) roi dem so phan
        tu tra ve, nen moi con so deu tran o 100 khi du lieu lon hon. Cac phep
        dem o day chay tren tap da loai thung rac, giong het bo loc cua list().
        """
        cached = self._memo_get(("totals",))   # cung chu ky + TTL 15s voi cac ban dung khac
        if cached:
            return dict(cached[0])
        signature = self._input_signature()
        result = self._totals_uncached()
        self._memo_put(("totals",), [result], signature)
        return dict(result)

    def _totals_uncached(self):
        conversations = self._live_rows("conversations")
        sessions = self._live_rows("sessions")
        return {
            "conversations": len(conversations),
            "sessions": len(sessions),
            "ai_replied": sum(row.get("responder") == "ai" for row in conversations),
            "manual_replied": sum(row.get("responder") == "manual" for row in conversations),
            "unanswered": sum(
                row.get("conversation_status") in {"waiting", "unanswered"}
                for row in conversations),
            **self._work_queue(conversations),
        }

    def _work_queue(self, conversations):
        """Việc còn phải làm trong Hộp thư — CÙNG định nghĩa với bộ lọc của trang.

        Đã dạy AI hoặc đã đánh giá thì coi như xử lý xong, không còn đếm.
        """
        try:
            taught = {cid for cid, ex in self._examples_theo_lo().items() if ex.get("status") == "active"}
        except (AttributeError, sqlite3.Error):
            taught = set()      # kho dựng tối giản (kiểm thử) không có CSDL mẫu
        todo = review = 0
        for row in conversations:
            x = row.get("effective") or row
            done = str(row.get("id")) in taught or (x.get("response_quality") or "unreviewed") != "unreviewed"
            if done:
                continue
            status = x.get("conversation_status")
            if status in {"unanswered", "waiting", "ai_failed"}:
                todo += 1
            elif status in {"ai_sent", "ai_unconfirmed"}:
                review += 1
        return {"todo": todo, "review": review}

    def _live_rows(self, resource):
        """Cac dong cua mot resource sau khi bo thung rac (khong phan trang)."""
        if resource in {"raw-events", "conversations"}:
            items = self._raw_events()
        elif resource in {"sessions", "interactions", "customers", "facts"}:
            items = self._derived_live_rows(resource) or self._memory_rows(resource)
        else:
            raise ValueError("workspace_resource_not_found")
        trashed = self._trashed_ids(resource)
        return [item for item in items if str(item.get("id")) not in trashed]

    def detail(self, resource, record_id):
        if resource == "video-jobs":
            item = self.video_job_detail(record_id)
            return {"ok": True, "resource": resource, "id": record_id, "data": item,
                    "revision": item["revision"], "editable": item.get("state") == "review",
                    "fields": {}, "history": self._history(resource, record_id),
                    "corrections": [], "source": "Video đã tải lên máy"}
        item = self._find_row(resource, record_id)
        # Chốt revision TRƯỚC mọi bước làm giàu bên dưới. Bản cũ băm dict sau
        # khi đã nhét thêm approved_example, similar_approved_examples,
        # context, review… nên revision đổi theo cả những thứ người dùng không
        # hề sửa.
        revision = item.get("revision") or _json_hash(item)
        if resource == "videos":
            transcript = VIDEO_DIR / record_id / "transcript.json"
            if transcript.exists():
                try: item["transcript"] = json.loads(transcript.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError): item["transcript"] = {"segments": []}
            with self._db() as db:
                draft = db.execute(
                    "SELECT saved_at,data_json FROM video_drafts WHERE video_id=?",
                    (record_id,),
                ).fetchone()
                draft_version = db.execute('SELECT revision FROM video_draft_revisions WHERE video_id=?', (record_id,)).fetchone()
                item['draft_revision'] = draft_version[0] if draft_version else 0
            if draft:
                try:
                    item["saved_draft"] = json.loads(draft["data_json"])
                    item["draft_saved_at"] = draft["saved_at"]
                except json.JSONDecodeError:
                    item["saved_draft"] = None
        if resource == "sessions":
            ids = set(item.get("conversation_ids") or [])
            item["conversations"] = [row for row in self._raw_events() if row["id"] in ids]
        if resource in {"conversations", "raw-events"}:
            item["approved_example"] = self._example_for(record_id)
            item["similar_approved_examples"] = self.reviewed_examples(
                item.get("original_text", ""), item.get("question_type", ""),
                item.get("selected_product_id", ""), mark_used=False)
            item["original"] = {"customer": item.get("nickname") or item.get("unique_id"),
                                "text": item.get("original_text"), "at": item.get("ts"),
                                "session_id": item.get("session_id")}
            item["context"] = {"previous_comments": item.get("previous_customer_comments", []),
                               "video_id": item.get("video_id", ""),
                               "video_position_s": item.get("video_position_s"),
                               "pinned_product_id": item.get("pin_id", ""),
                               "selected_product_id": item.get("selected_product_id", "")}
            item["processing"] = {"action": item.get("ai_action", ""),
                                  "question_type": item.get("question_type", ""),
                                  "missing_or_block_reason": item.get("no_reply_reason", "")}
            item["reply"] = {"text": item.get("reply_text", ""),
                             "responder": item.get("responder", "none"),
                             "at": item.get("reply_at", ""),
                             "delivery_status": item.get("delivery_status", ""),
                             "must_not_reply_again": item.get("must_not_reply_again", False)}
            item["review"] = {key: (item.get("effective") or item).get(key) for key in (
                "corrected_text", "question_type", "selected_product_id", "response_quality",
                "correct_answer", "operator_note", "conversation_status", "approved_for_ai")}
        editable = resource in {"profiles", "videos", "conversations"}
        return {"ok": True, "resource": resource, "id": record_id, "data": item,
                "revision": revision, "editable": editable,
                "fields": self.PROFILE_FIELDS if resource == "profiles" else {},
                "history": self._history(resource, record_id),
                "corrections": self._corrections(resource, record_id),
                "source": self._source_label(resource, item)}

    @staticmethod
    def _source_label(resource, item):
        if resource in {"raw-events", "conversations"}: return f"{item.get('source_file')} · dòng {item.get('source_line')}"
        if resource in {"sessions", "interactions", "customers", "facts"}: return "Phân tích từ bình luận LIVE đã thu"
        if resource == "profiles": return "shared knowledge + local override"
        if resource == "videos": return f"data/videos/{item.get('id')}"
        return resource

    @_serialized_write
    def patch(self, resource, record_id, payload):
        if resource not in self.RESOURCE_PATCH_DUOC:
            raise ValueError("workspace_resource_read_only")
        expected = _safe_text(payload.get("expected_revision"), 100)
        changes = payload.get("changes")
        reason = _safe_text(payload.get("reason") or "Chỉnh sửa từ Trung tâm điều khiển", 500)
        if not isinstance(changes, dict):
            raise ValueError("workspace_changes_required")
        current = self.detail(resource, record_id)
        if expected and expected != current["revision"]:
            raise ValueError("revision_conflict")
        before = current["data"]
        snapshot_id = self._snapshot(resource, record_id, before, reason)
        guard = (self._begin_guard("profile_override", self.override_path, resource, record_id)
                 if resource == "profiles" else None)
        try:
            return self._patch_apply(resource, record_id, before, changes, reason, snapshot_id, guard)
        except Exception:
            if guard is not None:
                self._abort_guard(guard)
            raise

    def _patch_apply(self, resource, record_id, before, changes, reason, snapshot_id, guard):
        if resource == "profiles":
            after = self._patch_profile(record_id, before, changes)
        elif resource == "videos":
            after = self._patch_video(record_id, before, changes)
        elif resource == "conversations":
            allowed = {"corrected_text", "question_type", "selected_product_id",
                       "response_quality", "correct_answer", "operator_note",
                       "conversation_status"}
            invalid = sorted(set(changes) - allowed)
            if invalid:
                raise ValueError("conversation_fields_not_editable:" + ",".join(invalid))
            return {**self.correct({"resource": "conversations",
                                    "record_id": record_id,
                                    "changes": changes, "reason": reason}),
                    "resource": resource, "id": record_id}
        else:
            raise ValueError("workspace_resource_read_only")
        self._record_change(resource, record_id, before, after, reason)
        if guard is not None:
            self._commit_guard(guard)
        # Trả revision theo ĐÚNG công thức của detail(); hash của bản ghi đầy
        # đủ (``_record_change``) khác revision mà lần lưu kế tiếp phải gửi lên,
        # nên lưu liên tiếp hai lần trên cùng màn hình luôn báo xung đột.
        revision = self._revision_hien_tai(resource, record_id)
        return {"ok": True, "resource": resource, "id": record_id,
                "revision": revision, "data": after, "applied": True,
                "snapshot_id": snapshot_id, "undo_available": True}

    def _read_overrides(self):
        raw = {"schema_version": 1, "updated_at": _now(), "overrides": {}}
        if self.override_path.exists():
            try: raw = json.loads(self.override_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError): pass
        if not isinstance(raw, dict):
            raw = {"schema_version": 1, "overrides": {}}
        if not isinstance(raw.get("overrides"), dict):
            raw["overrides"] = {}
        return raw

    @_serialized_write
    def create_profile(self, payload):
        """Tạo hồ sơ NHÁP trên máy này.

        Hồ sơ mới chỉ có tên nên KHÔNG được duyệt và không được AI dùng; có
        ``product_id`` thì chỉ ghi nhớ listing nguồn, việc gắn/duyệt làm sau
        khi hồ sơ đủ nội dung (xem ``approve_profiles``).
        """
        name = _safe_text(payload.get("ten_san_pham"), 300)
        if len(name) < 2:
            raise ValueError("profile_name_required")
        record_id = "LOCAL-" + uuid.uuid4().hex[:10].upper()
        product_id = _safe_text(payload.get("product_id"), 160)
        raw = self._read_overrides()
        raw["overrides"][record_id] = {
            "ten_san_pham": name, "created_local": True, "approved_for_live": False,
            "updated_at": _now(), "updated_by": "local_operator",
            **({"draft_for_product_id": product_id} if product_id else {})}
        raw.update({"schema_version": 1, "updated_at": _now()})
        _atomic_json(self.override_path, raw)
        self.engine.knowledge.reload()
        self._touch("profiles", "products", "dashboard")
        return {"ok": True, "id": record_id, "status": "draft", "mapped": False}

    BULK_DRAFT_MAX = 200

    @_serialized_write
    def bulk_draft(self, payload):
        """Tạo hồ sơ NHÁP cho nhiều listing chưa có hồ sơ (không gắn, không duyệt)."""
        import listing_match
        ids = payload.get("product_ids") if isinstance(payload, dict) else None
        if not isinstance(ids, list) or not ids:
            raise ValueError("product_ids_required")
        ids = list(dict.fromkeys(_safe_text(item, 160) for item in ids if _safe_text(item, 160)))
        if len(ids) > self.BULK_DRAFT_MAX:
            raise ValueError("too_many_products")
        state = self.engine.mapping_state()
        listings = {row["product_id"]: row for row in state.get("products", [])}
        raw = self._read_overrides()
        overrides = raw["overrides"]
        existing = {_safe_text(v.get("draft_for_product_id"), 160): rid for rid, v in overrides.items()
                    if isinstance(v, dict) and v.get("created_local") is True
                    and v.get("draft_for_product_id")}
        created, skipped = [], []
        for product_id in ids:
            listing = listings.get(product_id)
            if not listing:
                skipped.append({"product_id": product_id, "reason": "not_in_catalog"})
            elif listing.get("profile_id"):
                skipped.append({"product_id": product_id, "reason": "already_mapped"})
            elif product_id in existing:
                skipped.append({"product_id": product_id, "reason": "draft_exists",
                                "profile_id": existing[product_id]})
            else:
                title = _safe_text(listing.get("title"), 400)
                record_id = "LOCAL-" + uuid.uuid4().hex[:10].upper()
                sizes = listing_match.describe_sizes(listing_match.parse_sizes(title))
                entry = {"ten_san_pham": listing_match.short_name(title) or title[:80],
                         "listing_aliases": [title], "created_local": True,
                         "approved_for_live": False, "draft_for_product_id": product_id,
                         "updated_at": _now(), "updated_by": "local_operator"}
                if sizes:
                    entry["quy_cach_dong_goi"] = sizes
                overrides[record_id] = entry
                created.append({"product_id": product_id, "profile_id": record_id,
                                "name": entry["ten_san_pham"]})
        if created:
            raw.update({"schema_version": 1, "updated_at": _now()})
            _atomic_json(self.override_path, raw)
            self.engine.knowledge.reload()
            self._touch("profiles", "products", "dashboard")
        return {"ok": True, "created": created, "skipped": skipped}

    def _patch_profile(self, record_id, before, changes):
        invalid = sorted(set(changes) - set(self.PROFILE_FIELDS))
        if invalid:
            raise ValueError("profile_fields_not_editable:" + ",".join(invalid))
        clean_changes = {}
        for key, value in changes.items():
            field_type = self.PROFILE_FIELDS[key]
            if field_type == "list":
                if isinstance(value, str): value = [line.strip() for line in value.splitlines() if line.strip()]
                if not isinstance(value, list): raise ValueError(f"profile_field_invalid:{key}")
                clean_changes[key] = [_safe_text(item, 1000) for item in value if _safe_text(item, 1000)][:50]
            elif field_type == "pairs":
                if not isinstance(value, dict):
                    raise ValueError(f"profile_field_invalid:{key}")
                clean_changes[key] = {
                    _safe_text(name, 160): _safe_text(amount, 500)
                    for name, amount in list(value.items())[:50]
                    if _safe_text(name, 160)
                }
            else:
                clean_changes[key] = _safe_text(value, 4000)
        raw = {"schema_version": 1, "updated_at": _now(), "overrides": {}}
        if self.override_path.exists():
            try: raw = json.loads(self.override_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError): pass
        previous_raw = copy.deepcopy(raw)
        overrides = raw.get("overrides") if isinstance(raw.get("overrides"), dict) else {}
        overrides[record_id] = {**(overrides.get(record_id) or {}), **clean_changes,
                                "updated_at": _now(), "updated_by": "local_operator"}
        raw.update({"schema_version": 1, "updated_at": _now(), "overrides": overrides})
        _atomic_json(self.override_path, raw)
        try:
            self.engine.knowledge.reload()
        except Exception:
            _atomic_json(self.override_path, previous_raw)
            raise
        # Sửa hồ sơ là nơi giá / quy cách / profile_id thật sự đổi, nên đây
        # chính là lúc phải soát lại các mẫu trả lời đã duyệt. Không để việc
        # đổi nhãn làm hỏng lần lưu hồ sơ vừa thành công.
        try:
            self.danh_dau_mau_can_xem_lai()
        except Exception:
            pass
        return next(row for row in self._profiles() if row["id"] == record_id)

    def _patch_video(self, record_id, before, changes):
        allowed = {"title", "repin_every_ms", "events", "script_contexts", "sync_products"}
        invalid = sorted(set(changes) - allowed)
        if invalid: raise ValueError("video_fields_not_editable:" + ",".join(invalid))
        path = VIDEO_DIR / record_id / "timeline.json"
        timeline, errors = load_timeline(path)
        if errors: raise ValueError("invalid_existing_timeline:" + ";".join(errors))
        updated = copy.deepcopy(timeline)
        sync_products = bool(changes.get("sync_products"))
        for key, value in changes.items():
            if key != "sync_products":
                updated[key] = value
        # Studio được phép ghim sản phẩm CHƯA có trong timeline, miễn là nó có
        # trong danh mục đã quét. Mục ``products`` dựng bằng đúng công thức
        # của bước phát hành (timeline_product_entry), không nhận từ client.
        from video_analysis import timeline_product_entry
        catalog = {str(row.get("product_id")): row
                   for row in self.engine.mapping_state().get("products", [])}
        products = dict(updated.get("products") or {})
        wanted = set()
        for event in updated.get("events") or []:
            for key in (event.get("product"), event.get("alternate_with")):
                if key:
                    wanted.add(str(key))
        for key in sorted(wanted):
            product_id = key[4:] if key.startswith("sku_") else ""
            row = catalog.get(product_id)
            if key not in products and row:
                products[key] = timeline_product_entry({**row, "short_name": row.get("title", "")[:100]})
            elif key in products and row and sync_products:
                products[key] = timeline_product_entry({**row, "short_name": row.get("title", "")[:100]})
        updated["products"] = products
        errors = validate_timeline(updated)
        if errors: raise ValueError("invalid_timeline:" + ";".join(errors))
        tmp = path.with_suffix(path.suffix + ".journal.tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        op_id = self.journal.begin("video_timeline", [(path, tmp)], record_id,
                                   self._draft_revision_of(record_id))
        try:
            with self._db() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute('INSERT INTO video_draft_revisions(video_id,revision) VALUES (?,1) '
                           'ON CONFLICT(video_id) DO UPDATE SET revision=revision+1', (record_id,))
                version = db.execute('SELECT revision FROM video_draft_revisions WHERE video_id=?', (record_id,)).fetchone()[0]
                db.execute("DELETE FROM video_drafts WHERE video_id=?", (record_id,))
        except Exception:
            tmp.unlink(missing_ok=True)
            self.journal.done(op_id)
            raise
        self.journal.mark_db_committed(op_id)
        os.replace(tmp, path)
        self.journal.done(op_id)
        return {**next(row for row in self._videos() if row["id"] == record_id), 'draft_revision': version}

    @_serialized_write
    def save_video_draft(self, record_id, payload):
        current = self.detail("videos", record_id)
        expected = _safe_text(payload.get("expected_revision"), 100)
        if expected and expected != current["revision"]:
            raise ValueError("revision_conflict")
        discard = payload.get('discard') is True
        expected_draft = payload.get('expected_draft_revision')
        if expected_draft is not None and (isinstance(expected_draft, bool) or
                not isinstance(expected_draft, int) or expected_draft < 0):
            raise ValueError('draft_revision_invalid')
        draft = payload.get("draft")
        if not discard and not isinstance(draft, dict):
            raise ValueError("video_draft_required")
        if not discard and (not isinstance(draft.get("events"), list) or not isinstance(
            draft.get("script_contexts", []), list
        )):
            raise ValueError("video_draft_invalid")
        saved_at = _now()
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT revision FROM video_draft_revisions WHERE video_id=?', (record_id,)).fetchone()
            draft_revision = row[0] if row else 0
            if expected_draft is not None and expected_draft != draft_revision:
                raise ValueError('revision_conflict')
            draft_revision += 1
            db.execute('INSERT INTO video_draft_revisions(video_id,revision) VALUES (?,?) '
                       'ON CONFLICT(video_id) DO UPDATE SET revision=excluded.revision', (record_id, draft_revision))
            if discard:
                db.execute('DELETE FROM video_drafts WHERE video_id=?', (record_id,))
                return {'ok': True, 'discarded': True, 'draft_revision': draft_revision,
                        'message': 'Đã bỏ bản nháp'}
            db.execute(
                "INSERT INTO video_drafts(video_id,saved_at,data_json) VALUES (?,?,?) "
                "ON CONFLICT(video_id) DO UPDATE SET saved_at=excluded.saved_at,"
                "data_json=excluded.data_json",
                (record_id, saved_at, json.dumps(draft, ensure_ascii=False)),
            )
        return {"ok": True, "saved": True, "saved_at": saved_at, 'draft_revision': draft_revision,
                "message": "Đã lưu bản nháp"}

    def resolve_video_duplicate(self, record_id, payload):
        other_id = _safe_text(payload.get("other_id"), 160)
        action = _safe_text(payload.get("action"), 40)
        if action not in {"keep_both", "merge"} or not other_id or other_id == record_id:
            raise ValueError("duplicate_action_invalid")
        rows = {row["id"]: row for row in self._videos()}
        keep, other = rows.get(record_id), rows.get(other_id)
        if not keep or not other or other_id not in keep.get("duplicates", []):
            raise ValueError("videos_not_duplicates")
        pair_key = "|".join(sorted((record_id, other_id)))
        if action == "keep_both":
            with self._db() as db:
                db.execute(
                    "INSERT INTO duplicate_decisions VALUES (?,?,?,?,?) "
                    "ON CONFLICT(pair_key) DO UPDATE SET decision=excluded.decision,"
                    "decided_at=excluded.decided_at",
                    (pair_key, min(record_id, other_id), max(record_id, other_id),
                     action, _now()),
                )
            return {"ok": True, "decision": action,
                    "message": "Đã giữ riêng hai video"}

        position = self.engine.current_video_position()
        if position.get("video_id") == other_id and position.get("state") in {
            "watching", "detected", "running",
        }:
            raise ValueError("video_in_use_choose_replacement")
        trash_result = self.delete("videos", other_id, {
            "expected_revision": other.get("revision"),
            "reason": f"Gộp bản trùng vào {record_id}",
        })
        keep_path = VIDEO_DIR / record_id
        timeline_path = keep_path / "timeline.json"
        previous_timeline = json.loads(timeline_path.read_text(encoding="utf-8-sig"))
        copied_files = []
        copy_guards = []
        merge_op = merge_prev = None
        try:
            timeline = copy.deepcopy(keep.get("timeline") or {})
            other_timeline = other.get("timeline") or {}
            for key in ("source_file", "source_sha256"):
                if not timeline.get(key) and other_timeline.get(key):
                    timeline[key] = other_timeline[key]
            self._snapshot("videos", record_id, keep, "Trước khi gộp video trùng")
            merge_op, merge_prev = self._begin_file_first(
                "video_merge", keep_path / "timeline.json", timeline, "videos", record_id)
            for filename in ("transcript.json", "fingerprint.npz"):
                target = keep_path / filename
                if target.exists():
                    continue
                with self._db() as db:
                    trash_row = db.execute(
                        "SELECT archived_path FROM trash_items WHERE trash_id=?",
                        (trash_result["trash_id"],),
                    ).fetchone()
                source = Path(trash_row["archived_path"]) / filename if trash_row else None
                if source and source.is_file():
                    copy_guards.append(self._begin_guard("video_copy", target, "videos", record_id))
                    shutil.copy2(source, target)
                    copied_files.append(target)
            after = next(row for row in self._videos() if row["id"] == record_id)
            self._record_change("videos", record_id, keep, after,
                                f"Gộp thông tin từ {other_id}", action="merge")
            self._commit_file_first(merge_op, merge_prev)
            for copy_guard in copy_guards:
                self._commit_guard(copy_guard)
        except Exception:
            for copy_guard in copy_guards:
                self._abort_guard(copy_guard)
            if merge_op is not None:
                self._abort_file_first(merge_op, timeline_path, merge_prev)
            _atomic_json(timeline_path, previous_timeline)
            for copied in copied_files:
                copied.unlink(missing_ok=True)
            self.restore("videos", other_id)
            raise
        return {"ok": True, "decision": action, "removed_id": other_id,
                "trash_id": trash_result["trash_id"], "undo_available": True,
                "message": "Đã gộp thông tin và chuyển bản trùng vào Thùng rác"}

    def _revision_hien_tai(self, resource, record_id):
        """Revision SAU khi vừa ghi, để giao diện lưu tiếp mà không phải tải lại."""
        if resource in {"conversations", "raw-events"}:
            try:
                row = self._find_row(resource, record_id)
            except ValueError:
                return ""
            return row.get("revision", "")
        try:
            return self.detail(resource, record_id)["revision"]
        except ValueError:
            return ""

    def correct(self, payload):
        resource = _safe_text(payload.get("resource"), 80)
        record_id = _safe_text(payload.get("record_id"), 200)
        changes = payload.get("changes")
        reason = _safe_text(payload.get("reason"), 500)
        if resource not in self.RESOURCE_CORRECT_DUOC:
            raise ValueError("correction_resource_not_allowed")
        if not record_id or not isinstance(changes, dict) or not reason:
            raise ValueError("correction_fields_required")
        original = self.detail(resource, record_id)["data"]
        immutable = {"id", "revision", "source_file", "source_line", "corrections", "effective",
                     "original_text", "reply_text", "reply_at", "user_id", "sec_uid", "msg_id"}
        invalid = sorted(key for key in changes
                         if key in immutable or key not in original
                         or "secret" in key.casefold() or "api_key" in key.casefold())
        if invalid:
            raise ValueError("correction_fields_not_allowed:" + ",".join(invalid))
        snapshot_id = self._snapshot(resource, record_id, original,
                                     "Trước khi sửa dữ liệu lịch sử")
        correction_id = uuid.uuid4().hex
        with self._db() as db:
            db.execute("INSERT INTO corrections (correction_id,resource,record_id,created_at,reason,"
                       "changes_json,active,customer_key_hmac) VALUES (?,?,?,?,?,?,1,?)",
                       (correction_id, resource, record_id, _now(), reason,
                        json.dumps(changes, ensure_ascii=False),
                        self._row_hmac(original) if resource in self._CUSTOMER_RESOURCES else ""))
        self._record_change(resource, record_id, original, {**original, **changes},
                            reason, action="correction")
        updated = self.detail(resource, record_id)
        return {"ok": True, "correction_id": correction_id, "applied": True,
                "snapshot_id": snapshot_id, "undo_available": True,
                "revision": updated["revision"], "data": updated["data"]}

    def undo_correction(self, correction_id):
        with self._db() as db:
            row = db.execute(
                "SELECT resource,record_id FROM corrections WHERE correction_id=? AND active=1",
                (correction_id,),
            ).fetchone()
            if row is None:
                raise ValueError("correction_not_found")
            db.execute("UPDATE corrections SET active=0 WHERE correction_id=?",
                       (correction_id,))
        return {"ok": True, "undone": True, "resource": row["resource"],
                "record_id": row["record_id"], "message": "Đã hoàn tác bản sửa",
                "revision": self._revision_hien_tai(row["resource"], row["record_id"])}

    def delete(self, resource, record_id, payload=None):
        payload = payload or {}
        current = self.detail(resource, record_id)
        expected = _safe_text(payload.get("expected_revision"), 100)
        if expected and expected != current["revision"]:
            raise ValueError("revision_conflict")
        if resource == "videos":
            position = self.engine.current_video_position()
            if position.get("video_id") == record_id and position.get("state") in {
                "watching", "detected", "running",
            }:
                raise ValueError("video_in_use_choose_replacement")
        reason = _safe_text(payload.get("reason") or "Đưa vào Thùng rác", 500)
        snapshot_id = self._snapshot(resource, record_id, current["data"], reason)
        deleted = datetime.now(timezone.utc).astimezone()
        expires = deleted + timedelta(days=30)
        trash_id = uuid.uuid4().hex
        archived_path = None
        original_video_path = None
        move_guard = file_guard = None
        try:
            if resource == "videos":
                original_video_path = VIDEO_DIR / record_id
                if original_video_path.is_dir():
                    archive_root = self.root / "data" / "trash" / "videos"
                    archive_root.mkdir(parents=True, exist_ok=True)
                    archived_path = archive_root / trash_id
                    move_guard = self._begin_move_guard("video_trash", original_video_path,
                                                        archived_path, resource, record_id)
                    original_video_path.replace(archived_path)
            elif resource == "profiles":
                file_guard = self._begin_guard("profile_override", self.override_path, resource, record_id)
                self._set_profile_hidden(record_id, True)
            elif resource == "facts":
                fact = current["data"]
                self.engine.memory.delete_fact(fact.get("customer_key", ""), fact.get("fact_key", ""))
            self._delete_commit(resource, record_id, current, reason, snapshot_id, trash_id,
                                deleted, expires, archived_path)
        except Exception:
            if move_guard is not None:
                self._abort_move_guard(move_guard)
            if file_guard is not None:
                self._abort_guard(file_guard)
            raise
        if move_guard is not None:
            self._commit_move_guard(move_guard)
        if file_guard is not None:
            self._commit_guard(file_guard)
        self._touch(resource, "dashboard")
        return {"ok": True, "trash_id": trash_id, "expires_at": expires.isoformat(),
                "undo_available": True, "message": "Đã chuyển vào Thùng rác trong 30 ngày"}

    def _delete_commit(self, resource, record_id, current, reason, snapshot_id, trash_id,
                       deleted, expires, archived_path):
        """trash_items row + operator_changes row in ONE transaction (journal marker)."""
        with self._db() as db:
            existing = db.execute(
                "SELECT trash_id FROM trash_items WHERE resource=? AND record_id=? "
                "AND restored_at IS NULL AND purged_at IS NULL", (resource, record_id),
            ).fetchone()
            if existing:
                raise ValueError("already_in_trash")
            db.execute(
                "INSERT INTO trash_items "
                "(trash_id,resource,record_id,deleted_at,expires_at,label,data_json,"
                "snapshot_id,restored_at,purged_at,archived_path) "
                "VALUES (?,?,?,?,?,?,?,?,NULL,NULL,?)",
                (trash_id, resource, record_id, deleted.isoformat(timespec="seconds"),
                 expires.isoformat(timespec="seconds"), self._friendly_label(current["data"]),
                 json.dumps(current["data"], ensure_ascii=False), snapshot_id,
                 str(archived_path) if archived_path else None),
            )
            self._record_change(resource, record_id, current["data"], {}, reason,
                                action="trash", db=db)

    def trash(self, query=None):
        query = query or {}
        with self._db() as db:
            rows = db.execute(
                "SELECT trash_id AS id,resource,record_id,deleted_at,expires_at,label,"
                "snapshot_id,data_json FROM trash_items WHERE restored_at IS NULL "
                "AND purged_at IS NULL ORDER BY deleted_at DESC"
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            try:
                original = json.loads(item.pop("data_json", "{}"))
            except (TypeError, json.JSONDecodeError):
                original = {}
            if (not item.get("label") or item["label"] == item.get("record_id")) and original:
                item["label"] = self._friendly_label(original)
            if not item.get("label") or item["label"] == item.get("record_id"):
                item["label"] = {
                    "video-jobs": "Video đã phân tích",
                    "videos": "Kịch bản video",
                    "conversations": "Bình luận LIVE",
                }.get(item.get("resource"), "Nội dung đã xóa")
            items.append(item)
        return {"ok": True, "resource": "trash", **self._paginate(
            items, query)}

    def restore(self, resource, record_id):
        move_guard = file_guard = None
        try:
            with self._db() as db:
                row = db.execute(
                    "SELECT * FROM trash_items WHERE resource=? AND record_id=? "
                    "AND restored_at IS NULL AND purged_at IS NULL ORDER BY deleted_at DESC LIMIT 1",
                    (resource, record_id),
                ).fetchone()
                if row is None:
                    raise ValueError("trash_item_not_found")
                archived_path = Path(row["archived_path"]) if row["archived_path"] else None
                if resource == "videos" and archived_path:
                    target = VIDEO_DIR / record_id
                    if target.exists():
                        raise ValueError("restore_target_already_exists")
                    if not archived_path.is_dir():
                        raise ValueError("restore_source_missing")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    move_guard = self._begin_move_guard("video_restore", archived_path, target,
                                                        resource, record_id)
                    archived_path.replace(target)
                elif resource == "profiles":
                    file_guard = self._begin_guard("profile_override", self.override_path,
                                                   resource, record_id)
                    self._set_profile_hidden(record_id, False)
                restored_at = _now()
                db.execute("UPDATE trash_items SET restored_at=? WHERE trash_id=?",
                           (restored_at, row["trash_id"]))
                data = json.loads(row["data_json"])
                self._record_change(resource, record_id, {}, data, "Khôi phục từ Thùng rác",
                                    action="restore", db=db)
        except Exception:
            if move_guard is not None:
                self._abort_move_guard(move_guard)
            if file_guard is not None:
                self._abort_guard(file_guard)
            raise
        if move_guard is not None:
            self._commit_move_guard(move_guard)
        if file_guard is not None:
            self._commit_guard(file_guard)
        if resource == "facts" and data.get("customer_key") and data.get("fact_key"):
            self.engine.memory.set_fact(data["customer_key"], data["fact_key"], data.get("fact_value", ""),
                                        data.get("source_comment") or "Khôi phục từ Thùng rác")
        self._touch(resource, "dashboard")
        return {"ok": True, "restored": True, "resource": resource,
                "id": record_id, "message": "Đã khôi phục"}

    def purge_now(self, trash_ids=None, everything=False):
        """Xoá VĨNH VIỄN các mục chọn trong Thùng rác (hoặc cả thùng) — không chờ 30 ngày."""
        ids = [_safe_text(i, 40) for i in (trash_ids or []) if _safe_text(i, 40)]
        if not ids and not everything:
            raise ValueError("trash_ids_required")
        return self.purge_expired(only_ids=None if everything else ids, ignore_expiry=True)

    def purge_expired(self, only_ids=None, ignore_expiry=False):
        now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        with self._db() as db:
            rows = db.execute(
                "SELECT trash_id,snapshot_id,archived_path FROM trash_items WHERE restored_at IS NULL "
                "AND purged_at IS NULL AND expires_at<=?", ("9999" if ignore_expiry else now,),
            ).fetchall()
            if only_ids is not None:
                wanted = set(only_ids)
                rows = [row for row in rows if row["trash_id"] in wanted]
            eligible = []
            for row in rows:
                snapshot = db.execute(
                    "SELECT snapshot_id FROM operator_snapshots WHERE snapshot_id=?",
                    (row["snapshot_id"],),
                ).fetchone()
                if snapshot:
                    eligible.append(row["trash_id"])
            for trash_id in eligible:
                row = next(item for item in rows if item["trash_id"] == trash_id)
                archived = Path(row["archived_path"]) if row["archived_path"] else None
                if archived and archived.exists():
                    archive_root = (self.root / "data" / "trash" / "videos").resolve()
                    resolved = archived.resolve()
                    if archive_root not in resolved.parents:
                        continue
                    shutil.rmtree(resolved)
                db.execute("UPDATE trash_items SET purged_at=?,data_json='{}' WHERE trash_id=?",
                           (now, trash_id))
        if eligible:
            self._touch("dashboard")
        return {"ok": True, "purged": len(eligible)}

    def _set_profile_hidden(self, record_id, hidden):
        raw = {"schema_version": 1, "updated_at": _now(), "overrides": {}}
        if self.override_path.exists():
            try:
                raw = json.loads(self.override_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        previous = copy.deepcopy(raw)
        overrides = raw.get("overrides") if isinstance(raw.get("overrides"), dict) else {}
        values = dict(overrides.get(record_id) or {})
        if hidden:
            values["hidden"] = True
        else:
            values.pop("hidden", None)
        values.update({"updated_at": _now(), "updated_by": "local_operator"})
        overrides[record_id] = values
        raw.update({"schema_version": 1, "updated_at": _now(), "overrides": overrides})
        _atomic_json(self.override_path, raw)
        try:
            self.engine.knowledge.reload()
        except Exception:
            _atomic_json(self.override_path, previous)
            raise

    def rollback(self, resource, record_id):
        # Kiem truoc khi doc lich su: neu khong ho tro thi phai noi dung ly do,
        # khong de nguoi dung nhan "rollback_history_empty" roi tuong la chi
        # thieu lich su.
        if resource not in self.RESOURCE_ROLLBACK_DUOC:
            raise ValueError("rollback_not_supported")
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM operator_changes WHERE resource=? AND record_id=? "
                "ORDER BY changed_at DESC LIMIT 1", (resource, record_id),
            ).fetchone()
        if row is None: raise ValueError("rollback_history_empty")
        before = json.loads(row["before_json"])
        current = self.detail(resource, record_id)["data"]
        if resource == "profiles":
            # Re-apply editable fields from the previous effective profile.
            defaults = {"list": [], "pairs": {}}
            changes = {key: before.get(key, defaults.get(kind, ""))
                       for key, kind in self.PROFILE_FIELDS.items()}
            guard = self._begin_guard("profile_override", self.override_path, resource, record_id)
            try:
                after = self._patch_profile(record_id, current, changes)
                revision = self._record_change(resource, record_id, current, after,
                                               "Hoàn tác thay đổi gần nhất", action="rollback")
            except Exception:
                self._abort_guard(guard)
                raise
            self._commit_guard(guard)
            return {"ok": True, "revision": revision, "data": after}
        elif resource == "videos":
            timeline = before.get("timeline") or {}
            errors = validate_timeline(timeline)
            if errors: raise ValueError("rollback_snapshot_invalid")
            timeline_path = VIDEO_DIR / record_id / "timeline.json"
            rb_op, rb_prev = self._begin_file_first("video_rollback", timeline_path, timeline, "videos", record_id)
            try:
                after = next(item for item in self._videos() if item["id"] == record_id)
                revision = self._record_change(resource, record_id, current, after,
                                               "Hoàn tác thay đổi gần nhất", action="rollback")
            except Exception:
                self._abort_file_first(rb_op, timeline_path, rb_prev)
                raise
            self._commit_file_first(rb_op, rb_prev)
            return {"ok": True, "revision": revision, "data": after}
        else: raise ValueError("rollback_not_supported")
