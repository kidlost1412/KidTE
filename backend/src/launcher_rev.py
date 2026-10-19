"""Phát hiện KidTELauncher.exe cũ (đóng băng trong exe, không tự cập nhật theo gói app).

Launcher mới đặt KIDTE_LAUNCHER_REV khi khởi chạy core (xem update_launcher.LAUNCHER_REV).
Core chạy dưới launcher (có KIDTE_LAUNCH_NONCE) mà thiếu/thấp hơn REQUIRED_LAUNCHER_REV
thì báo `launcher_outdated` để giao diện nhắc chạy bộ cài mới. Chạy trực tiếp: không báo.
"""
from __future__ import annotations

import os

# Tăng cùng với update_launcher.LAUNCHER_REV khi gói app cần hành vi launcher mới.
REQUIRED_LAUNCHER_REV = 2
RELEASE_REPOSITORY = "kidlost1412/KidTE"


def _parse_rev(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def launcher_status(app_version, series="p4", environ=None):
    env = os.environ if environ is None else environ
    if not (env.get("KIDTE_LAUNCH_NONCE") or "").strip():
        return {"launcher_outdated": False}
    rev = _parse_rev(env.get("KIDTE_LAUNCHER_REV"))
    if rev >= REQUIRED_LAUNCHER_REV:
        return {"launcher_outdated": False, "launcher_rev": rev}
    result = {"launcher_outdated": True, "launcher_rev": rev,
              "launcher_required_rev": REQUIRED_LAUNCHER_REV}
    if app_version:
        result["launcher_download_url"] = (
            f"https://github.com/{RELEASE_REPOSITORY}/releases/tag/v{app_version}-{series}")
    return result
