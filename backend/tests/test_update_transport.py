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
