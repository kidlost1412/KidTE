"""Outbound HTTPS helpers: one TLS context + stable, human-readable error classes.

Why: urllib's default ssl context only trusts roots ALREADY in the Windows
store. A fresh Windows install fetches roots lazily (CryptoAPI chain building),
which python's ssl never triggers, so api.deepseek.com / huggingface.co failed
verification and was reported as "no network". truststore verifies through
CryptoAPI (incl. on-demand root download); fallback is Windows store + certifi.
"""
from __future__ import annotations

import logging
import os
import socket
import ssl
import threading
import urllib.error

log = logging.getLogger("kidte.net")

CA_ENV_VARS = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")
_lock = threading.Lock()
_context = None


def sanitize_env():
    """Drop CA-bundle env vars that point to missing files. Returns dropped names."""
    dropped = []
    for name in CA_ENV_VARS:
        value = os.environ.get(name)
        if value and not os.path.isfile(value):
            os.environ.pop(name, None)
            dropped.append(name)
            log.warning("Bỏ biến môi trường %s: file không tồn tại (%s)", name, value)
    return dropped


def _build_context():
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception as exc:  # ImportError or platform failure
        log.info("truststore không dùng được (%s); dùng certifi", type(exc).__name__)
    context = ssl.create_default_context()
    try:
        import certifi
        context.load_verify_locations(certifi.where())
    except Exception as exc:
        log.warning("Không nạp được certifi: %s", type(exc).__name__)
    return context


def ssl_context():
    global _context
    with _lock:
        if _context is None:
            sanitize_env()
            _context = _build_context()
        return _context


def reset_for_tests():
    global _context
    with _lock:
        _context = None


def _chain(exc):
    seen, items = set(), []
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        items.append(exc)
        if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, BaseException):
            exc = exc.reason
        else:
            exc = exc.__cause__ or exc.__context__
    return items


def _detail(exc):
    inner = _chain(exc)[-1]
    reason = str(inner)[:160]
    return f"{type(inner).__name__}: {reason}" if reason else type(inner).__name__


def classify(exc, host="máy chủ"):
    """Return {"code", "message", "detail"} for a network/HTTP exception."""
    chain = _chain(exc)
    names = {type(item).__name__ for item in chain}
    status = None
    for item in chain:
        if isinstance(item, urllib.error.HTTPError):
            status = item.code
        elif isinstance(getattr(item, "status_code", None), int):
            status = item.status_code
    if status is not None:
        if status in (401, 403, 402):
            code = f"http_{status}"
        elif status >= 500:
            code = "http_5xx"
        else:
            code = f"http_{status}"
    elif any(isinstance(i, ssl.SSLCertVerificationError)
             or "CertificateVerifyError" in type(i).__name__
             or "CERTIFICATE_VERIFY_FAILED" in str(i) for i in chain):
        code = "tls_untrusted"
    elif any(isinstance(i, ssl.SSLError) for i in chain):
        code = "tls_error"
    elif "ProxyError" in names or any(
            "proxy" in str(i).lower() and "tunnel" in str(i).lower() for i in chain):
        code = "proxy"
    elif any(isinstance(i, socket.gaierror) for i in chain) or any(
            "getaddrinfo" in str(i) or "Name or service not known" in str(i) for i in chain):
        code = "dns"
    elif any(isinstance(i, (TimeoutError, socket.timeout)) for i in chain) or any(
            "Timeout" in n for n in names):
        code = "timeout"
    else:
        code = "offline"
    messages = {
        "tls_untrusted": f"Máy chưa tin chứng chỉ bảo mật của {host} — thường do phần mềm diệt virus/proxy quét HTTPS hoặc Windows thiếu chứng chỉ gốc",
        "tls_error": f"Lỗi bắt tay bảo mật (TLS) với {host} — kiểm tra phần mềm diệt virus/proxy và giờ hệ thống",
        "dns": f"Không phân giải được tên {host} (DNS)",
        "proxy": f"Proxy/VPN của máy chặn kết nối tới {host}",
        "timeout": f"{host} phản hồi quá chậm (hết thời gian chờ) — kiểm tra mạng rồi thử lại",
        "offline": f"Không kết nối được {host} — kiểm tra mạng",
        "http_401": "Khoá bị từ chối (sai hoặc hết hạn)",
        "http_403": "Khoá bị từ chối (sai hoặc hết hạn)",
        "http_402": "Tài khoản đã hết số dư",
        "http_5xx": f"{host} đang lỗi, thử lại sau",
    }
    message = messages.get(code, f"{host} trả lỗi HTTP {status}")
    return {"code": code, "message": message, "detail": _detail(exc)}


def proxy_info():
    info = {name: os.environ[name] for name in
            ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "NO_PROXY")
            if os.environ.get(name)}
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as key:
            for name in ("ProxyEnable", "ProxyServer"):
                try:
                    info["registry_" + name] = str(winreg.QueryValueEx(key, name)[0])
                except OSError:
                    pass
    except (ImportError, OSError):
        log.debug("proxy registry read failed", exc_info=True, extra={"event": "proxy_registry_read_failed"})
    return info


def probe(host, port=443, timeout=6):
    """DNS + TCP + TLS handshake to host. Returns {"ok", "step", ...}."""
    result = {"host": host, "ok": False, "step": "dns"}
    try:
        socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        result["step"] = "tls"
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with ssl_context().wrap_socket(raw, server_hostname=host):
                pass
        result.update(ok=True, step="ok")
    except Exception as exc:
        result.update(classify(exc, host))
    return result
