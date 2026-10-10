import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock


SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from knowledge import KnowledgeBase  # noqa: E402
from workspace import WorkspaceStore  # noqa: E402


class FakeEngine:
    def __init__(self, knowledge):
        self.knowledge = knowledge
        self.extension_heartbeat = {}

    def mapping_state(self):
        return {"products": [{
            "product_id": "sku-1", "bag_index": 1, "title": "Đạm cá 5L",
            "profile_name": "Đạm cá", "seller_sku": "DAM5L", "price": "555k",
        }]}

    def video_analysis_jobs(self):
        return {"jobs": []}

    def plugin_state(self):
        return {"plugins": []}

    def settings_state(self):
        return {"audio_devices": []}

    def diagnostics_state(self):
        return {"checks": []}

    def current_video_position(self):
        return {"state": "waiting", "video_id": ""}


class WorkspaceStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        catalog = self.root / "knowledge.json"
        profiles = self.root / "profiles.json"
        catalog.write_text(json.dumps({"nhom_san_pham": [{"san_pham": [{
            "san_pham_id": "SP-01", "ten_san_pham": "Đạm cá 5L",
            "cong_dung": ["Nuôi cây"], "approved_for_live": True,
        }]}]}, ensure_ascii=False), encoding="utf-8")
        profiles.write_text('{"products":{}}', encoding="utf-8")
        self.knowledge = KnowledgeBase(catalog, profiles)
        self.store = WorkspaceStore(FakeEngine(self.knowledge), self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_catalog_exposes_capabilities_and_paginates(self):
        catalog = self.store.catalog()
        self.assertTrue(catalog["ok"])
        self.assertTrue(any(row["id"] == "profiles" and row["editable"]
                            for row in catalog["resources"]))
        result = self.store.list("profiles", {"limit": 50})
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["id"], "SP-01")

    def test_profile_edit_is_local_override_with_revision_and_rollback(self):
        detail = self.store.detail("profiles", "SP-01")
        changed = self.store.patch("profiles", "SP-01", {
            "expected_revision": detail["revision"],
            "changes": {"ten_san_pham": "Đạm cá can 5 lít"},
            "reason": "Đối chiếu nhãn",
        })
        self.assertEqual(changed["data"]["ten_san_pham"], "Đạm cá can 5 lít")
        original = json.loads(self.knowledge.catalog_path.read_text(encoding="utf-8"))
        self.assertEqual(original["nhom_san_pham"][0]["san_pham"][0]["ten_san_pham"],
                         "Đạm cá 5L")
        self.assertTrue(self.store.override_path.exists())
        rolled = self.store.rollback("profiles", "SP-01")
        self.assertEqual(rolled["data"]["ten_san_pham"], "Đạm cá 5L")
        self.assertEqual(len(self.store.detail("profiles", "SP-01")["history"]), 2)

    def test_revision_conflict_does_not_write(self):
        with self.assertRaisesRegex(ValueError, "revision_conflict"):
            self.store.patch("profiles", "SP-01", {
                "expected_revision": "stale", "changes": {"ten_san_pham": "Sai"},
            })
        self.assertFalse(self.store.override_path.exists())

    def test_recent_activity_uses_business_labels(self):
        before = self.store.detail("profiles", "SP-01")
        self.store.patch("profiles", "SP-01", {
            "expected_revision": before["revision"],
            "changes": {"ten_san_pham": "Đạm cá mới"},
            "reason": "Đổi tên sản phẩm",
        })
        activity = self.store.recent_activity()
        self.assertEqual(activity[0]["label"], "Đã cập nhật")
        self.assertEqual(activity[0]["description"], "Đổi tên sản phẩm")
        self.assertNotIn("revision", activity[0])

    def test_correction_preserves_raw_record_and_adds_effective_view(self):
        raw_dir = self.root / "raw_stream"
        raw_dir.mkdir()
        line = {"ts": "2026-09-14T01:00:00+07:00", "text": "đạm cá", "event_type": "comment"}
        (raw_dir / "stream_20260914.jsonl").write_text(
            json.dumps(line, ensure_ascii=False) + "\n", encoding="utf-8")
        with mock.patch("workspace.LOG_DIR", self.root):
            record = self.store.list("raw-events")["items"][0]
            self.store.correct({
                "resource": "raw-events", "record_id": record["id"],
                "changes": {"event_type": "product_question"}, "reason": "Gắn nhãn thủ công",
            })
            corrected = self.store.detail("raw-events", record["id"])["data"]
        self.assertEqual(corrected["event_type"], "comment")
        self.assertEqual(corrected["effective"]["event_type"], "product_question")
        self.assertEqual(json.loads((raw_dir / "stream_20260914.jsonl").read_text(encoding="utf-8")), line)

    def test_conversations_join_ai_reply_and_ignore_echo_without_double_counting(self):
        raw_dir = self.root / "raw_stream"
        raw_dir.mkdir()
        rows = [
            {"ts": "2026-09-14T01:00:00+07:00", "msg_id": "c1", "text": "đạm cá giá bao nhiêu",
             "unique_id": "anh-a", "nickname": "Anh A", "room_id": "room-1", "ai_action": "queued_for_ai"},
            {"ts": "2026-09-14T01:00:05+07:00", "msg_id": "r1", "text": "@anh-a dạ giá...",
             "is_host": True, "nickname": "Kid TE (AI)", "room_id": "room-1",
             "reply_to_username": "anh-a", "ai_action": "ai_replied"},
            {"ts": "2026-09-14T01:00:07+07:00", "msg_id": "echo", "text": "@anh-a dạ giá...",
             "is_host": True, "nickname": "Shop", "room_id": "room-1", "ai_action": "self_echo"},
        ]
        (raw_dir / "stream_20260914.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
        with mock.patch("workspace.LOG_DIR", self.root), \
             mock.patch.object(self.store, "_memory_by_comment", return_value={}):
            result = self.store.list("conversations")
            sessions = self.store.list("sessions")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["responder"], "ai")
        self.assertEqual(result["items"][0]["reply_text"], "@anh-a dạ giá...")
        self.assertEqual(sessions["items"][0]["comment_count"], 1)
        self.assertEqual(sessions["items"][0]["ai_reply_count"], 1)

    def test_only_approved_example_is_retrieved_and_can_be_disabled(self):
        raw_dir = self.root / "raw_stream"
        raw_dir.mkdir()
        source = {"ts": "2026-09-14T01:00:00+07:00", "msg_id": "c2",
                  "text": "đạm cá 5 lít giá bao nhiêu", "unique_id": "khach"}
        (raw_dir / "stream_20260914.jsonl").write_text(
            json.dumps(source, ensure_ascii=False) + "\n", encoding="utf-8")
        with mock.patch("workspace.LOG_DIR", self.root):
            row = self.store.list("conversations")["items"][0]
            self.assertEqual(self.store.reviewed_examples(source["text"]), [])
            self.store.approve_reply_example(row["id"], {
                "approved_answer": "Dạ anh xem giá tại giỏ số 1 giúp em.",
                "question_type": "Giá và khuyến mãi", "product_id": "sku-1",
                "reason": "Câu trả lời đúng đã kiểm tra",
            })
            chosen = self.store.reviewed_examples(
                "giá đạm cá can 5 lít", "Giá và khuyến mãi", "sku-1")
            self.assertEqual(len(chosen), 1)
            self.store.disable_reply_example(row["id"])
            self.assertEqual(self.store.reviewed_examples(
                "giá đạm cá can 5 lít", "Giá và khuyến mãi", "sku-1"), [])

    def test_secret_text_is_never_stored_in_audit_schema(self):
        with closing(sqlite3.connect(self.store.db_path)) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(operator_changes)")}
        self.assertNotIn("api_key", columns)
        self.assertNotIn("secret", columns)

    def test_video_duplicate_uses_fingerprint_or_normalized_source_name(self):
        videos = [
            {"video_id": "live1", "timeline": {"source_file": "live1.mp4"},
             "fingerprint": None, "errors": []},
            {"video_id": "live2", "timeline": {"source_file": "live 2.mp4"},
             "fingerprint": None, "errors": []},
            {"video_id": "video-20260914", "timeline": {"source_file": "live-2.mp4"},
             "fingerprint": None, "errors": []},
        ]
        video_dir = self.root / "videos"
        video_dir.mkdir()
        with mock.patch("workspace.list_videos", return_value=videos), \
             mock.patch("workspace.VIDEO_DIR", video_dir):
            result = self.store.list("videos")
        by_id = {row["id"]: row for row in result["items"]}
        self.assertEqual(by_id["live2"]["duplicates"], ["video-20260914"])
        self.assertEqual(by_id["video-20260914"]["duplicates"], ["live2"])
        self.assertEqual(by_id["live2"]["duplicate_reason"], "normalized_source_name")
        self.assertEqual(result["items"][0]["id"], "live2")
        with mock.patch("workspace.list_videos", return_value=videos), \
             mock.patch("workspace.VIDEO_DIR", video_dir):
            kept = self.store.resolve_video_duplicate("live2", {
                "other_id": "video-20260914", "action": "keep_both",
            })
            self.assertEqual(kept["decision"], "keep_both")
            resolved = {row["id"]: row for row in self.store.list("videos")["items"]}
        self.assertEqual(resolved["live2"]["duplicates"], [])

    def test_video_duplicate_merge_rolls_back_both_videos_on_copy_failure(self):
        video_dir = self.root / "videos"
        keep_dir, other_dir = video_dir / "keep", video_dir / "other"
        keep_dir.mkdir(parents=True)
        other_dir.mkdir(parents=True)
        keep_timeline = {
            "video_id": "keep", "title": "Giữ", "source_file": "live 2.mp4",
            "cycle_ms": 60000, "repin_every_ms": 20000,
            "products": {"p": {"bag_index": 1, "include_all": ["p"]}},
            "events": [{"at_ms": 0, "product": "p"}], "script_contexts": [],
        }
        other_timeline = {
            **keep_timeline, "video_id": "other", "title": "Bản kia",
            "source_file": "live-2.mp4", "source_sha256": "abc",
        }
        (keep_dir / "timeline.json").write_text(json.dumps(keep_timeline), encoding="utf-8")
        (other_dir / "timeline.json").write_text(json.dumps(other_timeline), encoding="utf-8")
        (other_dir / "transcript.json").write_text('{"segments":[]}', encoding="utf-8")

        def listed(check_fingerprint=True):
            rows = []
            for folder in (keep_dir, other_dir):
                if folder.exists():
                    timeline = json.loads((folder / "timeline.json").read_text(encoding="utf-8"))
                    rows.append({"video_id": folder.name, "timeline": timeline,
                                 "fingerprint": None, "errors": []})
            return rows

        original = json.loads((keep_dir / "timeline.json").read_text(encoding="utf-8"))
        with mock.patch("workspace.VIDEO_DIR", video_dir), \
             mock.patch("workspace.list_videos", side_effect=listed), \
             mock.patch("workspace.shutil.copy2", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                self.store.resolve_video_duplicate("keep", {
                    "other_id": "other", "action": "merge",
                })
        self.assertTrue(other_dir.exists())
        self.assertEqual(json.loads((keep_dir / "timeline.json").read_text(encoding="utf-8")), original)
        self.assertFalse((keep_dir / "transcript.json").exists())

    def test_delete_moves_to_30_day_trash_and_restore_reveals_record(self):
        before = self.store.detail("profiles", "SP-01")
        deleted = self.store.delete("profiles", "SP-01", {
            "expected_revision": before["revision"],
        })
        self.assertTrue(deleted["undo_available"])
        self.assertEqual(self.store.list("profiles")["total"], 0)
        trash = self.store.trash()["items"]
        self.assertEqual(len(trash), 1)
        deleted_at = __import__("datetime").datetime.fromisoformat(trash[0]["deleted_at"])
        expires_at = __import__("datetime").datetime.fromisoformat(trash[0]["expires_at"])
        self.assertEqual((expires_at - deleted_at).days, 30)
        restored = self.store.restore("profiles", "SP-01")
        self.assertTrue(restored["restored"])
        self.assertEqual(self.store.list("profiles")["total"], 1)

    def test_legacy_trash_record_uses_friendly_name_without_exposing_snapshot(self):
        record_id = "5065e5a9203e41a5b3ddb5d7f502ecab"
        with self.store._db() as db:
            db.execute(
                "INSERT INTO trash_items "
                "(trash_id,resource,record_id,deleted_at,expires_at,label,data_json,snapshot_id) "
                "VALUES (?,?,?,?,?,?,?,?)",
                ("trash-1", "video-jobs", record_id,
                 "2026-09-14T10:00:00+07:00", "2026-10-14T10:00:00+07:00",
                 record_id, json.dumps({"filename": "video mới.mp4"}), "snapshot-1"),
            )
        item = self.store.trash()["items"][0]
        self.assertEqual(item["label"], "video mới.mp4")
        self.assertNotIn("data_json", item)

    def test_correction_can_be_undone_without_changing_original(self):
        raw_dir = self.root / "raw_stream"
        raw_dir.mkdir()
        source = {"ts": "2026-09-14T01:00:00+07:00", "text": "dam ca"}
        path = raw_dir / "stream_20260914.jsonl"
        path.write_text(json.dumps(source) + "\n", encoding="utf-8")
        with mock.patch("workspace.LOG_DIR", self.root):
            row = self.store.list("raw-events")["items"][0]
            out = self.store.correct({"resource": "raw-events", "record_id": row["id"],
                                      "changes": {"text": "đạm cá"}, "reason": "Sửa dấu"})
            self.assertEqual(self.store.detail("raw-events", row["id"])["data"]["effective"]["text"], "đạm cá")
            self.store.undo_correction(out["correction_id"])
            after = self.store.detail("raw-events", row["id"])["data"]
        self.assertEqual(after["effective"]["text"], "dam ca")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), source)

    def test_video_draft_is_separate_from_applied_timeline(self):
        video_dir = self.root / "videos"
        folder = video_dir / "v1"
        folder.mkdir(parents=True)
        timeline = {"video_id": "v1", "title": "Video", "cycle_ms": 60000,
                    "repin_every_ms": 20000,
                    "products": {"p": {"bag_index": 1, "include_all": ["p"]}},
                    "events": [{"at_ms": 0, "product": "p"}], "script_contexts": []}
        (folder / "timeline.json").write_text(json.dumps(timeline), encoding="utf-8")
        listed = [{"video_id": "v1", "timeline": timeline,
                   "fingerprint": None, "errors": []}]
        with mock.patch("workspace.list_videos", return_value=listed), \
             mock.patch("workspace.VIDEO_DIR", video_dir):
            detail = self.store.detail("videos", "v1")
            draft = {**timeline, "title": "Bản nháp", "events": timeline["events"]}
            self.store.save_video_draft("v1", {
                "expected_revision": detail["revision"], "draft": draft,
            })
            reopened = self.store.detail("videos", "v1")
        self.assertEqual(reopened["data"]["saved_draft"]["title"], "Bản nháp")
        self.assertEqual(reopened["data"]["timeline"]["title"], "Video")

    def test_two_windows_cannot_overwrite_same_draft_revision(self):
        from concurrent.futures import ThreadPoolExecutor
        second = WorkspaceStore(FakeEngine(self.knowledge), self.root)
        payload = {'expected_revision': 'live', 'expected_draft_revision': 0,
                   'draft': {'events': [], 'script_contexts': []}}
        def save(store):
            try:
                return store.save_video_draft('v1', payload)
            except ValueError as error:
                return str(error)
        with mock.patch.object(self.store, 'detail', return_value={'revision': 'live'}), \
             mock.patch.object(second, 'detail', return_value={'revision': 'live'}):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(save, [self.store, second]))
            self.assertEqual(results.count('revision_conflict'), 1)
            winner = next(row for row in results if isinstance(row, dict))
            self.assertEqual(winner['draft_revision'], 1)
            discarded = self.store.save_video_draft('v1', {'expected_revision': 'live',
                'expected_draft_revision': 1, 'discard': True})
            self.assertEqual(discarded['draft_revision'], 2)
            with self.assertRaisesRegex(ValueError, 'revision_conflict'):
                second.save_video_draft('v1', payload)
        with self.store._db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM video_drafts').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT revision FROM video_draft_revisions').fetchone()[0], 2)

    def test_publishing_invalidates_pending_autosave_revision(self):
        video_dir = self.root / 'videos'
        folder = video_dir / 'v1'
        folder.mkdir(parents=True)
        path = folder / 'timeline.json'
        timeline = {'video_id': 'v1', 'title': 'Old', 'cycle_ms': 60000,
                    'repin_every_ms': 20000, 'products': {'p': {'bag_index': 1, 'include_all': ['p']}},
                    'events': [{'at_ms': 0, 'product': 'p'}], 'script_contexts': []}
        path.write_text(json.dumps(timeline), encoding='utf-8')
        def listed(check_fingerprint=True):
            return [{'video_id': 'v1', 'timeline': json.loads(path.read_text(encoding='utf-8')),
                     'fingerprint': None, 'errors': []}]
        with mock.patch('workspace.VIDEO_DIR', video_dir), mock.patch('workspace.list_videos', side_effect=listed):
            detail = self.store.detail('videos', 'v1')
            draft = self.store.save_video_draft('v1', {'expected_revision': detail['revision'],
                'expected_draft_revision': 0, 'draft': timeline})
            published = self.store.patch('videos', 'v1', {'expected_revision': detail['revision'],
                'changes': {'title': 'Published'}})
            assert published['data']['draft_revision'] == draft['draft_revision'] + 1
            with self.assertRaisesRegex(ValueError, 'revision_conflict'):
                self.store.save_video_draft('v1', {'expected_revision': published['revision'],
                    'expected_draft_revision': draft['draft_revision'], 'draft': timeline})
            with self.assertRaisesRegex(ValueError, 'revision_conflict'):
                self.store.patch('videos', 'v1', {'expected_revision': detail['revision'],
                    'changes': {'title': 'Stale'}})
            assert self.store.detail('videos', 'v1')['data'].get('saved_draft') is None

    def test_draft_revision_migration_preserves_existing_draft_and_checked_backup(self):
        with self.store._db() as db:
            db.execute('DROP TABLE video_draft_revisions')
            db.execute('INSERT INTO video_drafts VALUES (?,?,?)', ('v1', 'now', '{"events":[]}'))
        WorkspaceStore(FakeEngine(self.knowledge), self.root)
        backups = list((self.root / 'data' / 'schema_backups').glob('workspace-before-draft-v2-*.sqlite3'))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as db:
            self.assertEqual(db.execute('PRAGMA quick_check').fetchone()[0], 'ok')
            self.assertEqual(db.execute('SELECT data_json FROM video_drafts').fetchone()[0], '{"events":[]}')
        WorkspaceStore(FakeEngine(self.knowledge), self.root)
        self.assertEqual(len(list(backups[0].parent.glob('workspace-before-draft-v2-*.sqlite3'))), 1)

    def test_fingerprint_poll_reuses_hash_until_file_changes_and_cache_is_bounded(self):
        import os
        fingerprint = self.root / 'fingerprint.npz'
        fingerprint.write_bytes(b'old reference')
        reads = []
        original_open = Path.open
        def tracked(path, *args, **kwargs):
            if path == fingerprint and args and args[0] == 'rb': reads.append(path)
            return original_open(path, *args, **kwargs)
        with mock.patch.object(Path, 'open', tracked):
            first = self.store._fingerprint_hash(fingerprint)
            for _ in range(30): self.assertEqual(self.store._fingerprint_hash(fingerprint), first)
            self.assertEqual(len(reads), 1)
            previous = fingerprint.stat().st_mtime_ns
            fingerprint.write_bytes(b'new reference')
            os.utime(fingerprint, ns=(previous + 1000000000, previous + 1000000000))
            self.assertNotEqual(self.store._fingerprint_hash(fingerprint), first)
            self.assertEqual(len(reads), 2)
        for index in range(257):
            path = self.root / f'fingerprint-{index}.npz'
            path.write_bytes(b'reference')
            self.store._fingerprint_hash(path)
        self.assertEqual(len(self.store._fingerprint_hashes), 256)

    def test_trashed_video_stops_being_available_and_restore_moves_it_back(self):
        video_dir = self.root / "data" / "videos"
        folder = video_dir / "v1"
        folder.mkdir(parents=True)
        timeline = {"video_id": "v1", "title": "Video", "cycle_ms": 60000,
                    "products": {"p": {"bag_index": 1, "include_all": ["p"]}},
                    "events": [{"at_ms": 0, "product": "p"}]}
        (folder / "timeline.json").write_text(json.dumps(timeline), encoding="utf-8")

        def listed(check_fingerprint=True):
            return ([{"video_id": "v1", "timeline": timeline,
                      "fingerprint": None, "errors": []}] if folder.exists() else [])

        with mock.patch("workspace.VIDEO_DIR", video_dir), \
             mock.patch("workspace.list_videos", side_effect=listed):
            detail = self.store.detail("videos", "v1")
            self.store.delete("videos", "v1", {"expected_revision": detail["revision"]})
            self.assertFalse(folder.exists())
            self.assertEqual(self.store.list("videos")["total"], 0)
            self.store.restore("videos", "v1")
            self.assertTrue(folder.exists())
            self.assertEqual(self.store.list("videos")["total"], 1)

    def test_purged_item_never_reappears_after_expiry(self):
        self.store.delete("profiles", "SP-01")
        with closing(sqlite3.connect(self.store.db_path)) as db:
            db.execute("UPDATE trash_items SET expires_at='2000-01-01T00:00:00+00:00'")
            db.commit()
        self.assertEqual(self.store.purge_expired()["purged"], 1)
        self.assertEqual(self.store.list("profiles")["total"], 0)
        self.assertEqual(self.store.trash()["total"], 0)


if __name__ == "__main__":
    unittest.main()


class WorkspaceMemoTests(unittest.TestCase):
    """Bộ nhớ đệm bình luận: nhanh ở lượt lặp, nhưng KHÔNG BAO GIỜ trả dữ liệu cũ."""

    setUp = WorkspaceStoreTests.setUp
    tearDown = WorkspaceStoreTests.tearDown

    def _write(self, rows):
        raw_dir = self.root / "raw_stream"
        raw_dir.mkdir(exist_ok=True)
        (raw_dir / "stream_20260914.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")

    def test_binh_luan_moi_lam_moi_bo_nho_dem(self):
        base = {"ts": "2026-09-14T01:00:00+07:00", "text": "đạm cá", "unique_id": "a", "room_id": "r"}
        self._write([{**base, "msg_id": "1"}])
        # Cô lập khỏi file dữ liệu THẬT (app đang chạy ghi vào chúng làm chữ ký đổi).
        with mock.patch("workspace.LOG_DIR", self.root), \
             mock.patch("workspace.MEMORY_DB", self.root / "memory.sqlite3"), \
             mock.patch("workspace.LIVE_CATALOG_PATH", self.root / "live.json"):
            self.assertEqual(self.store.list("conversations")["total"], 1)
            with mock.patch.object(self.store, "_raw_events_uncached", side_effect=AssertionError("phải dùng bộ nhớ đệm")):
                self.assertEqual(self.store.list("conversations")["total"], 1)
            self._write([{**base, "msg_id": "1"}, {**base, "msg_id": "2", "text": "giá sao em"}])
            self.assertEqual(self.store.list("conversations")["total"], 2)

    def test_sua_du_lieu_xoa_bo_nho_dem(self):
        self._write([{"ts": "2026-09-14T01:00:00+07:00", "msg_id": "1", "text": "đạm cá", "unique_id": "a", "room_id": "r"}])
        with mock.patch("workspace.LOG_DIR", self.root):
            row = self.store.list("conversations")["items"][0]
            self.store.correct({"resource": "conversations", "record_id": row["id"],
                                "changes": {"conversation_status": "answered"}, "reason": "đã xử lý"})
            after = self.store.list("conversations", {"view": "summary"})["items"][0]
        self.assertEqual(after["conversation_status"], "answered")
        self.assertNotIn("effective", after)
        self.assertNotIn("previous_customer_comments", after)
        self.assertTrue(after["has_corrections"])
