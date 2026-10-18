"""Gợi ý "góc tiếp cận" + "kiểu mở/khép" cho từng lượt trả lời (C-ai mục 5, C10).

Chỉ là gợi ý mềm đặt ở PHẦN THEO LƯỢT của prompt (không đụng phần tĩnh để giữ cache).
Danh sách đọc từ prompts/reply_angles.json để chủ shop chỉnh; file hỏng thì dùng mặc định.
"""
from __future__ import annotations

import json
import random
import threading
from collections import OrderedDict, deque

# `hop` (tuỳ chọn): góc chỉ dùng khi câu khách thuộc một trong các chủ đề này
# (price, dose, usage, recommendation, smalltalk, general). Không có `hop` = hợp mọi câu.
# Rà soát 17/10 P4: bỏ các góc trái lời nhắc (mở bằng "dạ", tự nhắc mã ưu đãi, tự so sánh,
# "nhà vườn hay dùng" dễ thành lời chứng thực bịa).
DEFAULT_ANGLES = {
    "goc_tiep_can": [
        {"id": "vao_thang", "weight": 3, "text": "vào thẳng ý khách hỏi, gọn và chắc"},
        {"id": "hoi_lai", "weight": 2, "text": "hỏi lại một ý cần để tư vấn đúng (cây gì, diện tích, giai đoạn)",
         "hop": ["recommendation", "usage", "general"]},
        {"id": "loi_ich", "weight": 2, "text": "nhắc một lợi ích chính của sản phẩm gắn với nhu cầu khách",
         "hop": ["price", "usage", "recommendation", "general"]},
        {"id": "meo_dung", "weight": 1, "text": "thêm một mẹo dùng/thời điểm dùng hữu ích",
         "hop": ["dose", "usage"]},
        {"id": "dieu_kien_ngan", "weight": 2, "text": "nói kết luận rồi một điều kiện ngắn (cây, giai đoạn, cách pha)",
         "hop": ["dose", "usage"]},
        {"id": "chot_nhe", "weight": 2, "text": "khép bằng một lời mời đặt nhẹ, tự nhiên",
         "hop": ["price", "recommendation"]},
        {"id": "bat_y_khach", "weight": 3, "text": "bắt đúng một ý khách vừa nói, đáp có duyên",
         "hop": ["smalltalk"]},
    ],
    "kieu_mo_khep": [
        {"id": "khong_dem", "weight": 3, "text": "mở thẳng vào nội dung, không từ đệm"},
        {"id": "goi_ten", "weight": 2, "text": "mở bằng cách gọi anh/chị tự nhiên"},
        {"id": "cam_than_nhe", "weight": 1, "text": "mở bằng một câu cảm thán nhẹ, hợp ngữ cảnh (không Ơ/Ủa)",
         "hop": ["smalltalk", "price", "recommendation", "general"]},
        {"id": "khep_hoi", "weight": 1, "text": "khép bằng một câu hỏi ngắn"},
        {"id": "khep_tron", "weight": 2, "text": "khép gọn bằng ý chính, không thêm lời mời"},
    ],
}
TOPICS = ("price", "dose", "usage", "recommendation", "smalltalk", "general")


def topics_from_intents(intents):
    """Ý định (knowledge.intents + router) -> chủ đề chọn góc."""
    intents = intents or {}
    topics = set()
    if intents.get("price") or intents.get("promotion"):
        topics.add("price")
    if intents.get("dose"):
        topics.add("dose")
    if intents.get("mixing") or intents.get("technical") or intents.get("after_planting"):
        topics.add("usage")
    if intents.get("recommendation"):
        topics.add("recommendation")
    return tuple(sorted(topics)) or ("general",)


GROUPS = ("goc_tiep_can", "kieu_mo_khep")


def _clean(items):
    out = []
    for item in items if isinstance(items, list) else []:
        try:
            text, weight = str(item.get("text") or "").strip(), float(item.get("weight", 1))
        except (AttributeError, TypeError, ValueError):
            continue
        if text and weight > 0:
            row = {"id": str(item.get("id") or text), "weight": weight, "text": text[:200]}
            fits = item.get("hop")
            if isinstance(fits, list) and fits:
                row["hop"] = [str(t) for t in fits]
            out.append(row)
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
    """Chọn có trọng số trong các góc HỢP chủ đề câu khách; tránh mục đã dùng trong ``avoid_last`` lượt
    gần nhất của cả phiên và không lặp góc liên tiếp với cùng một khách."""

    MAX_CUSTOMERS = 500

    def __init__(self, options=None, *, avoid_last=3, rng=None):
        self.options = options if options is not None else load()
        self.rng = rng or random.Random()
        self.history = {group: deque(maxlen=max(0, avoid_last)) for group in GROUPS}
        self.per_customer = OrderedDict()
        self._lock = threading.Lock()

    def _choose(self, group, topics, customer):
        items = self.options.get(group) or []
        topics = set(topics or ())
        fitting = [item for item in items if not item.get("hop") or topics & set(item["hop"])] if topics else items
        items = fitting or items
        if not items:
            return None
        last_for_customer = (self.per_customer.get(customer) or {}).get(group) if customer else None
        recent = set(self.history[group]) | ({last_for_customer} if last_for_customer else set())
        pool = [item for item in items if item["id"] not in recent]
        if not pool:  # danh sách quá ngắn: chỉ tránh mục vừa dùng (với khách này, rồi của phiên)
            last = last_for_customer or (self.history[group][-1] if self.history[group] else None)
            pool = [item for item in items if item["id"] != last] or items
        item = self.rng.choices(pool, weights=[it["weight"] for it in pool], k=1)[0]
        self.history[group].append(item["id"])
        return item

    def pick(self, topics=None, customer=""):
        with self._lock:
            angle = self._choose("goc_tiep_can", topics, customer)
            opener = self._choose("kieu_mo_khep", topics, customer)
            if customer:
                self.per_customer[customer] = {"goc_tiep_can": angle and angle["id"],
                                               "kieu_mo_khep": opener and opener["id"]}
                self.per_customer.move_to_end(customer)
                while len(self.per_customer) > self.MAX_CUSTOMERS:
                    self.per_customer.popitem(last=False)
        return {"goc_tiep_can": angle["text"] if angle else "",
                "kieu_mo_khep": opener["text"] if opener else "",
                "_ids": (angle and angle["id"], opener and opener["id"])}

    def hint(self, topics=None, customer=""):
        """Gợi ý mềm cho phần theo lượt; '' nếu không có danh sách."""
        chosen = self.pick(topics, customer)
        parts = []
        if chosen["goc_tiep_can"]:
            parts.append(f"góc tiếp cận: {chosen['goc_tiep_can']}")
        if chosen["kieu_mo_khep"]:
            parts.append(f"cách mở/khép: {chosen['kieu_mo_khep']}")
        if not parts:
            return ""
        return ("Gợi ý cho lượt này (ưu tiên nếu hợp, có thể bỏ nếu không hợp câu khách hoặc trái dữ liệu): "
                + "; ".join(parts) + ".")
