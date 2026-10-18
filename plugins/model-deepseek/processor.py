"""Adapter model DeepSeek; lõi chỉ phụ thuộc giao diện reply/check_balance."""
from __future__ import annotations

import contextlib
import json
import re
import threading
import time
import urllib.error
import urllib.request

import prompt_view
from ai_context import APP_BRIEF, system_with_brief, task_block
from config import (
    AI_NOTES_PATH, CHAT_MAX_CHARS, CHAT_MAX_TOKENS, CHAT_TEMPERATURE, DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL,
    DEEPSEEK_DISABLE_THINKING, DEEPSEEK_MODEL, DEEPSEEK_REASONING_EFFORT,
    SYSTEM_PROMPT_OVERRIDE_PATH, SYSTEM_PROMPT_PATH,
)
from knowledge import clean
from delivery_store import utf16_length

# Tran token toi da cho lan thu lai khi thinking da tat.
MAX_RETRY_TOKENS = 8000

# Rà soát 17/10 C8: hạn TỔNG cho một lượt trả lời (mọi lần gọi + thử lại). Đặt bằng turn_budget() ở lõi;
# ngoài khối đó (Thử AI, lời gọi phụ) giữ hành vi cũ: 20 giây mỗi lần gọi.
TURN_BUDGET_S = 25.0
_turn = threading.local()


class TurnDeadline(Exception):
    """Lượt trả lời đã quá hạn tổng: không gọi model thêm."""


@contextlib.contextmanager
def turn_budget(seconds=TURN_BUDGET_S):
    """Đặt hạn tổng cho các lần gọi model trong luồng hiện tại (lồng nhau thì giữ hạn sớm hơn)."""
    previous = getattr(_turn, "deadline", None)
    deadline = time.monotonic() + float(seconds)
    _turn.deadline = deadline if previous is None else min(previous, deadline)
    try:
        yield
    finally:
        _turn.deadline = previous


def turn_remaining():
    """Số giây còn lại của lượt; None khi không đặt hạn."""
    deadline = getattr(_turn, "deadline", None)
    return None if deadline is None else deadline - time.monotonic()


# Rà soát 17/10 P9: lượt cần CHÍNH XÁC (giá, liều, cách ly, pha trộn) dùng nhiệt độ thấp và bỏ phạt lặp
# (frequency_penalty phạt cả số, đơn vị, tên sản phẩm lặp lại). Lượt xã giao giữ cài đặt của chủ shop.
PRECISE_TEMPERATURE = 0.35

# Mặc định lấy từ .env (nếu có) để máy cũ giữ nguyên hành vi; chỉnh tiếp ở
# Trợ lý tự động → Nhà cung cấp DeepSeek.
SETTINGS_SCHEMA = [
    {"key": "so_ky_tu_toi_da", "type": "number", "integer": True, "default": CHAT_MAX_CHARS,
     "min": 60, "max": 150, "step": 5, "group": "Câu trả lời",
     "label": "Độ dài tối đa một câu gửi lên TikTok (tính cả @tên khách)",
     "help": "TikTok cho tối đa 150 ký tự. Ngắn hơn thì dễ đọc trên LIVE nhưng dễ thiếu ý."},
    {"key": "viet_gon_khi_dai", "type": "bool", "default": True, "group": "Câu trả lời",
     "label": "Câu dài quá thì nhờ AI tự viết gọn lại (thay vì cắt cụt)",
     "help": "Thêm khoảng 1 giây ở những câu dài; gần như không tốn thêm tiền nhờ bộ nhớ đệm."},
    {"key": "nhiet_do", "type": "number", "default": CHAT_TEMPERATURE, "min": 0, "max": 1.5, "step": 0.05,
     "group": "Câu trả lời", "label": "Độ sáng tạo (temperature)",
     "help": "Thấp (0,2–0,4): câu an toàn nhưng lặp khuôn. 0,8: tự nhiên mà vẫn bám dữ kiện. "
             "Trên 1,1: đa dạng hơn, dễ nói lan man. Chỉ áp dụng khi tắt chế độ suy nghĩ."},
    {"key": "phat_lap_y", "type": "number", "default": 0.35, "min": 0, "max": 1, "step": 0.05,
     "group": "Câu trả lời", "label": "Tránh lặp ý/chữ đã dùng (presence_penalty)",
     "help": "0,3–0,4: câu đa dạng hơn mà vẫn bám dữ kiện. 0 để tắt. Chỉ áp dụng khi tắt chế độ suy nghĩ."},
    {"key": "phat_lap_tu", "type": "number", "default": 0.3, "min": 0, "max": 1, "step": 0.05,
     "group": "Câu trả lời", "label": "Tránh lặp cùng một từ (frequency_penalty)",
     "help": "0,3–0,4 là vừa. 0 để tắt. Chỉ áp dụng khi tắt chế độ suy nghĩ."},
    {"key": "nguong_sap_het", "type": "number", "integer": True, "default": 10, "min": 0, "max": 1000, "step": 1,
     "group": "Câu trả lời", "label": "Tồn kho từ mức này trở xuống thì AI được nói \"sắp hết\"",
     "help": "Chỉ dùng tồn thật TikTok gửi về cho sản phẩm đang bàn. 0 = không bao giờ nói sắp hết."},
    {"key": "suy_nghi", "type": "bool", "default": not DEEPSEEK_DISABLE_THINKING, "group": "Suy nghĩ",
     "label": "Bật chế độ suy nghĩ khi trả lời khách",
     "help": "Bật thì câu khó (liều, so sánh) chắc hơn nhưng chậm 3–10 giây và tốn hơn. "
             "Khi LIVE nên tắt để trả lời kịp."},
    {"key": "muc_suy_nghi", "type": "select", "default": DEEPSEEK_REASONING_EFFORT
     if DEEPSEEK_REASONING_EFFORT in {"low", "high", "max"} else "high",
     "options": [["low", "Nhẹ — nhanh"], ["high", "Kỹ"], ["max", "Rất kỹ — chậm nhất"]],
     "group": "Suy nghĩ", "label": "Mức suy nghĩ", "depends": "suy_nghi"},
]
SETTINGS = {field["key"]: field["default"] for field in SETTINGS_SCHEMA}


def configure(values):
    SETTINGS.update(values or {})


def max_chars():
    return int(SETTINGS["so_ky_tu_toi_da"])


def default_system_prompt():
    return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip()


MAX_NOTES_CHARS = 1500
NOTES_HEADER = ("GHI CHÚ THÊM CỦA CHỦ SHOP (bổ sung cho luật ở trên; không được trái luật GIÁ và dữ kiện cứng):")

# Nhiệm vụ mặc định của processor.reply: trả lời một bình luận LIVE.
REPLY_TASK = task_block(
    "Trả lời bình luận LIVE",
    "khách đang xem LIVE (TikTok/Shopee) vừa bình luận; viết MỘT câu trả lời ngắn đúng giọng người đang trực.",
    inputs="NỀN CỐ ĐỊNH của phiên (danh mục, chính sách, phong cách, quy tắc đã học) + dữ liệu riêng của lượt + câu khách.",
    output="một JSON object {action: reply|skip, reply|messages|missing_fields} theo mục ĐỘ DÀI VÀ ĐỊNH DẠNG.")


def ai_notes():
    """Ghi chú ngắn chủ shop dặn thêm cho AI ('' nếu chưa có)."""
    try:
        return AI_NOTES_PATH.read_text(encoding="utf-8").strip()[:MAX_NOTES_CHARS]
    except OSError:
        return ""


def legacy_prompt_override():
    """Lời nhắc ghi đè TOÀN BỘ kiểu cũ (nếu còn file). Không còn được dùng làm lời nhắc."""
    try:
        return SYSTEM_PROMPT_OVERRIDE_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def system_prompt():
    """Luật bán hàng gốc (trung tính; bản đi kèm app hoặc bản chủ shop đã sửa) + ghi chú thêm của chủ shop.

    Giọng/tính cách nằm ở phong cách người trực, KHÔNG ở đây. Không còn bản ghi đè toàn bộ.
    """
    notes = ai_notes()
    base = prompt_view.base_rules_text(default_system_prompt())
    return base + (f"\n\n{NOTES_HEADER}\n{notes}" if notes else "")


def _non_thinking_sampling(inference, kind=""):
    """Tham số lấy mẫu cho chế độ KHÔNG suy nghĩ. DeepSeek (OpenAI-compatible) nhận
    presence_penalty/frequency_penalty ở non-thinking; chế độ thinking bỏ qua nên không gửi.
    kind="precise": lượt giá/liều/cách ly/pha trộn → nhiệt độ thấp, không phạt lặp."""
    precise = kind == "precise"
    try:
        temperature = float(SETTINGS["nhiet_do"])
    except (TypeError, ValueError):
        temperature = 0.8
    inference["temperature"] = min(temperature, PRECISE_TEMPERATURE) if precise else temperature
    for key, name in (("phat_lap_y", "presence_penalty"), ("phat_lap_tu", "frequency_penalty")):
        try:
            value = max(0.0, min(2.0, float(SETTINGS.get(key) or 0)))
        except (TypeError, ValueError):
            value = 0.0
        if value and not precise:
            inference[name] = value
        else:
            inference.pop(name, None)
    return inference


def compose_system(task=REPLY_TASK, *extra):
    """APP_BRIEF → luật gốc (+ghi chú) → phần riêng chỗ gọi → khối nhiệm vụ. Luật gốc đứng trước nhiệm vụ để
    các đường gọi khác nhau (trả lời, hỏi lại SKU, chọn quy tắc liều) dùng chung tiền tố cache."""
    return system_with_brief(system_prompt(), *extra, task)


def has_prompt_override():
    """Luôn False: lời nhắc ghi đè toàn bộ đã bỏ (chuỗi chính sách luôn được xét như bản gốc)."""
    return False


_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029\u200b-\u200f\u202a-\u202e\u2066-\u2069]+")


def _one_line(value, limit):
    """Nickname/bình luận là dữ liệu của khách: bỏ xuống dòng/ký tự điều khiển để không giả được dòng chỉ thị (C3)."""
    return re.sub(r"\s{2,}", " ", _CONTROL.sub(" ", str(value or ""))).strip()[:limit]


def _trim_reply(value, limit):
    text = clean(value, limit * 3)
    text = re.sub(r"^\s*@[^\s]+\s+", "", text)
    if len(text) <= limit:
        return text
    cut = text[:max(1, limit - 1)].rstrip()
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:-") + "…"


def _fit_reply(value, limit):
    """Vừa khung mà KHÔNG cụt nghĩa: dừng ở hết câu gần nhất nếu được.

    Chỉ khi không có dấu hết câu nào đủ xa mới cắt theo từ và thêm "…".
    """
    text = clean(value, limit * 3)
    text = re.sub(r"^\s*@[^\s]+\s+", "", text)
    if len(text) <= limit:
        return text
    head = text[:limit]
    ends = [m.end() for m in re.finditer(r"[.!?…]+(?=\s|$)", head)]
    if ends and ends[-1] >= limit * .45:
        return head[:ends[-1]].strip()
    return _trim_reply(text, limit)


_UNIT_HEAD = re.compile(r"^(?:ml|l|lít|lit|g|gr|gam|gram|kg|cc|%|k|đ|₫|ngày|tuần|giờ|lần|bình|chai|gói|can|phuy|m2|ha|công|sào)(?![^\W\d_])",
                        re.I)


def _fit_utf16(value, limit):
    """Rút gọn câu model vượt khung (rà soát 17/10 P10) mà vẫn giữ ý: ưu tiên dừng ở hết câu, rồi ở hết
    mệnh đề (dấu phẩy/chấm phẩy/gạch), cuối cùng ở ranh giới từ; không bao giờ cắt giữa số và đơn vị."""
    text = " ".join(str(value or "").split())
    if utf16_length(text) <= limit:
        return text
    head = text
    while head and utf16_length(head) > limit:
        head = head[:-1]
    for pattern, floor in ((r"[.!?…]+(?=\s|$)", .45), (r"[,;:–-](?=\s)", .55)):
        ends = [m.end() for m in re.finditer(pattern, head)]
        while ends:
            cut = head[:ends[-1]].rstrip(" ,;:–-")
            if len(cut) < len(head) * floor:
                break
            if not _splits_number(cut, text[len(cut):]):
                return cut if pattern.startswith("[.") else cut + "."
            ends.pop()
    words = head.split(" ")
    if len(text) > len(head) and not text[len(head)].isspace():
        words = words[:-1]                       # từ cuối bị cắt dở
    while words:
        joined = " ".join(words)
        cut = joined.rstrip(" ,;:–-")
        if cut and utf16_length(cut + "…") <= limit and not _splits_number(joined, text[len(joined):]):
            return cut + "…"
        words = words[:-1]
    return ""


def _splits_number(kept, rest):
    """True nếu điểm cắt rơi giữa một con số và đơn vị/khoảng của nó ("pha 20 | ml", "20 | – 30ml")."""
    kept, rest = kept.rstrip(), rest.lstrip(" ,")
    if not kept or not rest:
        return False
    if re.search(r"\d$", kept) and (_UNIT_HEAD.match(rest) or re.match(r"^[–\-/x×]\s*\d", rest)):
        return True
    return bool(re.search(r"\d\s*[–\-/x×]$", kept))


def _strip_nickname(value, nickname, max_chars=100):
    text, name = re.sub(r'\s+', ' ', str(value or '')).strip(), clean(nickname, 160)
    if not name:
        return text
    # Chỉ xoá khi nickname là LỜI GỌI TÊN: "@nick" bất kỳ đâu, hoặc ở đầu câu
    # (sau "Dạ"/"Chào" nếu có). Nickname là từ thường/số ("5", "Anh", "Phân")
    # mà xoá giữa câu sẽ làm mất liều/số lượng: "pha 5 ml" -> "pha ml".
    escaped = re.escape(name)
    tagged_start = bool(re.match(rf"@{escaped}(?!\w)", text, flags=re.I))
    text = re.sub(rf"@{escaped}(?!\w)[,:]?", "", text, flags=re.I)
    if tagged_start and not re.fullmatch(r"[\d\s.,]+", name.strip()):
        # "@Hoa Dạ anh Hoa ơi, ..." — tên gọi lặp ngay sau @tag đầu câu (R12).
        text = re.sub(rf"^(\s*(?:dạ[\s,]*)?(?:(?:chào|hi|hello)[\s,!]+)?(?:(?:anh|chị|bạn|cô|chú|em)\s+)?)"
                      rf"{escaped}(?:\s+ơi[\s,!]*|\s*[,:!]\s*)", lambda m: m.group(1), text, flags=re.I)
    # Đầu câu: phải có dấu gọi ("Nam ơi", "Nam,") mới chắc là gọi tên, tránh
    # "Phân này bón gốc" hay "Dạ 5 ml/bình" bị cắt mất chữ.
    text = re.sub(rf"^((?:dạ\s*)?(?:chào|hi|hello)[\s,!]+){escaped}(?!\w)(?:\s+ơi)?[\s,:!]*",
                  lambda m: m.group(1), text, flags=re.I)
    text = re.sub(rf"^((?:dạ)?[\s,!]*){escaped}(?:\s+ơi[\s,!]*|\s*[,:!]\s*)",
                  lambda m: m.group(1), text, flags=re.I)
    plain = name.strip()
    if len(plain) > 2 and not re.fullmatch(r"[\d\s.,]+", plain) and " " in plain:
        # Nickname nhiều từ (khó trùng từ thường) -> xoá cả giữa câu như cũ.
        text = re.sub(rf"(?<!\w){escaped}(?!\w)", "", text, flags=re.I)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([,.!?])", r"\1", text)
    return text.strip(" ,;:-")


# Khoá ngữ cảnh gần như không đổi trong một phiên LIVE → phần prompt cố định.
STABLE_CONTEXT_KEYS = (
    "price_policy", "approved_example_policy", "catalog_scope_warning",
    "thong_tin_shop", "all_live_products", "kien_thuc_policy",
    "phong_cach_nguoi_truc", "quy_tac_da_hoc", "tai_lieu_policy",
    "sales_playbook", "the_khach_so_tay_policy",   # gói C: sổ tay tình huống (cố định, sắp theo id)
)


# Thứ tự phần theo lượt: cái gì hay lặp lại giữa các câu liên tiếp đứng trước.
TURN_ORDER = (
    "current_product", "approved_product_knowledge", "price", "pinned_product_id",
    "relevant_approved_products", "verified_dose_calculations", "kien_thuc_da_hoc",
    "tai_lieu_lien_quan", "operator_approved_reply_examples", "customer_price_signal",
    "customer_memory", "the_khach", "tinh_huong_phu_hop", "luot_gan_day", "lich_su_khach",
    "cau_da_gui_gan_day", "video_context",
)


# Bản gọn (cờ compact_catalog): tai_lieu_policy chỉ xuất hiện khi có tài liệu →
# để ở phần cố định sẽ làm nền cố định đổi theo lượt, vỡ cache tiền tố. Phần theo
# lượt xếp theo độ ổn định: cái lặp lại giữa các câu liên tiếp đứng trước.
STABLE_CONTEXT_KEYS_COMPACT = tuple(key for key in STABLE_CONTEXT_KEYS if key != "tai_lieu_policy")
TURN_ORDER_COMPACT = (
    "pinned_product_id", "current_product", "price", "approved_product_knowledge",
    "relevant_approved_products", "kien_thuc_da_hoc", "operator_approved_reply_examples",
    "tai_lieu_policy", "tai_lieu_lien_quan", "verified_dose_calculations",
    "customer_price_signal", "customer_memory", "the_khach", "tinh_huong_phu_hop", "luot_gan_day",
    "lich_su_khach", "cau_da_gui_gan_day", "video_context",
)


def split_context(context):
    context = dict(context or {})
    try:
        import context_budget
        compact = context_budget.enabled("compact_catalog")
    except Exception:  # noqa: BLE001 - cờ hỏng không được chặn câu trả lời
        compact = False
    stable_keys, turn_order = ((STABLE_CONTEXT_KEYS_COMPACT, TURN_ORDER_COMPACT) if compact
                               else (STABLE_CONTEXT_KEYS, TURN_ORDER))
    stable = {key: context.pop(key) for key in stable_keys if key in context}
    turn = {key: context.pop(key) for key in turn_order if key in context}
    turn.update(context)
    return stable, turn


def part_chars(system, stable, turn):
    """Số ký tự từng phần prompt (chỉ số, không nội dung) cho số liệu trực tiếp."""
    def size(value):
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    parts = {"system_prompt": len(system)}
    for key, value in stable.items():
        parts["stable." + key] = size(value)
    for key, value in turn.items():
        parts["turn." + key] = size(value)
    return parts


# Tên hiển thị cho model DeepSeek. Từ 10/09/2026 "deepseek-flash" CHÍNH LÀ
# V4.1 Flash; tên cũ "deepseek-v4-flash" được chuyển tạm sang V4.1.
MODEL_LABELS = {
    "deepseek-flash": "DeepSeek V4.1 Flash — mới nhất, nhanh, rẻ (khuyên dùng)",
    "deepseek-v4-flash": "DeepSeek V4 Flash — tên cũ, đang tự chuyển sang V4.1 Flash",
    "deepseek-v4-pro": "DeepSeek V4 Pro — thế hệ trước, chậm và đắt hơn V4.1 Flash",
    "deepseek-chat": "deepseek-chat — tên cũ, đã ngừng",
    "deepseek-reasoner": "deepseek-reasoner — tên cũ, đã ngừng",
}



def _is_transient_error(exc):
    name = type(exc).__name__
    if name in {"AuthenticationError", "PermissionDeniedError", "BadRequestError", "NotFoundError",
                "UnprocessableEntityError"}:
        return False
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status in {408, 409, 429} or status >= 500
    return name in {"APITimeoutError", "APIConnectionError", "InternalServerError", "RateLimitError",
                    "TimeoutError", "ReadTimeout", "ConnectTimeout"}


USAGE_KEYS = ("prompt_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "completion_tokens")


def _add_usage(metrics, usage):
    """Cộng dồn (không ghi đè) số token của MỘT lần gọi vào metrics của lượt (rà soát 17/10 C4)."""
    for key in USAGE_KEYS:
        metrics[key] = metrics.get(key, 0) + (getattr(usage, key, 0) or 0)
    details = getattr(usage, "completion_tokens_details", None)
    metrics["reasoning_tokens"] = metrics.get("reasoning_tokens", 0) + (getattr(details, "reasoning_tokens", 0) or 0)
    metrics["model_calls"] = metrics.get("model_calls", 0) + 1
    return metrics


def _flatten_reply(value):
    if not isinstance(value, dict):
        return ""
    rows = value.get("messages")
    if not isinstance(rows, list):
        rows = [value.get("reply")]
    return " ".join(r.strip() for r in rows if isinstance(r, str) and r.strip())


def _trim_utf16(text, limit):
    text = " ".join(str(text or "").split())
    if utf16_length(text) <= limit:
        return text
    cut = text
    while cut and utf16_length(cut) > limit:
        cut = cut[:-1]
    # Ưu tiên cắt ở cuối câu/từ gần nhất thay vì giữa chữ.
    for mark in (". ", "! ", "? ", ", ", " "):
        pos = cut.rfind(mark)
        if pos >= len(cut) * 0.5:
            return cut[:pos + (0 if mark == " " else 1)].rstrip(" ,;")
    return cut.rstrip(" ,;")

class DeepSeekResponder:
    model_labels = MODEL_LABELS

    def __init__(self):
        self.client = None
        # Khoa DANG hoat dong, giu tren instance. Truoc day check_balance()
        # doc thang hang so DEEPSEEK_API_KEY (nap tu .env luc import) trong
        # khi configure_api_key() chi thay self.client, nen sau khi doi khoa
        # thi tra loi dung khoa moi con kiem so du van dung khoa cu (F08).
        self.api_key = DEEPSEEK_API_KEY
        if DEEPSEEK_API_KEY:
            self.client = self._new_client(DEEPSEEK_API_KEY)
        self._balance_lock = threading.Lock()
        self._balance_checked_at = 0.0
        self._balance_status = None
        self.model = DEEPSEEK_MODEL
        self._models_checked_at = 0.0
        self._models = None

    @staticmethod
    def _new_client(api_key):
        from openai import OpenAI
        # 20 giây: bình thường ~1 giây; quá 20 giây thì khách đã trôi khỏi khung chat.
        import httpx
        from net import ssl_context
        return OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL,
                      http_client=httpx.Client(verify=ssl_context(), timeout=20),
                      timeout=20, max_retries=0)

    def configure_api_key(self, api_key):
        candidate = self._new_client(api_key)
        response = candidate.models.list()
        models = sorted({str(getattr(item, "id", "")).strip()
                         for item in getattr(response, "data", [])
                         if str(getattr(item, "id", "")).strip()})
        if not models:
            raise ValueError("Khóa không trả danh sách model")
        self.client, self._models = candidate, models
        self.api_key = api_key
        self._models_checked_at = time.monotonic()
        # Trạng thái số dư của khóa cũ không nói gì về khóa mới.
        with self._balance_lock:
            self._balance_status, self._balance_checked_at = None, 0.0
            self._balance_generation = getattr(self, "_balance_generation", 0) + 1
        return models

    def list_models(self, max_age=300):
        if self.client is None:
            raise RuntimeError("Chưa cấu hình khóa DeepSeek")
        now = time.monotonic()
        if self._models is not None and now - self._models_checked_at < max_age:
            return list(self._models)
        response = self.client.models.list()
        models = sorted({str(getattr(item, "id", "")).strip()
                         for item in getattr(response, "data", [])
                         if str(getattr(item, "id", "")).strip()})
        if not models:
            raise RuntimeError("Tài khoản không trả danh sách model")
        self._models, self._models_checked_at = models, now
        return list(models)

    def set_model(self, model):
        model = str(model or "").strip()
        if model not in self.list_models():
            raise ValueError("Model không nằm trong danh sách của tài khoản")
        self.model = model
        return model

    # Dashboard (wait=False) chưa có kết quả nào: chờ tối đa chừng này rồi trả
    # trạng thái "đang kiểm tra" thay vì treo tới 10 giây timeout mạng.
    BALANCE_NOWAIT_S = 1.5

    def check_balance(self, max_age=30, wait=True):
        """Fail closed when DeepSeek cannot safely accept a completion request.

        Khoá chỉ giữ để đọc/ghi trạng thái, KHÔNG giữ trong lúc gọi mạng. Một
        luồng gọi mạng (single-flight); luồng khác dùng giá trị cũ nếu có, hoặc
        chờ kết quả (wait=True). wait=False: làm mới ở nền, không bao giờ chờ mạng.
        """
        if self.client is None:
            return {"ok": False, "deepseek_available": False,
                    "reason": "deepseek_key_missing",
                    "error": "Chưa cấu hình khóa DeepSeek; AI trả lời đang tắt"}
        with self._balance_lock:
            cond = self.__dict__.get("_balance_cond")
            if cond is None:
                cond = self._balance_cond = threading.Condition(self._balance_lock)
            now = time.monotonic()
            stale = self._balance_status
            if stale is not None and now - self._balance_checked_at < max_age:
                return dict(stale)
            inflight = getattr(self, "_balance_inflight", False)
            if not inflight:
                self._balance_inflight = True
                generation = getattr(self, "_balance_generation", 0)
                api_key = self.api_key
        if not inflight:
            if wait:
                return self._balance_refresh(api_key, generation)
            threading.Thread(target=self._balance_refresh, args=(api_key, generation),
                             name="deepseek-balance", daemon=True).start()
        with self._balance_lock:
            if self._balance_status is not None and (not wait or stale is not None):
                return dict(self._balance_status)
            deadline = time.monotonic() + (12 if wait else self.BALANCE_NOWAIT_S)
            while getattr(self, "_balance_inflight", False) and time.monotonic() < deadline:
                cond.wait(max(0.01, deadline - time.monotonic()))
            if self._balance_status is not None:
                return dict(self._balance_status)
        return {"ok": False, "service": "kid-te-ai-standalone",
                "deepseek_available": False, "reason": "deepseek_balance_checking",
                "error": "Đang kiểm tra số dư DeepSeek; AI trả lời tạm tắt"}

    def _balance_refresh(self, api_key, generation):
        try:
            status = self._balance_fetch(api_key)
        except Exception as exc:   # khong de co in-flight ket mai
            status = {"ok": False, "service": "kid-te-ai-standalone",
                      "deepseek_available": False,
                      "reason": "deepseek_balance_check_failed",
                      "error": "Không kiểm tra được số dư DeepSeek; AI trả lời vẫn tắt",
                      "detail": type(exc).__name__}
        with self._balance_lock:
            self._balance_inflight = False
            if generation == getattr(self, "_balance_generation", 0):
                self._balance_status, self._balance_checked_at = status, time.monotonic()
            cond = self.__dict__.get("_balance_cond")
            if cond is not None:
                cond.notify_all()
        return dict(status)

    def _balance_fetch(self, api_key):
        if True:
            # Dùng đúng khóa đang hoạt động, không dùng hằng số lúc import.
            request = urllib.request.Request(
                DEEPSEEK_BASE_URL.rstrip("/") + "/user/balance",
                headers={"Authorization": f"Bearer {api_key}",
                         "Accept": "application/json"},
            )
            try:
                from net import ssl_context
                with urllib.request.urlopen(request, timeout=10, context=ssl_context()) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if payload.get("is_available") is True:
                    status = {"ok": True, "service": "kid-te-ai-standalone",
                              "deepseek_available": True}
                else:
                    status = {"ok": False, "service": "kid-te-ai-standalone",
                              "deepseek_available": False,
                              "reason": "deepseek_insufficient_balance",
                              "error": "DeepSeek đã hết số dư; AI trả lời vẫn tắt"}
            except json.JSONDecodeError as exc:
                status = {"ok": False, "service": "kid-te-ai-standalone",
                          "deepseek_available": False,
                          "reason": "deepseek_balance_check_failed",
                          "error": "Không kiểm tra được số dư DeepSeek; AI trả lời vẫn tắt",
                          "detail": type(exc).__name__}
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                from net import classify
                info = classify(exc, "api.deepseek.com")
                reasons = {"http_401": "deepseek_key_rejected", "http_403": "deepseek_key_rejected",
                           "http_402": "deepseek_insufficient_balance",
                           "tls_untrusted": "deepseek_tls_untrusted", "tls_error": "deepseek_tls_untrusted",
                           "dns": "deepseek_dns", "proxy": "deepseek_proxy",
                           "timeout": "deepseek_timeout", "offline": "deepseek_offline",
                           "http_5xx": "deepseek_server_error"}
                status = {"ok": False, "service": "kid-te-ai-standalone",
                          "deepseek_available": False,
                          "reason": reasons.get(info["code"], "deepseek_balance_check_failed"),
                          "error": info["message"] + "; AI trả lời vẫn tắt",
                          "detail": info["detail"]}
            return status

    def _create(self, kwargs):
        """Một lần gọi model; trong turn_budget thì timeout không vượt phần hạn tổng còn lại."""
        remaining = turn_remaining()
        if remaining is not None:
            if remaining < 1.0:
                raise TurnDeadline(f"turn budget exhausted ({remaining:.1f}s left)")
            kwargs = {**kwargs, "timeout": min(20.0, remaining)}
        return self.client.chat.completions.create(**kwargs)

    def _shorten(self, inference, previous, length, max_chars, metrics, kind=""):
        retry = dict(inference)
        retry["messages"] = inference["messages"] + [
            {"role": "assistant", "content": previous},
            {"role": "user", "content": (
                f"Câu vừa viết dài {length} ký tự, vượt khung {max_chars}. Viết lại cùng ý, tự nhiên, "
                f"trọn câu, TỐI ĐA {max(30, max_chars - 15)} ký tự. Trả đúng một JSON object.")},
        ]
        retry.pop("reasoning_effort", None)
        retry["extra_body"] = {"thinking": {"type": "disabled"}}
        _non_thinking_sampling(retry, kind)
        started = time.monotonic()
        try:
            response = self._create(retry)
            _add_usage(metrics, getattr(response, "usage", None))
            result = json.loads(response.choices[0].message.content or "{}")
        except Exception as exc:  # noqa: BLE001 - viết gọn hỏng thì rút gọn bằng mã, không chặn lượt
            metrics["shorten_error"] = type(exc).__name__
            return ""
        metrics["shortened_from"] = length
        metrics["model_ms"] = metrics.get("model_ms", 0) + int((time.monotonic() - started) * 1000)
        return result['reply'] if isinstance(result, dict) and result.get('action') == 'reply' and isinstance(result.get('reply'), str) else ''

    def reply(self, nickname, comment, context, max_chars, correction="", system_override=""):
        if self.client is None:
            return {"action": "skip", "missing_fields": ["deepseek_key_missing"],
                    "metrics": {}}
        started = time.monotonic()
        context = dict(context or {})
        kind = str(context.pop("_sampling", "") or "")     # khoá nội bộ, không gửi model
        system = system_override or compose_system()
        if not system.startswith(APP_BRIEF.rstrip()):
            system = system_with_brief(system)        # mọi lời gọi đều bắt đầu bằng APP_BRIEF
        multi = context.get('delivery_protocol_version') == 2
        system += ('\nJSON reply thêm awaiting_fields: danh sách thông tin thực sự vừa hỏi khách để giải quyết câu đang tư vấn; '
            'chỉ dùng product, variant, quantity, crop, stage, method, tank_volume; không thiếu thì []. '
            'Một câu có dấu hỏi không có nghĩa đang làm rõ. Không hỏi lại thông tin khách đã cung cấp.'
            '\nJSON reply đặt y_khach ĐẦU TIÊN, trước reply/messages: ≤15 từ ghi điều khách thật sự muốn '
            '(chỉ để bạn nghĩ, không gửi khách); rồi reply giải quyết đúng điều đó.')
        if multi:
            system += ('\nGiao thức lượt này: trả JSON {"action":"reply","messages":["tin 1","tin 2"]}. '
                       'Dùng 1–3 tin trọn ý, thường 1–2; mỗi tin không nhắc nickname và không vượt giới hạn UTF-16 đã cấp. '
                       'Không chia số khỏi đơn vị hoặc điều kiện của cùng hướng dẫn; không cần viết reply riêng.')
        stable, turn = split_context(context)
        # DeepSeek tính tiền & tốc độ theo CACHE TIỀN TỐ: phần giống hệt lượt
        # trước (tính từ ký tự đầu) được đọc từ cache — rẻ hơn nhiều và nhanh
        # hơn. Vì vậy: lời nhắc hệ thống → nền cố định của phiên (danh mục, chính
        # sách, phong cách, quy tắc đã học; JSON sắp khoá cố định) → cuối cùng
        # mới là dữ liệu riêng của lượt này. Bản cũ trộn tất cả vào một JSON mà
        # giá/sản phẩm đang hỏi đứng TRƯỚC danh mục nên cache chỉ trúng ~35%.
        messages = [{"role": "system", "content": system}]
        if stable:
            messages.append({"role": "system", "content": (
                "NỀN CỐ ĐỊNH CỦA PHIÊN (danh mục, chính sách, phong cách, quy tắc đã học):\n"
                + json.dumps(stable, ensure_ascii=False, separators=(",", ":"), sort_keys=True))})
        messages.append({"role": "user", "content": (
            "DỮ LIỆU VÀ NGỮ CẢNH CHO LƯỢT NÀY (đọc đúng mức chắc chắn ghi trong từng trường):\n"
            + json.dumps(turn, ensure_ascii=False, separators=(",", ":"))
            + f"\nKHÁCH {_one_line(nickname, 160)}: {_one_line(comment, 2000)}\n"
            + (f"Viết đủ ý trong 1–3 messages, thường 1–2 tin; mỗi tin tối đa {max_chars} đơn vị UTF-16. "
               "Giới hạn áp dụng từng tin, không áp dụng tổng lượt. Không tách điều kiện khỏi hướng dẫn. " if multi else
               f"Độ dài reply: viết TRỌN Ý trong khoảng {max(1, int(max_chars * .75))} đơn vị UTF-16, "
               f"tuyệt đối không quá {max_chars} đơn vị UTF-16 — câu bị cắt giữa chừng là lỗi nặng. ")
            + "Trả đúng một JSON object."
            + (f"\nBẮT BUỘC SỬA: {correction}" if correction else ""))})
        part_sizes = None
        try:
            part_sizes = part_chars(system, stable, turn)
        except Exception:  # noqa: BLE001
            part_sizes = None
        tat_suy_nghi = bool(getattr(self, "disable_thinking", not SETTINGS["suy_nghi"]))
        muc_suy_nghi = str(getattr(self, "reasoning_effort", SETTINGS["muc_suy_nghi"]))
        tran_token = int(getattr(self, "max_tokens", CHAT_MAX_TOKENS))
        inference = {"model": getattr(self, "model", DEEPSEEK_MODEL), "messages": messages,
                     "max_tokens": tran_token,
                     "response_format": {"type": "json_object"},
                     "extra_body": {"thinking": {"type": "disabled" if tat_suy_nghi else "enabled"}}}
        if tat_suy_nghi:
            _non_thinking_sampling(inference, kind)
        else:
            configured = muc_suy_nghi if muc_suy_nghi in {"low", "high", "max"} else "high"
            # An approved arithmetic conversion still uses thinking, but low
            # effort prevents a one-line dose answer consuming the whole budget.
            inference["reasoning_effort"] = "low" if context.get("verified_dose_calculations") else configured

        def elapsed_ms(): return int((time.monotonic() - started) * 1000)
        response = None
        for attempt in range(3):
            try:
                response = self._create(inference)
                break
            except Exception as exc:
                # Tối đa 2 lần thử lại, chờ ngắn, chỉ với lỗi thoáng qua (timeout/mạng/429/5xx).
                # 401/402 (khoá/số dư) và các lỗi 4xx khác không bao giờ thử lại. Vẫn fail
                # closed — không bao giờ gửi mù. Trong hạn tổng của lượt (C8): còn dưới 2 giây,
                # hoặc lần gọi đầu đã chậm quá 12 giây, thì thôi thử lại.
                remaining = turn_remaining()
                if (attempt == 2 or not _is_transient_error(exc) or elapsed_ms() > 20000
                        or (remaining is not None and (remaining < 2.0 or elapsed_ms() > 12000))):
                    return {"action": "skip", "missing_fields": [f"deepseek_{type(exc).__name__}"],
                            "metrics": {"model_ms": elapsed_ms(), **({"retried_transient": True} if attempt else {})}}
                time.sleep(0.6 * (attempt + 1))
        # DeepSeek tinh gia input theo cache hit/miss (hit re hon nhieu lan). Thieu hai
        # truong nay thi metrics_store phai coi CA prompt la cache miss — dat gia cao hon
        # that, an toan hon la bao re hon thuc te. Moi lan goi trong luot deu CONG DON.
        metrics = _add_usage({"model_ms": elapsed_ms(), "model": inference.get("model", "")},
                             getattr(response, "usage", None))
        metrics.update({
                   "finish_reason": getattr(response.choices[0], "finish_reason", "") or "",
                   "thinking": not tat_suy_nghi,
                   "reasoning_effort": inference.get("reasoning_effort", "")})
        if kind:
            metrics["sampling"] = kind
        if part_sizes:
            metrics["context_part_chars"] = part_sizes
        choice, content = response.choices[0], response.choices[0].message.content or ""
        # THANG THU LAI. Model dot het han muc suy nghi roi khong con cho de
        # viet cau tra loi. Ban cu chi ha tu high xuong low — nhung cau hoi
        # LIEU von da chay o muc low san (vi co verified_dose_calculations),
        # nen dieu kien "khong phai low" khien chung KHONG duoc thu lai lan
        # nao. Ngay 12/09 mat 3 cau vi dung loi nay, trong do co mot khach
        # hoi hai lan lien tiep ve lieu cho phuy 200 lit.
        # Nac cuoi TAT HAN suy nghi: cham nhat cung ra duoc mot cau tra loi.
        # Khi thinking da TAT san thi khong con nac nao de ha, nhung nguyen
        # nhan mat cau tra loi van con: het max_tokens truoc khi viet xong
        # JSON. Ban cu dat ladder = [] trong truong hop nay, ma mac dinh cua
        # config lai la TAT — nen tren may that thang thu lai chua bao gio
        # chay. Nay nac "rong" noi tran token roi goi lai.
        if tat_suy_nghi:
            ladder = ["roi-rong"]
        else:
            ladder = [step for step in ("low", "off")
                      if step == "off" or step != inference.get("reasoning_effort")]
        for step in ladder:
            if content.strip() or choice.finish_reason != "length": break
            if step == "roi-rong":
                inference["max_tokens"] = min(tran_token * 2, MAX_RETRY_TOKENS)
            elif step == "off":
                inference.pop("reasoning_effort", None)
                inference["extra_body"] = {"thinking": {"type": "disabled"}}
                _non_thinking_sampling(inference, kind)
            else:
                inference["reasoning_effort"] = step
            try:
                response = self._create(inference)
            except Exception as exc:
                return {"action": "skip", "missing_fields": [f"deepseek_{type(exc).__name__}"], "metrics": metrics}
            choice, content = response.choices[0], response.choices[0].message.content or ""
            _add_usage(metrics, getattr(response, "usage", None))
            metrics.update({"model_ms": elapsed_ms(),
                            "model": inference.get("model", ""),
                            "finish_reason": getattr(choice, "finish_reason", "") or "",
                            "reasoning_effort": "" if step in {"off", "roi-rong"}
                                                else step,
                            "thinking": not tat_suy_nghi and step != "off",
                            "retried_at_low_effort": True,
                            "nac_thu_lai": step})
        if not content.strip():
            return {"action": "skip", "missing_fields": [f"model_no_final_answer_{choice.finish_reason or 'unknown'}"], "metrics": metrics}
        try:
            result = json.loads(content)
        except json.JSONDecodeError:
            return {"action": "skip", "missing_fields": ["model_json_invalid"], "metrics": metrics}
        if not isinstance(result, dict):
            return {"action": "skip", "missing_fields": ["model_json_invalid"], "metrics": metrics}
        if result.get("action") != "reply":
            return {"action": "skip", "missing_fields": result.get("missing_fields") or [f"model_no_action_{choice.finish_reason or 'unknown'}"], "metrics": metrics}
        if multi:
            def parts(value):
                if not isinstance(value, dict) or value.get('action') != 'reply':
                    return None
                rows = value.get('messages', [value.get('reply')]) if isinstance(value, dict) else None
                if not isinstance(rows, list) or not 1 <= len(rows) <= 3 or any(not isinstance(row, str) for row in rows):
                    return None
                rows = [_strip_nickname(row, nickname, max_chars) for row in rows]
                return rows if all(row and utf16_length(row) <= max_chars for row in rows) else None
            rows = parts(result)
            first_result = result
            if rows is None:
                retry = dict(inference)
                retry['messages'] = messages + [{'role': 'assistant', 'content': content}, {'role': 'user', 'content':
                    f'Viết lại đủ ý trong 1–3 messages, mỗi tin tối đa {max_chars} đơn vị UTF-16. Không cắt chuỗi. Trả JSON action/messages.'}]
                retry.pop('reasoning_effort', None)
                retry['extra_body'] = {'thinking': {'type': 'disabled'}}
                _non_thinking_sampling(retry, kind)
                try:
                    rewritten = self._create(retry)
                    _add_usage(metrics, getattr(rewritten, 'usage', None))
                    result = json.loads(rewritten.choices[0].message.content or '{}')
                    rows = parts(result)
                    metrics['multi_message_rewritten'] = True
                except Exception as exc:  # noqa: BLE001 - hỏng thì gộp một tin bên dưới
                    metrics['multi_message_rewrite_error'] = type(exc).__name__
                    rows = None
            if rows is None:
                # Viết lại vẫn hỏng: gửi MỘT tin gộp, cắt gọn theo giới hạn thay vì im lặng.
                flat = _flatten_reply(result) or _flatten_reply(first_result)
                flat = _strip_nickname(flat, nickname, max_chars)
                flat = _fit_utf16(flat, max_chars) or _trim_utf16(flat, max_chars)   # không có ranh giới từ: cắt ký tự như cũ
                if not flat:
                    return {'action': 'skip', 'missing_fields': ['model_messages_invalid'], 'metrics': metrics}
                metrics['multi_message_trimmed'] = True
                metrics['model_ms'] = elapsed_ms()
                return {'action': 'reply', 'reply': flat, 'messages': [flat],
                        'awaiting_fields': first_result.get('awaiting_fields', []), 'metrics': metrics}
            metrics['model_ms'] = elapsed_ms()
            return {'action': 'reply', 'reply': ' '.join(rows), 'messages': rows,
                    'awaiting_fields': result.get('awaiting_fields', []), 'metrics': metrics}
        if not isinstance(result.get('reply'), str):
            return {'action': 'skip', 'missing_fields': ['model_reply_invalid'], 'metrics': metrics}
        body = _strip_nickname(result.get("reply"), nickname, max_chars)
        if utf16_length(body) > max_chars and SETTINGS["viet_gon_khi_dai"]:
            # Dài quá thì nhờ model tự viết gọn (cùng tiền tố → gần như toàn bộ
            # là cache hit, ~1 giây) thay vì chặt cụt câu trước mặt khách.
            for attempt in range(2):            # lần 2 viết gọn từ bản vừa gọn (giọng dài như miền Tây hay vượt nhẹ)
                remaining = turn_remaining()
                if remaining is not None and remaining < 3.0:
                    break                       # sắp hết hạn lượt: rút gọn bằng mã bên dưới
                shorter = self._shorten(inference, content, len(body), max_chars, metrics, kind)
                if shorter:
                    body = _strip_nickname(shorter, nickname, max_chars)
                if utf16_length(body) <= max_chars or not shorter:
                    break
                content = json.dumps({"action": "reply", "reply": body}, ensure_ascii=False)
        if utf16_length(body) > max_chars:
            # P10: rút gọn bằng mã tới hết câu/mệnh đề (không tách số khỏi đơn vị) thay vì bỏ cả câu.
            # Chỉ cắt được giữa câu ("…") hoặc còn quá ngắn = vô nghĩa -> vẫn bỏ, không gửi câu cụt.
            fitted = _fit_utf16(body, max_chars)
            if fitted.endswith("…") or len(fitted.strip()) < max(15, int(max_chars * .4)):
                return {"action": "skip", "missing_fields": ["model_reply_too_long"], "metrics": metrics}
            metrics["fitted_from"] = utf16_length(body)
            body = fitted
        if not body:
            return {"action": "skip", "missing_fields": ["empty_model_reply"], "metrics": metrics}
        return {"action": "reply", "reply": body, 'awaiting_fields': result.get('awaiting_fields', []), "metrics": metrics}
