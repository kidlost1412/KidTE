"""Goi 14: mot lan chay ket thuc phai de lai dau vet trong log.

Quan sat that trong phien nang cap: tien tринh production bien mat va
core.jsonl KHONG co bat ky ban ghi nao ve viec dung — ban ghi cuoi cung van
la `backend_ready`. File lock thi van giu PID da chet. Nghia la:
  - Tat binh thuong: khoi `finally` don dep nhung khong ghi gi.
  - Bi kill cung: khong chay duoc `finally`, nen cang khong ghi gi.
Khong co cach nao doi chieu "app da dung luc nao, vi sao".
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from instance_lock import SingleInstanceLock  # noqa: E402


class LockSotLaiTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.duong_dan = Path(self._tmp.name) / "runtime" / "live-re.lock"

    def test_lan_chay_sach_khong_bao_co_lock_sot(self):
        lock = SingleInstanceLock(self.duong_dan)
        lock.acquire()
        self.addCleanup(lock.release)
        self.assertIsNone(lock.stale_lock)

    def test_lock_cua_tien_trinh_da_chet_duoc_giu_lam_bang_chung(self):
        # Lock cua mot lan chay truoc, PID chac chan khong con song.
        self.duong_dan.parent.mkdir(parents=True, exist_ok=True)
        cu = {"schema_version": 1, "pid": 999999999,
              "started_at": "2026-09-15T03:45:03+00:00",
              "token": "cu", "process_identity": None}
        self.duong_dan.write_text(json.dumps(cu), encoding="utf-8")

        lock = SingleInstanceLock(self.duong_dan)
        lock.acquire()
        self.addCleanup(lock.release)

        self.assertIsNotNone(lock.stale_lock,
                             "phai giu lai lock cu lam bang chung lan chay truoc")
        self.assertEqual(lock.stale_lock["pid"], 999999999)
        self.assertEqual(lock.stale_lock["started_at"],
                         "2026-09-15T03:45:03+00:00")

        # Va lock moi van duoc lay binh thuong.
        moi = json.loads(self.duong_dan.read_text(encoding="utf-8"))
        self.assertNotEqual(moi["pid"], 999999999)
        self.assertEqual(moi["token"], lock.token)

    def test_lock_hong_dinh_dang_thi_tu_choi_khoi_dong_chu_khong_tu_xoa(self):
        """Hanh vi CO CHU DICH: file lock hong khong chung minh duoc rang
        khong con phien nao dang chay, nen khong duoc tu xoa roi chay tiep."""
        from instance_lock import InstanceAlreadyRunning
        self.duong_dan.parent.mkdir(parents=True, exist_ok=True)
        self.duong_dan.write_text("khong-phai-json", encoding="utf-8")
        lock = SingleInstanceLock(self.duong_dan)
        with self.assertRaises(InstanceAlreadyRunning):
            lock.acquire()
        self.assertEqual(self.duong_dan.read_text(encoding="utf-8"),
                         "khong-phai-json", "khong duoc tu xoa lock hong")


class GhiNhatKyDungTests(unittest.TestCase):
    """app_entry phai ghi ca ba su kien, khong chi backend_ready."""

    def test_app_entry_ghi_du_cac_su_kien_vong_doi(self):
        ma = (SRC / "app_entry.py").read_text(encoding="utf-8")
        for su_kien in ("backend_ready", "backend_stop_requested",
                        "backend_stopping", "backend_stopped",
                        "previous_run_ended_unexpectedly"):
            self.assertIn(f'"event": "{su_kien}"', ma,
                          f"thieu su kien {su_kien} trong app_entry")

    def test_khoi_finally_co_ghi_log_truoc_khi_don_dep(self):
        ma = (SRC / "app_entry.py").read_text(encoding="utf-8")
        khoi = ma[ma.rindex("    finally:"):]
        self.assertIn("backend_stopping", khoi)
        self.assertIn("backend_stopped", khoi)
        self.assertLess(khoi.index("backend_stopping"), khoi.index("lock.release()"),
                        "phai ghi truoc khi nha khoa")



class TinHieuDungTests(unittest.TestCase):
    """Windows gui Ctrl+Break thanh SIGBREAK, khong phai SIGINT.

    Ban cu chi dang ky SIGINT/SIGTERM. Do tren EXE that: Ctrl+Break lam tien
    trinh chet voi ma 0xC000013A (STATUS_CONTROL_C_EXIT), khoi `finally`
    khong chay, lock o lai va khong co dong log nao ve viec dung. Sau khi
    dang ky them SIGBREAK: ma thoat 0, ghi du backend_stop_requested /
    backend_stopping / backend_stopped, va lock duoc nha.
    """

    def test_dang_ky_ca_sigbreak(self):
        ma = (SRC / "app_entry.py").read_text(encoding="utf-8")
        self.assertIn("SIGBREAK", ma,
                      "thieu SIGBREAK thi Ctrl+Break tren Windows se bo qua "
                      "toan bo buoc don dep")
        for ten in ("SIGINT", "SIGTERM"):
            self.assertIn(ten, ma)

    def test_dang_ky_tin_hieu_khong_chan_khoi_dong_khi_that_bai(self):
        """Mot so moi truong khong cho dat handler; khong duoc vi the ma chet."""
        ma = (SRC / "app_entry.py").read_text(encoding="utf-8")
        khoi = ma[ma.index("SIGBREAK") - 400:ma.index("SIGBREAK") + 500]
        self.assertIn("except", khoi, "phai bo qua duoc loi dat handler")

if __name__ == "__main__":
    unittest.main()
