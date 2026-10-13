"""Nhà cung cấp AI cho việc ĐỌC tài liệu/ảnh sản phẩm (không phải trả lời khách).

- DeepSeek: chỉ đọc chữ (dùng lại client của bộ trả lời, không thêm khóa).
- Gemini: REST generativelanguage.googleapis.com, đọc được ảnh và PDF scan
  (``inline_data``). Chỉ dùng thư viện chuẩn (urllib), không thêm phụ thuộc.

Mọi lỗi đều thành ``AIProviderError`` với thông báo tiếng Việt dành cho chủ shop.
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request

GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_KEY_LINK_TARGET = "gemini_keys"       # khóa trong OPEN_TARGETS của server


class AIProviderError(Exception):
    """``code`` ổn định cho UI/test; ``str(exc)`` là câu tiếng Việt."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


# ------------------------------------------------------------------ Gemini
class GeminiProvider:
    name = "gemini"
    supports_files = True

    def __init__(self, api_key="", model="gemini-3.8-flash", timeout=90, opener=None):
        self.api_key = (api_key or "").strip()
        self.model = (model or "gemini-3.8-flash").strip()
        self.timeout = timeout
        self._opener = opener            # test: hàm (request, timeout) -> response

    @property
    def configured(self):
        return bool(self.api_key)

    def configure(self, api_key=None, model=None):
        if api_key is not None:
            self.api_key = str(api_key).strip()
        if model:
            self.model = str(model).strip()

    def _open(self, request):
        if self._opener:
            return self._opener(request, self.timeout)
        from net import ssl_context
        return urllib.request.urlopen(request, timeout=self.timeout, context=ssl_context())

    def build_body(self, system, user_text, files=()):
        parts = [{"text": user_text}]
        for item in files:
            parts.append({"inline_data": {"mime_type": item["mime"],
                                          "data": base64.b64encode(item["data"]).decode("ascii")}})
        return {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": parts}],
                "generationConfig": {"responseMimeType": "application/json", "temperature": 0.1}}

    def generate(self, system, user_text, files=()):
        """Trả về chuỗi JSON thô của model."""
        if not self.api_key:
            raise AIProviderError("gemini_key_missing",
                                  "Chưa có khóa Gemini. Vào Cài đặt, dán khóa vào ô “Key Gemini (đọc ảnh/PDF)”.")
        body = json.dumps(self.build_body(system, user_text, files)).encode("utf-8")
        request = urllib.request.Request(
            GEMINI_ENDPOINT.format(model=self.model), data=body, method="POST",
            headers={"Content-Type": "application/json", "x-goog-api-key": self.api_key})
        try:
            with self._open(request) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise AIProviderError("gemini_network",
                                  "Không kết nối được Gemini (kiểm tra mạng rồi thử lại).") from exc
        except json.JSONDecodeError as exc:
            raise AIProviderError("gemini_bad_response", "Gemini trả về dữ liệu không đọc được.") from exc
        return self._text_of(payload)

    def _http_error(self, exc):
        try:
            detail = json.loads(exc.read().decode("utf-8", "replace")).get("error", {})
        except Exception:
            detail = {}
        status = int(getattr(exc, "code", 0) or 0)
        message = str(detail.get("message") or "")
        if status == 429:
            return AIProviderError("gemini_quota",
                                   "Hết lượt miễn phí hôm nay (hoặc gọi quá nhanh). Đợi một lát rồi thử lại, "
                                   "hoặc ngày mai dùng tiếp.")
        if status in (401, 403) or (status == 400 and "api key" in message.lower()):
            return AIProviderError("gemini_key_rejected",
                                   "Khóa Gemini bị từ chối (sai, hết hạn hoặc chưa bật). Lấy khóa mới ở Cài đặt.")
        if status == 404:
            return AIProviderError("gemini_model_rejected",
                                   f"Gemini không nhận model “{self.model}”. Google có thể đã đổi tên: "
                                   "sửa GEMINI_MODEL trong file .env (xem danh sách ở ai.google.dev/gemini-api/docs/models).")
        if status == 400:
            return AIProviderError("gemini_bad_request", "Gemini không đọc được tệp này: " + message[:200])
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
                model=model, max_tokens=6000, temperature=0.1,
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
