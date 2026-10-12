"""Hộp thư phải có dữ liệu cả khi logs/raw_stream trống (cài lại, RAW_STREAM tắt)."""
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import unittest  # noqa: E402
import test_workspace as _tw  # noqa: E402


class MemoryFallbackTests(unittest.TestCase):
    setUp = _tw.WorkspaceStoreTests.setUp
    tearDown = _tw.WorkspaceStoreTests.tearDown

    def _make_memory(self):
        db_path = self.root / "customer_memory.sqlite3"
        with closing(sqlite3.connect(db_path)) as db:
            db.executescript("""
            CREATE TABLE sessions(session_id TEXT, channel TEXT, room_id TEXT, started_at TEXT, ended_at TEXT);
            CREATE TABLE customers(customer_key TEXT, platform_user_id TEXT, sec_uid TEXT, unique_id TEXT, display_name TEXT);
            CREATE TABLE interactions(interaction_id TEXT, session_id TEXT, happened_at TEXT, customer_key TEXT,
              event_type TEXT, comment TEXT, response TEXT, delivery_status TEXT, product_id TEXT,
              product_title TEXT, runtime_comment_id TEXT);
            INSERT INTO sessions VALUES('s1','live-re-ai','room1','2026-10-01T20:00:00+07:00',NULL);
            INSERT INTO sessions VALUES('s2','shopee-live-ai','','2026-10-02T20:00:00+07:00',NULL);
            INSERT INTO customers VALUES('tiktok:id:1','1','','khach1','Khách Một');
            INSERT INTO customers VALUES('shopee:id:2','2','','','Khách Shopee');
            INSERT INTO interactions VALUES('i1','s1','2026-10-01T20:01:00+07:00','tiktok:id:1','comment',
              'Giá bao nhiêu?','Dạ 555k ạ','sent','sku-1','Đạm cá 5L','m1');
            INSERT INTO interactions VALUES('i2','s2','2026-10-02T20:01:00+07:00','shopee:id:2','comment',
              'Còn hàng không?','','','','','shopee:9:9');
            """)
        return db_path

    def test_conversations_fall_back_to_customer_memory_when_raw_stream_empty(self):
        (self.root / "raw_stream").mkdir()
        memory = self._make_memory()
        with mock.patch("workspace.LOG_DIR", self.root), mock.patch("workspace.MEMORY_DB", memory):
            result = self.store.list("conversations", {"view": "summary", "limit": 50})
        self.assertEqual(result["total"], 2)
        by_text = {row["original_text"]: row for row in result["items"]}
        tiktok = by_text["Giá bao nhiêu?"]
        self.assertEqual(tiktok["reply_text"], "Dạ 555k ạ")
        self.assertEqual(tiktok["conversation_status"], "ai_sent")
        self.assertEqual(tiktok["platform"], "tiktok")
        self.assertEqual(by_text["Còn hàng không?"]["platform"], "shopee")

    def test_raw_stream_row_wins_over_memory_duplicate(self):
        raw_dir = self.root / "raw_stream"
        raw_dir.mkdir()
        line = {"ts": "2026-10-01T20:01:00+07:00", "text": "Giá bao nhiêu?", "msg_id": "m1",
                "nickname": "Khách Một", "room_id": "room1", "event_type": "comment"}
        (raw_dir / "stream_20261001.jsonl").write_text(json.dumps(line, ensure_ascii=False) + "\n", encoding="utf-8")
        memory = self._make_memory()
        with mock.patch("workspace.LOG_DIR", self.root), mock.patch("workspace.MEMORY_DB", memory):
            result = self.store.list("conversations", {"view": "summary", "limit": 50})
        self.assertEqual(result["total"], 2)       # m1 không bị đếm hai lần
