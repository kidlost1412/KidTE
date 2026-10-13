"""Nút "Kiểm tra key" (DeepSeek, Gemini): phân loại 401/402/429/ok bằng mô phỏng, không bao giờ lộ khóa."""
import io
import json
import urllib.error

import pytest

import key_check

SECRET = "sk-test-SECRET-1234567890"


class _Resp:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _opener(payload=None, status=200, seen=None):
    def opener(request, timeout):
        if seen is not None:
            seen.append(request)
        if status != 200:
            raise urllib.error.HTTPError(request.full_url, status, "x", {}, io.BytesIO(b"{}"))
        return _Resp(payload)
    return opener


def test_deepseek_ok_shows_balance_and_sends_bearer_only_in_header():
    seen = []
    out = key_check.check_deepseek(SECRET, opener=_opener(
        {"is_available": True, "balance_infos": [{"currency": "USD", "total_balance": "12.5"}]}, seen=seen))
    assert out["status"] == "ok" and out["valid"] and "12.50 USD" in out["message"]
    assert seen[0].full_url.endswith("/user/balance") and seen[0].get_header("Authorization") == f"Bearer {SECRET}"
    assert SECRET not in json.dumps(out)


@pytest.mark.parametrize("status,expect,text", [(401, "invalid", "không đúng"), (403, "invalid", "không đúng"),
                                                (402, "no_balance", "Hết tiền trong tài khoản DeepSeek"),
                                                (429, "quota", "giới hạn"), (503, "error", "lỗi tạm thời")])
def test_deepseek_http_errors_are_classified(status, expect, text):
    out = key_check.check_deepseek(SECRET, opener=_opener(status=status))
    assert out["ok"] and out["status"] == expect and text in out["message"] and not out["valid"]
    assert SECRET not in json.dumps(out)


def test_deepseek_zero_balance_flag_and_missing_key_and_network():
    out = key_check.check_deepseek(SECRET, opener=_opener({"is_available": False, "balance_infos": []}))
    assert out["status"] == "no_balance" and "Hết tiền trong tài khoản DeepSeek" in out["message"]
    assert key_check.check_deepseek("  ")["status"] == "missing"

    def boom(request, timeout):
        raise urllib.error.URLError("dns")
    net = key_check.check_deepseek(SECRET, opener=boom)
    assert net["status"] == "network" and SECRET not in json.dumps(net)


def test_gemini_ok_picks_model_and_prefers_owner_model():
    models = {"models": [{"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]},
                         {"name": "models/gemini-1.5-pro", "supportedGenerationMethods": ["generateContent"]}]}
    seen = []
    out = key_check.check_gemini("AIza-" + "x" * 20, opener=_opener(models, seen=seen))
    assert out["status"] == "ok" and out["model"] == "gemini-2.5-flash" and "gemini-2.5-flash" in out["message"]
    assert seen[0].get_header("X-goog-api-key").startswith("AIza-") and "AIza" not in seen[0].full_url
    assert key_check.check_gemini("AIza-" + "x" * 20, "my-model", opener=_opener(models))["model"] == "my-model"


@pytest.mark.parametrize("status,expect,text", [(400, "invalid", "không đúng"), (401, "invalid", "không đúng"),
                                                (403, "invalid", "không đúng"), (429, "quota", "hết lượt hôm nay")])
def test_gemini_http_errors_are_classified(status, expect, text):
    out = key_check.check_gemini("AIza-secret-key-0000", opener=_opener(status=status))
    assert out["status"] == expect and text in out["message"]
    assert "AIza-secret" not in json.dumps(out)


def test_engine_check_key_routes_and_never_echoes_key(monkeypatch, tmp_path):
    import ctx_harness as h
    engine, _captured = h.make_engine(tmp_path)
    try:
        monkeypatch.setattr(key_check, "check_deepseek",
                            lambda key, base_url, opener=None: {"ok": True, "status": "ok", "message": "m", "n": len(key)})
        monkeypatch.setattr(key_check, "check_gemini",
                            lambda key, model="", opener=None: {"ok": True, "status": "quota", "message": "q", "n": len(key)})
        out = engine.check_key({"provider": "deepseek", "api_key": SECRET})
        assert out["status"] == "ok" and out["n"] == len(SECRET) and SECRET not in json.dumps(out)
        assert engine.check_key({"provider": "gemini", "api_key": "AIza-0123456789"})["status"] == "quota"
        with pytest.raises(ValueError, match="key_provider_unknown"):
            engine.check_key({"provider": "other"})
    finally:
        engine.close()
