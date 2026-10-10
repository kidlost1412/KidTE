"""Hoi quy F07: mau tra loi da duyet khong duoc mang du kien san pham A
sang cau hoi ve san pham B; va doi anh khong duoc vo hieu hoa mau.

Loi goc trong reviewed_examples():
    score = .55*lexical + .25*(cung question_type) + .20*(cung product_id)
    if score >= .35: dung mau nay
Chi can trung tu ngu ~64% la mot mau cua san pham A duoc chon cho cau hoi ve
san pham B — ke ca khi cau tra loi do ghi ro lieu dung cua A. Hai cau hoi
lieu dung thuong dung gan het cung mot bo tu ("binh", "lit", "pha", "nhieu",
"gam") nen nguong nay rat de dat.

Loi thu hai: product_revision duoc bam tren CA ban ghi san pham, trong do co
image_url (kem image_hash) va subtitle. Tai mot anh san pham moi la moi mau
cua san pham do bi day sang "can xem lai", du khong co du kien nao thay doi.
"""
from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import workspace as workspace_module  # noqa: E402

SAN_PHAM_A = {
    "id": "A", "product_id": "A", "profile_id": "WEB-1",
    "original_price": "120.000₫", "title": "Dam ca Amino 5L",
    "image_url": "/api/product/image/A?v=anh-cu", "subtitle": "Gio #1",
}
SAN_PHAM_B = {
    "id": "B", "product_id": "B", "profile_id": "WEB-2",
    "original_price": "90.000₫", "title": "Kich re TC01 100ml",
    "image_url": "/api/product/image/B?v=anh-cu", "subtitle": "Gio #2",
}


def _store(san_pham=None):
    tmp = tempfile.TemporaryDirectory()
    store = workspace_module.WorkspaceStore.__new__(workspace_module.WorkspaceStore)
    store.root = Path(tmp.name)
    (store.root / "data").mkdir(parents=True, exist_ok=True)
    store.db_path = store.root / "data" / "ws.sqlite3"
    store.engine = types.SimpleNamespace()
    store._products = lambda: [dict(x) for x in (san_pham or [SAN_PHAM_A, SAN_PHAM_B])]
    store._create_schema()
    return store, tmp


class PhamViMauTraLoiTests(unittest.TestCase):
    def setUp(self):
        self.store, self._tmp = _store()
        self.addCleanup(self._tmp.cleanup)

    def _them_mau(self, *, cau_hoi, tra_loi, product_id, loai="lieu_dung",
                  phien_ban=None):
        import uuid
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        if phien_ban is None and product_id:
            sp = next(x for x in self.store._products() if x["id"] == product_id)
            phien_ban = self.store._phien_ban_du_kien(sp)
        with self.store._db() as db:
            db.execute(
                "INSERT INTO approved_reply_examples "
                "(example_id,conversation_id,created_at,updated_at,question,"
                "normalized_question,question_type,product_id,product_revision,"
                "approved_answer,reason,status,use_count) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,'active',0)",
                (uuid.uuid4().hex, uuid.uuid4().hex, now, now, cau_hoi,
                 self.store._fold(cau_hoi), loai, product_id, phien_ban or "",
                 tra_loi, "ly do"))

    # ---------- pham vi ----------

    def test_mau_co_lieu_cua_san_pham_A_khong_sang_san_pham_B(self):
        self._them_mau(cau_hoi="binh 20 lit pha nhieu gam",
                       tra_loi="50g cho binh 20 lit nha anh", product_id="A")
        # Cau hoi gan nhu y het, nhung dang hoi ve san pham B.
        ket = self.store.reviewed_examples(
            "binh 20 lit pha nhieu gam", question_type="lieu_dung", product_id="B")
        self.assertEqual(ket, [],
                         "mau chua lieu cua A khong duoc dung cho B — day la F07")

    def test_mau_co_lieu_van_dung_duoc_cho_dung_san_pham_cua_no(self):
        self._them_mau(cau_hoi="binh 20 lit pha nhieu gam",
                       tra_loi="50g cho binh 20 lit nha anh", product_id="A")
        ket = self.store.reviewed_examples(
            "binh 20 lit pha nhieu gam", question_type="lieu_dung", product_id="A")
        self.assertEqual(len(ket), 1)
        self.assertEqual(ket[0]["product_id"], "A")

    def test_mau_co_gia_cung_bi_gioi_han_pham_vi(self):
        self._them_mau(cau_hoi="gia nhieu", tra_loi="Gia goc 555k do anh",
                       product_id="A", loai="gia")
        self.assertEqual(
            self.store.reviewed_examples("gia nhieu", question_type="gia",
                                         product_id="B"), [])

    def test_mau_dien_dat_chung_dung_duoc_cho_moi_san_pham(self):
        self._them_mau(cau_hoi="ship bao lau",
                       tra_loi="Ben em gui trong ngay nha anh",
                       product_id="A", loai="van_chuyen")
        ket = self.store.reviewed_examples(
            "ship bao lau", question_type="van_chuyen", product_id="B")
        self.assertEqual(len(ket), 1,
                         "mau khong chua du kien rieng thi dung chung duoc")

    def test_mau_co_du_kien_nhung_khong_gan_san_pham_thi_khong_duoc_dung(self):
        """Khong the gioi han pham vi thi khong duoc dung — an toan hon."""
        self._them_mau(cau_hoi="binh 20 lit pha nhieu gam",
                       tra_loi="50g cho binh 20 lit", product_id="")
        self.assertEqual(
            self.store.reviewed_examples("binh 20 lit pha nhieu gam",
                                         question_type="lieu_dung",
                                         product_id="A"), [])

    # ---------- phien ban ----------

    def test_doi_anh_khong_lam_mat_hieu_luc_mau(self):
        self._them_mau(cau_hoi="ship bao lau", tra_loi="Gui trong ngay nha anh",
                       product_id="A", loai="van_chuyen")
        moi = dict(SAN_PHAM_A, image_url="/api/product/image/A?v=anh-moi",
                   subtitle="Gio #3", title="Dam ca Amino 5L (ban moi)")
        self.store._products = lambda: [moi, dict(SAN_PHAM_B)]
        ket = self.store.reviewed_examples("ship bao lau",
                                           question_type="van_chuyen",
                                           product_id="A")
        self.assertEqual(len(ket), 1,
                         "doi anh / tieu de khong duoc vo hieu hoa mau — F07")

    def test_doi_gia_thi_mau_khong_con_duoc_dung(self):
        """Nghiep vu giu nguyen: doi gia thi mau do KHONG duoc dung nua.

        Truoc day test nay con khang dinh ban ghi trong DB da thanh
        'needs_review' — nhung viec ghi do nam ngay trong duong DOC
        (reviewed_examples), khien detail() khong idempotent va sinh ra
        revision_conflict. Phan khang dinh ve DB da chuyen sang
        test_duong_ghi_van_danh_dau_needs_review ben duoi.
        """
        self._them_mau(cau_hoi="ship bao lau", tra_loi="Gui trong ngay nha anh",
                       product_id="A", loai="van_chuyen")
        moi = dict(SAN_PHAM_A, original_price="150.000₫")
        self.store._products = lambda: [moi, dict(SAN_PHAM_B)]
        self.assertEqual(
            self.store.reviewed_examples("ship bao lau",
                                         question_type="van_chuyen",
                                         product_id="A"), [])
        with self.store._db() as db:
            trang_thai = db.execute(
                "SELECT status FROM approved_reply_examples").fetchone()[0]
        self.assertEqual(trang_thai, "active",
                         "duong DOC khong duoc ghi gi vao DB")

    def test_duong_ghi_van_danh_dau_needs_review(self):
        """Nhan 'can xem lai' van duoc dat — nhung o duong GHI."""
        self._them_mau(cau_hoi="ship bao lau", tra_loi="Gui trong ngay nha anh",
                       product_id="A", loai="van_chuyen")
        moi = dict(SAN_PHAM_A, original_price="150.000₫")
        self.store._products = lambda: [moi, dict(SAN_PHAM_B)]
        self.assertEqual(len(self.store.danh_dau_mau_can_xem_lai()), 1)
        with self.store._db() as db:
            trang_thai = db.execute(
                "SELECT status FROM approved_reply_examples").fetchone()[0]
        self.assertEqual(trang_thai, "needs_review")

    def test_doc_mau_khong_bao_gio_ghi_db(self):
        self._them_mau(cau_hoi="ship bao lau", tra_loi="Gui trong ngay nha anh",
                       product_id="A", loai="van_chuyen")
        moi = dict(SAN_PHAM_A, original_price="150.000₫")
        self.store._products = lambda: [moi, dict(SAN_PHAM_B)]

        def chup():
            with self.store._db() as db:
                return [tuple(r) for r in db.execute(
                    "SELECT example_id,status,updated_at,use_count "
                    "FROM approved_reply_examples ORDER BY example_id")]

        truoc = chup()
        for _ in range(3):
            self.store.reviewed_examples("ship bao lau",
                                         question_type="van_chuyen",
                                         product_id="A")
        self.assertEqual(chup(), truoc,
                         "doc mau nhieu lan khong duoc lam DB thay doi")

    def test_doi_ho_so_ky_thuat_thi_mau_can_duoc_xem_lai(self):
        self._them_mau(cau_hoi="ship bao lau", tra_loi="Gui trong ngay nha anh",
                       product_id="A", loai="van_chuyen")
        moi = dict(SAN_PHAM_A, profile_id="WEB-999")
        self.store._products = lambda: [moi, dict(SAN_PHAM_B)]
        self.assertEqual(
            self.store.reviewed_examples("ship bao lau",
                                         question_type="van_chuyen",
                                         product_id="A"), [])

    # ---------- nhan dien du kien ----------

    def test_nhan_dien_du_kien_san_pham(self):
        co = ["50g cho binh 20 lit", "Gia goc 555k", "pha 25ml moi binh",
              "chai 100ml", "1.2 kg", "giam 10%", "120.000₫"]
        khong = ["Da nhan duoc don cua anh nhe", "Ben em gui trong ngay",
                 "Anh de lai so dien thoai giup em", "Da het hang a"]
        for t in co:
            with self.subTest(t=t):
                self.assertTrue(self.store._chua_du_kien_san_pham(t), t)
        for t in khong:
            with self.subTest(t=t):
                self.assertFalse(self.store._chua_du_kien_san_pham(t), t)


if __name__ == "__main__":
    unittest.main()
