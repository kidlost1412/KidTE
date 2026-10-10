"""Hoi quy F08: doi khoa thi MOI duong deu phai dung khoa moi.

Loi goc: configure_api_key() thay self.client bang client dung khoa moi,
nhung check_balance() lai dung header tu hang so DEEPSEEK_API_KEY — gia tri
nap tu .env luc import module. Sau khi nguoi dung doi khoa trong giao dien:
  - cau tra loi di bang khoa MOI
  - kiem so du van hoi bang khoa CU
Nen man hinh co the bao "het so du" hoac "chua cau hinh khoa" trong khi khoa
moi hoan toan tot, va nguoc lai.
"""
from __future__ import annotations

import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

GOC = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(GOC / "backend" / "src"))


def _nap_plugin():
    """Nap processor cua plugin theo duong dan, khong dua thu muc plugin vao
    sys.path — lam vay se khien moi `import processor` khac trong bo test tro
    nham sang day."""
    import importlib.util

    duong_dan = GOC / "plugins" / "model-deepseek" / "processor.py"
    spec = importlib.util.spec_from_file_location("_plugin_model_deepseek", duong_dan)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bo_xu_ly = _nap_plugin()


class _PhanHoi:
    """Gia lap phan hoi HTTP cua /user/balance."""

    def __init__(self, payload):
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _responder(khoa_dau="khoa-cu"):
    r = bo_xu_ly.DeepSeekResponder.__new__(bo_xu_ly.DeepSeekResponder)
    import threading
    r.client = object()
    r.api_key = khoa_dau
    r._balance_lock = threading.Lock()
    r._balance_checked_at = 0.0
    r._balance_status = None
    r._models = None
    r._models_checked_at = 0.0
    r.model = "model-cu"
    return r


class DoiKhoaTests(unittest.TestCase):
    def _bat_khoa(self):
        """Ghi lai khoa that su duoc gui trong header Authorization."""
        da_dung = []

        def urlopen(request, timeout=None, context=None):
            da_dung.append(request.headers.get("Authorization")
                           or request.get_header("Authorization"))
            return _PhanHoi({"is_available": True})

        return da_dung, urlopen

    def test_kiem_so_du_dung_khoa_dang_hoat_dong(self):
        r = _responder("khoa-cu")
        da_dung, urlopen = self._bat_khoa()
        with mock.patch.object(bo_xu_ly.urllib.request, "urlopen", urlopen):
            r.check_balance(max_age=0)
        self.assertEqual(da_dung, ["Bearer khoa-cu"])

    def test_sau_khi_doi_khoa_thi_kiem_so_du_dung_khoa_moi(self):
        r = _responder("khoa-cu")
        # Doi khoa: client moi tra ve danh sach model hop le.
        ds_model = types.SimpleNamespace(data=[types.SimpleNamespace(id="model-moi")])
        khach_moi = types.SimpleNamespace(
            models=types.SimpleNamespace(list=lambda: ds_model))
        with mock.patch.object(bo_xu_ly.DeepSeekResponder, "_new_client",
                               staticmethod(lambda key: khach_moi)):
            r.configure_api_key("khoa-moi")

        self.assertEqual(r.api_key, "khoa-moi")
        da_dung, urlopen = self._bat_khoa()
        with mock.patch.object(bo_xu_ly.urllib.request, "urlopen", urlopen):
            r.check_balance(max_age=0)
        self.assertEqual(da_dung, ["Bearer khoa-moi"],
                         "kiem so du phai hoi bang khoa vua duoc cau hinh")

    def test_doi_khoa_lam_mat_hieu_luc_ket_qua_so_du_cu(self):
        """Khoa cu het so du, khoa moi con tien: khong duoc tra ket qua cu."""
        r = _responder("khoa-cu")

        def urlopen_het_tien(request, timeout=None, context=None):
            return _PhanHoi({"is_available": False})

        with mock.patch.object(bo_xu_ly.urllib.request, "urlopen", urlopen_het_tien):
            cu = r.check_balance(max_age=300)
        self.assertFalse(cu["deepseek_available"])

        ds_model = types.SimpleNamespace(data=[types.SimpleNamespace(id="model-moi")])
        khach_moi = types.SimpleNamespace(
            models=types.SimpleNamespace(list=lambda: ds_model))
        with mock.patch.object(bo_xu_ly.DeepSeekResponder, "_new_client",
                               staticmethod(lambda key: khach_moi)):
            r.configure_api_key("khoa-moi")

        def urlopen_con_tien(request, timeout=None, context=None):
            return _PhanHoi({"is_available": True})

        with mock.patch.object(bo_xu_ly.urllib.request, "urlopen", urlopen_con_tien):
            moi = r.check_balance(max_age=300)
        self.assertTrue(moi["deepseek_available"],
                        "cache so du cua khoa cu khong duoc song sot qua lan doi khoa")

    def test_khong_co_khach_thi_bao_thieu_khoa(self):
        r = _responder()
        r.client = None
        ket = r.check_balance(max_age=0)
        self.assertFalse(ket["deepseek_available"])
        self.assertEqual(ket["reason"], "deepseek_key_missing")


if __name__ == "__main__":
    unittest.main()
