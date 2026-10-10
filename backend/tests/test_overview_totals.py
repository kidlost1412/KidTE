"""Hoi quy F02: tong so lieu phai doc lap voi phan trang.

Ban cu: overview goi workspace.list(..., {"limit": 100}) roi dem len(items).
_paginate lai chan limit o 100, nen voi 234 hoi thoai thi raw_stream_rows,
business_session_count, ai_replied, manual_replied va unanswered deu dung
o con so cua dung mot trang.
"""
import sys
import types
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import workspace as workspace_module  # noqa: E402


def _dung_workspace(so_hoi_thoai, so_phien):
    store = workspace_module.WorkspaceStore.__new__(workspace_module.WorkspaceStore)
    hoi_thoai = []
    for i in range(so_hoi_thoai):
        hoi_thoai.append({
            "id": f"c{i}",
            "responder": "ai" if i % 3 == 0 else ("manual" if i % 3 == 1 else ""),
            "conversation_status": "waiting" if i % 5 == 0 else "answered",
        })
    phien = [{"id": f"s{i}"} for i in range(so_phien)]
    store._raw_events = lambda: list(hoi_thoai)
    store._derived_live_rows = lambda resource: list(phien) if resource == "sessions" else []
    store._memory_rows = lambda resource: []
    store._trashed_ids = lambda resource: set()
    store.engine = types.SimpleNamespace()
    return store, hoi_thoai, phien


class OverviewTotalsTests(unittest.TestCase):
    def test_tong_khong_bi_chan_o_mot_trang(self):
        store, hoi_thoai, phien = _dung_workspace(234, 137)
        totals = store.totals()
        self.assertEqual(totals["conversations"], 234,
                         "tong hoi thoai phai la 234, khong phai 100")
        self.assertEqual(totals["sessions"], 137)
        self.assertEqual(totals["ai_replied"],
                         sum(r["responder"] == "ai" for r in hoi_thoai))
        self.assertEqual(totals["manual_replied"],
                         sum(r["responder"] == "manual" for r in hoi_thoai))
        self.assertEqual(totals["unanswered"],
                         sum(r["conversation_status"] == "waiting" for r in hoi_thoai))
        # Va thuc su lon hon mot trang, neu khong test nay vo nghia.
        self.assertGreater(totals["ai_replied"], 0)
        self.assertGreater(totals["conversations"], 100)

    def test_bo_qua_ban_ghi_trong_thung_rac(self):
        store, _, _ = _dung_workspace(10, 4)
        store._trashed_ids = lambda resource: {"c0", "c1"} if resource == "conversations" else set()
        self.assertEqual(store.totals()["conversations"], 8)

    def test_tap_rong_van_ra_so_khong(self):
        store, _, _ = _dung_workspace(0, 0)
        totals = store.totals()
        self.assertEqual(totals["conversations"], 0)
        self.assertEqual(totals["unanswered"], 0)


if __name__ == "__main__":
    unittest.main()
