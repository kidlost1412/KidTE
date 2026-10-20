"""Nhật ký chẩn đoán captcha trình duyệt TikTok (tiện ích gửi lô lên, ghi JSONL có xoay vòng).

Chỉ nhận danh sách trường cho phép, cắt ngắn, che mọi thứ giống token/cookie. KHÔNG ghi
nội dung chat hay định danh khách: tiện ích chỉ gửi độ dài + hash, và backend bỏ mọi
trường lạ.
"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

from console_io import rotate_file

FILE_NAME = "tiktok-browser.jsonl"
MAX_BYTES = 2 * 1024 * 1024
BACKUPS = 2
MAX_ENTRIES_PER_REQUEST = 200
MAX_LINE_BYTES = 4096
_LOCK = threading.Lock()

_SCALARS = {"t": 14, "kind": 16, "name": 32, "phase": 12, "op": 80, "blocked": 24, "sig": 24,
            "code": 24, "hash": 16, "reason": 60, "url": 200, "msg": 120}
_INTS = {"tab", "calls", "ms", "len", "from", "to", "step", "shown_ms"}
_BOOLS = {"user", "ok", "visible", "on", "fail_open"}
_SECRET = re.compile(
    r"(ms_?token|token|cookie|authorization|bearer|session_?id|sid_\w+|csrf|secret|password|"
    r"x-bogus|a_bogus|signature)\s*[=:]\s*\S+", re.I)
_LONG_BLOB = re.compile(r"[A-Za-z0-9_\-+=]{32,}")


def _text(value, limit):
    text = " ".join(str(value).split())[:limit * 2]
    text = _SECRET.sub("[x]", text)
    text = _LONG_BLOB.sub("[x]", text)
    return text[:limit]


def _clean_url(value):
    text = str(value)
    for sep in ("?", "#"):
        text = text.split(sep, 1)[0]
    return _text(text, _SCALARS["url"])


def _summary(raw):
    if not isinstance(raw, dict):
        return None
    by_method = raw.get("by_method")
    clean = {"window_s": int(raw.get("window_s") or 0) if isinstance(raw.get("window_s"), (int, float)) else 0}
    for key in ("calls", "writes", "tabs"):
        value = raw.get(key)
        clean[key] = int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0
    clean["by_method"] = {}
    if isinstance(by_method, dict):
        for name, count in list(by_method.items())[:20]:
            if isinstance(count, (int, float)) and not isinstance(count, bool):
                clean["by_method"][_text(name, 40)] = int(count)
    return clean


def sanitize_entry(raw):
    """Trả dict đã lọc hoặc None nếu không hợp lệ."""
    if not isinstance(raw, dict) or not isinstance(raw.get("kind"), str):
        return None
    out = {}
    for key, value in raw.items():
        if key in _SCALARS:
            if isinstance(value, bool) or value is None or isinstance(value, (dict, list)):
                continue
            if key == "t":
                if isinstance(value, (int, float)):
                    out[key] = int(value)
                continue
            out[key] = _clean_url(value) if key == "url" else _text(value, _SCALARS[key])
        elif key in _INTS:
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                out[key] = int(value)
        elif key in _BOOLS:
            if isinstance(value, bool):
                out[key] = value
        elif key == "summary":
            summary = _summary(value)
            if summary is not None:
                out[key] = summary
    if "t" not in out or not out.get("kind"):
        return None
    return out


def append(directory, payload, *, max_bytes=MAX_BYTES, backups=BACKUPS):
    """Ghi lô ``payload['entries']`` vào ``<directory>/tiktok-browser.jsonl``."""
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise ValueError("invalid_entries")
    lines, dropped = [], 0
    for raw in entries[:MAX_ENTRIES_PER_REQUEST]:
        clean = sanitize_entry(raw)
        line = json.dumps(clean, ensure_ascii=False, separators=(",", ":")) if clean else ""
        if not line or len(line.encode("utf-8")) > MAX_LINE_BYTES:
            dropped += 1
            continue
        lines.append(line)
    dropped += max(0, len(entries) - MAX_ENTRIES_PER_REQUEST)
    if lines:
        path = Path(directory) / FILE_NAME
        data = ("\n".join(lines) + "\n").encode("utf-8")
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size + len(data) > max_bytes:
                rotate_file(path, backups=backups)
            with open(path, "ab") as handle:
                handle.write(data)
    return {"ok": True, "written": len(lines), "dropped": dropped}
