"""Metrics vận hành theo phiên, không chứa nội dung hay danh tính khách."""
from __future__ import annotations

import json
import queue
import threading
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path


def _iso_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _percentile(values, percentile):
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * percentile)))
    return ordered[index]


# `[T12]` Giá CHÍNH THỨC đọc trực tiếp từ https://api-docs.deepseek.com/quick_start/pricing
# ngày 13/09/2026 (đối chiếu thêm với thông báo chính thức xác nhận
# deepseek-v4-pro tiếp tục phục vụ sau 14/09/2026, giá không đổi — không lấy
# từ blog/trang tổng hợp giá bên thứ ba, các trang đó có thông tin sai lệch
# về mốc 14/09 khi kiểm chéo). Đơn vị: USD / 1 triệu token.
# `[!]` GIÁ CÓ THỂ ĐỔI: kiểm lại trang chính thức trước khi tin số này nếu
# đã lâu không cập nhật — đừng chỉ tin comment này mãi mãi.
DEEPSEEK_PRICING_USD_PER_MILLION = {
    "deepseek-flash": {
        "input_cache_hit": {"off_peak": 0.003, "peak": 0.006},
        "input_cache_miss": {"off_peak": 0.15, "peak": 0.30},
        "output": {"off_peak": 0.60, "peak": 1.20},
    },
    "deepseek-v4-pro": {
        "input_cache_hit": {"off_peak": 0.022, "peak": 0.044},
        "input_cache_miss": {"off_peak": 0.66, "peak": 1.32},
        "output": {"off_peak": 1.98, "peak": 3.96},
    },
}
DEEPSEEK_PRICING_SOURCE = "api-docs.deepseek.com/quick_start/pricing (đọc 13/09/2026)"


def _is_deepseek_peak_hour(happened_utc):
    """Giờ cao điểm DeepSeek: 01:00–04:00 và 06:00–10:00 UTC, thứ Hai–thứ Sáu.

    Nguồn: trang giá chính thức, cùng ngày đọc ở trên.
    """
    if happened_utc.weekday() >= 5:  # 5=thứ Bảy, 6=Chủ nhật
        return False
    hour = happened_utc.hour
    return (1 <= hour < 4) or (6 <= hour < 10)


def _reply_cost_usd(row):
    """USD của một lượt trả lời, hoặc `None` nếu không đủ dữ liệu để tính.

    Không đoán giá cho model lạ — thiếu bảng giá thì trả None, để snapshot()
    báo rõ "chưa tính được" thay vì âm thầm hiện số 0 gây hiểu lầm là miễn phí.
    """
    pricing = DEEPSEEK_PRICING_USD_PER_MILLION.get(str(row.get("model") or "").strip())
    if not pricing:
        return None
    try:
        happened = datetime.fromisoformat(row["timestamp"])
    except (KeyError, ValueError, TypeError):
        return None
    peak = "peak" if _is_deepseek_peak_hour(happened.astimezone(timezone.utc)) else "off_peak"
    hit = int(row.get("prompt_cache_hit_tokens") or 0)
    miss = int(row.get("prompt_cache_miss_tokens") or 0)
    if hit == 0 and miss == 0:
        # Provider không trả cache hit/miss riêng (thiếu field) — coi TOÀN
        # BỘ prompt là cache miss: định giá cao hơn thật còn hơn báo rẻ hơn
        # thật, đúng tinh thần "im lặng an toàn hơn trả lời sai".
        miss = int(row.get("prompt_tokens") or 0)
    completion = int(row.get("completion_tokens") or 0)
    return (
        hit / 1_000_000 * pricing["input_cache_hit"][peak]
        + miss / 1_000_000 * pricing["input_cache_miss"][peak]
        + completion / 1_000_000 * pricing["output"][peak]
    )


class MetricsStore:
    """Giữ cửa sổ RAM hữu hạn và ghi JSONL bằng thread riêng."""

    def __init__(self, directory: Path, *, max_events=10_000, max_bytes=5_000_000,
                 backup_count=3, clock=None):
        self.directory = Path(directory)
        self.path = self.directory / "session-metrics.jsonl"
        self.max_bytes = int(max_bytes)
        self.backup_count = int(backup_count)
        self.clock = clock or _iso_now
        self.events = deque(maxlen=max_events)
        self._lock = threading.Lock()
        self._queue = queue.Queue(maxsize=5000)
        self._dropped = 0
        self._closed = threading.Event()
        self._writer = threading.Thread(target=self._write_loop, daemon=True,
                                        name="kid-te-metrics")
        self._writer.start()

    def record(self, kind, **values):
        row = {"timestamp": self.clock(), "kind": str(kind), **values}
        with self._lock:
            self.events.append(row)
        try:
            self._queue.put_nowait(row)
        except queue.Full:
            with self._lock:
                self._dropped += 1

    def _rotate(self):
        if not self.path.exists() or self.path.stat().st_size < self.max_bytes:
            return
        oldest = self.path.with_suffix(self.path.suffix + f".{self.backup_count}")
        if oldest.exists():
            oldest.unlink()
        for number in range(self.backup_count - 1, 0, -1):
            source = self.path.with_suffix(self.path.suffix + f".{number}")
            if source.exists():
                source.replace(self.path.with_suffix(self.path.suffix + f".{number + 1}"))
        self.path.replace(self.path.with_suffix(self.path.suffix + ".1"))

    def _write_loop(self):
        while not self._closed.is_set() or not self._queue.empty():
            try:
                row = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                self._rotate()
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            except OSError:
                with self._lock:
                    self._dropped += 1
            finally:
                self._queue.task_done()

    def snapshot(self):
        with self._lock:
            rows = list(self.events)
            dropped = self._dropped
        comments = [row for row in rows if row["kind"] == "comment"]
        replies = [row for row in rows if row["kind"] == "reply"]
        now = datetime.now(timezone.utc)
        recent = []
        for row in comments:
            try:
                happened = datetime.fromisoformat(row["timestamp"])
            except (ValueError, TypeError):
                continue
            if (now - happened.astimezone(timezone.utc)).total_seconds() <= 60:
                recent.append(row)
        outcomes = Counter(row.get("outcome", "skipped") for row in replies)
        missing = Counter(field for row in replies for field in row.get("missing_fields", []))
        model_ms = [int(row["model_ms"]) for row in replies
                    if isinstance(row.get("model_ms"), (int, float))]
        tokens = {}
        for key in ("prompt_tokens", "completion_tokens", "reasoning_tokens"):
            measured = [int(row[key]) for row in replies
                        if isinstance(row.get(key), (int, float))]
            tokens[key] = sum(measured) if measured else None
        # `[T12]` Chi phí phiên: chỉ tính được cho lượt có `model` khớp bảng
        # giá đã xác minh (DEEPSEEK_PRICING_USD_PER_MILLION). Model lạ/thiếu
        # thì lượt đó không tính, KHÔNG coi bằng 0 — báo rõ còn bao nhiêu
        # lượt chưa tính được thay vì im lặng làm tổng bị thiếu.
        priced = [_reply_cost_usd(row) for row in replies]
        counted = [value for value in priced if value is not None]
        uncounted = len(priced) - len(counted)
        if not replies:
            cost, cost_reason = None, "no_samples"
        elif not counted:
            cost, cost_reason = None, "model_not_in_pricing_table"
        else:
            cost = round(sum(counted), 6)
            cost_reason = "some_replies_missing_model" if uncounted else None
        return {
            "comments_per_minute": len(recent),
            "last_comment_at": comments[-1]["timestamp"] if comments else None,
            "outcomes": {
                "replied": outcomes.get("replied", 0),
                "skipped": outcomes.get("skipped", 0),
                "policy_blocked": outcomes.get("policy_blocked", 0),
            },
            "top_missing_fields": missing.most_common(10),
            "model": {
                "samples": len(model_ms),
                "p50_ms": _percentile(model_ms, .50),
                "p95_ms": _percentile(model_ms, .95),
                **tokens,
                "cost": cost,
                "cost_reason": cost_reason,
                "cost_priced_replies": len(counted),
                "cost_unpriced_replies": uncounted,
                "cost_pricing_source": DEEPSEEK_PRICING_SOURCE,
            },
            "events_kept": len(rows), "events_dropped": dropped,
        }

    def close(self):
        self._closed.set()
        self._writer.join(timeout=2)
