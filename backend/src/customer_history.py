"""Lịch sử từng khách THEO PHIÊN LIVE, để AI hiểu "khách này từng hỏi gì, lúc nào".

Nguồn: luồng bình luận thô (``logs/raw_stream``, có room_id = một phiên LIVE của
Shopee hoặc TikTok) + câu AI đã gửi (``customer_memory.interactions``) + câu chủ shop trả lời
(dòng của host có ``reply_to_username``).

Mỗi khách một định danh bền: ``<platform>:id:<user_id>`` (hoặc sec_uid / @unique_id) —
cùng công thức với ``CustomerMemory.customer_key``. Khách chỉ có tên hiển thị thì
KHÔNG dựng lịch sử: hai người trùng tên là chuyện thường, trộn nhầm còn tệ hơn không có.
"""
from __future__ import annotations

import json
import threading
from collections import OrderedDict, deque
from datetime import datetime, timedelta
from pathlib import Path

from customer_memory import CustomerMemory
from knowledge import clean

MAX_PAST_SESSIONS = 4
MAX_COMMENTS_PER_SESSION = 4
MAX_THIS_SESSION = 6
MAX_ROWS_PER_CUSTOMER = 200   # giữ 200 bình luận MỚI NHẤT mỗi khách (đủ cho MAX_PAST_SESSIONS*4 + phiên này)
MAX_CUSTOMERS = 5000          # LRU: bỏ khách lâu không có bình luận mới
MAX_HOST_REPLIES_PER_USER = 200
HOST_REPLY_WINDOW = timedelta(minutes=10)
_BOUGHT = ("đã mua", "da mua", "mua rồi", "mua roi", "nhận hàng", "nhan hang", "đã nhận", "mới nhận", "vừa nhận",
           "đặt rồi", "dat roi", "đã đặt", "dùng rồi", "xài rồi")


def _parse_ts(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone()
    except (TypeError, ValueError):
        return None


class CustomerHistory:
    def __init__(self, stream_dir, memory=None):
        self.stream_dir = Path(stream_dir)
        self.memory = memory
        self._lock = threading.Lock()
        self._offsets = {}          # file -> số byte đã đọc (đọc tiếp, không đọc lại)
        self._by_customer = OrderedDict()   # customer_key -> deque[row] (LRU, có trần)
        self._host_replies = OrderedDict()  # unique_id (thường) -> deque[(ts, room_id, text)] (LRU, có trần)

    # ------------------------------------------------------------ chỉ mục
    def _refresh(self):
        for path in sorted(self.stream_dir.glob("stream_*.jsonl")):
            try:
                size = path.stat().st_size
            except OSError:
                continue
            start = self._offsets.get(path.name, 0)
            if size < start:            # file bị thay (khôi phục sao lưu) → đọc lại từ đầu
                self._by_customer.clear()
                self._host_replies.clear()
                self._offsets.clear()
                return self._refresh()
            if size == start:
                continue
            with path.open("rb") as handle:
                handle.seek(start)
                chunk = handle.read()
            # Chỉ nhận tới dòng hoàn chỉnh cuối cùng; phần dở dang đọc ở lần sau.
            end = chunk.rfind(b"\n") + 1
            self._offsets[path.name] = start + end
            for line in chunk[:end].decode("utf-8", errors="replace").splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._add(row)

    def _add(self, row):
        text = clean(row.get("text"), 300)
        ts = _parse_ts(row.get("ts"))
        if not text or ts is None:
            return
        room = clean(row.get("room_id"), 160)
        if row.get("is_host"):
            target = clean(row.get("reply_to_username"), 160).casefold()
            if target:
                self._touch(self._host_replies, target, MAX_HOST_REPLIES_PER_USER).append((ts, room, text))
            return
        key, strength = CustomerMemory.customer_key(
            platform_user_id=row.get("user_id") or "", unique_id=row.get("unique_id") or "",
            display_name=row.get("nickname") or "",
            platform="shopee" if row.get("platform") == "shopee" or row.get("data_source") == "shopee-live" else "tiktok")
        if strength < 2 or self._is_forgotten(key):
            return
        self._touch(self._by_customer, key, MAX_ROWS_PER_CUSTOMER).append({
            "ts": ts, "room": room, "text": text, "msg_id": clean(row.get("msg_id"), 160),
            "unique_id": clean(row.get("unique_id"), 160), "nickname": clean(row.get("nickname"), 160),
            "pin_id": clean(row.get("pin_id"), 160),
        })

    @staticmethod
    def _touch(store, key, per_key):
        """Lấy deque của key (đưa lên cuối LRU); vượt MAX_CUSTOMERS thì bỏ key cũ nhất."""
        bucket = store.get(key)
        if bucket is None:
            bucket = store[key] = deque(maxlen=per_key)
            while len(store) > MAX_CUSTOMERS:
                store.popitem(last=False)
        else:
            store.move_to_end(key)
        return bucket

    def _is_forgotten(self, customer_key):
        check = getattr(self.memory, "is_forgotten", None)
        available = getattr(self.memory, "memory_available", None)
        try:
            if callable(available) and not available():
                return True  # bộ nhớ khách tắt: không lập chỉ mục, không trả lịch sử
            return bool(check and check(customer_key))
        except Exception:
            return False

    def forget_customer(self, customer_key):
        """Drop this customer's derived in-memory index.

        The raw stream logs are append-only files owned by another writer; they are
        not rewritten, so the history layer EXCLUDES the customer instead (tombstone
        check in ``_add``/``for_customer``). Returns counts for the privacy report.
        """
        with self._lock:
            rows = self._by_customer.pop(customer_key, [])
            for uid in {r["unique_id"].casefold() for r in rows if r.get("unique_id")}:
                self._host_replies.pop(uid, None)
        return {"deleted": len(rows)}

    def _ai_replies(self, customer_key):
        """runtime_comment_id -> (câu AI đã gửi, trạng thái gửi)."""
        memory = self.memory
        if memory is None or not hasattr(memory, "_db"):
            return {}
        try:
            with memory._lock:
                rows = memory._db.execute(
                    "SELECT runtime_comment_id, response, delivery_status, interaction_id FROM interactions "
                    "WHERE customer_key = ? AND response != ''", (customer_key,)).fetchall()
        except Exception:
            return {}
        result = {}
        for row in rows:
            if row[0]:
                confirmed = memory.confirmed_response(row[3])
                result[row[0]] = (confirmed, 'sent') if confirmed else (row[1], row[2])
        return result

    # ------------------------------------------------------------ cho AI
    def for_customer(self, customer_key, *, current_room="", current_msg_id="", current_text="",
                     product_titles=None):
        if not customer_key or ":name:" in customer_key or self._is_forgotten(customer_key):
            return {}
        with self._lock:
            self._refresh()
            rows = list(self._by_customer.get(customer_key) or [])
        current_text = clean(current_text, 300)
        all_rooms = {r["room"] or r["ts"].strftime("%Y%m%d") for r in rows} | ({current_room} if current_room else set())
        rows = [r for r in rows if not (current_msg_id and r["msg_id"] == current_msg_id)
                and not (not current_msg_id and r["room"] == current_room and r["text"] == current_text)]
        if not rows:
            return {}
        rows.sort(key=lambda r: r["ts"])
        titles = product_titles or {}
        ai = self._ai_replies(customer_key)
        host = self._host_replies.get(rows[-1]["unique_id"].casefold(), []) if rows[-1]["unique_id"] else []

        def reply_of(row):
            answer = ai.get(row["msg_id"])
            if answer and answer[1] == "sent":
                return "AI: " + clean(answer[0], 160)
            for ts, room, text in host:
                if room == row["room"] and row["ts"] <= ts <= row["ts"] + HOST_REPLY_WINDOW:
                    return "Chủ shop: " + clean(text, 160)
            return "chưa ai trả lời"

        def entry(row):
            item = {"luc": row["ts"].strftime("%H:%M"), "khach": row["text"][:160], "tra_loi": reply_of(row)}
            product = titles.get(row["pin_id"])
            if product:
                item["dang_ghim"] = clean(product, 60)
            return item

        sessions = {}
        for row in rows:
            sessions.setdefault(row["room"] or row["ts"].strftime("%Y%m%d"), []).append(row)
        past, now, notes = [], [], []
        for room, items in sorted(sessions.items(), key=lambda kv: kv[1][0]["ts"], reverse=True):
            if room == current_room:
                now = [entry(r) for r in items[-MAX_THIS_SESSION:]]
                continue
            if len(past) >= MAX_PAST_SESSIONS:
                continue
            entries = [entry(r) for r in items[-MAX_COMMENTS_PER_SESSION:]]
            day = items[0]["ts"].strftime("%d/%m/%Y")
            past.append({"ngay": day, "gio": f"{items[0]['ts']:%H:%M}–{items[-1]['ts']:%H:%M}",
                         "so_binh_luan": len(items), "binh_luan": entries})
            missed = [e for e in entries if e["tra_loi"] == "chưa ai trả lời" and "?" in e["khach"] or
                      e["tra_loi"] == "chưa ai trả lời" and len(e["khach"]) > 12]
            if missed and len(notes) < 2:
                notes.append(f"Phiên {day} khách hỏi \"{missed[-1]['khach'][:80]}\" mà chưa ai trả lời.")
        if any(word in r["text"].casefold() for r in rows for word in _BOUGHT):
            notes.append("Khách từng nói đã mua / đã nhận hàng — là khách cũ.")
        last = rows[-1]
        return {
            "dinh_danh": customer_key,
            "ten_hien_thi": last["nickname"], "tai_khoan": ("@" + last["unique_id"]) if last["unique_id"] else "",
            "so_phien_da_binh_luan": len(all_rooms),
            "lan_dau": rows[0]["ts"].strftime("%d/%m/%Y"),
            "phien_truoc": past, "phien_nay": now, "luu_y": notes,
            "cach_dung": ("Đây là lịch sử của ĐÚNG khách này (khớp theo ID tài khoản và nguồn sàn). "
                          "Dùng để hiểu câu nói tắt và tình trạng của khách (khách quen, đã mua, lần trước hỏi "
                          "gì chưa được trả lời). Có thể nhắc nhẹ một ý nếu giúp ích; không đọc lại lịch sử, "
                          "không nhắc ngày giờ cụ thể trừ khi khách hỏi."),
        }
