import json
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import support_report


def test_support_report_is_local_redacted_and_excludes_private_sources(tmp_path, monkeypatch):
    logs, reports = tmp_path / "logs", tmp_path / "reports"
    logs.mkdir()
    (logs / "core.jsonl").write_text(
        'api_key=sk-abcdefghijk cookie=session-secret email=a@example.com phone=0901234567',
        encoding="utf-8",
    )
    (logs / "raw_stream").mkdir()
    (logs / "raw_stream" / "private.jsonl").write_text("nội dung khách", encoding="utf-8")
    monkeypatch.setattr(support_report, "LOG_DIR", logs)
    monkeypatch.setattr(support_report, "REPORT_DIR", reports)
    monkeypatch.setattr(support_report, "_generated_documents", lambda *_: {"generated/runtime.json": {}})
    result = support_report.create_support_report(
        now=datetime(2026, 9, 13, tzinfo=timezone.utc),
    )
    with zipfile.ZipFile(result["path"]) as archive:
        names = archive.namelist()
        content = archive.read("logs/core.jsonl").decode("utf-8")
        manifest = json.loads(archive.read("manifest.json"))
    assert "raw_stream/private.jsonl" not in names
    assert "session-secret" not in content and "a@example.com" not in content
    assert "0901234567" not in content and "sk-abcdefghijk" not in content
    assert manifest["sent"] is False


def test_preview_says_report_will_not_be_sent():
    preview = support_report.preview_support_report()
    assert preview["will_send"] is False
    assert ".env" in preview["excluded"]
