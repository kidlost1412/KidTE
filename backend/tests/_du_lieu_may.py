r"""Ho tro cho cac test KIEM DU LIEU MAY (khac voi unit test quy tac).

Mot so test khoa lai quyet định mapping tren dung du lieu dang chay cua may
van hanh. Du lieu do la du lieu RIENG, khong duoc dua vao ma nguon phat hanh
(xem F09), nen tren mot ban checkout sach chung khong co gi de doc.

Truoc day chung fail thang khi thieu file, khien bo test cua ban ban giao
khong bao gio xanh. Nay:
  - Quy tac duoc phu bang fixture tu chua o cac test ben canh (luon chay).
  - Cac test doi chieu du lieu that chi chay khi du lieu that co mat, va bao
    ro ly do khi bo qua — khong am tham bien mat.

Tren may van hanh, chi ro du lieu that bang bien moi truong RIENG cua test:
    set KID_TE_TEST_DATA=D:\KidTE\DuLieu\data

Co y khong dung KID_TE_ROOT: bien do con doi ca PLUGINS_DIR (ROOT.parent/
plugins) nen tro no ra ngoai cay nguon se lam hong viec nap plugin.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

LY_DO = (
    "Thieu du lieu van hanh cua may (day la du lieu rieng, khong nam trong ma "
    "nguon). Dat KID_TE_TEST_DATA tro toi thu muc DuLieu\\data de chay kiem "
    "tra nay."
)


def goc_du_lieu_may():
    """Thu muc `data` that, neu nguoi chay da chi ro va no ton tai."""
    override = (os.getenv("KID_TE_TEST_DATA", "") or os.getenv("LIVE_RE_TEST_DATA", "")).strip()
    if override:
        goc = Path(override).expanduser()
        if goc.is_dir():
            return goc
    mac_dinh = Path(__file__).resolve().parents[1] / "data"
    return mac_dinh if mac_dinh.is_dir() else None


def can_file(*duong_dan_tuong_doi):
    """Tra ve goc du lieu; bo qua test neu thieu bat ky file nao can doc."""
    goc = goc_du_lieu_may()
    if goc is None:
        pytest.skip(LY_DO)
    thieu = [str(p) for p in duong_dan_tuong_doi
             if not (goc / p).is_file()]
    if thieu:
        pytest.skip(f"{LY_DO} Thieu: {', '.join(thieu)}")
    return goc
