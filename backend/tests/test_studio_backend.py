"""Hành vi backend mà Studio video mới dựa vào.

Không grep chuỗi trong mã nguồn: mọi test ở đây gọi đúng hàm/đường API mà
giao diện gọi và kiểm tra dữ liệu thật được ghi ra.
"""
import io
import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from knowledge import KnowledgeBase  # noqa: E402
from workspace import WorkspaceStore  # noqa: E402
import media_assets  # noqa: E402


class Engine:
    def __init__(self, knowledge, products):
        self.knowledge = knowledge
        self.extension_heartbeat = {}
        self._products = products

    def mapping_state(self):
        return {"products": self._products}

    def video_analysis_jobs(self):
        return {"jobs": []}

    def current_video_position(self):
        return {"state": "waiting", "video_id": ""}


class StudioTimelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        catalog = self.root / "knowledge.json"
        catalog.write_text(json.dumps({"nhom_san_pham": [{"san_pham": []}]}), encoding="utf-8")
        profiles = self.root / "profiles.json"
        profiles.write_text('{"products":{}}', encoding="utf-8")
        self.products = [
            {"product_id": "111", "bag_index": 1, "title": "Kích Rễ TC01 - chai 100ml"},
            {"product_id": "222", "bag_index": 7, "title": "Đạm Cá Amino 1L"},
        ]
        self.store = WorkspaceStore(Engine(KnowledgeBase(catalog, profiles), self.products), self.root)
        self.video_dir = self.root / "videos"
        folder = self.video_dir / "v1"
        folder.mkdir(parents=True)
        self.timeline = {
            "schema_version": 1, "video_id": "v1", "title": "Live", "cycle_ms": 120000,
            "repin_every_ms": 20000,
            "products": {"sku_111": {"product_id": "111", "bag_index": 3, "short_name": "TC01",
                                     "title": "Kích Rễ TC01 - chai 100ml",
                                     "include_all": ["kich re tc01"], "exclude_any": []}},
            "events": [{"at_ms": 0, "product": "sku_111"}],
            "script_contexts": [{"at_ms": 0, "topic": "TC01", "facts": []}],
        }
        (folder / "timeline.json").write_text(json.dumps(self.timeline, ensure_ascii=False), encoding="utf-8")
        self.patches = [mock.patch("workspace.VIDEO_DIR", self.video_dir),
                        mock.patch("video_library.VIDEO_DIR", self.video_dir)]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in self.patches:
            patcher.stop()
        self.temp.cleanup()

    def timeline_on_disk(self):
        return json.loads((self.video_dir / "v1" / "timeline.json").read_text(encoding="utf-8"))

    def test_ghim_san_pham_chua_co_trong_timeline_tu_danh_muc(self):
        detail = self.store.detail("videos", "v1")
        self.store.patch("videos", "v1", {
            "expected_revision": detail["revision"],
            "changes": {"events": [{"at_ms": 0, "product": "sku_111"}, {"at_ms": 60000, "product": "sku_222"}],
                        "script_contexts": [{"at_ms": 0, "topic": "", "facts": []}, {"at_ms": 60000, "topic": "Đạm", "facts": []}]},
            "reason": "test",
        })
        saved = self.timeline_on_disk()
        self.assertEqual(saved["products"]["sku_222"]["bag_index"], 7)
        self.assertEqual(saved["products"]["sku_222"]["include_all"], ["dam ca amino 1l"])
        # Không yêu cầu đồng bộ thì mục cũ giữ nguyên số giỏ cũ
        self.assertEqual(saved["products"]["sku_111"]["bag_index"], 3)

    def test_dong_bo_so_gio_theo_danh_muc_hom_nay(self):
        detail = self.store.detail("videos", "v1")
        self.store.patch("videos", "v1", {"expected_revision": detail["revision"],
                                          "changes": {"sync_products": True}, "reason": "đổi giỏ"})
        saved = self.timeline_on_disk()
        self.assertEqual(saved["products"]["sku_111"]["bag_index"], 1)
        self.assertNotIn("sync_products", saved)

    def test_revision_tra_ve_dung_de_luu_lien_tiep(self):
        detail = self.store.detail("videos", "v1")
        first = self.store.patch("videos", "v1", {"expected_revision": detail["revision"],
                                                  "changes": {"title": "Lần 1"}, "reason": "a"})
        self.assertEqual(first["revision"], self.store.detail("videos", "v1")["revision"])
        second = self.store.patch("videos", "v1", {"expected_revision": first["revision"],
                                                   "changes": {"title": "Lần 2"}, "reason": "b"})
        self.assertTrue(second["ok"])
        self.assertEqual(self.timeline_on_disk()["title"], "Lần 2")

    def test_ban_nhap_luu_mo_lai_va_bo_duoc(self):
        detail = self.store.detail("videos", "v1")
        self.store.save_video_draft("v1", {"expected_revision": detail["revision"],
                                           "draft": {**self.timeline, "title": "Nháp"}})
        rows = {row["id"]: row for row in self.store.list("videos", {})["items"]}
        self.assertTrue(rows["v1"]["draft_saved_at"])
        self.assertEqual(self.store.detail("videos", "v1")["data"]["saved_draft"]["title"], "Nháp")
        out = self.store.save_video_draft("v1", {"expected_revision": detail["revision"], "discard": True})
        self.assertTrue(out["discarded"])
        self.assertNotIn("saved_draft", self.store.detail("videos", "v1")["data"])

    def test_luan_phien_luu_lien_tiep_khong_xung_dot_revision(self):
        job_id = "job-review"
        job_root = self.root / "video_uploads"
        folder = job_root / job_id
        folder.mkdir(parents=True)
        draft = {"cycle_ms": 120000, "events": [
            {"at_ms": 0, "product_id": "111", "review_status": "approved"},
        ]}
        (folder / "draft.json").write_text(json.dumps(draft), encoding="utf-8")
        self.store.engine.video_analysis = SimpleNamespace(root=job_root)
        self.store.engine.video_analysis_status = lambda _job_id, include_draft=False: {
            "job_id": job_id, "state": "review", "duration_s": 120,
            "draft": json.loads((folder / "draft.json").read_text(encoding="utf-8")),
        }

        first_revision = self.store.video_job_detail(job_id)["revision"]
        draft["events"][0].update(alternate_product_id="222", alternate_every_ms=60000)
        first = self.store.save_video_job_draft(job_id, {
            "expected_revision": first_revision, "draft": draft,
        })
        self.assertEqual(first["revision"], self.store.video_job_detail(job_id)["revision"])
        self.assertEqual(first["draft"]["events"][0]["context_before"], "")

        draft["events"][0]["alternate_every_ms"] = 30000
        second = self.store.save_video_job_draft(job_id, {
            "expected_revision": first["revision"], "draft": draft,
        })
        self.assertTrue(second["saved"])
        self.assertEqual(second["revision"], self.store.video_job_detail(job_id)["revision"])

    def test_san_pham_khong_co_trong_danh_muc_bi_timeline_tu_choi(self):
        detail = self.store.detail("videos", "v1")
        with self.assertRaises(ValueError):
            self.store.patch("videos", "v1", {"expected_revision": detail["revision"],
                                              "changes": {"events": [{"at_ms": 0, "product": "sku_999"}]},
                                              "reason": "x"})


class PublishRouteTests(unittest.TestCase):
    def test_server_goi_publish_dung_chu_ky_va_revision(self):
        from server import ReplyEngine
        calls = {}

        def publish(job_id, title, events, *, operation_id="", expected_revision=""):
            calls.update(job_id=job_id, title=title, events=events,
                         operation_id=operation_id, expected_revision=expected_revision)
            return {"ok": True, "video_id": "video-x"}

        fake = SimpleNamespace(
            workspace=SimpleNamespace(video_job_detail=lambda job_id: {
                "revision": "rev-1",
                "draft": {"events": [{"at_ms": 0, "product_id": "p", "review_status": "approved"},
                                     {"at_ms": 5000, "product_id": "q", "review_status": "rejected"}]}}),
            video_analysis=SimpleNamespace(publish=publish))
        out = ReplyEngine.video_analysis_publish(fake, {"job_id": "j1", "title": "Live 9"})
        self.assertEqual(out["video_id"], "video-x")
        self.assertEqual(calls["expected_revision"], "rev-1")
        self.assertRegex(calls["operation_id"], r"^[A-Za-z0-9_-]{16,100}$")
        self.assertEqual([e["product_id"] for e in calls["events"]], ["p"])

    def test_con_moc_chua_duyet_thi_khong_phat_hanh(self):
        from server import ReplyEngine
        fake = SimpleNamespace(
            workspace=SimpleNamespace(video_job_detail=lambda job_id: {
                "revision": "r", "draft": {"events": [{"at_ms": 0, "product_id": "p"}]}}),
            video_analysis=SimpleNamespace(publish=lambda *a, **k: self.fail("không được gọi")))
        with self.assertRaises(ValueError):
            ReplyEngine.video_analysis_publish(fake, {"job_id": "j"})


class ShortNameTests(unittest.TestCase):
    def test_ten_gon_lay_tu_listing_that_giu_quy_cach(self):
        from server import ReplyEngine
        name = ReplyEngine._short_product_name
        self.assertEqual(name("Kích Rễ Cực Mạnh TC01 - Chuyên Dùng Cho Mai Vàng (chai 100ml)"), "Kích Rễ Cực Mạnh TC01 · 100ml")
        self.assertEqual(name("BO TRI ONE | Chế Phẩm Top Bo One Tri 200ml - Công Nghệ"), "BO TRI ONE · 200ml")
        self.assertEqual(name("Đạm Cá Amino 1L Chất Hữu Cơ 20% Amino Acid", "ĐẠM CÁ HỮU CƠ AMINO (5 lít)"), "Đạm Cá Amino 1L")
        self.assertEqual(name("SIÊU BÁM DÍNH TOÀN CẦU Công Nghệ NANO Mới"), "SIÊU BÁM DÍNH")


class ProductImageCacheTests(unittest.TestCase):
    def test_tai_mot_lan_chuan_hoa_jpeg_va_thu_nguon_ke_tiep(self):
        from PIL import Image
        buffer = io.BytesIO()
        Image.new("RGBA", (900, 600), (10, 200, 30, 255)).save(buffer, "PNG")
        png = buffer.getvalue()
        seen = []

        def fake_download(url, timeout=8):
            seen.append(url)
            if "bad" in url:
                raise OSError("404")
            return png

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(media_assets, "_download", fake_download):
            cache = media_assets.ProductImageCache(Path(tmp))
            path = cache.fetch("123", ["https://bad.example/a.jpg", "https://ok.example/b.png"])
            self.assertTrue(path and path.is_file())
            with Image.open(path) as image:
                self.assertEqual(image.format, "JPEG")
                self.assertLessEqual(max(image.size), media_assets.PRODUCT_THUMB_MAX)
            cache.fetch("123", ["https://ok.example/b.png"])
            self.assertEqual(len(seen), 2, "lần hai phải lấy từ bộ nhớ đệm")


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "cần ffmpeg")
class VideoAssetTests(unittest.TestCase):
    def test_tao_poster_filmstrip_va_song_am_tu_video_that(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            video = tmp / "clip.mp4"
            subprocess.run([
                "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=180x320:rate=15:duration=12",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=12", "-shortest",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(video),
            ], check=True, timeout=120)
            manager = media_assets.VideoAssetManager(tmp / "cache")
            state = manager.describe(video)
            self.assertIn(state["state"], {"queued", "running"})
            deadline = time.time() + 240       # máy bận (đang build/nén) thì ffmpeg chậm
            while time.time() < deadline:
                state = manager.describe(video)
                if state["state"] in {"ready", "error"}:
                    break
                time.sleep(0.3)
            self.assertEqual(state["state"], "ready", state)
            self.assertEqual(state["media"]["width"], 180)
            self.assertGreaterEqual(state["filmstrip"]["count"], 2)
            wave = json.loads(manager.file(state["key"], "waveform.json").read_text(encoding="utf-8"))
            self.assertGreater(max(wave["peaks"]), 100, "có tiếng sine thì phải có sóng")
            self.assertTrue(manager.file(state["key"], "poster.jpg").stat().st_size > 500)
            with self.assertRaises(ValueError):
                manager.file(state["key"], "../../etc/passwd")


@unittest.skipUnless(sys.platform == "win32", "Windows file-lock behavior")
class AtomicMediaWriteTests(unittest.TestCase):
    def test_retry_when_windows_temporarily_denies_replace(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            actual_replace = media_assets.os.replace
            attempts = []

            def replace(source, destination):
                attempts.append((source, destination))
                if len(attempts) == 1:
                    error = PermissionError("temporarily locked")
                    error.winerror = 32
                    raise error
                return actual_replace(source, destination)

            with mock.patch.object(media_assets.os, "replace", side_effect=replace):
                media_assets._atomic_json(path, {"complete": True})

            self.assertEqual(len(attempts), 2)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"complete": True})
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
