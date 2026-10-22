"""Independent tests from the owner's requirement (fresh-Windows "lỗi kết nối GitHub").

Requirement:
- the updater reaches GitHub over HTTPS with the app trust store (net.ssl_context()),
  including after redirects to the release-asset CDN;
- errors name the real cause (TLS / DNS / timeout / proxy / other network);
- network/TLS errors do NOT lock the updater for 60 s; only GitHub 403/429/5xx do;
- a manual "Cập nhật" is not blocked by a wait the background auto-check set,
  but a wait set by a manual check still blocks the next manual check;
- the background check is blocked by any wait.
"""
import hashlib
import io
import socket
import ssl
import urllib.error
import urllib.request

import net
import pytest
from update_manager import UpdateManager
from update_transport import GitHubTransport, TransportError, _SafeRedirectHandler

PACKAGE_URL = "https://github.com/kidlost1412/KidTE/releases/download/v1/a.zip"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Response:
    def __init__(self, data, *, status=200, url="https://release-assets.githubusercontent.com/download/file", headers=None):
        self._stream = io.BytesIO(data)
        self.status = status
        self._url = url
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, count=-1):
        return self._stream.read(count)

    def geturl(self):
        return self._url

    def getcode(self):
        return self.status


class Opener:
    """Fake network: pops one outcome per request; records every request that reached it."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request.full_url)
        outcome = self.outcomes.pop(0) if self.outcomes else Response(b'{"x":1}')
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def http_error(code):
    return urllib.error.HTTPError("https://github.com/x", code, "err", {}, None)


# Shapes urllib really raises on Windows.
def tls_verify_error():
    return urllib.error.URLError(ssl.SSLCertVerificationError(
        1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate (_ssl.c:1006)"))


def dns_error():
    return urllib.error.URLError(socket.gaierror(11001, "getaddrinfo failed"))


def timeout_error():
    return urllib.error.URLError(TimeoutError("timed out"))


def proxy_error():
    return urllib.error.URLError(OSError("Tunnel connection failed: 407 Proxy Authentication Required"))


def refused_error():
    return urllib.error.URLError(ConnectionRefusedError(10061, "No connection could be made because the target machine actively refused it"))


# ---------------------------------------------------------------- trust store

def _https_handlers(opener):
    return [h for h in opener.handlers if isinstance(h, urllib.request.HTTPSHandler)]


def test_default_opener_uses_app_trust_store_context():
    net.reset_for_tests()
    transport = GitHubTransport(series="p4")
    handlers = _https_handlers(transport.opener)
    assert len(handlers) == 1, "exactly one HTTPS handler, else urllib may pick a default-context one"
    assert handlers[0]._context is net.ssl_context()


def test_default_opener_keeps_redirect_guard_on_same_opener():
    """Redirects to the CDN go through the same opener => same TLS context, still host-checked."""
    transport = GitHubTransport(series="p4")
    guards = [h for h in transport.opener.handlers if isinstance(h, _SafeRedirectHandler)]
    assert len(guards) == 1
    plain = [h for h in transport.opener.handlers
             if type(h) is urllib.request.HTTPRedirectHandler]
    assert plain == [], "a stock redirect handler would bypass host validation"
    req = urllib.request.Request("https://github.com/kidlost1412/KidTE/releases/download/v1/a.zip")
    ok = guards[0].redirect_request(req, None, 302, "Found", {},
                                    "https://objects.githubusercontent.com/github-production-release-asset/x")
    assert ok is not None
    ok = guards[0].redirect_request(req, None, 302, "Found", {},
                                    "https://release-assets.githubusercontent.com/x")
    assert ok is not None
    with pytest.raises(TransportError, match="redirect_destination_rejected"):
        guards[0].redirect_request(req, None, 302, "Found", {}, "https://evil.example.com/x")
    with pytest.raises(TransportError, match="redirect_destination_rejected"):
        guards[0].redirect_request(req, None, 302, "Found", {}, "http://objects.githubusercontent.com/x")


def test_manager_default_transport_uses_trust_store_and_series():
    net.reset_for_tests()
    p4 = UpdateManager(None, launcher_caps="split-components x")
    p3 = UpdateManager(None, launcher_caps="")
    for manager in (p4, p3):
        handlers = _https_handlers(manager.transport.opener)
        assert handlers and handlers[0]._context is net.ssl_context()
    assert p4.transport.series == "p4"
    assert p3.transport.series == "p3"
    assert p3._channel == "beta"


# ------------------------------------------------------- named catalog errors

@pytest.mark.parametrize("make_error, code", [
    (tls_verify_error, "catalog_tls_error"),
    (dns_error, "catalog_dns_error"),
    (timeout_error, "catalog_timeout"),
    (lambda: socket.timeout("timed out"), "catalog_timeout"),
    (proxy_error, "catalog_proxy_error"),
    (refused_error, "catalog_network_error"),
    (lambda: OSError(22, "weird"), "catalog_network_error"),
])
def test_catalog_network_errors_are_named_and_never_lock_out(make_error, code):
    clock = Clock()
    opener = Opener([make_error(), make_error()])
    transport = GitHubTransport(opener=opener, clock=clock, series="p4")
    with pytest.raises(TransportError) as first:
        transport.fetch_catalog(channel="beta")
    assert str(first.value) == code
    assert transport.retry_delay("beta") == 0
    # Immediate retry must reach the network again (no 60 s lockout).
    with pytest.raises(TransportError) as second:
        transport.fetch_catalog(channel="beta")
    assert str(second.value) == code
    assert len(opener.requests) == 2


def test_catalog_generic_ssl_error_is_tls_not_network():
    """Antivirus HTTPS scanning often breaks the handshake with a non-verify SSLError."""
    opener = Opener([urllib.error.URLError(ssl.SSLError(1, "[SSL: WRONG_VERSION_NUMBER] wrong version number"))])
    transport = GitHubTransport(opener=opener, clock=Clock(), series="p4")
    with pytest.raises(TransportError, match="^catalog_tls_error$"):
        transport.fetch_catalog(channel="beta")
    assert transport.retry_delay("beta") == 0


@pytest.mark.parametrize("status", [403, 429, 500, 502, 503])
def test_github_side_errors_set_backoff(status):
    clock = Clock()
    opener = Opener([http_error(status)])
    transport = GitHubTransport(opener=opener, clock=clock, series="p4")
    with pytest.raises(TransportError, match=f"^catalog_http_{status}$"):
        transport.fetch_catalog(channel="beta")
    assert 30 <= transport.retry_delay("beta") <= 600
    with pytest.raises(TransportError, match="^catalog_backoff$"):
        transport.fetch_catalog(channel="beta")
    assert len(opener.requests) == 1
    clock.now += 601
    transport.fetch_catalog(channel="beta")
    assert len(opener.requests) == 2


def test_network_error_after_backoff_expiry_does_not_relock():
    clock = Clock()
    opener = Opener([http_error(429), dns_error(), Response(b'{"ok":1}')])
    transport = GitHubTransport(opener=opener, clock=clock, series="p4")
    with pytest.raises(TransportError):
        transport.fetch_catalog(channel="beta")
    clock.now += 601
    with pytest.raises(TransportError, match="^catalog_dns_error$"):
        transport.fetch_catalog(channel="beta")
    assert transport.retry_delay("beta") == 0
    assert transport.fetch_catalog(channel="beta") == b'{"ok":1}'


# ------------------------------------------------------- named package errors

@pytest.mark.parametrize("make_error, code", [
    (tls_verify_error, "package_tls_error"),
    (proxy_error, "package_proxy_error"),
])
def test_package_errors_are_named(tmp_path, make_error, code):
    sleeps = []
    opener = Opener([make_error() for _ in range(10)])
    transport = GitHubTransport(opener=opener, clock=Clock(), sleep=sleeps.append, series="p4")
    with pytest.raises(TransportError) as info:
        transport.download(PACKAGE_URL, tmp_path / "a.zip", hashlib.sha256(b"abc").hexdigest(), 3)
    assert str(info.value) == code
    assert not (tmp_path / "a.zip").exists()


def test_package_dns_error_is_named_and_retried_then_succeeds(tmp_path):
    sleeps = []
    opener = Opener([dns_error(), dns_error(), Response(b"abc")])
    transport = GitHubTransport(opener=opener, clock=Clock(), sleep=sleeps.append, series="p4")
    result = transport.download(PACKAGE_URL, tmp_path / "a.zip", hashlib.sha256(b"abc").hexdigest(), 3)
    assert result.read_bytes() == b"abc"
    assert len(opener.requests) == 3
    assert len(sleeps) == 2


def test_package_dns_error_gives_up_with_dns_code(tmp_path):
    opener = Opener([dns_error() for _ in range(20)])
    transport = GitHubTransport(opener=opener, clock=Clock(), sleep=lambda s: None,
                                download_retries=3, series="p4")
    with pytest.raises(TransportError, match="^package_dns_error$"):
        transport.download(PACKAGE_URL, tmp_path / "a.zip", hashlib.sha256(b"abc").hexdigest(), 3)


def test_package_connection_reset_and_timeout_codes_unchanged(tmp_path):
    for error, code in ((urllib.error.URLError(ConnectionResetError(10054, "reset")), "package_connection_reset"),
                        (urllib.error.URLError(TimeoutError("timed out")), "package_network_timeout")):
        opener = Opener([error for _ in range(20)])
        transport = GitHubTransport(opener=opener, clock=Clock(), sleep=lambda s: None,
                                    download_retries=2, series="p4")
        with pytest.raises(TransportError, match=f"^{code}$"):
            transport.download(PACKAGE_URL, tmp_path / "a.zip", hashlib.sha256(b"abc").hexdigest(), 3)


# ------------------------------------------- manual vs background (end-to-end)

class FakeStore:
    def load_pending(self):
        return None

    def load_active(self):
        return {"components": {}}

    def read_highwater(self):
        return 0

    def accept_sequence(self, sequence):
        pass


def _manager(tmp_path, outcomes):
    clock = Clock()
    opener = Opener(outcomes)
    transport = GitHubTransport(opener=opener, clock=clock, series="p4")
    manager = UpdateManager(str(tmp_path), transport=transport, trusted_keys={"k": b"x"}, store=FakeStore())
    return manager, opener, clock


def _join(manager):
    if manager._thread is not None:
        manager._thread.join(5)
        assert not manager._thread.is_alive()


def _manual(manager):
    manager.action("check", {"channel": "beta"})
    _join(manager)
    return manager.state()["error"]


def _background(manager):
    result = manager.auto_check()
    _join(manager)
    return result


def test_manual_check_not_blocked_by_background_backoff(tmp_path):
    manager, opener, clock = _manager(tmp_path, [http_error(429), http_error(429)])
    _background(manager)
    assert manager.state()["error"] == "catalog_http_429"
    assert len(opener.requests) == 1
    clock.now += 5
    # Owner presses "Cập nhật": must reach GitHub, not answer "catalog_backoff".
    assert _manual(manager) == "catalog_http_429"
    assert len(opener.requests) == 2


def test_manual_backoff_blocks_next_manual(tmp_path):
    manager, opener, clock = _manager(tmp_path, [http_error(503)])
    assert _manual(manager) == "catalog_http_503"
    clock.now += 5
    assert _manual(manager) == "catalog_backoff"
    assert len(opener.requests) == 1


def test_background_blocked_by_any_backoff(tmp_path):
    manager, opener, clock = _manager(tmp_path, [http_error(429), http_error(429)])
    _manual(manager)
    clock.now += 5
    _background(manager)
    assert len(opener.requests) == 1
    _background(manager)
    assert len(opener.requests) == 1


def test_background_backoff_then_manual_429_blocks_next_manual(tmp_path):
    """Manual bypass is one-shot: once a manual check is refused by GitHub, the next manual waits."""
    manager, opener, clock = _manager(tmp_path, [http_error(429), http_error(429), http_error(429)])
    _background(manager)
    clock.now += 5
    assert _manual(manager) == "catalog_http_429"
    clock.now += 5
    assert _manual(manager) == "catalog_backoff"
    assert len(opener.requests) == 2


def test_stale_background_flag_does_not_let_manual_bypass_later_manual_backoff(tmp_path):
    """Background wait expires; later a manual 429 must block the next manual check."""
    manager, opener, clock = _manager(tmp_path, [http_error(429), http_error(429), http_error(429)])
    _background(manager)
    clock.now += 601
    assert _manual(manager) == "catalog_http_429"
    clock.now += 5
    assert _manual(manager) == "catalog_backoff"
    assert len(opener.requests) == 2


def test_manual_backoff_then_background_backoff_lets_manual_through(tmp_path):
    manager, opener, clock = _manager(tmp_path, [http_error(429), http_error(429), http_error(429)])
    _manual(manager)
    clock.now += 601
    _background(manager)
    assert len(opener.requests) == 2
    clock.now += 5
    assert _manual(manager) == "catalog_http_429"
    assert len(opener.requests) == 3


def test_successful_manual_fetch_clears_backoff_for_background(tmp_path):
    manager, opener, clock = _manager(tmp_path, [http_error(429), Response(b'{"not":"signed"}'), http_error(500)])
    _background(manager)
    clock.now += 5
    _manual(manager)  # fetch succeeds (signature check fails afterwards, irrelevant here)
    assert len(opener.requests) == 2
    assert manager.transport.retry_delay("beta") == 0
    clock.now += 5
    _background(manager)
    assert len(opener.requests) == 3


def test_tls_error_end_to_end_manual_then_immediate_retry(tmp_path):
    manager, opener, clock = _manager(tmp_path, [tls_verify_error(), tls_verify_error()])
    assert _manual(manager) == "catalog_tls_error"
    assert _manual(manager) == "catalog_tls_error"
    assert len(opener.requests) == 2
    _background(manager)
    assert len(opener.requests) == 3


def test_proxy_refusing_connect_tunnel_is_named_proxy():
    """http.client raises OSError('Tunnel connection failed: <code> <reason>') only when a proxy
    refused CONNECT. A corporate proxy answering 403 Forbidden is still a proxy problem."""
    opener = Opener([urllib.error.URLError(OSError("Tunnel connection failed: 403 Forbidden"))])
    transport = GitHubTransport(opener=opener, clock=Clock(), series="p4")
    with pytest.raises(TransportError, match="^catalog_proxy_error$"):
        transport.fetch_catalog(channel="beta")
