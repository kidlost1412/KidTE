"""Nhà cung cấp AI cho việc ĐỌC tài liệu/ảnh sản phẩm (không phải trả lời khách).

- DeepSeek: chỉ đọc chữ (dùng lại client của bộ trả lời, không thêm khóa).
- Gemini: REST generativelanguage.googleapis.com, đọc được ảnh và PDF scan
  (``inline_data``). Chỉ dùng thư viện chuẩn (urllib), không thêm phụ thuộc.

Mọi lỗi đều thành ``AIProviderError`` với thông báo tiếng Việt dành cho chủ shop.
"""
from __future__ import annotations

import base64
import json
import re
import time
import urllib.error
import urllib.request

GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_MODELS_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models"
GEMINI_FLASH_ALIAS = "gemini-flash-latest"   # Google luôn trỏ alias này tới một bản flash đang chạy
GEMINI_FALLBACK_MODEL = GEMINI_FLASH_ALIAS   # khi chưa dò được danh sách model
GEMINI_DEFAULT_CHAIN = (GEMINI_FLASH_ALIAS, "gemini-3.7-flash")   # dùng khi chưa dò được danh sách
GEMINI_DETECT_TTL = 24 * 3600                # dò lại model mới sau ~1 ngày
GEMINI_DETECT_RETRY = 600                    # dò lỗi (mạng/khóa) thì 10 phút sau mới thử lại
GEMINI_KEY_LINK_TARGET = "gemini_keys"       # khóa trong OPEN_TARGETS của server
GEMINI_TIMEOUT = 180                         # một lượt đọc tài liệu dài có thể mất gần 2 phút
GEMINI_MAX_ATTEMPTS = 4                      # số model thử tối đa cho một lượt gọi
GEMINI_RETRY_BACKOFF = 2                     # 503: chờ 2 giây rồi thử model kế tiếp
GEMINI_SKIP_NOT_FOUND = 24 * 3600            # model trả 404: tạm bỏ qua 1 ngày
GEMINI_SKIP_OVERLOADED = 600                 # model trả 503: tạm bỏ qua 10 phút

_SPECIAL = ("image", "tts", "audio", "live", "embed", "robotics", "computer", "learnlm", "gemma", "aqa")
_VERSION = re.compile(r"gemini-(\d+(?:\.\d+)*)-flash")


def _flash_order(models):
    """Model flash có generateContent, xếp theo ưu tiên: ổn định mới nhất > preview/exp > lite.

    Trong cùng nhóm: số phiên bản lớn nhất, rồi tên ngắn nhất (bỏ hậu tố ngày).
    Trả về (danh sách tên, alias gemini-flash-latest có trong danh sách hay không).
    """
    tiers = ([], [], [])
    alias_listed = False
    for item in models or []:
        name = str(item.get("name") or "").split("/")[-1].strip()
        low = name.lower()
        methods = item.get("supportedGenerationMethods") or []
        if "generateContent" not in methods or "flash" not in low:
            continue
        if low == GEMINI_FLASH_ALIAS:
            alias_listed = True
            continue
        if any(s in low for s in _SPECIAL):
            continue
        found = _VERSION.search(low)
        version = tuple(int(x) for x in found.group(1).split(".")) if found else ()
        if "lite" in low:
            tier = 2
        elif any(s in low for s in ("preview", "exp", "thinking")):
            tier = 1
        else:
            tier = 0
        tiers[tier].append(((version, -len(name)), name))
    ordered = [name for tier in tiers for _, name in sorted(tier, reverse=True)]
    return ordered, alias_listed


def pick_flash_model(models):
    """Model flash mới nhất (ổn định trước), từ phản hồi ``models.list``. Trả về "" nếu không có."""
    ordered, _ = _flash_order(models)
    return ordered[0] if ordered else ""


def pick_default_model(models):
    """Model mặc định tự chọn: alias gemini-flash-latest nếu Google liệt kê, không thì bản flash mới nhất."""
    ordered, alias_listed = _flash_order(models)
    if alias_listed:
        return GEMINI_FLASH_ALIAS
    return ordered[0] if ordered else ""


def auto_chain(models):
    """Chuỗi model tự chọn để thử theo thứ tự: alias trước nếu có, rồi các bản flash ổn định (mới nhất trước).

    Alias luôn có mặt trong chuỗi (kể cả khi không được liệt kê) để làm lựa chọn dự phòng.
    """
    ordered, alias_listed = _flash_order(models)
    if not ordered and not alias_listed:
        return []
    if alias_listed:
        return [GEMINI_FLASH_ALIAS] + ordered
    return ordered[:1] + [GEMINI_FLASH_ALIAS] + ordered[1:]


class AIProviderError(Exception):
    """``code`` ổn định cho UI/test; ``str(exc)`` là câu tiếng Việt."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


# ------------------------------------------------------------------ Gemini
class _ModelRejected(Exception):
    """Model cụ thể bị từ chối theo cách có thể thử model khác (404, 429, 503)."""

    def __init__(self, status, model, error):
        super().__init__(str(error))
        self.status = status
        self.model = model
        self.error = error


class GeminiProvider:
    name = "gemini"
    supports_files = True

    def __init__(self, api_key="", model="", timeout=GEMINI_TIMEOUT, opener=None, clock=time.time,
                 sleep=time.sleep):
        self.api_key = (api_key or "").strip()
        self.explicit_model = (model or "").strip()   # chủ shop tự đặt: luôn thử đầu tiên
        self.timeout = timeout
        self._opener = opener            # test: hàm (request, timeout) -> response
        self._clock = clock
        self._sleep = sleep
        self._candidates = []            # chuỗi model tự dò, đã xếp thứ tự thử
        self._model_options = []
        self._model_list_error = ""
        self._detected_at = 0.0
        self._failed_at = 0.0
        self._skipped = {}               # model -> thời điểm được thử lại (sau 404 / 503)
        self.last_model = ""             # model đã trả lời lần gọi gần nhất

    @property
    def configured(self):
        return bool(self.api_key)

    def _chain(self, loaded=True):
        """Thứ tự thử: model chủ shop đặt, rồi chuỗi tự dò (hoặc chuỗi mặc định nếu chưa dò được)."""
        names = [self.explicit_model] if self.explicit_model else []
        if loaded:
            for name in self._candidates or GEMINI_DEFAULT_CHAIN:
                chosen = _VERSION.search(self.explicit_model)
                version = _VERSION.search(name)
                # A fixed older Flash must not silently fall back to a newer model/alias.
                if chosen and (not version or tuple(map(int, version.group(1).split("."))) >
                               tuple(map(int, chosen.group(1).split(".")))):
                    continue
                if name not in names:
                    names.append(name)
        return names

    def _is_live(self, model):
        return self._clock() >= self._skipped.get(model, 0.0)

    @property
    def model(self):
        """Model sẽ thử đầu tiên: chủ shop đặt > chuỗi tự dò (bỏ model đang tạm nghỉ)."""
        names = self._chain()
        live = [name for name in names if self._is_live(name)]
        return (live or names or [GEMINI_FALLBACK_MODEL])[0]

    @property
    def model_source(self):
        if self.explicit_model and self.model == self.explicit_model:
            return "owner"
        return "auto" if self._candidates else "fallback"

    def configure(self, api_key=None, model=None):
        if api_key is not None:
            new_key = str(api_key).strip()
            if new_key != self.api_key:
                self._candidates, self._detected_at, self._failed_at = [], 0.0, 0.0
                self._model_options, self._model_list_error = [], ""
                self._skipped = {}
            self.api_key = new_key
        if model is not None:
            self.explicit_model = str(model).strip()
            self._skipped.pop(self.explicit_model, None)

    def available_models(self, force=False):
        """Public model metadata only; listing does not call generateContent."""
        if self.configured:
            self._refresh(force=force)
        return {"ok": True, "models": list(self._model_options),
                "selected_model": self.explicit_model, "effective_model": self.model,
                "model_source": self.model_source,
                "error": self._model_list_error if self.configured else "Lưu khóa Gemini để tải danh sách model.",
                "stale": bool(self._failed_at and self._model_options)}

    def detect_model(self, force=False):
        """Dò danh sách model (GET /v1beta/models). Không bao giờ ném lỗi; trả model đầu tiên sẽ thử.

        Chủ shop đặt model tay thì không dò. Có khóa + chưa đặt tay: lần đầu hoặc quá ~1 ngày mới gọi;
        lỗi thì thử lại sau 10 phút.
        """
        if self.explicit_model or not self.api_key:
            return self.model
        self._refresh(force)
        return self.model

    def _refresh(self, force=False):
        now = self._clock()
        if not force:
            if self._candidates and now - self._detected_at < GEMINI_DETECT_TTL:
                return True
            if self._failed_at and now - self._failed_at < GEMINI_DETECT_RETRY:
                return False
        try:
            models, token = [], ""
            for _ in range(10):                      # tối đa 10 trang
                url = GEMINI_MODELS_ENDPOINT + "?pageSize=1000" + (f"&pageToken={token}" if token else "")
                request = urllib.request.Request(url, method="GET", headers={"x-goog-api-key": self.api_key})
                with self._open(request, timeout=min(self.timeout, 15)) as response:
                    page = json.loads(response.read().decode("utf-8"))
                models.extend(page.get("models") or [])
                token = str(page.get("nextPageToken") or "")
                if not token:
                    break
            chain = auto_chain(models)
            ordered, alias_listed = _flash_order(models)
            names = ([GEMINI_FLASH_ALIAS] if alias_listed else []) + ordered
            by_name = {str(m.get("name") or "").split("/")[-1]: m for m in models}
            options = [{"id": name, "label": str(by_name[name].get("displayName") or name)} for name in names]
        except urllib.error.HTTPError as exc:
            self._model_list_error = str(self._http_error(exc))
            chain = []
        except Exception:
            self._model_list_error = "Không tải được danh sách model từ Google. Kiểm tra mạng rồi bấm tải lại."
            chain = []
        if chain:
            self._candidates, self._detected_at, self._failed_at = chain, now, 0.0
            self._model_options, self._model_list_error = options, ""
            return True
        if not self._model_list_error:
            self._model_list_error = "Google chưa trả về model Flash dùng được cho khóa này. Thử tải lại danh sách."
        self._failed_at = now
        return False

    def _open(self, request, timeout=None):
        timeout = timeout or self.timeout
        if self._opener:
            return self._opener(request, timeout)
        from net import ssl_context
        return urllib.request.urlopen(request, timeout=timeout, context=ssl_context())

    def build_body(self, system, user_text, files=()):
        parts = [{"text": user_text}]
        for item in files:
            parts.append({"inline_data": {"mime_type": item["mime"],
                                          "data": base64.b64encode(item["data"]).decode("ascii")}})
        return {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": parts}],
                "generationConfig": {"responseMimeType": "application/json", "temperature": 0.1,
                                     "maxOutputTokens": 32768}}

    def generate(self, system, user_text, files=()):
        """Trả về chuỗi JSON thô của model. Model lỗi 404/429/503 thì thử model kế tiếp (tối đa 4 lần)."""
        if not self.api_key:
            raise AIProviderError("gemini_key_missing",
                                  "Chưa có khóa Gemini. Vào Cài đặt, dán khóa vào ô “Key Gemini (đọc ảnh/PDF)”.")
        body = json.dumps(self.build_body(system, user_text, files)).encode("utf-8")
        return self._call(lambda _model: body, self._post)

    def generate_grounded(self, system, user_text):
        """Gọi Gemini có công cụ tra Google. Trả về (chữ trả lời, groundingMetadata).

        Gemini 2.x trở lên dùng ``google_search``; Gemini 1.x dùng ``google_search_retrieval``.
        Không bật responseMimeType JSON (nhiều model không cho dùng chung với công cụ tra cứu).
        """
        if not self.api_key:
            raise AIProviderError("gemini_key_missing",
                                  "Chưa có khóa Gemini. Vào Cài đặt, dán khóa vào ô “Key Gemini (đọc ảnh/PDF)”.")

        def body_for(model):
            tool = ({"google_search_retrieval": {}} if re.match(r"gemini-1(\.|-)", model)
                    else {"google_search": {}})
            return json.dumps({"systemInstruction": {"parts": [{"text": system}]},
                               "contents": [{"role": "user", "parts": [{"text": user_text}]}],
                               "tools": [tool],
                               "generationConfig": {"temperature": 0.1, "maxOutputTokens": 8192}}).encode("utf-8")
        payload = self._call(body_for, self._post_payload)
        text = self._text_of(payload)
        meta = {}
        for candidate in payload.get("candidates") or []:
            if isinstance(candidate.get("groundingMetadata"), dict):
                meta = candidate["groundingMetadata"]
                break
        return text, meta

    def _call(self, body_for, post):
        """Thử lần lượt các model; trả về payload JSON của model đầu tiên trả lời được."""
        loaded = not self.explicit_model        # lười: chỉ dò khi chưa có/hết hạn; model tay thì dò khi cần
        if loaded:
            self._refresh()
        tried, rejected = [], []
        while len(tried) < GEMINI_MAX_ATTEMPTS:
            pending = [n for n in self._chain(loaded) if n not in tried and self._is_live(n)]
            if not pending and not loaded:       # model tay lỗi: bấy giờ mới dò danh sách để dự phòng
                loaded = True
                self._refresh()
                continue
            if not pending and not tried:        # mọi model đang tạm nghỉ: vẫn thử một lượt
                pending = [n for n in self._chain(loaded) if n not in tried]
            if not pending:
                break
            model = pending[0]
            tried.append(model)
            try:
                payload = post(model, body_for(model))
            except _ModelRejected as exc:
                rejected.append(exc)
                now = self._clock()
                if exc.status == 404:
                    self._skipped[model] = now + GEMINI_SKIP_NOT_FOUND
                elif exc.status == 503:
                    self._skipped[model] = now + GEMINI_SKIP_OVERLOADED
                    if len(tried) < GEMINI_MAX_ATTEMPTS:
                        self._sleep(GEMINI_RETRY_BACKOFF)
                continue
            self.last_model = model
            return payload
        raise self._exhausted(rejected)

    def _post(self, model, body):
        return self._text_of(self._post_payload(model, body))

    def _post_payload(self, model, body):
        request = urllib.request.Request(
            GEMINI_ENDPOINT.format(model=model), data=body, method="POST",
            headers={"Content-Type": "application/json", "x-goog-api-key": self.api_key})
        try:
            with self._open(request) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            error = self._http_error(exc, model)
            status = int(getattr(exc, "code", 0) or 0)
            if status in (404, 429, 503):
                raise _ModelRejected(status, model, error) from None
            raise error from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise AIProviderError("gemini_network",
                                  "Không kết nối được Gemini (kiểm tra mạng rồi thử lại).") from exc
        except json.JSONDecodeError as exc:
            raise AIProviderError("gemini_bad_response", "Gemini trả về dữ liệu không đọc được.") from exc
        return payload

    @staticmethod
    def _exhausted(rejected):
        """Mọi model đều bị từ chối: báo nguyên nhân chung nhất để chủ shop biết phải làm gì."""
        statuses = [item.status for item in rejected]
        if 429 in statuses:
            return AIProviderError("gemini_quota",
                                   "Hết lượt miễn phí hôm nay (hoặc gọi quá nhanh). Đợi một lát rồi thử lại, "
                                   "hoặc ngày mai dùng tiếp.")
        if 503 in statuses:
            return AIProviderError("gemini_overloaded", "Google đang quá tải, thử lại sau ít phút.")
        if rejected:
            return rejected[-1].error
        return AIProviderError("gemini_model_rejected", "Gemini không nhận model nào. Thử lại sau.")

    def _http_error(self, exc, model=""):
        try:
            detail = json.loads(exc.read().decode("utf-8", "replace")).get("error", {})
        except Exception:
            detail = {}
        status = int(getattr(exc, "code", 0) or 0)
        message = str(detail.get("message") or "")
        model = model or self.model
        if status == 429:
            return AIProviderError("gemini_quota",
                                   "Hết lượt miễn phí hôm nay (hoặc gọi quá nhanh). Đợi một lát rồi thử lại, "
                                   "hoặc ngày mai dùng tiếp.")
        if status in (401, 403) or (status == 400 and "api key" in message.lower()):
            return AIProviderError("gemini_key_rejected",
                                   "Khóa Gemini bị từ chối (sai, hết hạn hoặc chưa bật). Lấy khóa mới ở Cài đặt.")
        if status == 404:
            return AIProviderError("gemini_model_rejected",
                                   f"Gemini không nhận model “{model}”. Google có thể đã đổi tên: "
                                   "nhập tên mới ở Cài đặt → Model Gemini (xem ai.google.dev/gemini-api/docs/models).")
        low = message.lower()
        if status == 400 and ("google_search" in low or "search" in low and ("tool" in low or "ground" in low)
                              or "grounding" in low):
            return AIProviderError("gemini_search_unsupported",
                                   f"Model “{model}” không hỗ trợ tra Google. Vào Cài đặt → Model Gemini, "
                                   "chọn bản Flash 2.0 trở lên (hoặc để tự chọn) rồi thử lại.")
        if status == 400:
            return AIProviderError("gemini_bad_request", "Gemini không đọc được tệp này: " + message[:200])
        if status == 503:
            return AIProviderError("gemini_overloaded", "Google đang quá tải, thử lại sau ít phút.")
        if status >= 500:
            return AIProviderError("gemini_server", "Gemini đang lỗi tạm thời, thử lại sau ít phút.")
        return AIProviderError("gemini_error", f"Gemini báo lỗi {status}.")

    @staticmethod
    def _text_of(payload):
        feedback = payload.get("promptFeedback") or {}
        if feedback.get("blockReason"):
            raise AIProviderError("gemini_blocked", "Gemini từ chối đọc nội dung này.")
        for candidate in payload.get("candidates") or []:
            parts = (candidate.get("content") or {}).get("parts") or []
            text = "".join(str(part.get("text") or "") for part in parts)
            if text.strip():
                return text
        raise AIProviderError("gemini_empty", "Gemini không trả về nội dung nào.")


# ------------------------------------------------------------------ DeepSeek
class DeepSeekTextProvider:
    name = "deepseek"
    supports_files = False

    def __init__(self, responder=None):
        self._responder = responder

    @property
    def configured(self):
        responder = self._responder() if callable(self._responder) else self._responder
        return getattr(responder, "client", None) is not None

    def generate(self, system, user_text, files=()):
        if files:
            raise AIProviderError("deepseek_no_files", "DeepSeek không đọc được ảnh/PDF scan.")
        responder = self._responder() if callable(self._responder) else self._responder
        client = getattr(responder, "client", None)
        if client is None:
            raise AIProviderError("deepseek_key_missing", "Chưa có khóa DeepSeek hay Gemini để đọc tài liệu.")
        model = getattr(responder, "model", "") or "deepseek-flash"
        try:
            response = client.with_options(timeout=120).chat.completions.create(
                model=model, max_tokens=8000, temperature=0.1,
                response_format={"type": "json_object"},
                extra_body={"thinking": {"type": "disabled"}},
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user_text}])
        except Exception as exc:
            name = type(exc).__name__
            if "RateLimit" in name:
                raise AIProviderError("deepseek_quota", "DeepSeek đang giới hạn lượt gọi, thử lại sau.") from exc
            if "Authentication" in name or "PermissionDenied" in name:
                raise AIProviderError("deepseek_key_rejected", "Khóa DeepSeek bị từ chối.") from exc
            raise AIProviderError("deepseek_error", "Không gọi được DeepSeek: " + name) from exc
        return response.choices[0].message.content or ""


# ------------------------------------------------------------------ chọn nhà cung cấp
def choose_provider(needs_vision, gemini, deepseek):
    """Ảnh / PDF scan -> bắt buộc Gemini. Còn lại: Gemini nếu có khóa, không thì DeepSeek."""
    gemini_ok = bool(gemini and gemini.configured)
    if needs_vision:
        if not gemini_ok:
            raise AIProviderError(
                "vision_needs_gemini",
                "Đọc ảnh hoặc PDF scan cần khóa Gemini (miễn phí). Vào Cài đặt, bấm “Lấy key miễn phí” "
                "rồi dán vào ô “Key Gemini (đọc ảnh/PDF)”. Hoặc chỉ dùng file có chữ / dán chữ / địa chỉ web.")
        return gemini
    if gemini_ok:
        return gemini
    if deepseek and deepseek.configured:
        return deepseek
    raise AIProviderError("no_provider", "Chưa có khóa AI. Dán khóa Gemini hoặc DeepSeek ở Cài đặt.")
