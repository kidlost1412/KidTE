"""Khởi tạo thư mục dữ liệu cho bản cài đặt.

Máy mới cài là máy TRẮNG: không mang theo khách hàng, Trí nhớ AI, hồ sơ sản phẩm,
thông tin shop hay khoá DeepSeek của bất kỳ máy nào khác. Chủ shop tự nhập, tự tạo
hồ sơ và tải tài liệu của mình lên.
"""
from __future__ import annotations

from config import IS_FROZEN, ROOT

# Giữ tên hằng để test đóng gói kiểm "spec và runtime cùng rỗng".
SEED_PATHS = ()


def prepare_user_layout():
    if not IS_FROZEN:
        return {"created": [], "root": str(ROOT)}
    for relative in ("data/machine", "logs", "backups", "runtime", "reports"):
        (ROOT / relative).mkdir(parents=True, exist_ok=True)
    return {"created": [], "root": str(ROOT)}
