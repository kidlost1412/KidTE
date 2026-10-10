"""Durable per-platform FIFO. No platform or model calls belong here."""
import hashlib
import json
import math
import sqlite3
import threading
import time
import uuid
from pathlib import Path


class CommentQueue:
    def __init__(self, path, *, clock=time.time, privacy=None):
        self.privacy = privacy
        self.clock = clock
        self.lock = threading.RLock()
        if str(path) != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=5, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        with self.db:
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS reply_comment_queue (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    queue_id TEXT NOT NULL UNIQUE, platform TEXT NOT NULL,
                    room_id TEXT NOT NULL, comment_id TEXT NOT NULL,
                    customer_key TEXT NOT NULL, received_at REAL NOT NULL,
                    expires_at REAL NOT NULL, payload_json TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'queued',
                    worker_token TEXT NOT NULL DEFAULT '', lease_until REAL,
                    turn_id TEXT NOT NULL DEFAULT '', result_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(platform,room_id,comment_id)
                );
                CREATE INDEX IF NOT EXISTS idx_comment_queue_fifo
                    ON reply_comment_queue(platform,state,sequence);
            ''')

    def close(self):
        with self.lock:
            self.db.close()

    def enqueue(self, platform, room_id, customer_key, payload):
        now = self.clock()
        if platform not in {'tiktok', 'shopee'} or any(
                not isinstance(v, str) or not v or len(v) > 240 for v in (room_id, customer_key)):
            raise ValueError('queue_identity_invalid')
        if not isinstance(payload, dict):
            raise ValueError('queue_payload_invalid')
        received_ms = payload.get('received_at_ms', now * 1000)
        if (not isinstance(received_ms, (int, float)) or isinstance(received_ms, bool)
                or not math.isfinite(received_ms) or received_ms > now * 1000 + 5000):
            raise ValueError('queue_received_at_invalid')
        comment_id = payload.get('comment_id')
        if not comment_id:
            # Retain event time in the fallback: asking again later is a new event.
            comment_id = hashlib.sha256(json.dumps([room_id, customer_key,
                payload.get('created_at') or payload.get('create_time') or received_ms, payload.get('comment')],
                ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest()
        if not isinstance(comment_id, str) or len(comment_id) > 160:
            raise ValueError('queue_comment_id_invalid')
        encoded = json.dumps({**payload, 'comment_id': comment_id, 'received_at_ms': received_ms},
                             ensure_ascii=False, allow_nan=False)
        if len(encoded.encode('utf-8')) > 100_000:
            raise ValueError('queue_payload_too_large')
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if self.privacy is not None and self.privacy.is_forgotten(customer_key):
                return {'state': 'cancelled', 'reason': 'memory_disabled'}
            if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_opt_outs' AND type='table'").fetchone():
                if self.db.execute('SELECT 1 FROM memory_opt_outs WHERE customer_key=?', (customer_key,)).fetchone():
                    return {'state': 'cancelled', 'reason': 'memory_disabled'}
            self.db.execute('''INSERT OR IGNORE INTO reply_comment_queue
                (queue_id,platform,room_id,comment_id,customer_key,received_at,expires_at,payload_json,state)
                VALUES (?,?,?,?,?,?,?,?,?)''', (uuid.uuid4().hex, platform, room_id, comment_id,
                    customer_key, received_ms / 1000, received_ms / 1000 + 120, encoded,
                    'expired' if now >= received_ms / 1000 + 120 else 'queued'))
            return dict(self.db.execute('''SELECT queue_id,sequence,state,expires_at FROM reply_comment_queue
                WHERE platform=? AND room_id=? AND comment_id=?''', (platform, room_id, comment_id)).fetchone())

    def claim_next(self, platform, room_id, *, lease_s=120):
        if platform not in {'tiktok', 'shopee'} or not isinstance(room_id, str) or not room_id:
            raise ValueError('queue_identity_invalid')
        if not isinstance(lease_s, (int, float)) or isinstance(lease_s, bool) or not math.isfinite(lease_s) or not 1 <= lease_s <= 120:
            raise ValueError('queue_lease_invalid')
        now = self.clock()
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self.db.execute("UPDATE reply_comment_queue SET state='cancelled',worker_token='' "
                            "WHERE platform=? AND room_id<>? AND state IN ('queued','generating','ready','unknown','partially_sent')",
                            (platform, room_id))
            # Preserve ambiguity for an old room's in-flight operation, but
            # never let its stale queue lock block the new room indefinitely.
            self.db.execute("UPDATE reply_comment_queue SET state='unknown',worker_token='' "
                            "WHERE platform=? AND room_id<>? AND state='sending'", (platform, room_id))
            # A lost generation lease may already have produced a reply. Never
            # rerun blindly; reconciliation needs the stored turn/interaction.
            self.db.execute("UPDATE reply_comment_queue SET state='unknown',worker_token='' "
                            "WHERE platform=? AND state='generating' AND lease_until<=?", (platform, now))
            self.db.execute("UPDATE reply_comment_queue SET state='expired' "
                            "WHERE platform=? AND state IN ('queued','ready') AND expires_at<=?", (platform, now))
            if self.privacy is not None:
                for r in self.db.execute("SELECT DISTINCT customer_key FROM reply_comment_queue WHERE platform=? "
                                         "AND state IN ('queued','ready')", (platform,)).fetchall():
                    if self.privacy.is_forgotten(r['customer_key']):
                        self.db.execute("UPDATE reply_comment_queue SET state='cancelled',worker_token='' "
                                        "WHERE platform=? AND customer_key=? AND state IN ('queued','ready')",
                                        (platform, r['customer_key']))
            active = self.db.execute("SELECT 1 FROM reply_comment_queue WHERE platform=? "
                                     "AND (state IN ('generating','ready','sending') OR "
                                     "(state='partially_sent' AND expires_at>?)) LIMIT 1", (platform, now)).fetchone()
            if active:
                return None
            row = self.db.execute("SELECT * FROM reply_comment_queue WHERE platform=? AND room_id=? "
                                  "AND state='queued' ORDER BY sequence LIMIT 1", (platform, room_id)).fetchone()
            if not row:
                return None
            token = uuid.uuid4().hex
            self.db.execute("UPDATE reply_comment_queue SET state='generating',worker_token=?,lease_until=? "
                            "WHERE queue_id=?", (token, min(now + lease_s, row['expires_at']), row['queue_id']))
            return {'queue_id': row['queue_id'], 'worker_token': token,
                    'payload': json.loads(row['payload_json']), 'expires_at': row['expires_at']}

    def finish_generation(self, queue_id, token, result):
        if not isinstance(result, dict):
            raise ValueError('queue_result_invalid')
        now = self.clock()
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute('SELECT * FROM reply_comment_queue WHERE queue_id=?', (queue_id,)).fetchone()
            if not row or row['state'] != 'generating' or row['worker_token'] != token:
                return False
            state = 'ready' if result.get('should_send') else 'cancelled'
            if now >= row['expires_at']:
                state = 'expired'
                result = {'ok': True, 'should_send': False, 'reason': 'expired'}
            self.db.execute('UPDATE reply_comment_queue SET state=?,turn_id=?,result_json=?,worker_token=? '
                            'WHERE queue_id=?', (state, result.get('turn_id', result.get('reply_id', '')),
                            json.dumps(result, ensure_ascii=False, allow_nan=False), '', queue_id))
            return True

    def settle(self, turn_id, state, *, first_sent_at=None):
        if not isinstance(turn_id, str) or not turn_id:
            raise ValueError('queue_turn_id_invalid')
        if state not in {'sent', 'failed', 'unknown', 'partially_sent', 'expired', 'cancelled'}:
            raise ValueError('queue_state_invalid')
        with self.lock, self.db:
            self.db.execute("UPDATE reply_comment_queue SET state=? WHERE turn_id=? AND state NOT IN ('sent','cancelled','expired')",
                            (state, turn_id))
            if first_sent_at is not None:
                if not isinstance(first_sent_at, (int, float)) or isinstance(first_sent_at, bool) or not math.isfinite(first_sent_at):
                    raise ValueError('queue_first_sent_invalid')
                self.db.execute('UPDATE reply_comment_queue SET expires_at=? WHERE turn_id=?',
                                (first_sent_at + 60, turn_id))

    def by_turn(self, turn_id):
        with self.lock:
            row = self.db.execute('SELECT * FROM reply_comment_queue WHERE turn_id=?', (turn_id,)).fetchone()
            if row is None:
                return None
            return {'queue_id': row['queue_id'], 'platform': row['platform'], 'room_id': row['room_id'],
                    'state': row['state'], 'expires_at': row['expires_at'],
                    'payload': json.loads(row['payload_json'])}

    def replace_turn(self, old_turn_id, result):
        """Swap a recomposed turn in; the client picks it up from ready() on its next request."""
        if not isinstance(result, dict) or not result.get('should_send'):
            raise ValueError('queue_result_invalid')
        with self.lock, self.db:
            return self.db.execute(
                "UPDATE reply_comment_queue SET turn_id=?,result_json=?,state='ready',worker_token='' "
                "WHERE turn_id=? AND state IN ('ready','sending')",
                (result.get('turn_id', result.get('reply_id', '')),
                 json.dumps(result, ensure_ascii=False, allow_nan=False), old_turn_id)).rowcount == 1

    def ready(self, platform, room_id):
        """Reloading a client retrieves the existing turn, never regenerates it."""
        now = self.clock()
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self.db.execute("UPDATE reply_comment_queue SET state='expired' "
                            "WHERE platform=? AND room_id=? AND state='ready' AND expires_at<=?",
                            (platform, room_id, now))
            row = self.db.execute("SELECT * FROM reply_comment_queue WHERE platform=? AND room_id=? "
                                  "AND (state='ready' OR (state='partially_sent' AND expires_at>?)) "
                                  "ORDER BY sequence LIMIT 1", (platform, room_id, now)).fetchone()
            if row is None:
                return None
            return {'queue_id': row['queue_id'], 'result': json.loads(row['result_json']),
                    'source_comment': json.loads(row['payload_json'])}

    def cancel(self, platform, room_id, comment_id):
        """Cancel a still-queued item the client dropped locally without sending.

        Items already generating/ready/sending are never touched: they may hold
        an in-flight model call or a delivered turn.
        """
        if platform not in {'tiktok', 'shopee'} or any(
                not isinstance(v, str) or not v or len(v) > 240 for v in (room_id, comment_id)):
            raise ValueError('queue_identity_invalid')
        with self.lock, self.db:
            cursor = self.db.execute("UPDATE reply_comment_queue SET state='cancelled',worker_token='' "
                                     "WHERE platform=? AND room_id=? AND comment_id=? AND state='queued'",
                                     (platform, room_id, comment_id))
            if cursor.rowcount:
                return {'cancelled': True}
            row = self.db.execute('SELECT state FROM reply_comment_queue WHERE platform=? AND room_id=? AND comment_id=?',
                                  (platform, room_id, comment_id)).fetchone()
            return {'cancelled': False, 'state': row['state'] if row else 'missing'}

    def summary(self, platform):
        with self.lock:
            rows = self.db.execute('SELECT state,COUNT(*) AS n FROM reply_comment_queue WHERE platform=? GROUP BY state',
                                   (platform,)).fetchall()
            oldest = self.db.execute("SELECT MIN(received_at) FROM reply_comment_queue WHERE platform=? AND state='queued'",
                                     (platform,)).fetchone()[0]
            return {'states': {r['state']: r['n'] for r in rows},
                    'oldest_age_s': max(0, self.clock() - oldest) if oldest is not None else 0}

    def window_counts(self, window_s=86400):
        """Read-only: one grouped query over the recent window (uses the fifo index prefix)."""
        now = self.clock()
        with self.lock:
            rows = self.db.execute(
                'SELECT platform,state,COUNT(*) AS n,MIN(received_at) AS oldest FROM reply_comment_queue '
                'WHERE received_at>=? GROUP BY platform,state', (now - window_s,)).fetchall()
        return [{'platform': r['platform'], 'state': r['state'], 'n': r['n'], 'oldest': r['oldest']} for r in rows], now
