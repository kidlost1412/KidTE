"""Goi 13: nhap goi kien thuc phai cho XEM truoc va khong ghi de du lieu rieng.

apply_package() ban cu thay NGUYEN tep bang noi dung goi. Hai chot bi vi pham:
  - "Nhap goi cho xem: them moi, thay doi, giu nguyen va xung dot."
  - "Du lieu rieng khong bi ghi de boi truong trong trong goi."

Va mot hau qua rat cu the: `legacy_profile_id` nam ngay trong tep nay. Mot goi
thieu truong do se xoa sach duong noi toi ho so ky thuat da duyet — tai tao
dung su co F13, trong do AI tra loi lieu dung ma khong co ho so da duyet.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from knowledge_diff import TRUONG_RIENG_CUA_MAY, gop, so_sanh  # noqa: E402


def _noi_dung(*san_pham):
    return {"nhom_san_pham": [{"nhom": "thu", "san_pham": list(san_pham)}]}


TAI_MAY = _noi_dung(
    {"san_pham_id": "WEB-958", "ten_san_pham": "Dam ca 5L",
     "legacy_profile_id": "SP-13",            # rieng cua may (F13)
     "gia_goc_duyet": "555k",                 # rieng cua may
     "cong_dung": ["Bat mam"], "luu_y": "Ghi chu tai cho"},
    {"san_pham_id": "WEB-1236", "ten_san_pham": "Bo tri Top One",
     "legacy_profile_id": "SP-12", "cong_dung": ["Bo tri"]},
)


class SoSanhTests(unittest.TestCase):
    def test_san_pham_moi_duoc_bao_la_them_moi(self):
        goi = _noi_dung({"san_pham_id": "WEB-777", "ten_san_pham": "San pham moi"})
        kb = so_sanh(TAI_MAY, goi)
        self.assertEqual([m["san_pham_id"] for m in kb["them_moi"]], ["WEB-777"])
        self.assertEqual(kb["tom_tat"]["xung_dot"], 0)

    def test_giong_het_thi_bao_giu_nguyen(self):
        goi = _noi_dung({"san_pham_id": "WEB-958", "ten_san_pham": "Dam ca 5L",
                         "cong_dung": ["Bat mam"], "luu_y": "Ghi chu tai cho"})
        kb = so_sanh(TAI_MAY, goi)
        self.assertEqual([m["san_pham_id"] for m in kb["giu_nguyen"]], ["WEB-958"])
        self.assertEqual(kb["can_nguoi_quyet"], 0)

    def test_truong_tai_may_dang_rong_thi_la_thay_doi_chu_khong_xung_dot(self):
        goi = _noi_dung({"san_pham_id": "WEB-1236", "ten_san_pham": "Bo tri Top One",
                         "cong_dung": ["Bo tri"], "luu_y": "Huong dan moi"})
        kb = so_sanh(TAI_MAY, goi)
        self.assertEqual([m["san_pham_id"] for m in kb["thay_doi"]], ["WEB-1236"])
        self.assertEqual(kb["can_nguoi_quyet"], 0,
                         "chi hoi o muc THUC SU xung dot")

    def test_hai_ben_khac_nhau_va_may_da_co_ban_rieng_thi_la_xung_dot(self):
        goi = _noi_dung({"san_pham_id": "WEB-958", "ten_san_pham": "Dam ca 5L",
                         "luu_y": "Ghi chu tu goi chung"})
        kb = so_sanh(TAI_MAY, goi)
        self.assertEqual([m["san_pham_id"] for m in kb["xung_dot"]], ["WEB-958"])
        truong = kb["xung_dot"][0]["cac_truong"][0]
        self.assertEqual(truong["truong"], "luu_y")
        self.assertEqual(truong["tai_may"], "Ghi chu tai cho")
        self.assertEqual(truong["trong_goi"], "Ghi chu tu goi chung")

    def test_truong_rong_trong_goi_khong_duoc_tinh_la_thay_doi(self):
        goi = _noi_dung({"san_pham_id": "WEB-958", "ten_san_pham": "Dam ca 5L",
                         "cong_dung": [], "luu_y": ""})
        kb = so_sanh(TAI_MAY, goi)
        self.assertEqual([m["san_pham_id"] for m in kb["giu_nguyen"]], ["WEB-958"])

    def test_san_pham_chi_co_tai_may_duoc_liet_ke_rieng(self):
        goi = _noi_dung({"san_pham_id": "WEB-958", "ten_san_pham": "Dam ca 5L"})
        kb = so_sanh(TAI_MAY, goi)
        self.assertEqual([m["san_pham_id"] for m in kb["chi_co_tai_may"]],
                         ["WEB-1236"])


class GopTests(unittest.TestCase):
    def _lay(self, noi_dung, ma):
        for nhom in noi_dung["nhom_san_pham"]:
            for sp in nhom["san_pham"]:
                if sp["san_pham_id"] == ma:
                    return sp
        return None

    def test_goi_THIEU_legacy_profile_id_khong_xoa_duoc_lien_ket(self):
        """Day chinh la duong tai tao su co F13."""
        goi = _noi_dung({"san_pham_id": "WEB-958", "ten_san_pham": "Dam ca 5L",
                         "cong_dung": ["Bat mam", "Xanh la"]})
        ket = gop(TAI_MAY, goi)["noi_dung"]
        self.assertEqual(self._lay(ket, "WEB-958")["legacy_profile_id"], "SP-13",
                         "goi chung khong duoc pha duong noi toi ho so da duyet")

    def test_goi_CO_legacy_profile_id_khac_cung_khong_duoc_doi(self):
        goi = _noi_dung({"san_pham_id": "WEB-958", "ten_san_pham": "Dam ca 5L",
                         "legacy_profile_id": "SP-99"})
        ket = gop(TAI_MAY, goi)["noi_dung"]
        self.assertEqual(self._lay(ket, "WEB-958")["legacy_profile_id"], "SP-13")

    def test_moi_truong_rieng_cua_may_deu_duoc_bao_ve(self):
        goi_sp = {"san_pham_id": "WEB-958", "ten_san_pham": "Dam ca 5L"}
        for truong in TRUONG_RIENG_CUA_MAY:
            goi_sp[truong] = "GIA-TRI-TU-GOI"
        ket = gop(TAI_MAY, _noi_dung(goi_sp))["noi_dung"]
        sp = self._lay(ket, "WEB-958")
        self.assertEqual(sp["legacy_profile_id"], "SP-13")
        self.assertEqual(sp["gia_goc_duyet"], "555k")

    def test_truong_rong_trong_goi_khong_xoa_noi_dung_tai_may(self):
        goi = _noi_dung({"san_pham_id": "WEB-958", "ten_san_pham": "Dam ca 5L",
                         "cong_dung": [], "luu_y": ""})
        sp = self._lay(gop(TAI_MAY, goi)["noi_dung"], "WEB-958")
        self.assertEqual(sp["cong_dung"], ["Bat mam"])
        self.assertEqual(sp["luu_y"], "Ghi chu tai cho")

    def test_mac_dinh_o_muc_xung_dot_la_GIU_ban_tai_may(self):
        goi = _noi_dung({"san_pham_id": "WEB-958", "ten_san_pham": "Dam ca 5L",
                         "luu_y": "Ghi chu tu goi chung"})
        sp = self._lay(gop(TAI_MAY, goi)["noi_dung"], "WEB-958")
        self.assertEqual(sp["luu_y"], "Ghi chu tai cho",
                         "khong tu ghi de cong suc soan tai cho")

    def test_nguoi_dung_chon_lay_theo_goi_thi_moi_doi(self):
        goi = _noi_dung({"san_pham_id": "WEB-958", "ten_san_pham": "Dam ca 5L",
                         "luu_y": "Ghi chu tu goi chung"})
        ket = gop(TAI_MAY, goi, {"WEB-958": "lay_theo_goi"})
        self.assertEqual(self._lay(ket["noi_dung"], "WEB-958")["luu_y"],
                         "Ghi chu tu goi chung")
        # Va ngay ca khi lay theo goi, truong rieng van duoc giu.
        self.assertEqual(
            self._lay(ket["noi_dung"], "WEB-958")["legacy_profile_id"], "SP-13")

    def test_san_pham_moi_duoc_them_vao(self):
        goi = _noi_dung({"san_pham_id": "WEB-777", "ten_san_pham": "San pham moi",
                         "cong_dung": ["Cong dung moi"]})
        ket = gop(TAI_MAY, goi)
        self.assertIsNotNone(self._lay(ket["noi_dung"], "WEB-777"))
        self.assertIn("WEB-777.(thêm mới)", ket["da_doi"])

    def test_san_pham_chi_co_tai_may_khong_bi_xoa(self):
        goi = _noi_dung({"san_pham_id": "WEB-958", "ten_san_pham": "Dam ca 5L"})
        ket = gop(TAI_MAY, goi)["noi_dung"]
        self.assertIsNotNone(self._lay(ket, "WEB-1236"),
                            "goi khong nhac toi thi khong duoc xoa")

    def test_khong_sua_vao_du_lieu_dau_vao(self):
        goi = _noi_dung({"san_pham_id": "WEB-958", "ten_san_pham": "Dam ca 5L",
                         "cong_dung": ["Hoan toan khac"]})
        gop(TAI_MAY, goi)
        self.assertEqual(self._lay(TAI_MAY, "WEB-958")["cong_dung"], ["Bat mam"])


if __name__ == "__main__":
    unittest.main()
