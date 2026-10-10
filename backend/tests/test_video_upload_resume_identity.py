"""Hoi quy F06: tiep tuc upload phai xac minh NOI DUNG, khong chi ten + size.

Loi goc: viec tim job de tiep tuc do trinh duyet lam, va chi so
  row.total_size === file.size && row.filename === safeUploadName(file.name)
Hai video KHAC NHAU cung ten va cung dung so byte (vi du mot ban dung lai,
hay mot file duoc doi ten) se bi noi byte cua file moi len phan da tai cua
file cu. finish_upload khong the phat hien vi kich thuoc cuoi cung van dung —
ket qua la mot video lai tap, hong nhung duoc danh dau san sang.
"""
from __future__ import annotations

import io
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from video_analysis import VideoAnalysisManager  # noqa: E402

VAN_TAY_A = "a" * 64
VAN_TAY_B = "b" * 64


def _catalog():
    return {"synced_at": "2026-09-15T00:00:00Z",
            "products": [{"product_id": "sku-1", "bag_index": 1, "title": "San pham"}]}


class TiepTucUploadTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.manager = VideoAnalysisManager(
            root=Path(self._tmp.name), catalog_provider=_catalog)

    def _job_do(self, van_tay, ten="phim.mp4", tong=8192, da_tai=4096):
        state = self.manager.start_upload(ten, tong, van_tay)
        self.manager.append_chunk(state["job_id"], 0, io.BytesIO(b"a" * da_tai), da_tai)
        return state

    def test_cung_file_thi_tiep_tuc_duoc(self):
        goc = self._job_do(VAN_TAY_A)
        thay = self.manager.resumable_job("phim.mp4", 8192, VAN_TAY_A)
        self.assertIsNotNone(thay)
        self.assertEqual(thay["job_id"], goc["job_id"])
        self.assertEqual(thay["uploaded_size"], 4096)

    def test_file_khac_cung_ten_cung_kich_thuoc_thi_KHONG_duoc_tiep_tuc(self):
        self._job_do(VAN_TAY_A)
        # Nguoi dung chon nham mot video khac: ten y het, size y het.
        self.assertIsNone(
            self.manager.resumable_job("phim.mp4", 8192, VAN_TAY_B),
            "van tay khac nhau thi khong duoc noi tiep — day chinh la F06")

    def test_khac_kich_thuoc_thi_khong_tiep_tuc(self):
        self._job_do(VAN_TAY_A)
        self.assertIsNone(self.manager.resumable_job("phim.mp4", 9999, VAN_TAY_A))

    def test_khac_ten_thi_khong_tiep_tuc(self):
        self._job_do(VAN_TAY_A)
        self.assertIsNone(self.manager.resumable_job("phim-khac.mp4", 8192, VAN_TAY_A))

    def test_job_cu_khong_co_van_tay_thi_khong_cho_tiep_tuc(self):
        """An toan hon la tai lai tu dau, vi khong the chung minh cung file."""
        self.manager.start_upload("phim.mp4", 8192)          # khong co van tay
        self.assertIsNone(self.manager.resumable_job("phim.mp4", 8192, VAN_TAY_A))

    def test_thieu_van_tay_o_lan_hoi_thi_bao_loi_ro_rang(self):
        self._job_do(VAN_TAY_A)
        with self.assertRaises(ValueError) as ctx:
            self.manager.resumable_job("phim.mp4", 8192, "")
        self.assertEqual(str(ctx.exception), "upload_probe_required")

    def test_van_tay_di_dang_bi_coi_nhu_khong_co(self):
        self._job_do(VAN_TAY_A)
        for xau in ("KHONG-PHAI-HEX" * 4, "a" * 63, "a" * 65, "Z" * 64):
            with self.subTest(xau=xau[:16]):
                with self.assertRaises(ValueError):
                    self.manager.resumable_job("phim.mp4", 8192, xau)

    def test_van_tay_khong_phan_biet_hoa_thuong(self):
        self._job_do(VAN_TAY_A)
        self.assertIsNotNone(
            self.manager.resumable_job("phim.mp4", 8192, VAN_TAY_A.upper()))

    def test_job_da_tai_xong_khong_bi_nham_la_dang_do(self):
        state = self.manager.start_upload("xong.mp4", 4096, VAN_TAY_A)
        self.manager.append_chunk(state["job_id"], 0, io.BytesIO(b"x" * 4096), 4096)
        # Khong chay worker phan tich: o day chi kiem trang thai job.
        with mock.patch.object(self.manager, "_start_worker", lambda job_id: None):
            self.manager.finish_upload(state["job_id"])
        self.assertIsNone(self.manager.resumable_job("xong.mp4", 4096, VAN_TAY_A))


if __name__ == "__main__":
    unittest.main()
