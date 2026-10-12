"""Bounded, anonymous transport for public GitHub update releases."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

REPOSITORY = "kidlost1412/KidTE"
_ASSET_HOSTS = {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com", "github-releases.githubusercontent.com"}
_MAX_CATALOG = 2 * 1024 * 1024
_MAX_ASSET = 2 * 1024**3
_CHUNK = 256 * 1024


class TransportError(RuntimeError):
    pass


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
    def __init__(self, repository=REPOSITORY, opener=None, timeout=20, clock=time.monotonic, *, series="p2"):
        if repository != REPOSITORY:
            raise ValueError("unsupported_repository")
        self.repository = repository
        if series not in {"p2", "p3", "p4"}:
            raise ValueError("unsupported_catalog_series")
        self.series = series
        self.opener = opener or urllib.request.build_opener(_SafeRedirectHandler())
        self.timeout = timeout
        self.clock = clock
        self._etag = {}
        self._cache = {}
        self._retry_after = {}

    def fetch_catalog(self, channel="stable", cached=None):
        if channel not in {"stable", "beta"}:
            raise ValueError("unsupported_channel")
        if self.retry_delay(channel) > 0:
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
            self._retry_after[channel] = self.clock() + 60
            raise TransportError(f"catalog_http_{exc.code}") from exc
        except (OSError, TimeoutError) as exc:
            self._retry_after[channel] = self.clock() + 60
            raise TransportError("catalog_network_error") from exc
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
                return self.download(url, target, expected_sha256, expected_bytes, progress)
            raise TransportError("package_network_error") from exc
        except (OSError, TimeoutError) as exc:
            raise TransportError("package_network_error") from exc
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
                    chunk = response.read(_CHUNK)
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
