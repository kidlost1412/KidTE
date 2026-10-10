"""Tải model: rớt mạng giữa file thì tự tải tiếp từ byte đang có, không bắt người dùng bấm tay."""
import io
import threading

import pytest

import whisper_models

DATA = bytes(range(256)) * 40  # 10 240 byte


class _Resp(io.BytesIO):
    def __init__(self, payload, status, fail_after=None):
        super().__init__(payload)
        self.status, self.fail_after, self.sent = status, fail_after, 0

    def read(self, n=-1):
        if self.fail_after is not None and self.sent >= self.fail_after:
            raise TimeoutError("The read operation timed out")
        chunk = super().read(min(n, 1000) if n and n > 0 else -1)
        self.sent += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _downloader(monkeypatch, fails):
    calls = []

    def fake_open(url, headers=None, timeout=30):
        start = int((headers or {}).get("Range", "bytes=0-")[6:-1] or 0)
        calls.append(start)
        fail = fails.pop(0) if fails else None
        return _Resp(DATA[start:], 206 if start else 200, fail)

    d = whisper_models.ModelDownloader()
    monkeypatch.setattr(d, "_open", fake_open)
    monkeypatch.setattr(threading.Event, "wait", lambda self, t=None: False)
    return d, calls


def test_resumes_after_timeouts_and_completes(tmp_path, monkeypatch):
    d, calls = _downloader(monkeypatch, [3000, 3000])
    dest = tmp_path / "model.bin"
    assert d._download("repo", "model.bin", len(DATA), dest, 0, threading.Event()) == len(DATA)
    assert dest.read_bytes() == DATA
    assert calls[0] == 0 and calls[1] > 0 and calls[2] > calls[1]


def test_gives_up_when_no_progress(tmp_path, monkeypatch):
    d, _ = _downloader(monkeypatch, [0] * 50)
    with pytest.raises(TimeoutError):
        d._download("repo", "model.bin", len(DATA), tmp_path / "model.bin", 0, threading.Event())
