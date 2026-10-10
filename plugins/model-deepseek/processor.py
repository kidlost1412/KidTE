"""Adapter model DeepSeek; lõi chỉ phụ thuộc giao diện reply/check_balance."""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request

from config import (
    CHAT_MAX_CHARS, CHAT_MAX_TOKENS, CHAT_TEMPERATURE, DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL,
    DEEPSEEK_DISABLE_THINKING, DEEPSEEK_MODEL, DEEPSEEK_REASONING_EFFORT,
    SYSTEM_PROMPT_OVERRIDE_PATH, SYSTEM_PROMPT_PATH,
)
from knowledge import clean
from delivery_store import utf16_length

# Tran token toi da cho lan thu lai khi thinking da tat.
MAX_RETRY_TOKENS = 8000

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


def system_prompt():
    """Lời nhắc hệ thống: bản chủ shop sửa (nếu có) thay cho bản gốc đi kèm app."""
    try:
        custom = SYSTEM_PROMPT_OVERRIDE_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        custom = ""
    return custom or default_system_prompt()


def has_prompt_override():
    """Có lời nhắc chủ shop tự sửa không (khi đó giữ nguyên các chuỗi chính sách)."""
    try:
        return bool(SYSTEM_PROMPT_OVERRIDE_PATH.read_text(encoding="utf-8").strip())
    except OSError:
        return False


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


def _strip_nickname(value, nickname, max_chars=100):
    text, name = re.sub(r'\s+', ' ', str(value or '')).strip(), clean(nickname, 160)
    if not name:
        return text
    text = re.sub(rf"(?<!\w){re.escape(name)}(?!\w)", "", text, flags=re.I)
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

    def check_balance(self, max_age=30):
        """Fail closed when DeepSeek cannot safely accept a completion request."""
        if self.client is None:
            return {"ok": False, "deepseek_available": False,
                    "reason": "deepseek_key_missing",
                    "error": "Chưa cấu hình khóa DeepSeek; AI trả lời đang tắt"}
        now = time.monotonic()
        with self._balance_lock:
            if self._balance_status is not None and now - self._balance_checked_at < max_age:
                return dict(self._balance_status)
            # Dùng đúng khóa đang hoạt động, không dùng hằng số lúc import.
            request = urllib.request.Request(
                DEEPSEEK_BASE_URL.rstrip("/") + "/user/balance",
                headers={"Authorization": f"Bearer {self.api_key}",
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
            self._balance_status, self._balance_checked_at = status, now
            return dict(status)

    def _shorten(self, inference, previous, length, max_chars, metrics):
        retry = dict(inference)
        retry["messages"] = inference["messages"] + [
            {"role": "assistant", "content": previous},
            {"role": "user", "content": (
                f"Câu vừa viết dài {length} ký tự, vượt khung {max_chars}. Viết lại cùng ý, tự nhiên, "
                f"trọn câu, TỐI ĐA {max(30, max_chars - 15)} ký tự. Trả đúng một JSON object.")},
        ]
        retry.pop("reasoning_effort", None)
        retry["extra_body"] = {"thinking": {"type": "disabled"}}
        retry["temperature"] = SETTINGS["nhiet_do"]
        started = time.monotonic()
        try:
            response = self.client.chat.completions.create(**retry)
            result = json.loads(response.choices[0].message.content or "{}")
        except Exception:
            return ""
        usage = getattr(response, "usage", None)
        metrics["shortened_from"] = length
        metrics["model_ms"] = metrics.get("model_ms", 0) + int((time.monotonic() - started) * 1000)
        metrics["prompt_tokens"] = metrics.get("prompt_tokens", 0) + (getattr(usage, "prompt_tokens", 0) or 0)
        metrics["prompt_cache_hit_tokens"] = metrics.get("prompt_cache_hit_tokens", 0) + (
            getattr(usage, "prompt_cache_hit_tokens", 0) or 0)
        metrics["completion_tokens"] = metrics.get("completion_tokens", 0) + (getattr(usage, "completion_tokens", 0) or 0)
        return result['reply'] if isinstance(result, dict) and result.get('action') == 'reply' and isinstance(result.get('reply'), str) else ''

    def reply(self, nickname, comment, context, max_chars, correction="", system_override=""):
        if self.client is None:
            return {"action": "skip", "missing_fields": ["deepseek_key_missing"],
                    "metrics": {}}
        started = time.monotonic()
        system = system_override or system_prompt()
        multi = context.get('delivery_protocol_version') == 2
        system += ('\nJSON reply thêm awaiting_fields: danh sách thông tin thực sự vừa hỏi khách để giải quyết câu đang tư vấn; '
            'chỉ dùng product, variant, quantity, crop, stage, method, tank_volume; không thiếu thì []. '
            'Một câu có dấu hỏi không có nghĩa đang làm rõ. Không hỏi lại thông tin khách đã cung cấp.')
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
            + f"\nKHÁCH {nickname}: {comment}\n"
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
            inference["temperature"] = SETTINGS["nhiet_do"]
        else:
            configured = muc_suy_nghi if muc_suy_nghi in {"low", "high", "max"} else "high"
            # An approved arithmetic conversion still uses thinking, but low
            # effort prevents a one-line dose answer consuming the whole budget.
            inference["reasoning_effort"] = "low" if context.get("verified_dose_calculations") else configured

        def elapsed_ms(): return int((time.monotonic() - started) * 1000)
        response = None
        for attempt in range(3):
            try:
                response = self.client.chat.completions.create(**inference)
                break
            except Exception as exc:
                # Tối đa 2 lần thử lại, chờ ngắn, chỉ với lỗi thoáng qua (timeout/mạng/429/5xx).
                # 401/402 (khoá/số dư) và các lỗi 4xx khác không bao giờ thử lại. Vẫn fail
                # closed — không bao giờ gửi mù.
                if attempt == 2 or not _is_transient_error(exc) or elapsed_ms() > 20000:
                    return {"action": "skip", "missing_fields": [f"deepseek_{type(exc).__name__}"],
                            "metrics": {"model_ms": elapsed_ms(), **({"retried_transient": True} if attempt else {})}}
                time.sleep(0.6 * (attempt + 1))
        usage = getattr(response, "usage", None)
        details = getattr(usage, "completion_tokens_details", None)
        metrics = {"model_ms": elapsed_ms(),
                   "model": inference.get("model", ""),
                   "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
                   # DeepSeek tinh gia input theo cache hit/miss (hit re hon
                   # nhieu lan). Thieu hai truong nay thi metrics_store phai
                   # coi CA prompt la cache miss — dat gia cao hon that, an
                   # toan hon la bao re hon thuc te.
                   "prompt_cache_hit_tokens": getattr(usage, "prompt_cache_hit_tokens", 0) or 0,
                   "prompt_cache_miss_tokens": getattr(usage, "prompt_cache_miss_tokens", 0) or 0,
                   "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
                   "reasoning_tokens": getattr(details, "reasoning_tokens", 0) or 0,
                   "finish_reason": getattr(response.choices[0], "finish_reason", "") or "",
                   "thinking": not tat_suy_nghi,
                   "reasoning_effort": inference.get("reasoning_effort", "")}
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
                inference["temperature"] = SETTINGS["nhiet_do"]
            else:
                inference["reasoning_effort"] = step
            try:
                response = self.client.chat.completions.create(**inference)
            except Exception as exc:
                return {"action": "skip", "missing_fields": [f"deepseek_{type(exc).__name__}"], "metrics": metrics}
            usage = getattr(response, "usage", None)
            details = getattr(usage, "completion_tokens_details", None)
            choice, content = response.choices[0], response.choices[0].message.content or ""
            metrics.update({"model_ms": elapsed_ms(),
                            "model": inference.get("model", ""),
                            "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
                            "prompt_cache_hit_tokens": getattr(usage, "prompt_cache_hit_tokens", 0) or 0,
                            "prompt_cache_miss_tokens": getattr(usage, "prompt_cache_miss_tokens", 0) or 0,
                            "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
                            "reasoning_tokens": getattr(details, "reasoning_tokens", 0) or 0,
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
                retry['temperature'] = SETTINGS['nhiet_do']
                try:
                    rewritten = self.client.chat.completions.create(**retry)
                    result = json.loads(rewritten.choices[0].message.content or '{}')
                    rows = parts(result)
                    usage = getattr(rewritten, 'usage', None)
                    for key in ('prompt_tokens', 'completion_tokens', 'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens'):
                        metrics[key] = metrics.get(key, 0) + (getattr(usage, key, 0) or 0)
                    metrics['multi_message_rewritten'] = True
                except Exception:
                    rows = None
            if rows is None:
                # Viết lại vẫn hỏng: gửi MỘT tin gộp, cắt gọn theo giới hạn thay vì im lặng.
                flat = _flatten_reply(result) or _flatten_reply(first_result)
                flat = _strip_nickname(_trim_utf16(flat, max_chars), nickname, max_chars)
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
            shorter = self._shorten(inference, content, len(body), max_chars, metrics)
            if shorter:
                body = _strip_nickname(shorter, nickname, max_chars)
        if utf16_length(body) > max_chars:
            return {"action": "skip", "missing_fields": ["model_reply_too_long"], "metrics": metrics}
        if not body:
            return {"action": "skip", "missing_fields": ["empty_model_reply"], "metrics": metrics}
        return {"action": "reply", "reply": body, 'awaiting_fields': result.get('awaiting_fields', []), "metrics": metrics}
