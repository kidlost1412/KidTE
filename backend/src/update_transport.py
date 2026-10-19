"""Bounded, anonymous transport for public GitHub update releases."""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import stat
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

import net

log = logging.getLogger("kidte.update")

REPOSITORY = "kidlost1412/KidTE"
_ASSET_HOSTS = {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com", "github-releases.githubusercontent.com"}
_MAX_CATALOG = 2 * 1024 * 1024
_MAX_ASSET = 2 * 1024**3
_CHUNK = 256 * 1024


class TransportError(RuntimeError):
    pass


# Lỗi tạm thời của mạng: tự thử lại (tải tiếp từ .part), giống tải model Whisper.
_RETRYABLE = {"package_network_error", "package_connection_reset", "package_network_timeout",
              "package_size_mismatch", "package_http_429", "package_http_500", "package_http_502",
              "package_http_503", "package_http_504", "package_dns_error"}
DOWNLOAD_RETRIES = 6
# Chỉ lỗi phía GitHub (chặn/giới hạn/quá tải) mới phải chờ; lỗi mạng/TLS của máy thì thử lại ngay được.
_BACKOFF_HTTP = {403, 429}
_BACKOFF_SECONDS = 60
_CATALOG_NET_CODES = {"tls_untrusted": "catalog_tls_error", "tls_error": "catalog_tls_error",
                      "dns": "catalog_dns_error", "timeout": "catalog_timeout", "proxy": "catalog_proxy_error"}


def _catalog_network_code(exc):
    """Lỗi mạng khi lấy danh mục -> mã riêng (TLS/DNS/proxy/quá giờ) và ghi log nguyên nhân thật."""
    info = net.classify(exc, "github.com")
    log.warning("update catalog fetch failed: %s (%s)", info["code"], info["detail"])
    return _CATALOG_NET_CODES.get(info["code"], "catalog_network_error")


def _network_code(exc):
    """Đổi lỗi mạng thô (ConnectionResetError [Errno 10054]...) thành mã có tên."""
    import http.client
    import socket

    info = net.classify(exc, "github.com")
    if info["code"] in {"tls_untrusted", "dns", "proxy"}:
        log.warning("update package download failed: %s (%s)", info["code"], info["detail"])
        return {"tls_untrusted": "package_tls_error", "dns": "package_dns_error",
                "proxy": "package_proxy_error"}[info["code"]]
    reason = getattr(exc, "reason", None)
    inner = reason if isinstance(reason, BaseException) else exc
    if isinstance(inner, (TimeoutError, socket.timeout)):
        return "package_network_timeout"
    if isinstance(inner, (ConnectionError, http.client.IncompleteRead, http.client.RemoteDisconnected)):
        return "package_connection_reset"
    return "package_network_error"


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject unsafe redirect destinations before urllib makes the next request."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        p = urlparse(newurl)
        if (p.scheme != "https" or p.hostname not in _ASSET_HOSTS or p.port is not None
                or p.username or p.password or p.fragment):
            raise TransportError("redirect_destination_rejected")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _allowed_url(url, package=False):
    if not isinstance(url, str):
        return False
    p = urlparse(url)
    prefix = f"/{REPOSITORY}/releases/download/"
    if p.scheme != "https" or p.hostname != "github.com" or p.port is not None or p.query or p.fragment or p.username or p.password or not p.path.startswith(prefix):
        return False
    rest = p.path[len(prefix):]
    return bool(re.fullmatch(r"updates-(?:stable|beta)(?:-p3|-p4)?/kidte-catalog-v2\.json", rest)) if not package else bool(re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", rest))


class GitHubTransport:
    def __init__(self, repository=REPOSITORY, opener=None, timeout=20, clock=time.monotonic, *, series="p2",
                 sleep=time.sleep, download_retries=DOWNLOAD_RETRIES):
        if repository != REPOSITORY:
            raise ValueError("unsupported_repository")
        self.repository = repository
        if series not in {"p2", "p3", "p4"}:
            raise ValueError("unsupported_catalog_series")
        self.series = series
        # HTTPS qua net.ssl_context() (truststore/certifi): Windows mới cài chưa nạp sẵn chứng chỉ gốc.
        self.opener = opener or urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=net.ssl_context()), _SafeRedirectHandler())
        self.timeout = timeout
        self.clock = clock
        self._etag = {}
        self._cache = {}
        self._retry_after = {}
        self._background_backoff = set()  # kênh mà lần chờ do kiểm tra nền đặt ra
        self.sleep = sleep
        self.download_retries = download_retries

    def fetch_catalog(self, channel="stable", cached=None, *, background=False):
        """``background=True`` cho kiểm tra tự động; người dùng bấm tay thì bỏ qua lần chờ do nền đặt."""
        if channel not in {"stable", "beta"}:
            raise ValueError("unsupported_channel")
        if self.retry_delay(channel) > 0 and (background or channel not in self._background_backoff):
            raise TransportError("catalog_backoff")
        suffix = {"p3": "-p3", "p4": "-p4"}.get(self.series, "")
        url = f"https://github.com/{self.repository}/releases/download/updates-{channel}{suffix}/kidte-catalog-v2.json"
        headers = {"User-Agent": "KidTE-Updater/2", "Accept": "application/octet-stream"}
        if self._etag.get(channel):
            headers["If-None-Match"] = self._etag[channel]
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            response = self.opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 304 and (cached is not None or channel in self._cache):
                return cached if cached is not None else self._cache[channel]
            if exc.code in _BACKOFF_HTTP or exc.code >= 500:
                self._retry_after[channel] = self.clock() + _BACKOFF_SECONDS
                if background:
                    self._background_backoff.add(channel)
                else:
                    self._background_backoff.discard(channel)
            log.warning("update catalog fetch failed: HTTP %s", exc.code)
            raise TransportError(f"catalog_http_{exc.code}") from exc
        except (OSError, TimeoutError) as exc:
            raise TransportError(_catalog_network_code(exc)) from exc
        with response:
            if not self._check_final_url(response, catalog=True):
                raise TransportError("catalog_redirect_rejected")
            length = response.headers.get("Content-Length")
            if length and (not length.isdigit() or int(length) > _MAX_CATALOG):
                raise TransportError("catalog_too_large")
            raw = response.read(_MAX_CATALOG + 1)
            if len(raw) > _MAX_CATALOG:
                raise TransportError("catalog_too_large")
            if len(raw) == 0:
                raise TransportError("empty_catalog")
            etag = response.headers.get("ETag")
            if etag:
                self._etag[channel] = etag
            self._cache[channel] = raw
            self._retry_after.pop(channel, None)
            self._background_backoff.discard(channel)
            return raw

    def retry_delay(self, channel="stable"):
        return max(0.0, self._retry_after.get(channel, 0) - self.clock())

    @staticmethod
    def _check_final_url(response, catalog=False):
        final = urlparse(response.geturl())
        return (final.scheme == "https" and final.hostname in _ASSET_HOSTS and final.port is None
                and not final.username and not final.password and not final.fragment)

    @staticmethod
    def _check_local_path(path):
        path = Path(path).absolute()
        chain = list(reversed((path, *path.parents)))
        for item in chain:
            try:
                info = item.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                raise TransportError("unsafe_download_path")

    @staticmethod
    def _digest_file(path):
        digest = hashlib.sha256()
        with open(path, "rb") as src:
            for chunk in iter(lambda: src.read(_CHUNK), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def download(self, url, destination, expected_sha256, expected_bytes, progress=None):
        """Tải có thử lại: lỗi mạng tạm thời thì chờ (1, 2, 4... tối đa 30 s) rồi tải
        tiếp từ ``.part``; có tiến triển thì đếm lại số lần thử."""
        attempt, last_have = 0, -1
        partial = Path(destination).with_name(Path(destination).name + ".part")
        while True:
            try:
                return self._download_once(url, destination, expected_sha256, expected_bytes, progress)
            except TransportError as exc:
                if str(exc) not in _RETRYABLE:
                    raise
                have = partial.stat().st_size if partial.is_file() else 0
                attempt = 0 if have > last_have else attempt + 1
                last_have = have
                if attempt >= self.download_retries:
                    raise
                self.sleep(min(30, 2 ** attempt))

    def _download_once(self, url, destination, expected_sha256, expected_bytes, progress=None):
        if not _allowed_url(url, package=True):
            raise TransportError("package_url_rejected")
        if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256) or type(expected_bytes) is not int or not 1 <= expected_bytes <= _MAX_ASSET:
            raise ValueError("invalid_expected_artifact")
        target = Path(destination)
        partial = target.with_name(target.name + ".part")
        self._check_local_path(target.parent)
        self._check_local_path(target)
        self._check_local_path(partial)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._check_local_path(target.parent)
        if target.is_file() and target.stat().st_size == expected_bytes and self._digest_file(target) == expected_sha256:
            return target
        offset = partial.stat().st_size if partial.is_file() else 0
        if offset == expected_bytes:
            if self._digest_file(partial) == expected_sha256:
                self._check_local_path(target)
                os.replace(partial, target)
                return target
            partial.unlink()
            offset = 0
        if offset > expected_bytes:
            partial.unlink()
            offset = 0
        disk_root = target.parent
        while not disk_root.exists():
            disk_root = disk_root.parent
        if shutil.disk_usage(disk_root).free < expected_bytes - offset:
            raise TransportError("insufficient_download_space")
        headers = {"User-Agent": "KidTE-Updater/2", "Accept": "application/octet-stream", "Accept-Encoding": "identity"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            response = self.opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 416 and offset:
                self._check_local_path(partial)
                partial.unlink(missing_ok=True)
                return self._download_once(url, target, expected_sha256, expected_bytes, progress)
            raise TransportError(f"package_http_{exc.code}") from exc
        except (OSError, TimeoutError) as exc:
            raise TransportError(_network_code(exc)) from exc
        with response:
            if not self._check_final_url(response):
                raise TransportError("package_redirect_rejected")
            status = getattr(response, "status", None) or response.getcode()
            if offset and status == 200:
                offset = 0
            elif status == 206:
                match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                if not match or int(match.group(1)) != offset or int(match.group(2)) < offset or int(match.group(3)) != expected_bytes:
                    raise TransportError("package_content_range_invalid")
            elif offset:
                raise TransportError("package_resume_rejected")
            if not offset and status == 206:
                match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                if not match or int(match.group(1)) != 0 or int(match.group(2)) < 0 or int(match.group(3)) != expected_bytes:
                    raise TransportError("package_content_range_invalid")
            if not offset and status not in {200, 206}:
                raise TransportError("package_http_error")
            mode = "ab" if offset else "wb"
            total = offset
            chunk_total = 0
            with open(partial, mode) as out:
                while True:
                    try:
                        chunk = response.read(_CHUNK)
                    except Exception as exc:  # noqa: BLE001 - every read failure becomes a named code
                        import http.client
                        if not isinstance(exc, (OSError, TimeoutError, http.client.HTTPException)):
                            raise
                        out.flush()
                        raise TransportError(_network_code(exc)) from exc
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > expected_bytes:
                        out.close()
                        partial.unlink(missing_ok=True)
                        raise TransportError("package_too_large")
                    out.write(chunk)
                    chunk_total += len(chunk)
                    if progress:
                        progress(total, expected_bytes)
                out.flush()
                os.fsync(out.fileno())
        if status == 206:
            match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
            if match and chunk_total != int(match.group(2)) - int(match.group(1)) + 1:
                raise TransportError("package_size_mismatch")
        if total != expected_bytes:
            raise TransportError("package_size_mismatch")
        if self._digest_file(partial) != expected_sha256:
            self._check_local_path(partial)
            partial.unlink(missing_ok=True)
            raise TransportError("package_digest_mismatch")
        self._check_local_path(target)
        os.replace(partial, target)
        return target
