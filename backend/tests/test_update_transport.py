import hashlib
import io
import urllib.error

import pytest
from update_transport import GitHubTransport, TransportError, _SafeRedirectHandler


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
    def __init__(self, response):
        self.response = response
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        response = self.response.pop(0) if isinstance(self.response, list) else self.response
        if isinstance(response, Exception):
            raise response
        return response


def test_fetch_catalog_uses_fixed_public_release_and_bounds_response():
    opener = Opener(Response(b'{"catalog":true}', headers={"ETag": '"v1"'}))
    transport = GitHubTransport(opener=opener)
    assert transport.fetch_catalog() == b'{"catalog":true}'
    assert opener.requests[0].full_url == "https://github.com/kidlost1412/KidTE/releases/download/updates-stable/kidte-catalog-v2.json"
    assert "Authorization" not in opener.requests[0].header_items()
    assert opener.requests[0].get_header("If-none-match") is None
    opener.response = urllib.error.HTTPError("https://github.com/catalog", 304, "Not Modified", {}, None)
    assert transport.fetch_catalog(cached=b"known") == b"known"
    assert opener.requests[1].get_header("If-none-match") == '"v1"'


def test_download_verifies_digest_and_size_before_publishing(tmp_path):
    content = b"release-archive"
    opener = Opener(Response(content))
    target = tmp_path / "package.zip"
    transport = GitHubTransport(opener=opener)
    result = transport.download(
        "https://github.com/kidlost1412/KidTE/releases/download/v1.2.3/core.zip",
        target, hashlib.sha256(content).hexdigest(), len(content),
    )
    assert result == target
    assert target.read_bytes() == content
    assert not target.with_name("package.zip.part").exists()


def test_download_rejects_untrusted_urls_redirects_and_digest_mismatch(tmp_path):
    transport = GitHubTransport(opener=Opener(Response(b"bad")))
    with pytest.raises(TransportError, match="url_rejected"):
        transport.download("https://example.com/a.zip", tmp_path / "a.zip", "a" * 64, 3)
    with pytest.raises(TransportError, match="digest_mismatch"):
        transport.download("https://github.com/kidlost1412/KidTE/releases/download/v1/a.zip", tmp_path / "a.zip", hashlib.sha256(b"good").hexdigest(), 3)
    assert not (tmp_path / "a.zip.part").exists()
    transport = GitHubTransport(opener=Opener(Response(b"bad", url="https://evil.example/a")))
    with pytest.raises(TransportError, match="redirect_rejected"):
        transport.download("https://github.com/kidlost1412/KidTE/releases/download/v1/a.zip", tmp_path / "a.zip", hashlib.sha256(b"bad").hexdigest(), 3)


def test_redirect_is_rejected_before_request_to_arbitrary_host():
    handler = _SafeRedirectHandler()
    request = __import__("urllib.request", fromlist=["Request"]).Request(
        "https://github.com/kidlost1412/KidTE/releases/download/v1/a.zip"
    )
    with pytest.raises(TransportError, match="redirect_destination_rejected"):
        handler.redirect_request(request, None, 302, "Found", {}, "https://evil.example/a.zip")
    with pytest.raises(TransportError, match="redirect_destination_rejected"):
        handler.redirect_request(request, None, 302, "Found", {}, "https://release-assets.githubusercontent.com:444/a.zip")


def test_download_resumes_only_from_valid_content_range(tmp_path):
    content = b"release-archive"
    partial = tmp_path / "package.zip.part"
    partial.write_bytes(content[:8])
    opener = Opener(Response(content[8:], status=206, headers={"Content-Range": f"bytes 8-{len(content)-1}/{len(content)}"}))
    target = tmp_path / "package.zip"
    transport = GitHubTransport(opener=opener)
    transport.download(
        "https://github.com/kidlost1412/KidTE/releases/download/v1.2.3/core.zip",
        target, hashlib.sha256(content).hexdigest(), len(content),
    )
    assert opener.requests[0].get_header("Range") == "bytes=8-"
    assert target.read_bytes() == content

    partial.write_bytes(content[:8])
    target.unlink()
    opener = Opener(Response(content[8:], status=206, headers={"Content-Range": f"bytes 7-{len(content)-1}/{len(content)}"}))
    with pytest.raises(TransportError, match="content_range_invalid"):
        GitHubTransport(opener=opener).download(
            "https://github.com/kidlost1412/KidTE/releases/download/v1.2.3/core.zip",
            target, hashlib.sha256(content).hexdigest(), len(content),
        )


def test_verified_cached_file_and_complete_partial_avoid_network(tmp_path):
    content = b"release-archive"
    digest = hashlib.sha256(content).hexdigest()
    target = tmp_path / "package.zip"
    target.write_bytes(content)
    transport = GitHubTransport(opener=Opener(OSError("network should not be called")))
    assert transport.download("https://github.com/kidlost1412/KidTE/releases/download/v1/a.zip", target, digest, len(content)) == target
    target.unlink()
    partial = target.with_name(target.name + ".part")
    partial.write_bytes(content)
    assert transport.download("https://github.com/kidlost1412/KidTE/releases/download/v1/a.zip", target, digest, len(content)) == target
    assert target.read_bytes() == content


def test_corrupt_complete_partial_is_removed_then_downloaded_again(tmp_path):
    content = b"verified"
    target = tmp_path / "package.zip"
    target.with_name("package.zip.part").write_bytes(b"corrupt!!")
    opener = Opener(Response(content))
    GitHubTransport(opener=opener).download(
        "https://github.com/kidlost1412/KidTE/releases/download/v1/a.zip",
        target, hashlib.sha256(content).hexdigest(), len(content),
    )
    assert target.read_bytes() == content
    assert not target.with_name("package.zip.part").exists()


def test_http_416_discards_stale_partial_and_restarts_from_zero(tmp_path):
    content = b"fresh-content"
    partial = tmp_path / "package.zip.part"
    partial.write_bytes(b"stale")
    opener = Opener([
        urllib.error.HTTPError("https://github.com/file", 416, "Range Not Satisfiable", {}, None),
        Response(content),
    ])
    target = tmp_path / "package.zip"
    GitHubTransport(opener=opener).download(
        "https://github.com/kidlost1412/KidTE/releases/download/v1/a.zip",
        target, hashlib.sha256(content).hexdigest(), len(content),
    )
    assert opener.requests[0].get_header("Range") == "bytes=5-"
    assert opener.requests[1].get_header("Range") is None
    assert target.read_bytes() == content


def test_download_rejects_assets_over_github_limit(tmp_path):
    with pytest.raises(ValueError, match="invalid_expected_artifact"):
        GitHubTransport(opener=Opener(Response(b""))).download(
            "https://github.com/kidlost1412/KidTE/releases/download/v1/a.zip",
            tmp_path / "a.zip", "a" * 64, 2 * 1024**3 + 1,
        )


class _BrokenResponse(Response):
    """Trả một phần rồi rớt kết nối như Windows (WinError 10054)."""
    def __init__(self, data, **kwargs):
        super().__init__(data, **kwargs)
        self._sent = False

    def read(self, count=-1):
        if self._sent:
            raise ConnectionResetError(10054, "An existing connection was forcibly closed")
        self._sent = True
        return self._stream.read(4)


def test_download_retries_and_resumes_after_connection_reset(tmp_path):
    content = b"release-archive-long"
    sleeps = []
    opener = Opener([
        _BrokenResponse(content),
        Response(content[4:], status=206, headers={"Content-Range": f"bytes 4-{len(content)-1}/{len(content)}"}),
    ])
    target = tmp_path / "package.zip"
    GitHubTransport(opener=opener, sleep=sleeps.append).download(
        "https://github.com/kidlost1412/KidTE/releases/download/v1/a.zip",
        target, hashlib.sha256(content).hexdigest(), len(content),
    )
    assert target.read_bytes() == content
    assert opener.requests[1].get_header("Range") == "bytes=4-"
    assert sleeps == [1]


def test_download_gives_up_with_named_code_after_retries(tmp_path):
    sleeps = []
    opener = Opener([ConnectionResetError(10054, "reset") for _ in range(10)])
    with pytest.raises(TransportError, match="^package_connection_reset$"):
        GitHubTransport(opener=opener, sleep=sleeps.append, download_retries=3).download(
            "https://github.com/kidlost1412/KidTE/releases/download/v1/a.zip",
            tmp_path / "a.zip", "a" * 64, 5,
        )
    assert sleeps == [1, 2, 4]
    assert len(opener.requests) == 4


def test_download_http_404_is_named_and_not_retried(tmp_path):
    opener = Opener([urllib.error.HTTPError("https://github.com/f", 404, "nf", {}, None)])
    with pytest.raises(TransportError, match="package_http_404"):
        GitHubTransport(opener=opener, sleep=lambda _s: None).download(
            "https://github.com/kidlost1412/KidTE/releases/download/v1/a.zip",
            tmp_path / "a.zip", "a" * 64, 5,
        )


def test_default_opener_uses_shared_ssl_context(monkeypatch):
    import ssl
    import urllib.request

    import update_transport
    context = ssl.create_default_context()
    monkeypatch.setattr(update_transport.net, "ssl_context", lambda: context)
    opener = GitHubTransport().opener
    https = [h for h in opener.handlers if isinstance(h, urllib.request.HTTPSHandler)]
    assert https and https[0]._context is context
    assert any(isinstance(h, _SafeRedirectHandler) for h in opener.handlers)


def _url_error(reason):
    return urllib.error.URLError(reason)


@pytest.mark.parametrize("error, code", [
    (lambda: _url_error(__import__("ssl").SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer certificate")), "catalog_tls_error"),
    (lambda: _url_error(__import__("socket").gaierror(11001, "getaddrinfo failed")), "catalog_dns_error"),
    (lambda: _url_error(TimeoutError("timed out")), "catalog_timeout"),
    (lambda: _url_error(OSError("Tunnel connection failed: 407 Proxy Authentication Required")), "catalog_proxy_error"),
    (lambda: _url_error(ConnectionRefusedError(10061, "refused")), "catalog_network_error"),
])
def test_catalog_network_errors_are_classified_and_not_backed_off(error, code, caplog):
    opener = Opener([error(), Response(b'{"ok":1}')])
    transport = GitHubTransport(opener=opener)
    with caplog.at_level("WARNING", logger="kidte.update"):
        with pytest.raises(TransportError, match=f"^{code}$"):
            transport.fetch_catalog()
    assert "update catalog fetch failed" in caplog.text
    assert transport.retry_delay() == 0
    assert transport.fetch_catalog() == b'{"ok":1}'  # retry right away, no catalog_backoff


@pytest.mark.parametrize("status, backoff", [(403, True), (429, True), (500, True), (503, True), (404, False)])
def test_catalog_backoff_only_for_github_refusals(status, backoff):
    now = [0.0]
    opener = Opener(urllib.error.HTTPError("https://github.com/c", status, "x", {}, None))
    transport = GitHubTransport(opener=opener, clock=lambda: now[0])
    with pytest.raises(TransportError, match=f"catalog_http_{status}"):
        transport.fetch_catalog()
    assert (transport.retry_delay() > 0) is backoff
    if backoff:
        with pytest.raises(TransportError, match="catalog_backoff"):
            transport.fetch_catalog()
        now[0] = 61
        opener.response = Response(b"{}")
        assert transport.fetch_catalog() == b"{}"


def test_manual_check_bypasses_backoff_left_by_background_check():
    opener = Opener(urllib.error.HTTPError("https://github.com/c", 429, "x", {}, None))
    transport = GitHubTransport(opener=opener, clock=lambda: 0.0)
    with pytest.raises(TransportError, match="catalog_http_429"):
        transport.fetch_catalog(background=True)
    with pytest.raises(TransportError, match="catalog_backoff"):
        transport.fetch_catalog(background=True)  # background still waits
    opener.response = Response(b"{}")
    assert transport.fetch_catalog() == b"{}"  # user click goes through
    # A backoff created by a manual check still applies to the next manual check.
    opener.response = urllib.error.HTTPError("https://github.com/c", 429, "x", {}, None)
    with pytest.raises(TransportError, match="catalog_http_429"):
        transport.fetch_catalog()
    with pytest.raises(TransportError, match="catalog_backoff"):
        transport.fetch_catalog()


def test_download_tls_error_is_named_and_not_retried(tmp_path):
    import ssl
    sleeps = []
    opener = Opener([_url_error(ssl.SSLCertVerificationError(1, "CERTIFICATE_VERIFY_FAILED")) for _ in range(3)])
    with pytest.raises(TransportError, match="^package_tls_error$"):
        GitHubTransport(opener=opener, sleep=sleeps.append).download(
            "https://github.com/kidlost1412/KidTE/releases/download/v1/a.zip",
            tmp_path / "a.zip", "a" * 64, 5,
        )
    assert sleeps == [] and len(opener.requests) == 1
