"""Hoi quy F01: chunk dut giua chung roi retry khong duoc nhan doi byte.

Loi goc: append_chunk mo .part o che do "ab" va ghi dan. Khi stream dut,
byte da ghi nam lai nhung uploaded_size khong tang, nen retry cung offset
noi them mot ban sao -> file phinh ra. finish_upload chi so uploaded_size
voi total_size nen van nhan file hong.
"""
import io
import sys
import unittest
from unittest import mock
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from video_analysis import (  # noqa: E402
    UploadOffsetMismatch,
    UploadPartCorrupt,
    VideoAnalysisManager,
)


class _DutGiuaChung(io.RawIOBase):
    """Stream tra duoc `cho_phep` byte roi im lang (giong ket noi dut)."""

    def __init__(self, data, cho_phep):
        self._data = data
        self._con_lai = cho_phep
        self._vi_tri = 0

    def read(self, size=-1):
        if self._con_lai <= 0:
            return b""
        lay = min(size if size and size > 0 else len(self._data), self._con_lai)
        block = self._data[self._vi_tri:self._vi_tri + lay]
        self._vi_tri += len(block)
        self._con_lai -= len(block)
        return block


def _catalog():
    return {"synced_at": "2026-09-15T00:00:00Z",
            "products": [{"product_id": "sku-1", "bag_index": 1,
                          "title": "San pham thu"}]}


class VideoUploadIntegrityTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.manager = VideoAnalysisManager(
            root=Path(self._tmp.name), catalog_provider=_catalog)
        self.addCleanup(self._tmp.cleanup)

    def _job(self, total):
        return self.manager.start_upload("phim-thu.mp4", total)

    def _part(self, job_id, state):
        return self.manager._dir(job_id) / (state["filename"] + ".part")

    def test_chunk_dut_giua_chung_roi_retry_khong_nhan_doi_byte(self):
        noi_dung = bytes(range(256)) * 32          # 8192 byte
        chunk1, chunk2 = noi_dung[:4096], noi_dung[4096:]
        state = self._job(len(noi_dung))
        job_id = state["job_id"]

        self.manager.append_chunk(job_id, 0, io.BytesIO(chunk1), len(chunk1))

        # Chunk 2 dut sau 1000 byte.
        with self.assertRaises(ValueError):
            self.manager.append_chunk(
                job_id, 4096, _DutGiuaChung(chunk2, 1000), len(chunk2))

        part = self._part(job_id, state)
        self.assertEqual(part.stat().st_size, 4096,
                         "that bai phai quay ve dung ranh gioi chunk da commit")
        self.assertEqual(self.manager._read(job_id)["uploaded_size"], 4096)

        # Retry dung offset cu, lan nay du byte.
        self.manager.append_chunk(job_id, 4096, io.BytesIO(chunk2), len(chunk2))
        self.assertEqual(part.stat().st_size, len(noi_dung))
        self.assertEqual(part.read_bytes(), noi_dung,
                         "noi dung phai giong het ban goc, khong thua byte")

    def test_finish_upload_tu_choi_file_lech_kich_thuoc_that(self):
        noi_dung = b"x" * 4096
        state = self._job(len(noi_dung))
        job_id = state["job_id"]
        self.manager.append_chunk(job_id, 0, io.BytesIO(noi_dung), len(noi_dung))

        # Gia lap .part bi hong sau khi da commit (thua byte tren dia).
        part = self._part(job_id, state)
        with part.open("ab") as handle:
            handle.write(b"THUA")

        with self.assertRaises(UploadPartCorrupt) as ctx:
            self.manager.finish_upload(job_id)
        self.assertEqual(str(ctx.exception), "upload_size_mismatch")
        self.assertEqual(ctx.exception.actual_size, 4100)

    def test_sai_offset_tra_ve_offset_da_commit(self):
        state = self._job(8192)
        job_id = state["job_id"]
        self.manager.append_chunk(job_id, 0, io.BytesIO(b"a" * 4096), 4096)
        with self.assertRaises(UploadOffsetMismatch) as ctx:
            self.manager.append_chunk(job_id, 0, io.BytesIO(b"b" * 4096), 4096)
        self.assertEqual(ctx.exception.committed_offset, 4096)

    def test_part_ngan_hon_metadata_thi_tu_choi_noi_tiep(self):
        state = self._job(8192)
        job_id = state["job_id"]
        self.manager.append_chunk(job_id, 0, io.BytesIO(b"a" * 4096), 4096)
        part = self._part(job_id, state)
        with part.open("r+b") as handle:      # file bi cat bot ngoai y muon
            handle.truncate(1000)
        with self.assertRaises(UploadPartCorrupt) as ctx:
            self.manager.append_chunk(job_id, 4096, io.BytesIO(b"b" * 4096), 4096)
        self.assertEqual(str(ctx.exception), "upload_part_truncated_on_disk")

    def test_duong_thuan_loi_van_hoan_tat_binh_thuong(self):
        noi_dung = bytes(range(256)) * 16
        state = self._job(len(noi_dung))
        job_id = state["job_id"]
        buoc = 1024
        for offset in range(0, len(noi_dung), buoc):
            khoi = noi_dung[offset:offset + buoc]
            self.manager.append_chunk(job_id, offset, io.BytesIO(khoi), len(khoi))
        # Khong chay worker phan tich: o day chi kiem tinh nguyen ven file.
        with mock.patch.object(self.manager, "_start_worker", lambda job_id: None):
            ket = self.manager.finish_upload(job_id, title="Phim thu")
        self.assertEqual(ket["state"], "queued")
        nguon = self.manager._dir(job_id) / state["filename"]
        self.assertEqual(nguon.read_bytes(), noi_dung)


if __name__ == "__main__":
    unittest.main()
