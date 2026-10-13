"""Kiểm tra khóa API (DeepSeek, Gemini) bằng một lời gọi rẻ, KHÔNG bao giờ trả lại hay ghi khóa ra log.

Kết quả luôn là dict {"ok": True, "provider", "status", "message", ...}; ``status`` là một trong
ok | invalid | no_balance | quota | missing | network | error. Mã HTTP bất thường không ném lỗi ra UI.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from ai_providers import GEMINI_MODELS_ENDPOINT, pick_flash_model

TIMEOUT_S = 12


def _result(provider, status, message, **extra):
    return {"ok": True, "provider": provider, "status": status, "valid": status == "ok",
            "message": message, **extra}


def _open(request, opener=None, timeout=TIMEOUT_S):
    if opener:
        return opener(request, timeout)
    from net import ssl_context
    return urllib.request.urlopen(request, timeout=timeout, context=ssl_context())


def _net_error(provider, name, exc):
    try:
        from net import classify
        info = classify(exc, "api.deepseek.com" if provider == "deepseek" else "generativelanguage.googleapis.com")
        return _result(provider, "network", info.get("message") or f"Không kết nối được {name}.")
    except Exception:
        return _result(provider, "network", f"Không kết nối được {name}. Kiểm tra mạng rồi thử lại.")


def _fmt_money(info):
    try:
        amount = float(str(info.get("total_balance")))
    except (TypeError, ValueError):
        return ""
    currency = str(info.get("currency") or "").strip()[:6]
    return f"{amount:,.2f} {currency}".strip()


def check_deepseek(api_key, base_url="https://api.deepseek.com", opener=None):
    """GET /user/balance: 401 = khóa sai, 402/số dư 0 = hết tiền, 200 = ok (kèm số dư)."""
    key = str(api_key or "").strip()
    if not key:
        return _result("deepseek", "missing", "Chưa có khóa DeepSeek.")
    request = urllib.request.Request(base_url.rstrip("/") + "/user/balance", method="GET",
                                     headers={"Authorization": f"Bearer {key}", "Accept": "application/json"})
    try:
        with _open(request, opener) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        code = int(getattr(exc, "code", 0) or 0)
        if code in (401, 403):
            return _result("deepseek", "invalid", "Khóa DeepSeek không đúng hoặc đã bị thu hồi.")
        if code == 402:
            return _result("deepseek", "no_balance", "Hết tiền trong tài khoản DeepSeek.")
        if code == 429:
            return _result("deepseek", "quota", "DeepSeek đang giới hạn tốc độ, thử lại sau ít phút.")
        if code >= 500:
            return _result("deepseek", "error", "DeepSeek đang lỗi tạm thời, thử lại sau.")
        return _result("deepseek", "error", f"DeepSeek báo lỗi {code}.")
    except json.JSONDecodeError:
        return _result("deepseek", "error", "DeepSeek trả về dữ liệu không đọc được.")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return _net_error("deepseek", "DeepSeek", exc)
    infos = [i for i in (payload.get("balance_infos") or []) if isinstance(i, dict)]
    balance = "; ".join(filter(None, (_fmt_money(i) for i in infos)))
    if payload.get("is_available") is False:
        return _result("deepseek", "no_balance", "Hết tiền trong tài khoản DeepSeek.", balance=balance)
    message = "Khóa DeepSeek dùng được." + (f" Số dư: {balance}." if balance else "")
    return _result("deepseek", "ok", message, balance=balance)


def check_gemini(api_key, explicit_model="", opener=None):
    """GET /v1beta/models: 400/401/403 = khóa sai, 429 = hết lượt, 200 = ok + model sẽ dùng."""
    key = str(api_key or "").strip()
    if not key:
        return _result("gemini", "missing", "Chưa có khóa Gemini.")
    request = urllib.request.Request(GEMINI_MODELS_ENDPOINT + "?pageSize=1000", method="GET",
                                     headers={"x-goog-api-key": key})
    try:
        with _open(request, opener) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        code = int(getattr(exc, "code", 0) or 0)
        if code in (400, 401, 403):
            return _result("gemini", "invalid", "Khóa Gemini không đúng, hết hạn hoặc chưa bật.")
        if code == 429:
            return _result("gemini", "quota", "Khóa Gemini hợp lệ nhưng đã hết lượt hôm nay, thử lại sau.")
        if code >= 500:
            return _result("gemini", "error", "Gemini đang lỗi tạm thời, thử lại sau.")
        return _result("gemini", "error", f"Gemini báo lỗi {code}.")
    except json.JSONDecodeError:
        return _result("gemini", "error", "Gemini trả về dữ liệu không đọc được.")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return _net_error("gemini", "Gemini", exc)
    model = str(explicit_model or "").strip() or pick_flash_model(payload.get("models") or [])
    return _result("gemini", "ok", "Khóa Gemini dùng được." + (f" Model sẽ dùng: {model}." if model else ""),
                   model=model)
