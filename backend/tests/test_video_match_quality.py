"""Goi 9: muc chac chan khop video phai la LOI, khong phai phan tram.

`margin` la khoang cach giua ung vien tot nhat va ung vien ke tiep, khong
phai xac suat. Giao dien cu hien `Math.round(margin*100)%`. Gia tri that doc
duoc tren may van hanh la 0.0722 — vua qua nguong 0.06, tuc la MOT lan khop
hop le — nhung man hinh bao "Do khop 7%", khien nguoi dung tuong la gan nhu
khong khop.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from server import VideoPositionStore, _muc_chac_chan  # noqa: E402

WEB = SRC / "web"


class MucChacChanTests(unittest.TestCase):
    def test_vua_qua_nguong_la_mot_lan_khop_hop_le(self):
        ket = _muc_chac_chan(0.0722, 0.06)
        self.assertEqual(ket["match_quality"], "vua_du")
        self.assertEqual(ket["match_label"], "Khop vua du")

    def test_duoi_nguong_thi_bao_chua_du_chac_chan(self):
        self.assertEqual(_muc_chac_chan(0.03, 0.06)["match_quality"], "yeu")

    def test_gap_doi_nguong_tro_len_la_khop_ro(self):
        self.assertEqual(_muc_chac_chan(0.13, 0.06)["match_quality"], "ro")

    def test_thieu_so_lieu_thi_bao_chua_xac_dinh(self):
        for margin in (None, "", "khong-phai-so"):
            with self.subTest(margin=margin):
                self.assertEqual(_muc_chac_chan(margin, 0.06)["match_quality"],
                                 "khong_ro")

    def test_nguong_bang_khong_van_dung_mac_dinh(self):
        self.assertEqual(_muc_chac_chan(0.07, 0)["match_quality"], "vua_du")

    def test_trang_thai_hien_hanh_kem_theo_nhan(self):
        store = VideoPositionStore()
        store.update({"position_s": 12.0, "margin": 0.0722, "min_margin": 0.06,
                      "video_id": "live2", "video_duration_s": 2573.6})
        ket = store.current()
        self.assertTrue(ket["available"])
        self.assertEqual(ket["match_label"], "Khop vua du")
        self.assertEqual(ket["min_margin"], 0.06)

    def test_giao_dien_khong_con_quy_margin_thanh_phan_tram(self):
        # Giao diện là các module JS trong web/app (operator.html chỉ còn khung).
        for trang in sorted(WEB.glob("app/**/*.js")):
            ma = trang.read_text(encoding="utf-8")
            self.assertNotRegex(
                ma, r"margin\s*\*\s*100",
                f"{trang.name} van quy margin thanh phan tram")


if __name__ == "__main__":
    unittest.main()
