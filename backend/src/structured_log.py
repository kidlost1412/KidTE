"""Logging có cấu trúc, xoay vòng và loại bí mật cho Kid TE."""
from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path


_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_ -]?key|authorization|bearer|token|secret)\b\s*[:=]\s*([^\s,;]+)"
)
_KEY_LIKE = re.compile(r"(?i)\bsk-[a-z0-9_-]{8,}\b")
# Các dạng khoá phổ biến không có "api_key=" đứng trước (B8): Gemini (AIza...),
# token bot Telegram (123456:AA...), GitHub, Slack, AWS, JWT.
_KNOWN_KEYS = re.compile(
    r"AIza[0-9A-Za-z_-]{30,}"
    r"|(?<![\w:])\d{6,12}:[A-Za-z0-9_-]{30,}"
    r"|\b(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{30,}"
    r"|\bgithub_pat_[A-Za-z0-9_]{30,}"
    r"|\bxox[abpors]-[A-Za-z0-9-]{10,}"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
)


def redact(value) -> str:
    text = str(value)
    text = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    text = _KNOWN_KEYS.sub("[REDACTED]", text)
    return _KEY_LIKE.sub("[REDACTED]", text)


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "component": getattr(record, "component", record.name),
            "severity": record.levelname.lower(),
            "event": getattr(record, "event", "message"),
            "correlation_id": getattr(record, "correlation_id", ""),
            "message": redact(record.getMessage()),
        }
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logger(component: str, log_dir: Path, *, max_bytes=5_000_000,
                     backup_count=5, console=True) -> logging.Logger:
    logger = logging.getLogger(f"kid-te.{component}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if getattr(logger, "_kid_te_configured", False):
        return logger
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    formatter = JsonFormatter()
    file_handler = RotatingFileHandler(
        Path(log_dir) / f"{component}.jsonl", maxBytes=max_bytes,
        backupCount=backup_count, encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    if console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
    logger._kid_te_configured = True
    return logger
