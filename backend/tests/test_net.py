"""Outbound TLS context and error classification (fresh-install false 'no network')."""
import importlib.util
import json
import socket
import ssl
import sys
import urllib.error
from pathlib import Path
from unittest import mock

import pytest

import net

PROJECT = Path(__file__).resolve().parents[2]


class ProxyError(Exception):
    pass


def _tls():
    return urllib.error.URLError(ssl.SSLCertVerificationError(1, "CERTIFICATE_VERIFY_FAILED"))


@pytest.mark.parametrize("exc,code", [
    (_tls(), "tls_untrusted"),
    (urllib.error.HTTPError("u", 401, "x", {}, None), "http_401"),
    (urllib.error.HTTPError("u", 403, "x", {}, None), "http_403"),
    (urllib.error.HTTPError("u", 402, "x", {}, None), "http_402"),
    (urllib.error.HTTPError("u", 503, "x", {}, None), "http_5xx"),
    (urllib.error.URLError(socket.gaierror(11001, "getaddrinfo failed")), "dns"),
    (ProxyError("Tunnel connection failed: 407"), "proxy"),
    (TimeoutError("timed out"), "timeout"),
    (urllib.error.URLError(ConnectionRefusedError(10061, "refused")), "offline"),
])
def test_classify_codes(exc, code):
    info = net.classify(exc, "api.deepseek.com")
    assert info["code"] == code
    assert info["message"] and info["detail"]


def test_classify_messages_name_host():
    assert "api.deepseek.com" in net.classify(_tls(), "api.deepseek.com")["message"]
    assert "chứng chỉ" in net.classify(_tls(), "h")["message"]
    assert "sai hoặc hết hạn" in net.classify(urllib.error.HTTPError("u", 401, "x", {}, None))["message"]


def test_env_sanitize_drops_missing_ca_files(tmp_path, monkeypatch):
    good = tmp_path / "ca.pem"
    good.write_text("x")
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "missing.pem"))
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(good))
    monkeypatch.delenv("CURL_CA_BUNDLE", raising=False)
    assert net.sanitize_env() == ["SSL_CERT_FILE"]
    import os
    assert "SSL_CERT_FILE" not in os.environ and os.environ["REQUESTS_CA_BUNDLE"] == str(good)


def test_ssl_context_falls_back_to_certifi_without_truststore(monkeypatch):
    net.reset_for_tests()
    monkeypatch.setitem(sys.modules, "truststore", None)  # import raises ImportError
    try:
        context = net.ssl_context()
        assert isinstance(context, ssl.SSLContext) and context.verify_mode == ssl.CERT_REQUIRED
        assert net.ssl_context() is context  # cached
    finally:
        net.reset_for_tests()


def test_ssl_context_prefers_truststore():
    truststore = pytest.importorskip("truststore")
    net.reset_for_tests()
    try:
        assert isinstance(net.ssl_context(), truststore.SSLContext)
    finally:
        net.reset_for_tests()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _responder():
    module = _load("_deepseek_net", PROJECT / "plugins" / "model-deepseek" / "processor.py")
    import threading
    r = module.DeepSeekResponder.__new__(module.DeepSeekResponder)
    r.client, r.api_key = object(), "k"
    r._balance_lock, r._balance_checked_at, r._balance_status = threading.Lock(), 0.0, None
    return module, r


def test_check_balance_401_is_key_rejected_not_network():
    module, r = _responder()
    err = urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)
    with mock.patch.object(module.urllib.request, "urlopen", side_effect=err):
        status = r.check_balance()
    assert status["ok"] is False and status["reason"] == "deepseek_key_rejected"
    assert "mạng" not in status["error"].lower().split(";")[0]


def test_check_balance_tls_and_unknown_codes():
    module, r = _responder()
    with mock.patch.object(module.urllib.request, "urlopen", side_effect=_tls()):
        assert r.check_balance()["reason"] == "deepseek_tls_untrusted"
    module, r = _responder()
    with mock.patch.object(module.urllib.request, "urlopen",
                           side_effect=json.JSONDecodeError("x", "y", 0)):
        assert r.check_balance()["reason"] == "deepseek_balance_check_failed"


def test_whisper_friendly_distinguishes_tls_and_offline():
    import whisper_models
    tls = whisper_models._friendly(_tls())
    off = whisper_models._friendly(urllib.error.URLError(ConnectionRefusedError("refused")))
    assert "chứng chỉ" in tls and "chứng chỉ" not in off
    assert "kiểm tra mạng" in off.lower()


def test_save_settings_returns_key_rejected():
    import openai
    import httpx
    import server
    request = httpx.Request("POST", "https://api.deepseek.com/models")
    auth = openai.AuthenticationError("bad", response=httpx.Response(401, request=request), body=None)
    assert server._key_failure(auth)["error"] == "key_rejected"
    conn = openai.APIConnectionError(request=request)
    conn.__cause__ = ssl.SSLCertVerificationError(1, "CERTIFICATE_VERIFY_FAILED")
    failure = server._key_failure(conn)
    assert failure["error"] == "key_tls_untrusted" and failure["ok"] is False
    assert server._key_failure(ValueError("x")) is None


def test_packaging_requires_certifi_and_truststore():
    reqs = (PROJECT / "backend" / "requirements.txt").read_text(encoding="utf-8")
    assert "truststore" in reqs and "certifi" in reqs
    spec = (PROJECT / "packaging" / "KidTE.spec").read_text(encoding="utf-8")
    assert 'collect_data_files("certifi")' in spec and "truststore" in spec
    assert "cacert.pem" in (PROJECT / "packaging" / "verify_release.ps1").read_text(encoding="utf-8")
    assert "_internal/certifi/cacert.pem" in (PROJECT / "packaging" / "stage_runtime.py").read_text(encoding="utf-8")
