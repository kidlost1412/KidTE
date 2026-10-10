import json
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from log_viewer import recent_logs


def test_recent_logs_filters_and_redacts(tmp_path):
    rows = [
        {"timestamp": "2026-01-01T00:00:00Z", "component": "core", "severity": "info", "event": "ready", "message": "ok"},
        {"timestamp": "2026-01-01T00:01:00Z", "component": "core", "severity": "error", "event": "failed", "message": "api_key=secret-value"},
    ]
    (tmp_path / "core.jsonl").write_text("\n".join(json.dumps(x) for x in rows), encoding="utf-8")
    result = recent_logs(tmp_path, component="core", severity="error")
    assert len(result["rows"]) == 1
    assert "secret-value" not in result["rows"][0]["message"]
