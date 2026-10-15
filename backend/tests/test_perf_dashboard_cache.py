"""Cache/single-flight/đọc tiếp theo offset cho dashboard poll (F1-F6)."""
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

GOC = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(GOC / "backend" / "src"))

import knowledge
from captcha_bridge import CaptchaBridge
from customer_memory import CustomerMemory
from knowledge import KnowledgeBase, load_json
from workspace import WorkspaceStore

OLD = time.time() - 3600


def _write(path, text, mtime=OLD):
    path.write_text(text, encoding="utf-8")
    os.utime(path, (mtime, mtime))


class _Engine:
    def __init__(self, kb):
        self.knowledge = kb

    def mapping_state(self):
        return {"products": [], "profiles": []}


def _reference_rows(store, raw_dir):
    """Thuật toán cũ: đọc lại toàn bộ tệp."""
    rows = []
    import hashlib
    for path in sorted(raw_dir.glob("stream_*.jsonl")):
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue
            stable = str(item.get("msg_id") or f"{path.name}:{number}:{line}")
            rows.append({"id": hashlib.sha256(stable.encode("utf-8")).hexdigest()[:24],
                         "source_file": path.name, "source_line": number,
                         "_at": store._parse_at(item.get("ts")), **item})
    rows.sort(key=lambda row: (row["_at"], row["source_file"], row["source_line"]))
    return rows


class LoadJsonCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "catalog.json"

    def tearDown(self):
        self.temp.cleanup()

    def test_returns_independent_copies(self):
        _write(self.path, json.dumps({"items": [{"a": 1}]}))
        first = load_json(self.path, {})
        first["items"][0]["a"] = 99
        first["new"] = True
        second = load_json(self.path, {})
        self.assertEqual(second, {"items": [{"a": 1}]})
        second["items"].append(2)
        self.assertEqual(load_json(self.path, {}), {"items": [{"a": 1}]})

    def test_changed_file_refreshes_cache(self):
        _write(self.path, '{"v": 1}', OLD)
        self.assertEqual(load_json(self.path, {}), {"v": 1})
        _write(self.path, '{"v": 2}', OLD + 10)       # cùng cỡ, khác mtime
        self.assertEqual(load_json(self.path, {}), {"v": 2})
        _write(self.path, '{"v": 333}', OLD + 10)     # cùng mtime, khác cỡ
        self.assertEqual(load_json(self.path, {}), {"v": 333})

    def test_cache_hit_skips_parse_and_errors_are_not_cached(self):
        _write(self.path, '{"v": 1}')
        load_json(self.path, {})
        with mock.patch.object(knowledge.json, "loads", side_effect=AssertionError("parsed")):
            self.assertEqual(load_json(self.path, {}), {"v": 1})
        _write(self.path, "{broken", OLD + 20)
        self.assertEqual(load_json(self.path, {"d": 1}), {"d": 1})
        _write(self.path, '{"v": 5}', OLD + 30)
        self.assertEqual(load_json(self.path, {}), {"v": 5})

    def test_recently_written_file_is_always_reread(self):
        self.path.write_text('{"v": 1}', encoding="utf-8")
        self.assertEqual(load_json(self.path, {}), {"v": 1})
        stat = self.path.stat()
        self.path.write_text('{"v": 2}', encoding="utf-8")
        os.utime(self.path, ns=(stat.st_atime_ns, stat.st_mtime_ns))  # cùng mtime, cùng cỡ
        self.assertEqual(load_json(self.path, {}), {"v": 2})


class RawStreamOffsetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        catalog, profiles = self.root / "knowledge.json", self.root / "profiles.json"
        catalog.write_text('{"nhom_san_pham": []}', encoding="utf-8")
        profiles.write_text('{"products":{}}', encoding="utf-8")
        self.store = WorkspaceStore(_Engine(KnowledgeBase(catalog, profiles)), self.root)
        self.raw = self.root / "raw_stream"
        self.raw.mkdir()
        self.patch = mock.patch("workspace.LOG_DIR", self.root)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    @staticmethod
    def _line(i, msg=True):
        item = {"ts": f"2026-09-14T01:{i // 60 % 60:02d}:{i % 60:02d}+07:00", "text": f"câu {i}"}
        if msg:
            item["msg_id"] = f"m{i}"
        return json.dumps(item, ensure_ascii=False)

    def _check(self):
        self.assertEqual(self.store._read_raw_rows(), _reference_rows(self.store, self.raw))

    def test_appends_are_read_incrementally_and_match_full_read(self):
        path = self.raw / "stream_20260914.jsonl"
        path.write_text("\n".join(self._line(i, i % 3 != 0) for i in range(10)) + "\n", encoding="utf-8")
        self._check()
        offset = self.store._raw_files[str(path)]["offset"]
        self.assertEqual(offset, path.stat().st_size)
        with path.open("a", encoding="utf-8") as stream:
            stream.write("not json\n" + self._line(10, False) + "\n" + self._line(11))  # dòng dở dang
        real_open = Path.open
        reads = []

        def spy(self_path, *args, **kwargs):
            handle = real_open(self_path, *args, **kwargs)
            original = handle.read
            handle.read = lambda n=-1: reads.append(n) or original(n)
            return handle

        with mock.patch.object(Path, "open", spy):
            self._check()
        self.assertLess(max(n for n in reads if n is not None), path.stat().st_size)
        with path.open("a", encoding="utf-8") as stream:
            stream.write("\n" + self._line(12) + "\n")
        self._check()
        (self.raw / "stream_20260915.jsonl").write_text(self._line(13) + "\n", encoding="utf-8")
        self._check()

    def test_rewritten_or_truncated_file_is_reread(self):
        path = self.raw / "stream_20260914.jsonl"
        path.write_text("\n".join(self._line(i) for i in range(5)) + "\n", encoding="utf-8")
        self._check()
        path.write_text(self._line(50) + "\n", encoding="utf-8")       # ngắn hơn
        self._check()
        path.write_text("\n".join(self._line(i) for i in range(60, 70)) + "\n", encoding="utf-8")
        self._check()                                                    # dài hơn, đầu tệp khác
        path.unlink()
        self._check()
        self.assertEqual(self.store._raw_files, {})

    def test_returned_rows_are_copies(self):
        (self.raw / "stream_20260914.jsonl").write_text(self._line(1) + "\n", encoding="utf-8")
        rows = self.store._read_raw_rows()
        rows[0]["business_session_id"] = "x"
        self.assertNotIn("business_session_id", self.store._read_raw_rows()[0])


class SingleFlightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        catalog, profiles = root / "knowledge.json", root / "profiles.json"
        catalog.write_text('{"nhom_san_pham": []}', encoding="utf-8")
        profiles.write_text('{"products":{}}', encoding="utf-8")
        self.store = WorkspaceStore(_Engine(KnowledgeBase(catalog, profiles)), root)

    def tearDown(self):
        self.temp.cleanup()

    def test_concurrent_caller_gets_previous_result_while_one_builds(self):
        calls, started, release = [], threading.Event(), threading.Event()

        def builder():
            calls.append(1)
            if len(calls) == 2:
                started.set()
                release.wait(5)
            return [len(calls)]

        self.assertEqual(self.store._memo_build(("k",), builder), [1])
        self.store.MEMO_TTL_S = -1          # hết hạn ngay
        worker = threading.Thread(target=lambda: self.store._memo_build(("k",), builder))
        worker.start()
        self.assertTrue(started.wait(5))
        began = time.monotonic()
        self.assertEqual(self.store._memo_build(("k",), builder), [1])   # bản cũ, không chờ
        self.assertLess(time.monotonic() - began, 1)
        release.set()
        worker.join(5)
        self.assertEqual(len(calls), 2)

    def test_invalidate_drops_stale_result(self):
        self.store._memo_build(("k",), lambda: [1])
        self.store.invalidate_memo()
        self.assertEqual(self.store._memo_build(("k",), lambda: [2]), [2])


class TranscriptCountTests(unittest.TestCase):
    def test_count_is_memoised_and_refreshes_on_change(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "transcript.json"
            _write(path, json.dumps({"segments": [1, 2]}))
            self.assertEqual(WorkspaceStore._transcript_segments(path), 2)
            with mock.patch("workspace.json.loads", side_effect=AssertionError("parsed")):
                self.assertEqual(WorkspaceStore._transcript_segments(path), 2)
            _write(path, json.dumps({"segments": [1, 2, 3]}), OLD + 5)
            self.assertEqual(WorkspaceStore._transcript_segments(path), 3)
            self.assertEqual(WorkspaceStore._transcript_segments(Path(temp) / "none.json"), 0)


class MemorySummaryCacheTests(unittest.TestCase):
    def test_cached_summary_uses_ttl_and_returns_copies(self):
        with tempfile.TemporaryDirectory() as temp:
            memory = CustomerMemory(Path(temp) / "m.sqlite3", Path(temp) / "j")
            try:
                memory.record_comment("chào shop", comment_id="c1", data_source="live",
                                      platform_user_id="u1")
                first = memory.data_summary(max_age=60)
                self.assertEqual(first["interactions"], 1)
                first["data_sources"]["bad"] = 1
                memory.record_comment("còn hàng?", comment_id="c2", data_source="live",
                                      platform_user_id="u2")
                cached = memory.data_summary(max_age=60)
                self.assertEqual(cached["interactions"], 1)          # trễ trong TTL
                self.assertNotIn("bad", cached["data_sources"])
                self.assertEqual(memory.data_summary()["interactions"], 2)   # không cache
                memory._summary_cache = (time.monotonic() - 120, cached)
                self.assertEqual(memory.data_summary(max_age=60), memory.data_summary())
            finally:
                memory.close()

    def test_summary_is_computed_outside_shared_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            memory = CustomerMemory(Path(temp) / "m.sqlite3", Path(temp) / "j")
            try:
                memory.record_comment("a", comment_id="c1", platform_user_id="u1")
                result = {}
                with memory._lock:   # đường trả lời đang giữ khoá chung
                    worker = threading.Thread(
                        target=lambda: result.update(memory.data_summary(max_age=30)))
                    worker.start()
                    worker.join(5)
                    self.assertFalse(worker.is_alive())
                self.assertEqual(result["interactions"], 1)
            finally:
                memory.close()


class CaptchaStatusCacheTests(unittest.TestCase):
    def test_status_is_cached_and_invalidated(self):
        with tempfile.TemporaryDirectory() as temp:
            bridge = CaptchaBridge(temp)
            calls = []

            def uncached():
                calls.append(1)
                return {"configured": True, "running": False, "events": []}

            with mock.patch.object(bridge, "_status_uncached", side_effect=uncached):
                first = bridge.status()
                first["events"].append("x")
                self.assertEqual(bridge.status()["events"], [])
                self.assertEqual(len(calls), 1)
                bridge.status(fresh=True)
                self.assertEqual(len(calls), 2)
                bridge.invalidate_status()
                bridge.status()
                self.assertEqual(len(calls), 3)
                bridge._status_cache = (time.monotonic() - 6, bridge._status_cache[1])
                bridge.status()
                self.assertEqual(len(calls), 4)


def _deepseek():
    spec = importlib.util.spec_from_file_location(
        "_plugin_model_deepseek_perf", GOC / "plugins" / "model-deepseek" / "processor.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    responder = module.DeepSeekResponder.__new__(module.DeepSeekResponder)
    responder.client, responder.api_key = object(), "k"
    responder._balance_lock = threading.Lock()
    responder._balance_checked_at, responder._balance_status = 0.0, None
    return module, responder


class BalanceNoWaitTests(unittest.TestCase):
    def test_lock_not_held_during_network_and_dashboard_never_waits(self):
        module, responder = _deepseek()
        entered, release = threading.Event(), threading.Event()
        responder._balance_status = {"ok": True, "deepseek_available": True, "old": True}
        responder._balance_checked_at = time.monotonic() - 100

        def slow_fetch(api_key):
            entered.set()
            release.wait(5)
            return {"ok": True, "deepseek_available": True}

        with mock.patch.object(responder, "_balance_fetch", side_effect=slow_fetch):
            began = time.monotonic()
            first = responder.check_balance(wait=False)
            self.assertTrue(first["old"])                      # giá trị cũ, không chờ
            self.assertTrue(entered.wait(5))
            self.assertFalse(responder._balance_lock.locked())  # không giữ khoá khi gọi mạng
            self.assertTrue(responder.check_balance()["old"])   # luồng khác: dùng bản cũ
            self.assertLess(time.monotonic() - began, 1)
            release.set()
            for _ in range(100):
                if not getattr(responder, "_balance_inflight", False):
                    break
                time.sleep(0.02)
            self.assertNotIn("old", responder.check_balance())

    def test_without_cached_value_dashboard_returns_checking_status(self):
        module, responder = _deepseek()
        responder.BALANCE_NOWAIT_S = 0.05
        release = threading.Event()
        with mock.patch.object(responder, "_balance_fetch",
                               side_effect=lambda key: release.wait(5) and {"ok": True}):
            status = responder.check_balance(wait=False)
            self.assertEqual(status["reason"], "deepseek_balance_checking")
            self.assertFalse(status["ok"])
            release.set()


if __name__ == "__main__":
    unittest.main()
