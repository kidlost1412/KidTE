"""Số phiên bản: KHÔNG ghi cứng ở đâu cả.

``VERSION`` ở gốc dự án chỉ giữ phần chính (vd ``1.2``) — người phát triển đổi
khi có thay đổi lớn. Lúc đóng gói, ``packaging/build.ps1`` ghép thêm số lần
commit (vd ``1.4.96``) và ghi vào ``VERSION`` cạnh ``KidTE.exe``; bộ cài dùng
đúng số đó. Chạy từ mã nguồn thì hiện ``1.2-dev``.
"""
from __future__ import annotations

from config import INSTALL_ROOT, IS_FROZEN, PROJECT_ROOT


def app_version():
    candidates = [INSTALL_ROOT / "VERSION"] if IS_FROZEN else [PROJECT_ROOT / "VERSION"]
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            return text if IS_FROZEN else f"{text}-dev"
    return "0.0-dev"
