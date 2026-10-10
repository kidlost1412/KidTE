import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from metrics_store import DEEPSEEK_PRICING_USD_PER_MILLION, MetricsStore


def test_session_metrics_distinguish_zero_from_unmeasured(tmp_path):
    store = MetricsStore(tmp_path)
    try:
        empty = store.snapshot()
        assert empty["comments_per_minute"] == 0
        assert empty["last_comment_at"] is None
        assert empty["model"]["p50_ms"] is None
        assert empty["model"]["prompt_tokens"] is None
        store.record("comment")
        store.record("reply", outcome="replied", model_ms=100,
                     prompt_tokens=20, completion_tokens=5, reasoning_tokens=2)
        store.record("reply", outcome="policy_blocked",
                     missing_fields=["price", "price"])
        view = store.snapshot()
        assert view["comments_per_minute"] == 1
        assert view["outcomes"] == {"replied": 1, "skipped": 0, "policy_blocked": 1}
        assert view["model"]["p50_ms"] == 100
        assert view["model"]["prompt_tokens"] == 20
        assert view["model"]["cost"] is None
        assert view["top_missing_fields"][0] == ("price", 2)
    finally:
        store.close()


def test_cost_off_peak_deepseek_v4_pro_matches_official_pricing(tmp_path):
    """`[T12]` Giá đọc từ api-docs.deepseek.com/quick_start/pricing 13/09/2026:
    v4-pro off-peak = 0.022 (hit) / 0.66 (miss) / 1.98 (output) USD/1M token.
    Thứ Hai 12:00 UTC không rơi vào khung cao điểm (01-04h, 06-10h)."""
    store = MetricsStore(tmp_path)
    try:
        store.record(
            "reply", outcome="replied", timestamp="2026-09-14T12:00:00+00:00",
            model="deepseek-v4-pro", prompt_tokens=2_000_000,
            prompt_cache_hit_tokens=1_000_000, prompt_cache_miss_tokens=1_000_000,
            completion_tokens=1_000_000,
        )
        cost = store.snapshot()["model"]["cost"]
        assert cost == round(0.022 + 0.66 + 1.98, 6)
    finally:
        store.close()


def test_cost_peak_hour_doubles_the_rate(tmp_path):
    """Thứ Hai 07:00 UTC rơi đúng khung cao điểm 06-10h -> giá gấp đôi."""
    store = MetricsStore(tmp_path)
    try:
        store.record(
            "reply", outcome="replied", timestamp="2026-09-14T07:00:00+00:00",
            model="deepseek-v4-pro", prompt_tokens=2_000_000,
            prompt_cache_hit_tokens=1_000_000, prompt_cache_miss_tokens=1_000_000,
            completion_tokens=1_000_000,
        )
        cost = store.snapshot()["model"]["cost"]
        assert cost == round(0.044 + 1.32 + 3.96, 6)
    finally:
        store.close()


def test_cost_weekend_is_never_peak_even_during_peak_hour_window(tmp_path):
    """Chủ nhật 07:00 UTC nằm trong KHUNG GIỜ cao điểm nhưng DeepSeek chỉ
    tính cao điểm thứ Hai-thứ Sáu — cuối tuần luôn off-peak."""
    store = MetricsStore(tmp_path)
    try:
        store.record(
            "reply", outcome="replied", timestamp="2026-09-13T07:00:00+00:00",
            model="deepseek-v4-pro", prompt_tokens=1_000_000,
            prompt_cache_hit_tokens=0, prompt_cache_miss_tokens=1_000_000,
            completion_tokens=0,
        )
        cost = store.snapshot()["model"]["cost"]
        assert cost == DEEPSEEK_PRICING_USD_PER_MILLION["deepseek-v4-pro"]["input_cache_miss"]["off_peak"]
    finally:
        store.close()


def test_cost_missing_cache_split_is_treated_as_full_cache_miss(tmp_path):
    """Không có cache hit/miss (provider cũ, hoặc field thiếu) -> coi TOÀN
    BỘ prompt_tokens là cache miss, đắt hơn thật còn hơn báo rẻ hơn thật."""
    store = MetricsStore(tmp_path)
    try:
        store.record(
            "reply", outcome="replied", timestamp="2026-09-14T12:00:00+00:00",
            model="deepseek-v4-pro", prompt_tokens=1_000_000, completion_tokens=0,
        )
        cost = store.snapshot()["model"]["cost"]
        assert cost == DEEPSEEK_PRICING_USD_PER_MILLION["deepseek-v4-pro"]["input_cache_miss"]["off_peak"]
    finally:
        store.close()


def test_cost_sums_only_priced_replies_and_flags_the_unpriced_ones(tmp_path):
    """Model không có trong bảng giá (vd tương lai đổi tên) thì lượt đó
    KHÔNG được tính bằng 0 — phải cộng dồn riêng lượt đã tính được và báo rõ
    còn bao nhiêu lượt chưa tính được."""
    store = MetricsStore(tmp_path)
    try:
        store.record(
            "reply", outcome="replied", timestamp="2026-09-14T12:00:00+00:00",
            model="deepseek-v4-pro", prompt_tokens=1_000_000,
            prompt_cache_hit_tokens=0, prompt_cache_miss_tokens=1_000_000,
            completion_tokens=0,
        )
        store.record(
            "reply", outcome="replied", timestamp="2026-09-14T12:00:00+00:00",
            model="mot-model-tuong-lai-chua-co-trong-bang-gia",
            prompt_tokens=1_000_000, completion_tokens=1_000_000,
        )
        view = store.snapshot()["model"]
        assert view["cost"] == DEEPSEEK_PRICING_USD_PER_MILLION["deepseek-v4-pro"]["input_cache_miss"]["off_peak"]
        assert view["cost_priced_replies"] == 1
        assert view["cost_unpriced_replies"] == 1
        assert view["cost_reason"] == "some_replies_missing_model"
    finally:
        store.close()


def test_cost_deepseek_flash_pricing_spot_check(tmp_path):
    store = MetricsStore(tmp_path)
    try:
        store.record(
            "reply", outcome="replied", timestamp="2026-09-14T12:00:00+00:00",
            model="deepseek-flash", prompt_tokens=1_000_000,
            prompt_cache_hit_tokens=1_000_000, prompt_cache_miss_tokens=0,
            completion_tokens=1_000_000,
        )
        cost = store.snapshot()["model"]["cost"]
        assert cost == round(0.003 + 0.60, 6)
    finally:
        store.close()


def test_old_comments_are_not_counted_per_minute(tmp_path):
    old = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    store = MetricsStore(tmp_path, clock=lambda: old)
    try:
        store.record("comment")
        assert store.snapshot()["comments_per_minute"] == 0
    finally:
        store.close()


def test_metrics_write_without_sensitive_payload_and_rotate(tmp_path):
    store = MetricsStore(tmp_path, max_bytes=120, backup_count=2)
    try:
        for _ in range(12):
            store.record("reply", outcome="skipped", missing_fields=["unknown"])
        store.close()
        rows = []
        for path in tmp_path.glob("session-metrics.jsonl*"):
            rows.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
        assert rows
        assert all("comment" not in row and "user_id" not in row for row in rows)
        assert len(list(tmp_path.glob("session-metrics.jsonl*"))) <= 3
    finally:
        store.close()
