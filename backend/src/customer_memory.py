"""Bộ nhớ khách hàng xuyên phiên cho Kid TE.

Mỗi phiên ghi một nhật ký JSONL dễ kiểm tra, đồng thời cập nhật SQLite để
truy xuất nhanh ở phiên sau. Dữ kiện bền vững chỉ được rút từ lời khách nói;
lời Kid TE trả lời được lưu để nối mạch, không được xem là nguồn sự thật kỹ thuật.
"""
from __future__ import annotations

import contextlib
import copy
import json
import os
import re
import sqlite3
import time
import threading
import uuid
from datetime import datetime
from pathlib import Path


_PHONE = re.compile(r"(?<!\d)(?:\+?84|0)(?:[\s.\-]?\d){8,10}(?!\d)")
_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_FORGET = re.compile(
    r"\bđừng\s+(?:nhớ|lưu)\s+(?:thông tin|dữ liệu|về\s+tôi|về\s+mình)\b|"
    r"\bkhông\s+(?:được\s+)?lưu\s+(?:thông tin|dữ liệu)\b|"
    r"\b(?:xóa|xoá)\s+(?:hết\s+)?(?:dữ liệu|thông tin)(?:\s+của\s+(?:tôi|mình|anh|chị))?\b|"
    r"\bquên\s+(?:dữ liệu|thông tin|tôi|mình)(?:\s+đi)?\b",
    re.I,
)
_AREA = re.compile(
    r"\b(\d+(?:[.,]\d+)?)\s*"
    r"(ha|héc\s*ta|hec\s*ta|sào|sao|công|cong|m2|m²|"
    r"gốc|goc|cây|cay)\b",
    re.I,
)

_CROPS = (
    ("cà phê", ("cà phê", "cafe", "coffee")),
    ("sầu riêng", ("sầu riêng",)),
    ("xoài", ("xoài",)),
    ("cam", ("cam",)),
    ("bưởi", ("bưởi",)),
    ("hồ tiêu", ("hồ tiêu", "tiêu")),
    ("lúa", ("lúa",)),
    ("ớt", ("ớt",)),
    ("rau", ("rau",)),
    ("mít", ("mít",)),
    ("điều", ("điều",)),
    ("thanh long", ("thanh long",)),
    ("chôm chôm", ("chôm chôm",)),
    ("nhãn", ("nhãn",)),
    ("vải", ("vải",)),
    ("mai", ("mai",)),
)

_PRODUCTS = (
    "Nano Đồng", "Đồng Gold", "Nano Tonic", "Furagin", "Đạm Cá",
    "TC01", "Bung Rễ Roots", "MiG 38", "Nano Amino A6", "Siêu Sâu",
    "Sạch Sâu", "Diệt Rầy", "Bọ Trĩ Top One", "Siêu Bám Dính",
)

_SYMPTOMS = (
    "thán thư", "vàng lá", "thối rễ", "nấm", "bọ trĩ", "rầy", "sâu",
    "cháy lá", "rụng trái", "rụng hoa", "xì mủ", "chậm lớn", "suy cây",
)


def _now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _short(value, limit):
    value = " ".join(str(value or "").split())
    return value[:limit]


def redact_customer_text(value):
    """Không ghi số điện thoại/email vào bộ nhớ hội thoại dài hạn."""
    value = _short(value, 1000)
    value = _PHONE.sub("[SỐ ĐIỆN THOẠI ĐÃ ẨN]", value)
    return _EMAIL.sub("[EMAIL ĐÃ ẨN]", value)


def wants_forget(value):
    return bool(_FORGET.search(str(value or "")))


def extract_customer_facts(comment):
    """Rút dữ kiện có bằng chứng trực tiếp từ chính bình luận của khách."""
    text = redact_customer_text(comment)
    low = text.casefold()
    facts = {}

    area = _AREA.search(low)
    if area:
        unit = {
            "goc": "gốc", "cay": "cây", "hec ta": "héc ta",
        }.get(area.group(2).casefold(), area.group(2).casefold())
        key = "quy_mo" if unit in {"gốc", "cây"} else "dien_tich"
        facts[key] = f"{area.group(1).replace(',', '.')} {unit}"

    for canonical, aliases in _CROPS:
        if any(alias in low for alias in aliases):
            facts["cay_trong"] = canonical
            break

    for product in _PRODUCTS:
        if product.casefold() in low:
            facts["san_pham_quan_tam"] = product
            break
    else:
        if "đạm cá" in low or ("đạm" in low and "cá" in low):
            facts["san_pham_quan_tam"] = "Đạm Cá"
        elif "đồng" in low:
            facts["san_pham_quan_tam"] = "sản phẩm gốc đồng"

    found_symptoms = [item for item in _SYMPTOMS if item in low]
    if found_symptoms:
        facts["van_de"] = ", ".join(found_symptoms[:3])
    return facts


class CustomerMemory:
    """Kho nhớ local, thread-safe và không làm LIVE phụ thuộc vào mạng."""

    def __init__(
        self,
        db_path,
        journal_dir,
        channel="",
        session_id="",
        max_prior_turns=3,
        platform="tiktok",
        privacy=None,
    ):
        self.privacy = privacy
        self.derived_stores = None  # optional callable -> {'learning':..,'workspace':..,'history':..}
        self.db_path = Path(db_path)
        self.journal_dir = Path(journal_dir)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.journal_dir.mkdir(parents=True, exist_ok=True)
        self.session_id = session_id or datetime.now().strftime("%Y%m%d_%H%M%S")
        self.channel = _short(channel, 120)
        self.platform = _short(platform, 30).casefold() or "tiktok"
        self.max_prior_turns = max(1, int(max_prior_turns))
        self._lock = threading.RLock()
        self._closed = False
        self._recall_announced = set()
        self.started_at = _now_iso()
        self.journal_path = self.journal_dir / f"customers_{self.session_id}.jsonl"
        self._db = sqlite3.connect(
            self.db_path,
            timeout=3.0,
            check_same_thread=False,
        )
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._create_schema()
        with self._lock, self._db:
            self._db.execute(
                """
                INSERT OR REPLACE INTO sessions
                    (session_id, channel, room_id, started_at, ended_at)
                VALUES (?, ?, COALESCE(
                    (SELECT room_id FROM sessions WHERE session_id = ?), ''
                ), ?, NULL)
                """,
                (self.session_id, self.channel, self.session_id, self.started_at),
            )
        try:
            self._journal = open(self.journal_path, "a", encoding="utf-8")
        except Exception:
            self._db.close()
            raise

    def _create_schema(self):
        with self._db:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    channel TEXT NOT NULL DEFAULT '',
                    room_id TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL,
                    ended_at TEXT
                );
                CREATE TABLE IF NOT EXISTS customers (
                    customer_key TEXT PRIMARY KEY,
                    platform_user_id TEXT NOT NULL DEFAULT '',
                    sec_uid TEXT NOT NULL DEFAULT '',
                    unique_id TEXT NOT NULL DEFAULT '',
                    display_name TEXT NOT NULL DEFAULT '',
                    aliases_json TEXT NOT NULL DEFAULT '[]',
                    identity_strength INTEGER NOT NULL DEFAULT 0,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    last_session_id TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS interactions (
                    interaction_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    happened_at TEXT NOT NULL,
                    customer_key TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    comment TEXT NOT NULL DEFAULT '',
                    response TEXT NOT NULL DEFAULT '',
                    response_complete INTEGER,
                    delivery_status TEXT NOT NULL DEFAULT 'none',
                    product_id TEXT NOT NULL DEFAULT '',
                    product_title TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    runtime_comment_id TEXT NOT NULL DEFAULT '',
                    runtime_turn_id TEXT NOT NULL DEFAULT '',
                    last_segment_id TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_interactions_customer_time
                    ON interactions(customer_key, happened_at DESC);
                CREATE INDEX IF NOT EXISTS idx_interactions_session
                    ON interactions(session_id);
                CREATE TABLE IF NOT EXISTS customer_facts (
                    customer_key TEXT NOT NULL,
                    fact_key TEXT NOT NULL,
                    fact_value TEXT NOT NULL,
                    source_comment TEXT NOT NULL,
                    source_at TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 1.0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (customer_key, fact_key)
                );
                CREATE TABLE IF NOT EXISTS memory_opt_outs (
                    customer_key TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL DEFAULT '',
                    opted_out_at TEXT NOT NULL
                );
                """
            )
            # D5: bản cũ lưu tên thật của khách đã opt-out; xoá tên, giữ khoá để vẫn chặn ghi nhớ.
            self._db.execute("UPDATE memory_opt_outs SET display_name='' WHERE display_name!=''")
            columns = {
                row[1] for row in self._db.execute(
                    "PRAGMA table_info(interactions)"
                )
            }
            for name in (
                "runtime_comment_id",
                "runtime_turn_id",
                "last_segment_id",
            ):
                if name not in columns:
                    self._db.execute(
                        f"ALTER TABLE interactions ADD COLUMN {name} "
                        "TEXT NOT NULL DEFAULT ''"
                    )
            fact_columns = {row[1] for row in self._db.execute("PRAGMA table_info(customer_facts)")}
            if "source_interaction_id" not in fact_columns:
                self._db.execute("ALTER TABLE customer_facts ADD COLUMN source_interaction_id TEXT")
            if "delivery_status" not in columns:
                self._db.execute(
                    "ALTER TABLE interactions ADD COLUMN delivery_status "
                    "TEXT NOT NULL DEFAULT 'none'"
                )
            self._db.execute(
                """
                UPDATE interactions SET delivery_status = CASE
                    WHEN response_complete = 1 THEN 'legacy_unverified'
                    WHEN response_complete = 0 AND response != '' THEN 'generated'
                    ELSE 'none' END
                WHERE delivery_status = 'none'
                """
            )

    @staticmethod
    def customer_key(
        platform_user_id="",
        sec_uid="",
        unique_id="",
        display_name="",
        platform="tiktok",
    ):
        platform = _short(platform, 30).casefold() or "tiktok"
        platform_user_id = _short(platform_user_id, 160)
        sec_uid = _short(sec_uid, 240)
        unique_id = _short(unique_id, 160).casefold()
        display_name = _short(display_name, 160).casefold()
        if platform_user_id and platform_user_id != "0":
            return f"{platform}:id:{platform_user_id}", 3
        if sec_uid:
            return f"{platform}:sec:{sec_uid}", 3
        if unique_id:
            return f"{platform}:unique:{unique_id}", 2
        return f"{platform}:name:{display_name}", 0

    def _identity(self, **identity):
        key, strength = self.customer_key(**identity, platform=self.platform)
        return {
            "customer_key": key,
            "identity_strength": strength,
            "platform_user_id": _short(identity.get("platform_user_id"), 160),
            "sec_uid": _short(identity.get("sec_uid"), 240),
            "unique_id": _short(identity.get("unique_id"), 160),
            "display_name": _short(
                identity.get("display_name")
                or identity.get("unique_id")
                or "Khách",
                160,
            ),
        }

    def _upsert_customer(self, ident, happened_at):
        old = self._db.execute(
            "SELECT aliases_json, display_name FROM customers WHERE customer_key = ?",
            (ident["customer_key"],),
        ).fetchone()
        aliases = []
        if old:
            try:
                aliases = json.loads(old["aliases_json"] or "[]")
            except json.JSONDecodeError:
                aliases = []
            old_name = old["display_name"]
            if old_name and old_name not in aliases:
                aliases.append(old_name)
        if ident["display_name"] and ident["display_name"] not in aliases:
            aliases.append(ident["display_name"])
        aliases = aliases[-8:]
        self._db.execute(
            """
            INSERT INTO customers (
                customer_key, platform_user_id, sec_uid, unique_id,
                display_name, aliases_json, identity_strength,
                first_seen, last_seen, last_session_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(customer_key) DO UPDATE SET
                platform_user_id = CASE WHEN excluded.platform_user_id != ''
                    THEN excluded.platform_user_id ELSE customers.platform_user_id END,
                sec_uid = CASE WHEN excluded.sec_uid != ''
                    THEN excluded.sec_uid ELSE customers.sec_uid END,
                unique_id = CASE WHEN excluded.unique_id != ''
                    THEN excluded.unique_id ELSE customers.unique_id END,
                display_name = excluded.display_name,
                aliases_json = excluded.aliases_json,
                identity_strength = MAX(
                    customers.identity_strength, excluded.identity_strength
                ),
                last_seen = excluded.last_seen,
                last_session_id = excluded.last_session_id
            """,
            (
                ident["customer_key"], ident["platform_user_id"],
                ident["sec_uid"], ident["unique_id"], ident["display_name"],
                json.dumps(aliases, ensure_ascii=False),
                ident["identity_strength"], happened_at, happened_at,
                self.session_id,
            ),
        )

    def _append_journal(self, event, **data):
        row = {
            "schema_version": 1,
            "session_id": self.session_id,
            "timestamp": _now_iso(),
            "event": event,
        }
        row.update(data)
        self._journal.write(json.dumps(row, ensure_ascii=False) + "\n")
        self._journal.flush()

    def set_room_id(self, room_id):
        room_id = _short(room_id, 160)
        with self._lock, self._db:
            if getattr(self, '_room_id', None) == room_id:
                return
            self._db.execute("""UPDATE interactions SET metadata_json = json_set(
                metadata_json, '$.room_id', (SELECT room_id FROM sessions WHERE session_id = ?))
                WHERE session_id = ? AND json_extract(metadata_json, '$.room_id') IS NULL""",
                (self.session_id, self.session_id))
            self._db.execute(
                "UPDATE sessions SET room_id = ? WHERE session_id = ?",
                (room_id, self.session_id),
            )
            self._room_id = room_id

    def recent_live_turns(self, customer_key, comment_prefix, exclude_id, limit=6, *, room_id=None):
        """Recent turns of this exact customer/session for resolving short follow-ups."""
        with self._lock:
            rows = self._db.execute("""SELECT i.interaction_id, i.comment, i.response, i.product_title, i.product_id,
                i.delivery_status, i.happened_at,
                json_extract(i.metadata_json, '$.resolved_question') AS resolved_question,
                json_extract(i.metadata_json, '$.conversation_state') AS conversation_state FROM interactions i
                JOIN sessions s ON s.session_id = i.session_id
                WHERE i.customer_key = ? AND i.interaction_id != ?
                  AND substr(i.runtime_comment_id, 1, ?) = ?
                  AND (? IS NULL OR coalesce(json_extract(i.metadata_json, '$.room_id'), s.room_id) = ?)
                ORDER BY i.happened_at DESC, i.rowid DESC LIMIT ?""",
                (customer_key, exclude_id, len(comment_prefix), comment_prefix,
                 room_id, room_id, min(12, limit))).fetchall()
        now = datetime.now().astimezone()
        result = []
        for row in rows:
            try:
                at = datetime.fromisoformat(row['happened_at']).astimezone()
                if 0 <= (now - at).total_seconds() < 20 * 60:
                    item = dict(row)
                    try:
                        item['conversation_state'] = json.loads(item.get('conversation_state') or '{}')
                    except (TypeError, ValueError):
                        item['conversation_state'] = {}
                    if item['delivery_status'] != 'sent':
                        item['conversation_state'] = {}
                    confirmed = self.confirmed_response(item['interaction_id'])
                    if confirmed:
                        item.update(response=confirmed, delivery_status='sent')
                    result.append(item)
            except (TypeError, ValueError):
                continue
        return result

    def note_resolved_question(self, interaction_id, question):
        with self._lock, self._db:
            self._db.execute("""UPDATE interactions SET metadata_json = json_set(
                metadata_json, '$.resolved_question', ?) WHERE interaction_id = ? AND session_id = ?""",
                (_short(question, 1200), interaction_id, self.session_id))

    def note_conversation_state(self, interaction_id, state):
        from live_conversation import conversation_state
        state = state if isinstance(state, dict) else {}
        normalized = conversation_state(state.get('awaiting_fields'),
            product_id=state.get('selected_sku', ''), observed_pin=state.get('observed_pin', ''),
            product_family=state.get('product_family', ''), candidate_skus=state.get('candidate_skus'),
            clarify_count=state.get('clarify_count'))
        with self._lock, self._db:
            self._db.execute("UPDATE interactions SET metadata_json=json_set(metadata_json, '$.conversation_state', json(?)) "
                "WHERE interaction_id=? AND session_id=?", (json.dumps(normalized, ensure_ascii=False), interaction_id, self.session_id))

    def forget_if_requested(self, comment, **identity):
        if not wants_forget(comment):
            return False
        ident = self._identity(**identity)
        if self.privacy is not None and self.memory_available():
            self.privacy.forget(ident["customer_key"], memory=self,
                                display_name=ident["display_name"],
                                derived=self._derived(),
                                identifiers=[ident["platform_user_id"], ident["unique_id"], ident["sec_uid"]])
        else:
            self.purge_customer(ident["customer_key"], ident["display_name"])
        return True

    def _derived(self):
        provider = getattr(self, "derived_stores", None)
        if not callable(provider):
            return None
        try:
            return provider()
        except Exception:
            return None

    def is_forgotten(self, customer_key):
        return self.privacy is not None and self.privacy.is_forgotten(customer_key)

    def memory_available(self):
        """False khi khoá quyền riêng tư hỏng: không ghi/không trả trí nhớ khách."""
        return getattr(self.privacy, "memory_available", True) is not False

    def purge_customer(self, customer_key, display_name=""):
        ident = {"customer_key": customer_key, "display_name": display_name}
        with self._lock, self._db:
            # Delivery records contain the actual customer-facing reply. Remove
            # them before deleting their interaction provenance.
            if self._db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='reply_comment_queue'").fetchone():
                self._db.execute('DELETE FROM reply_comment_queue WHERE customer_key=?',
                                 (ident['customer_key'],))
            has_delivery = self._db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reply_turns'"
            ).fetchone()
            if has_delivery:
                self._db.execute(
                    "DELETE FROM reply_messages WHERE turn_id IN "
                    "(SELECT turn_id FROM reply_turns WHERE interaction_id IN "
                    "(SELECT interaction_id FROM interactions WHERE customer_key=?))",
                    (ident["customer_key"],),
                )
                self._db.execute(
                    "DELETE FROM reply_turns WHERE interaction_id IN "
                    "(SELECT interaction_id FROM interactions WHERE customer_key=?)",
                    (ident["customer_key"],),
                )
            self._db.execute(
                "DELETE FROM customer_facts WHERE customer_key = ?",
                (ident["customer_key"],),
            )
            self._db.execute(
                "DELETE FROM interactions WHERE customer_key = ?",
                (ident["customer_key"],),
            )
            self._db.execute(
                "DELETE FROM customers WHERE customer_key = ?",
                (ident["customer_key"],),
            )
            if self.privacy is not None and self.privacy.is_forgotten(ident["customer_key"]):
                # D5: tombstone (chỉ HMAC) đã chặn khách này ở mọi đường; không giữ thêm
                # khoá thô/tên của người đã yêu cầu quên trong DB và bản sao lưu.
                self._db.execute("DELETE FROM memory_opt_outs WHERE customer_key = ?",
                                 (ident["customer_key"],))
            else:
                self._db.execute(
                    """
                    INSERT OR REPLACE INTO memory_opt_outs
                        (customer_key, display_name, opted_out_at)
                    VALUES (?, '', ?)
                    """,
                    (ident["customer_key"], _now_iso()),
                )
            self._recall_announced.discard(ident["customer_key"])
            # Never journal the plain key/name of someone who asked to be forgotten.
            self._append_journal(
                "memory_deleted_by_customer",
                key_hmac=(self.privacy.safe_key_hmac(ident["customer_key"]) if self.privacy is not None else ""),
            )

    @staticmethod
    def _journal_line_mentions(line, needles):
        folded = line.casefold()
        if any(n.casefold() in folded for n in needles):
            return True
        try:
            return json.loads(line).get("customer_key") in needles
        except (ValueError, AttributeError):
            return False

    def purge_journal(self, customer_key, display_name=""):
        """Remove every journal line (all sessions) that mentions this customer.

        Rewrites files via temp + replace; the live journal handle is closed and
        reopened around its own rewrite. Returns lines removed.
        """
        needles = [n for n in (str(customer_key or ""),) if n]
        removed = 0
        if not needles:
            return 0
        with self._lock:
            for path in sorted(self.journal_dir.glob("customers_*.jsonl")):
                try:
                    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
                except OSError:
                    continue
                keep = [ln for ln in lines if not self._journal_line_mentions(ln, needles)]
                if len(keep) == len(lines):
                    continue
                live = path == self.journal_path
                if live:
                    self._journal.flush()
                    self._journal.close()
                tmp = path.with_name(path.name + ".tmp")
                tmp.write_text("".join(keep), encoding="utf-8")
                os.replace(tmp, path)
                if live:
                    self._journal = open(self.journal_path, "a", encoding="utf-8")
                removed += len(lines) - len(keep)
        return removed

    def record_comment(self, comment, comment_id="", data_source="", room_id=None,
                       deduplicate=False, **identity):
        ident = self._identity(**identity)
        with self._lock:
            opted_out = self._db.execute(
                "SELECT 1 FROM memory_opt_outs WHERE customer_key = ?",
                (ident["customer_key"],),
            ).fetchone()
        if opted_out or self.is_forgotten(ident["customer_key"]):
            return {
                "interaction_id": "",
                "customer_key": ident["customer_key"],
                "identity_strength": ident["identity_strength"],
                "opted_out": True,
                "comment_id": _short(comment_id, 160),
            }
        happened_at = _now_iso()
        clean_comment = redact_customer_text(comment)
        interaction_id = uuid.uuid4().hex
        facts = extract_customer_facts(clean_comment)
        with self._lock, self._db:
            if self._db.execute('SELECT 1 FROM memory_opt_outs WHERE customer_key=?',
                                (ident['customer_key'],)).fetchone():
                return {'interaction_id': '', 'customer_key': ident['customer_key'], 'opted_out': True}
            if deduplicate and comment_id and room_id is not None:
                duplicate = self._db.execute("""SELECT interaction_id FROM interactions
                    WHERE runtime_comment_id=? AND customer_key LIKE ?
                        AND json_valid(metadata_json)
                        AND json_extract(metadata_json,'$.room_id')=? LIMIT 1""",
                    (_short(comment_id, 160), self.platform + ':%', _short(room_id, 160))).fetchone()
                if duplicate:
                    return {'interaction_id': duplicate['interaction_id'],
                            'customer_key': ident['customer_key'], 'duplicate': True}
            self._upsert_customer(ident, happened_at)
            self._db.execute(
                """
                INSERT INTO interactions (
                    interaction_id, session_id, happened_at, customer_key,
                    event_type, comment, response_complete, runtime_comment_id,
                    metadata_json
                ) VALUES (?, ?, ?, ?, 'comment', ?, NULL, ?, ?)
                """,
                (
                    interaction_id, self.session_id, happened_at,
                    ident["customer_key"], clean_comment,
                    _short(comment_id, 160),
                    json.dumps({
                        "data_source": _short(data_source, 40) or "unlabeled",
                        "room_id": (_short(room_id, 160) if room_id is not None else
                            (self._db.execute('SELECT room_id FROM sessions WHERE session_id = ?',
                                (self.session_id,)).fetchone() or {'room_id': ''})['room_id']),
                    }, ensure_ascii=False),
                ),
            )
            for fact_key, fact_value in (facts.items() if self.memory_available() else ()):
                self._db.execute(
                    """
                    INSERT INTO customer_facts (
                        customer_key, fact_key, fact_value, source_comment,
                        source_at, confidence, updated_at, source_interaction_id
                    ) VALUES (?, ?, ?, ?, ?, 1.0, ?, ?)
                    ON CONFLICT(customer_key, fact_key) DO UPDATE SET
                        source_interaction_id = excluded.source_interaction_id,
                        fact_value = excluded.fact_value,
                        source_comment = excluded.source_comment,
                        source_at = excluded.source_at,
                        confidence = excluded.confidence,
                        updated_at = excluded.updated_at
                    """,
                    (
                        ident["customer_key"], fact_key, fact_value,
                        clean_comment, happened_at, happened_at, interaction_id,
                    ),
                )
        return {
            "interaction_id": interaction_id,
            "customer_key": ident["customer_key"],
            "identity_strength": ident["identity_strength"],
            "comment_id": _short(comment_id, 160),
        }

    def has_comment_id(self, comment_id):
        """Deduplicate a platform comment across backend restarts."""
        comment_id = _short(comment_id, 160)
        if not comment_id:
            return False
        with self._lock:
            return self._db.execute(
                "SELECT 1 FROM interactions WHERE runtime_comment_id = ? LIMIT 1",
                (comment_id,),
            ).fetchone() is not None

    def can_retry_unanswered(self, comment_id, user_id, comment):
        """Explicit recovery of a recent real comment that never generated a reply."""
        with self._lock:
            rows = self._db.execute(
                "SELECT i.happened_at, i.comment, i.response, i.delivery_status, c.platform_user_id "
                "FROM interactions i JOIN customers c ON c.customer_key = i.customer_key "
                "WHERE i.runtime_comment_id = ? ORDER BY i.happened_at DESC",
                (_short(comment_id, 160),),
            ).fetchall()
        if not rows or any(row[2] or row[3] != "none" for row in rows):
            return False
        row = rows[0]
        try:
            at = datetime.fromisoformat(row[0])
            age = (datetime.now(at.tzinfo) - at).total_seconds()
        except (ValueError, TypeError):
            return False
        return (0 <= age <= 1800 and row[1] == comment and str(row[4]) == str(user_id))

    def record_reply(
        self,
        interaction_id,
        response,
        complete=True,
        product=None,
        turn_id="",
        segment_id="",
        metrics=None,
    ):
        if not interaction_id:
            return
        clean_response = redact_customer_text(response)
        product = product or {}
        product_id = _short(product.get("product_id"), 160)
        product_title = _short(product.get("title"), 300)
        with self._lock, self._db:
            row = self._db.execute(
                """
                SELECT i.customer_key, i.happened_at, i.comment,
                    i.runtime_comment_id, i.metadata_json,
                    c.platform_user_id, c.unique_id, c.display_name
                FROM interactions AS i
                LEFT JOIN customers AS c
                    ON c.customer_key = i.customer_key
                WHERE i.interaction_id = ? AND i.session_id = ?
                """,
                (interaction_id, self.session_id),
            ).fetchone()
            if not row:
                return
            self._db.execute(
                """
                UPDATE interactions SET response = ?, response_complete = ?,
                    delivery_status = ?, product_id = ?, product_title = ?, runtime_turn_id = ?,
                    last_segment_id = ?
                WHERE interaction_id = ?
                """,
                (
                    clean_response, 1 if complete else 0,
                    "sent" if complete else "generated",
                    product_id, product_title, _short(turn_id, 160),
                    _short(segment_id, 160), interaction_id,
                ),
            )
            self._append_journal(
                "conversation_turn",
                interaction_id=interaction_id,
                customer_key=row["customer_key"],
                platform_user_id=row["platform_user_id"] or "",
                unique_id=row["unique_id"] or "",
                display_name=row["display_name"] or "",
                comment_at=row["happened_at"],
                comment=row["comment"],
                response=clean_response,
                response_complete=bool(complete),
                product_id=product_id,
                product_title=product_title,
                comment_id=row["runtime_comment_id"] or "",
                turn_id=_short(turn_id, 160),
                segment_id=_short(segment_id, 160),
                metrics=metrics or {},
                data_source=(json.loads(row["metadata_json"] or "{}")
                             .get("data_source", "unlabeled")),
            )

    def confirm_reply_sent(self, interaction_id, turn_id, receipt=None, *, allow_previous_session=False):
        """Chỉ hoàn tất một reply sau khi TikTok trả receipt thành công."""
        interaction_id = _short(interaction_id, 160)
        turn_id = _short(turn_id, 160)
        if not interaction_id or not turn_id:
            return False
        with self._lock, self._db:
            row = self._db.execute(
                """
                SELECT metadata_json, delivery_status FROM interactions
                WHERE interaction_id = ? AND (session_id = ? OR ? = 1)
                    AND customer_key LIKE ?
                    AND runtime_turn_id = ? AND response != ''
                """,
                (interaction_id, self.session_id, int(allow_previous_session), self.platform + ':%', turn_id),
            ).fetchone()
            if not row:
                return False
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except (TypeError, ValueError):
                metadata = {}
            if not isinstance(receipt, dict):
                return False
            if self.platform == "shopee":
                if not _short(receipt.get("shopee_message_id"), 160):
                    return False
            else:
                code = receipt.get("response_code")
                if not ((type(code) is int and code == 0) or
                        (type(code) is str and code == "0")):
                    return False
                expected_room = _short(metadata.get("room_id"), 160)
                actual_room = _short(receipt.get("room_id"), 160)
                if expected_room and actual_room != expected_room:
                    return False
            if row["delivery_status"] == "sent":
                return metadata.get(f"{self.platform}_send_receipt") == receipt
            metadata[f"{self.platform}_send_receipt"] = receipt or {}
            metadata["sent_at"] = _now_iso()
            self._db.execute(
                """
                UPDATE interactions SET response_complete = 1,
                    delivery_status = 'sent', metadata_json = ? WHERE interaction_id = ?
                """,
                (json.dumps(metadata, ensure_ascii=False), interaction_id),
            )
            self._append_journal(
                "reply_send_receipt",
                interaction_id=interaction_id,
                turn_id=turn_id,
                receipt=receipt or {},
            )
        return True

    def mark_reply_delivery(self, interaction_id, turn_id, status, detail="", *, allow_previous_session=False):
        """Ghi kết cục không-gửi của một câu đã soạn để không lẫn với receipt."""
        interaction_id = _short(interaction_id, 160)
        turn_id = _short(turn_id, 160)
        status = _short(status, 24).casefold()
        if status not in {"failed", "cancelled", "unknown", "expired", "partially_sent"} or not interaction_id or not turn_id:
            return False
        with self._lock, self._db:
            row = self._db.execute(
                """
                SELECT metadata_json FROM interactions
                WHERE interaction_id = ? AND (session_id = ? OR ? = 1)
                    AND customer_key LIKE ?
                    AND runtime_turn_id = ? AND response != ''
                    AND delivery_status != 'sent'
                """,
                (interaction_id, self.session_id, int(allow_previous_session), self.platform + ':%', turn_id),
            ).fetchone()
            if not row:
                return False
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except (TypeError, ValueError):
                metadata = {}
            metadata["delivery_detail"] = _short(detail, 300)
            metadata["delivery_updated_at"] = _now_iso()
            self._db.execute(
                """
                UPDATE interactions SET delivery_status = ?, metadata_json = ?
                WHERE interaction_id = ?
                """,
                (status, json.dumps(metadata, ensure_ascii=False), interaction_id),
            )
            self._append_journal(
                "reply_delivery_status", interaction_id=interaction_id,
                turn_id=turn_id, status=status,
                detail=_short(detail, 300),
            )
        return True

    def confirmed_response(self, interaction_id):
        """Only contents supported by message receipts, including partial turns."""
        with self._lock:
            if not self._db.execute("SELECT 1 FROM sqlite_master WHERE name='reply_messages' AND type='table'").fetchone():
                return ''
            rows = self._db.execute('''SELECT m.content FROM reply_messages m JOIN reply_turns t
                ON t.turn_id=m.turn_id WHERE t.interaction_id=? AND m.state='sent'
                ORDER BY m.ordinal''', (interaction_id,)).fetchall()
            return ' '.join(row[0] for row in rows)

    def data_summary(self, max_age=0.0):
        """Số liệu vận hành không chứa danh tính, dùng để tách live khỏi test.

        max_age > 0 (dashboard poll 5 giây): dùng lại kết quả còn mới; khi phải
        đếm lại thì đếm bằng kết nối ĐỌC RIÊNG (WAL) ngoài khoá chung, để
        record_comment/record_reply trên đường trả lời không xếp hàng sau một
        lần quét toàn bảng. Một luồng dựng, luồng khác dùng kết quả cũ.
        """
        if max_age <= 0:
            return self._data_summary_compute()
        cache = getattr(self, "_summary_cache", None)
        if cache and time.monotonic() - cache[0] < max_age:
            return copy.deepcopy(cache[1])
        flight = self.__dict__.setdefault("_summary_flight", threading.Lock())
        if not flight.acquire(blocking=cache is None):
            return copy.deepcopy(cache[1])
        try:
            cache = getattr(self, "_summary_cache", None)
            if cache and time.monotonic() - cache[0] < max_age:
                return copy.deepcopy(cache[1])
            result = self._data_summary_compute(separate=True)
            self._summary_cache = (time.monotonic(), result)
            return copy.deepcopy(result)
        finally:
            flight.release()

    def _data_summary_compute(self, separate=False):
        if separate and not self._closed and self.db_path.exists():
            try:
                db = sqlite3.connect(f"file:{self.db_path.resolve().as_posix()}?mode=ro",
                                     uri=True, timeout=3.0)
            except sqlite3.Error:
                db = None
            if db is not None:
                try:
                    db.row_factory = sqlite3.Row
                    return self._data_summary_rows(db, contextlib.nullcontext())
                except sqlite3.Error:
                    pass   # lui ve duong cu duoi khoa chung
                finally:
                    db.close()
        return self._data_summary_rows(self._db, self._lock)

    def _data_summary_rows(self, db, guard):
        with guard:
            status_rows = db.execute(
                "SELECT delivery_status, COUNT(*) FROM interactions "
                "GROUP BY delivery_status"
            ).fetchall()
            try:
                # Gom ngay trong SQLite thay vi nap + json.loads 100k dong.
                source_rows = db.execute(
                    "SELECT json_extract(metadata_json, '$.data_source'), COUNT(*) FROM interactions "
                    "GROUP BY 1").fetchall()
            except sqlite3.Error:   # metadata_json hong -> lui ve duong cham
                source_rows = None
            metadata_rows = None if source_rows is not None else db.execute(
                "SELECT metadata_json FROM interactions").fetchall()
        sources = {}
        if source_rows is not None:
            for value, count in source_rows:
                source = _short(value if value is not None else "unlabeled", 40) or "unlabeled"
                sources[source] = sources.get(source, 0) + int(count)
        for row in metadata_rows or ():
            try:
                source = json.loads(row["metadata_json"] or "{}").get(
                    "data_source", "unlabeled"
                )
            except (TypeError, ValueError):
                source = "unlabeled"
            source = _short(source, 40) or "unlabeled"
            sources[source] = sources.get(source, 0) + 1
        return {
            "interactions": sum(int(row[1]) for row in status_rows),
            "delivery_statuses": {str(row[0]): int(row[1]) for row in status_rows},
            "data_sources": sources,
        }

    def fact_rows(self):
        """Thông tin khách AI đang nhớ, kèm tên hiển thị — cho trang Khách hàng."""
        with self._lock:
            rows = self._db.execute(
                """
                SELECT f.customer_key, f.fact_key, f.fact_value, f.source_comment,
                    f.source_at, f.confidence, f.updated_at,
                    COALESCE(c.display_name, '') AS display_name
                FROM customer_facts f LEFT JOIN customers c ON c.customer_key = f.customer_key
                ORDER BY f.updated_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def set_fact(self, customer_key, fact_key, fact_value, source_comment="Chủ shop sửa", source_interaction_id=None):
        now = _now_iso()
        with self._lock:
            self._db.execute(
                """
                INSERT INTO customer_facts(customer_key,fact_key,fact_value,source_comment,source_at,confidence,updated_at,source_interaction_id)
                VALUES (?,?,?,?,?,1.0,?,?)
                ON CONFLICT(customer_key,fact_key) DO UPDATE SET fact_value=excluded.fact_value,
                    source_interaction_id=excluded.source_interaction_id,
                    source_comment=excluded.source_comment, confidence=1.0, updated_at=excluded.updated_at
                """,
                (customer_key, fact_key, _short(fact_value, 200), _short(source_comment, 300), now, now, source_interaction_id),
            )
            self._db.commit()

    def delete_fact(self, customer_key, fact_key):
        with self._lock:
            self._db.execute("DELETE FROM customer_facts WHERE customer_key=? AND fact_key=?",
                             (customer_key, fact_key))
            self._db.commit()

    def context_for_reply(
        self,
        customer_key,
        current_interaction_id="",
        display_name="",
        include_turns=True,
    ):
        """Ngữ cảnh riêng của khách cho trả lời chat chữ.

        Lấy cả các lượt trước trong LIVE hiện tại. Lượt đang xử lý bị
        loại bằng interaction_id để model không thấy câu hỏi hiện tại hai lần.
        """
        if not customer_key or customer_key.startswith("tiktok:name:"):
            return ""
        with self._lock:
            opted_out = self._db.execute(
                "SELECT 1 FROM memory_opt_outs WHERE customer_key = ?",
                (customer_key,),
            ).fetchone()
            if opted_out or self.is_forgotten(customer_key) or not self.memory_available():
                return ""
            customer = self._db.execute(
                """
                SELECT display_name, identity_strength
                FROM customers WHERE customer_key = ?
                """,
                (customer_key,),
            ).fetchone()
            if not customer or customer["identity_strength"] < 2:
                return ""
            turns = self._db.execute(
                """
                SELECT interaction_id, session_id, happened_at, comment, response,
                    response_complete, delivery_status
                FROM interactions
                WHERE customer_key = ? AND event_type = 'comment'
                    AND interaction_id != ?
                ORDER BY happened_at DESC, rowid DESC
                LIMIT ?
                """,
                (
                    customer_key,
                    _short(current_interaction_id, 160),
                    self.max_prior_turns,
                ),
            ).fetchall()
            facts = self._db.execute(
                """
                SELECT fact_key, fact_value
                FROM customer_facts
                WHERE customer_key = ?
                ORDER BY updated_at DESC
                LIMIT 6
                """,
                (customer_key,),
            ).fetchall()

        if not include_turns:
            turns = []      # lịch sử theo phiên đã có ở lich_su_khach
        if not turns and not facts:
            return ""
        name = display_name or customer["display_name"]
        lines = [
            "NGỮ CẢNH RIÊNG CỦA ĐÚNG KHÁCH NÀY (đã khớp bằng ID tài khoản và nguồn sàn):",
            f"- Khách: {_short(name, 160)}",
            "- Chỉ dùng để hiểu câu nói tắt; vẫn phải trả lời đúng câu hiện tại.",
        ]
        if facts:
            lines.append("- Dữ kiện khách từng tự nói:")
            for fact in facts:
                lines.append(
                    f"  + {fact['fact_key']}: {_short(fact['fact_value'], 180)}"
                )
        if turns:
            lines.append("- Các lượt gần nhất của riêng khách này (mới nhất trước):")
            for turn in turns:
                scope = "LIVE này" if turn["session_id"] == self.session_id else "LIVE trước"
                lines.append(
                    f"  + [{scope}] Khách: {_short(turn['comment'], 220)}"
                )
                confirmed = self.confirmed_response(turn['interaction_id'])
                heard = confirmed or (turn['response'] if turn['delivery_status'] == 'sent' else '')
                if heard:
                    lines.append(
                        f"    Đã trả lời: {_short(heard, 500)}"
                    )
        lines.append(
            "- Không trộn dữ liệu của người khác. Không tự nhắc chuyện cũ nếu "
            "nó không giúp hiểu câu hiện tại."
        )
        return "\n".join(lines)

    def card_source(self, customer_key, current_interaction_id=""):
        """Dữ liệu thô cho thẻ khách (customer_card): CHỈ của đúng khách này, truy vấn có chặn.

        Cùng cổng với context_for_reply: khách chỉ có tên (định danh yếu), đã tắt ghi nhớ
        (opt-out) hoặc đã bị xóa (tombstone) => None, tuyệt đối không có dữ liệu.
        Trả {session_id, rows: [{session_id, happened_at, comment, product_title}] mới nhất trước
        (tối đa 60, KHÔNG gồm lượt hiện tại), facts: [(key, value)] tối đa 8}.
        """
        if not customer_key or ":name:" in customer_key:
            return None
        with self._lock:
            if self._db.execute("SELECT 1 FROM memory_opt_outs WHERE customer_key = ?",
                                (customer_key,)).fetchone() or self.is_forgotten(customer_key)                     or not self.memory_available():
                return None
            customer = self._db.execute(
                "SELECT identity_strength FROM customers WHERE customer_key = ?", (customer_key,)).fetchone()
            if not customer or customer["identity_strength"] < 2:
                return None
            rows = self._db.execute(
                "SELECT session_id, happened_at, comment, product_title FROM interactions "
                "WHERE customer_key = ? AND event_type = 'comment' AND interaction_id != ? "
                "ORDER BY happened_at DESC LIMIT 60",
                (customer_key, _short(current_interaction_id, 160))).fetchall()
            facts = self._db.execute(
                "SELECT fact_key, fact_value FROM customer_facts WHERE customer_key = ? "
                "ORDER BY updated_at DESC LIMIT 8", (customer_key,)).fetchall()
        return {"session_id": self.session_id,
                "rows": [dict(row) for row in rows],
                "facts": [(row["fact_key"], row["fact_value"]) for row in facts]}

    def close(self):
        with self._lock:
            if self._closed:
                return
            ended_at = _now_iso()
            try:
                with self._db:
                    unanswered = self._db.execute(
                        """
                        SELECT i.interaction_id, i.customer_key, i.happened_at,
                            i.comment, i.metadata_json,
                            c.platform_user_id, c.unique_id,
                            c.display_name
                        FROM interactions AS i
                        LEFT JOIN customers AS c
                            ON c.customer_key = i.customer_key
                        WHERE i.session_id = ? AND i.event_type = 'comment'
                            AND i.response_complete IS NULL
                        ORDER BY i.happened_at
                        """,
                        (self.session_id,),
                    ).fetchall()
                    for row in unanswered:
                        self._append_journal(
                            "unanswered_comment",
                            interaction_id=row["interaction_id"],
                            customer_key=row["customer_key"],
                            platform_user_id=row["platform_user_id"] or "",
                            unique_id=row["unique_id"] or "",
                            display_name=row["display_name"] or "",
                            comment_at=row["happened_at"],
                            comment=row["comment"],
                            response="",
                            response_complete=False,
                            data_source=(json.loads(row["metadata_json"] or "{}")
                                         .get("data_source", "unlabeled")),
                        )
                    self._db.execute(
                        "UPDATE sessions SET ended_at = ? WHERE session_id = ?",
                        (ended_at, self.session_id),
                    )
            finally:
                self._journal.flush()
                self._journal.close()
                self._db.close()
                self._closed = True
