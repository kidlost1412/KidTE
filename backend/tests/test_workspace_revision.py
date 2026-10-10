"""Hoi quy: sua cau tra loi roi luu luon bao "da luu o noi khac" nhung khong luu.

Thong bao do la `revision_conflict`. Nguyen nhan: detail() bam revision tren CA
dict da duoc lam giau, trong do co hang loat truong tu doi:

  * `conversation_status` nhay waiting -> unanswered sau DUNG 180 giay
    (workspace.py, nhanh `time.time() - item["_at"] <= 180`). Go cau tra loi
    1-3 phut la chac chan xung dot.
  * `corrections` nhung ca danh sach ban sua -> lan luu THU HAI luon hong.
  * `product_image_url` chua image_hash -> doi anh san pham la doi revision cua
    moi hoi thoai gan san pham do.
  * `reviewed_examples()` GHI DB ngay trong duong doc -> detail() khong
    idempotent, xung dot tu sinh trong cung mot thao tac.
  * `previous_customer_comments` tro sang ban ghi hang xom -> hang xom doi la
    minh doi.

Bo test cu khong bat duoc vi chi phu `profiles`, noi revision la gia tri tinh.
"""
from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import workspace as workspace_module  # noqa: E402

MOC = 1789000000.0


def _dong_tho(ma, text, at, user="u1"):
    return {"id": ma, "msg_id": f"m-{ma}", "text": text, "_at": at,
            "ts": "2026-09-15T10:00:00Z", "user_id": user,
            "unique_id": f"khach{user}", "nickname": f"Khach {user}"}


def _store(rows):
    tmp = tempfile.TemporaryDirectory()
    store = workspace_module.WorkspaceStore.__new__(workspace_module.WorkspaceStore)
    store.root = Path(tmp.name)
    (store.root / "data").mkdir(parents=True, exist_ok=True)
    store.db_path = store.root / "data" / "ws.sqlite3"
    store.engine = types.SimpleNamespace(
        mapping_state=lambda: {"products": []}, extension_heartbeat={})
    store._read_raw_rows = lambda: [dict(r) for r in rows]
    store._products = lambda: []
    store._create_schema()
    return store, tmp


class RevisionOnDinhTests(unittest.TestCase):
    def setUp(self):
        self.rows = [_dong_tho("c1", "dam ca gia nhieu", MOC)]
        self.store, self._tmp = _store(self.rows)
        self.addCleanup(self._tmp.cleanup)

    def _revision(self, bay_gio):
        with mock.patch.object(workspace_module.time, "time", return_value=bay_gio):
            return self.store.detail("conversations", "c1")["revision"]

    def test_revision_on_dinh_sau_180_giay(self):
        """Go cau tra loi qua 3 phut khong duoc bien thanh xung dot."""
        vua_toi = self._revision(MOC + 10)       # con "waiting"
        lau_sau = self._revision(MOC + 600)      # da thanh "unanswered"
        self.assertEqual(vua_toi, lau_sau,
                         "revision khong duoc doi chi vi dong ho troi")

    def test_detail_hai_lan_lien_tiep_cung_revision(self):
        a = self._revision(MOC + 10)
        b = self._revision(MOC + 10)
        self.assertEqual(a, b)

    def test_doc_chi_tiet_khong_ghi_vao_db(self):
        """detail() phai idempotent — khong duoc co tac dung phu."""
        def chup():
            with self.store._db() as db:
                return [tuple(r) for r in db.execute(
                    "SELECT example_id,status,updated_at FROM approved_reply_examples")]
        truoc = chup()
        for _ in range(3):
            self._revision(MOC + 10)
        self.assertEqual(chup(), truoc)

    def test_hang_xom_doi_khong_lam_doi_revision_cua_minh(self):
        self.rows.append(_dong_tho("c2", "con hang khong", MOC + 5, user="u1"))
        truoc = self._revision(MOC + 10)
        self.store.correct({"resource": "conversations", "record_id": "c2",
                            "changes": {"operator_note": "ghi chu"},
                            "reason": "thu"})
        self.assertEqual(self._revision(MOC + 10), truoc,
                         "sua hoi thoai khac cua cung khach khong duoc dung toi day")


class LuuDuocTests(unittest.TestCase):
    def setUp(self):
        self.rows = [_dong_tho("c1", "dam ca gia nhieu", MOC)]
        self.store, self._tmp = _store(self.rows)
        self.addCleanup(self._tmp.cleanup)

    def test_luu_sau_ba_phut_van_thanh_cong(self):
        with mock.patch.object(workspace_module.time, "time", return_value=MOC + 10):
            rev = self.store.detail("conversations", "c1")["revision"]
        # Thao tac vien go cau tra loi trong 5 phut roi moi bam Luu.
        with mock.patch.object(workspace_module.time, "time", return_value=MOC + 300):
            ket = self.store.patch("conversations", "c1", {
                "expected_revision": rev,
                "changes": {"correct_answer": "Gia goc 555k nha anh"},
                "reason": "sua cau tra loi"})
        self.assertTrue(ket["ok"])

    def test_luu_lan_hai_trong_cung_phien_van_thanh_cong(self):
        rev = self.store.detail("conversations", "c1")["revision"]
        lan1 = self.store.patch("conversations", "c1", {
            "expected_revision": rev,
            "changes": {"correct_answer": "Lan mot"}, "reason": "sua"})
        self.assertIn("revision", lan1, "phai tra revision moi cho giao dien")
        self.assertNotEqual(lan1["revision"], rev, "luu xong thi revision phai doi")
        lan2 = self.store.patch("conversations", "c1", {
            "expected_revision": lan1["revision"],
            "changes": {"correct_answer": "Lan hai"}, "reason": "sua tiep"})
        self.assertTrue(lan2["ok"])

    def test_revision_cu_van_bi_chan_dung_luc_can_chan(self):
        """Chong ghi de that: nguoi khac vua sua thi minh phai bi chan."""
        rev = self.store.detail("conversations", "c1")["revision"]
        self.store.correct({"resource": "conversations", "record_id": "c1",
                            "changes": {"operator_note": "nguoi khac sua"},
                            "reason": "thu"})
        with self.assertRaisesRegex(ValueError, "revision_conflict"):
            self.store.patch("conversations", "c1", {
                "expected_revision": rev,
                "changes": {"correct_answer": "ghi de"}, "reason": "sua"})


class KichThuocChiTietTests(unittest.TestCase):
    """Mot lan bam xem chi tiet tung sinh 218 MB JSON."""

    def setUp(self):
        self.rows = [_dong_tho(f"c{i}", f"cau hoi thu {i}", MOC + i, user="u1")
                     for i in range(40)]
        self.store, self._tmp = _store(self.rows)
        self.addCleanup(self._tmp.cleanup)

    def test_chi_tiet_khong_long_cay_ban_ghi(self):
        import json
        d = self.store.detail("conversations", "c39")
        so_byte = len(json.dumps(d, ensure_ascii=False, default=str))
        self.assertLess(so_byte, 200_000,
                        f"chi tiet phinh {so_byte} byte — cay ban ghi long nhau")

    def test_binh_luan_truoc_la_ban_sao_rut_gon(self):
        d = self.store.detail("conversations", "c39")
        truoc = d["data"].get("previous_customer_comments") or []
        self.assertTrue(truoc)
        for muc in truoc:
            self.assertNotIn("previous_customer_comments", muc,
                             "khong duoc tro thang sang ban ghi day du")
            self.assertNotIn("effective", muc)
            self.assertEqual(set(muc), {"id", "ts", "original_text", "question_type"})

    def test_detail_khong_di_vong_qua_list(self):
        with mock.patch.object(self.store, "list",
                               side_effect=AssertionError("detail khong duoc goi list()")):
            self.store.detail("conversations", "c39")

    def test_detail_mo_it_ket_noi_sqlite(self):
        dem = {"n": 0}
        goc = workspace_module.WorkspaceStore._connect

        def dem_connect(self):
            dem["n"] += 1
            return goc(self)

        workspace_module.WorkspaceStore._connect = dem_connect
        self.addCleanup(setattr, workspace_module.WorkspaceStore, "_connect", goc)
        self.store.detail("conversations", "c39")
        self.assertLessEqual(dem["n"], 12,
                             f"mo {dem['n']} ket noi SQLite cho mot lan bam")


if __name__ == "__main__":
    unittest.main()


class TimKiemTests(unittest.TestCase):
    """Bo `json.dumps` tren ban ghi day du nhung KHONG duoc thu hep tim kiem."""

    def setUp(self):
        self.rows = [
            _dong_tho("c1", "dam ca amino gia nhieu", MOC, user="u1"),
            _dong_tho("c2", "kich re tc01 con hang khong", MOC + 5, user="u2"),
        ]
        self.store, self._tmp = _store(self.rows)
        self.addCleanup(self._tmp.cleanup)

    def _tim(self, tu_khoa):
        return [r["id"] for r in self.store.list(
            "conversations", {"q": tu_khoa, "limit": 50})["items"]]

    def test_tim_theo_noi_dung_binh_luan(self):
        self.assertEqual(self._tim("dam ca"), ["c1"])
        self.assertEqual(self._tim("kich re"), ["c2"])

    def test_tim_theo_ten_khach(self):
        self.assertIn("c1", self._tim("khachu1"))

    def test_tim_theo_id(self):
        self.assertEqual(self._tim("c2"), ["c2"])

    def test_tim_theo_noi_dung_DA_SUA(self):
        """Nguoi van hanh sua cau tra loi thi phai tim duoc theo ban sua."""
        self.store.correct({"resource": "conversations", "record_id": "c1",
                            "changes": {"correct_answer": "chuoi rat dac biet zzz"},
                            "reason": "thu"})
        self.assertEqual(self._tim("rat dac biet zzz"), ["c1"])

    def test_khong_tim_thay_thi_tra_rong(self):
        self.assertEqual(self._tim("khong he ton tai"), [])
