"""Durable reply associations and per-message delivery leases.

No platform API is called here. A lease lost after dispatch is uncertain and
must never grant another sender permission to repeat that message.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path

import fact_revision


def utf16_length(text):
    return len(text.encode("utf-16-le")) // 2


class DeliveryStore:
    def __init__(self, path, *, clock=time.time, privacy=None):
        self.privacy = privacy
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, timeout=5, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        tables = {row[0] for row in self._db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "reply_turns" not in tables and "interactions" in tables and self._db.execute(
                "SELECT 1 FROM interactions LIMIT 1").fetchone():
            backup_dir = path.parent / "schema_backups"
            backup_dir.mkdir(exist_ok=True)
            backup_path = backup_dir / (path.stem + "-before-delivery-v2.sqlite3")
            if not backup_path.exists():
                staging = backup_path.with_suffix(".tmp")
                with closing(sqlite3.connect(staging)) as copy:
                    self._db.backup(copy)
                    if copy.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise ValueError("delivery_migration_backup_invalid")
                os.replace(staging, backup_path)
        with self._db:
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS delivery_schema (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO delivery_schema VALUES (1, 2);
                CREATE TABLE IF NOT EXISTS reply_turns (
                    turn_id TEXT PRIMARY KEY, platform TEXT NOT NULL,
                    room_id TEXT NOT NULL, interaction_id TEXT NOT NULL,
                    created_at REAL NOT NULL, expires_at REAL NOT NULL,
                    state TEXT NOT NULL DEFAULT 'ready',
                    source_dependent INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS reply_messages (
                    message_id TEXT PRIMARY KEY, turn_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL, content TEXT NOT NULL,
                    content_hash TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'ready',
                    claim_token TEXT NOT NULL DEFAULT '', claim_until REAL,
                    dispatched_at REAL, sent_at REAL, receipt_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(turn_id, ordinal)
                );
                CREATE INDEX IF NOT EXISTS idx_reply_turns_room
                    ON reply_turns(platform, room_id, created_at);
            """)
            # Migration-safe: databases created before B06 lack these columns.
            columns = {row[1] for row in self._db.execute("PRAGMA table_info(reply_turns)")}
            for name, ddl in (("fact_revision", "TEXT NOT NULL DEFAULT ''"),
                              ("recompose_count", "INTEGER NOT NULL DEFAULT 0"),
                              ("cancel_reason", "TEXT NOT NULL DEFAULT ''")):
                if name not in columns:
                    self._db.execute(f"ALTER TABLE reply_turns ADD COLUMN {name} {ddl}")

    def close(self):
        with self._lock:
            self._db.close()

    def reconcile(self, turn_id):
        """Expose expired leases as unknown even when nobody tries to resend."""
        now = self.clock()
        with self._lock, self._db:
            self._db.execute('BEGIN IMMEDIATE')
            turn = self.get(turn_id)
            if not turn:
                return None
            self._db.execute("UPDATE reply_messages SET state=CASE WHEN dispatched_at IS NULL "
                             "THEN 'ready' ELSE 'unknown' END,claim_token='' "
                             "WHERE turn_id=? AND state='sending' AND claim_until<=?", (turn_id, now))
            sent_times = [m['sent_at'] for m in turn['messages'] if m['sent_at'] is not None]
            deadline = min(sent_times) + 60 if sent_times else turn['expires_at']
            if now >= deadline:
                self._db.execute("UPDATE reply_messages SET state='expired' WHERE turn_id=? AND state='ready'", (turn_id,))
            self._refresh_state(turn_id)
            return self.get(turn_id)

    def prepare(self, turn_id, platform, room_id, interaction_id, messages, *,
                expires_at=None, source_dependent=True, fact_revision=None, recompose_count=0):
        if platform not in {"tiktok", "shopee"} or any(
                not isinstance(value, str) or not value for value in (room_id, turn_id, interaction_id)):
            raise ValueError("delivery_identity_invalid")
        if not isinstance(messages, list) or not 1 <= len(messages) <= 3:
            raise ValueError("delivery_messages_invalid")
        if any(not isinstance(text, str) or not text.strip() or utf16_length(text) > 150
               for text in messages):
            raise ValueError("delivery_content_invalid")
        now = self.clock()
        deadline = expires_at if expires_at is not None else now + 120
        if not isinstance(deadline, (int, float)) or isinstance(deadline, bool) or not math.isfinite(deadline):
            raise ValueError("delivery_expiry_invalid")
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            if self._db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                "AND name='interactions'").fetchone():
                interaction = self._db.execute(
                    "SELECT customer_key FROM interactions WHERE interaction_id=?",
                    (interaction_id,),
                ).fetchone()
                if interaction is None:
                    raise ValueError("delivery_interaction_missing")
                if self._db.execute("SELECT 1 FROM memory_opt_outs WHERE customer_key=?",
                                    (interaction['customer_key'],)).fetchone():
                    raise ValueError("delivery_customer_deleted")
                if self.privacy is not None and self.privacy.is_forgotten(interaction['customer_key']):
                    raise ValueError("delivery_customer_deleted")
            old = self.get(turn_id)
            if old:
                if (old["platform"], old["room_id"], old["interaction_id"],
                    [item["content"] for item in old["messages"]]) != (
                        platform, room_id, interaction_id, messages):
                    raise ValueError("delivery_turn_conflict")
                return old
            self._db.execute("""INSERT INTO reply_turns (turn_id,platform,room_id,interaction_id,
                created_at,expires_at,state,source_dependent,fact_revision,recompose_count)
                VALUES (?,?,?,?,?,?,'ready',?,?,?)""",
                (turn_id, platform, room_id, interaction_id, now, deadline, int(source_dependent),
                 json.dumps(fact_revision, sort_keys=True) if fact_revision else '', int(recompose_count)))
            for ordinal, content in enumerate(messages):
                self._db.execute("""INSERT INTO reply_messages
                    (message_id,turn_id,ordinal,content,content_hash) VALUES (?,?,?,?,?)""",
                    (uuid.uuid4().hex, turn_id, ordinal, content,
                     hashlib.sha256(content.encode("utf-8")).hexdigest()))
        return self.get(turn_id)

    def get(self, turn_id):
        with self._lock:
            row = self._db.execute("SELECT * FROM reply_turns WHERE turn_id=?", (turn_id,)).fetchone()
            if not row:
                return None
            out = dict(row)
            out["messages"] = [dict(item) for item in self._db.execute(
                "SELECT * FROM reply_messages WHERE turn_id=? ORDER BY ordinal", (turn_id,))]
            return out

    def limit_expiry(self, turn_id, deadline):
        if not isinstance(deadline, (int, float)) or isinstance(deadline, bool) or not math.isfinite(deadline):
            raise ValueError('delivery_expiry_invalid')
        with self._lock, self._db:
            self._db.execute('UPDATE reply_turns SET expires_at=MIN(expires_at,?) WHERE turn_id=?',
                             (deadline, turn_id))

    def claim(self, turn_id, message_id, platform, room_id, *, lease_s=30, current_facts=None):
        if not isinstance(lease_s, (int, float)) or isinstance(lease_s, bool) or not math.isfinite(lease_s) or not 1 <= lease_s <= 60:
            raise ValueError("delivery_lease_invalid")
        now = self.clock()
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            turn = self.get(turn_id)
            if not turn or turn["platform"] != platform or turn["room_id"] != room_id:
                return {"ok": False, "error": "delivery_identity_mismatch"}
            message = next((item for item in turn["messages"] if item["message_id"] == message_id), None)
            if not message:
                return {"ok": False, "error": "delivery_message_not_found"}
            if message["state"] == "sending" and (message["claim_until"] or 0) <= now:
                state = "unknown" if message["dispatched_at"] is not None else "ready"
                self._db.execute("UPDATE reply_messages SET state=?,claim_token='' WHERE message_id=?",
                                 (state, message_id))
                message["state"] = state
                self._refresh_state(turn_id)
            if message["state"] != "ready":
                return {"ok": False, "error": "delivery_not_ready", "state": message["state"]}
            first_sent = next((item["sent_at"] for item in turn["messages"] if item["sent_at"] is not None), None)
            deadline = turn["expires_at"] if first_sent is None else first_sent + 60
            # Facts are re-checked only before the first message; legacy turns have none.
            if message["ordinal"] == 0 and first_sent is None and current_facts is not None and turn["fact_revision"]:
                reason = fact_revision.stale_reason(json.loads(turn["fact_revision"]), current_facts)
                if reason:
                    can_recompose = turn["recompose_count"] == 0 and now < deadline
                    verdict = "revision_changed" if can_recompose else "revision_stale"
                    self._cancel(turn_id, verdict)
                    return {"ok": False, "error": verdict, "reason": reason,
                            "recompose": can_recompose, "recompose_count": turn["recompose_count"]}
            if now >= deadline:
                self._db.execute("UPDATE reply_messages SET state='expired' WHERE message_id=?", (message_id,))
                self._refresh_state(turn_id)
                return {"ok": False, "error": "delivery_expired"}
            if any(item["ordinal"] < message["ordinal"] and item["state"] != "sent" for item in turn["messages"]):
                return {"ok": False, "error": "delivery_previous_message_pending"}
            last_sent = self._db.execute('''SELECT MAX(m.sent_at) FROM reply_messages m
                JOIN reply_turns t ON t.turn_id=m.turn_id WHERE t.platform=?''', (platform,)).fetchone()[0]
            cooldown = 2.5 if platform == 'tiktok' else 10
            if last_sent is not None and now < last_sent + cooldown:
                return {'ok': False, 'error': 'delivery_cooldown', 'retry_after_s': last_sent + cooldown - now}
            unfinished = self._db.execute('''SELECT t.turn_id FROM reply_turns t
                JOIN reply_messages m ON m.turn_id=t.turn_id
                WHERE t.platform=? AND t.turn_id<>? AND t.state='partially_sent'
                GROUP BY t.turn_id HAVING MIN(m.sent_at)+60>? LIMIT 1''', (platform, turn_id, now)).fetchone()
            if unfinished:
                return {'ok': False, 'error': 'delivery_previous_turn_pending'}
            active = self._db.execute('''SELECT 1 FROM reply_messages m JOIN reply_turns t
                ON t.turn_id=m.turn_id WHERE t.platform=? AND m.state='sending'
                AND m.claim_until>? LIMIT 1''', (platform, now)).fetchone()
            if active:
                return {'ok': False, 'error': 'delivery_platform_busy'}
            token = uuid.uuid4().hex
            claim_until = min(now + lease_s, deadline)
            self._db.execute("""UPDATE reply_messages SET state='sending',claim_token=?,claim_until=?
                WHERE message_id=?""", (token, claim_until, message_id))
            self._refresh_state(turn_id)
            return {"ok": True, "claim_token": token, "content": message["content"],
                    "content_hash": message["content_hash"], "claim_until": claim_until}

    def _cancel(self, turn_id, reason):
        self._db.execute("UPDATE reply_messages SET state='cancelled',claim_token='' "
                         "WHERE turn_id=? AND state='ready'", (turn_id,))
        self._db.execute("UPDATE reply_turns SET cancel_reason=? WHERE turn_id=?", (reason, turn_id))
        self._refresh_state(turn_id)

    def cancel_turn(self, turn_id, reason):
        """Cancel the unsent messages of a turn (nothing dispatched is touched)."""
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            self._cancel(turn_id, reason)

    def dispatched(self, message_id, token):
        with self._lock, self._db:
            return self._db.execute("""UPDATE reply_messages SET dispatched_at=?
                WHERE message_id=? AND claim_token=? AND state='sending'
                    AND claim_until>? AND dispatched_at IS NULL""",
                (self.clock(), message_id, token, self.clock())).rowcount == 1

    def confirm_message(self, turn_id, message_id, platform, room_id, content_hash, receipt):
        """A v2 receipt proves exactly one dispatched message, never a whole turn."""
        if not isinstance(receipt, dict) or not isinstance(content_hash, str):
            return False
        if platform == 'tiktok':
            code = receipt.get('response_code')
            if not ((type(code) is int and code == 0) or (type(code) is str and code == '0')):
                return False
        elif platform == 'shopee':
            if not isinstance(receipt.get('shopee_message_id'), str) or not receipt['shopee_message_id'].strip():
                return False
        else:
            return False
        if receipt.get('room_id') != room_id:
            return False
        encoded = json.dumps(receipt, sort_keys=True, ensure_ascii=False, allow_nan=False)
        with self._lock, self._db:
            self._db.execute('BEGIN IMMEDIATE')
            turn = self.get(turn_id)
            if not turn or (turn['platform'], turn['room_id']) != (platform, room_id):
                return False
            message = next((m for m in turn['messages'] if m['message_id'] == message_id), None)
            if not message or message['content_hash'] != content_hash:
                return False
            if message['state'] == 'sent':
                return message['receipt_json'] == encoded
            if message['state'] not in {'sending', 'unknown'} or message['dispatched_at'] is None:
                return False
            self._db.execute("UPDATE reply_messages SET state='sent', receipt_json=?, sent_at=? WHERE message_id=?",
                             (encoded, self.clock(), message_id))
            self._refresh_state(turn_id)
            return True

    def mark_message_unknown(self, turn_id, message_id, platform, room_id, content_hash):
        with self._lock, self._db:
            self._db.execute('BEGIN IMMEDIATE')
            turn = self.get(turn_id)
            if not turn or (turn['platform'], turn['room_id']) != (platform, room_id):
                return False
            message = next((m for m in turn['messages'] if m['message_id'] == message_id), None)
            if not message or message['content_hash'] != content_hash or message['state'] == 'sent':
                return False
            if message['dispatched_at'] is None:
                return False
            self._db.execute("UPDATE reply_messages SET state='unknown' WHERE message_id=?", (message_id,))
            self._refresh_state(turn_id)
            return True

    def record_legacy_delivery(self, turn_id, platform, room_id, receipt, state):
        """Adapter for one-message clients; the durable turn supplies identity."""
        if state not in {"sent", "failed", "cancelled", "expired", "unknown"}:
            raise ValueError("delivery_state_invalid")
        if not isinstance(receipt, dict):
            return False
        if state == "sent":
            if platform == "shopee":
                if not isinstance(receipt.get("shopee_message_id"), str) or not receipt["shopee_message_id"].strip():
                    return False
            elif platform == "tiktok":
                code = receipt.get("response_code")
                if not ((type(code) is int and code == 0) or (type(code) is str and code == "0")):
                    return False
                if receipt.get("room_id") != room_id:
                    return False
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            turn = self.get(turn_id)
            if not turn or turn["platform"] != platform or turn["room_id"] != room_id:
                return False
            if len(turn["messages"]) != 1:
                return False
            message = turn["messages"][0]
            encoded = json.dumps(receipt, sort_keys=True, ensure_ascii=False)
            if message["state"] == "sent":
                return state == "sent" and message["receipt_json"] == encoded
            self._db.execute("UPDATE reply_messages SET state=?,receipt_json=?,sent_at=? WHERE message_id=?",
                (state, encoded, self.clock() if state == "sent" else None, message["message_id"]))
            self._db.execute("UPDATE reply_turns SET state=? WHERE turn_id=?", (state, turn_id))
            return True

    def _refresh_state(self, turn_id):
        states = [row[0] for row in self._db.execute(
            "SELECT state FROM reply_messages WHERE turn_id=?", (turn_id,))]
        if not states:
            return
        if all(state == "sent" for state in states):
            state = "sent"
        elif "sent" in states:
            state = "partially_sent"
        else:
            state = next((value for value in ("unknown", "sending", "expired", "cancelled", "failed")
                          if value in states), "ready")
        self._db.execute("UPDATE reply_turns SET state=? WHERE turn_id=?", (state, turn_id))

    def unknown_count(self, window_s=86400):
        """Read-only: messages needing reconcile (unknown) in recent turns, one grouped query."""
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) FROM reply_messages m JOIN reply_turns t ON t.turn_id=m.turn_id "
                "WHERE m.state='unknown' AND t.created_at>=?", (self.clock() - window_s,)).fetchone()
        return int(row[0])
