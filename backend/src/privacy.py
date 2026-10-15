"""Privacy service: HMAC tombstones + auditable forget requests.

The tombstone stores only HMAC-SHA256(machine secret, normalised customer key),
never the plain key, and is written BEFORE any data is deleted so reply
generation and queue claims stop immediately.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from pathlib import Path


def fold_text(value):
    """Case-insensitive, diacritic-folded text used for legacy-row matching."""
    import unicodedata
    text = unicodedata.normalize("NFD", str(value or "").casefold().replace("\u0111", "d"))
    return "".join(ch for ch in text if unicodedata.category(ch) != "Mn")


def _distinctive(folded, min_len):
    # Tên một từ ngắn ("mai", "lan", "nguyen") trùng từ thông dụng: chỉ nhận ID có số,
    # tên từ hai chữ trở lên, hoặc chuỗi đủ dài để không trùng ngẫu nhiên.
    if len(folded) < min_len:
        return False
    return any(ch.isdigit() for ch in folded) or len(folded.split()) >= 2 or len(folded) >= 10


def fold_identifiers(values, min_len=6):
    out = []
    for value in values or ():
        folded = " ".join(fold_text(value).split())
        if _distinctive(folded, min_len) and folded not in out:
            out.append(folded)
    return out


def row_mentions(row, identifiers):
    """True when any identifier appears as a whole word/phrase in the row's stored text/json (folded)."""
    if not identifiers:
        return False
    try:
        blob = fold_text(json.dumps(dict(row), ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return False
    return any(re.search(r'(?<![0-9a-z])' + re.escape(ident) + r'(?![0-9a-z])', blob) for ident in identifiers)


def normalize_key(customer_key):
    return " ".join(str(customer_key or "").split()).casefold()


class PrivacyUnavailableError(ValueError):
    """Khoá quyền riêng tư có trên đĩa nhưng không đọc được — cần người dùng xử lý."""


class PrivacyService:
    def __init__(self, db_path, key_path, *, clock=time.time, exclude_legacy_learning_on_forget=True):
        self.db_path = Path(db_path)
        self.key_path = Path(key_path)
        self.clock = clock
        self.exclude_legacy_learning_on_forget = bool(exclude_legacy_learning_on_forget)
        self._lock = threading.RLock()
        self.unavailable_reason = ""
        try:
            self._secret = self._load_secret()
        except PrivacyUnavailableError as exc:
            # Không làm sập toàn app: tính năng quyền riêng tư báo lỗi rõ ràng,
            # bộ nhớ khách hàng coi mọi khách là "đã xoá" (fail-closed).
            self._secret = None
            self.unavailable_reason = str(exc)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.db_path, timeout=5, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        with self._db:
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS privacy_tombstones (
                    key_hmac TEXT PRIMARY KEY, created_at REAL NOT NULL,
                    scope TEXT NOT NULL DEFAULT 'all'
                );
                CREATE TABLE IF NOT EXISTS privacy_requests (
                    id TEXT PRIMARY KEY, key_hmac TEXT NOT NULL,
                    status TEXT NOT NULL, steps_json TEXT NOT NULL DEFAULT '[]',
                    created_at REAL NOT NULL, finished_at REAL
                );
                CREATE TABLE IF NOT EXISTS privacy_browser_purge (
                    id TEXT PRIMARY KEY, request_id TEXT NOT NULL, key_hmac TEXT NOT NULL,
                    identifiers_json TEXT NOT NULL, acked_json TEXT NOT NULL DEFAULT '[]',
                    created_at REAL NOT NULL
                );
            """)

    def _load_secret(self, *, attempts=4, delay=0.15):
        """Khoá HMAC máy. CHỈ sinh khoá mới khi tệp chưa tồn tại; tệp có mà
        đọc lỗi/ngắn → thử lại ngắn rồi ném ``PrivacyUnavailableError`` (sinh
        khoá mới sẽ làm mọi tombstone cũ mất hiệu lực → khách đã xoá hiện lại)."""
        last = None
        for i in range(attempts):
            try:
                data = self.key_path.read_bytes()
            except FileNotFoundError:
                if i == 0:
                    return self._create_secret()
                last = FileNotFoundError(str(self.key_path))
            except OSError as exc:
                last = exc
            else:
                if len(data) >= 32:
                    return data[:32]
                last = ValueError(f"privacy_key_too_short:{len(data)}")
            if i + 1 < attempts:
                time.sleep(delay)
        raise PrivacyUnavailableError(f"privacy_key_unreadable: {last}")

    def _create_secret(self):
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        data = secrets.token_bytes(32)
        tmp = self.key_path.with_name(self.key_path.name + ".tmp")
        tmp.write_bytes(data)
        try:
            # Không ghi đè nếu tiến trình khác vừa tạo khoá.
            os.link(tmp, self.key_path)
        except FileExistsError:
            tmp.unlink(missing_ok=True)
            return self._load_secret()
        except OSError:
            os.replace(tmp, self.key_path)
        else:
            tmp.unlink(missing_ok=True)
        return data

    def _require_secret(self):
        if self._secret is None:
            raise PrivacyUnavailableError(self.unavailable_reason or "privacy_key_unavailable")
        return self._secret

    def key_hmac(self, customer_key):
        return hmac.new(self._require_secret(), normalize_key(customer_key).encode("utf-8"),
                        hashlib.sha256).hexdigest()

    def is_forgotten(self, customer_key):
        if not customer_key:
            return False
        if self._secret is None:
            return True  # fail-closed: không biết khách đã xoá hay chưa
        digest = self.key_hmac(customer_key)
        with self._lock:
            return self._db.execute("SELECT 1 FROM privacy_tombstones WHERE key_hmac=?",
                                    (digest,)).fetchone() is not None

    def _save(self, request_id, status, steps, finished=False):
        with self._lock, self._db:
            self._db.execute("UPDATE privacy_requests SET status=?,steps_json=?,finished_at=? WHERE id=?",
                             (status, json.dumps(steps), self.clock() if finished else None, request_id))

    def forget(self, customer_key, *, memory=None, display_name="", derived=None, identifiers=None):
        """Tombstone first, then purge memory DB and queue; each step is recorded.

        ``derived`` (optional dict with any of ``learning``, ``history``,
        ``workspace``) switches on the F02 report: every derived store is purged or
        excluded and reported as deleted / excluded / unsupported. The request ends
        ``complete`` only if nothing is excluded or unsupported, else ``partial``.
        Without ``derived`` the F01 behaviour (status ``done``) is unchanged.
        """
        if not normalize_key(customer_key):
            raise ValueError("privacy_customer_key_invalid")
        digest = self.key_hmac(customer_key)
        idents = self.legacy_identifiers(customer_key, display_name, identifiers) if derived is not None else []
        request_id = uuid.uuid4().hex
        now = self.clock()
        with self._lock, self._db:
            self._db.execute("INSERT OR IGNORE INTO privacy_tombstones (key_hmac,created_at,scope) VALUES (?,?,'all')",
                             (digest, now))
            self._db.execute("INSERT INTO privacy_requests (id,key_hmac,status,steps_json,created_at) "
                             "VALUES (?,?,'running','[]',?)", (request_id, digest, now))
        steps = [{"step": "tombstone", "status": "done"}]
        self._save(request_id, "running", steps)
        status = "done"
        plan = [("customer_db", (lambda: memory.purge_customer(customer_key, display_name))
                 if memory is not None else None),
                ("comment_queue", lambda: self._purge_queue(customer_key))]
        if derived is not None:
            plan.extend(self._derived_plan(customer_key, digest, memory, display_name, derived, idents))
        for name, action in plan:
            if action is None:
                steps.append({"step": name, "status": "skipped"})
                continue
            try:
                result = action()
                steps.append(self._step(name, result, derived is not None))
            except Exception as exc:
                steps.append({"step": name, "status": "failed", "error": type(exc).__name__})
                status = "failed"
            self._save(request_id, "running", steps)
        if derived is not None:
            if self._enqueue_browser_purge(request_id, digest, customer_key, idents, identifiers):
                steps.append({"step": "browser_extension_storage", "status": "pending",
                              "detail": "waiting for extension acknowledgement (tiktok, shopee)"})
            else:
                steps.append({"step": "browser_extension_storage", "status": "unsupported",
                              "detail": "unsupported: browser_extension_storage"})
            if status != "failed":
                status = "complete" if all(s["status"] in ("done", "deleted") for s in steps) else "partial"
        self._save(request_id, status, steps, finished=True)
        return {"request_id": request_id, "status": status}

    PURGE_TTL_S = 7 * 86400
    PURGE_PLATFORMS = ("tiktok", "shopee")

    def _enqueue_browser_purge(self, request_id, digest, customer_key, idents, extra):
        values = list(idents or [])
        parts = str(customer_key or "").split(":", 2)
        if len(parts) == 3 and parts[1] in ("id", "sec", "unique"):
            values.append(parts[2])
        values += list(extra or ())
        idents = fold_identifiers(values)
        if not idents:
            return False
        with self._lock, self._db:
            self._db.execute("INSERT INTO privacy_browser_purge (id,request_id,key_hmac,identifiers_json,created_at) "
                             "VALUES (?,?,?,?,?)", (uuid.uuid4().hex, request_id, digest,
                                                    json.dumps(idents), self.clock()))
        return True

    def _expire_purges(self):
        cutoff = self.clock() - self.PURGE_TTL_S
        rows = self._db.execute("SELECT id,request_id FROM privacy_browser_purge WHERE created_at<?",
                                (cutoff,)).fetchall()
        for row in rows:
            self._db.execute("DELETE FROM privacy_browser_purge WHERE id=?", (row["id"],))
            self._set_purge_step(row["request_id"], "expired")

    def pending_purges(self, platform):
        """[{id, platform, identifiers}] not yet acknowledged by ``platform``."""
        if platform not in self.PURGE_PLATFORMS:
            return []
        with self._lock, self._db:
            self._expire_purges()
            out = []
            for row in self._db.execute("SELECT * FROM privacy_browser_purge ORDER BY created_at LIMIT 50"):
                if platform in json.loads(row["acked_json"]):
                    continue
                out.append({"id": row["id"], "platform": platform,
                            "identifiers": json.loads(row["identifiers_json"])})
            return out

    def ack_purges(self, ids, platform):
        if platform not in self.PURGE_PLATFORMS or not isinstance(ids, list):
            raise ValueError("privacy_purge_ack_invalid")
        done = 0
        with self._lock, self._db:
            for pid in ids[:200]:
                if not isinstance(pid, str):
                    continue
                row = self._db.execute("SELECT * FROM privacy_browser_purge WHERE id=?", (pid,)).fetchone()
                if row is None:
                    continue
                acked = sorted(set(json.loads(row["acked_json"])) | {platform})
                done += 1
                if all(p in acked for p in self.PURGE_PLATFORMS):
                    # identifiers are deleted as soon as both platforms confirmed
                    self._db.execute("DELETE FROM privacy_browser_purge WHERE id=?", (pid,))
                    self._set_purge_step(row["request_id"], "deleted")
                else:
                    self._db.execute("UPDATE privacy_browser_purge SET acked_json=? WHERE id=?",
                                     (json.dumps(acked), pid))
        return {"acked": done}

    def _set_purge_step(self, request_id, status):
        row = self._db.execute("SELECT status,steps_json FROM privacy_requests WHERE id=?",
                               (request_id,)).fetchone()
        if row is None:
            return
        steps = json.loads(row["steps_json"])
        for step in steps:
            if step.get("step") == "browser_extension_storage":
                step["status"] = status
                step["detail"] = "extension storage purge " + status
        new = row["status"]
        if new not in ("failed", "running"):
            new = "complete" if all(s["status"] in ("done", "deleted") for s in steps) else "partial"
        self._db.execute("UPDATE privacy_requests SET status=?,steps_json=? WHERE id=?",
                         (new, json.dumps(steps), request_id))

    @staticmethod
    def _step(name, result, derived_mode):
        if not derived_mode:
            return {"step": name, "status": "done"}
        if isinstance(result, dict) and result.get("status") in ("deleted", "excluded", "unsupported"):
            return {"step": name, **result}
        if isinstance(result, dict) and result.get("excluded"):
            return {"step": name, "status": "excluded", "deleted": int(result.get("deleted") or 0),
                    "excluded": int(result["excluded"])}
        deleted = int(result.get("deleted") or 0) if isinstance(result, dict) else 0
        return {"step": name, "status": "deleted", "deleted": deleted}

    def legacy_identifiers(self, customer_key, display_name="", extra=None):
        """Folded names/ids of the customer, read BEFORE their rows are purged."""
        values = [display_name, *(extra or ())]
        parts = str(customer_key or "").split(":", 2)
        if len(parts) == 3 and parts[1] in ("id", "sec", "unique"):
            values.append(parts[2])
        try:
            with self._lock:
                row = self._db.execute(
                    "SELECT platform_user_id,sec_uid,unique_id,display_name,aliases_json FROM customers "
                    "WHERE customer_key=?", (customer_key,)).fetchone()
            if row is not None:
                values += [row["platform_user_id"], row["sec_uid"], row["unique_id"], row["display_name"]]
                try:
                    values += [a for a in json.loads(row["aliases_json"] or "[]") if isinstance(a, str)]
                except ValueError:
                    pass
        except sqlite3.Error:
            pass
        return fold_identifiers(values)

    def _derived_plan(self, customer_key, digest, memory, display_name, derived, idents=()):
        exclude = self.exclude_legacy_learning_on_forget
        plan = []

        def store_action(label, store, call):
            if store is None:
                plan.append((label, lambda: {"status": "unsupported", "detail": "store_not_wired"}))
            else:
                plan.append((label, lambda: call(store)))

        if memory is not None and hasattr(memory, "purge_journal"):
            plan.append(("conversation_journals",
                         lambda: {"deleted": memory.purge_journal(customer_key, display_name)}))
        else:
            plan.append(("conversation_journals", lambda: {"status": "unsupported", "detail": "store_not_wired"}))
        store_action("learning", derived.get("learning"),
                     lambda s: s.forget_customer(digest, exclude_legacy=exclude, identifiers=idents))
        store_action("corrections_examples", derived.get("workspace"),
                     lambda s: s.forget_customer(digest, exclude_legacy=exclude, identifiers=idents))
        history = derived.get("history")
        store_action("customer_history", history, lambda s: s.forget_customer(customer_key))
        if history is not None:
            plan.append(("raw_stream_logs", lambda: {
                "status": "excluded", "detail": "append-only logs not rewritten; excluded from history by tombstone"}))
        return plan

    def _purge_queue(self, customer_key):
        with self._lock, self._db:
            if self._db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                "AND name='reply_comment_queue'").fetchone():
                self._db.execute("DELETE FROM reply_comment_queue WHERE customer_key=?", (customer_key,))

    def request(self, request_id):
        with self._lock:
            row = self._db.execute("SELECT * FROM privacy_requests WHERE id=?", (request_id,)).fetchone()
        if row is None:
            return None
        return {"request_id": row["id"], "status": row["status"],
                "steps": json.loads(row["steps_json"]), "created_at": row["created_at"],
                "finished_at": row["finished_at"]}

    def close(self):
        with self._lock:
            self._db.close()


# ---- F03: functions operating on a given db path (backup/restore filtering) ----

def load_secret_from_path(key_path):
    """Read the machine HMAC secret without creating it (None when absent/short)."""
    try:
        data = Path(key_path).read_bytes()
    except OSError:
        return None
    return data[:32] if len(data) >= 32 else None


def hmac_for_key(secret, customer_key):
    return hmac.new(secret, normalize_key(customer_key).encode("utf-8"), hashlib.sha256).hexdigest()


def read_tombstones(db_path):
    """Return [{key_hmac, created_at, scope}] from a db file (empty if none/missing)."""
    path = Path(db_path)
    if not path.is_file():
        return []
    db = sqlite3.connect(path, timeout=5)
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='privacy_tombstones'").fetchone():
            return []
        return [{"key_hmac": r[0], "created_at": r[1], "scope": r[2]} for r in
                db.execute("SELECT key_hmac,created_at,scope FROM privacy_tombstones ORDER BY key_hmac")]
    finally:
        db.close()


def merge_tombstones(*lists):
    """Union by key_hmac (earliest created_at wins); entries are HMAC records only."""
    merged = {}
    for items in lists:
        for item in items or ():
            if isinstance(item, str):
                item = {"key_hmac": item}
            digest = item.get("key_hmac") if isinstance(item, dict) else None
            if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
                continue
            created = item.get("created_at")
            created = float(created) if isinstance(created, (int, float)) and not isinstance(created, bool) else time.time()
            old = merged.get(digest)
            if old is None or created < old["created_at"]:
                merged[digest] = {"key_hmac": digest, "created_at": created, "scope": item.get("scope") or "all"}
    return [merged[k] for k in sorted(merged)]


def purge_db_by_tombstones(db_path, tombstones, secret):
    """Delete rows of tombstoned customers from the sqlite file at db_path and
    write the tombstone union into it. Operates only on the given path.

    Returns {"deleted": int, "tombstones": int}. Customer-keyed tables are found
    by a ``customer_key`` column, so every table privacy purges is covered.
    """
    items = merge_tombstones(tombstones)
    hashes = {t["key_hmac"] for t in items}
    if hashes and not secret:
        raise ValueError("privacy_secret_required")
    db = sqlite3.connect(Path(db_path), timeout=10)
    deleted = 0
    try:
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        cols = {t: [c[1] for c in db.execute('PRAGMA table_info("%s")' % t.replace('"', '""'))] for t in tables}
        keyed = [t for t in tables if "customer_key" in cols[t] and t != "memory_opt_outs"]
        is_store = bool(keyed) or "privacy_tombstones" in tables
        if not is_store:
            return {"deleted": 0, "tombstones": 0}
        with db:
            matched = []
            if hashes:
                seen = set()
                for t in keyed + (["memory_opt_outs"] if "memory_opt_outs" in tables else []):
                    seen.update(r[0] for r in db.execute('SELECT DISTINCT customer_key FROM "%s"' % t) if r[0])
                matched = [k for k in seen if hmac_for_key(secret, k) in hashes]
            for key in matched:
                if "reply_turns" in tables and "interactions" in tables:
                    db.execute("DELETE FROM reply_messages WHERE turn_id IN (SELECT turn_id FROM reply_turns WHERE "
                               "interaction_id IN (SELECT interaction_id FROM interactions WHERE customer_key=?))", (key,)) \
                        if "reply_messages" in tables else None
                    db.execute("DELETE FROM reply_turns WHERE interaction_id IN "
                               "(SELECT interaction_id FROM interactions WHERE customer_key=?)", (key,))
                for t in keyed:
                    deleted += db.execute('DELETE FROM "%s" WHERE customer_key=?' % t, (key,)).rowcount
            db.execute("CREATE TABLE IF NOT EXISTS privacy_tombstones (key_hmac TEXT PRIMARY KEY, "
                       "created_at REAL NOT NULL, scope TEXT NOT NULL DEFAULT 'all')")
            for t in items:
                db.execute("INSERT OR IGNORE INTO privacy_tombstones (key_hmac,created_at,scope) VALUES (?,?,?)",
                           (t["key_hmac"], t["created_at"], t["scope"]))
        return {"deleted": deleted, "tombstones": len(items)}
    finally:
        db.close()
