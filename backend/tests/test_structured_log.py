import json
import logging
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from structured_log import JsonFormatter, configure_logger, redact


def test_redacts_assignments_bearer_and_key_like_values():
    text = redact("api_key=abc123 token:xyz sk-abcdefghijk")
    assert "abc123" not in text and "xyz" not in text and "sk-abcdefghijk" not in text
    assert text.count("[REDACTED]") == 3


def test_json_log_has_required_fields_and_rotates(tmp_path):
    logger = configure_logger("test-component", tmp_path, max_bytes=120, backup_count=2)
    logger.info("API key=super-secret", extra={"event": "startup", "correlation_id": "c-1"})
    for index in range(5):
        logger.info("x" * 100 + str(index), extra={"event": "line"})
    for handler in logger.handlers:
        handler.flush()
    files = list(tmp_path.glob("test-component.jsonl*"))
    assert 1 <= len(files) <= 3
    rows = []
    for path in files:
        rows.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
    assert all({"timestamp", "component", "severity", "event", "correlation_id", "message"} <= set(row) for row in rows)
    assert "super-secret" not in "".join(path.read_text(encoding="utf-8") for path in files)


def test_formatter_redacts_exception_text():
    formatter = JsonFormatter()
    try:
        raise RuntimeError("authorization:secret-value")
    except RuntimeError:
        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed", (), sys.exc_info())
    rendered = formatter.format(record)
    assert "secret-value" not in rendered
    assert "[REDACTED]" in rendered
