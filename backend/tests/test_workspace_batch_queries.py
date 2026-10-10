"""Hoi quy hieu nang: khong duoc mo mot ket noi SQLite cho MOI dong.

Do duoc tren du lieu that (234 hoi thoai): moi lan ve /api/dashboard mo
khoang 1450 ket noi SQLite va chay 1450 cau lenh, chiem 94% thoi gian
(848ms trung vi). Man hinh nay duoc poll moi 5 giay.
"""
import sys
import types
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import workspace as workspace_module


class GopTruyVanTests(unittest.TestCase):
    def test_raw_events_khong_mo_ket_noi_theo_tung_dong(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = workspace_module.WorkspaceStore.__new__(workspace_module.WorkspaceStore)
        store.root = Path(tmp.name)
        (store.root / "data").mkdir(parents=True, exist_ok=True)
        store.db_path = store.root / "data" / "ws.sqlite3"
        store.engine = types.SimpleNamespace(
            mapping_state=lambda: {"products": []},
            extension_heartbeat={})
        store._create_schema()

        dem = {"n": 0}
        goc = workspace_module.WorkspaceStore._connect

        def dem_connect(self):
            dem["n"] += 1
            return goc(self)

        workspace_module.WorkspaceStore._connect = dem_connect
        self.addCleanup(setattr, workspace_module.WorkspaceStore, "_connect", goc)

        so_dong = 200
        store._read_raw_rows = lambda: [
            {"id": f"c{i}", "msg_id": f"m{i}", "text": "hoi gia", "_at": 0.0,
             "nickname": f"khach{i}", "unique_id": f"u{i}", "user_id": f"u{i}"}
            for i in range(so_dong)
        ]
        store._products = lambda: []
        store._raw_events()
        self.assertLess(dem["n"], 30,
                        f"mo {dem['n']} ket noi SQLite cho {so_dong} dong — "
                        f"phai nap theo lo, khong phai theo tung dong")

    def test_co_ham_nap_theo_lo(self):
        for ten in ("_corrections_theo_lo", "_examples_theo_lo"):
            self.assertTrue(hasattr(workspace_module.WorkspaceStore, ten),
                            f"thieu {ten}")


if __name__ == "__main__":
    unittest.main()
