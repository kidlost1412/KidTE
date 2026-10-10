"""Thu dữ liệu độc lập với trạng thái bật/tắt AI."""
from __future__ import annotations

import json
import os
import re
import threading
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

from knowledge import clean


SETTINGS_SCHEMA = [
    {"key": "ghi_luong", "type": "bool", "default": True, "group": "Ghi dữ liệu",
     "label": "Ghi lại mọi bình luận của phiên LIVE",
     "help": "Hộp thư, Khách hàng và việc AI tự học sau mỗi LIVE đều đọc từ đây. Tắt thì các trang "
             "đó không có dữ liệu mới — chỉ tắt khi thử nghiệm."},
    {"key": "giu_so_ngay", "type": "number", "integer": True, "default": 0, "min": 0, "max": 3650,
     "step": 30, "group": "Dọn dữ liệu cũ", "label": "Chỉ giữ bình luận của N ngày gần nhất (0 = giữ mãi)",
     "help": "Mỗi ngày LIVE chiếm khoảng 50–200 KB. Xoá bớt thì hội thoại cũ biến mất khỏi Hộp thư."},
]
SETTINGS = {field["key"]: field["default"] for field in SETTINGS_SCHEMA}


def configure(values):
    SETTINGS.update(values or {})


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _day_of(path):
    try:
        return datetime.strptime(path.stem.split("_", 1)[1], "%Y%m%d").date()
    except (IndexError, ValueError):
        return None


def stream_stats(directory):
    """Số ngày, dung lượng, số dòng — cho trang Trợ lý tự động."""
    days = []
    for path in sorted(Path(directory).glob("stream_*.jsonl")):
        day = _day_of(path)
        if not day:
            continue
        try:
            with path.open("r", encoding="utf-8") as handle:
                lines = sum(1 for line in handle if line.strip())
            size = path.stat().st_size
        except OSError:
            continue
        days.append({"day": day.isoformat(), "rows": lines, "bytes": size})
    return {"days": days, "rows": sum(d["rows"] for d in days),
            "bytes": sum(d["bytes"] for d in days)}


def prune_old_streams(directory, keep_days, *, today=None):
    """Xoá file luồng cũ hơn ``keep_days`` ngày (0 = không xoá). Trả tên file đã xoá."""
    keep_days = int(keep_days or 0)
    if keep_days <= 0:
        return []
    today = today or datetime.now().date()
    removed = []
    for path in sorted(Path(directory).glob("stream_*.jsonl")):
        day = _day_of(path)
        if day and (today - day).days >= keep_days:
            try:
                path.unlink()
                removed.append(path.name)
            except OSError:
                continue
    return removed


class RawStreamRecorder:
    """Ghi TOÀN BỘ luồng bình luận, kể cả khi AI đang tắt.

    Đây là kho dữ liệu để sau này lọc ra lớp phản xạ local và hồ sơ giọng. Nó
    cố tình tách khỏi bộ nhớ khách: chỉ ghi, không suy luận, không chặn gì.
    """

    _AT_PREFIX = re.compile(r"^@([A-Za-z0-9._-]{2,})")

    def __init__(self, directory=None, enabled=None):
        if directory is None or enabled is None:
            from config import RAW_STREAM_DIR, RAW_STREAM_ENABLED
            directory = RAW_STREAM_DIR if directory is None else directory
            enabled = RAW_STREAM_ENABLED if enabled is None else enabled
        self.enabled = bool(enabled)
        self.dir = Path(directory)
        self._lock = threading.Lock()
        self.count = 0
        self.duplicates = 0
        self.source_counts = {}
        # Chốt chặn cuối cho chất lượng dữ liệu: hai tab cùng ghi, hoặc một lô
        # bị gửi lại sau lỗi mạng, đều không được nhân đôi bình luận.
        self._seen = OrderedDict()
        self._active_path = None
        if self.enabled:
            self.dir.mkdir(parents=True, exist_ok=True)
            self._load_existing(self._path())

    def _load_existing(self, path):
        """Khôi phục bộ đếm/chống trùng sau khi backend khởi động lại."""
        self._active_path = Path(path)
        self.count = 0
        self.source_counts = {}
        self._seen.clear()
        try:
            with self._active_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except (TypeError, json.JSONDecodeError):
                        continue
                    self.count += 1
                    source = clean(row.get("data_source"), 40) or "unlabeled"
                    self.source_counts[source] = self.source_counts.get(source, 0) + 1
                    msg_id = clean(row.get("msg_id"), 160)
                    if msg_id:
                        self._remember(msg_id)
        except FileNotFoundError:
            pass

    def _path(self):
        return self.dir / f"stream_{datetime.now():%Y%m%d}.jsonl"

    def _remember(self, msg_id):
        if not msg_id:
            return
        self._seen[msg_id] = True
        while len(self._seen) > 20000:
            self._seen.popitem(last=False)

    def _build_rows(self, events):
        rows, batch_ids = [], set()
        for event in events[:200]:
            if not isinstance(event, dict):
                continue
            text = clean(event.get("text") or event.get("comment"), 600)
            if not text:
                continue
            is_host = bool(event.get("is_host"))
            reply_to = clean(event.get("reply_to_username"), 160)
            if is_host and not reply_to:
                found = self._AT_PREFIX.match(text)
                reply_to = found.group(1) if found else ""
            msg_id = clean(event.get("msg_id") or event.get("comment_id"), 160)
            if (msg_id and msg_id in self._seen) or (msg_id and msg_id in batch_ids):
                self.duplicates += 1
                continue
            if msg_id:
                batch_ids.add(msg_id)
            rows.append({
                "ts": clean(event.get("ts"), 40) or now_iso(),
                "room_id": clean(event.get("room_id"), 160),
                "session_id": clean(event.get("session_id"), 80),
                "msg_id": msg_id, "user_id": clean(event.get("user_id"), 160),
                "unique_id": clean(event.get("unique_id"), 160),
                "nickname": clean(event.get("nickname"), 160), "text": text,
                "is_host": is_host, "reply_to_username": reply_to,
                "pin_id": clean(event.get("pin_id"), 160),
                "video_position_s": event.get("video_position_s") or 0,
                "ai_enabled": bool(event.get("ai_enabled")),
                "ai_action": clean(event.get("ai_action"), 40) or "observed",
                "data_source": clean(event.get("data_source"), 40) or "unlabeled",
                "platform": "shopee" if event.get("platform") == "shopee" else "tiktok",
            })
        return rows

    def record(self, events):
        if not self.enabled or not SETTINGS["ghi_luong"] or not isinstance(events, list):
            return 0
        with self._lock:
            path = self._path()
            if path != self._active_path:
                self._load_existing(path)
            rows = self._build_rows(events)
            if not rows:
                return 0
            # Chỉ nhớ msg_id SAU KHI dữ liệu đã flush xuống đĩa. Nếu append lỗi,
            # extension có thể gửi lại mà không bị chốt chống trùng nuốt mất.
            with path.open("a", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            for row in rows:
                self._remember(row.get("msg_id"))
                source = clean(row.get("data_source"), 40) or "unlabeled"
                self.source_counts[source] = self.source_counts.get(source, 0) + 1
            self.count += len(rows)
        return len(rows)


def confirm_delivery(memory, interaction_id, reply_id, receipt):
    return memory.confirm_reply_sent(interaction_id, reply_id, receipt)


def mark_delivery(memory, interaction_id, reply_id, status, detail):
    return memory.mark_reply_delivery(interaction_id, reply_id, status, detail)
