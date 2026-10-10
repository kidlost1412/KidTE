"""Hoi quy F10: hop dong capability phai khop dung handler.

catalog() suy ra `actions` tu co `editable` cua bang tai nguyen:
    "actions": (["patch","rollback"] if editable else []) + (["correct"] ...)
Nhung patch() chi xu ly profiles / videos / conversations, va rollback() chi
xu ly profiles / videos. Ket qua do duoc qua HTTP that:
    catalog cong bo  products -> ["patch","rollback"]
    PATCH products   -> 400 workspace_resource_read_only
Giao dien vi the bay ra nut cho mot thao tac chac chan that bai.
"""
from __future__ import annotations

import ast
import sys
import types
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import workspace as workspace_module  # noqa: E402

Store = workspace_module.WorkspaceStore


def _catalog():
    store = Store.__new__(Store)
    store.engine = types.SimpleNamespace()
    return store.catalog()["resources"]


class HopDongCapabilityTests(unittest.TestCase):
    def test_moi_thao_tac_cong_bo_deu_co_handler_that(self):
        for row in _catalog():
            for thao_tac, tap in (
                ("patch", Store.RESOURCE_PATCH_DUOC),
                ("rollback", Store.RESOURCE_ROLLBACK_DUOC),
                ("correct", Store.RESOURCE_CORRECT_DUOC),
            ):
                if thao_tac in row["actions"]:
                    self.assertIn(row["id"], tap,
                                  f"{row['id']} cong bo {thao_tac} ma handler khong ho tro")

    def test_moi_tai_nguyen_handler_ho_tro_deu_duoc_cong_bo(self):
        cong_bo = {row["id"]: set(row["actions"]) for row in _catalog()}
        for thao_tac, tap in (
            ("patch", Store.RESOURCE_PATCH_DUOC),
            ("rollback", Store.RESOURCE_ROLLBACK_DUOC),
            ("correct", Store.RESOURCE_CORRECT_DUOC),
        ):
            for rid in tap:
                self.assertIn(rid, cong_bo, f"{rid} khong co trong catalog")
                self.assertIn(thao_tac, cong_bo[rid],
                              f"{rid} ho tro {thao_tac} nhung khong duoc cong bo")

    def test_products_khong_con_bi_cong_bo_nham_la_sua_duoc(self):
        products = next(r for r in _catalog() if r["id"] == "products")
        self.assertEqual(products["actions"], [],
                         "products khong co handler patch/rollback/correct nao")
        self.assertFalse(products["editable"])

    def test_conversations_cong_bo_dung_patch_va_correct(self):
        conv = next(r for r in _catalog() if r["id"] == "conversations")
        self.assertIn("patch", conv["actions"])
        self.assertIn("correct", conv["actions"])
        self.assertNotIn("rollback", conv["actions"],
                         "rollback() khong ho tro conversations")

    def test_patch_tu_choi_tai_nguyen_ngoai_danh_sach(self):
        store = Store.__new__(Store)
        with self.assertRaises(ValueError) as ctx:
            store.patch("products", "x", {"changes": {}})
        self.assertEqual(str(ctx.exception), "workspace_resource_read_only")

    def test_rollback_noi_ro_khong_ho_tro_thay_vi_bao_thieu_lich_su(self):
        store = Store.__new__(Store)
        with self.assertRaises(ValueError) as ctx:
            store.rollback("products", "x")
        self.assertEqual(str(ctx.exception), "rollback_not_supported")

    def test_handler_patch_thuc_su_xu_ly_dung_nhung_tai_nguyen_da_khai(self):
        """Doc than ham patch() de chac chan danh sach hang so khong bi lech
        voi cac nhanh if/elif that su co trong ma."""
        cay = ast.parse((SRC / "workspace.py").read_text(encoding="utf-8"))
        cac_ham = [n for n in ast.walk(cay)
                   if isinstance(n, ast.FunctionDef) and n.name in {"patch", "_patch_apply"}]
        nhanh = set()
        for node in (x for ham in cac_ham for x in ast.walk(ham)):
            if (isinstance(node, ast.Compare) and isinstance(node.left, ast.Name)
                    and node.left.id == "resource"
                    and isinstance(node.ops[0], ast.Eq)
                    and isinstance(node.comparators[0], ast.Constant)):
                nhanh.add(node.comparators[0].value)
        self.assertEqual(nhanh, set(Store.RESOURCE_PATCH_DUOC),
                         "cac nhanh trong patch() lech voi RESOURCE_PATCH_DUOC")


if __name__ == "__main__":
    unittest.main()
