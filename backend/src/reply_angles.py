"""Gợi ý "góc tiếp cận" + "kiểu mở/khép" cho từng lượt trả lời (C-ai mục 5, C10).

Chỉ là gợi ý mềm đặt ở PHẦN THEO LƯỢT của prompt (không đụng phần tĩnh để giữ cache).
Danh sách đọc từ prompts/reply_angles.json để chủ shop chỉnh; file hỏng thì dùng mặc định.
"""
from __future__ import annotations

import json
import random
import threading
from collections import deque

DEFAULT_ANGLES = {
    "goc_tiep_can": [
        {"id": "vao_thang", "weight": 3, "text": "vào thẳng ý khách hỏi, gọn và chắc"},
        {"id": "hoi_lai", "weight": 2, "text": "hỏi lại một ý cần để tư vấn đúng (cây gì, diện tích, giai đoạn)"},
        {"id": "loi_ich", "weight": 2, "text": "nhắc một lợi ích chính của sản phẩm gắn với nhu cầu khách"},
        {"id": "ma_uu_dai", "weight": 1, "text": "nhắc khách xem mã/ưu đãi riêng trên tài khoản"},
        {"id": "so_sanh_nhe", "weight": 1, "text": "so sánh nhẹ để khách dễ hình dung"},
        {"id": "kinh_nghiem", "weight": 1, "text": "kể ngắn kinh nghiệm nhà vườn hay dùng"},
    ],
    "kieu_mo_khep": [
        {"id": "khong_dem", "weight": 3, "text": "mở thẳng vào nội dung, không từ đệm"},
        {"id": "goi_ten", "weight": 2, "text": "mở bằng cách gọi anh/chị tự nhiên"},
        {"id": "da_vang", "weight": 1, "text": "mở bằng dạ/vâng nhẹ nhàng"},
        {"id": "cam_than_nhe", "weight": 1, "text": "mở bằng một câu cảm thán nhẹ, hợp ngữ cảnh (không Ơ/Ủa)"},
    ],
}
GROUPS = ("goc_tiep_can", "kieu_mo_khep")


def _clean(items):
    out = []
    for item in items if isinstance(items, list) else []:
        try:
            text, weight = str(item.get("text") or "").strip(), float(item.get("weight", 1))
        except (AttributeError, TypeError, ValueError):
            continue
        if text and weight > 0:
            out.append({"id": str(item.get("id") or text), "weight": weight, "text": text[:200]})
    return out


def load(path=None):
    """Đọc danh sách; nhóm nào thiếu/hỏng thì dùng mặc định của nhóm đó."""
    data = {}
    if path is None:
        try:
            import config
            path = config.REPLY_ANGLES_PATH
        except Exception:  # noqa: BLE001
            path = None
    if path is not None:
        try:
            with open(path, encoding="utf-8-sig") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            data = {}
    if not isinstance(data, dict):
        data = {}
    return {group: (_clean(data.get(group)) or _clean(DEFAULT_ANGLES[group])) for group in GROUPS}


class AnglePicker:
    """Chọn ngẫu nhiên có trọng số, tránh các mục đã dùng trong ``avoid_last`` lượt gần nhất."""

    def __init__(self, options=None, *, avoid_last=3, rng=None):
        self.options = options if options is not None else load()
        self.rng = rng or random.Random()
        self.history = {group: deque(maxlen=max(0, avoid_last)) for group in GROUPS}
        self._lock = threading.Lock()

    def _choose(self, group):
        items = self.options.get(group) or []
        if not items:
            return None
        recent = set(self.history[group])
        pool = [item for item in items if item["id"] not in recent]
        if not pool:  # danh sách quá ngắn: chỉ tránh mục vừa dùng
            last = self.history[group][-1] if self.history[group] else None
            pool = [item for item in items if item["id"] != last] or items
        item = self.rng.choices(pool, weights=[it["weight"] for it in pool], k=1)[0]
        self.history[group].append(item["id"])
        return item

    def pick(self):
        with self._lock:
            angle, opener = self._choose("goc_tiep_can"), self._choose("kieu_mo_khep")
        return {"goc_tiep_can": angle["text"] if angle else "",
                "kieu_mo_khep": opener["text"] if opener else "",
                "_ids": (angle and angle["id"], opener and opener["id"])}

    def hint(self):
        """Gợi ý mềm cho phần theo lượt; '' nếu không có danh sách."""
        chosen = self.pick()
        parts = []
        if chosen["goc_tiep_can"]:
            parts.append(f"góc tiếp cận: {chosen['goc_tiep_can']}")
        if chosen["kieu_mo_khep"]:
            parts.append(f"cách mở/khép: {chosen['kieu_mo_khep']}")
        if not parts:
            return ""
        return ("Gợi ý cho lượt này (ưu tiên nếu hợp, có thể bỏ nếu không hợp câu khách hoặc trái dữ liệu): "
                + "; ".join(parts) + ".")
